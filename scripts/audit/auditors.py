"""质量门控（确定性重构版·技能自包含版）：从"LLM 主观打分"改为"确定性校验 + 异常清单"。

根因（第1条）：让 LLM 审 LLM 存在同源幻觉——审核者与被审者共享同一幻觉空间，
只能抓"不一致"，抓不住"不正确"。故全部审核器均为**零 LLM 调用**的可复现规则检查。

本文件是平台 agents/audit.py 的自包含分叉，差异仅三处：
  1. 配置项内置于 DEFAULTS（平台从 utils.config / config.yaml 读取，默认值相同）
  2. 验证状态解析改用同目录 verification_lite.py（平台为 utils/verification.py）
  3. NumberAuditor 依赖平台证据注册表（evidence_chain），独立版默认跳过不计分

  LogicAuditor   : 结构化检查清单 + 占位符/负值/执行失败/假成功检测
  DataAuditor    : 内部数值自洽 + 参考真值比对（有真值时）
  FormatAuditor  : 规则引擎（公式编号/参考文献/图表编号/单位/标题层级/代码块闭合）
  PhysicsAuditor : 效率>100%/反射率>1/功率量级/负值/超光速/温度越界
  MathAuditor    : 符号表一致性/表格求和 vs 声称总和/代入验证
  FigureAuditor  : 幽灵图号/图号连续性/空表格/最少图表数
  LanguageAuditor: AI 套话密度/段落均匀性/填充词/连接词密度
  CodeAuditor    : 论文数字 vs metrics.json 代码输出（无执行上下文时跳过）

输出：通过/不通过 + 异常项清单 + 确定性合规分（0-10）。
分数不由 LLM 主观给出，同一论文每次审核结果完全一致。
"""
from __future__ import annotations

import re
from typing import Any

# ── 平台 utils.config 的自包含替代：与平台代码内默认值一致 ──
DEFAULTS: dict = {
    "auditor.score_threshold": 8.0,
    "auditor.overall_threshold": 9.0,
    "auditor.data_tolerance": 0.02,
    "auditor.code_tolerance": 0.02,
    "auditor.code_coverage_min": 0.6,
    "auditor.figure_min_count": 1,
    "auditor.table_min_count": 1,
    "auditor.language.enabled": True,
    "auditor.language.cliche_density_threshold": 3.0,
    "auditor.language.paragraph_cv_threshold": 0.25,
    "auditor.language.filler_ratio_threshold": 0.05,
    "auditor.language.transition_density_threshold": 5,
}


def get(key: str, default=None):
    """点号键查内置默认值（签名与平台 utils.config.get 一致）。"""
    return DEFAULTS.get(key, default)


from verification_lite import parse_verified_refs, VERIFIED  # noqa: E402

SECTIONS = [
    "摘要", "问题重述", "问题分析", "模型假设", "符号说明",
    "模型建立与求解", "灵敏度分析", "模型评价与推广", "参考文献", "全文",
]


def _clamp10(x: float) -> float:
    return max(0.0, min(10.0, round(x, 2)))


# ══════════════════════════════════════════════════════════
# 逻辑审核：结构化检查清单（确定性，无 LLM）
# ══════════════════════════════════════════════════════════
class LogicAuditor:
    """逻辑审核：检查论文是否包含通用建模必备要素，不打分、只列缺失项。

    清单为**题型无关**的通用要素（任何建模论文都应具备）；题型专用要素（如 FFT、
    多光束判据）不入清单，避免对不同题型误罚。
    """

    # (检查项, 判定该要素存在的正则)
    CHECKS = [
        ("摘要", r"摘要|Abstract"),
        ("问题重述/背景", r"问题重述|问题背景|问题的提出|重述"),
        ("模型假设", r"模型假设|假设|assumption"),
        ("模型建立", r"模型建立|数学模型|建立模型|模型构建"),
        ("求解过程与算法", r"求解|算法|步骤|最小二乘|拟合|回归"),
        ("拟合优度/误差指标", r"RMSE|R\^2|R²|误差|残差|AIC|BIC|F检验|MSE|MAE"),
        ("不确定度/置信区间", r"不确定度|置信区间|95%|±|\\pm|标准差"),
        ("结果分析与结论", r"结果分析|结论|讨论"),
        ("模型评价/推广", r"模型评价|灵敏度|稳健性|敏感性|推广"),
    ]

    def run(self, paper: str, summary: dict) -> dict:
        issues = []
        for name, pattern in self.CHECKS:
            if not re.search(pattern, paper, re.IGNORECASE):
                issues.append({
                    "problem": f"论文缺少必要要素：{name}",
                    "severity": "高",
                    "section": "全文",
                })
        # 占位符检测：表格中不应有"请填入"等占位文字
        placeholders = re.findall(r"\[请填入[^\]]*\]|TODO|FIXME|TBD|占位", paper)
        if placeholders:
            issues.append({
                "problem": f"论文包含 {len(placeholders)} 处占位符文字（如「{placeholders[0][:30]}」），需补充实际数值",
                "severity": "高",
                "section": "表格",
            })
        # 负值异常检测：需求量/价格/距离等物理量不应为负
        neg_patterns = re.findall(
            r"(需求量|需求|价格|电价|距离|成本|收入|负荷)\s*[：:=]?\s*-[0-9]+\.?[0-9]*",
            paper
        )
        if neg_patterns:
            issues.append({
                "problem": f"发现 {len(neg_patterns)} 处物理量为负值（如「{neg_patterns[0][:30]}」），需在文中说明处理方式",
                "severity": "中",
                "section": "数值结果",
            })
        # 子问题失败检查：执行失败的子问题对应章节无真实数值支撑。
        # 存在性正则查不出这类"结构齐全但结果缺失"的论文（实测 Q4 失败
        # 时逻辑分 8.89 照常通过门控）——失败的求解无法靠重写论文修复，
        # 必须列为高严重度问题提示人工重跑求解
        execs = summary.get("executions") or []
        failed = [
            e for e in execs
            if isinstance(e, dict) and e.get("status") == "error"
        ]
        if failed:
            names = "；".join(
                (e.get("sub_problem", "") or "")[:20] for e in failed
            )
            issues.append({
                "problem": (
                    f"{len(failed)}/{len(execs)} 个子问题执行失败（{names}），"
                    "对应章节无真实数值支撑。此问题无法通过论文重写修复，"
                    "需人工检查失败原因（依赖缺失/数据契约/模型规模）后重跑求解"
                ),
                "severity": "高",
                "section": "全文",
            })
        # 假成功检测：status=ok 但数值可疑（体检告警）或未通过数值验证——
        # 结构齐全的论文照样可能数值全错（2025A 实测：遮蔽时长 0.0 被门控
        # 打了逻辑 10 分满分，完全掩盖物理错误）。此类结果必须降分，
        # 否则门控的"通过"是虚假信号
        suspicious = []
        for e in execs:
            if not isinstance(e, dict) or e.get("status") != "ok":
                continue
            sp = (e.get("sub_problem", "") or "")[:20]
            if e.get("sanity_issue"):
                suspicious.append(f"「{sp}」数值异常：{e['sanity_issue'][:60]}")
            elif e.get("verification_status") not in VERIFIED | {None}:
                # 用 VERIFIED 集合判定（verified_human/crosscheck/metrics 均为
                # 已验证档）——此前硬编码只认 verified_metrics，会把人工确认
                # 和交叉验证的结果误标为"未验证"
                suspicious.append(f"「{sp}」数值未验证（{e.get('verification_status')}）")
        if suspicious:
            issues.append({
                "problem": (
                    f"{len(suspicious)} 个子问题执行成功但数值可疑/未验证："
                    + "；".join(suspicious[:3])
                    + "。论文中的这些数值不可信，需修复求解代码后重跑"
                ),
                "severity": "高",
                "section": "数值结果",
            })
        score = _clamp10((len(self.CHECKS) - len(issues)) / len(self.CHECKS) * 10)
        return {
            "score": score,
            "issues": issues,
            "suggestions": [f"请修正：{i['problem']}" for i in issues],
            "auditor": "logic",
        }


# ══════════════════════════════════════════════════════════
# 数据审核：只做数值比对，参照真值，不打分
# ══════════════════════════════════════════════════════════
# 内部自洽检查：参与比对的指标键（含"总/最优/最终/最小"等总量词，
# 或以 距离/成本/惩罚/函数值/阈值 结尾）——排除"净需求/容量/车辆"这类
# 每站点一个值的多值指标，避免把站点数据表误报为矛盾
_CONSISTENCY_KEYS = re.compile(
    r"[\u4e00-\u9fff]{0,8}?(?:总|最优|最终|最小|最大)[\u4e00-\u9fff]{0,8}"
    r"(?:距离|成本|惩罚|函数值|阈值|车辆数|装卸量|调度量|路线|路径|需求)"
    r"|[\u4e00-\u9fff]{1,8}(?:距离|成本|惩罚|函数值|阈值)"
)


def _check_internal_consistency(paper: str) -> list[str]:
    """论文内部数值自洽：同一指标键出现多个不同数值 → 矛盾。

    不依赖人工真值，拦截"总行驶距离 46.69 与 51.15 并存"这类
    多子问题结果拼装矛盾（b2025 与共享单车题实测均出现）。
    标量与列表分开比较：同一列表重复出现（摘要/结论各抄一遍）不算矛盾。
    """
    body = paper.split("## 参考文献")[0]
    scalar_pairs: dict[str, list[float]] = {}
    list_pairs: dict[str, list[tuple]] = {}

    # 列表：如 "最终车辆数 [20, 7, 22, ...]" / "卡车路线：0-1-3-4-6-8-7-5-2-0"
    list_pat = re.compile(
        r"([\u4e00-\u9fff]{2,16}?)\s*[：:为是=]?\s*[\[【（(]\s*"
        r"(-?\d+(?:\.\d+)?(?:\s*[,，、\s]\s*-?\d+(?:\.\d+)?){3,})"
    )
    for m in list_pat.finditer(body):
        key = m.group(1)
        if not _CONSISTENCY_KEYS.fullmatch(key):
            continue
        vals = tuple(float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", m.group(2)))
        list_pairs.setdefault(key, []).append(vals)

    # 标量：键 + 数值，如 "总行驶距离 51.15 km" / "总行驶距离51.15" / "总成本为 51.1536"
    # 分隔符支持 空格/中文冒号/为/是/=/| 竖线（表格 "| 总行驶距离 | 46.693 |"），
    # 也允许 0 个分隔符（摘要里 "总行驶距离51.15" 键值直接相连）
    pat = re.compile(
        r"([\u4e00-\u9fff]{2,16}?)\s*[\s：:为是=|\uFF5C]*\s*"
        r"(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    )
    for line in body.splitlines():
        # 摘要行常同时含列表（"最终车辆数为[20,7,...]，总行驶距离51.15 km"），
        # 只剔除方括号字符（列表由 list_pat 处理），不能跳过整行
        for m in pat.finditer(line.replace("[", " ").replace("]", " ")):
            key = m.group(1)
            if not _CONSISTENCY_KEYS.fullmatch(key):
                continue
            try:
                v = float(m.group(2))
            except ValueError:
                continue
            scalar_pairs.setdefault(key, []).append(v)

    issues: list[str] = []
    for key, vals in scalar_pairs.items():
        if len(vals) < 2:
            continue
        uniq = sorted({round(v, 4) for v in vals})
        if len(uniq) < 2:
            continue
        base = max(abs(uniq[-1]), abs(uniq[0]), 1e-9)
        if abs(uniq[-1] - uniq[0]) / base > 0.02:  # 相对偏差 > 2%
            issues.append(
                f"论文内部数值不自洽：「{key}」出现多个不同值 {uniq[:6]}"
                "（多子问题结果拼装矛盾，需统一为同一来源）"
            )
    for key, lists in list_pairs.items():
        if len(lists) < 2:
            continue
        uniq = list({lst for lst in lists})
        if len(uniq) < 2:
            continue
        issues.append(
            f"论文内部数值不自洽：「{key}」出现多个不同取值 {[list(l) for l in uniq[:3]]}"
            "（多子问题结果拼装矛盾，需统一为同一来源）"
        )
    return issues


class DataAuditor:
    """数据审核：论文内部自洽检查（始终） + 与参考真值比对（有真值时）。"""

    def run(self, paper: str, summary: dict) -> dict:
        tol = float(get("auditor.data_tolerance", 0.02))
        # 提取论文数值（含科学计数法，如 2.82e-24）。预处理剔除无关数字噪声：
        #  - 参考文献区域（年份）
        #  - 括号公式编号 (4.1)
        #  - LaTeX 上标（cm^{-1} 的 -1、e^{-2iδ} 的 -2，会被负值硬伤检测误报）
        #  - 数字-数字 区间/编号（表 7-1、[1-2]、界面（1-2）），连字符拆成空格
        body = paper.split("## 参考文献")[0]
        body = re.sub(r"\(\s*\d+(?:\.\d+)?\s*\)", " ", body)
        body = re.sub(r"\^\{-?\d+(?:\.\d+)?[^}]*\}", " ", body)
        body = re.sub(r"\d+-\d+", lambda m: m.group(0).replace("-", " "), body)
        # 负百分比（-10% / $-10\%$ 灵敏度幅度）是"下降幅度"而非物理量符号错误，去掉负号
        body = re.sub(r"-(\d+(?:\.\d+)?\\?%)", r"\1", body)
        paper_floats = [float(m) for m in re.findall(r"-?\d+\.?\d*(?:[eE][+-]?\d+)?", body)]
        # 负值硬伤检测的候选：按行提取，跳过含 % 的行——灵敏度表格的变化幅度列
        # （-4.76 等）所在行必有 $+5\%$ 之类的百分比标记，负物理量行（-64.53 μm）没有。
        # 另跳过"正负配对"行（+5.0 / -5.00 绝对值相等）：灵敏度表的变化输入/输出成对
        # 出现且不带 % 号（表头注明），不是物理量符号错误。不能用章节过滤：LLM 可能
        # 把求解表格放进"灵敏度分析"章节。
        neg_candidates: list[float] = []
        for line in body.splitlines():
            if "%" in line or "\\%" in line:
                continue
            nums = [float(m) for m in re.findall(r"-?\d+\.?\d*(?:[eE][+-]?\d+)?", line)]
            negs = [n for n in nums if n < 0]
            if not negs:
                continue
            positives = {abs(n) for n in nums if n > 0}
            for n in negs:
                if not any(abs(abs(n) - p) < 1e-9 for p in positives):
                    neg_candidates.append(n)

        # ── 论文内部自洽检查（不依赖真值，始终运行）──
        contradictions = _check_internal_consistency(paper)

        # ── 与参考真值比对（无真值时跳过比对，内部自洽矛盾仍参与评分）──
        refs = self._extract_refs(summary)
        if refs:
            found = 0
            neg_conflicts: dict[float, tuple[str, float]] = {}  # 负值 v -> (最近的键, ref)
            for key, ref in refs.items():
                ref_abs = max(abs(ref), 1e-6)
                matched = any(abs(v - ref) <= ref_abs * tol for v in paper_floats)
                if matched:
                    found += 1
                else:
                    # 未命中真值：找论文中与 ref 最近的数值；偏差超过容差且同数量级
                    # （比值 0.1~10）一律标记。旧逻辑只查 [tol, 10*tol] 窗口，偏差 >20%
                    # 的大错误反而无声放行。附建议修正值，给重写 LLM 明确指令。
                    if paper_floats:
                        v = min(paper_floats, key=lambda x: abs(x - ref))
                        ratio = v / ref_abs
                        if abs(v - ref) > ref_abs * tol and 0.1 <= ratio <= 10:
                            contradictions.append(
                                f"{key}={ref:.4g} vs 论文最近值 {v:.4g}"
                                f"（偏差{abs(v - ref) / ref_abs * 100:.1f}%，"
                                f"建议修正为 {ref:.4g}）"
                            )
                    continue
                # 硬伤检测：论文已引用真值（matched）时，仍检查是否存在**负值**错误。
                # 物理量真值为正、论文中出现同数量级的负值（如 -64.53 vs 7.581）是
                # 符号/公式硬伤，即使论文同时抄了真值也必须标记，不能被 found 掩护。
                # 候选已排除含 % 的行（变化幅度）；窗口 [0.5, 10] 排除 -1（cm^{-1} 上标、
                # 表 7-1 编号、[1-2] 文献区间等小量噪声）。
                for v in neg_candidates:
                    if (
                        v < 0 < ref
                        and abs(v - ref) > ref_abs * tol
                        and 0.5 <= abs(v) / ref_abs <= 10
                    ):
                        # 每个负值只报与【最接近真值】的一条矛盾（负值常同时与多个
                        # 同量级真值冲突，报多条会让重写 LLM 不知道该改成哪个值）
                        cur = neg_conflicts.get(v)
                        if cur is None or abs(v - ref) < abs(v - cur[1]):
                            neg_conflicts[v] = (key, ref)
                        break
            for v, (key, ref) in neg_conflicts.items():
                ref_abs = max(abs(ref), 1e-6)
                contradictions.append(
                    f"{key}={ref:.4g} vs 论文负值 {v:.4g}"
                    f"（疑似符号错误，建议修正为 {ref:.4g}）"
                )
        else:
            found = 0

        if not refs and not contradictions:
            return {
                "score": None,
                "issues": [{"problem": "【已跳过】无参考真值且论文内部数值自洽，"
                                      "数据审核不参与评分。",
                            "severity": "中", "section": "全文"}],
                "suggestions": [],
                "auditor": "data",
                "skipped": True,
            }

        issues: list[dict] = []
        if contradictions:
            issues.append({
                "problem": "存在疑似数值矛盾：" + "；".join(contradictions[:3]),
                "severity": "高", "section": "模型建立与求解",
            })
        if refs:
            ratio = found / max(len(refs), 1)
            if ratio < 0.3:
                issues.append({
                    "problem": f"论文仅反映 {found}/{len(refs)} 个求解关键值，与求解结果脱节",
                    "severity": "高", "section": "模型建立与求解",
                })
        score = _clamp10(10.0 - 3.0 * len(issues))
        return {
            "score": score,
            "issues": issues,
            "suggestions": [f"请核对：{i['problem']}" for i in issues],
            "auditor": "data",
            # 数值一致性验收明细（供 verify_report 使用）
            "refs_total": len(refs),
            "refs_found": found,
            "contradictions": contradictions,
            "ref_keys": list(refs.keys()),
        }

    @staticmethod
    def _extract_refs(summary: dict) -> dict[str, float]:
        """提取参考真值：仅人工/独立验证结果（_verified_results）。

        设计意图（DESIGN_数值可靠性改造.md）：
          数据审核优先级 = 人工值(verified_human) > verified_metrics > 缺省跳过。
        verified_metrics 只是"代码真实运行"的状态标记，**不作为审核参考**——
        把它当参考会把"论文未引用某次运行的输出"误判为矛盾，且多子问题各自
        给出不同最优解（合法多解）时审核标准不可达，重写永远无法通过。
        无人工真值时数据审核跳过（不评分，逻辑/排版照常）。
        """
        verified = summary.get("_verified_results") or ""
        if not isinstance(verified, str):
            return {}
        return parse_verified_refs(verified)


# ══════════════════════════════════════════════════════════
# 排版审核：规则引擎（确定性，无 LLM）
# ══════════════════════════════════════════════════════════
class FormatAuditor:
    """排版审核：正则规则检查格式规范，不靠 LLM。"""

    def run(self, paper: str, summary: dict) -> dict:
        issues = []
        # 1. 公式编号：存在形如 (数字.数字) 的编号，且无重复
        nums = re.findall(r"\(\s*\d+(?:\.\d+)?\s*\)", paper)
        if not nums:
            issues.append({"problem": "未检测到公式编号（如 (4.1)）", "severity": "中", "section": "全文"})
        elif len(nums) != len(set(nums)):
            issues.append({"problem": "存在重复公式编号", "severity": "中", "section": "全文"})

        # 2. 参考文献：存在 [n] 引用，且含年份（19xx/20xx）
        refs = re.findall(r"\[\d+\]", paper)
        if not refs:
            issues.append({"problem": "正文缺少参考文献引用标记 [n]", "severity": "高", "section": "参考文献"})
        if not re.search(r"19\d{2}|20\d{2}", paper):
            issues.append({"problem": "参考文献中未检测到年份", "severity": "中", "section": "参考文献"})

        # 3. 图表编号与标题
        tables = re.findall(r"表\s?\d+", paper)
        figs = re.findall(r"图\s?\d+", paper)
        if not tables:
            issues.append({"problem": "未检测到表格（表N）", "severity": "中", "section": "全文"})
        if not figs:
            issues.append({"problem": "未检测到插图（图N）", "severity": "中", "section": "全文"})

        # 4. 单位规范：μm 使用规范，检测裸 um
        if re.search(r"(?<![μu])\bum\b", paper):
            issues.append({"problem": "存在不规范单位写法 'um'（应写 μm）", "severity": "低", "section": "全文"})
        if not re.search(r"μm", paper):
            issues.append({"problem": "未检测到 μm 单位写法", "severity": "低", "section": "全文"})

        # 5. 标题层级
        if not re.search(r"^## ", paper, re.MULTILINE):
            issues.append({"problem": "缺少二级标题（##）", "severity": "高", "section": "全文"})
        if not re.search(r"^### ", paper, re.MULTILINE):
            issues.append({"problem": "缺少三级标题（###）", "severity": "中", "section": "全文"})

        # 6. 代码块闭合
        if paper.count("```") % 2 != 0:
            issues.append({"problem": "Markdown 代码块未闭合（``` 数量为奇数）", "severity": "中", "section": "全文"})

        total = 6
        # 计算通过项：上述每个问题归为一个"检查族"，存在高严重度问题的族计为未通过
        high = sum(1 for i in issues if i["severity"] == "高")
        score = _clamp10((total - high) / total * 10)
        return {
            "score": score,
            "issues": issues,
            "suggestions": [f"请修正：{i['problem']}" for i in issues],
            "auditor": "format",
        }


# ══════════════════════════════════════════════════════════
# 总审：聚合三审，输出 通过/不通过 + 异常清单
# ══════════════════════════════════════════════════════════
class NumberAuditor:
    """数字比对审计器：论文数字 vs 证据注册表。

    扫描论文中所有数值，与代码执行产出的 metrics.json 对比，
    自动标记不一致、未引用、未匹配的数字。
    """

    def run(self, paper: str, summary: dict) -> dict:
        # 技能独立版：论文数字与代码输出的比对由 CodeAuditor 承担（metrics.json），
        # 证据注册表（evidence_chain）为平台组件，独立版默认跳过不计分
        # （与平台无执行上下文时的行为一致：score=None 不参与均分）
        return {
            "score": None,
            "issues": [{"problem": "【已跳过】技能独立版无证据注册表，数字比对审计不参与评分",
                        "severity": "低", "section": "全文"}],
            "suggestions": [],
            "auditor": "number",
            "skipped": True,
        }


class QualityGateAgent:
    """质量门控总审（确定性）。保留 scores/overall 字段以兼容流水线，
    但分数为合规分（可复现），核心输出为异常项清单。"""

    def __init__(self) -> None:
        self.logic = LogicAuditor()
        self.data = DataAuditor()
        self.format = FormatAuditor()
        # 新增审核器
        self.physics = PhysicsAuditor()
        self.math = MathAuditor()
        self.figure = FigureAuditor()
        self.language = LanguageAuditor()
        self.code = CodeAuditor()
        self.number = NumberAuditor()

    def run(self, paper: str, summary: dict) -> dict:
        threshold = float(get("auditor.score_threshold", 8.0))
        overall_threshold = float(get("auditor.overall_threshold", 9.0))

        logic_res = self.logic.run(paper, summary)
        data_res = self.data.run(paper, summary)
        format_res = self.format.run(paper, summary)
        # 新增审核器结果
        physics_res = self.physics.run(paper, summary)
        math_res = self.math.run(paper, summary)
        figure_res = self.figure.run(paper, summary)
        language_res = self.language.run(paper, summary)
        code_res = self.code.run(paper, summary)
        number_res = self.number.run(paper, summary)

        data_score = None if data_res.get("skipped") else data_res["score"]
        lang_score = None if language_res.get("skipped") else language_res["score"]
        code_score = None if code_res.get("skipped") else code_res["score"]
        number_score = None if number_res.get("skipped") else number_res["score"]

        scores = {
            "logic": logic_res["score"],
            "data": data_score,
            "format": format_res["score"],
            "physics": physics_res["score"],
            "math": math_res["score"],
            "figure": figure_res["score"],
            "language": lang_score,
            "code": code_score,
            "number": number_score,
        }
        active = {k: v for k, v in scores.items() if v is not None}

        if active:
            overall = round(sum(active.values()) / len(active), 2)
        else:
            overall = 0.0
        passed = (
            all(v > threshold for v in active.values())
            and overall > overall_threshold
        )

        # ── 硬性红线：占位符/未填充数值直接不通过（不能靠分数稀释混过）──
        # 占位符意味着论文存在未完成内容，任何数量都视为未交付，必须重写
        hard_placeholders = re.findall(
            r"\[请填入[^\]]*\]|\[具体[^\]]*\]|占位符|待补充|待导入|TODO|FIXME|TBD"
            r"|图X\.|表X\.|图 ?X\d|表 ?X\d",
            paper,
        )
        if hard_placeholders:
            passed = False
            logic_res["issues"].append({
                "problem": f"论文包含 {len(hard_placeholders)} 处未填充占位符"
                           f"（如「{hard_placeholders[0][:40]}」），硬性红线：必须全部补充实际数值后才能通过",
                "severity": "高",
                "section": "全文",
            })
            logic_res["score"] = _clamp10(min(logic_res["score"], 5.0))

        details = {
            "logic": logic_res, "data": data_res, "format": format_res,
            "physics": physics_res, "math": math_res, "figure": figure_res,
            "language": language_res, "code": code_res, "number": number_res,
        }
        feedback = self._build_feedback(active, details, threshold)
        feedback_by_section = self._feedback_by_section(details)

        return {
            "passed": passed,
            "scores": active,
            "overall": overall,
            "overall_threshold_used": overall_threshold,
            "data_skipped": bool(data_res.get("skipped")),
            "details": details,
            "feedback": feedback,
            "feedback_by_section": feedback_by_section,
        }

    @staticmethod
    def _build_feedback(scores: dict, details: dict, threshold: float) -> str:
        """汇总异常项清单为反馈文本。"""
        name_map = {
            "logic": "逻辑", "data": "数据", "format": "排版",
            "physics": "物理", "math": "数学", "figure": "图表",
            "language": "语言", "code": "代码", "number": "数字",
        }
        prefix = {
            "logic": "L", "data": "D", "format": "F",
            "physics": "P", "math": "M", "figure": "G",
            "language": "Lg", "code": "C", "number": "Nu",
        }
        parts = []
        for key, name in name_map.items():
            res = details[key]
            score = scores.get(key)
            issues = res.get("issues", [])
            if score is None:
                parts.append(f"【{name}审核 已跳过】")
            else:
                passed_txt = "通过" if score > threshold else "未通过"
                parts.append(f"【{name}审核 {passed_txt}（合规分 {score}）共{len(issues)}个异常】")
            for idx, iss in enumerate(issues, 1):
                parts.append(
                    f"  - [{prefix[key]}{idx}] ({iss.get('severity', '中')}"
                    f"|{iss.get('section', '全文')}) {iss.get('problem', '')}"
                )
        return "\n".join(parts)

    @staticmethod
    def _feedback_by_section(details: dict) -> dict[str, str]:
        """按章节聚合异常项（兼容 Writer 按章节注入反馈）。"""
        prefix = {
            "logic": "L", "data": "D", "format": "F",
            "physics": "P", "math": "M", "figure": "G",
            "language": "Lg", "code": "C", "number": "Nu",
        }
        all_issues = []
        for key, res in details.items():
            if res.get("skipped"):
                continue
            for idx, iss in enumerate(res.get("issues", []), 1):
                all_issues.append({**iss, "code": f"[{prefix[key]}{idx}]"})

        by_section: dict[str, list] = {s: [] for s in SECTIONS}
        global_issues = [i for i in all_issues if i.get("section") == "全文"]
        for iss in all_issues:
            sec = iss.get("section", "全文")
            by_section.setdefault(sec, []).append(iss)

        result: dict[str, str] = {}
        for sec in SECTIONS:
            issues = by_section.get(sec, []) + global_issues
            seen = set()
            dedup = []
            for iss in issues:
                if iss["code"] in seen:
                    continue
                seen.add(iss["code"])
                dedup.append(iss)
            if dedup:
                lines = [f"【{sec}需修改的异常】"]
                lines += [f"  - {i['code']} ({i.get('severity','中')}) {i.get('problem','')}"
                          for i in dedup]
                result[sec] = "\n".join(lines)
        return result


# ══════════════════════════════════════════════════════════
# 物理常识审核：拦截违反物理定律/常识的数值（确定性，无 LLM）
# ══════════════════════════════════════════════════════════
class PhysicsAuditor:
    """物理常识审核：检查论文中的数值是否违反物理定律或常识。

    核心检查：
    1. 效率/转化率超过 100%（违反热力学第二定律）
    2. 反射率/吸收率超过 1.0（违反能量守恒）
    3. 功率量级异常（如 20 面镜子发 60MW）
    4. 负物理量异常（温度变化、误差等允许为负，其他物理量需说明）
    5. 速度超光速
    6. 温度超出物理可能范围
    """

    # 负值白名单：这些物理量允许为负（关键词子串匹配）
    NEGATIVE_WHITELIST = [
        "温度变化", "误差", "偏差", "残差", "利润", "收益",
        "位移", "坐标", "海拔", "增益", "灵敏度变化", "变化率",
        "增长", "下降", "减少", "波动",
    ]

    def run(self, paper: str, summary: dict) -> dict:
        issues = []

        # ── 1. 效率/转化率超 100% ──
        efficiency_patterns = re.findall(
            r"(?:效率|转化率|转化效率|热效率|光学效率|吸收率|反射率|透射率)"
            r"[\s:：为达到]*?(\d+(?:\.\d+)?)\s*%",
            paper
        )
        for val_str in efficiency_patterns:
            val = float(val_str)
            if val > 100:
                issues.append({
                    "problem": f"效率/转化率 {val}% 超过 100%，违反热力学第二定律",
                    "severity": "高",
                    "section": "模型建立与求解",
                })
                break  # 只报第一个

        # ── 2. 反射率/吸收率超过 1.0 ──
        reflectivity_patterns = re.findall(
            r"(?:反射率|吸收率|透射率|反射系数)[\s:：为]*?(\d+(?:\.\d+)?)\s*(%|％)?",
            paper
        )
        for val_str, pct in reflectivity_patterns:
            val = float(val_str)
            if pct:  # 百分数形态（如"反射率 92%"）归一化为 0-1 比值再判界
                val /= 100.0
            if val > 1.0:
                issues.append({
                    "problem": f"反射率/吸收率 {val} 超过 1.0，违反能量守恒定律",
                    "severity": "高",
                    "section": "模型建立与求解",
                })
                break

        # ── 3. 功率量级异常检查（同行配对 + 同类去重）──
        # 功率必须与镜子数量、面积出现在同一行内才配对：全文交叉配对会把
        # 无关段落的"60MW"和"3 面镜子"错误组合成风暴式重复误报（实测 26 连报）
        power_flags: list[dict] = []
        for line in paper.splitlines():
            line_powers = re.findall(r"(\d+(?:\.\d+)?)\s*(?:MW|兆瓦)", line)
            line_counts = re.findall(r"(\d+(?:\.\d+)?)\s*(?:面|块|个)\s*(?:定日镜|镜子|反射镜)", line)
            line_areas = re.findall(
                r"(?:每面\s*)?(?:面积|镜面面积|单镜面积)[\s:：为每面]*?(\d+(?:\.\d+)?)\s*(?:m²|平方米|m\^2)",
                line
            )
            if not (line_powers and line_counts and line_areas):
                continue
            for power_str in line_powers:
                power_mw = float(power_str)
                for count_str in line_counts:
                    count = float(count_str)
                    for area_str in line_areas:
                        area = float(area_str)
                        # 假设最大光学效率 0.9，辐照度 1000 W/m²
                        max_possible_power_mw = count * area * 1000 * 0.9 / 1e6
                        if power_mw > max_possible_power_mw * 3:  # 允许 3 倍容差
                            power_flags.append({
                                "problem": (
                                    f"功率 {power_mw}MW 对于 {count:.0f} 面镜子（面积 {area}m²）"
                                    f"量级不合理（理论上限约 {max_possible_power_mw:.1f}MW），"
                                    "请检查镜子数量或功率单位是否正确"
                                ),
                                "severity": "高",
                                "section": "模型建立与求解",
                            })
        seen_flags: set[str] = set()
        uniq_flags: list[dict] = []
        for flag in power_flags:
            if flag["problem"] not in seen_flags:
                seen_flags.add(flag["problem"])
                uniq_flags.append(flag)
        # 同类问题聚合呈现：只列前 3 条，其余合并为一条（避免几十连报刷屏）
        if len(uniq_flags) > 3:
            issues.extend(uniq_flags[:3])
            issues.append({
                "problem": f"另有 {len(uniq_flags) - 3} 处同类功率量级问题（配对相同，略）",
                "severity": "高",
                "section": "模型建立与求解",
            })
        else:
            issues.extend(uniq_flags)

        # ── 4. 负物理量检查 ──
        # 扫描所有 "物理量: -数值" 或 "物理量 = -数值" 模式
        neg_patterns = re.findall(
            r"([\u4e00-\u9fff]+)\s*[：:=]\s*-(\d+(?:\.\d+)?)",
            paper
        )
        for key, val_str in neg_patterns:
            val = float(val_str)
            # 检查是否在白名单中
            is_whitelisted = any(wl in key for wl in self.NEGATIVE_WHITELIST)
            if not is_whitelisted and val > 0.01:  # 忽略极小负值（如 -0.001）
                issues.append({
                    "problem": f"物理量「{key}」为负值 -{val}，需在文中说明处理方式",
                    "severity": "中",
                    "section": "模型建立与求解",
                })

        # ── 5. 速度超光速 ──
        speed_patterns = re.findall(
            r"速度.*?(\d+\.?\d*(?:[eE]\d+)?)\s*(?:m/s|千米/秒|km/s)",
            paper
        )
        for val_str in speed_patterns:
            val = float(val_str)
            if val > 3e8:  # 光速约 3×10^8 m/s
                issues.append({
                    "problem": f"速度 {val:.2e}m/s 超过光速（3×10^8 m/s），违反相对论",
                    "severity": "高",
                    "section": "模型建立与求解",
                })

        # ── 6. 温度超出物理可能范围 ──
        temp_patterns = re.findall(
            r"(?:温度|气温)[\s:：为]*?(-?\d+(?:\.\d+)?)\s*(?:°?C|摄氏度|开尔文|K)",
            paper
        )
        for val_str in temp_patterns:
            val = float(val_str)
            # 绝对零度 -273.15°C，太阳表面约 5500°C
            if val < -273.15 or val > 10000:
                issues.append({
                    "problem": f"温度 {val}°C 超出物理可能范围（-273.15°C ~ 10000°C）",
                    "severity": "高",
                    "section": "模型建立与求解",
                })

        # ── 评分 ──
        # 物理常识审核采用更严格的评分：每个高严重度问题扣 4 分，中严重度扣 2 分
        score = 10.0
        for issue in issues:
            if issue["severity"] == "高":
                score -= 4.0
            elif issue["severity"] == "中":
                score -= 2.0
            else:
                score -= 0.5
        score = _clamp10(score)

        return {
            "score": score,
            "issues": issues,
            "suggestions": [f"请修正：{i['problem']}" for i in issues],
            "auditor": "physics",
        }


# ══════════════════════════════════════════════════════════
# 代码一致性审核：论文数字与代码输出比对（确定性，无 LLM）
# ══════════════════════════════════════════════════════════
class CodeAuditor:
    """代码一致性审核：验证论文中的数值是否与代码实际输出一致。

    核心检查：
    1. 代码输出的关键指标是否在论文中出现
    2. 论文中的数值是否有代码输出作为来源
    3. 误差指标（RMSE、R²、MAE等）是否与代码一致
    """

    def run(self, paper: str, summary: dict) -> dict:
        execs = summary.get("executions") or []
        if not execs:
            return {
                "score": None,
                "issues": [{"problem": "【已跳过】无执行结果，代码一致性审核不参与评分",
                            "severity": "中", "section": "全文"}],
                "suggestions": [],
                "auditor": "code",
                "skipped": True,
            }

        issues = []
        tolerance = float(get("auditor.code_tolerance", 0.02))

        # 收集所有代码输出的指标
        all_metrics: dict[str, float] = {}
        for ex in execs:
            if not isinstance(ex, dict) or ex.get("status") != "ok":
                continue
            # 来源 1: metrics_json
            mj = ex.get("metrics_json", {})
            if isinstance(mj, dict):
                for k, v in mj.items():
                    if not k.startswith("_") and isinstance(v, (int, float)):
                        all_metrics[k] = float(v)
            # 来源 2: metrics.numbers
            numbers = (ex.get("metrics") or {}).get("numbers", {})
            if isinstance(numbers, dict):
                for k, v in numbers.items():
                    if k not in all_metrics and isinstance(v, (int, float)):
                        all_metrics[k] = float(v)
                    elif k not in all_metrics and isinstance(v, list) and v:
                        all_metrics[k] = float(v[0])

        if not all_metrics:
            return {
                "score": None,
                "issues": [{"problem": "【已跳过】代码无输出指标，代码一致性审核不参与评分",
                            "severity": "中", "section": "全文"}],
                "suggestions": [],
                "auditor": "code",
                "skipped": True,
            }

        # 从论文中提取数值
        body = paper.split("## 参考文献")[0]
        paper_floats = [float(m) for m in re.findall(r"-?\d+\.?\d*(?:[eE][+-]?\d+)?", body)]

        # 检查每个代码输出指标是否在论文中出现
        found_count = 0
        for key, value in all_metrics.items():
            ref_abs = max(abs(value), 1e-6)
            matched = any(abs(v - value) <= ref_abs * tolerance for v in paper_floats)
            if matched:
                found_count += 1
            else:
                # 指标未在论文中出现，找论文中最接近的值
                if paper_floats:
                    closest = min(paper_floats, key=lambda x: abs(x - value))
                    ratio = closest / ref_abs if ref_abs > 0 else 0
                    if 0.1 <= ratio <= 10:  # 同数量级才报
                        issues.append({
                            "problem": (
                                f"代码输出 {key}={value:.4g}，"
                                f"论文最近值 {closest:.4g}（偏差 "
                                f"{abs(closest - value) / ref_abs * 100:.1f}%），"
                                f"建议修正为 {value:.4g}"
                            ),
                            "severity": "中",
                            "section": "模型建立与求解",
                        })

        # 覆盖率检查
        coverage = found_count / len(all_metrics) if all_metrics else 0
        min_coverage = float(get("auditor.code_coverage_min", 0.6))
        if coverage < min_coverage:
            issues.append({
                "problem": (
                    f"论文仅反映 {found_count}/{len(all_metrics)} 个代码输出指标"
                    f"（覆盖率 {coverage:.0%}，低于阈值 {min_coverage:.0%}），"
                    "与求解结果脱节"
                ),
                "severity": "高",
                "section": "模型建立与求解",
            })

        score = _clamp10(10.0 - 3.0 * len(issues))

        return {
            "score": score,
            "issues": issues,
            "suggestions": [f"请核对：{i['problem']}" for i in issues],
            "auditor": "code",
            "metrics_total": len(all_metrics),
            "metrics_found": found_count,
            "coverage": coverage,
        }


# ══════════════════════════════════════════════════════════
# 数学正确性审核：公式一致性、量纲检查（确定性，无 LLM）
# ══════════════════════════════════════════════════════════
class MathAuditor:
    """数学正确性审核：检查公式变量一致性、量纲检查、数值代入验证。

    核心检查：
    1. 符号表定义的符号是否都被使用
    2. 公式中使用的符号是否都在符号表中定义
    3. 数值代入验证（从公式提取简单等式，代入数值验证）
    4. 求和一致性（表格数据之和是否等于声称的总和）
    """

    def run(self, paper: str, summary: dict) -> dict:
        issues = []

        # ── 1. 符号表提取与使用检查 ──
        # 从"符号说明"章节提取定义的符号（标题级别不限定，兼容多种章节编号风格）
        symbol_section = re.search(
            r"^#{1,4}[^\n]*符号说明[^\n]*\n(.*?)(?=^#{1,4}|\Z)",
            paper, re.DOTALL | re.MULTILINE
        )
        defined_symbols = set()
        if symbol_section:
            # 提取 LaTeX 符号：$...$ 中的内容
            latex_symbols = re.findall(r"\$([^$]+)\$", symbol_section.group(1))
            for sym in latex_symbols:
                # 提取主要符号名（去掉下标等）
                main_sym = re.sub(r"[_^{}\\]", "", sym).strip()
                if main_sym and len(main_sym) <= 5:  # 忽略过长的表达式
                    defined_symbols.add(main_sym)

        # 从公式中提取使用的符号
        formula_symbols = set()
        formulas = re.findall(r"\$\$?(.*?)\$\$?", paper, re.DOTALL)
        for formula in formulas:
            # 跳过符号说明章节中的公式
            if symbol_section and symbol_section.group(0) in formula:
                continue
            # 先剥离 LaTeX 命令（\dfrac、\eta 等）——命令名不是数学符号
            formula_clean = re.sub(r"\\[a-zA-Z]+", " ", formula)
            # 提取变量符号
            syms = re.findall(r"[a-zA-Z]+(?:_[a-zA-Z0-9]+)?", formula_clean)
            for sym in syms:
                main_sym = re.sub(r"[_^{}\\]", "", sym).strip()
                if main_sym and len(main_sym) <= 5:
                    formula_symbols.add(main_sym)

        # 检查未定义的符号
        undefined = formula_symbols - defined_symbols
        # 白名单：常见数学符号不需要定义
        math_whitelist = {
            "sin", "cos", "tan", "log", "ln", "exp", "sqrt", "max", "min",
            "sum", "prod", "int", "lim", "sup", "inf", "lim", "limsup",
            "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta",
            "theta", "iota", "kappa", "lambda", "mu", "nu", "xi", "pi",
            "rho", "sigma", "tau", "upsilon", "phi", "chi", "psi", "omega",
            "Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta", "Eta",
            "Theta", "Iota", "Kappa", "Lambda", "Mu", "Nu", "Xi", "Pi",
            "Rho", "Sigma", "Tau", "Upsilon", "Phi", "Chi", "Psi", "Omega",
            "partial", "nabla", "infty", "forall", "exists", "in", "notin",
            "subset", "supset", "cup", "cap", "emptyset", "times", "cdot",
            "leq", "geq", "neq", "approx", "equiv", "sim", "propto",
            "hat", "bar", "tilde", "vec", "dot", "ddot",
        }
        undefined -= math_whitelist

        # 无符号说明章节时跳过该项（命题人范式"其中…"随文定义符号，
        # 建立不了已定义集合，强查只会全量误报）
        if symbol_section and undefined and len(undefined) > 3:
            issues.append({
                "problem": (
                    f"公式中使用了 {len(undefined)} 个未在符号说明中定义的符号"
                    f"（如 {', '.join(list(undefined)[:3])}），建议补充符号说明"
                ),
                "severity": "中",
                "section": "符号说明",
            })

        # ── 2. 求和一致性检查（保守版）──
        # 仅当"求和语义"的总值声明与表格同块（声明前 800 字符内有表格行）时比对：
        # 全局求和会把全文无关表格的数字全加起来，对多表论文必然误报
        #（实测 2023A：'总面积 62,820 m²' 被千分位截断成 62 后与全表和比较）。
        # 声明词限定求和语义（总计/合计/总成本/总距离等），不含"总面积"这类属性量；
        # 数值支持千分位分隔。
        _CLAIM_RE = re.compile(
            r"(?:总(?:计|和|需求量|成本|距离|费用|惩罚|时间|功率)|合计)"
            r"[\s:：为]*?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
        )
        _CELL_RE = re.compile(r"\|\s*(-?\d[\d,]*(?:\.\d+)?)\s*\|")
        for claim_m in _CLAIM_RE.finditer(paper):
            claim = float(claim_m.group(1).replace(",", ""))
            window = paper[max(0, claim_m.start() - 800):claim_m.start()]
            rows = [ln for ln in window.splitlines()
                    if ln.strip().startswith("|")]
            if not rows:
                continue
            cells: list[float] = []
            for ln in rows:
                cells += [float(c.replace(",", "")) for c in _CELL_RE.findall(ln)]
            if not cells:
                continue
            table_sum = sum(cells)
            if abs(table_sum - claim) > 0.01 and abs(table_sum - claim) / max(abs(claim), 1e-6) > 0.02:
                issues.append({
                    "problem": (
                        f"紧邻表格数据之和 {table_sum:.2f} 与论文声称的"
                        f"「{claim_m.group(0)[:24]}」不一致"
                        f"（偏差 {abs(table_sum - claim):.2f}），请检查是否遗漏数据或计算错误"
                    ),
                    "severity": "高",
                    "section": "模型建立与求解",
                })
                break

        # ── 3. 数值代入验证（简单线性公式） ──
        # 检查 "y = ax + b, 代入 x=c 得 y=d" 的模式
        substitution_patterns = re.findall(
            r"[yY]\s*=\s*(\d+(?:\.\d+)?)\s*[xX]\s*[+\-]\s*(\d+(?:\.\d+)?)"
            r".*?代入\s*[xX]\s*=\s*(\d+(?:\.\d+)?)\s*得\s*[yY]\s*=\s*(\d+(?:\.\d+)?)",
            paper, re.DOTALL
        )
        for a_str, b_str, x_str, y_claim_str in substitution_patterns:
            a, b, x, y_claim = float(a_str), float(b_str), float(x_str), float(y_claim_str)
            y_calc = a * x + b
            if abs(y_calc - y_claim) > 0.01:
                issues.append({
                    "problem": (
                        f"公式代入验证失败：y={a}×{x}+{b}={y_calc:.2f}，"
                        f"论文声称 y={y_claim:.2f}（偏差 {abs(y_calc - y_claim):.2f}）"
                    ),
                    "severity": "高",
                    "section": "模型建立与求解",
                })

        # ── 评分 ──
        score = _clamp10(10.0 - 3.0 * len(issues))
        return {
            "score": score,
            "issues": issues,
            "suggestions": [f"请修正：{i['problem']}" for i in issues],
            "auditor": "math",
        }


# ══════════════════════════════════════════════════════════
# 图表完整性审核：检查图表存在性与引用一致性（确定性，无 LLM）
# ══════════════════════════════════════════════════════════
class FigureAuditor:
    """图表完整性审核：验证图表是否存在、是否被引用、图号是否连续。

    核心检查：
    1. 孤立图片：代码生成了图片但论文未引用
    2. 幽灵引用：论文引用了不存在的图号
    3. 图号连续性：图号应连续（图1、图2、图3）
    4. 表格完整性：表格不应全为空
    """

    def run(self, paper: str, summary: dict) -> dict:
        from pathlib import Path
        issues = []
        execs = summary.get("executions") or []

        # ── 1. 收集代码生成的图片 ──
        generated_figures: list[str] = []
        for ex in execs:
            if isinstance(ex, dict) and ex.get("status") == "ok":
                for fig in ex.get("figures") or []:
                    generated_figures.append(Path(str(fig)).name)

        # ── 2. 收集论文中引用的图号 ──
        # 匹配 "图N" 或 "图 N" 模式（中文数字或阿拉伯数字）
        chinese_nums = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                       "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        referenced_figs = set()
        for m in re.finditer(r"图\s*([一二三四五六七八九十\d]+)", paper):
            num_str = m.group(1)
            if num_str in chinese_nums:
                referenced_figs.add(chinese_nums[num_str])
            elif num_str.isdigit():
                referenced_figs.add(int(num_str))

        # ── 3. 收集论文中定义的图（有标题的图）──
        # 兼容两种图注：行首"图N 标题"、Markdown 图片"![图N 标题](文件)"
        defined_figs = set()
        for m in re.finditer(
                r"^(?:[#>*\-\s]*|!\[)图\s*([一二三四五六七八九十\d]+)[\s：:、\]]*[^\n]*",
                paper, re.MULTILINE):
            num_str = m.group(1)
            if num_str in chinese_nums:
                defined_figs.add(chinese_nums[num_str])
            elif num_str.isdigit():
                defined_figs.add(int(num_str))

        # ── 4. 孤立图片检查 ──
        # 如果代码生成了图片但论文中没有出现图片文件名引用
        for fig_name in generated_figures:
            if fig_name not in paper:
                issues.append({
                    "problem": f"代码生成了图片 {fig_name}，但论文中未引用该图片",
                    "severity": "中",
                    "section": "模型建立与求解",
                })

        # ── 5. 幽灵引用检查 ──
        # 论文引用了不存在的图号（引用的图号不在定义的图号集合中）
        # 允许引用下一个图（可能是即将定义的）
        if defined_figs:
            for ref in referenced_figs:
                if ref not in defined_figs and ref != max(defined_figs) + 1:
                    issues.append({
                        "problem": f"论文引用了图{ref}，但该图号未定义（已定义图号：{sorted(defined_figs)}）",
                        "severity": "高",
                        "section": "模型建立与求解",
                    })

        # ── 6. 图号连续性检查 ──
        if len(defined_figs) >= 2:
            sorted_figs = sorted(defined_figs)
            for i in range(1, len(sorted_figs)):
                if sorted_figs[i] - sorted_figs[i-1] > 1:
                    issues.append({
                        "problem": f"图号不连续：从图{sorted_figs[i-1]}直接跳到图{sorted_figs[i]}，缺少中间图号",
                        "severity": "中",
                        "section": "全文",
                    })

        # ── 7. 表格完整性检查 ──
        # 检查是否有全为空的表格（只有表头没有数据）
        table_blocks = re.findall(r"\|[^\n]*\|\n\|[-:|\s]+\|\n((?:\|[^\n]*\|\n)*)", paper)
        empty_tables = sum(1 for block in table_blocks if not block.strip())
        if empty_tables > 0:
            issues.append({
                "problem": f"发现 {empty_tables} 个空表格（只有表头没有数据行）",
                "severity": "中",
                "section": "模型建立与求解",
            })

        # ── 8. 最少图表数量检查 ──
        min_figs = int(get("auditor.figure_min_count", 1))
        min_tables = int(get("auditor.table_min_count", 1))
        fig_count = len(defined_figs)
        table_count = len(re.findall(r"表\s*\d+", paper))
        if fig_count < min_figs:
            issues.append({
                "problem": f"论文仅有 {fig_count} 张图，少于最低要求 {min_figs} 张",
                "severity": "中",
                "section": "全文",
            })
        if table_count < min_tables:
            issues.append({
                "problem": f"论文仅有 {table_count} 个表，少于最低要求 {min_tables} 个",
                "severity": "中",
                "section": "全文",
            })

        # ── 评分 ──
        high_severity_count = sum(1 for i in issues if i["severity"] == "高")
        score = _clamp10(10.0 - 3.0 * high_severity_count - 1.0 * (len(issues) - high_severity_count))

        return {
            "score": score,
            "issues": issues,
            "suggestions": [f"请修正：{i['problem']}" for i in issues],
            "auditor": "figure",
        }


# ══════════════════════════════════════════════════════════
# 语言AI味审核：检测AI生成的语言特征（确定性，无 LLM）
# ══════════════════════════════════════════════════════════
class LanguageAuditor:
    """语言AI味审核：检测论文中的AI生成语言特征。

    核心检查：
    1. AI套话密度：高频AI套话出现频率
    2. 段落均匀性：AI生成的段落长度往往非常均匀
    3. 修饰词密度：填充词占比过高说明内容空洞
    4. 连接词过度使用：转折/递进连接词密度过高
    """

    # 高频AI套话（中文）
    AI_CLICHES_CN = [
        r"值得注意的是",
        r"需要指出的是",
        r"综上所述",
        r"总而言之",
        r"不难发现",
        r"显而易见",
        r"众所周知",
        r"具有重要的(?:理论|现实|实践)意义",
        r"为.*提供了.*(?:新的|有效的)(?:思路|视角|方法|途径)",
        r"本文的(?:主要|创新|核心)贡献(?:在于|是)",
        r"通过.*深入.*分析",
        r"取得了.*显著.*的.*(?:效果|成果|进展)",
        r"具有.*广泛.*的.*应用.*前景",
        r"在.*领域.*具有.*重要.*(?:价值|意义)",
    ]

    # 过渡连接词
    TRANSITION_WORDS = [
        "因此", "所以", "然而", "但是", "此外", "另外", "同时",
        "与此同时", "不过", "尽管", "虽然", "即使", "既然",
    ]

    # 填充修饰词
    FILLER_WORDS = [
        "显著", "有效", "合理", "科学", "系统", "全面",
        "深入", "充分", "重要", "关键", "核心", "创新",
    ]

    def run(self, paper: str, summary: dict) -> dict:
        # 检查是否启用
        if not get("auditor.language.enabled", True):
            return {
                "score": None,
                "issues": [],
                "suggestions": [],
                "auditor": "language",
                "skipped": True,
            }

        issues = []
        text_len = len(paper)
        if text_len < 100:
            return {
                "score": None,
                "issues": [{"problem": "【已跳过】论文内容过短，语言审核不参与评分",
                            "severity": "低", "section": "全文"}],
                "suggestions": [],
                "auditor": "language",
                "skipped": True,
            }

        # ── 1. AI套话密度检测 ──
        cliche_count = 0
        for pattern in self.AI_CLICHES_CN:
            cliche_count += len(re.findall(pattern, paper))
        cliche_density = cliche_count / (text_len / 1000)  # 每千字
        cliche_threshold = float(get("auditor.language.cliche_density_threshold", 3.0))
        if cliche_density > cliche_threshold:
            issues.append({
                "problem": (
                    f"AI套话密度 {cliche_density:.1f} 次/千字（阈值 {cliche_threshold}），"
                    f"检测到 {cliche_count} 处AI套话，建议减少程式化表达"
                ),
                "severity": "中",
                "section": "全文",
            })

        # ── 2. 段落长度均匀性 ──
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", paper) if p.strip() and len(p.strip()) > 20]
        if len(paragraphs) >= 5:
            para_lengths = [len(p) for p in paragraphs]
            mean_len = sum(para_lengths) / len(para_lengths)
            if mean_len > 0:
                variance = sum((l - mean_len) ** 2 for l in para_lengths) / len(para_lengths)
                cv = (variance ** 0.5) / mean_len  # 变异系数
                cv_threshold = float(get("auditor.language.paragraph_cv_threshold", 0.25))
                if cv < cv_threshold:
                    issues.append({
                        "problem": (
                            f"段落长度变异系数 {cv:.2f}（阈值 {cv_threshold}），"
                            "段落长度过于均匀，疑似AI生成"
                        ),
                        "severity": "低",
                        "section": "全文",
                    })

        # ── 3. 填充修饰词密度 ──
        filler_count = sum(1 for w in self.FILLER_WORDS if w in paper)
        filler_ratio = filler_count / (text_len / 100)
        filler_threshold = float(get("auditor.language.filler_ratio_threshold", 0.05))
        if filler_ratio > filler_threshold:
            issues.append({
                "problem": (
                    f"填充修饰词密度 {filler_ratio:.1%}（阈值 {filler_threshold:.0%}），"
                    f"检测到 {filler_count} 处填充词，建议减少空洞修饰"
                ),
                "severity": "低",
                "section": "全文",
            })

        # ── 4. 过渡连接词过度使用 ──
        transition_count = sum(1 for w in self.TRANSITION_WORDS if w in paper)
        transition_density = transition_count / (text_len / 1000)
        transition_threshold = float(get("auditor.language.transition_density_threshold", 5))
        if transition_density > transition_threshold:
            issues.append({
                "problem": (
                    f"过渡连接词密度 {transition_density:.1f} 次/千字（阈值 {transition_threshold}），"
                    "连接词过度使用，建议精简"
                ),
                "severity": "低",
                "section": "全文",
            })

        # ── 评分 ──
        # 语言审核相对宽松，每个问题扣 0.5 分
        score = _clamp10(10.0 - 0.5 * len(issues))

        return {
            "score": score,
            "issues": issues,
            "suggestions": [f"建议优化：{i['problem']}" for i in issues],
            "auditor": "language",
        }

