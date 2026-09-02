"""数值验证状态（技能自包含版）：给执行结果盖章，未验证的数值禁止进入论文。

状态档位（优先级从高到低）：
  verified_human      人工确认/人工填写的真值（来自 HIL 闸门或 verified_results 文本）
  verified_crosscheck 两种独立方法结果一致（预留，暂未启用）
  verified_metrics    代码真实运行回读（metrics.json），但无 ground truth 对照（默认）
  unverified          未跑通 / LLM 估计 / 被人工拒绝（禁止进入论文）

本文件是平台 utils/verification.py 的裁剪版：仅保留审计器所需部分。
"""
from __future__ import annotations

import re

STATUS_HUMAN = "verified_human"
STATUS_CROSSCHECK = "verified_crosscheck"
STATUS_METRICS = "verified_metrics"
STATUS_UNVERIFIED = "unverified"

VERIFIED = {STATUS_HUMAN, STATUS_CROSSCHECK, STATUS_METRICS}

# 数字（含科学计数法，如 2.82e-24、-3.4e+2）
_NUM_RE = r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"

# 键值行：键（中文/字母/数字/括号/点/连字符）[:=] 可选"人工值"前缀 + 数字
_KV_RE = re.compile(
    r"([\w\u4e00-\u9fff()（）.\-]+)\s*[:=]\s*"
    r"(?:人工值|人工验证值|人工验证结果)?\s*(" + _NUM_RE + r")"
)

# 表格解析的数据行数上限（结果表通常很小；原始数据表不产生真值）
_MAX_TABLE_ROWS = 50


def _to_float(s: str) -> float | None:
    try:
        return float(s)
    except ValueError:
        return None


def parse_verified_refs(text: str) -> dict[str, float]:
    """从已验证结果文本解析参考真值 {键: 数值}。

    支持三种格式（与平台 DataAuditor/Writer 共用）：
      1. 裸键值行：  ``RMSE=15.2`` 或 ``厚度: 10.471``
      2. HIL 人工行：``子问题1: 42.5``（兼容"子问题1（xx）: 人工值 42.5"）
      3. 验证 CSV 表格文本（pandas to_string 输出）：
            厚度计算结果:
                  附件  材料  折射率  入射角  厚度_um
            附件1.xlsx SiC 3.40 10° 10.471
            解析为 ``厚度计算结果.附件1.xlsx.厚度_um=10.471`` 等（非数值列跳过）
    """
    refs: dict[str, float] = {}

    label = ""                # 当前表格标签（来自 "文件名:" 行）
    table_header: list[str] | None = None
    expect_header = False     # 标签行后第一行视为表头
    table_rows = 0            # 当前表格已解析数据行数（上限保护）

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            table_header = None
            expect_header = False
            table_rows = 0
            continue

        # 标签行：xxx: （冒号结尾且冒号后无数字，如 "厚度计算结果:"）
        if line.endswith(":") and not re.search(r"[:=]\s*" + _NUM_RE, line):
            label = line[:-1].strip()
            table_header = None
            expect_header = True
            table_rows = 0
            continue

        # 表格模式：标签行后首行（≥2列）为表头，之后按列位置配对
        if expect_header:
            expect_header = False
            if len(line.split()) >= 2:
                table_header = line.split()
            continue
        if table_header is not None:
            tokens = line.split()
            if len(tokens) == len(table_header):
                table_rows += 1
                if table_rows > _MAX_TABLE_ROWS:
                    # 超大表格（原始数据而非结果表）只取前 N 行，避免 refs 爆炸
                    continue
                # 行标识：取首个非数值列（如附件1.xlsx），全数值行退回行号。
                # 多行表格每列有多个真值，必须按行建键，否则 dict 键被覆盖只剩最后一行
                row_id = ""
                for tok in tokens:
                    if _to_float(tok) is None:
                        row_id = tok
                        break
                if not row_id:
                    row_id = f"row{len(refs) + 1}"
                for h, tok in zip(table_header, tokens):
                    # 表头为数字（如年份列）或无表头时跳过
                    if not h or _to_float(h) is not None:
                        continue
                    num = _to_float(tok)
                    if num is None:
                        continue
                    key = f"{label}.{row_id}.{h}" if label else f"{row_id}.{h}"
                    refs[key] = num
                continue

        # 裸键值 / HIL 人工行
        m = _KV_RE.match(line)
        if m:
            num = _to_float(m.group(2))
            if num is not None:
                refs[m.group(1).strip()] = num

    return refs


def is_verified(status) -> bool:
    """状态是否属于已验证档位。"""
    return bool(status) and status in VERIFIED


def status_of_execution(execution: dict) -> str:
    """判定执行记录的验证状态。

    显式标记优先；否则 status=ok 且有指标 → verified_metrics；否则 unverified。
    """
    explicit = execution.get("verification_status")
    if explicit:
        return explicit if explicit in VERIFIED | {STATUS_UNVERIFIED} else STATUS_UNVERIFIED
    if execution.get("status") == "ok" and (
        execution.get("metrics_json") or execution.get("metrics", {}).get("numbers")
    ):
        return STATUS_METRICS
    return STATUS_UNVERIFIED
