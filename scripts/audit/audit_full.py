"""完整审计闸门：9 确定性审计器 + 质量门控（零 LLM、零平台依赖）。

平台 QualityGateAgent 的自包含运行器。与快速自检 audit_runner.py 的分工：
  - audit_runner.py：结构/物理/数学三维 + 数字抽取，写作过程中随手自检
  - audit_full.py ：交付前闸门，9 审计器全集 + 门控判定（与平台同口径）

用法：
    python audit_full.py 论文.md
    python audit_full.py 论文.md --metrics metrics.json     # 代码输出指标比对（激活代码审计）
    python audit_full.py 论文.md --verified refs.txt        # 人工真值比对（激活数据审计）
    python audit_full.py 论文.md --json report.json -o 自定义报告.md

通过标准（与平台一致）：每个激活审计器分数 > 8.0 且综合分 > 9.0；
占位符命中硬红线直接不通过。退出码：0 = 通过，1 = 未通过。

依赖：仅本目录模块 + Python 标准库。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from auditors import QualityGateAgent  # noqa: E402

# (键, 中文名)——顺序即报告展示顺序
_AUDITOR_NAMES = [
    ("logic", "逻辑"), ("data", "数据"), ("format", "排版"),
    ("physics", "物理"), ("math", "数学"), ("figure", "图表"),
    ("language", "语言"), ("code", "代码"), ("number", "数字"),
]

THRESHOLD = 8.0      # 单项阈值（auditor.score_threshold）
OVERALL_THRESHOLD = 9.0  # 综合阈值（auditor.overall_threshold）


def load_summary(metrics_path: str, verified_path: str) -> dict:
    """把可选的本地证据文件装配成平台 summary 上下文。"""
    summary: dict = {"executions": []}
    if metrics_path:
        data = json.loads(Path(metrics_path).read_text(encoding="utf-8"))
        # 兼容三种形态：平台执行记录列表 / 含 executions 的 summary / 裸 {指标: 值}
        if isinstance(data, list):
            summary["executions"] = data
        elif isinstance(data, dict) and "executions" in data:
            summary = data
        elif isinstance(data, dict):
            summary["executions"] = [{"status": "ok", "sub_problem": "main",
                                      "metrics_json": data}]
    if verified_path:
        summary["_verified_results"] = Path(verified_path).read_text(encoding="utf-8")
    return summary


def render_md(gate: dict, source: str) -> str:
    lines = [f"# 论文完整检测报告：{source}\n"]
    verdict = "✅ 通过" if gate["passed"] else "❌ 未通过"
    lines.append(f"## 门控结论：{verdict}（综合 {gate['overall']}/10）\n")
    lines.append(f"> 通过标准：每个激活审计器 >{THRESHOLD} 且综合 >{OVERALL_THRESHOLD}；"
                 "占位符为硬红线（命中直接不通过）。\n")

    scores: dict = gate["scores"]
    details: dict = gate["details"]
    lines.append("| 审计器 | 分数 | 状态 |")
    lines.append("|---|---|---|")
    for key, name in _AUDITOR_NAMES:
        res = details[key]
        score = scores.get(key)
        if res.get("skipped"):
            lines.append(f"| {name} | — | 跳过 |")
        else:
            mark = "✅" if (score is not None and score > THRESHOLD) else "❌"
            n = len(res.get("issues", []))
            lines.append(f"| {name} | {score} | {mark}（{n} 项异常） |")
    lines.append("")

    lines.append("## 异常清单\n")
    lines.append("```")
    lines.append(gate["feedback"])
    lines.append("```")

    by_section = gate.get("feedback_by_section") or {}
    if by_section:
        lines.append("\n## 分章节修改指引\n")
        for sec, txt in by_section.items():
            lines.append(f"### {sec}\n")
            lines.append("```")
            lines.append(txt)
            lines.append("```")

    lines.append("\n> 本报告为确定性规则引擎结果（零 LLM，同输入同结果）；"
                 "模型正确性与创新性仍需人工评审。")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="论文完整审计闸门（9 审计器 + 门控）")
    ap.add_argument("paper", help="论文 Markdown 文件")
    ap.add_argument("--metrics", default="",
                    help="metrics.json（代码输出指标，激活代码一致性审计）")
    ap.add_argument("--verified", default="",
                    help="真值文件（键值文本，激活数据真值比对审计）")
    ap.add_argument("--json", dest="json_out", default="", help="同时输出 JSON 报告")
    ap.add_argument("-o", dest="out", default="",
                    help="报告输出路径（默认 <论文>.完整检测报告.md）")
    args = ap.parse_args()

    paper_path = Path(args.paper)
    paper = paper_path.read_text(encoding="utf-8")
    summary = load_summary(args.metrics, args.verified)
    gate = QualityGateAgent().run(paper, summary)

    out_md = Path(args.out) if args.out else paper_path.with_suffix(".完整检测报告.md")
    out_md.write_text(render_md(gate, paper_path.name), encoding="utf-8")

    status = "通过" if gate["passed"] else "未通过"
    active = ", ".join(f"{k}={v}" for k, v in gate["scores"].items()) or "无激活审计器"
    print(f"完整检测报告: {out_md}")
    print(f"门控结论: {status}（综合 {gate['overall']}/10）")
    print(f"各审计器: {active}")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(gate, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        print(f"JSON: {args.json_out}")

    sys.exit(0 if gate["passed"] else 1)


if __name__ == "__main__":
    main()
