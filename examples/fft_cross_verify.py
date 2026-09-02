# -*- coding: utf-8 -*-
"""干涉测厚第二方法：零填充 FFT 交叉验证（双角度一致性裁决）。

用途：极值配对法给出双角度厚度后，用本脚本交叉裁决——
同一晶圆的两个入射角厚度必须一致（偏差 >5% 即极值法跳级/漏半级）。

用法：
    python fft_cross_verify.py <数据目录> <材料键> [文件1 文件2]
    python fft_cross_verify.py ./data sic          # 默认 附件1(10°)/附件2(15°)
    python fft_cross_verify.py ./data si 附件3.xlsx 附件4.xlsx

物理：条纹相位 = 2π·(2·n·d·cosθ₁)·ν → 频率 f = 2·n·d·cosθ₁（cycles/cm⁻¹）
      d = f / (2·n·cosθ₁)。量纲自检：μm 级外延层 → f ≈ 10⁻³ 量级。
注意：仅去均值（去基线窗口会吃掉真条纹峰）；突增峰检测列前 3 候选；
      候选呈整数比 = 多光束干涉特征。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.signal import find_peaks

N = {"sic": 2.65, "si": 3.42}          # 红外波段常数近似（色散修正见 PITFALLS）
DEFAULT_FILES = {"sic": ("附件1.xlsx", "附件2.xlsx"), "si": ("附件3.xlsx", "附件4.xlsx")}
ANGLES = (10.0, 15.0)


def load(path: Path):
    df = pd.read_excel(path) if path.suffix.lower() == ".xlsx" else pd.read_csv(path)
    d = df.to_numpy(float)
    d = d[d[:, 1] > 0]
    return d[:, 0], d[:, 1]


def fft_thickness(wn, ref, n: float, theta_deg: float, pad: int = 16):
    """零填充 FFT 主频（突增峰检测）→ (厚度 μm, 频率, 前3候选)。"""
    grid = np.linspace(wn.min(), wn.max(), len(wn))
    ref_g = np.interp(grid, wn, ref.astype(float))
    ref_g = ref_g - np.mean(ref_g)                     # 仅去均值！
    spec = np.abs(np.fft.rfft(ref_g, len(ref_g) * pad))
    dnu = grid[1] - grid[0]
    freqs = np.fft.rfftfreq(len(ref_g) * pad, d=dnu)
    band = (freqs > 0.002) & (freqs < 0.5)
    pm, fm = spec[band], freqs[band]
    peaks, _ = find_peaks(pm, prominence=max(pm.max() * 0.02, 1e-9), distance=5)
    order = peaks[np.argsort(pm[peaks])[::-1]] if len(peaks) else [int(np.argmax(pm))]
    cands = [(fm[k], pm[k]) for k in order[:3]]
    sin_t1 = np.sin(np.radians(theta_deg)) / n
    cos_t1 = np.sqrt(1 - sin_t1 ** 2)
    d_um = cands[0][0] / (2 * n * cos_t1) * 1e4
    return d_um, cands[0][0], cands, cos_t1


def main():
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data")
    mat = (sys.argv[2] if len(sys.argv) > 2 else "sic").lower()
    files = tuple(sys.argv[3:5]) or DEFAULT_FILES[mat]
    n = N[mat]

    results = []
    cand_lists = []
    for fname, theta in zip(files, ANGLES):
        wn, ref = load(data_dir / fname)
        d_um, f, cands, cos_t1 = fft_thickness(wn, ref, n, theta)
        results.append((fname, theta, d_um))
        cand_lists.append([fk / (2 * n * cos_t1) * 1e4 for fk, amp in cands])
        print(f"{fname}（{theta}°）: 主频 {f:.5f} → 厚度 {d_um:.2f} μm")
        print("    候选峰（按显著度，整数比=多光束特征）:")
        for rank, (fk, amp) in enumerate(cands, 1):
            dk = fk / (2 * n * cos_t1) * 1e4
            print(f"      {rank}. f={fk:.5f} → d={dk:.2f} μm (幅度 {amp:.0f})")

    dev = abs(results[0][2] - results[1][2]) / np.mean([r[2] for r in results]) * 100
    joint = np.mean([r[2] for r in results])
    flag = "✓ 一致" if dev <= 5 else "✗ 不一致（>5%：极值法/主频选择有错，须排查）"
    print(f"\n双角度偏差: {dev:.2f}% {flag}")
    if dev <= 5:
        print(f"联合估计厚度: {joint:.2f} μm（n≈{n} 常数近似；色散修正见 PITFALLS 干涉测厚节）")

    def _harmonic_family(lists):
        """任一角度的候选内部出现近似整数比 → 多光束/谐波家族，频率无法唯一定真值。"""
        for lst in lists:
            for i in range(len(lst)):
                for j in range(i + 1, len(lst)):
                    if lst[j] > 0:
                        r = lst[j] / lst[i]
                        if 1.7 <= r <= 2.3 or 2.7 <= r <= 3.3:
                            return True
        return False

    if dev > 5 or _harmonic_family(cand_lists):
        print("注意：候选峰呈整数比（多光束特征）或主频受低频包络干扰时，频率无法唯一定真值——")
        print("      用极值配对法（求解主脚本）或物理先验从自洽候选中裁决，勿直接取均值。")


if __name__ == "__main__":
    main()
