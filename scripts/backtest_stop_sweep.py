"""Sweep stop-loss parameters over real history to pick / re-validate values.

For each config, run several strategies (trend + mean-reversion) over a liquid
basket and average sharpe / annual / maxDD across them. Then re-run a robust
subset per calendar year — the per-year spread is the evidence that a single
fixed value over-fits one regime (a -8% that's fine in a rally is wrong in a
bear). Use this to choose RiskConfig defaults and to re-check them periodically.

Usage:
    python scripts/backtest_stop_sweep.py --db data/market.db
    python scripts/backtest_stop_sweep.py --start 2023-06-25 --end 2026-06-24
    python scripts/backtest_stop_sweep.py --years 2022,2023,2024,2025
"""
from __future__ import annotations

import argparse
import statistics as st
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quanti.backtest.engine import BacktestEngine  # noqa: E402
from quanti.data.database import Database  # noqa: E402
from quanti.data.provider import DataProvider  # noqa: E402
from quanti.risk.manager import RiskConfig, RiskManager  # noqa: E402
from quanti.strategy.loader import StrategyLoader  # noqa: E402

DEFAULT_BASKET = [
    "600519", "000001", "300750", "002594", "600036", "000858", "601318",
    "600276", "000333", "002415", "300059", "600887", "601888", "000651",
    "002475", "601012", "600030", "000725", "002714", "600309", "601899",
    "000063", "600585", "002304", "601166", "000338", "600048", "601288",
]
STRATEGIES = ["turtle_breakout", "macd_cross", "ma_cross", "bollinger_band",
              "rsi_ob_os"]

# Stop configs to sweep. ATR rows set a wide fixed floor so the ATR stop drives.
SWEEP = [("fixed -5%", dict(stop_loss_pct=-0.05, atr_stop_k=0.0)),
         ("fixed -6%", dict(stop_loss_pct=-0.06, atr_stop_k=0.0)),
         ("fixed -8%", dict(stop_loss_pct=-0.08, atr_stop_k=0.0)),
         ("fixed -10%", dict(stop_loss_pct=-0.10, atr_stop_k=0.0)),
         ("fixed -12%", dict(stop_loss_pct=-0.12, atr_stop_k=0.0)),
         ("ATR k=2 n=14", dict(stop_loss_pct=-0.20, atr_stop_k=2.0, atr_stop_n=14)),
         ("ATR k=2.5 n=14", dict(stop_loss_pct=-0.20, atr_stop_k=2.5, atr_stop_n=14)),
         ("ATR k=3 n=14", dict(stop_loss_pct=-0.20, atr_stop_k=3.0, atr_stop_n=14))]
# Smaller robust set for the per-year drift table.
ROBUST = [("fixed -8%", dict(stop_loss_pct=-0.08, atr_stop_k=0.0)),
          ("ATR k=2 n=14", dict(stop_loss_pct=-0.20, atr_stop_k=2.0, atr_stop_n=14))]


def _codes(provider, basket, start, end, min_bars):
    return [c for c in basket
            if len(provider.get_daily_bars(c, start, end)) >= min_bars]


def _run(provider, protos, codes, start, end, **kw):
    """Average annual / sharpe / maxDD across strategies for one stop config."""
    cfg = RiskConfig(strategy_exit_enabled=False,
                     take_profit_activate_pct=0.0, **kw)
    rows = []
    for sn in STRATEGIES:
        proto = protos.get(sn)
        if proto is None:
            continue
        strat = type(proto)()
        strat.init(getattr(strat, "params", {}) or {})
        res = BacktestEngine(provider, 1_000_000.0,
                             risk_manager=RiskManager(cfg)).run(
            strat, codes, start, end)
        m = res.metrics or {}
        rows.append((m.get("annual_return", 0.0), m.get("sharpe_ratio", 0.0),
                     m.get("max_drawdown", 0.0)))
    if not rows:
        return None
    return (round(st.mean(r[1] for r in rows), 2),
            round(st.mean(r[0] for r in rows), 4),
            round(st.mean(r[2] for r in rows), 4))


def _fmt(name, r):
    return (f"  {name:16s} sharpe={r[0]:+.2f} annual={r[1]*100:+5.1f}% "
            f"maxdd={r[2]*100:5.1f}%") if r else f"  {name:16s} (无数据)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/market.db")
    ap.add_argument("--start", default="2023-06-25")
    ap.add_argument("--end", default="2026-06-24")
    ap.add_argument("--codes", default="")
    ap.add_argument("--years", default="2022,2023,2024,2025")
    args = ap.parse_args()

    db = Database(args.db)
    db.initialize()
    provider = DataProvider(db)
    protos = {s.name: s for s in StrategyLoader().load_directory("strategies")}
    basket = [c.strip() for c in args.codes.split(",") if c.strip()] or DEFAULT_BASKET
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    codes = _codes(provider, basket, start, end, 250)
    print(f"窗口 {start}~{end} | {len(codes)} 只标的 | {len(STRATEGIES)} 策略均值\n")
    for name, kw in SWEEP:
        print(_fmt(name, _run(provider, protos, codes, start, end, **kw)))

    print("\n分年份(同参数在不同市场状态下的漂移 = 动态化依据):")
    for y in [int(x) for x in args.years.split(",") if x.strip()]:
        ys, ye = date(y, 1, 1), date(y, 12, 31)
        yc = _codes(provider, basket, ys, ye, 180)
        if len(yc) < 5:
            continue
        print(f" {y} ({len(yc)} 只):")
        for name, kw in ROBUST:
            print(_fmt(name, _run(provider, protos, yc, ys, ye, **kw)))
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
