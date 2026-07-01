#!/usr/bin/env python3
"""宽度即 beta —— 钉死"收益大头是选池宽度,不是选股/择时",并检验它是否可交易、是否稳健。

基准脚本已显示(5y 月度等权,0.3%成本):
  全市场 +10.5%/yr 回撤-24.5%  >>  ADV1000 -2.5%/yr -53.5%  >  ADV100(生产默认) -3.2%/yr -58%
即"放开宽度纳小盘"是过去 5 年最大的收益杠杆。两条必须先证伪的怀疑:
  (1) 可交易性:全市场含微盘,0.3%成本严重低估摩擦 → 成本敏感性(0.3/0.6/1.0%)。
  (2) regime 押注 vs 稳健 beta:小盘溢价有肥左尾 → IS/OOS 双半程宽度排序是否翻转。

效率:每月把全市场每只票的前向收益算一次缓存,所有池×成本只是切片+平均+换手惩罚。
用法:.venv/bin/python scripts/breadth_alpha.py
"""
import sys
from collections import defaultdict
from datetime import date, timedelta

import numpy as np

sys.path.insert(0, ".")
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.backtest.overfit import deflated_sharpe_ratio, pbo_cscv, sharpe_per_obs

ACCOUNT_DB, MARKET_DB = "data/agent_bt.db", "data/market.db"
START, END = date(2021, 8, 1), date(2026, 6, 24)
POOLS = [  # (名字, lo, hi):ADV 降序排名取 [lo:hi]
    ("全市场", 0, None), ("ADV1500", 0, 1500),
    ("可交易band[200:1500]", 200, 1500), ("可交易band[300:2000]", 300, 2000),
    ("ADV500", 0, 500), ("ADV100(默认)", 0, 100),
]
COSTS = [0.003, 0.006, 0.010]


def monthly_dates(provider):
    by_m = defaultdict(list)
    for d in provider.get_trade_dates(START, END):
        by_m[(d.year, d.month)].append(d)
    return sorted(max(ds) for ds in by_m.values())


def precompute(provider, rebal):
    """每月:ADV 降序 code 列表 + {code: 上一调仓日→本调仓日 的收益}。"""
    ranks, fwd = [], []
    for rd in rebal:
        adv = provider.get_adv20_map(rd - timedelta(days=40), rd)
        order = sorted([c for c, v in adv.items() if v and v > 0],
                       key=lambda c: adv[c], reverse=True)
        ranks.append(order)
    for i, rd in enumerate(rebal):
        d = {}
        if i > 0:
            prev_rd = rebal[i-1]
            for c in ranks[i-1]:  # 上月池里的票才需要收益
                b = provider.get_daily_bars(c, prev_rd, rd)
                if len(b) >= 2 and b[0].close > 0:
                    d[c] = b[-1].close / b[0].close - 1.0
        fwd.append(d)
    return ranks, fwd


def pool_series(ranks, fwd, lo, hi, cost):
    rets, prev = [], None
    for i in range(len(ranks)):
        pool = ranks[i][lo:hi]
        if prev is not None:
            rs = [fwd[i][c] for c in prev if c in fwd[i]]
            r = float(np.mean(rs)) if rs else 0.0
            turn = 1.0 - len(set(prev) & set(pool)) / max(len(prev), 1)
            rets.append(r * (1 - turn * cost))
        prev = pool
    return np.array(rets)


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
    rebal = monthly_dates(provider)
    oos = int(len(rebal) * 0.6) - 1
    print(f"窗口 {rebal[0]}~{rebal[-1]} {len(rebal)}月  IS前{oos}/OOS后{len(rebal)-1-oos} 分界≈{rebal[oos+1]}", flush=True)
    print("预计算全市场月度收益…", flush=True)
    ranks, fwd = precompute(provider, rebal)

    base = {}
    print(f"\n{'池':<22}{'成本':>5} | {'全期年化':>9}{'夏普':>6}{'回撤':>8} | {'IS年化':>8} | {'OOS年化':>9}{'OOS回撤':>8}", flush=True)
    for name, lo, hi in POOLS:
        for cost in COSTS:
            s = pool_series(ranks, fwd, lo, hi, cost)
            f, i, o = stat(s), stat(s[:oos]), stat(s[oos:])
            if cost == COSTS[0]:
                base[name] = s
            tag = "  <-默认" if cost == COSTS[0] else ""
            print(f"{name:<22}{cost*100:>4.1f}% | {f['ann']*100:>+8.2f}%{f['sharpe']:>6.2f}{f['mdd']*100:>+7.1f}% | "
                  f"{i['ann']*100:>+7.2f}% | {o['ann']*100:>+8.2f}%{o['mdd']*100:>+7.1f}%{tag}", flush=True)
        print(flush=True)

    print("=== 宽度排序稳健性(默认成本0.3%)===", flush=True)
    is_rank = sorted(base, key=lambda k: stat(base[k][:oos])['ann'], reverse=True)
    oos_rank = sorted(base, key=lambda k: stat(base[k][oos:])['ann'], reverse=True)
    print(f"  IS : {' > '.join(is_rank)}", flush=True)
    print(f"  OOS: {' > '.join(oos_rank)}", flush=True)
    print(f"  => {'一致(稳健beta)' if is_rank==oos_rank else '翻转(含regime成分)'}", flush=True)

    names = list(base)
    trials = [sharpe_per_obs(base[n]) for n in names]
    best = names[int(np.argmax(trials))]
    dsr = deflated_sharpe_ratio(base[best], trials)
    M = np.column_stack([base[n] for n in names])
    pbo = pbo_cscv(M, n_splits=min(12, (len(rebal)//2)*2))
    print(f"\n=== 过拟合闸(挑最优宽度={best})===", flush=True)
    print(f"  DSR={dsr['dsr']:.3f} (sr_obs={dsr['sr_observed']:.3f} vs sr0={dsr['sr0_benchmark']:.3f})", flush=True)
    print(f"  PBO={pbo['pbo']:.3f} ({pbo['n_configs']}池/{pbo['n_combos']}组合)", flush=True)
    print("\nDONE", flush=True)
    db.close()


if __name__ == "__main__":
    main()
