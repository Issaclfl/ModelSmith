"""科学门禁：合并多维验证结果，做最终通过/不通过决策（零 LLM）。

平台 agents/scientific_gate.py 的自包含版（权重/阈值内置，与平台默认一致）。

用法：
    python gate.py scores.json
    python gate.py -            # 从 stdin 读 JSON

输入 JSON（五维，来自验证链各成员 / 独立评审 / 完整审计）：
    {
      "critic":   {"score": 8.0},      // 建模批判性评审（LLM/子代理）
      "physics":  {"score": 7.5},      // 物理合理性
      "math":     {"score": 9.0},      // 数学正确性
      "evidence": {"score": 8.0},      // 证据链完整（代码-论文一致）
      "audit":    {"overall": 9.81}    // audit_full 的综合分（兼容 overall 字段）
    }

权重：audit 0.30 / physics 0.20 / math 0.20 / critic 0.15 / evidence 0.15
红线：任一维度 < 6.0 直接不通过；综合分（0-10 制）需 ≥ 7.0。
输出：通过/不通过 + 100 分制综合分 + A+~F 档位。退出码：0 = 通过，1 = 未通过。
"""
from __future__ import annotations

import json
import sys

# 等级划分（100 分制）
GRADE_THRESHOLDS = [
    (90, "A+"),
    (85, "A"),
    (80, "A-"),
    (75, "B+"),
    (70, "B"),
    (65, "B-"),
    (60, "C+"),
    (55, "C"),
    (50, "C-"),
    (40, "D"),
    (0,  "F"),
]

# 维度权重（audit 权重最高：零 LLM 可复现）
DEFAULT_WEIGHTS = {
    "critic": 0.15,
    "physics": 0.20,
    "math": 0.20,
    "evidence": 0.15,
    "audit": 0.30,
}

MIN_DIMENSION_SCORE = 6.0   # 任一维度低于此直接不通过
PASS_THRESHOLD = 7.0        # 综合分阈值（0-10 制）


def _grade(score: float) -> str:
    for threshold, label in GRADE_THRESHOLDS:
        if score >= threshold:
            return label
    return "F"


class ScientificGate:
    """科学门禁：合并验证结果，做出最终通过/不通过决策。"""

    def run(self, verification_results: dict) -> dict:
        # 归一化权重
        weights = {k: float(v) for k, v in DEFAULT_WEIGHTS.items()}
        total_weight = sum(weights.values())
        if total_weight > 0:
            weights = {k: v / total_weight for k, v in weights.items()}

        # 计算加权综合分（兼容 score 或 QualityGate 的 overall 字段）
        total_score = 0.0
        active_dims = {}
        for name, weight in weights.items():
            result = verification_results.get(name)
            if result:
                score = result.get("score") or result.get("overall")
                if score is not None:
                    score = float(score)
                    total_score += score * weight
                    active_dims[name] = {
                        "score": score,
                        "weight": weight,
                        "weighted_score": round(score * weight, 2),
                        "issues_count": len(result.get("issues", [])),
                    }

        total_score = round(total_score, 1)
        # 0-10 制转 100 分制
        total_score_100 = round(total_score * 10, 1)

        # 硬性红线：任何维度 < 6.0 直接不通过
        failed_dims = [
            name for name, dim in active_dims.items()
            if dim["score"] < MIN_DIMENSION_SCORE
        ]
        passed = len(failed_dims) == 0 and total_score >= PASS_THRESHOLD

        verdict = self._build_verdict(active_dims, failed_dims, passed, total_score_100)

        return {
            "passed": passed,
            "total_score": total_score_100,
            "grade": _grade(total_score_100),
            "dimensions": active_dims,
            "failed_dimensions": failed_dims,
            "verdict": verdict,
        }

    @staticmethod
    def _build_verdict(dims: dict, failed_dims: list, passed: bool,
                       total_score: float) -> str:
        parts = []
        if passed:
            parts.append(f"科学门禁通过（综合分 {total_score}/100）。\n")
        elif failed_dims:
            parts.append("科学门禁未通过：以下维度低于红线：\n")
            for dim_name in failed_dims:
                dim = dims.get(dim_name, {})
                parts.append(f"  - {dim_name}: {dim.get('score', 0)}/10\n")
        else:
            parts.append(f"科学门禁未通过：综合分 {total_score} 未达到阈值。\n")

        parts.append("\n各维度评分：")
        for name, dim in sorted(dims.items(), key=lambda x: x[1]["score"], reverse=True):
            issues_count = dim.get("issues_count", 0)
            issues_note = f"（{issues_count}个问题）" if issues_count > 0 else ""
            parts.append(
                f"\n  - {name}: {dim['score']}/10 × {dim['weight']*100:.0f}%"
                f" = {dim['weighted_score']} {issues_note}")
        return "".join(parts)


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    if sys.argv[1] == "-":
        raw = sys.stdin.read()
    else:
        raw = open(sys.argv[1], encoding="utf-8").read()
    results = json.loads(raw)

    gate = ScientificGate().run(results)
    print(gate["verdict"])
    print(f"\n档位：{gate['grade']}")
    sys.exit(0 if gate["passed"] else 1)


if __name__ == "__main__":
    main()
