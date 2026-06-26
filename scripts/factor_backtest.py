#!/usr/bin/env python3
"""纯因子选股回测 —— 横截面因子 composite 排序 → top-K(去掉 agent 里无 alpha 的策略信号层)。

用法:.venv/bin/python scripts/factor_backtest.py [--start --end --recent N]

池子=ADV 前 1000(含中小盘),验证"放开小盘 beta 后因子选股能否赢被动等权"。
compute_factor_panel 向量化(200 股 sub-second),1000 股×60 月约 12 分钟。
对比:同池等权。真基准看全市场等权(5y +11.78%,见 memory)。
临时库 data/agent_bt.db;行情 data/market.db。PIT:compute_factor_panel(as_of=rd)。
结论见 memory: stoploss-regime-research-findings(2026-06-26)。
"""
import sys
import time
import argparse
from datetime import date, timedelta
from collections import defaultdict
import numpy as np

sys.path.insert(0, ".")
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.factors.cross_sectional import compute_factor_panel

ACCOUNT_DB = "data/agent_bt.db"   # 临时 account 库(可删)
MARKET_DB = "data/market.db"
N_CAND = 1000         # 宽池:含中小盘
TOP_K = 20
COST_PER_TURN = 0.003


def monthly_rebal_dates(provider, start, end):
    tds = provider.get_trade_dates(start, end)
    by_m = defaultdict(list)
    for d in tds:
        by_m[(d.year, d.month)].append(d)
    return sorted(max(ds) for ds in by_m.values())


def liquidity_pool(provider, rd, n):
    adv = provider.get_adv20_map(rd - timedelta(days=40), rd)
    ranked = sorted([c for c, v in adv.items() if v and v > 0],
                    key=lambda c: adv[c], reverse=True)
    return ranked[:n]


def select_holdings(provider, db, candidates, rd):
    if not candidates:
        return []
    panel = compute_factor_panel(provider, db, candidates, as_of=rd)
    if panel is None or panel.empty or "composite" not in panel.columns:
        return []
    comp = panel["composite"].dropna().sort_values(ascending=False)
    return list(comp.head(TOP_K).index)


def period_returns(provider, codes, d0, d1):
    rets = []
    for code in codes:
        bars = provider.get_daily_bars(code, d0, d1)
        if len(bars) >= 2 and bars[0].close > 0:
            rets.append(bars[-1].close / bars[0].close - 1.0)
    return float(np.mean(rets)) if rets else 0.0


def stats(monthly_returns):
    if not monthly_returns:
        return dict(total=0, ann=0, sharpe=0, mdd=0, n=0)
    r = np.array(monthly_returns)
    nav = np.cumprod(1 + r)
    total = nav[-1] - 1
    n = len(r)
    ann = (1 + total) ** (12.0 / n) - 1 if total > -1 else -1
    sh = r.mean() / r.std(ddof=0) * np.sqrt(12) if r.std(ddof=0) > 0 else 0
    pk = np.maximum.accumulate(nav)
    mdd = float(((nav - pk) / pk).min())
    return dict(total=total, ann=ann, sharpe=sh, mdd=mdd, n=n)


def main():
    ap = argparse.ArgumentParser(description="纯因子选股回测")
    ap.add_argument("--start", default="2021-08-01")
    ap.add_argument("--end", default="2026-06-24")
    ap.add_argument("--recent", type=int, default=0)
    args = ap.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    db = Database(ACCOUNT_DB, market_db_path=MARKET_DB)
    db.initialize()
    provider = DataProvider(db)
    rebal = monthly_rebal_dates(provider, start, end)
    if args.recent:
        rebal = rebal[-args.recent:]
    print(f"调仓月数={len(rebal)} 起{rebal[0]} 止{rebal[-1]} 池=ADV前{N_CAND} top{TOP_K}", flush=True)

    nav = bench_nav = 1.0
    fac_rets, bench_rets = [], []
    prev_hold, prev_pool, prev_rd = None, None, None
    for rd in rebal:
        t = time.time()
        pool = liquidity_pool(provider, rd, N_CAND)
        hold = select_holdings(provider, db, pool, rd)
        if prev_hold is not None:
            fr = period_returns(provider, prev_hold, prev_rd, rd)
            br = period_returns(provider, prev_pool, prev_rd, rd)
            f_turn = 1.0 - len(set(prev_hold) & set(hold)) / max(len(prev_hold), 1)
            b_turn = 1.0 - len(set(prev_pool) & set(pool)) / max(len(prev_pool), 1)
            fr *= (1 - f_turn * COST_PER_TURN)
            br *= (1 - b_turn * COST_PER_TURN)
            nav *= (1 + fr)
            bench_nav *= (1 + br)
            fac_rets.append(fr)
            bench_rets.append(br)
            print(f"{rd} hold={len(hold)} factor={fr*100:+.2f}% bench={br*100:+.2f}% "
                  f"NAV={nav:.3f} bench={bench_nav:.3f} ({time.time()-t:.1f}s)", flush=True)
        else:
            print(f"{rd} 首月 选{len(hold)}只 ({time.time()-t:.1f}s)", flush=True)
        prev_hold, prev_pool, prev_rd = hold, pool, rd

    f, b = stats(fac_rets), stats(bench_rets)
    print("\n=== 结果(池=ADV前1000 含中小盘) ===")
    print(f"纯因子选股top{TOP_K}: 总{f['total']*100:+.1f}% 年化{f['ann']*100:+.2f}% "
          f"夏普{f['sharpe']:.2f} 回撤{f['mdd']*100:.1f}% ({f['n']}月)")
    print(f"同池等权(前1000)  : 总{b['total']*100:+.1f}% 年化{b['ann']*100:+.2f}% "
          f"夏普{b['sharpe']:.2f} 回撤{b['mdd']*100:.1f}%")
    print(f"因子超额(年化): {(f['ann']-b['ann'])*100:+.2f}%")
    db.close()


if __name__ == "__main__":
    main()
