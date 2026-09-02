# -*- coding: utf-8 -*-
"""SiC 双候选裁决：色散模型 + 局部条纹频率（配合 fft_cross_verify.py 使用）。

用途：FFT 交叉验证给出多个厚度候选时（如 7.7 vs 13.4 μm），用本脚本裁决——
色散介质中条纹频率必须随波数啁啾：f_local(ν) = 2·n(ν)·d·cosθ₁。
在 n(ν) 已知的高波数窗口测 f_local 反解 d，各候选给出不同理论 f，数据裁决。

用法：
    python sic_discriminate.py <数据目录> [文件1 文件2]
    python sic_discriminate.py ./data          # 默认 附件1(10°)/附件2(15°)

物理：条纹频率 f = OPD = 2·n·d·cosθ₁（cycles/cm⁻¹），Snell: sinθ₁=sinθ₀/n。
判据：若某候选在所有窗口都对不上实测 f，或实测 f 随 ν 近似恒定（不随 n(ν)
      啁啾），则该细条纹不属于色散薄膜——多半是装置光程（窗口/分束器等厚
      etalon），见 PITFALLS 干涉测厚"恒定 OPD 异常"坑。
注意：去基线窗口过宽会吃掉细条纹（本脚本节1/节2 因 savgol 窗口不同可差
      一倍，读数以量级判断为主）；最终裁决应上传输矩阵全谱拟合。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.signal import find_peaks, savgol_filter

ANGLES = (10.0, 15.0)
DEFAULT_FILES = ("附件1.xlsx", "附件2.xlsx")
CANDIDATES_UM = (7.7, 13.4)            # 待裁决候选厚度

# 4H-SiC 单声子 Lorentz 色散（近似；量值自检：ν→∞ 时 n→√ε∞=2.56 ✓）
W_TO, W_LO, EPS_INF = 797.0, 971.0, 6.56


def n_sic(nu):
    """4H-SiC 折射率色散。剩余射线带（797-971 cm⁻¹）内返回 NaN。"""
    nu = np.asarray(nu, dtype=float)
    n2 = EPS_INF * (W_LO**2 - nu**2) / (W_TO**2 - nu**2)
    n2 = np.where((nu > W_TO) & (nu < W_LO), np.nan, n2)
    return np.sqrt(np.abs(n2)) * np.sign(n2)


def load(path: Path):
    df = pd.read_excel(path)
    d = df.to_numpy(float)
    d = d[d[:, 1] > 0]
    return d[:, 0], d[:, 1]


def local_freq(wn, ref, lo, hi, win=61, prom=0.03):
    """窗口内局部条纹频率（cycles/cm⁻¹）。"""
    m = (wn >= lo) & (wn <= hi)
    w, r = wn[m], ref[m]
    if len(w) < 100:
        return None, None, None
    r_d = r - savgol_filter(r, win, 3)
    peaks, _ = find_peaks(r_d, distance=8, prominence=prom)
    valleys, _ = find_peaks(-r_d, distance=8, prominence=prom)
    ext = np.sort(np.concatenate([w[peaks], w[valleys]]))
    if len(ext) < 4:
        return None, None, None
    spacing = float(np.median(np.diff(ext)))   # 相邻极值间隔 = 半条纹周期
    return 1.0 / (2 * spacing), spacing, len(ext)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    data_dir = Path(sys.argv[1])
    files = tuple(sys.argv[2:]) or DEFAULT_FILES

    print(f"色散自检: n(3333)={float(n_sic(3333)):.3f}（文献≈2.55-2.6）")
    for k, (fname, theta) in enumerate(zip(files, ANGLES)):
        wn, ref = load(data_dir / fname)
        print(f"\n== {fname}（{theta}°）==")
        for lo, hi in ((1500, 4000), (2200, 3000), (3000, 4000)):
            f, spacing, n_ext = local_freq(wn, ref, lo, hi)
            if f is None:
                print(f"  {lo}-{hi}: 极值不足")
                continue
            n_mid = float(n_sic((lo + hi) / 2))
            cos_t1 = np.sqrt(1 - (np.sin(np.radians(theta)) / n_mid) ** 2)
            d_um = f / (2 * n_mid * cos_t1) * 1e4
            theory = "  ".join(
                f"{d}μm理论f={2 * n_mid * d * 1e-4 * cos_t1:.5f}"
                for d in CANDIDATES_UM
            )
            print(f"  {lo}-{hi}: 极值{n_ext} 间隔{spacing:.1f}/cm⁻¹ → f={f:.5f}"
                  f" → d={d_um:.1f}μm   [{theory}]")


if __name__ == "__main__":
    main()
