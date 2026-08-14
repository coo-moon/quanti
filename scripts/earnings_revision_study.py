#!/usr/bin/env python3
"""盈利修正因子实证 — growth_rev(增长率环比)与 SUE(季节意外)的截面 IC。

动机:13 因子里的 growth_earnings 用的是 netprofit_yoy 的「水平」;本脚本测
两个真正的事件驱动因子,全部点对点(ann_date ≤ D 才可见,无前视):

  growth_rev = netprofit_yoy(t) - netprofit_yoy(t-1)  (增长率动能/修正)
  sue        = (net_profit(t) - net_profit(t-4)) / |net_profit(t-4)|  (季节性
               随机游走下的意外盈利代理,无分析师预期数据时的标准做法)

评估:每 5 个交易日的调仓日 D,横截面 rank-IC vs hfq 前视 5/10 日收益;
对比基线:netprofit_yoy 水平(现有 growth_earnings 口径)与 0。NW t 检验
(滞后 = fwd-1),清洁评估(因子 PIT by 构造,标签窗口从 D 开始,无重叠问题)。

用法: python scripts/earnings_revision_study.py --codes 300
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _rank_ic(pred: pd.Series, y: pd.Series) -> float:
    a, b = pred.rank(), y.rank()
    if a.std() == 0 or b.std() == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


SQL = """
WITH ranked AS (
  SELECT code, end_date, ann_date, net_profit, netprofit_yoy,
         ROW_NUMBER() OVER (PARTITION BY code
                            ORDER BY ann_date DESC, end_date DESC) AS rn
  FROM financials WHERE ann_date <= ?
)
SELECT code, end_date, ann_date, net_profit, netprofit_yoy, rn
FROM ranked WHERE rn <= 5
"""


def _factors_at(db, d: date) -> pd.DataFrame:
    """One query per rebalance day: the latest 5 announced reports per code.
    Returns a DataFrame indexed by code with growth_rev / sue / yoy_level."""
    df = pd.read_sql_query(SQL, db._raw_conn, params=(d.isoformat(),))
    if df.empty:
        return pd.DataFrame()
    rows = {}
    for code, g in df.groupby("code"):
        g = g.sort_values(["ann_date", "end_date"], ascending=False)
        yoy = pd.to_numeric(g["netprofit_yoy"], errors="coerce").tolist()
        np_ = pd.to_numeric(g["net_profit"], errors="coerce").tolist()
        entry = {}
        if len(yoy) >= 2 and yoy[0] == yoy[0] and yoy[1] == yoy[1]:
            entry["growth_rev"] = yoy[0] - yoy[1]
        ok5 = (len(np_) >= 5 and all(v == v for v in np_[:5])
               and np_[4] == np_[4] and np_[4] != 0)
        if ok5:
            entry["sue"] = (np_[0] - np_[4]) / abs(np_[4])
        if yoy and yoy[0] == yoy[0]:
            entry["yoy_level"] = yoy[0]
        if entry:
            rows[code] = entry
    out = pd.DataFrame.from_dict(rows, orient="index")
    for c in out.columns:
        q1, q99 = out[c].quantile(0.01), out[c].quantile(0.99)
        out[c] = out[c].clip(q1, q99)
        sd = out[c].std()
        if sd and sd > 0:
            out[c] = (out[c] - out[c].mean()) / sd
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", type=int, default=300)
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--end", default="2026-08-10")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    from quanti.agent.universe import resolve_tradable_universe
    from quanti.data.database import Database
    from quanti.data.provider import DataProvider
    from quanti.factors.evaluation import _nw_tstat

    db = Database("data/paper.db", market_db_path="data/market.db")
    db.initialize()
    provider = DataProvider(db)
    end = date.fromisoformat(args.end)
    codes = resolve_tradable_universe(db, provider, pool=None,
                                      params={"liquidity_filter": True,
                                              "selector_max_universe": args.codes},
                                      as_of=date(2023, 1, 1))
    days = provider.get_trade_dates(date(2023, 1, 1), end)
    rebal = days[::5][-args.days:]
    print("universe=%d rebalance_days=%d %s..%s"
          % (len(codes), len(rebal), rebal[0], rebal[-1]))

    ics = {f: {"5": [], "10": []} for f in ("growth_rev", "sue", "yoy_level")}
    by_year = {(f, h): {} for f in ics for h in ("5", "10")}
    for i, d in enumerate(rebal):
        f = _factors_at(db, d)
        if f.empty:
            continue
        f = f[f.index.isin(codes)]
        if len(f) < 30:
            continue
        fwd = {}
        for code in f.index:
            try:
                bars = provider.get_daily_bars(code, d, d + timedelta(days=30))
            except Exception:
                continue
            closes = [(b.date, float(b.close)) for b in bars if b.close > 0]
            if not closes or closes[0][0] != d:
                continue
            for h in (5, 10):
                if h < len(closes):
                    fwd[(code, h)] = closes[h][1] / closes[0][1] - 1.0
        for h in (5, 10):
            y = pd.Series({c: fwd[(c, h)] for c in f.index if (c, h) in fwd})
            for fac in ics:
                if fac not in f.columns:
                    continue
                ic = _rank_ic(f.loc[y.index, fac], y) if len(y) >= 20 else np.nan
                if not np.isnan(ic):
                    ics[fac][str(h)].append(ic)
                    by_year[(fac, str(h))].setdefault(d.year, []).append(ic)
        if (i + 1) % 30 == 0:
            print("  done %d days up to %s" % (i + 1, d))

    out = {"universe": len(codes), "rebalance_days": len(rebal),
           "factors": {}}
    for fac in ics:
        out["factors"][fac] = {}
        for h in ("5", "10"):
            vals = ics[fac][h]
            if not vals:
                continue
            arr = np.array(vals)
            t = _nw_tstat(arr, max(0, int(h) - 1))
            out["factors"][fac][h] = {
                "ic_mean": float(arr.mean()), "nw_t": float(t) if t == t else None,
                "n": len(vals),
            }
            print("%s h=%s: ic=%+.4f nw_t=%+.2f n=%d"
                  % (fac, h, arr.mean(), t, len(vals)))
    print("--- per-year ic_mean (factor, h, year, mean, n) ---")
    for key, years in sorted(by_year.items()):
        for yr in sorted(years):
            vals = years[yr]
            print("%s h=%s %d: %+.4f n=%d" % (key[0], key[1], yr,
                                              np.mean(vals), len(vals)))
    if args.json:
        import json
        with open(args.json, "w") as fp:
            json.dump(out, fp, ensure_ascii=False, indent=2)
    print("done")


if __name__ == "__main__":
    main()

