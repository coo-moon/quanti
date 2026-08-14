#!/usr/bin/env python3
"""唯一没测过的方向:市场中性 long-short —— 能否把池内因子 IC 变成稳定、去 beta 的收益。

前置(已证):long-only 净值=beta+alpha,beta 去不掉→不可能稳定;但池内因子有真实 IC
(低波0.098/低换手0.094/反转0.076,OOS),而 **long-only 吃不到空头腿**。中性 long-short
(多顶部五分位、空底部五分位,美元中性)按构造去掉市场 beta,理论上能收割全 IC → 若净成本
后仍稳定正、低回撤、过 DSR/PBO,那就是"能稳定赚钱的系统"。

关键=A 股做空的真实摩擦:
  - raw:只算换手交易成本(理论上限,不可交易);
  - 融券:个股空头,~8%/yr 融券费 + A 股融券稀缺(小盘借不到,尤其因子想空的垃圾股)→ 施加
    8%/yr 空头腿拖累,并标注"可执行性存疑";
  - 期指对冲:多头因子篮子 - 空 IC/IM 指数期货,只去 beta、不用融个股,但基差 ~6%/yr 拖累
    (近似:多头篮子超额 = 多头 - 全池等权;再减基差)。

用法:.venv/bin/python scripts/neutral_longshort.py
"""
import sys
import time
from collections import defaultdict
from datetime import date, timedelta

import numpy as np

sys.path.insert(0, ".")
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.factors.library import FACTOR_EXPRS, as_factor_fn
from quanti.factors.cross_sectional import compute_factor_panel, FactorConfig
from quanti.backtest.overfit import deflated_sharpe_ratio, pbo_cscv, sharpe_per_obs

ACCOUNT_DB, MARKET_DB = "data/agent_bt.db", "data/market.db"
START, END = date(2021, 8, 1), date(2026, 6, 24)
N_CAND = 1000            # 可交易池
QUANTILE = 0.2           # 顶/底 20%
TX_COST = 0.003          # 每腿换手交易成本
BORROW_ANNUAL = 0.08     # 融券费/yr(个股空头)
BASIS_ANNUAL = 0.06      # 期指对冲基差/yr(IC/IM 贴水)
IC_FACTORS = ["realized_vol_20d", "turnover_20d", "reversal_1w"]  # 有真实 OOS IC
# 变体:(名字, 权重dict或None=三者等权)
VARIANTS = [
    ("低波 L/S", {"realized_vol_20d": 1}),
    ("低换手 L/S", {"turnover_20d": 1}),
    ("反转 L/S", {"reversal_1w": 1}),
    ("IC三合一 L/S", None),
]
APRIORI = "IC三合一 L/S"


def monthly(provider):
    by_m = defaultdict(list)
    for d in provider.get_trade_dates(START, END):
        by_m[(d.year, d.month)].append(d)
    return sorted(max(ds) for ds in by_m.values())


def adv_pool(provider, rd, n):
    adv = provider.get_adv20_map(rd - timedelta(days=40), rd)
    return sorted([c for c, v in adv.items() if v and v > 0],
                  key=lambda c: adv[c], reverse=True)[:n]


def ranked_by(cols, weights):
    sc = {}
    for code, zs in cols.items():
        vals = ([zs.get(n, np.nan) for n in IC_FACTORS] if weights is None
                else [zs.get(n, np.nan) * w for n, w in weights.items()])
        vals = [v for v in vals if v == v]
        if vals:
            sc[code] = float(np.mean(vals))
    return sorted(sc, key=lambda c: sc[c], reverse=True)


def fwd(provider, codes, d0, d1):
    rs = []
    for c in codes:
        b = provider.get_daily_bars(c, d0, d1)
        if len(b) >= 2 and b[0].close > 0:
            rs.append(b[-1].close / b[0].close - 1.0)
    return rs


def stat(r):
    r = np.asarray(r)
    if len(r) == 0:
        return dict(ann=0, sharpe=0, mdd=0, n=0)
    nav = np.cumprod(1 + r)
    n = len(r)
    tot = float(nav[-1] - 1)
    ann = (1 + tot) ** (12.0 / n) - 1 if tot > -1 else -1.0
    sd = r.std(ddof=0)
    sh = r.mean() / sd * np.sqrt(12) if sd > 0 else 0.0
    pk = np.maximum.accumulate(nav)
    return dict(ann=ann, sharpe=sh, mdd=float(((nav - pk) / pk).min()), n=n)


def main():
    db = Database(ACCOUNT_DB, market_db_path=MARKET_DB)
    db.initialize()
    provider = DataProvider(db)
    cfg = FactorConfig(factors={n: as_factor_fn(FACTOR_EXPRS[n]) for n in IC_FACTORS})
    rebal = monthly(provider)
    oos = int(len(rebal) * 0.6) - 1
    print(f"窗口 {rebal[0]}~{rebal[-1]} {len(rebal)}月 IS前{oos}/OOS后{len(rebal)-1-oos} 池ADV{N_CAND} 分位{QUANTILE:.0%}", flush=True)

    # raw L/S 每期收益(多头-空头,未计成本)+ 换手 + 多头篮子超额(用于期指对冲口径)
    raw = {v[0]: [] for v in VARIANTS}
    long_turn = {v[0]: [] for v in VARIANTS}
    long_excess = {v[0]: [] for v in VARIANTS}   # 多头 - 全池等权(期指对冲口径)
    prev_top = {v[0]: None for v in VARIANTS}
    prev_pool = prev_rd = None
    for k, rd in enumerate(rebal):
        t = time.time()
        pool = adv_pool(provider, rd, N_CAND)
        panel = compute_factor_panel(provider, db, pool, as_of=rd, config=cfg)
        cols = {}
        if panel is not None and not panel.empty:
            for code, row in panel.iterrows():
                cols[code] = {n: row[n] for n in IC_FACTORS if n in panel.columns}
        nq = max(5, int(len(cols) * QUANTILE))
        tops, bots = {}, {}
        for name, w in VARIANTS:
            r = ranked_by(cols, w)
            tops[name], bots[name] = r[:nq], r[-nq:]
        if prev_pool is not None:
            pool_ew = np.mean(fwd(provider, prev_pool, prev_rd, rd) or [0.0])
            for name, w in VARIANTS:
                lt = fwd(provider, prev_top[name][0], prev_rd, rd)
                lb = fwd(provider, prev_top[name][1], prev_rd, rd)
                long_r = float(np.mean(lt)) if lt else 0.0
                short_r = float(np.mean(lb)) if lb else 0.0
                raw[name].append(long_r - short_r)
                long_excess[name].append(long_r - pool_ew)
                turn = 1.0 - len(set(prev_top[name][0]) & set(tops[name])) / max(len(prev_top[name][0]), 1)
                long_turn[name].append(turn)
        prev_top = {name: (tops[name], bots[name]) for name, w in VARIANTS}
        prev_pool, prev_rd = pool, rd
        if k % 12 == 0:
            print(f"  ..{rd} ({time.time()-t:.1f}s/月)", flush=True)

    def apply_costs(name, mode):
        r = np.array(raw[name])
        turn = np.array(long_turn[name])
        if mode == "raw":                       # 两腿换手成本
            return r - 2 * turn * TX_COST
        if mode == "borrow":                    # 两腿换手 + 空头腿融券费
            return r - 2 * turn * TX_COST - BORROW_ANNUAL / 12
        if mode == "futures":                   # 多头篮子超额 - 换手 - 基差(期指对冲)
            return np.array(long_excess[name]) - turn * TX_COST - BASIS_ANNUAL / 12
        return r

    print(f"\n{'变体':<14}{'口径':<8}| {'年化':>8}{'夏普':>6}{'回撤':>8} | {'IS夏普':>7} {'OOS夏普':>7} {'OOS年化':>8}", flush=True)
    series_for_gate = {}
    for name, w in VARIANTS:
        for mode, label in [("raw", "raw理论"), ("borrow", "融券8%"), ("futures", "期指对冲")]:
            s = apply_costs(name, mode)
            f, i, o = stat(s), stat(s[:oos]), stat(s[oos:])
            if mode == "borrow":                # 用可执行(融券)口径过闸
                series_for_gate[name] = s
            print(f"{name:<14}{label:<8}| {f['ann']*100:>+7.2f}%{f['sharpe']:>6.2f}{f['mdd']*100:>+7.1f}% | "
                  f"{i['sharpe']:>7.2f} {o['sharpe']:>7.2f} {o['ann']*100:>+7.2f}%", flush=True)
        print(flush=True)

    # 过拟合闸(可执行融券口径)
    names = list(series_for_gate)
    trials = [sharpe_per_obs(series_for_gate[n]) for n in names]
    best = names[int(np.argmax(trials))]
    dsr_ap = deflated_sharpe_ratio(series_for_gate[APRIORI], trials)
    dsr_best = deflated_sharpe_ratio(series_for_gate[best], trials)
    M = np.column_stack([series_for_gate[n] for n in names])
    pbo = pbo_cscv(M, n_splits=min(12, (len(rebal)//2)*2))
    print("=== 过拟合闸(可执行融券8%口径)===", flush=True)
    print(f"  先验={APRIORI}: DSR={dsr_ap['dsr']:.3f} (sr_obs={dsr_ap['sr_observed']:.3f} vs sr0={dsr_ap['sr0_benchmark']:.3f})", flush=True)
    print(f"  挑最优={best}: DSR={dsr_best['dsr']:.3f}", flush=True)
    print(f"  PBO={pbo['pbo']:.3f} ({pbo['n_configs']}变体/{pbo['n_combos']}组合)", flush=True)
    print("\nDONE", flush=True)
    db.close()


if __name__ == "__main__":
    main()
