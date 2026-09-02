"""2023 CUMCM A题：定日镜场的优化设计 — v2 物理修正版（逐镜尺寸）

v1 的问题（自查确认）：
  1. eta_sb = 1.0 硬编码（无阴影遮挡计算）→ v2 用投影法真实计算
  2. 截断效率用粗糙几何重叠比 → v2 用太阳模糊盘解析模型（零蒙特卡洛）
  3. 余弦效率公式 → v2 用标准角平分线公式 cos θ = sqrt((1+s·r)/2)

v2 物理模型：
  η = η_cos · η_sb · η_at · η_trunc · η_ref

  η_cos   : cos θ = sqrt((1+s·r)/2)
  η_sb    : 投影法。阴影=沿太阳光线把邻居镜投影到本镜平面；阻挡=沿反射
            光线同法投影（附加 t<d 条件）。多邻居取并集面积（栅格化），
            避免重叠阴影重复累加。支持逐镜尺寸（P3 变尺寸布局）。
  η_at    : 题目给定公式 0.99321 − 0.0001176·d + 1.97e−8·d²
  η_trunc : 太阳模糊盘模型。镜面网格点反射光斑 = 半径 d·tan(4.65mrad) 圆盘，
            截断效率 = 圆-圆透镜交集面积占比的网格均值。
            集热器按圆柱等效接受盘 R_eq=4.0m。
  η_ref   : 0.92（题目给定）

运行：python scripts/solve_A_2023_v2.py
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import openpyxl

# ══════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════
LATITUDE = 39.4          # 北纬（度）
ALTITUDE_KM = 3.0        # 海拔（km）
FIELD_RADIUS = 350.0     # 镜场半径（m）
TOWER_HEIGHT = 80.0      # 吸收塔高（m）
COLLECTOR_HEIGHT = 8.0   # 集热器高（m）
COLLECTOR_D = 7.0        # 集热器直径（m）
R_COLLECTOR_EQ = 4.0     # 圆柱等效接受半径（截面7×8 面积≈56m² → R_eq≈4.2，取4.0）
ETA_REF = 0.92           # 镜面反射率
G0 = 1.366               # 太阳常数 kW/m²
SUN_HALF_ANGLE = 4.65e-3 # 太阳角半径（rad）
TIME_POINTS = [9.0, 10.5, 12.0, 13.5, 15.0]
MONTHS = list(range(1, 13))
# 每月21日距春分(3月21日=第0天)的天数：1月21日在春分前59天
DAYS_FROM_EQUINOX = [-59, -28, 0, 31, 61, 92, 122, 153, 184, 214, 245, 275]
Z_HAT = np.array([0.0, 0.0, 1.0])

# 光斑网格（镜面局部坐标 8×8）
_GRID_N = 8
_GUV = np.stack(np.meshgrid(
    np.linspace(-0.5, 0.5, _GRID_N), np.linspace(-0.5, 0.5, _GRID_N)
), axis=-1).reshape(-1, 2)  # (G,2)


# ══════════════════════════════════════════════════════════
# 太阳位置与 DNI
# ══════════════════════════════════════════════════════════

def solar_declination(day: float) -> float:
    return np.arcsin(np.sin(2 * np.pi * day / 365) * np.sin(np.radians(23.45)))


def sun_vector(alt: float, az: float) -> np.ndarray:
    """太阳方向单位向量（镜场坐标系 x东 y北 z上，方位角自正北顺时针）。"""
    return np.array([np.cos(alt) * np.sin(az),
                     np.cos(alt) * np.cos(az),
                     np.sin(alt)])


def solar_position(day: float, time_h: float) -> tuple[float, float, np.ndarray]:
    """返回 (高度角, 方位角, 太阳单位向量)；太阳在地平线下时 alt<=0。"""
    decl = solar_declination(day)
    ha = np.pi / 12 * (time_h - 12)
    lat = np.radians(LATITUDE)
    sin_alt = np.cos(decl) * np.cos(lat) * np.cos(ha) + np.sin(decl) * np.sin(lat)
    alt = np.arcsin(np.clip(sin_alt, -1, 1))
    cos_az = (np.sin(decl) - np.sin(alt) * np.sin(lat)) / (np.cos(alt) * np.cos(lat) + 1e-12)
    az = np.arccos(np.clip(cos_az, -1, 1))
    if ha > 0:
        az = 2 * np.pi - az
    return alt, az, sun_vector(alt, az)


def dni(alt: float) -> float:
    if alt <= 0:
        return 0.0
    H = ALTITUDE_KM
    a = 0.4237 - 0.00821 * (6 - H) ** 2
    b = 0.5055 + 0.00595 * (6.5 - H) ** 2
    c = 0.2711 + 0.01858 * (2.5 - H) ** 2
    return G0 * (a + b * np.exp(-c / np.sin(alt)))


# ══════════════════════════════════════════════════════════
# 几何工具
# ══════════════════════════════════════════════════════════

def mirror_frames(C: np.ndarray, s: np.ndarray, collector_center: np.ndarray):
    """全部镜面的（反射方向、法向、水平轴、竖直轴、距离）。

    C: (N,3) 镜面中心；s: (3,) 太阳单位向量。
    """
    r = collector_center[None, :] - C
    d = np.linalg.norm(r, axis=1)
    r = r / d[:, None]
    n = s[None, :] + r
    n /= np.linalg.norm(n, axis=1)[:, None]
    h = np.cross(n, Z_HAT[None, :])
    hn = np.linalg.norm(h, axis=1)
    h[hn < 1e-8] = np.array([1.0, 0.0, 0.0])
    h[hn >= 1e-8] /= hn[hn >= 1e-8][:, None]
    v = np.cross(h, n)
    return r, n, h, v, d


def lens_area(r1, r2, d):
    """两圆交集面积；r1/r2/d 可为标量或可互相广播的数组。"""
    d_b, r1_b, r2_b = np.broadcast_arrays(
        np.asarray(d, float), np.asarray(r1, float), np.asarray(r2, float))
    out = np.zeros(d_b.shape)
    m_cont = d_b <= np.abs(r1_b - r2_b)
    m_sep = d_b >= r1_b + r2_b
    m_mid = ~(m_cont | m_sep)
    out[m_cont] = np.pi * np.minimum(r1_b, r2_b)[m_cont] ** 2
    if m_mid.any():
        dd = d_b[m_mid]; a = r1_b[m_mid]; b = r2_b[m_mid]
        a1 = a ** 2 * np.arccos(np.clip((dd ** 2 + a ** 2 - b ** 2) / (2 * dd * a), -1, 1))
        a2 = b ** 2 * np.arccos(np.clip((dd ** 2 + b ** 2 - a ** 2) / (2 * dd * b), -1, 1))
        a3 = 0.5 * np.sqrt(np.maximum(
            (-dd + a + b) * (dd + a - b) * (dd - a + b) * (dd + a + b), 0))
        out[m_mid] = a1 + a2 - a3
    return out


def _project_corners_to_frame(corners: np.ndarray, d_ray: np.ndarray,
                              plane_pt: np.ndarray, n_plane: np.ndarray,
                              h_ax: np.ndarray, v_ax: np.ndarray) -> np.ndarray:
    """把角点沿 d_ray 投影到平面(plane_pt,n_plane)，返回局部(u,v)坐标 (4,2)。"""
    denom = float(np.dot(d_ray, n_plane))
    if abs(denom) < 1e-9:
        return np.empty((0, 2))
    t = (np.dot(plane_pt - corners, n_plane)) / denom
    pts = corners + t[:, None] * d_ray[None, :]
    rel = pts - plane_pt[None, :]
    return np.column_stack([rel @ h_ax, rel @ v_ax])


def _quad_union_area(quads: list[np.ndarray], hw: float, hh: float,
                     grid_n: int = 24) -> float:
    """多个凸四边形在镜面矩形内的**并集**面积（栅格化，避免重叠重复累加）。"""
    if not quads:
        return 0.0
    gu = (np.arange(grid_n) + 0.5) / grid_n * 2 * hw - hw
    gv = (np.arange(grid_n) + 0.5) / grid_n * 2 * hh - hh
    UU, VV = np.meshgrid(gu, gv)
    pts = np.column_stack([UU.ravel(), VV.ravel()])
    covered = np.zeros(len(pts), dtype=bool)
    for quad in quads:
        n_v = len(quad)
        cr = np.zeros((n_v, len(pts)))
        for e in range(n_v):
            a = quad[e]
            b = quad[(e + 1) % n_v]
            cr[e] = (b[0] - a[0]) * (pts[:, 1] - a[1]) - (b[1] - a[1]) * (pts[:, 0] - a[0])
        inside = (np.all(cr >= -1e-12, axis=0)) | (np.all(cr <= 1e-12, axis=0))
        covered |= inside
        if covered.all():
            break
    cell_area = (2 * hw / grid_n) * (2 * hh / grid_n)
    return float(covered.sum()) * cell_area


# ══════════════════════════════════════════════════════════
# 效率计算（支持逐镜尺寸）
# ══════════════════════════════════════════════════════════

def eta_trunc_field(r, h, v, d, mirror_w, mirror_h) -> np.ndarray:
    """太阳模糊盘截断效率（N×G 网格；mirror_w/h 可为标量或 (N,) 数组）。"""
    w_arr = np.broadcast_to(np.asarray(mirror_w, float), d.shape)
    h_arr = np.broadcast_to(np.asarray(mirror_h, float), d.shape)
    Rb = d * SUN_HALF_ANGLE
    off = (w_arr[:, None, None] * _GUV[None, :, 0, None] * h[:, None, :]
           + h_arr[:, None, None] * _GUV[None, :, 1, None] * v[:, None, :])
    off_par = np.sum(off * r[:, None, :], axis=2, keepdims=True)
    off_perp = off - off_par * r[:, None, :]
    dist = np.linalg.norm(off_perp, axis=2)
    inter = lens_area(Rb[:, None], R_COLLECTOR_EQ, dist)
    return inter.mean(axis=1) / (np.pi * Rb ** 2)


def eta_sb_field(C, r, n, h, v, d, s, mirror_w, mirror_h) -> np.ndarray:
    """阴影遮挡效率：投影法 + 并集面积（支持逐镜尺寸）。"""
    N = len(C)
    w_arr = np.broadcast_to(np.asarray(mirror_w, float), d.shape)
    h_arr = np.broadcast_to(np.asarray(mirror_h, float), d.shape)
    A = w_arr * h_arr
    hw = w_arr / 2
    hh = h_arr / 2
    half_diag = 0.5 * np.sqrt(w_arr ** 2 + h_arr ** 2)
    signs = np.array([[1, 1], [1, -1], [-1, -1], [-1, 1]])
    corners = (C[:, None, :]
               + signs[None, :, 0, None] * hw[:, None, None] * h[:, None, :]
               + signs[None, :, 1, None] * hh[:, None, None] * v[:, None, :])
    loss = np.zeros(N)
    d_sh = -s
    idx = np.arange(N)
    rej_self = 2 * half_diag + 0.5

    shadow_quads: list[list[np.ndarray]] = [[] for _ in range(N)]
    block_quads: list[list[np.ndarray]] = [[] for _ in range(N)]

    # ── 阴影：j 沿太阳光线投影到 i 平面 ──
    for i in range(N):
        denom = float(np.dot(d_sh, n[i]))
        if denom >= -1e-9:
            continue
        t = (C[i] - C) @ n[i] / denom
        X = C + t[:, None] * d_sh[None, :]
        rel = X - C[i]
        u = rel @ h[i]
        w = rel @ v[i]
        rej = rej_self[i] + rej_self
        cand = np.where((t > 0.05) & (np.abs(u) < rej) & (np.abs(w) < rej)
                        & (idx != i))[0]
        for j in cand:
            quad = _project_corners_to_frame(corners[j], d_sh, C[i], n[i], h[i], v[i])
            if len(quad) < 3:
                continue
            if (quad[:, 0].max() < -hw[i] or quad[:, 0].min() > hw[i] or
                    quad[:, 1].max() < -hh[i] or quad[:, 1].min() > hh[i]):
                continue
            shadow_quads[i].append(quad)

    # ── 阻挡：i 的反射光线被 j 挡住（j 为阻挡者，向量化 i）──
    for j in range(N):
        denom = r @ n[j]
        valid = denom > 1e-9
        t = (C[j] - C) @ n[j] / np.where(valid, denom, 1)
        X = C + t[:, None] * r
        rel = X - C[j]
        u = rel @ h[j]
        w = rel @ v[j]
        rej = rej_self[j] + rej_self
        cand = np.where(valid & (t > 0.05) & (t < d) &
                        (np.abs(u) < rej) & (np.abs(w) < rej) & (idx != j))[0]
        for i in cand:
            quad = _project_corners_to_frame(corners[i], r[i], C[j], n[j], h[j], v[j])
            if len(quad) < 3:
                continue
            if (quad[:, 0].max() < -hw[j] or quad[:, 0].min() > hw[j] or
                    quad[:, 1].max() < -hh[j] or quad[:, 1].min() > hh[j]):
                continue
            block_quads[i].append(quad)

    for i in range(N):
        if shadow_quads[i] or block_quads[i]:
            loss[i] = _quad_union_area(shadow_quads[i] + block_quads[i],
                                       float(hw[i]), float(hh[i]))

    return np.clip(1.0 - loss / A, 0.0, 1.0)


# ══════════════════════════════════════════════════════════
# 场级评估（支持逐镜尺寸 + 塔位置偏移）
# ══════════════════════════════════════════════════════════

def evaluate_field(xy: np.ndarray, mirror_w, mirror_h, install_h,
                   tower_xy: tuple[float, float] = (0.0, 0.0),
                   verbose: bool = False, months=MONTHS, times=TIME_POINTS) -> dict:
    """评估定日镜场。mirror_w/h/install_h 可为标量或长度 N 的数组。"""
    xy = np.asarray(xy, float)
    N = len(xy)
    w_arr = np.broadcast_to(np.asarray(mirror_w, float), (N,))
    h_arr = np.broadcast_to(np.asarray(mirror_h, float), (N,))
    z_arr = np.broadcast_to(np.asarray(install_h, float), (N,))
    A = w_arr * h_arr
    total_area = float(np.sum(A))

    C = np.column_stack([xy[:, 0], xy[:, 1], z_arr])
    collector_center = np.array([tower_xy[0], tower_xy[1],
                                 TOWER_HEIGHT + COLLECTOR_HEIGHT / 2])

    monthly = []
    sums = dict(eta=0.0, cos=0.0, sb=0.0, at=0.0, trunc=0.0, power=0.0, unit=0.0, cnt=0)

    t0 = time.time()
    for mi, month in enumerate(months):
        day = DAYS_FROM_EQUINOX[month - 1]
        m = dict(eta=0.0, cos=0.0, sb=0.0, at=0.0, trunc=0.0, unit=0.0, cnt=0)
        for t in times:
            alt, az, s = solar_position(day, t)
            if alt <= np.radians(1.0):
                continue
            dni_v = dni(alt)
            r, n, h, v, d = mirror_frames(C, s, collector_center)
            e_cos = np.sqrt(np.clip((1 + np.sum(s[None, :] * r, axis=1)) / 2, 0, 1))
            e_at = 0.99321 - 0.0001176 * d + 1.97e-8 * d ** 2
            e_tr = eta_trunc_field(r, h, v, d, w_arr, h_arr)
            e_sb = eta_sb_field(C, r, n, h, v, d, s, w_arr, h_arr)
            eta = e_sb * e_cos * e_at * e_tr * ETA_REF
            power_kw = dni_v * float(np.sum(A * eta))
            unit = power_kw / total_area
            m["eta"] += float(np.mean(eta)); m["cos"] += float(np.mean(e_cos))
            m["sb"] += float(np.mean(e_sb)); m["at"] += float(np.mean(e_at))
            m["trunc"] += float(np.mean(e_tr)); m["unit"] += unit
            m["cnt"] += 1
            sums["eta"] += float(np.mean(eta)); sums["cos"] += float(np.mean(e_cos))
            sums["sb"] += float(np.mean(e_sb)); sums["at"] += float(np.mean(e_at))
            sums["trunc"] += float(np.mean(e_tr))
            sums["power"] += power_kw; sums["unit"] += unit; sums["cnt"] += 1
        if m["cnt"]:
            monthly.append({
                "month": month, "eta": m["eta"] / m["cnt"], "cos": m["cos"] / m["cnt"],
                "sb": m["sb"] / m["cnt"], "at": m["at"] / m["cnt"],
                "trunc": m["trunc"] / m["cnt"], "unit": m["unit"] / m["cnt"],
            })
        if verbose:
            print(f"  {month:2d}月: η={monthly[-1]['eta']:.4f} cos={monthly[-1]['cos']:.4f} "
                  f"sb={monthly[-1]['sb']:.4f} trunc={monthly[-1]['trunc']:.4f} "
                  f"unit={monthly[-1]['unit']:.4f} kW/m²  [{time.time()-t0:.0f}s]")

    cnt = sums["cnt"]
    return {
        "N": N, "A": float(np.mean(A)), "total_area": total_area,
        "monthly": monthly,
        "annual": {
            "eta": sums["eta"] / cnt, "cos": sums["cos"] / cnt, "sb": sums["sb"] / cnt,
            "at": sums["at"] / cnt, "trunc": sums["trunc"] / cnt,
            "power_mw": sums["power"] / cnt / 1000, "unit": sums["unit"] / cnt,
        },
        "elapsed_s": time.time() - t0,
    }


# ══════════════════════════════════════════════════════════
# 主程序：问题1
# ══════════════════════════════════════════════════════════

def main():
    base = Path("data/2023A")   # ← 改成你的 2023A 数据目录（含 附件.xlsx）
    print("=" * 64)
    print("2023 CUMCM A题 v2 — 投影法阴影遮挡 + 太阳模糊盘截断模型")
    print("=" * 64)

    wb = openpyxl.load_workbook(base / '附件.xlsx', data_only=True)
    ws = wb.active
    xy = [row for row in ws.iter_rows(min_row=2, values_only=True)
          if row[0] is not None and row[1] is not None]
    wb.close()
    xy = np.array(xy, dtype=float)
    print(f"定日镜: {len(xy)} 面, 尺寸 6m×6m, 安装高度 4m\n")

    res = evaluate_field(xy, 6.0, 6.0, 4.0, verbose=True)

    a = res["annual"]
    print("\n表2 年平均（v2 物理修正版）:")
    print(f"  年平均光学效率   : {a['eta']:.4f}")
    print(f"  年平均余弦效率   : {a['cos']:.4f}")
    print(f"  年平均阴影遮挡效率: {a['sb']:.4f}")
    print(f"  年平均截断效率   : {a['trunc']:.4f}")
    print(f"  年平均输出热功率 : {a['power_mw']:.2f} MW")
    print(f"  单位面积年输出   : {a['unit']:.4f} kW/m²")
    print(f"  计算耗时        : {res['elapsed_s']:.0f}s")
    print("\n参考区间（优秀论文）: η 60%-70%, P 36-40 MW, 单位 0.58-0.60 kW/m²")

    import json
    out = Path(__file__).parent.parent / "data" / "results"
    out.mkdir(parents=True, exist_ok=True)
    (out / "A2023_v2_problem1.json").write_text(
        json.dumps({"monthly": res["monthly"], "annual": res["annual"],
                    "N": res["N"], "A": res["A"]},
                   ensure_ascii=False, indent=2, default=float),
        encoding="utf-8")
    print(f"\n结果已保存: {out / 'A2023_v2_problem1.json'}")
    return res


if __name__ == "__main__":
    main()
