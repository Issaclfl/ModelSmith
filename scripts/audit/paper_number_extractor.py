"""论文数字提取器：从论文文本中提取所有数值及其上下文。

零LLM调用，纯正则引擎。
"""
from __future__ import annotations

import re


class PaperNumberExtractor:
    """从论文中提取所有数值及其上下文。"""

    # 匹配带千分位的数字（如 12,350）和普通数字
    # 优先匹配带千分位的数字，再匹配普通数字（含科学计数法）
    # 允许数字紧跟中文字符（中文文本中常见"效率为92.3%"格式）
    _NUMBER_PATTERN = re.compile(
        r"(?<![a-zA-Z])"              # 前面不是英文字母（允许中文和数字）
        r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"  # 数字
        r"(?![a-zA-Z])"               # 后面不是英文字母（允许中文和数字）
    )

    # 匹配带单位的数值
    _NUMBER_WITH_UNIT_PATTERN = re.compile(
        r"(-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
        r"\s*"
        r"(km|mm|μm|um|nm|cm|m²|m³|m/s|km/h|kg|g|mg|t|吨|"
        r"s|ms|min|h|天|日|年|"
        r"℃|°C|K|"
        r"W|kW|MW|GW|J|kJ|MJ|cal|kWh|eV|"
        r"Pa|kPa|MPa|atm|"
        r"Hz|kHz|MHz|GHz|"
        r"N|kN|V|kV|mV|A|mA|"
        r"%|°|rad)"
    )

    def extract_numbers(self, paper: str) -> list[dict]:
        """提取论文中所有数值。

        Returns:
            [{"value": float, "raw": str, "context": str, 
              "section": str, "position": int}]
        """
        results = []
        seen = set()  # 去重（同一位置的数字）

        for match in self._NUMBER_PATTERN.finditer(paper):
            raw = match.group(1)
            pos = match.start()

            if pos in seen:
                continue
            seen.add(pos)

            # 解析数值（去除千分位逗号）
            try:
                value = float(raw.replace(",", ""))
            except ValueError:
                continue

            # 跳过明显不是指标的数字（年份、页码等）
            if 1900 <= value <= 2100 and "." not in raw and "e" not in raw.lower():
                continue  # 年份
            if value == int(value) and 1 <= value <= 100 and "." not in raw:
                # 可能是序号，检查上下文
                context_start = max(0, pos - 20)
                context_end = min(len(paper), pos + len(raw) + 20)
                context = paper[context_start:context_end]
                if re.search(r"第.{0,5}" + re.escape(raw) + r"|问题\s*" + re.escape(raw), context):
                    continue  # "第X" 或 "问题X"

            # 获取上下文
            context_start = max(0, pos - 60)
            context_end = min(len(paper), pos + len(raw) + 60)
            context = paper[context_start:context_end].replace("\n", " ")

            # 找到所在章节
            section = self._find_section(paper, pos)

            results.append({
                "value": value,
                "raw": raw,
                "context": context,
                "section": section,
                "position": pos,
            })

        return results

    def extract_with_units(self, paper: str) -> list[dict]:
        """提取带单位的数值。

        Returns:
            [{"value": float, "raw": str, "unit": str, "context": str, 
              "section": str, "position": int}]
        """
        results = []
        seen = set()

        for match in self._NUMBER_WITH_UNIT_PATTERN.finditer(paper):
            raw = match.group(1)
            unit = match.group(2)
            pos = match.start()

            if pos in seen:
                continue
            seen.add(pos)

            try:
                value = float(raw.replace(",", ""))
            except ValueError:
                continue

            context_start = max(0, pos - 60)
            context_end = min(len(paper), pos + len(raw) + len(unit) + 60)
            context = paper[context_start:context_end].replace("\n", " ")

            section = self._find_section(paper, pos)

            results.append({
                "value": value,
                "raw": raw,
                "unit": unit,
                "context": context,
                "section": section,
                "position": pos,
            })

        return results

    @staticmethod
    def _find_section(paper: str, position: int) -> str:
        """找到 position 所在的章节标题。"""
        before = paper[:position]
        heading_match = list(re.finditer(r"##\s+(.+)", before))
        if heading_match:
            return heading_match[-1].group(1).strip()
        return "全文"
