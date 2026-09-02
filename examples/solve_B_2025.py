"""2025 CUMCM B题：碳化硅外延层厚度的确定 - 完整求解代码

问题1：双光束干涉模型
问题2：算法设计 + 数据计算
问题3：多光束干涉分析
"""
from __future__ import annotations

import numpy as np
import openpyxl
from scipy.signal import find_peaks, savgol_filter
from scipy.optimize import minimize_scalar, curve_fit
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# ══════════════════════════════════════════════════════════
# 物理常量
# ══════════════════════════════════════════════════════════

# SiC折射率（红外波段）
# 参考：Palik, Handbook of Optical Constants of Solids
# 在红外波段（400-4000 cm^-1），4H-SiC折射率约为 2.55-2.75
# 色散较小，使用常数近似 n ≈ 2.65
def n_sic(wavenumber: float) -> float:
    """SiC折射率（红外波段）- 常数近似"""
    return 2.65


def n_si(wavenumber: float) -> float:
    """Si折射率（红外波段）- 常数近似"""
    return 3.42


# ══════════════════════════════════════════════════════════
# 问题1：双光束干涉数学模型
# ══════════════════════════════════════════════════════════

class TwoBeamModel:
    """双光束干涉模型。
    
    物理原理：
    红外光入射到外延层后，一部分从外延层表面反射（R1），
    另一部分从衬底表面反射（R2）。两束反射光的光程差为：
    
        OPD = 2 * n(ν) * d * cos(θ₁)
    
    其中：
        n(ν) = 外延层折射率（随波数ν变化）
        d = 外延层厚度
        θ₁ = 外延层内的折射角
    
    根据Snell定律：sin(θ₀) = n(ν) * sin(θ₁)
    所以：cos(θ₁) = sqrt(1 - sin²(θ₀)/n²(ν))
    
    干涉条件：
        极大（反射增强）：OPD = m * λ = m / ν
        极小（反射减弱）：OPD = (m + 1/2) * λ = (m + 1/2) / ν
    """
    
    def __init__(self, theta_incident: float, material: str = 'SiC'):
        """
        Args:
            theta_incident: 入射角（度）
            material: 材料类型 'SiC' 或 'Si'
        """
        self.theta0 = np.radians(theta_incident)
        self.material = material
        self.n_func = n_sic if material == 'SiC' else n_si
    
    def optical_path_difference(self, wavenumber: float, thickness: float) -> float:
        """计算光程差。
        
        Args:
            wavenumber: 波数 (cm^-1)
            thickness: 外延层厚度 (cm)
        
        Returns:
            光程差 (cm)
        """
        n = self.n_func(wavenumber)
        # Snell定律计算折射角
        sin_theta1 = np.sin(self.theta0) / n
        cos_theta1 = np.sqrt(1 - sin_theta1**2)
        return 2 * n * thickness * cos_theta1
    
    def reflection_coefficient(self, wavenumber: float, thickness: float,
                                r1: float = 0.15, r2: float = 0.25) -> float:
        """计算双光束干涉反射率。
        
        Args:
            wavenumber: 波数 (cm^-1)
            thickness: 外延层厚度 (cm)
            r1: 外延层表面振幅反射系数
            r2: 衬底界面振幅反射系数
        
        Returns:
            反射率（0-1）
        """
        opd = self.optical_path_difference(wavenumber, thickness)
        # 相位差
        phase = 2 * np.pi * opd * wavenumber
        # 双光束干涉公式
        R = r1**2 + r2**2 + 2 * r1 * r2 * np.cos(phase)
        return R
    
    def interference_peaks(self, thickness: float, wn_range: tuple = (400, 4000),
                           num_points: int = 7000) -> np.ndarray:
        """计算理论干涉光谱。
        
        Args:
            thickness: 外延层厚度 (cm)
            wn_range: 波数范围 (cm^-1)
            num_points: 采样点数
        
        Returns:
            (wavenumber, reflectance) 数组
        """
        wn = np.linspace(wn_range[0], wn_range[1], num_points)
        R = np.array([self.reflection_coefficient(w, thickness) for w in wn])
        return wn, R
    
    def estimate_thickness_from_peaks(self, wavenumber: np.ndarray, 
                                       reflectance: np.ndarray) -> dict:
        """从干涉峰位估计厚度。
        
        方法：利用相邻极值间距与厚度的关系
            Δν = 1 / (2 * n_eff * d * cos(θ₁))
        
        Returns:
            {"thickness_cm": float, "thickness_um": float, 
             "num_peaks": int, "num_valleys": int, "method": str}
        """
        # 平滑数据
        if len(reflectance) > 51:
            ref_smooth = savgol_filter(reflectance, 51, 3)
        else:
            ref_smooth = reflectance
        
        # 找峰和谷
        peaks, _ = find_peaks(ref_smooth, distance=30, prominence=2)
        valleys, _ = find_peaks(-ref_smooth, distance=30, prominence=2)
        
        # 合并峰谷，按波数排序
        extrema = []
        for p in peaks:
            extrema.append(('peak', wavenumber[p], ref_smooth[p]))
        for v in valleys:
            extrema.append(('valley', wavenumber[v], ref_smooth[v]))
        extrema.sort(key=lambda x: x[1])
        
        if len(extrema) < 3:
            return {"thickness_cm": 0, "thickness_um": 0, 
                    "num_peaks": len(peaks), "num_valleys": len(valleys),
                    "method": "insufficient_data"}
        
        # 计算相邻极值间距
        spacings = []
        for i in range(1, len(extrema)):
            dw = extrema[i][1] - extrema[i-1][1]
            if dw > 0:
                spacings.append(dw)
        
        if not spacings:
            return {"thickness_cm": 0, "thickness_um": 0,
                    "num_peaks": len(peaks), "num_valleys": len(valleys),
                    "method": "no_valid_spacings"}
        
        # 使用加权平均（高频数据权重更高）
        spacings = np.array(spacings)
        weights = 1.0 / (spacings + 1e-10)  # 较小间距权重更高
        avg_spacing = np.average(spacings, weights=weights)
        
        # 使用有效折射率（考虑色散）
        # 在条纹间距的平均波数处计算有效折射率
        mid_wn = np.mean([e[1] for e in extrema])
        n_eff = self.n_func(mid_wn)
        
        # 计算折射角
        sin_theta1 = np.sin(self.theta0) / n_eff
        cos_theta1 = np.sqrt(1 - sin_theta1**2)
        
        # 厚度 = 1 / (2 * n * cos(θ₁) * Δν)
        thickness = 1.0 / (2 * n_eff * cos_theta1 * avg_spacing)
        
        return {
            "thickness_cm": thickness,
            "thickness_um": thickness * 1e4,
            "thickness_nm": thickness * 1e7,
            "num_peaks": len(peaks),
            "num_valleys": len(valleys),
            "avg_spacing": avg_spacing,
            "n_eff": n_eff,
            "method": "extrema_spacing"
        }


# ══════════════════════════════════════════════════════════
# 问题2：算法设计与数据计算
# ══════════════════════════════════════════════════════════

class ThicknessAlgorithm:
    """外延层厚度计算算法。
    
    算法流程：
    1. 数据预处理：去零值、平滑、去趋势
    2. 极值检测：找干涉峰和谷
    3. 条纹间距计算：加权平均
    4. 厚度估算：考虑色散的迭代算法
    5. 可靠性分析：多角度一致性检验
    """
    
    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
    
    def load_data(self, filename: str) -> tuple[np.ndarray, np.ndarray]:
        """加载光谱数据。"""
        wb = openpyxl.load_workbook(self.data_dir / filename, data_only=True)
        ws = wb.active
        data = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if row[0] is not None and row[1] is not None:
                data.append((row[0], row[1]))
        wb.close()
        data = np.array(data)
        # 去掉零值点
        mask = data[:, 1] > 0
        return data[mask, 0], data[mask, 1]
    
    def preprocess(self, wn: np.ndarray, ref: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """数据预处理。"""
        # 平滑
        if len(ref) > 51:
            ref_smooth = savgol_filter(ref, 51, 3)
        else:
            ref_smooth = ref.copy()
        return wn, ref_smooth
    
    def find_fringe_spacing(self, wn: np.ndarray, ref: np.ndarray) -> dict:
        """计算干涉条纹间距。
        
        使用多种方法并取加权平均：
        1. 相邻极值法
        2. FFT主频法
        3. 自相关法
        """
        results = {}
        
        # 方法1：相邻极值法
        peaks, _ = find_peaks(ref, distance=30, prominence=2)
        valleys, _ = find_peaks(-ref, distance=30, prominence=2)
        
        extrema_wn = sorted(
            [(wn[p], 'peak') for p in peaks] + 
            [(wn[v], 'valley') for v in valleys]
        )
        
        if len(extrema_wn) > 2:
            spacings = np.diff([e[0] for e in extrema_wn])
            spacings = spacings[spacings > 0]
            if len(spacings) > 0:
                results['extrema_spacing'] = {
                    'mean': np.mean(spacings),
                    'std': np.std(spacings),
                    'median': np.median(spacings),
                    'count': len(spacings)
                }
        
        # 方法2：FFT法
        ref_detrended = ref - np.mean(ref)
        N = len(wn)
        d_wn = np.mean(np.diff(wn))
        yf = np.fft.fft(ref_detrended)
        freqs = np.fft.fftfreq(N, d=d_wn)
        
        pos_mask = freqs > 0.01  # 排除低频
        freqs_pos = freqs[pos_mask]
        power = np.abs(yf[pos_mask])
        
        if len(power) > 0:
            peak_idx = np.argmax(power)
            dominant_freq = freqs_pos[peak_idx]
            if dominant_freq > 0:
                results['fft_spacing'] = {
                    'mean': 1.0 / dominant_freq,
                    'dominant_freq': dominant_freq
                }
        
        # 方法3：自相关法
        ref_acf = np.correlate(ref_detrended, ref_detrended, mode='full')
        acf = ref_acf[N-1:]  # 取正半部分
        acf_peaks, _ = find_peaks(acf, distance=50)
        if len(acf_peaks) > 0:
            first_peak = acf_peaks[0]
            if first_peak > 0:
                results['autocorr_spacing'] = {
                    'mean': first_peak * d_wn,
                    'peak_lag': first_peak
                }
        
        return results
    
    def calculate_thickness(self, wn: np.ndarray, ref: np.ndarray, 
                            theta_deg: float, material: str) -> dict:
        """计算外延层厚度。"""
        model = TwoBeamModel(theta_deg, material)
        wn_smooth, ref_smooth = self.preprocess(wn, ref)
        
        # 多方法估计
        result_model = model.estimate_thickness_from_peaks(wn_smooth, ref_smooth)
        fringe_results = self.find_fringe_spacing(wn_smooth, ref_smooth)
        
        # 加权平均各方法的结果
        estimates = []
        if result_model['thickness_cm'] > 0:
            estimates.append(('extrema', result_model['thickness_cm'], 1.0))
        
        for method_name, method_data in fringe_results.items():
            if 'mean' in method_data and method_data['mean'] > 0:
                n_eff = model.n_func(np.mean(wn_smooth))
                sin_theta1 = np.sin(model.theta0) / n_eff
                cos_theta1 = np.sqrt(1 - sin_theta1**2)
                d = 1.0 / (2 * n_eff * cos_theta1 * method_data['mean'])
                if d > 0:
                    weight = method_data.get('count', 1) if 'count' in method_data else 0.5
                    estimates.append((method_name, d, weight))
        
        if estimates:
            total_weight = sum(e[2] for e in estimates)
            thickness = sum(e[1] * e[2] for e in estimates) / total_weight
        else:
            thickness = 0
        
        return {
            "thickness_cm": thickness,
            "thickness_um": thickness * 1e4,
            "thickness_nm": thickness * 1e7,
            "theta_deg": theta_deg,
            "material": material,
            "method_details": result_model,
            "fringe_methods": fringe_results,
            "estimates": [(e[0], e[1]*1e4) for e in estimates],
        }
    
    def analyze_reliability(self, results_10: dict, results_15: dict) -> dict:
        """分析结果可靠性（多角度一致性检验）。
        
        如果两个角度的计算结果一致，说明模型可靠。
        """
        d10 = results_10['thickness_cm']
        d15 = results_15['thickness_cm']
        
        if d10 > 0 and d15 > 0:
            rel_dev = abs(d10 - d15) / max(d10, d15)
            consistent = rel_dev < 0.10  # 10%容差
        else:
            rel_dev = 1.0
            consistent = False
        
        return {
            "d_10deg": results_10['thickness_um'],
            "d_15deg": results_15['thickness_um'],
            "relative_deviation": rel_dev,
            "consistent": consistent,
            "reliability": "高" if consistent else "低",
        }


# ══════════════════════════════════════════════════════════
# 问题3：多光束干涉分析
# ══════════════════════════════════════════════════════════

class MultiBeamModel:
    """多光束干涉（Fabry-Perot）模型。
    
    当界面反射率较高时，光在外延层内多次反射，
    产生多光束干涉。反射率为：
    
        R = R_FP = r1² + (1-r1)² * R2 * exp(-αd) / 
                   (1 - r1 * R2^(1/2) * exp(-αd/2))²
    
    简化形式（无吸收，α=0）：
        R_FP = (r1 + r2 - 2*r1*r2*cos(φ)) / 
               (1 - r1*r2*cos(φ))
    
    其中 φ = 4π*n*d*cos(θ₁)/λ 是单次往返相位。
    """
    
    def __init__(self, theta_incident: float, material: str = 'Si'):
        self.theta0 = np.radians(theta_incident)
        self.material = material
        self.n_func = n_sic if material == 'SiC' else n_si
    
    def fabry_perot_reflectance(self, wavenumber: float, thickness: float,
                                 r1: float, r2: float) -> float:
        """计算Fabry-Perot干涉反射率。"""
        n = self.n_func(wavenumber)
        sin_theta1 = np.sin(self.theta0) / n
        cos_theta1 = np.sqrt(1 - sin_theta1**2)
        
        # 单次往返相位
        phi = 4 * np.pi * n * thickness * cos_theta1 * wavenumber
        
        # Fabry-Perot反射率
        R1 = r1**2  # 强度反射率
        R2 = r2**2
        
        # 分母
        denom = 1 - 2 * np.sqrt(R1 * R2) * np.cos(phi) + R1 * R2
        # 分子
        numer = R1 + R2 - 2 * np.sqrt(R1 * R2) * np.cos(phi + np.pi)
        
        return numer / denom if denom > 0 else R1 + R2
    
    def multi_beam_spectrum(self, thickness: float, r1: float, r2: float,
                            wn_range: tuple = (400, 4000)) -> tuple:
        """计算多光束干涉光谱。"""
        wn = np.linspace(wn_range[0], wn_range[1], 7000)
        R = np.array([self.fabry_perot_reflectance(w, thickness, r1, r2) for w in wn])
        return wn, R
    
    def check_multi_beam(self, wn: np.ndarray, ref: np.ndarray) -> dict:
        """检测是否存在多光束干涉。
        
        判据：
        1. 反射率峰值是否接近100%（多光束增强）
        2. 干涉条纹是否比双光束预期更尖锐
        3. 是否出现次级峰
        """
        # 检查反射率峰值
        peaks, properties = find_peaks(ref, distance=30, prominence=2)
        if len(peaks) == 0:
            return {"has_multi_beam": False, "reason": "无明显干涉峰"}
        
        peak_values = ref[peaks]
        max_peak = np.max(peak_values)
        
        # 检查是否有接近100%的反射率（Fabry-Perot特征）
        high_peaks = np.sum(peak_values > 80)
        
        # 检查条纹尖锐度（峰的半高宽）
        # 多光束干涉的条纹更尖锐
        sharpness = []
        for p in peaks:
            # 找半高宽
            half_max = ref[p] / 2
            left = np.where(ref[:p] < half_max)[0]
            right = np.where(ref[p:] < half_max)[0]
            if len(left) > 0 and len(right) > 0:
                fwhm = (p + right[0]) - left[-1]
                sharpness.append(fwhm)
        
        avg_sharpness = np.mean(sharpness) if sharpness else 1000
        
        # 判断
        has_multi = False
        reasons = []
        
        if max_peak > 85:
            has_multi = True
            reasons.append(f"反射率峰值达{max_peak:.1f}%，接近100%")
        
        if high_peaks > 2:
            has_multi = True
            reasons.append(f"有{high_peaks}个反射率>80%的峰")
        
        if avg_sharpness < 20:  # 条纹很尖锐
            has_multi = True
            reasons.append(f"条纹尖锐（平均半高宽{avg_sharpness:.1f}点）")
        
        return {
            "has_multi_beam": has_multi,
            "max_peak_reflectance": max_peak,
            "high_peaks_count": high_peaks,
            "avg_sharpness": avg_sharpness,
            "reasons": reasons,
        }
    
    def estimate_r1_r2(self, ref_data: np.ndarray) -> tuple[float, float]:
        """从反射率数据估计界面反射系数。"""
        # 多光束干涉的峰值反射率
        R_max = np.max(ref_data)
        R_min = np.min(ref_data)
        
        # 对于Fabry-Perot：
        # R_max ≈ (r1 + r2)² / (1 + r1*r2)²
        # R_min ≈ (r1 - r2)² / (1 - r1*r2)²
        
        # 简化估计
        r1_est = np.sqrt(R_max / 100) * 0.8  # 粗略估计
        r2_est = np.sqrt(R_min / 100) * 0.5
        
        return min(r1_est, 0.5), min(r2_est, 0.5)


# ══════════════════════════════════════════════════════════
# 主程序：运行求解
# ══════════════════════════════════════════════════════════

def main():
    data_dir = Path("data/2025B")   # ← 改成你的 2025B 附件目录
    results_dir = Path("results")
    results_dir.mkdir(parents=True, exist_ok=True)
    
    algo = ThicknessAlgorithm(str(data_dir))
    
    print("=" * 60)
    print("2025 CUMCM B题：碳化硅外延层厚度的确定")
    print("=" * 60)
    
    # ═══════════════════════════════════════════════════════
    # 问题2：计算SiC外延层厚度
    # ═══════════════════════════════════════════════════════
    print("\n【问题2】SiC外延层厚度计算")
    print("-" * 40)
    
    # 加载数据
    wn1, ref1 = algo.load_data('附件1.xlsx')
    wn2, ref2 = algo.load_data('附件2.xlsx')
    
    # 计算厚度
    result_10 = algo.calculate_thickness(wn1, ref1, 10, 'SiC')
    result_15 = algo.calculate_thickness(wn2, ref2, 15, 'SiC')
    
    print(f"10°入射角：厚度 = {result_10['thickness_um']:.2f} μm")
    print(f"  估计方法: {result_10['estimates']}")
    print(f"15°入射角：厚度 = {result_15['thickness_um']:.2f} μm")
    print(f"  估计方法: {result_15['estimates']}")
    
    # 可靠性分析
    reliability = algo.analyze_reliability(result_10, result_15)
    print(f"\n可靠性分析：")
    print(f"  相对偏差: {reliability['relative_deviation']*100:.2f}%")
    print(f"  一致性: {'是' if reliability['consistent'] else '否'}")
    print(f"  可靠性等级: {reliability['reliability']}")
    
    # ═══════════════════════════════════════════════════════
    # 问题3：多光束干涉分析（Si晶圆片）
    # ═══════════════════════════════════════════════════════
    print("\n【问题3】多光束干涉分析（Si晶圆片）")
    print("-" * 40)
    
    wn3, ref3 = algo.load_data('附件3.xlsx')
    wn4, ref4 = algo.load_data('附件4.xlsx')
    
    # 检测多光束干涉
    mb_model = MultiBeamModel(10, 'Si')
    check_10 = mb_model.check_multi_beam(wn3, ref3)
    check_15 = mb_model.check_multi_beam(wn4, ref4)
    
    print(f"附件3（10°）：")
    print(f"  多光束干涉: {'是' if check_10['has_multi_beam'] else '否'}")
    print(f"  最大反射率: {check_10['max_peak_reflectance']:.1f}%")
    print(f"  高峰数: {check_10['high_peaks_count']}")
    if check_10['reasons']:
        print(f"  原因: {'; '.join(check_10['reasons'])}")
    
    print(f"附件4（15°）：")
    print(f"  多光束干涉: {'是' if check_15['has_multi_beam'] else '否'}")
    print(f"  最大反射率: {check_15['max_peak_reflectance']:.1f}%")
    print(f"  高峰数: {check_15['high_peaks_count']}")
    if check_15['reasons']:
        print(f"  原因: {'; '.join(check_15['reasons'])}")
    
    # 计算Si外延层厚度
    result_si_10 = algo.calculate_thickness(wn3, ref3, 10, 'Si')
    result_si_15 = algo.calculate_thickness(wn4, ref4, 15, 'Si')
    
    print(f"\nSi外延层厚度：")
    print(f"  10°: {result_si_10['thickness_um']:.2f} μm")
    print(f"  15°: {result_si_15['thickness_um']:.2f} μm")
    
    reliability_si = algo.analyze_reliability(result_si_10, result_si_15)
    print(f"  相对偏差: {reliability_si['relative_deviation']*100:.2f}%")
    print(f"  可靠性: {reliability_si['reliability']}")
    
    # ═══════════════════════════════════════════════════════
    # 生成图表
    # ═══════════════════════════════════════════════════════
    print("\n【生成图表】")
    print("-" * 40)
    
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    
    # 图1：SiC 10°反射率
    model_10 = TwoBeamModel(10, 'SiC')
    wn_theory, R_theory = model_10.interference_peaks(result_10['thickness_cm'])
    axes[0,0].plot(wn1, ref1, 'b-', alpha=0.5, linewidth=0.5, label='Measured')
    axes[0,0].plot(wn_theory, R_theory * 100, 'r-', linewidth=1, label='Theory')
    axes[0,0].set_xlabel('Wavenumber (cm$^{-1}$)')
    axes[0,0].set_ylabel('Reflectance (%)')
    axes[0,0].set_title(f"SiC, 10 deg, d={result_10['thickness_um']:.1f} um")
    axes[0,0].legend()
    axes[0,0].grid(True, alpha=0.3)
    
    # 图2：SiC 15°反射率
    model_15 = TwoBeamModel(15, 'SiC')
    wn_theory, R_theory = model_15.interference_peaks(result_15['thickness_cm'])
    axes[0,1].plot(wn2, ref2, 'b-', alpha=0.5, linewidth=0.5, label='Measured')
    axes[0,1].plot(wn_theory, R_theory * 100, 'r-', linewidth=1, label='Theory')
    axes[0,1].set_xlabel('Wavenumber (cm$^{-1}$)')
    axes[0,1].set_ylabel('Reflectance (%)')
    axes[0,1].set_title(f"SiC, 15 deg, d={result_15['thickness_um']:.1f} um")
    axes[0,1].legend()
    axes[0,1].grid(True, alpha=0.3)
    
    # 图3：Si 10°反射率
    axes[1,0].plot(wn3, ref3, 'b-', alpha=0.5, linewidth=0.5, label='Measured')
    axes[1,0].set_xlabel('Wavenumber (cm$^{-1}$)')
    axes[1,0].set_ylabel('Reflectance (%)')
    axes[1,0].set_title(f"Si, 10 deg, d={result_si_10['thickness_um']:.1f} um")
    axes[1,0].legend()
    axes[1,0].grid(True, alpha=0.3)
    
    # 图4：Si 15°反射率
    axes[1,1].plot(wn4, ref4, 'b-', alpha=0.5, linewidth=0.5, label='Measured')
    axes[1,1].set_xlabel('Wavenumber (cm$^{-1}$)')
    axes[1,1].set_ylabel('Reflectance (%)')
    axes[1,1].set_title(f"Si, 15 deg, d={result_si_15['thickness_um']:.1f} um")
    axes[1,1].legend()
    axes[1,1].grid(True, alpha=0.3)
    
    plt.tight_layout()
    plt.savefig(results_dir / 'B题_干涉光谱分析.png', dpi=150, bbox_inches='tight')
    print(f"  图表已保存: {results_dir / 'B题_干涉光谱分析.png'}")
    
    # ═══════════════════════════════════════════════════════
    # 输出汇总
    # ═══════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("计算结果汇总")
    print("=" * 60)
    print(f"SiC外延层厚度：")
    print(f"  10°: {result_10['thickness_um']:.2f} μm")
    print(f"  15°: {result_15['thickness_um']:.2f} μm")
    print(f"  推荐值: {np.mean([result_10['thickness_um'], result_15['thickness_um']]):.2f} μm")
    print(f"  可靠性: {reliability['reliability']}")
    print(f"\nSi外延层厚度：")
    print(f"  10°: {result_si_10['thickness_um']:.2f} μm")
    print(f"  15°: {result_si_15['thickness_um']:.2f} μm")
    print(f"  推荐值: {np.mean([result_si_10['thickness_um'], result_si_15['thickness_um']]):.2f} μm")
    print(f"  可靠性: {reliability_si['reliability']}")
    
    return {
        "sic_10": result_10,
        "sic_15": result_15,
        "si_10": result_si_10,
        "si_15": result_si_15,
        "reliability_sic": reliability,
        "reliability_si": reliability_si,
        "multi_beam_check": {"si_10": check_10, "si_15": check_15},
    }


if __name__ == "__main__":
    results = main()
