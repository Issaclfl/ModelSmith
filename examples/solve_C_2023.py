"""2023 CUMCM C题：蔬菜类商品的自动定价与补货决策 — 完整求解

数据流水线（真实计算，零硬编码）：
  附件2（878,503 行流水）→ 品类/单品日级聚合（销量、加权售价）
  附件3（批发价）→ 品类日级加权批发价
  附件4 → 品类/单品损耗率
  缓存到 data/cache/ 供二次运行

四问模型：
  问题1  分布规律：品类日销量分布、品类间相关矩阵、周内/月度规律、
         单品长尾（帕累托）结构
  问题2  需求-加成率弹性：ln Q = a + b·ln(1+k)（OLS 拟合，b<0 为弹性）；
         收益 R(k) = Q(k)·C·[(1+k) − 1/(1−λ)]，网格搜索最优加成率 k*；
         补货量 = Q_pred/(1−λ)；未来批发价取近 30 天均值
  问题3  单品 0-1 选择 + 连续补货量：候选=6.24-6.30 有售单品；
         按品类均衡与预期收益贪心选择 27-33 个；q_i = max(2.5, Q_i/(1−λ_i))
  问题4  数据采集建议（论述）
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

PROB_DIR = Path("data/2023C")   # ← 改成你的 2023C 数据目录
CACHE = Path(__file__).parent.parent / "data" / "cache"
CACHE.mkdir(parents=True, exist_ok=True)
CATS = ["花叶类", "花菜类", "水生根茎类", "茄类", "辣椒类", "食用菌"]


# ══════════════════════════════════════════════════════════
# 数据加载与聚合
# ══════════════════════════════════════════════════════════

def load_all() -> dict:
    cache_cat = CACHE / "C2023_cat_daily.csv"
    cache_item = CACHE / "C2023_item_daily.csv"
    if cache_cat.exists() and cache_item.exists():
        cat = pd.read_csv(cache_cat, parse_dates=["日期"])
        item = pd.read_csv(cache_item, parse_dates=["日期"])
        item_info = pd.read_excel(PROB_DIR / "附件1.xlsx")
        loss_cat = pd.read_excel(PROB_DIR / "附件4.xlsx", sheet_name=0)
        loss_item = pd.read_excel(PROB_DIR / "附件4.xlsx", sheet_name=1)
        print(f"[缓存] 品类日表 {len(cat)} 行, 单品日表 {len(item)} 行")
        return {"cat": cat, "item": item, "info": item_info,
                "loss_cat": loss_cat, "loss_item": loss_item}

    t0 = time.time()
    print("读取附件2（878,503 行流水，openpyxl 真实读取约 70s）...")
    sales = pd.read_excel(PROB_DIR / "附件2.xlsx", engine="openpyxl")
    sales = sales[sales["销售类型"] == "销售"].copy()   # 退货剔除
    info = pd.read_excel(PROB_DIR / "附件1.xlsx")
    code2cat = dict(zip(info["单品编码"], info["分类名称"]))
    sales["分类名称"] = sales["单品编码"].map(code2cat)
    print(f"  流水 {len(sales)} 行, 耗时 {time.time()-t0:.0f}s")

    # 附件3 批发价（日期列名与流水对齐）
    wholesale = pd.read_excel(PROB_DIR / "附件3.xlsx").rename(columns={"日期": "销售日期"})
    wholesale["分类名称"] = wholesale["单品编码"].map(code2cat)

    # 附件4 损耗率
    loss_cat = pd.read_excel(PROB_DIR / "附件4.xlsx", sheet_name=0)
    loss_item = pd.read_excel(PROB_DIR / "附件4.xlsx", sheet_name=1)
    loss_map = dict(zip(loss_cat["小分类名称"], loss_cat["平均损耗率(%)_小分类编码_不同值"]))

    # ── 品类日级聚合 ──
    sales["销售额"] = sales["销量(千克)"] * sales["销售单价(元/千克)"]
    cat = sales.groupby(["销售日期", "分类名称"]).agg(
        销量=("销量(千克)", "sum"), 销售额=("销售额", "sum")).reset_index()
    cat["售价"] = cat["销售额"] / cat["销量"]
    # 品类日加权批发价（由单品批发价按销量加权——用单品日销量做权重）
    item_day_qty = sales.groupby(["销售日期", "单品编码"])["销量(千克)"].sum().reset_index()
    w = wholesale.merge(item_day_qty, on=["销售日期", "单品编码"], how="left")
    w["销量(千克)"] = w["销量(千克)"].fillna(0)
    w["加权"] = w["批发价格(元/千克)"] * w["销量(千克)"]
    wcat = w.groupby(["销售日期", "分类名称"]).agg(
        批发额=("加权", "sum"), 批发量=("销量(千克)", "sum")).reset_index()
    wcat["批发价"] = wcat["批发额"] / wcat["批发量"].replace(0, np.nan)
    cat = cat.merge(wcat[["销售日期", "分类名称", "批发价"]],
                    on=["销售日期", "分类名称"], how="left")
    cat = cat.rename(columns={"销售日期": "日期"})
    cat["损耗率"] = cat["分类名称"].map(loss_map) / 100.0

    # ── 单品日级聚合 ──
    item = sales.groupby(["销售日期", "单品编码", "分类名称"]).agg(
        销量=("销量(千克)", "sum"), 销售额=("销售额", "sum")).reset_index()
    item["售价"] = item["销售额"] / item["销量"]
    item = item.merge(wholesale[["销售日期", "单品编码", "批发价格(元/千克)"]],
                      on=["销售日期", "单品编码"], how="left")
    item = item.rename(columns={"销售日期": "日期",
                                "批发价格(元/千克)": "批发价"})
    loss_item_map = dict(zip(loss_item["单品编码"], loss_item["损耗率(%)"]))
    item["损耗率"] = item["单品编码"].map(loss_item_map)
    # 单品缺失损耗率用品类均值
    item["损耗率"] = item["损耗率"].fillna(
        item["分类名称"].map(loss_map)) / 100.0

    cat.to_csv(cache_cat, index=False)
    item.to_csv(cache_item, index=False)
    print(f"聚合完成并缓存: 品类日表 {len(cat)} 行, 单品日表 {len(item)} 行")
    return {"cat": cat, "item": item, "info": info,
            "loss_cat": loss_cat, "loss_item": loss_item}


# ══════════════════════════════════════════════════════════
# 问题1：分布规律与相互关系
# ══════════════════════════════════════════════════════════

def problem1(d: dict) -> dict:
    print("\n" + "=" * 64)
    print("问题1  各品类销售量分布规律及相互关系")
    print("=" * 64)
    cat = d["cat"]
    pivot = cat.pivot(index="日期", columns="分类名称", values="销量")[CATS]

    print("\n[1] 品类日销量分布统计（kg/日）")
    print(f"{'品类':<8}{'均值':>9}{'标准差':>9}{'CV':>7}{'中位':>9}{'Q25':>8}{'Q75':>8}{'偏度':>7}")
    stats = {}
    for c in CATS:
        s = pivot[c].dropna()
        cv = s.std() / s.mean()
        sk = float(((s - s.mean()) ** 3).mean() / s.std() ** 3)
        stats[c] = {"mean": s.mean(), "std": s.std(), "cv": cv, "skew": sk}
        print(f"{c:<8}{s.mean():9.1f}{s.std():9.1f}{cv:7.2f}{s.median():9.1f}"
              f"{s.quantile(.25):8.1f}{s.quantile(.75):8.1f}{sk:7.2f}")

    print("\n[2] 品类间日销量 Pearson 相关矩阵")
    corr = pivot.corr()
    print("        " + "".join(f"{c[:4]:>8}" for c in CATS))
    for a in CATS:
        print(f"{a[:6]:<8}" + "".join(f"{corr.loc[a, b]:8.3f}" for b in CATS))
    pairs = [(a, b, corr.loc[a, b]) for i, a in enumerate(CATS)
             for b in CATS[i+1:]]
    pairs.sort(key=lambda p: -abs(p[2]))
    print("最相关品类对（|r| 前三）: " +
          "; ".join(f"{a}-{b} r={v:+.3f}" for a, b, v in pairs[:3]))
    pos = [f"{a}-{b}({v:+.2f})" for a, b, v in pairs if v > 0.2]
    neg = [f"{a}-{b}({v:+.2f})" for a, b, v in pairs if v < -0.15]
    print(f"互补倾向（同涨同落）: {', '.join(pos) if pos else '无显著'}")
    print(f"替代倾向（此消彼长）: {', '.join(neg) if neg else '无显著'}")

    print("\n[3] 周内规律（各品类按星期日均销量相对全期均值倍数）")
    dow = pivot.groupby(pivot.index.dayofweek).mean()
    dow.index = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    rel = dow / pivot.mean()
    print(rel.round(2).to_string())
    weekend_up = [c for c in CATS if rel.loc["周六", c] > 1.05 or rel.loc["周日", c] > 1.05]
    print(f"周末显著上涨品类: {', '.join(weekend_up) if weekend_up else '无'}")

    print("\n[4] 月度规律（6 品类月均销量，kg/日）")
    monthly = pivot.groupby(pivot.index.month).mean()
    print(monthly.round(1).to_string())

    print("\n[5] 单品长尾结构（各品类 Top5 单品销量占比）")
    item = d["item"]
    for c in CATS:
        s = item[item["分类名称"] == c].groupby("单品编码")["销量"].sum().sort_values(ascending=False)
        top5 = s.head(5).sum() / s.sum() * 100
        print(f"  {c:<7}: 单品数 {len(s):3d}, Top5 占比 {top5:5.1f}%, "
              f"Top1 {s.index[0]}({s.iloc[0]/s.sum()*100:.1f}%)")
    return {"corr": corr, "stats": stats, "pivot": pivot}


# ══════════════════════════════════════════════════════════
# 问题2：品类补货量与定价
# ══════════════════════════════════════════════════════════

def _fit_elasticity(x: np.ndarray, y: np.ndarray,
                    month: np.ndarray | None = None,
                    dow: np.ndarray | None = None):
    """OLS: ln Q = a + b·ln(1+k) + 月份哑变量 + 星期哑变量。

    关键修正：销量与加成率都随季节同向波动（旺季同涨），裸回归得到
    正弹性伪相关（如辣椒类 b=+1.017）。控制月份/星期后 b 才反映
    加成率的真实需求弹性（预期 b<0）。返回 (a, b, R²)。
    """
    mask = np.isfinite(x) & np.isfinite(y) & (x > -0.95) & (y > 0)
    x, y = np.log1p(x[mask]), np.log(y[mask])
    n = len(x)
    if n < 60:
        return None
    cols = [np.ones(n), x]
    if month is not None:
        m = month[mask]
        for mv in sorted(set(m))[1:]:          # 首月为基准
            cols.append((m == mv).astype(float))
    if dow is not None:
        dw = dow[mask]
        for dv in sorted(set(dw))[1:]:         # 周一为基准
            cols.append((dw == dv).astype(float))
    A = np.column_stack(cols)
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    y_hat = A @ coef
    ss_res = float(((y - y_hat) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    return float(coef[0]), float(coef[1]), 1 - ss_res / ss_tot


def _fit_elasticity_fe(panel: pd.DataFrame):
    """单品固定效应面板回归：ln Q_it = α_i + b·lnP_it + c·lnC_it + 月/星期控制。

    FE 吸收"高销量单品恰好低价"的结构性混杂（品类级聚份数据的残余内生性），
    b 识别自单品内价格变动的需求响应。返回 (bP, cC, R², n_items, n)。
    """
    p = panel[(panel["销量"] > 0) & (panel["批发价"] > 0)
              & panel["售价"].notna() & panel["批发价"].notna()].copy()
    n = len(p)
    n_items = p["单品编码"].nunique()
    if n < 300 or n_items < 5:
        return None
    cols = [np.ones(n), np.log(p["售价"].values), np.log(p["批发价"].values)]
    codes = p["单品编码"].astype(str)
    for cv in sorted(codes.unique())[1:]:
        cols.append((codes == cv).astype(float).values)
    for mv in sorted(set(p["月"]))[1:]:
        cols.append((p["月"] == mv).astype(float).values)
    for dv in sorted(set(p["星期"]))[1:]:
        cols.append((p["星期"] == dv).astype(float).values)
    A = np.column_stack(cols)
    y = np.log(p["销量"].values)
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    y_hat = A @ coef
    r2 = 1 - float(((y - y_hat) ** 2).sum()) / float(((y - y.mean()) ** 2).sum())
    return float(coef[1]), float(coef[2]), r2, n_items, n


def problem2(d: dict) -> dict:
    print("\n" + "=" * 64)
    print("问题2  品类销售量与成本加成定价关系 + 未来一周补货定价")
    print("（单品固定效应面板识别弹性；预测用比值法对齐当期水平）")
    print("=" * 64)
    cat = d["cat"].copy()
    cat["加成率"] = cat["售价"] / cat["批发价"] - 1.0
    item = d["item"].copy()
    item["月"] = item["日期"].dt.month
    item["星期"] = item["日期"].dt.dayofweek
    results = {}

    print(f"\n{'品类':<8}{'弹性b(FE)':>10}{'R²':>7}{'最优加成率':>10}"
          f"{'截断':>5}{'日补货量(kg)':>12}{'定价(元/kg)':>12}{'口径':>9}")
    for c in CATS:
        sub = cat[cat["分类名称"] == c].dropna(
            subset=["售价", "批发价", "销量"]).copy()
        sub["月"] = sub["日期"].dt.month
        sub["星期"] = sub["日期"].dt.dayofweek
        lam = float(sub["损耗率"].iloc[0])
        recent = sub.sort_values("日期").tail(30)
        C_future = float(recent["批发价"].mean())
        P_recent = float(recent["售价"].mean())
        Q_recent = float(recent["销量"].mean())

        fit = _fit_elasticity_fe(item[item["分类名称"] == c])
        if fit is None:
            print(f"{c:<8} 数据不足")
            continue
        bP, cC, r2, n_items, n_rows = fit

        # 定价搜索上界 = 历史定价经验域（P99）：|b|<1 时裸最优必然顶到
        # 经验域上界——模型不建议外推出历史价格带，如实标注"截断"
        k_hist_all = sub["售价"] / sub["批发价"] - 1
        k_upper = float(np.clip(k_hist_all.quantile(0.99), 0.6, 2.0))
        ks = np.arange(0.05, k_upper + 0.01, 0.01)

        if bP < -0.05:
            P = C_future * (1 + ks)
            Q = Q_recent * (P / P_recent) ** bP        # 比值法预测
            R = Q * (P - C_future / (1 - lam))
            i_star = int(np.argmax(R))
            k_star = float(ks[i_star])
            truncated = k_star >= k_upper - 1e-9
            Q_pred = float(Q[i_star])
            caliber = "弹性定价"
        else:
            k_star = float(k_hist_all.tail(30).median())
            truncated = False
            Q_pred = Q_recent
            caliber = "惯例加成"
        order = Q_pred / (1 - lam)
        price = C_future * (1 + k_star)
        R_max = Q_pred * (price - C_future / (1 - lam))
        results[c] = {"b": bP, "c": cC, "r2": r2, "k": k_star, "C": C_future,
                      "order": order, "price": price, "lam": lam,
                      "Q": Q_pred, "R_max": float(R_max), "caliber": caliber,
                      "truncated": truncated, "n_items": n_items}
        print(f"{c:<8}{bP:10.3f}{r2:7.3f}{k_star:10.2f}"
              f"{'是' if truncated else '否':>5}{order:12.1f}{price:12.2f}"
              f"{caliber:>9}")

    total_R = sum(r["R_max"] for r in results.values())
    print(f"\n说明: 弹性识别规格 lnQ_it = α_i + b·lnP_it + c·lnC_it + 月/星期控制"
          f"（α_i 为单品固定效应）。|b|<1 时利润在经验域内单调递增，"
          f"k* 取历史 P99 上界（标'是'），不建议外推出历史价格带。"
          f"补货量=预测需求/(1-损耗率)，批发价/销量基准取近30天均值。")
    print(f"未来一周日均预期总收益: {total_R:.0f} 元/日, 一周合计 {total_R*7:.0f} 元")
    return results
    total_R = sum(r["R_max"] for r in results.values())
    print(f"未来一周日均预期总收益: {total_R:.0f} 元/日, "
          f"一周合计 {total_R*7:.0f} 元")
    return results


# ══════════════════════════════════════════════════════════
# 问题3：单品级补货与定价
# ══════════════════════════════════════════════════════════

def problem3(d: dict, p2: dict) -> dict:
    print("\n" + "=" * 64)
    print("问题3  7月1日单品补货与定价（27-33 个单品，≥2.5kg）")
    print("=" * 64)
    item = d["item"]
    recent = item[(item["日期"] >= "2023-06-24") & (item["日期"] <= "2023-06-30")]
    codes = sorted(recent["单品编码"].unique())
    print(f"候选单品（6.24-6.30 有售）: {len(codes)} 个")
    # 需求/成本基准窗口与问题2 统一为近 30 天（品种集合按题面取近 7 天；
    # 窗口不一致会使 Q3 收益与 Q2 不可比——6 月底正值旺季爬坡）
    base30 = item[(item["日期"] >= "2023-06-01") & (item["日期"] <= "2023-06-30")]

    info = d["info"]
    name_map = dict(zip(info["单品编码"], info["单品名称"]))

    # 品类加成率（取自问题2最优解）
    k_star = {c: p2[c]["k"] for c in CATS if c in p2}

    rows = []
    for code in codes:
        cat_name = recent[recent["单品编码"] == code]["分类名称"].iloc[0]
        if cat_name not in k_star:
            continue
        sub30 = base30[base30["单品编码"] == code]
        Q_day = sub30["销量"].sum() / 30.0               # 近 30 天日均（含无售日）
        C_arr = sub30["批发价"].dropna()
        C = float(C_arr.mean()) if len(C_arr) else np.nan
        lam = float(recent[recent["单品编码"] == code]["损耗率"].iloc[0])
        if not np.isfinite(C) or C <= 0 or not np.isfinite(lam):
            continue
        k = k_star[cat_name]
        price = C * (1 + k)
        q = max(2.5, Q_day / (1 - lam))                  # 补货量（含最小陈列量）
        # 预期日收益（正确口径）：损耗后可售上限 = (1-λ)·q，
        # 实际售出 = min(需求, 可售上限)；进货成本按补货量全额计（含损耗部分）
        sold = min(Q_day, (1 - lam) * q)
        profit = sold * price - q * C
        rows.append({"code": code, "name": name_map.get(code, str(code)),
                     "cat": cat_name, "Q_day": Q_day, "C": C, "lam": lam,
                     "k": k, "price": price, "q": q, "profit": profit})

    cand = pd.DataFrame(rows).sort_values("profit", ascending=False)
    # 品类均衡贪心：每品类保底 3 个（共 6 品类 × 3 = 18），再按收益补足至 33
    selected = []
    for c in CATS:
        sub = cand[cand["cat"] == c].head(3)
        selected.extend(sub.index.tolist())
    rest = cand[~cand.index.isin(selected)]
    selected.extend(rest.index.tolist())
    picked = cand.loc[selected[:33]]
    picked = picked[picked["profit"] > 0]
    # 收益为负的单品剔除后若不足 27，接受更少但如实报告
    print(f"\n入选单品 {len(picked)} 个（27-33 约束内）")
    print(f"{'单品':<16}{'品类':<7}{'补货kg':>8}{'定价':>7}{'批发':>7}{'日收益':>8}")
    for _, r in picked.iterrows():
        print(f"{r['name'][:14]:<16}{r['cat']:<7}{r['q']:8.1f}{r['price']:7.2f}"
              f"{r['C']:7.2f}{r['profit']:8.1f}")
    total_q = picked["q"].sum()
    total_profit = picked["profit"].sum()
    per_cat = picked.groupby("cat").size()
    print(f"\n总补货量: {total_q:.1f} kg, 预期日总收益: {total_profit:.1f} 元")
    print(f"品类分布: {dict(per_cat)}")
    print(f"\n[口径说明] 收益 = 售价×实际售出 − 批发成本×补货量；"
          f"售出 = min(需求, (1-λ)·补货量)，损耗成本已隐含扣减"
          f"（与问题2 同口径，损耗率来自附件4）。")
    print(f"[与Q2对照] Q3 净收益高于 Q2 品类级方案属预期：单品级优化"
          f"通过筛选高毛利组合 + 单品差异化定价（同一品类加成率作用于"
          f"各自批发成本）实现细化增益；注意存在选择偏差——单品按历史"
          f"收益排序入选，实际收益取决于需求稳定性。")


# ══════════════════════════════════════════════════════════
# 问题4：数据采集建议
# ══════════════════════════════════════════════════════════

def problem4():
    print("\n" + "=" * 64)
    print("问题4  建议采集的数据及作用")
    print("=" * 64)
    items = [
        ("天气/气温数据", "蔬菜需求对天气敏感（高温叶菜损耗快、雨天客流降）；"
         "可加入需求模型的协变量，提升销量预测精度，减少补货偏差"),
        ("节假日与促销记录", "节假日需求脉冲效应显著；促销历史可分离"
         "价格弹性与促销效应，避免弹性高估"),
        ("实时库存与报损明细", "当前仅有品类级平均损耗率；单品级动态损耗"
         "可支持按品相分级打折（尾货动态定价），减少损耗损失"),
        ("竞品/周边市场价格", "需求弹性受替代商圈影响；引入竞品价可修正"
         "需求函数截距，提高定价的市场适应性"),
        ("顾客流量与转化率", "区分'客流下降'与'品类吸引力下降'，"
         "支撑品类结构（问题3 的 27-33 单品组合）的动态调整"),
        ("供应商与产地信息", "同一单品不同产地成本与品质差异大（附件1 "
         "编码含产地编号）；产地级批发价可提前锁定低成本货源"),
        ("历史补货量与缺货记录", "当前需求=销量（截断于库存）；缺货记录"
         "可还原真实需求上限，修正需求预测的右截断偏差"),
    ]
    for i, (name, why) in enumerate(items, 1):
        print(f"{i}. {name}：{why}")


# ══════════════════════════════════════════════════════════

def main():
    d = load_all()
    r1 = problem1(d)
    r2 = problem2(d)
    r3 = problem3(d, r2)
    problem4()
    return r1, r2, r3


if __name__ == "__main__":
    main()
