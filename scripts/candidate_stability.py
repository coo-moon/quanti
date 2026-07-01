#!/usr/bin/env python3
"""候选系统:纪律化 beta 收割 + 趋势去风险叠加。

前提(已被本仓库 5y 全市场重跑证实):池内选股 alpha≈0,收益=池 beta。
所以唯一可辩护的改进不是追 alpha,而是**让 beta 收割更稳**——用趋势滤网在池自身
走坏时降杠杆到现金,削掉 A 股小盘的肥左尾。成败标准是**风险调整后(夏普/Calmar/回撤)
样本外改善**,而非绝对收益;且必须过 DSR/PBO 过拟合闸,否则视为运气。

趋势叠加(无前视):第 t 月的仓位只由截至 t-1 月的净值路径决定——
  exposure[t] = 1 if nav[t-1] >= SMA_W(nav)[t-1] else 0(否则空仓吃现金 rf)。
现金收益故意设 0(不粉饰叠加;真实货基 ~1.5%/y 只会让叠加更好看)。

网格 W∈{3..12} 月。先验固定 W=10(≈200 交易日经典趋势)报 OOS,再用整网格算 PBO——
若"挑最优窗口"就是过拟合,PBO 会接近 0.5。

用法:.venv/bin/python scripts/candidate_stability.py [--start --end --pools 1000,100]
"""
import sys
import argparse
from collections import defaultdict
from datetime import date, timedelta

import numpy as np

sys.path.insert(0, ".")
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.backtest.overfit import (
    deflated_sharpe_ratio, pbo_cscv, sharpe_per_obs, probabilistic_sharpe_ratio,
)

ACCOUNT_DB, MARKET_DB = "data/agent_bt.db", "data/market.db"
COST_PER_TURN = 0.003
CASH_RF_MONTHLY = 0.0        # 故意保守:空仓不给利息
W_GRID = [3, 4, 5, 6, 7, 8, 9, 10, 12]
W_APRIORI = 10               # 先验固定窗口(不许挑)


def monthly_dates(provider, start, end):
    by_m = defaultdict(list)
    for d in provider.get_trade_dates(start, end):
        by_m[(d.year, d.month)].append(d)
    return sorted(max(ds) for ds in by_m.values())


def adv_pool(provider, rd, n):
    adv = provider.get_adv20_map(rd - timedelta(days=40), rd)
    return sorted([c for c, v in adv.items() if v and v > 0],
                  key=lambda c: adv[c], reverse=True)[:n]


def pool_return_series(provider, rebal, n):
    """ADV前n等权、月度调仓的净收益序列(减池换手成本)。"""
    rets, prev_pool, prev_rd = [], None, None
    for rd in rebal:
        pool = adv_pool(provider, rd, n)
        if prev_pool is not None:
            rs = []
            for c in prev_pool:
                b = provider.get_daily_bars(c, prev_rd, rd)
                if len(b) >= 2 and b[0].close > 0:
                    rs.append(b[-1].close / b[0].close - 1.0)
            r = float(np.mean(rs)) if rs else 0.0
            turn = 1.0 - len(set(prev_pool) & set(pool)) / max(len(prev_pool), 1)
            rets.append(r * (1 - turn * COST_PER_TURN))
        prev_pool, prev_rd = pool, rd
    return np.array(rets)


def trend_overlay(base_rets, w, rf=CASH_RF_MONTHLY):
    """exposure[t] 由 nav[..t-1] 与其 W 月均线决定(无前视)。前 W 月默认持有。"""
    r = np.asarray(base_rets, dtype=float)
    nav = np.cumprod(1 + r)
    out = np.empty_like(r)
    for t in range(len(r)):
        if t < w:
            invested = True
        else:
            sma = nav[t-w:t].mean()          # 截至 t-1 的 W 月均线
            invested = nav[t-1] >= sma
        out[t] = r[t] if invested else rf
    return out


def stats(r):
    r = np.asarray(r, dtype=float)
    if len(r) == 0:
        return dict(total=0, ann=0, sharpe=0, mdd=0, calmar=0, n=0)
    nav = np.cumprod(1 + r)
    total = float(nav[-1] - 1)
    n = len(r)
    ann = (1 + total) ** (12.0 / n) - 1 if total > -1 else -1.0
    sd = r.std(ddof=0)
    sh = r.mean() / sd * np.sqrt(12) if sd > 0 else 0.0
    pk = np.maximum.accumulate(nav)
    mdd = float(((nav - pk) / pk).min())
    calmar = ann / abs(mdd) if mdd < 0 else 0.0
    return dict(total=total, ann=ann, sharpe=sh, mdd=mdd, calmar=calmar, n=n)


def fmt(tag, s):
    return (f"{tag:<22} 总{s['total']*100:+7.1f}% 年化{s['ann']*100:+6.2f}% "
            f"夏普{s['sharpe']:5.2f} 回撤{s['mdd']*100:6.1f}% Calmar{s['calmar']:5.2f} ({s['n']}月)")


def analyze_pool(name, base, oos_from):
    print(f"\n{'='*78}\n池 = ADV前{name} 等权(base beta)\n{'='*78}", flush=True)
    is_r, oos_r = base[:oos_from], base[oos_from:]
    print("[全期]", flush=True)
    print(" ", fmt("base 买入持有", stats(base)), flush=True)
    # 网格叠加
    grid = {w: trend_overlay(base, w) for w in W_GRID}
    for w in W_GRID:
        print(" ", fmt(f"+趋势叠加 W={w}", stats(grid[w])), flush=True)

    print(f"\n[样本内 IS 前{oos_from}月 / 样本外 OOS 后{len(base)-oos_from}月]", flush=True)
    print("  base    IS:", fmt("", stats(is_r)), flush=True)
    print("  base    OOS:", fmt("", stats(oos_r)), flush=True)
    ov_ap = grid[W_APRIORI]
    print(f"  W={W_APRIORI}先验 IS:", fmt("", stats(ov_ap[:oos_from])), flush=True)
    print(f"  W={W_APRIORI}先验 OOS:", fmt("", stats(ov_ap[oos_from:])), flush=True)

    # DSR:整网格叠加的每期Sharpe 作 trials;检验先验 W 配置
    trial_sharpes = [sharpe_per_obs(grid[w]) for w in W_GRID]
    dsr = deflated_sharpe_ratio(ov_ap, trial_sharpes)
    psr0 = probabilistic_sharpe_ratio(ov_ap, 0.0)
    # PBO:矩阵 (T月 × W配置)
    M = np.column_stack([grid[w] for w in W_GRID])
    pbo = pbo_cscv(M, n_splits=min(12, (len(base)//2)*2))
    print(f"\n[过拟合闸] 先验 W={W_APRIORI}:", flush=True)
    print(f"  PSR(基准0)={psr0:.3f}  DSR(扣{dsr['n_trials']}次多重检验)={dsr['dsr']:.3f} "
          f"(sr_obs={dsr['sr_observed']:.3f} vs sr0={dsr['sr0_benchmark']:.3f})", flush=True)
    print(f"  PBO(整网格 {pbo['n_configs']}配置/{pbo['n_combos']}组合)={pbo['pbo']:.3f} "
          f"(中位logit={pbo['median_logit']:+.2f})", flush=True)
    return dict(base=stats(base), overlay_apriori=stats(ov_ap), dsr=dsr, pbo=pbo)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-08-01")
    ap.add_argument("--end", default="2026-06-24")
    ap.add_argument("--pools", default="1000,100")
    args = ap.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    db = Database(ACCOUNT_DB, market_db_path=MARKET_DB)
    db.initialize()
    provider = DataProvider(db)
    rebal = monthly_dates(provider, start, end)
    oos_from = int(len(rebal) * 0.6)  # 60% IS / 40% OOS
    print(f"窗口 {rebal[0]}~{rebal[-1]} {len(rebal)}月  IS/OOS 分界≈{rebal[oos_from]}", flush=True)

    for token in args.pools.split(","):
        n = int(token)
        base = pool_return_series(provider, rebal, n)
        analyze_pool(str(n), base, oos_from - 1)  # base 比 rebal 少 1(首月无收益)
    print("\nDONE", flush=True)
    db.close()


if __name__ == "__main__":
    main()
