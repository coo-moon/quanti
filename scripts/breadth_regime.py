#!/usr/bin/env python3
"""#3 选池广度=beta:ADV 前 N 等权净收益,按 regime 分窗(回应 red-team:单窗会被
2024-26 大盘行情带偏)。纯等权池收益,不算因子,故快(~1-2 分钟)。

窗口:全5y / 小盘+踩踏期(2021-08~2023-12)/ 2024-01 微盘踩踏 / 大盘修复(OOS 2024-07~2026-06)。
若 ADV100/300/1000 的排序在窗口间翻转 → "保大盘默认"是 regime 押注而非稳健默认。
临时库 data/agent_bt.db;行情 data/market.db。
"""
import sys
from collections import defaultdict
from datetime import date, timedelta

import numpy as np

sys.path.insert(0, ".")
from quanti.data.database import Database
from quanti.data.provider import DataProvider

COST = 0.003
NS = [100, 300, 1000]
WINDOWS = [
    ("全5y", date(2021, 8, 1), date(2026, 6, 24)),
    ("小盘期+踩踏 21-08~23-12", date(2021, 8, 1), date(2023, 12, 31)),
    ("微盘踩踏 23-12~24-03", date(2023, 12, 1), date(2024, 3, 31)),
    ("大盘修复(OOS) 24-07~26-06", date(2024, 7, 1), date(2026, 6, 24)),
]


def monthly(provider, start, end):
    by_m = defaultdict(list)
    for d in provider.get_trade_dates(start, end):
        by_m[(d.year, d.month)].append(d)
    return sorted(max(ds) for ds in by_m.values())


def adv_ranked(provider, rd, n):
    adv = provider.get_adv20_map(rd - timedelta(days=40), rd)
    return sorted([c for c, v in adv.items() if v and v > 0],
                  key=lambda c: adv[c], reverse=True)[:n]


def fwd(provider, codes, d0, d1):
    rs = []
    for c in codes:
        b = provider.get_daily_bars(c, d0, d1)
        if len(b) >= 2 and b[0].close > 0:
            rs.append(b[-1].close / b[0].close - 1.0)
    return float(np.mean(rs)) if rs else 0.0


def stat(rets):
    if not rets:
        return (0, 0, 0)
    r = np.array(rets)
    nav = np.cumprod(1 + r)
    n = len(r)
    ann = (1 + nav[-1] - 1) ** (12.0 / n) - 1 if nav[-1] > 0 else -1
    sh = r.mean() / r.std(ddof=0) * np.sqrt(12) if r.std(ddof=0) > 0 else 0
    pk = np.maximum.accumulate(nav)
    mdd = float(((nav - pk) / pk).min())
    return ann, sh, mdd


def main():
    db = Database("data/agent_bt.db", market_db_path="data/market.db")
    db.initialize()
    provider = DataProvider(db)
    for label, s, e in WINDOWS:
        rebal = monthly(provider, s, e)
        rets = {n: [] for n in NS}
        prev = {n: None for n in NS}
        prev_rd = None
        for rd in rebal:
            pools = {n: adv_ranked(provider, rd, n) for n in NS}
            if prev_rd is not None:
                for n in NS:
                    r = fwd(provider, prev[n], prev_rd, rd)
                    turn = 1 - len(set(prev[n]) & set(pools[n])) / max(len(prev[n]), 1)
                    rets[n].append(r * (1 - turn * COST))
            prev = pools
            prev_rd = rd
        print(f"\n[{label}] {len(rebal)}月", flush=True)
        for n in NS:
            ann, sh, mdd = stat(rets[n])
            print(f"  ADV{n:<4} 等权: 年化{ann*100:+7.2f}% 夏普{sh:5.2f} 回撤{mdd*100:6.1f}%",
                  flush=True)
    print("\nDONE", flush=True)
    db.close()


if __name__ == "__main__":
    main()
