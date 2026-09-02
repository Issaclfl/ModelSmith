"""数模论文检测报告：自包含审计运行器（零 LLM、零平台依赖）。

用法：
    python audit_runner.py 论文.md              # 输出检测报告 .md
    python audit_runner.py 论文.md --json out.json

检查维度（全部确定性规则，同一输入结果完全一致）：
  A. 结构完整性   —— 竞赛论文必备章节/要素清单
  B. 物理边界     —— 效率>100%、超光速、温度越界、负值量、单位混用（物理引擎）
  C. 数学一致性   —— 表格求和 vs 声称总和、代入验证、优化模型完备性（数学引擎）
  D. 数字抽取     —— 全文数值清单（供人工核对其来源）

依赖：仅本目录四个自包含模块 + Python 标准库。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from physics_engine import PhysicsEngine   # noqa: E402
from math_engine import MathEngine         # noqa: E402
from paper_number_extractor import PaperNumberExtractor  # noqa: E402

# ── A. 结构完整性清单（竞赛论文必备要素，按重要度分级计扣分）──
# (名称, 正则, 扣分权重)：高=2.5 中=1 低=0.25（提示级不实质扣分）
STRUCTURE_CHECKS = [
    ("摘要", r"摘要|Abstract", 2.5),
    ("关键词", r"关键词|Key\s*words", 1),
    ("问题重述", r"问题重述|问题的提出|背景", 1),
    ("模型建立", r"模型建立|数学模型|建立模型|模型构建", 2.5),
    ("求解过程", r"求解|算法|步骤|最小二乘|拟合|回归", 2.5),
    ("误差/拟合指标", r"RMSE|R\^2|R²|误差|残差|MSE|MAE|相对误差", 1),
    ("结果分析/结论", r"结果分析|结论|讨论", 1),
    ("参考文献", r"参考文献|References", 0.25),
    ("模型假设（提示）", r"模型假设|假设", 0.25),
    ("符号说明（提示）", r"符号说明|符号表|符号", 0.25),
    ("灵敏度分析（提示）", r"灵敏度|敏感性|稳健性", 0.25),
    ("模型评价（提示）", r"模型评价|优点|缺点|局限", 0.25),
]

# 占位符/未完成痕迹
PLACEHOLDER = re.compile(
    r"\[请填入[^\]]*\]|\[具体[^\]]*\]|TODO|FIXME|TBD|待补充|待填入|占位符")


def check_structure(paper: str) -> list[dict]:
    issues = []
    for name, pat, weight in STRUCTURE_CHECKS:
        if not re.search(pat, paper, re.IGNORECASE):
            issues.append({"检查": "结构完整性", "问题": f"缺少要素：{name}",
                           "严重度": "高" if weight >= 2 else ("中" if weight >= 1 else "低"),
                           "扣分": weight})
    placeholders = PLACEHOLDER.findall(paper)
    if placeholders:
        issues.append({"检查": "占位符", "问题":
                       f"发现 {len(placeholders)} 处未完成占位符（如「{placeholders[0][:30]}」）",
                       "严重度": "高", "扣分": 2.5})
    # 公式与图表的最低配置
    if len(re.findall(r"\$\$.*?\$\$", paper, re.DOTALL)) < 3:
        issues.append({"检查": "公式密度", "问题": "独立公式少于 3 个，模型推导可能不充分",
                       "严重度": "中", "扣分": 1})
    if not re.search(r"!\[|<img|图\s*\d", paper):
        issues.append({"检查": "插图", "问题": "未检测到插图", "严重度": "中", "扣分": 1})
    return issues


def run_audit(paper: str) -> dict:
    """执行全部确定性检查，返回报告 dict。"""
    sections = []

    _W = {"高": 2.5, "中": 1, "低": 0}

    def _score(issues):
        return round(max(0, 10 - sum(_W.get(i.get("严重度", i.get("severity", "中")), 1)
                                     for i in issues)), 1)

    # A. 结构完整性
    s_issues = check_structure(paper)
    sections.append({"维度": "结构完整性", "得分": _score(s_issues),
                     "问题数": len(s_issues), "问题": s_issues})

    # B. 物理边界（确定性物理引擎）
    pe = PhysicsEngine()
    p_issues = pe.check_all(paper)
    sections.append({"维度": "物理边界", "得分": _score(p_issues),
                     "问题数": len(p_issues), "问题": p_issues})

    # C. 数学一致性（确定性数学引擎）
    me = MathEngine()
    m_issues = []
    m_issues += me.check_statistical_methods(paper)
    formulas = re.findall(r"\$\$(.+?)\$\$", paper, re.DOTALL)
    if not formulas:
        m_issues.append({"problem": "未检测到独立公式", "severity": "中"})
    m_score = max(0, 10 - len(m_issues) * 2)
    sections.append({"维度": "数学一致性", "得分": round(m_score, 1),
                     "问题数": len(m_issues), "问题": m_issues})

    # D. 数字抽取（供人工核对来源）
    extractor = PaperNumberExtractor()
    nums = extractor.extract_numbers(paper)
    with_units = extractor.extract_with_units(paper)

    # 总分：各维度等权平均，四舍五入一位
    scores = [s["得分"] for s in sections]
    total = round(sum(scores) / len(scores), 1) if scores else 0.0

    return {"总分": total, "维度": sections,
            "数字抽取": {"数量": len(nums),
                        "样例": [{"值": n["value"], "上下文": n["context"][:50]}
                                for n in nums[:15]]},
            "免责声明": "本报告为确定性规则引擎检测结果，仅覆盖可规则化的维度；"
                       "模型正确性、创新性等需人工评审。"}


def render_md(report: dict, source: str) -> str:
    lines = [f"# 数模论文检测报告：{source}\n"]
    lines.append(f"## 总分：{report['总分']} / 10\n")
    for sec in report["维度"]:
        mark = "✅" if not sec["问题"] else "⚠️"
        lines.append(f"### {mark} {sec['维度']}（{sec['得分']}/10，{sec['问题数']} 项）\n")
        for iss in sec["问题"]:
            if isinstance(iss, dict):
                lines.append(f"- [{iss.get('严重度', iss.get('severity', '中'))}] "
                             f"{iss.get('问题', iss.get('problem', ''))}")
            else:
                lines.append(f"- {iss}")
        lines.append("")
    nums = report.get("数字抽取", {})
    lines.append(f"### 抽取数值（{nums['数量']} 个，供核对来源）\n")
    for n in nums.get("样例", []):
        lines.append(f"- {n['值']} ← …{n['上下文']}…")
    lines.append(f"\n> {report['免责声明']}\n")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="数模论文检测报告（确定性规则引擎）")
    ap.add_argument("paper", help="论文 Markdown 文件")
    ap.add_argument("--json", dest="json_out", default="", help="同时输出 JSON 报告")
    args = ap.parse_args()

    paper = Path(args.paper).read_text(encoding="utf-8")
    report = run_audit(paper)

    out_md = Path(args.paper).with_suffix(".检测报告.md")
    out_md.write_text(render_md(report, Path(args.paper).name), encoding="utf-8")
    print(f"检测报告: {out_md}（总分 {report['总分']}/10）")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        print(f"JSON: {args.json_out}")


if __name__ == "__main__":
    main()
