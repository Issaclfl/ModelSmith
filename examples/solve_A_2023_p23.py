"""2023 CUMCM A题 — 问题2/3：定日镜场优化设计（代理粗筛 + 全量终评）

设计变量：
  P2: 镜面尺寸（统一）、安装高度（统一）、塔位置(y向偏移)、六边形布局密度
  P3: 半径分带变尺寸/变高度 + 塔位置

策略（应对全量评估 ~1-2min/次 的算力约束）：
  - 代理评估：随机采样 300 面镜 × 12 代表时刻 ≈ 3s/次，用于配置粗筛
  - 终评：候选最优配置用全量 60 时刻模型精确计算（逐镜尺寸）
  - 布局：六边形蜂窝排列（最近邻 = 宽+5，理论最密合法布点）
  - 塔北移：南向镜场扩大 → 余弦效率提升（北纬39.4°太阳偏南）

输出：data/results/result2.xlsx、result3.xlsx（题目模板格式）
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import openpyxl

sys.path.insert(0, str(Path(__file__).parent))          # scripts/
sys.path.insert(0, str(Path(__file__).parent.parent))   # 项目根

from solve_A_2023_v2 import evaluate_field

RATED_MW = 60.0
FIELD_R = 350.0
TOWER_EXCL = 100.0        # 塔周围禁区半径
SUR_N = 300               # 代理评估采样镜数
SUR_MONTHS = [3, 6, 9, 12]
SUR_TIMES = [9.0, 12.0, 15.0]
SUR_FEAS_MW = 55.0        # 代理候选阈值（代理对高密度阴影偏乐观，终评定可行性）
GOLDEN = np.pi * (3 - np.sqrt(5))

RESULTS_DIR = Path(__file__).parent.parent / "data" / "results"


# ══════════════════════════════════════════════════════════
# 布局生成：六边形蜂窝 + 塔周围禁区
# ══════════════════════════════════════════════════════════

def gen_hex_layout(size_by_radius, height_by_radius, tower_xy=(0.0, 0.0),
                   gap_extra: float = 0.0):
    """六边形蜂窝排列（最近邻距离 = 该处镜宽+5+余量，按最大镜宽统一网格）。"""
    r_probe = np.linspace(0.0, FIELD_R, 400)
    w_max = max(size_by_radius(max(1e-3, r)) for r in r_probe)
    gap = w_max + 5.0 + gap_extra
    dy = gap * np.sqrt(3) / 2

    xy, sizes, heights = [], [], []
    y = -FIELD_R
    row = 0
    while y <= FIELD_R:
        x = -FIELD_R + (gap / 2 if row % 2 else 0.0)
        while x <= FIELD_R:
            r_abs = float(np.hypot(x, y))
            d_tower = float(np.hypot(x - tower_xy[0], y - tower_xy[1]))
            if r_abs <= FIELD_R and d_tower >= TOWER_EXCL:
                w = size_by_radius(max(1e-3, r_abs))
                xy.append((x, y))
                sizes.append(w)
                heights.append(4.0 if height_by_radius is None
                               else height_by_radius(r_abs))
            x += gap
        y += dy
        row += 1
    return np.array(xy), np.array(sizes), np.array(heights)


def gen_two_band(r_split: float, w_near: float, w_far: float,
                 h_near: float, h_far: float, tower_xy=(0.0, 0.0)):
    """双带布局：近带小镜密格 + 远带大镜疏格，跨界冲突贪婪修剪。

    每带独立六边形网格（间距 = 该带镜宽+5），合并后检查所有近距点对，
    违反 (w_i+w_j)/2+5 约束者按违规数贪婪移除。
    """
    def band_points(r_lo, r_hi, w):
        gap = w + 5.0
        dy = gap * np.sqrt(3) / 2
        pts = []
        y = -r_hi
        row = 0
        while y <= r_hi:
            x = -r_hi + (gap / 2 if row % 2 else 0.0)
            while x <= r_hi:
                r_abs = float(np.hypot(x, y))
                d_tower = float(np.hypot(x - tower_xy[0], y - tower_xy[1]))
                if r_lo <= r_abs <= r_hi and d_tower >= TOWER_EXCL:
                    pts.append((x, y))
                x += gap
            y += dy
            row += 1
        return pts

    pts = (band_points(TOWER_EXCL + 1.0, r_split, w_near)
           + band_points(r_split - 5.0, FIELD_R, w_far))
    P = np.array(pts)
    r_abs = np.hypot(P[:, 0], P[:, 1])
    w = np.where(r_abs < r_split, w_near, w_far)
    h = np.where(r_abs < r_split, h_near, h_far)

    # ── 冲突检测与贪婪修剪（分块，避免 O(N²) 内存）──
    min_pair = (w + w) / 2 + 5.0        # 点对约束距离下界的近似（同尺寸带内精确）
    cutoff = (max(w_near, w_far) + 5.0) * 2.2
    alive = np.ones(len(P), dtype=bool)
    for _ in range(80):
        idx_alive = np.where(alive)[0]
        if len(idx_alive) < 2:
            break
        counts = np.zeros(len(idx_alive))
        chunk = 400
        for s in range(0, len(idx_alive), chunk):
            sel = idx_alive[s:s + chunk]
            D = np.sqrt(np.maximum(
                ((P[sel][:, None, :] - P[idx_alive][None, :, :]) ** 2).sum(-1),
                1e-12))
            md = (w[sel][:, None] + w[idx_alive][None, :]) / 2 + 5.0
            viol = (D < md) & (D > 1e-9) & (D < cutoff)
            counts[s:s + chunk] = viol.sum(1)
        if counts.max() == 0:
            break
        alive[idx_alive[int(np.argmax(counts))]] = False

    return P[alive], w[alive], h[alive]


# ══════════════════════════════════════════════════════════
# 代理评估
# ══════════════════════════════════════════════════════════

def surrogate_eval(xy, sizes, heights, tower_xy, rng) -> dict:
    """采样代理评估（12 代表时刻）。排名用；可行性以全量终评为准。"""
    N_full = len(xy)
    idx = rng.choice(N_full, size=min(SUR_N, N_full), replace=False)
    res = evaluate_field(xy[idx], sizes[idx], sizes[idx], heights[idx],
                         tower_xy=tower_xy, months=SUR_MONTHS, times=SUR_TIMES)
    unit = res["annual"]["unit"]
    total_area = float(np.sum(sizes ** 2))
    return {"unit": unit, "total_mw": unit * total_area / 1000,
            "N": N_full, "total_area": total_area}


# ══════════════════════════════════════════════════════════
# 写结果文件（题目模板格式）
# ══════════════════════════════════════════════════════════

def write_result(path: Path, xy, sizes, heights, tower_xy):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["吸收塔x坐标 (m)", "吸收塔y坐标 (m)", "定日镜序号",
               "定日镜宽度 (m)", "定日镜高度 (m)",
               "定日镜x坐标 (m)", "定日镜y坐标 (m)", "定日镜z坐标 (m)"])
    for i in range(len(xy)):
        ws.append([
            tower_xy[0] if i == 0 else None,
            tower_xy[1] if i == 0 else None,
            i + 1, float(sizes[i]), float(heights[i]),
            round(float(xy[i, 0]), 3), round(float(xy[i, 1]), 3), float(heights[i]),
        ])
    wb.save(path)


# ══════════════════════════════════════════════════════════
# 主程序
# ══════════════════════════════════════════════════════════

def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(42)

    print("=" * 64)
    print("2023 CUMCM A题 — 问题2/3（六边形布局 + 塔位优化 + 代理粗筛）")
    print("=" * 64)

    TOWER_YS = (0.0, 60.0)   # 塔北移候选（y轴正向为北）

    # ── 问题2：统一尺寸 ──
    print("\n【问题2】统一尺寸粗搜（六边形布局，候选阈值 55MW）")
    print(f"{'尺寸':>6} {'塔y':>5} {'N':>6} {'面积':>9} {'P_est':>8} {'unit_est':>9}")
    cands2 = []
    for size in (5.5, 6.0, 6.5):
        for ty in TOWER_YS:
            xy, sizes, heights = gen_hex_layout(
                lambda r, s=size: s, None, tower_xy=(0.0, ty))
            est = surrogate_eval(xy, sizes, heights, (0.0, ty), rng)
            feas = est["total_mw"] >= RATED_MW
            print(f"{size:6.1f} {ty:5.0f} {est['N']:6d} {est['total_area']:9.0f} "
                  f"{est['total_mw']:8.2f} {est['unit']:9.4f}"
                  + ("  ✓" if feas else ""))
            if est["total_mw"] >= SUR_FEAS_MW:
                cands2.append((est["unit"], f"{size}m/塔y{ty:.0f}",
                               xy, sizes, heights, (0.0, ty)))
    cands2.sort(key=lambda c: -c[0])

    # ── 问题3：分带变尺寸 ──
    print("\n【问题3】分带变尺寸粗搜")
    zone_cfgs = {
        "4/6/8":  (lambda r: 4.0 if r < 180 else (6.0 if r < 270 else 8.0),
                   lambda r: 3.0 if r < 180 else (4.0 if r < 270 else 5.0)),
        "5/6/8":  (lambda r: 5.0 if r < 180 else (6.0 if r < 270 else 8.0),
                   lambda r: 3.0 if r < 180 else (4.0 if r < 270 else 5.0)),
        "4.5/6.5/8": (lambda r: 4.5 if r < 180 else (6.5 if r < 270 else 8.0),
                      lambda r: 3.0 if r < 180 else (4.0 if r < 270 else 5.5)),
    }
    cands3 = []
    for name, (sf, hf) in zone_cfgs.items():
        for ty in TOWER_YS:
            xy, sizes, heights = gen_hex_layout(sf, hf, tower_xy=(0.0, ty))
            est = surrogate_eval(xy, sizes, heights, (0.0, ty), rng)
            feas = est["total_mw"] >= RATED_MW
            print(f"  {name} 塔y{ty:.0f}: N={est['N']:5d} 面积={est['total_area']:8.0f} "
                  f"P_est={est['total_mw']:6.2f} unit_est={est['unit']:.4f}"
                  + ("  ✓" if feas else ""))
            if est["total_mw"] >= SUR_FEAS_MW:
                cands3.append((est["unit"], f"{name}/塔y{ty:.0f}",
                               xy, sizes, heights, (0.0, ty)))
    cands3.sort(key=lambda c: -c[0])

    # ── P3 双带布局：近带小镜密格 + 远带大镜疏格，直接全量终评 ──
    # （代理对混合网格密度不可靠，跳过粗筛）
    twoband = []
    for r_split, wn, wf, hn, hf in ((210.0, 5.0, 7.0, 3.0, 5.0),
                                     (210.0, 4.5, 7.5, 3.0, 5.5)):
        xy, sizes, heights = gen_two_band(r_split, wn, wf, hn, hf, (0.0, 0.0))
        area = float(np.sum(sizes ** 2))
        print(f"  双带{wn}/{wf} (r_split={r_split:.0f}): N={len(xy)} 面积={area:.0f}m²")
        twoband.append((0.0, f"双带{wn}/{wf}", xy, sizes, heights, (0.0, 0.0)))

    # ── 全量终评（每问最多 3 个候选，选 P≥60 中 unit 最高者）──
    print("\n【全量终评 60 时刻】（每问最多 3 个候选）")
    final = {}
    p3_eval_list = twoband + cands3[:3]     # 双带优先（全量直接评）
    for tag, cands in (("P2", cands2), ("P3", p3_eval_list)):
        if not cands:
            print(f"  {tag}: 无候选")
            continue
        best = None
        full_results = []
        for unit0, name, xy, sizes, heights, txy in cands[:3]:
            res = evaluate_field(xy, sizes, sizes, heights, tower_xy=txy)
            a = res["annual"]
            ok = a["power_mw"] >= RATED_MW
            print(f"  {tag}({name}): N={res['N']} 面积={res['total_area']:.0f}m² "
                  f"η={a['eta']:.4f} P={a['power_mw']:.2f}MW "
                  f"unit={a['unit']:.4f}kW/m² "
                  f"{'✓达额定' if ok else '未达额定'} [{res['elapsed_s']:.0f}s]")
            rec = {"name": name, "xy": xy, "sizes": sizes, "heights": heights,
                   "tower_xy": txy, "annual": a}
            full_results.append(rec)
            if ok and (best is None or a["unit"] > best["annual"]["unit"]):
                best = rec
        if best is None:
            best = max(full_results, key=lambda r: r["annual"]["power_mw"])
            print(f"  {tag}: 所有候选均未达 60MW，取全量功率最高者 "
                  f"P={best['annual']['power_mw']:.2f}MW（如实报告缺口）")
        final[tag] = best

    # ── 写 result 文件 ──
    for tag, fname in (("P2", "result2.xlsx"), ("P3", "result3.xlsx")):
        if tag in final:
            f = final[tag]
            write_result(RESULTS_DIR / fname, f["xy"], f["sizes"],
                         f["heights"], f["tower_xy"])
            a = f["annual"]
            print(f"  已写入 {RESULTS_DIR / fname}：{f['name']}，{len(f['xy'])} 面，"
                  f"P={a['power_mw']:.2f}MW，unit={a['unit']:.4f}kW/m²")

    print("\n完成。")
    return final


if __name__ == "__main__":
    main()
