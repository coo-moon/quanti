#!/usr/bin/env python3
"""诚实基准 —— 把"被动等权 5y ≈ +11.78%"从 memory 里的孤零数字变成可复现脚本。

任何"系统能赚钱"的论断都要先赢过这些**可交易、含退市股、计成本**的被动标尺:
  - 全市场等权(当日在交易的全部 A 股)
  - ADV 前 1000 / 300 / 100 等权(流动性收窄的池)
两种持有方式:
  - monthly = 月度调仓等权(池内新陈代谢的换手计 0.3%/换手 成本)→ 有净值路径,给 年化/夏普/回撤
  - hold    = 期初选池、一直持有到期末(退市股按最后可得 bar 结算)→ "什么都不做"的参照

幸存者偏差:按**调仓日当时在交易**取池(daily_quotes 在 [rd-40,rd] 有价的 code),
退市股的历史 bar 已在库中(quanti 补过 359 只退市股),period_returns 用最后可得 bar,
故不是"只看活到今天的赢家"。无 point-in-time 指数成分,故不含指数基准(需另拉数据)。

用法:.venv/bin/python scripts/baseline_returns.py [--start 2021-08-01 --end 2026-06-24]
临时账户库 data/agent_bt.db;行情 data/market.db。
"""
import sys
import argparse
from collections import defaultdict
from datetime import date, timedelta

import numpy as np

sys.path.insert(0, ".")
from quanti.data.database import Database
from quanti.data.provider import DataProvider

ACCOUNT_DB = "data/agent_bt.db"
MARKET_DB = "data/market.db"
COST_PER_TURN = 0.003  # 与 factor_backtest / breadth_regime 一致


def monthly_rebal_dates(provider, start, end):
    by_m = defaultdict(list)
    for d in provider.get_trade_dates(start, end):
        by_m[(d.year, d.month)].append(d)
    return sorted(max(ds) for ds in by_m.values())


def adv_ranked(provider, rd, n=None):
    """当日在交易的 code,按 ADV20 降序。n=None 返回全市场。"""
    adv = provider.get_adv20_map(rd - timedelta(days=40), rd)
    ranked = sorted([c for c, v in adv.items() if v and v > 0],
                    key=lambda c: adv[c], reverse=True)
    return ranked if n is None else ranked[:n]


def period_returns(provider, codes, d0, d1):
    """等权:每只 close-to-close,退市股用区间内最后可得 bar。"""
    rets = []
    for c in codes:
        b = provider.get_daily_bars(c, d0, d1)
        if len(b) >= 2 and b[0].close > 0:
            rets.append(b[-1].close / b[0].close - 1.0)
    return float(np.mean(rets)) if rets else 0.0


def stats(monthly_rets):
    if not monthly_rets:
        return dict(total=0, ann=0, sharpe=0, mdd=0, n=0)
    r = np.array(monthly_rets)
    nav = np.cumprod(1 + r)
    total = float(nav[-1] - 1)
    n = len(r)
    ann = (1 + total) ** (12.0 / n) - 1 if total > -1 else -1.0
    sh = r.mean() / r.std(ddof=0) * np.sqrt(12) if r.std(ddof=0) > 0 else 0.0
    pk = np.maximum.accumulate(nav)
    mdd = float(((nav - pk) / pk).min())
    return dict(total=total, ann=ann, sharpe=sh, mdd=mdd, n=n)


def run_monthly(provider, rebal, n_take):
    """月度调仓等权净值。n_take=None 全市场。"""
    rets = []
    prev_pool, prev_rd = None, None
    for rd in rebal:
        pool = adv_ranked(provider, rd, n_take)
        if prev_pool is not None:
            r = period_returns(provider, prev_pool, prev_rd, rd)
            turn = 1.0 - len(set(prev_pool) & set(pool)) / max(len(prev_pool), 1)
            rets.append(r * (1 - turn * COST_PER_TURN))
        prev_pool, prev_rd = pool, rd
    return stats(rets), len(prev_pool)


def run_hold(provider, start_rd, end_rd, n_take):
    """期初选池买入持有到期末,等权总收益(减一次买入成本)。"""
    pool = adv_ranked(provider, start_rd, n_take)
    tot = period_returns(provider, pool, start_rd, end_rd) - COST_PER_TURN
    yrs = (end_rd - start_rd).days / 365.25
    ann = (1 + tot) ** (1 / yrs) - 1 if tot > -1 and yrs > 0 else -1.0
    return dict(total=tot, ann=ann, n=len(pool))


def main():
    ap = argparse.ArgumentParser(description="诚实被动基准")
    ap.add_argument("--start", default="2021-08-01")
    ap.add_argument("--end", default="2026-06-24")
    args = ap.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    db = Database(ACCOUNT_DB, market_db_path=MARKET_DB)
    db.initialize()
    provider = DataProvider(db)
    rebal = monthly_rebal_dates(provider, start, end)
    print(f"窗口 {rebal[0]}~{rebal[-1]}  {len(rebal)}个月  成本{COST_PER_TURN:.1%}/换手\n", flush=True)

    universes = [("全市场", None), ("ADV1000", 1000), ("ADV300", 300), ("ADV100", 100)]

    print("=== 月度调仓等权(有净值路径)===", flush=True)
    print(f"{'池':<10} {'总收益':>9} {'年化':>8} {'夏普':>6} {'回撤':>8} {'期末池':>7}", flush=True)
    for name, n in universes:
        s, sz = run_monthly(provider, rebal, n)
        print(f"{name:<10} {s['total']*100:>+8.1f}% {s['ann']*100:>+7.2f}% "
              f"{s['sharpe']:>6.2f} {s['mdd']*100:>+7.1f}% {sz:>7}", flush=True)

    print("\n=== 期初买入持有到期末(什么都不做)===", flush=True)
    print(f"{'池':<10} {'总收益':>9} {'年化':>8} {'期初池':>7}", flush=True)
    for name, n in universes:
        h = run_hold(provider, rebal[0], rebal[-1], n)
        print(f"{name:<10} {h['total']*100:>+8.1f}% {h['ann']*100:>+7.2f}% {h['n']:>7}", flush=True)

    print("\nDONE", flush=True)
    db.close()


if __name__ == "__main__":
    main()
