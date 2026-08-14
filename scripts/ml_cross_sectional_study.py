#!/usr/bin/env python3
"""截面 ML 实证 — HistGradientBoosting 用 13 个截面因子预测未来收益,
与线性复合(现有生产口径)对比 rank-IC,严格走查(无前视)。

这是本项目「线性截面无 alpha」(2026-07-01)之后未测的最后一页:同样的因子,
非线性组合能不能磨出锐角。方法上完全点对点(point-in-time):每个调仓日 D
的特征只来自 ≤D 的 bar(compute_factor_panel as_of),标签是 D 之后的未来
收益;训练只用 D 之前的历史(expanding window,无未来泄漏)。

口径:
  - 宇宙:2022-01-01 时点流动性 top-N(默认 100),固定到期末;
  - 调仓:每 5 个交易日;标签:hfq 前视 5/10 日收益;
  - 模型:HistGradientBoostingRegressor(默认超参,不调参——调参另行计账),
    前 30 个调仓日作为最小训练史,之后每步重训(expanding);
  - 对比:线性等权复合 z(生产口径)同窗口的 rank-IC;
  - 检验:IC 差的 Newey-West t 值(复用 factors.evaluation._nw_tstat,
    滞后=fwd-1),诚实报告,不装模作样。

用法: python scripts/ml_cross_sectional_study.py --codes 100 --days 130
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _rank_ic(pred: pd.Series, y: pd.Series) -> float:
    a = pred.rank()
    b = y.rank()
    if a.std() == 0 or b.std() == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", type=int, default=100)
    ap.add_argument("--days", type=int, default=130,
                    help="调仓日数(每 5 个交易日一个)")
    ap.add_argument("--min-train", dest="min_train", type=int, default=30)
    ap.add_argument("--end", default="2026-07-31")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    from sklearn.ensemble import HistGradientBoostingRegressor

    from quanti.agent.universe import resolve_tradable_universe
    from quanti.data.database import Database
    from quanti.data.provider import DataProvider
    from quanti.factors.cross_sectional import FactorConfig, compute_factor_panel
    from quanti.factors.evaluation import _nw_tstat

    db = Database("data/paper.db", market_db_path="data/market.db")
    db.initialize()
    provider = DataProvider(db)
    end = date.fromisoformat(args.end)
    codes = resolve_tradable_universe(db, provider, pool=None,
                                      params={"liquidity_filter": True},
                                      as_of=date(2022, 1, 1))[:args.codes]
    days = provider.get_trade_dates(date(2022, 1, 1), end)
    rebal = days[::5][-args.days:]
    print("universe=%d rebalance_days=%d %s..%s"
          % (len(codes), len(rebal), rebal[0], rebal[-1]))

    cfg = FactorConfig(industry_neutralize=False)
    features_all = list(cfg.resolved())
    print("factors:", features_all)

    rows = []
    for d in rebal:
        panel = compute_factor_panel(provider, db, codes, as_of=d, config=cfg)
        if panel.empty:
            continue
        fwd = {}
        for code in codes:
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
        for code in panel.index:
            feat = {f: panel.loc[code, f] for f in features_all}
            if any(v is None or (isinstance(v, float) and np.isnan(v))
                   for v in feat.values()):
                continue
            row = {"day": d, "code": code, **feat}
            for h in (5, 10):
                row["fwd%d" % h] = fwd.get((code, h), np.nan)
            rows.append(row)
    if not rows:
        print("no data — abort")
        sys.exit(2)
    data = pd.DataFrame(rows)
    avg_codes = data.groupby("day").size().mean()
    print("rows=%d avg_codes_per_day=%.0f" % (len(data), avg_codes))

    data["linear"] = data[features_all].mean(axis=1)

    ml_ic = {"5": [], "10": []}
    lin_ic = {"5": [], "10": []}
    diffs = {"5": [], "10": []}
    pred_days = 0
    for d in rebal:
        # CLEAN walk-forward: training rows must have their fwd10 label
        # window CLOSE on or before d. Rows from day d-5 carry labels through
        # d+5 — training on them leaks post-d returns into the model. The
        # 15-calendar-day gap (≈10 trading days) guarantees label windows
        # end before the test day.
        cutoff = d - timedelta(days=15)
        hist = data[(data["day"] < d) & (data["day"] <= cutoff)]
        if len(hist["day"].unique()) < args.min_train:
            continue
        cur = data[data["day"] == d].dropna(subset=["fwd10"])
        if cur.empty or len(hist) < 100:
            continue
        model = HistGradientBoostingRegressor(max_iter=100,
                                              learning_rate=0.05,
                                              max_depth=4,
                                              random_state=0)
        train = hist.dropna(subset=["fwd10"])
        model.fit(train[features_all].values, train["fwd10"].values)
        pred = model.predict(cur[features_all].values)
        pred_days += 1
        for h in ("5", "10"):
            sub = cur.dropna(subset=["fwd%s" % h])
            if len(sub) < 20:
                continue
            idx = cur.index.intersection(sub.index)
            p = pd.Series(pred, index=cur.index).loc[idx]
            y = sub["fwd%s" % h]
            ic_ml = _rank_ic(p, y)
            ic_lin = _rank_ic(sub["linear"], y)
            if not np.isnan(ic_ml):
                ml_ic[h].append(ic_ml)
                lin_ic[h].append(ic_lin)
                diffs[h].append(ic_ml - ic_lin)
        if pred_days % 20 == 0:
            print("  predicted %d days up to %s" % (pred_days, d))

    out = {"features": features_all, "n_codes": len(codes),
           "rebalance_days": len(rebal), "pred_days": pred_days,
           "horizons": {}}
    for h in ("5", "10"):
        if not ml_ic[h]:
            continue
        ml_mean = float(np.mean(ml_ic[h]))
        lin_mean = float(np.mean(lin_ic[h]))
        t = _nw_tstat(np.array(diffs[h]), max(0, int(h) - 1))
        out["horizons"][h] = {
            "ml_ic_mean": ml_mean, "linear_ic_mean": lin_mean,
            "ic_diff_mean": float(np.mean(diffs[h])),
            "nw_t": float(t) if t == t else None,
            "n_days": len(ml_ic[h]),
        }
        print("h=%s: ml_ic=%+.4f linear_ic=%+.4f diff=%+.4f nw_t=%+.2f n=%d"
              % (h, ml_mean, lin_mean, np.mean(diffs[h]), t, len(ml_ic[h])))
    if args.json:
        import json
        with open(args.json, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    print("done")


if __name__ == "__main__":
    main()

