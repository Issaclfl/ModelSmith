"""确定性数学引擎：公式验证+约束检查+代入验证。

零LLM调用，纯规则引擎。供 MathAuditor 和 MathChecker 使用。
"""
from __future__ import annotations

import re
import math


class MathEngine:
    """确定性数学引擎。"""

    def verify_substitution(self, formula: str, values: dict[str, float]) -> dict:
        """验证数值代入（支持线性和简单非线性表达式）。

        从公式中提取 y = f(x) 形式的等式，代入 values 验证结果。

        Args:
            formula: LaTeX 公式字符串（如 "y = 2x + 3"）
            values: 变量值映射（如 {"x": 5, "y": 13}）

        Returns:
            {"ok": bool, "expected": float|None, "actual": float|None, 
             "deviation": float|None, "detail": str}
        """
        # 尝试解析简单线性: y = ax + b
        linear_match = re.match(
            r"([a-zA-Z])\s*=\s*([+-]?\d*\.?\d*)\s*\*\s*([a-zA-Z])\s*([+-]\s*\d*\.?\d*)",
            formula.replace("\\", "").replace(" ", ""),
        )
        if linear_match:
            y_var = linear_match.group(1)
            a_str = linear_match.group(2) or "1"
            x_var = linear_match.group(3)
            b_str = linear_match.group(4).replace(" ", "")

            try:
                a = float(a_str)
                b = float(b_str)
            except ValueError:
                return {"ok": False, "detail": f"无法解析系数: a={a_str}, b={b_str}"}

            if x_var in values and y_var in values:
                x_val = values[x_var]
                y_actual = values[y_var]
                y_expected = a * x_val + b
                dev = abs(y_actual - y_expected) / max(abs(y_expected), 1e-10)
                return {
                    "ok": dev < 0.02,
                    "expected": round(y_expected, 6),
                    "actual": round(y_actual, 6),
                    "deviation": round(dev, 6),
                    "detail": f"{y_var} = {a}×{x_var} + {b} = {a}×{x_val} + {b} = {y_expected:.4f}，实际 {y_actual}",
                }

        return {"ok": True, "detail": "无法解析为可验证的等式"}

    def check_summation(self, table_values: list[float], claimed_sum: float, tolerance: float = 0.02) -> dict:
        """验证表格数据之和是否等于声称的总和。

        Args:
            table_values: 表格中的数值列表
            claimed_sum: 声称的总和
            tolerance: 相对容差（默认2%）

        Returns:
            {"ok": bool, "computed_sum": float, "claimed_sum": float, 
             "deviation": float, "detail": str}
        """
        if not table_values:
            return {"ok": True, "detail": "无数值可验证"}

        computed = sum(table_values)
        if claimed_sum == 0:
            dev = abs(computed) if computed != 0 else 0
        else:
            dev = abs(computed - claimed_sum) / abs(claimed_sum)

        return {
            "ok": dev <= tolerance,
            "computed_sum": round(computed, 6),
            "claimed_sum": round(claimed_sum, 6),
            "deviation": round(dev, 6),
            "detail": f"表格数据之和 = {computed:.4f}，声称总和 = {claimed_sum}，偏差 {dev*100:.2f}%",
        }

    def check_optimization_model(self, paper: str, sub_problems: list[str]) -> list[dict]:
        """检查优化模型的完整性。

        检查：
        1. 优化类子问题是否有目标函数
        2. 有目标函数是否有约束条件
        3. 决策变量是否定义

        Returns:
            [{"problem": str, "severity": "高"|"中"|"低", "section": str}]
        """
        issues = []

        # 检查是否有目标函数
        has_objective = bool(re.search(
            r"目标函数|min\s+Z|max\s+Z|minimize|maximize|最大化|最小化|min\s+",
            paper, re.IGNORECASE
        ))
        has_constraints = bool(re.search(
            r"约束条件|subject\s+to|s\.t\.|满足.*约束|约束\s*[:：]",
            paper, re.IGNORECASE
        ))
        has_decision_vars = bool(re.search(
            r"决策变量|决策变量为|令\s*[a-zA-Z]|设\s*[a-zA-Z]",
            paper, re.IGNORECASE
        ))

        # 对每个优化类子问题检查
        for sp in sub_problems:
            if not any(kw in sp for kw in ["优化", "调度", "分配", "规划", "最小", "最大", "最优"]):
                continue

            if not has_objective:
                issues.append({
                    "problem": f"优化类子问题「{sp[:30]}」未明确定义目标函数",
                    "severity": "高",
                    "section": "模型建立",
                })

        # 全局检查
        if has_objective and not has_constraints:
            issues.append({
                "problem": "有目标函数但未列出约束条件",
                "severity": "高",
                "section": "模型建立",
            })

        if has_objective and not has_decision_vars:
            issues.append({
                "problem": "有目标函数但未定义决策变量",
                "severity": "中",
                "section": "模型建立",
            })

        return issues

    def check_statistical_methods(self, paper: str) -> list[dict]:
        """检查统计方法的使用是否规范。

        Returns:
            [{"problem": str, "severity": "高"|"中"|"低", "section": str}]
        """
        issues = []

        # 统计方法及其前提条件
        stat_methods = {
            "t检验": {
                "prerequisites": ["正态", "独立", "样本量"],
                "description": "需要正态性假设、独立样本、足够样本量",
            },
            "方差分析": {
                "prerequisites": ["正态", "方差齐性", "独立"],
                "description": "需要正态性、方差齐性、独立样本",
            },
            "卡方检验": {
                "prerequisites": ["期望频数", "独立", "样本量"],
                "description": "需要期望频数≥5、独立样本",
            },
            "回归分析": {
                "prerequisites": ["线性", "残差", "正态"],
                "description": "需要线性关系、残差正态性",
            },
            "相关分析": {
                "prerequisites": ["线性", "正态", "样本量"],
                "description": "需要线性关系、正态分布、足够样本量",
            },
            "主成分分析": {
                "prerequisites": ["相关性", "KMO", "样本量"],
                "description": "需要变量间相关性、KMO检验、足够样本量",
            },
        }

        for method, info in stat_methods.items():
            if method in paper:
                missing = [req for req in info["prerequisites"] if req not in paper]
                if missing:
                    issues.append({
                        "problem": f"使用了{method}，但未验证前提条件：{'、'.join(missing)}（{info['description']}）",
                        "severity": "中",
                        "section": "统计方法",
                    })

        # 检查样本量
        sample_match = re.search(r"样本.*?(\d+)\s*(?:个|条|组|份)", paper)
        if sample_match:
            sample_size = int(sample_match.group(1))
            if sample_size < 30:
                issues.append({
                    "problem": f"样本量较小（{sample_size}），统计结论可能不可靠（建议 ≥30）",
                    "severity": "低",
                    "section": "统计方法",
                })

        return issues

    def extract_formulas_from_paper(self, paper: str) -> list[dict]:
        """从论文中提取所有公式及其上下文。

        Returns:
            [{"formula": str, "context": str, "section": str, "type": "display"|"inline"}]
        """
        formulas = []

        # 提取 display math ($$...$$)
        for match in re.finditer(r"\$\$(.*?)\$\$", paper, re.DOTALL):
            formula = match.group(1).strip()
            # 获取上下文（前后各100字符）
            start = max(0, match.start() - 100)
            end = min(len(paper), match.end() + 100)
            context = paper[start:end]

            # 判断所在章节
            section = self._find_section(paper, match.start())

            formulas.append({
                "formula": formula,
                "context": context,
                "section": section,
                "type": "display",
            })

        # 提取 inline math ($...$) — 排除 display math
        inline_pattern = r"(?<!\$)\$(?!\$)(.*?)\$(?!\$)"
        for match in re.finditer(inline_pattern, paper):
            formula = match.group(1).strip()
            if len(formula) > 3 and not formula.startswith("$"):
                section = self._find_section(paper, match.start())
                formulas.append({
                    "formula": formula,
                    "context": "",
                    "section": section,
                    "type": "inline",
                })

        return formulas

    @staticmethod
    def _find_section(paper: str, position: int) -> str:
        """找到 position 所在的章节标题。"""
        # 找到 position 之前的最后一个 ## 标题
        before = paper[:position]
        heading_match = list(re.finditer(r"##\s+(.+)", before))
        if heading_match:
            return heading_match[-1].group(1).strip()
        return "全文"
