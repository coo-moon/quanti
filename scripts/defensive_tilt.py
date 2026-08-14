#!/usr/bin/env python3
"""最后一块石头:防御性因子(低波/质量/价值EP/股息)的低换手倾斜能否稳健改善**风险调整后**收益。

已被严格证伪:选股(within-pool alpha≈0)、融合、择时叠加(DSR<0.5/PBO>0.5)、宽度(IS/OOS翻转=regime)。
低波异象是最不 regime 依赖、最能稳健抬夏普的因子;过去测的是"因子 composite 混合的**收益**",
没专门测"防御子集的**风险调整后** + IS/OOS + DSR/PBO"。这是"稳定(=风险调整后)"目标的最后检验。

设计:ADV1000 池月度调仓;每月算一次防御因子面板,各变体从同面板派生(不重算);
对照=同池等权(纯 beta)。成败=夏普/回撤在 IS 与 OOS 双半程都稳健优于基准,且过 DSR/PBO。
用法:.venv/bin/python scripts/defensive_tilt.py
"""
import sys
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
N_CAND = 1000
COST = 0.003
DEF_NAMES = ["realized_vol_20d", "quality_roe", "value_ep", "dividend_yield"]
# 变体:(名字, 因子权重dict 或 None=四合一等权, top_k)
VARIANTS = [
    ("低波top50", {"realized_vol_20d": 1}, 50),
    ("质量top50", {"quality_roe": 1}, 50),
    ("价值EP top50", {"value_ep": 1}, 50),
    ("防御四合一top50", None, 50),
    ("防御四合一top30", None, 30),
    ("防御四合一top100", None, 100),
]
APRIORI = "防御四合一top50"


def monthly(provider):
    by_m = defaultdict(list)
    for d in provider.get_trade_dates(START, END):
        by_m[(d.year, d.month)].append(d)
    return sorted(max(ds) for ds in by_m.values())


def adv_pool(provider, rd, n):
    adv = provider.get_adv20_map(rd - timedelta(days=40), rd)
    return sorted([c for c, v in adv.items() if v and v > 0],
                  key=lambda c: adv[c], reverse=True)[:n]


def rank_variant(panel_cols, weights):
    """从缓存的因子z列(dict code->{factor:z})派生变体 composite,返回按分降序 code。"""
    scores = {}
    for code, zs in panel_cols.items():
        if weights is None:
            vals = [zs.get(n, np.nan) for n in DEF_NAMES]
        else:
            vals = [zs.get(n, np.nan) * w for n, w in weights.items()]
        vals = [v for v in vals if v == v]  # drop nan
        if vals:
            scores[code] = float(np.mean(vals))
    return sorted(scores, key=lambda c: scores[c], reverse=True)


def fwd_ret(provider, codes, d0, d1):
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
    cfg = FactorConfig(factors={n: as_factor_fn(FACTOR_EXPRS[n]) for n in DEF_NAMES})
    rebal = monthly(provider)
    oos = int(len(rebal) * 0.6) - 1
    print(f"窗口 {rebal[0]}~{rebal[-1]} {len(rebal)}月 IS前{oos}/OOS后{len(rebal)-1-oos} 池ADV{N_CAND}", flush=True)

    var_rets = {v[0]: [] for v in VARIANTS}
    bench_rets = []
    prev = {v[0]: None for v in VARIANTS}
    prev_pool = prev_rd = None
    import time
    for k, rd in enumerate(rebal):
        t = time.time()
        pool = adv_pool(provider, rd, N_CAND)
        panel = compute_factor_panel(provider, db, pool, as_of=rd, config=cfg)
        cols = {}
        if panel is not None and not panel.empty:
            for code, row in panel.iterrows():
                cols[code] = {n: row[n] for n in DEF_NAMES if n in panel.columns}
        holds = {name: rank_variant(cols, w)[:tk] for name, w, tk in VARIANTS}
        if prev_pool is not None:
            br = fwd_ret(provider, prev_pool, prev_rd, rd)
            bench_rets.append(float(np.mean(br)) if br else 0.0)
            for name, w, tk in VARIANTS:
                pv = prev[name]
                if pv:
                    vr = fwd_ret(provider, pv, prev_rd, rd)
                    r = float(np.mean(vr)) if vr else 0.0
                    turn = 1.0 - len(set(pv) & set(holds[name])) / max(len(pv), 1)
                    var_rets[name].append(r * (1 - turn * COST))
                else:
                    var_rets[name].append(0.0)
        prev = holds
        prev_pool, prev_rd = pool, rd
        if k % 12 == 0:
            print(f"  ..{rd} ({time.time()-t:.1f}s/月)", flush=True)

    bench = np.array(bench_rets)
    print(f"\n{'变体':<18}| {'全期年化':>8}{'夏普':>6}{'回撤':>8} | {'IS夏普':>7} | {'OOS夏普':>7}{'OOS超额年化':>10}", flush=True)
    bo = stat(bench[oos:])
    print(f"{'同池等权(基准)':<18}| {stat(bench)['ann']*100:>+7.2f}%{stat(bench)['sharpe']:>6.2f}"
          f"{stat(bench)['mdd']*100:>+7.1f}% | {stat(bench[:oos])['sharpe']:>7.2f} | {bo['sharpe']:>7.2f}{'—':>10}", flush=True)
    series = {}
    for name, w, tk in VARIANTS:
        r = np.array(var_rets[name])
        series[name] = r
        f, i, o = stat(r), stat(r[:oos]), stat(r[oos:])
        exc = (o['ann'] - bo['ann']) * 100
        print(f"{name:<18}| {f['ann']*100:>+7.2f}%{f['sharpe']:>6.2f}{f['mdd']*100:>+7.1f}% | "
              f"{i['sharpe']:>7.2f} | {o['sharpe']:>7.2f}{exc:>+9.2f}%", flush=True)

    # 稳健性:IS 与 OOS 夏普是否都 > 基准
    print("\n=== 风险调整后稳健性(夏普 IS&OOS 双超基准?)===", flush=True)
    for name, w, tk in VARIANTS:
        r = series[name]
        is_ok = stat(r[:oos])['sharpe'] > stat(bench[:oos])['sharpe']
        oos_ok = stat(r[oos:])['sharpe'] > bo['sharpe']
        print(f"  {name:<18} IS超基准={'✓' if is_ok else '✗'} OOS超基准={'✓' if oos_ok else '✗'}"
              f" {'← 双超(稳健)' if is_ok and oos_ok else ''}", flush=True)

    # 过拟合闸:先验变体 vs 挑最优
    names = list(series)
    trials = [sharpe_per_obs(series[n]) for n in names]
    best = names[int(np.argmax(trials))]
    dsr_ap = deflated_sharpe_ratio(series[APRIORI], trials)
    dsr_best = deflated_sharpe_ratio(series[best], trials)
    M = np.column_stack([series[n] for n in names])
    pbo = pbo_cscv(M, n_splits=min(12, (len(rebal)//2)*2))
    print("\n=== 过拟合闸 ===", flush=True)
    print(f"  先验={APRIORI}: DSR={dsr_ap['dsr']:.3f} (sr_obs={dsr_ap['sr_observed']:.3f} vs sr0={dsr_ap['sr0_benchmark']:.3f})", flush=True)
    print(f"  挑最优={best}: DSR={dsr_best['dsr']:.3f}", flush=True)
    print(f"  PBO={pbo['pbo']:.3f} ({pbo['n_configs']}变体/{pbo['n_combos']}组合)", flush=True)
    print("\nDONE", flush=True)
    db.close()


if __name__ == "__main__":
    main()
