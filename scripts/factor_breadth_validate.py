#!/usr/bin/env python3
"""验证 #4(因子横截面口径)+ #3(选池广度 beta)。

#4 核心问题:production 的 ensemble 把因子面板只在 ADV-100 候选上算横截面
(winsorize/zscore/industry-demean 全相对这 100 只同质大盘),区分度衰减。
本脚本对比"同样的 ADV-100 候选里选 top-K":
  - narrow: 在这 100 只里算 composite 排序(= 现状)
  - wide  : 在 ADV-1000 宽池里算 composite,再回到这 100 只里按宽口径排序
看 (a) 选出来的票重合度,(b) 前向净收益谁更好。若几乎一样 → #4 不值得改;
若 wide 明显更好 → 值得把面板搬到宽池。

#3:顺带打印 ADV 前 100/300/1000 等权净收益 + 2024-01 尾部,量化 breadth=beta。

PIT:compute_factor_panel(as_of=rd) + point-in-time ADV。净成本 COST_PER_TURN。
临时 account 库 data/agent_bt.db;行情 data/market.db。
用法:.venv/bin/python scripts/factor_breadth_validate.py [--start --end --recent N]
"""
import argparse
import sys
import time
from collections import defaultdict
from datetime import date, timedelta

import numpy as np

sys.path.insert(0, ".")
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.factors.cross_sectional import compute_factor_panel

ACCOUNT_DB = "data/agent_bt.db"
MARKET_DB = "data/market.db"
N_NARROW = 100        # production 候选(no_screener_take 默认)
N_WIDE = 1000         # 宽池(给因子横截面更多对照)
TOP_K = 20
COST_PER_TURN = 0.003
OOS_START = date(2024, 7, 1)


def monthly_rebal_dates(provider, start, end):
    by_m = defaultdict(list)
    for d in provider.get_trade_dates(start, end):
        by_m[(d.year, d.month)].append(d)
    return sorted(max(ds) for ds in by_m.values())


def adv_ranked(provider, rd, n):
    adv = provider.get_adv20_map(rd - timedelta(days=40), rd)
    ranked = sorted([c for c, v in adv.items() if v and v > 0],
                    key=lambda c: adv[c], reverse=True)
    return ranked[:n]


def top_by_panel(panel, restrict_to, k):
    """Top-k codes by composite, restricted to `restrict_to` (a set)."""
    if panel is None or panel.empty or "composite" not in panel.columns:
        return []
    comp = panel["composite"].dropna()
    comp = comp[[c for c in comp.index if c in restrict_to]]
    return list(comp.sort_values(ascending=False).head(k).index)


def fwd_ret(provider, codes, d0, d1):
    rets = []
    for c in codes:
        bars = provider.get_daily_bars(c, d0, d1)
        if len(bars) >= 2 and bars[0].close > 0:
            rets.append(bars[-1].close / bars[0].close - 1.0)
    return float(np.mean(rets)) if rets else 0.0


def stats(rets):
    if not rets:
        return dict(total=0, ann=0, sharpe=0, mdd=0, n=0)
    r = np.array(rets)
    nav = np.cumprod(1 + r)
    total = nav[-1] - 1
    n = len(r)
    ann = (1 + total) ** (12.0 / n) - 1 if total > -1 else -1
    sh = r.mean() / r.std(ddof=0) * np.sqrt(12) if r.std(ddof=0) > 0 else 0
    pk = np.maximum.accumulate(nav)
    mdd = float(((nav - pk) / pk).min())
    return dict(total=total, ann=ann, sharpe=sh, mdd=mdd, n=n)


def cost_adj(ret, prev, cur):
    turn = 1.0 - len(set(prev) & set(cur)) / max(len(prev), 1)
    return ret * (1 - turn * COST_PER_TURN)


def line(tag, s):
    print(f"  {tag:18} 总{s['total']*100:+7.1f}% 年化{s['ann']*100:+6.2f}% "
          f"夏普{s['sharpe']:5.2f} 回撤{s['mdd']*100:6.1f}% ({s['n']}月)", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-08-01")
    ap.add_argument("--end", default="2026-06-24")
    ap.add_argument("--recent", type=int, default=0)
    args = ap.parse_args()

    db = Database(ACCOUNT_DB, market_db_path=MARKET_DB)
    db.initialize()
    provider = DataProvider(db)
    rebal = monthly_rebal_dates(provider, date.fromisoformat(args.start),
                                date.fromisoformat(args.end))
    if args.recent:
        rebal = rebal[-args.recent:]
    print(f"调仓 {len(rebal)} 月 {rebal[0]}~{rebal[-1]} | narrow={N_NARROW} "
          f"wide={N_WIDE} topK={TOP_K} cost/turn={COST_PER_TURN}", flush=True)

    # accumulators: each is list of monthly net returns
    R = defaultdict(list)            # key -> [monthly net rets]
    Roos = defaultdict(list)
    overlaps = []
    prev = {}                        # key -> prev holdings
    prev_rd = None

    for i, rd in enumerate(rebal):
        t = time.time()
        pool_n = adv_ranked(provider, rd, N_NARROW)
        pool_w = adv_ranked(provider, rd, N_WIDE)
        set_n = set(pool_n)
        # narrow panel: only the 100 candidates
        pan_n = compute_factor_panel(provider, db, pool_n, as_of=rd)
        # wide panel: 1000-stock cross-section, then restrict ranking to the 100
        pan_w = compute_factor_panel(provider, db, pool_w, as_of=rd)

        hold = {
            "narrow_top20": top_by_panel(pan_n, set_n, TOP_K),       # #4 现状
            "wide_top20": top_by_panel(pan_w, set_n, TOP_K),         # #4 宽口径
            "adv100_ew": pool_n,                                     # #3 池等权
            "adv300_ew": adv_ranked(provider, rd, 300),
            "adv1000_ew": pool_w,
        }
        ov = (len(set(hold["narrow_top20"]) & set(hold["wide_top20"]))
              / max(len(hold["narrow_top20"]), 1))
        if prev_rd is not None:
            overlaps.append(ov)
            for k, codes in hold.items():
                if not prev.get(k):
                    continue
                r = cost_adj(fwd_ret(provider, prev[k], prev_rd, rd), prev[k], codes)
                R[k].append(r)
                if rd >= OOS_START:
                    Roos[k].append(r)
        prev = hold
        prev_rd = rd
        print(f"  [{i+1}/{len(rebal)}] {rd} overlap(narrow∩wide)={ov*100:3.0f}% "
              f"({time.time()-t:.1f}s)", flush=True)

    print(f"\n=== #4 因子横截面口径(同在 ADV-{N_NARROW} 里选 top{TOP_K})===", flush=True)
    print(f"narrow∩wide 平均重合 {np.mean(overlaps)*100:.0f}%", flush=True)
    print("[全5y]", flush=True)
    line("narrow(现状)", stats(R["narrow_top20"]))
    line("wide(宽口径)", stats(R["wide_top20"]))
    line("ADV100 等权基准", stats(R["adv100_ew"]))
    print("[OOS 2y]", flush=True)
    line("narrow(现状)", stats(Roos["narrow_top20"]))
    line("wide(宽口径)", stats(Roos["wide_top20"]))
    line("ADV100 等权基准", stats(Roos["adv100_ew"]))

    print("\n=== #3 选池广度=beta(等权,净成本)===", flush=True)
    print("[全5y]", flush=True)
    for k in ("adv100_ew", "adv300_ew", "adv1000_ew"):
        line(k, stats(R[k]))
    print("[OOS 2y]", flush=True)
    for k in ("adv100_ew", "adv300_ew", "adv1000_ew"):
        line(k, stats(Roos[k]))
    print("\nDONE", flush=True)
    db.close()


if __name__ == "__main__":
    main()
