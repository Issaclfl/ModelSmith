"""求解脚本偷懒检测（AST 预检，零依赖）。

背景（2023A 实测）：曾生成 eta_sb=1.0 硬编码 + 种群5/迭代2 的代码，
几分钟"跑完"但结果偏差 21%。本检测在执行前用 AST 扫描赋值语句，
拦截三类偷懒：种群过小 / 迭代过小 / 效率常数硬编码。
规则与平台 solver._check_lazy_code 同口径。

用法：
    python laziness_check.py solve.py [solve2.py ...]
    python laziness_check.py examples/*.py

退出码：0 = 全部通过，1 = 发现偷懒模式，2 = 文件无法读取。

检测规则：
  1. 种群变量 < 20（pop/pop_size/population/num_particles/NP 等）
  2. 迭代变量 < 30（generations/max_iter/epochs 等）
  3. 物理效率变量（eta_* / *efficiency / eff_*）被赋 (0,1] 常数
     ——效率必须由模型公式计算；镜面反射率等题目给定常数用
     ref/reflect 命名，不在检测范围；重叠率等设计参数勿用 eta 命名
"""
from __future__ import annotations

import ast
import sys

MIN_POP = 20
MIN_GEN = 30

_POP_EXACT = {
    "pop", "pop_size", "population", "population_size", "npop", "popsize",
    "num_particles", "n_particles", "swarm_size", "num_agents", "n_agents",
}
_POP_EXACT_CASE = {"NP"}   # 差分进化文献标准记号（大写 NP，区别于 numpy 别名 np）
_ITER_EXACT = {
    "generations", "ngen", "n_gen", "max_gen", "max_generations",
    "num_generations", "max_iter", "n_iter", "num_iter", "num_iterations",
    "iterations", "iters", "maxiters", "max_iterations", "n_iterations",
    "num_epochs", "max_epochs", "epochs",
}


def _sanitize_code(code: str) -> str:
    """清洗控制字符（LLM 输出偶发混入 NUL 字节，ast 解析会报错）。"""
    return code.replace("\x00", "")


def check_lazy_code(code: str) -> list[str]:
    """AST 扫描伪优化代码。返回违规描述列表（空 = 通过）。"""
    issues: list[str] = []

    try:
        tree = ast.parse(_sanitize_code(code))
    except SyntaxError:
        return issues  # 语法错误由运行阶段暴露，不在本检测范围

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        val = node.value
        if not (isinstance(val, ast.Constant) and isinstance(val.value, (int, float))):
            continue  # 只查常数赋值；函数调用计算的种群/迭代不拦
        v = val.value
        for tgt in node.targets:
            if not isinstance(tgt, ast.Name):
                continue
            name = tgt.id.lower()
            # 1/2. 种群与迭代下限
            is_pop = tgt.id in _POP_EXACT_CASE or name in _POP_EXACT \
                or "population" in name or "pop_size" in name or "particles" in name
            is_iter = name in _ITER_EXACT or "generation" in name
            if is_pop and v < MIN_POP:
                issues.append(f"种群规模 {tgt.id}={v} 低于下限 {MIN_POP}")
            elif is_iter and v < MIN_GEN:
                issues.append(f"迭代次数 {tgt.id}={v} 低于下限 {MIN_GEN}")
            # 3. 效率硬编码（排除 ref/reflect——反射率允许为题目给定常数）
            elif ("eta" == name[:3] or name.startswith("eta_") or name.endswith("_eta")
                  or "efficiency" in name or name.startswith("eff_")) \
                    and "ref" not in name and 0 < v <= 1.0:
                issues.append(
                    f"物理效率 {tgt.id}={v} 被硬编码为常数（必须由模型公式计算）")
    return issues


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)

    total_issues = 0
    for path in sys.argv[1:]:
        try:
            code = open(path, encoding="utf-8").read()
        except OSError as e:
            print(f"[SKIP] {path}: {e}")
            continue
        issues = check_lazy_code(code)
        if issues:
            total_issues += len(issues)
            print(f"[FAIL] {path}")
            for iss in issues:
                print(f"  - {iss}")
        else:
            print(f"[PASS] {path}")

    if total_issues:
        print(f"\n共 {total_issues} 处偷懒模式：种群/迭代不足或效率硬编码，"
              "先修代码再跑，禁止用象征性计算冒充求解")
        sys.exit(1)
    print("\n偷懒检测通过")


if __name__ == "__main__":
    main()
