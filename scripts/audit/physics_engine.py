"""确定性物理引擎：量纲分析+常量校验+边界检查。

零LLM调用，纯规则引擎。技能包自包含版本（无外部依赖）。
"""
from __future__ import annotations

import re

from physics_constants import (
    CONSTANTS, BOUNDARIES, UNIT_DIMENSIONS, UNIT_SCALE,
    DIMENSIONLESS_KEYWORDS, NEGATIVE_WHITELIST,
)


class PhysicsEngine:
    """确定性物理引擎。"""

    def check_value(self, name: str, value: float, unit: str = "") -> list[dict]:
        """检查单个数值是否符合物理边界。"""
        issues = []

        # 转换百分比到小数
        if unit == "%":
            value_normalized = value / 100.0
        else:
            value_normalized = value

        # 匹配边界规则
        for boundary_name, boundary in BOUNDARIES.items():
            keywords = boundary.get("keywords", [])
            if not any(kw in name for kw in keywords):
                continue

            bmin = boundary.get("min")
            bmax = boundary.get("max")

            # 百分比特殊处理
            if boundary_name == "percentage" and unit == "%":
                if bmin is not None and value < bmin:
                    issues.append({"problem": f"{name}={value}{unit} 低于最小值 {bmin}",
                                   "severity": "高", "boundary": boundary_name})
                if bmax is not None and value > bmax:
                    issues.append({"problem": f"{name}={value}{unit} 超过最大值 {bmax}（{boundary.get('description', '')}）",
                                   "severity": "高", "boundary": boundary_name})
            elif boundary_name != "percentage":
                if bmin is not None and value_normalized < bmin:
                    issues.append({"problem": f"{name}={value} 低于物理下界 {bmin}（{boundary.get('description', '')}）",
                                   "severity": "高", "boundary": boundary_name})
                if bmax is not None and value_normalized > bmax:
                    issues.append({"problem": f"{name}={value} 超过物理上界 {bmax}（{boundary.get('description', '')}）",
                                   "severity": "高", "boundary": boundary_name})

        return issues

    def check_constants(self, paper: str) -> list[dict]:
        """检查论文中的物理常量是否正确（与标准值偏差 1%~10% 视为可疑）。"""
        issues = []
        number_unit_pattern = (r"(\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
                               r"\s*(m/s|J/K|J·s|W/\(m²·K⁴\)|W/m²|m/s²|°C|K)")
        matches = re.findall(number_unit_pattern, paper)

        for val_str, unit in matches:
            try:
                val = float(val_str)
            except ValueError:
                continue

            for const_name, const in CONSTANTS.items():
                const_unit = const.get("unit", "")
                const_val = const.get("value", 0)
                if const_unit != unit or const_val == 0:
                    continue

                rel_dev = abs(val - const_val) / abs(const_val)
                if 0.01 < rel_dev < 0.1:
                    pos = paper.find(val_str)
                    context = paper[max(0, pos - 50):pos + 50] if pos >= 0 else ""
                    aliases = const.get("aliases", [])
                    if any(alias in context for alias in aliases):
                        issues.append({
                            "problem": f"物理常量 {const_name}（{const.get('symbol', '')}）"
                                       f"取值 {val} 与标准值 {const_val} 偏差 {rel_dev*100:.1f}%",
                            "severity": "中", "constant": const_name,
                        })
        return issues

    def check_unit_consistency(self, paper: str) -> list[dict]:
        """单位一致性检查（低误报版）。

        设计取舍：同族倍数单位共存（W/kW/MW、m/cm）是工程文本常态，不报；
        仅当同句出现"跨族"单位组合（如 ℃ 与 K、年 与 s）时提示，
        且降为低严重度——该检查只作提示不作判定。
        """
        issues: list[dict] = []
        cross_family = [
            ("日", "s"), ("年", "s"), ("小时", "秒"), ("分钟", "秒"),
        ]
        for sentence in re.split(r"[。；;]", paper.replace("\n", "。")):
            if len(sentence) < 10:
                continue
            if "=" in sentence or "换算" in sentence:
                continue  # 换算说明场景豁免
            for a, b in cross_family:
                if a in sentence and b in sentence:
                    issues.append({
                        "problem": f"同一句中同时出现 {a} 与 {b}，请确认单位换算一致（提示级）",
                        "severity": "低",
                        "section": "单位一致性",
                    })
        return issues

    def check_negative_values(self, paper: str) -> list[dict]:
        """检查论文中不应为负的物理量。"""
        issues = []
        pattern = r"([\u4e00-\u9fff]{2,8})\s*[：:=]\s*-(\d+(?:\.\d+)?)"
        matches = re.findall(pattern, paper)

        for name, val_str in matches:
            if any(wl in name for wl in NEGATIVE_WHITELIST):
                continue
            for boundary_name, boundary in BOUNDARIES.items():
                keywords = boundary.get("keywords", [])
                if any(kw in name for kw in keywords):
                    bmin = boundary.get("min")
                    if bmin is not None and bmin >= 0:
                        issues.append({
                            "problem": f"物理量「{name}」出现负值 -{val_str}，"
                                       f"但该量不应为负（{boundary.get('description', '')}）",
                            "severity": "中", "section": "负值检查",
                        })
                    break
        return issues

    def check_power_magnitude(self, paper: str) -> list[dict]:
        """检查功率量级是否合理。"""
        issues = []
        power_pattern = r"(\d+(?:\.\d+)?)\s*(MW|GW|kW|W)"
        powers = re.findall(power_pattern, paper)

        for val_str, unit in powers:
            try:
                val = float(val_str)
            except ValueError:
                continue
            scale = UNIT_SCALE.get(unit, 1)
            val_watts = val * scale

            if val_watts > 1e11:
                pos = paper.find(val_str)
                context = paper[max(0, pos - 100):pos + 100] if pos >= 0 else ""
                if not any(kw in context for kw in ["电厂", "电网", "发电", "装机", "总装机"]):
                    issues.append({
                        "problem": f"功率 {val}{unit}（{val_watts:.2e}W）异常大，请检查量级",
                        "severity": "中", "section": "功率量级",
                    })
        return issues

    def check_all(self, paper: str) -> list[dict]:
        """运行所有物理检查。"""
        all_issues = []
        all_issues.extend(self.check_negative_values(paper))
        all_issues.extend(self.check_unit_consistency(paper))
        all_issues.extend(self.check_power_magnitude(paper))
        all_issues.extend(self.check_constants(paper))
        all_issues.extend(self._check_paper_values(paper))
        return all_issues

    def _check_paper_values(self, paper: str) -> list[dict]:
        """从论文中提取带单位的数值并检查边界。"""
        issues = []
        patterns = [
            (r"([\u4e00-\u9fff]{2,10})\s*[：:=为达到]*?\s*(-?\d+(?:\.\d+)?)\s*(%)", "%"),
            (r"([\u4e00-\u9fff]{2,10})\s*[：:=为达到]*?\s*(-?\d+(?:\.\d+)?)\s*(°C|℃)", "°C"),
            (r"([\u4e00-\u9fff]{2,10})\s*[：:=为达到]*?\s*(-?\d+(?:\.\d+)?)\s*(K)\b", "K"),
            (r"([\u4e00-\u9fff]{2,10})\s*[：:=为达到]*?\s*(-?\d+(?:\.\d+)?)\s*(m/s|km/h)", "m/s"),
        ]

        for pattern, default_unit in patterns:
            matches = re.findall(pattern, paper)
            for name, val_str, unit in matches:
                try:
                    val = float(val_str)
                except ValueError:
                    continue
                issues.extend(self.check_value(name, val, unit))
        return issues
