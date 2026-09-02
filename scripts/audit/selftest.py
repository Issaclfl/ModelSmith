"""技能包自测试：确定性审计器校准回归（零 LLM、零平台依赖）。

运行：python selftest.py    （退出码 0 = 全绿）

校准锚点（实测于 2026-08-30；改动审计规则后如分数漂移，请更新锚点并注明原因）：
  - GOOD_PAPER（结构完整论文）logic 分 ≥ 9.0
  - BAD_PAPER（空泛一句话）logic 分 < 8.0
  - 同一论文两次打分完全一致（确定性）
  - A2023 范文（平台命题人范式旗舰论文，能在同仓库找到时才测）：
    完整审计综合 9.81/10 通过门禁；唯一扣分 = 全文无假设叙述（真缺口，
    有意保留——范式要求"假设随文叙述"，写作时必须出现"假设/假定/不考虑"等字样）
  - PhysicsAuditor：反射率 92%（百分数形态）不误报；反射率 1.5 必须报错
  - 偷懒检测：eta_sb=1.0 硬编码 + 小种群小迭代必报；范例脚本零误报
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from auditors import PhysicsAuditor, QualityGateAgent, LogicAuditor  # noqa: E402
from laziness_check import check_lazy_code  # noqa: E402

GOOD_PAPER = """# 测试论文

## 摘要
本文建立回归模型，采用最小二乘求解，RMSE=15.2，R²=0.93，给出 95% 置信区间。

## 一、问题重述
根据赛题要求，建立租用量预测模型。

## 二、模型假设
假设数据满足线性关系，误差独立同分布。

## 三、模型建立
建立多元线性回归模型，定义变量与目标函数。

## 四、求解过程与算法
采用最小二乘算法拟合，回归系数见式(4.1)。

## 五、结果分析
测试集 RMSE=15.2，R²=0.93，F 检验 p<0.05，不确定度 ±0.3。

## 六、结论
模型有效，推广至其他城市，灵敏度分析表明参数稳健。
"""

BAD_PAPER = "本文研究药物浓度衰减，采用回归分析。结束。"

LAZY_CODE = """import numpy as np
eta_sb = 1.0
pop = 5
max_iter = 10
result = optimize(f, pop, max_iter)
"""

CLEAN_CODE = """import numpy as np
pop = 50
max_iter = 100
eta = compute_eta(pop, max_iter)
reflect = 0.92
"""

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"（{detail}）" if detail else ""))


def main() -> None:
    gate = QualityGateAgent()

    # 1. 确定性：同一论文两次打分完全一致
    summary = {"_verified_results": "RMSE=15.2", "executions": []}
    r1 = gate.run(GOOD_PAPER, summary)
    r2 = gate.run(GOOD_PAPER, summary)
    check("确定性：两次打分一致", r1["scores"] == r2["scores"]
          and r1["overall"] == r2["overall"], f"overall={r1['overall']}")

    # 2. 结构清单：完整论文通过、空泛论文扣分
    good_logic = LogicAuditor().run(GOOD_PAPER, {})["score"]
    bad_logic = LogicAuditor().run(BAD_PAPER, {})["score"]
    check("GOOD_PAPER logic ≥ 9.0", good_logic >= 9.0, f"actual={good_logic}")
    check("BAD_PAPER logic < 8.0", bad_logic < 8.0, f"actual={bad_logic}")

    # 3. 物理审计：百分数不误报、真硬伤必报
    phys_ok = PhysicsAuditor().run(
        "镜面反射率为 92%，大气透射率 96.5%，转换效率 58.34%。", {})["score"]
    phys_bad = PhysicsAuditor().run("反射率 1.5，效率 150%。", {})["score"]
    check("物理：反射率 92% 不误报", phys_ok == 10.0, f"actual={phys_ok}")
    check("物理：反射率 1.5 / 效率 150% 必报", phys_bad < 8.0, f"actual={phys_bad}")

    # 4. 偷懒检测：硬编码必报、正常代码不误报
    lazy = check_lazy_code(LAZY_CODE)
    clean = check_lazy_code(CLEAN_CODE)
    check("偷懒：eta_sb=1.0+小种群+小迭代必报", len(lazy) >= 3, f"actual={lazy}")
    check("偷懒：正常代码与 ref 常数零误报", not clean, f"actual={clean}")

    # 5. A2023 范文锚点（技能包独立分发时若无该文件则跳过）
    anchor = Path(__file__).parents[4] / "docs" / "papers" / "A2023_paper.md"
    if anchor.exists():
        paper = anchor.read_text(encoding="utf-8")
        gate_a = gate.run(paper, {"executions": []})
        check("A2023 范文通过门禁（综合 ≥ 9.0）",
              gate_a["passed"] and gate_a["overall"] >= 9.0,
              f"actual={gate_a['overall']} scores={gate_a['scores']}")
    else:
        print("[SKIP] A2023 范文锚点（独立分发包中不存在 docs/papers/）")

    # 6. 范例脚本零误报（信息性展示）
    examples = Path(__file__).parents[2] / "examples"
    if examples.exists():
        bad = []
        for f in sorted(examples.glob("*.py")):
            issues = check_lazy_code(f.read_text(encoding="utf-8"))
            if issues:
                bad.append(f"{f.name}: {issues}")
        check("范例脚本偷懒检测零误报", not bad, "; ".join(bad) if bad else
              f"{len(list(examples.glob('*.py')))} 个脚本全过")

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{'='*50}\n自测试：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    if failed:
        print("未通过：" + "；".join(failed))
        sys.exit(1)
    print("校准全绿")


if __name__ == "__main__":
    main()
