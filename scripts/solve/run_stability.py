# -*- coding: utf-8 -*-
"""多种子稳定性统计：随机类算法的数值可信度闸门（配合 sandbox_run.py）。

为什么必须做：随机类算法（遗传/粒子群/模拟退火/蒙特卡洛…）单次运行结果
有随机波动，论文里引用的可能是运气好的一次。多种子复跑取均值±标准差，
CV（变异系数）过大说明启发式未收敛或实现有随机 bug。

种子由 sandbox_run.run_once 统一注入（random/numpy 双源），被测脚本内
不要再自己设种子。

用法：
    python scripts/solve/run_stability.py 求解脚本.py --runs 10
    python scripts/solve/run_stability.py 脚本.py --algo 蒙特卡洛仿真 --timeout 120
    python scripts/solve/run_stability.py --selftest

判定：max CV ≤ 0.15 稳定；> 0.15 不稳定（论文须报告均值±标准差）；
> 0.5 严重不稳定（结果不可信，必须修代码）。
退出码：0=稳定；1=不稳定/样本不足；2=用法错误。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sandbox_run import run_once  # noqa: E402

CV_STABLE = 0.15
CV_CRITICAL = 0.5


def collect_scalar_metrics(metrics_json: dict) -> dict[str, float]:
    """提取 metrics.json 中的标量指标（跳过 _元数据键/布尔/列表多值）。"""
    out: dict[str, float] = {}
    for key, v in metrics_json.items():
        if not isinstance(key, str) or key.startswith("_") or isinstance(v, bool):
            continue
        vals = v if isinstance(v, list) else [v]
        nums = [x for x in vals
                if isinstance(x, (int, float)) and not isinstance(x, bool)]
        if len(nums) == 1:
            out[key] = float(nums[0])
    return out


def stability_stats(samples: dict[str, list[float]]) -> tuple[dict, float]:
    """各指标均值/标准差/CV。返回 ({key: {mean,std,cv}}, 最大CV)。"""
    stats: dict = {}
    max_cv = 0.0
    for key, vals in samples.items():
        if len(vals) < 2:
            continue
        mean = statistics.mean(vals)
        std = statistics.pstdev(vals)
        if abs(mean) < 1e-12:
            cv = 0.0 if std < 1e-12 else float("inf")
        else:
            cv = abs(std / mean)
        stats[key] = {
            "mean": round(mean, 6),
            "std": round(std, 6),
            "cv": round(min(cv, 99.0), 4),
        }
        max_cv = max(max_cv, cv)
    return stats, max_cv


def _selftest() -> int:
    checks: list[tuple[str, bool]] = []
    stats, max_cv = stability_stats({"x": [1.0, 1.0, 1.0]})
    checks.append(("常数序列 CV=0", stats["x"]["cv"] == 0.0 and max_cv == 0.0))
    stats, max_cv = stability_stats({"x": [1.0, 3.0]})
    # pstdev([1,3])=1.0, mean=2 → cv=0.5
    checks.append(("CV 计算正确", abs(stats["x"]["cv"] - 0.5) < 1e-9 and max_cv == 0.5))
    checks.append(("单样本不进统计",
                   stability_stats({"x": [7.0]})[0] == {}))
    m = collect_scalar_metrics({"最优值": 3.2, "_expected_ranges": [0, 1],
                                "历史": [1, 2, 3], "开关": True})
    checks.append(("标量提取过滤元数据/多值/布尔",
                   list(m.keys()) == ["最优值"] and m["最优值"] == 3.2))

    failed = [name for name, passed in checks if not passed]
    for name, passed in checks:
        print(f"  {'✓' if passed else '✗ FAIL'} {name}")
    return 1 if failed else (print(f"自检通过（{len(checks)} 项）") or 0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("script", nargs="?", help="求解脚本路径")
    ap.add_argument("--runs", type=int, default=10, help="总运行次数（默认 10）")
    ap.add_argument("--algo", default=None, help="算法名（用于动态超时）")
    ap.add_argument("--timeout", type=int, default=None, help="单次超时秒数")
    ap.add_argument("--base-seed", type=int, default=42, help="基准种子（第 k 次用 base+k*1000）")
    ap.add_argument("--selftest", action="store_true", help="运行内置自检")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()
    if not args.script:
        ap.print_help()
        return 2
    script = Path(args.script)
    if not script.exists():
        print(f"脚本不存在: {script}")
        return 2
    if args.runs < 3:
        print("--runs 至少 3 次，否则统计无意义")
        return 2

    samples: dict[str, list[float]] = {}
    failures = 0
    for k in range(args.runs):
        seed = args.base_seed + k * 1000
        print(f"[{k + 1}/{args.runs}] seed={seed} ...", flush=True)
        r = run_once(script, timeout=args.timeout or 60, seed=seed,
                     figures_dir=None)
        if r["status"] != "ok":
            failures += 1
            print(f"  ↳ 运行失败（{r['status']}），跳过该次采样")
            continue
        for key, val in collect_scalar_metrics(r["metrics_json"]).items():
            samples.setdefault(key, []).append(val)

    stats, max_cv = stability_stats(samples)
    print("=" * 60)
    if not stats:
        print(f"有效样本不足（{args.runs - failures}/{args.runs} 次成功，"
              "且需 ≥2 次产出标量指标）——先修到脚本能稳定跑通")
        return 1

    print(f"{'指标':<20} {'均值':>12} {'标准差':>12} {'CV':>8}")
    for key, s in stats.items():
        print(f"{key:<20} {s['mean']:>12.4f} {s['std']:>12.4f} {s['cv']:>8.4f}")

    if max_cv > CV_CRITICAL:
        verdict = "critical"
        advice = (f"最大 CV={max_cv:.2f} > {CV_CRITICAL}：结果不可信！"
                  "检查迭代次数/种群规模是否过小导致未收敛，或实现存在随机缺陷，"
                  "修复后重跑")
    elif max_cv > CV_STABLE:
        verdict = "unstable"
        advice = (f"最大 CV={max_cv:.2f} > {CV_STABLE}：结果波动偏大，"
                  "论文必须报告均值±标准差，且说明波动来源")
    else:
        verdict = "stable"
        advice = f"最大 CV={max_cv:.4f} ≤ {CV_STABLE}：结果稳定，论文引用均值±标准差"
    print("-" * 60)
    print(f"判定: {verdict} — {advice}")
    if failures:
        print(f"注意: {failures}/{args.runs} 次运行失败（超时/报错），"
              "本身也是不稳定的信号")
    print(json.dumps({"verdict": verdict, "max_cv": round(min(max_cv, 99.0), 4),
                      "runs": args.runs, "failures": failures, "stats": stats},
                     ensure_ascii=False, indent=2))
    return 0 if verdict == "stable" else 1


if __name__ == "__main__":
    sys.exit(main())
