"""A/B backtest: does the trailing take-profit overlay help or hurt?

Both arms keep the strategy's own buy/sell logic AND the -8% stop-loss
(that's how the backtest engine already runs). The only difference:

  A (baseline)  take_profit_activate_pct = 0   → no take-profit
  B (trailing)  take_profit_activate_pct = .15, trail .10

So any metric delta is attributable purely to the trailing take-profit.
Run several strategies — TP should matter most for trend followers
(turtle, ma_cross), least for mean-reversion (rsi, bollinger).

Usage:
    python scripts/backtest_exit_compare.py
    python scripts/backtest_exit_compare.py --codes 600519,000001 --start 2025-06-16
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quanti.backtest.engine import BacktestEngine  # noqa: E402
from quanti.data.database import Database  # noqa: E402
from quanti.data.provider import DataProvider  # noqa: E402
from quanti.risk.manager import RiskConfig, RiskManager  # noqa: E402
from quanti.strategy.loader import StrategyLoader  # noqa: E402

# A liquid, diversified default basket (filtered to what's actually synced).
DEFAULT_BASKET = [
    "600519", "000001", "300750", "002594", "600036", "000858", "601318",
    "600276", "000333", "002415", "300059", "600887", "601888", "000651",
    "002475", "601012", "600030", "000725", "002714", "600309", "601899",
    "000063", "600585", "002304", "601166", "000338", "600048", "601288",
]


def _fmt(m: dict) -> str:
    if not m:
        return "  (无交易/数据不足)"
    return (f"  总收益 {m.get('total_return', 0) * 100:+6.2f}% | "
            f"年化 {m.get('annual_return', 0) * 100:+6.2f}% | "
            f"夏普 {m.get('sharpe_ratio', 0):5.2f} | "
            f"最大回撤 {m.get('max_drawdown', 0) * 100:6.2f}%")


def _risk(take_profit: bool) -> RiskManager:
    cfg = RiskConfig(
        take_profit_activate_pct=0.15 if take_profit else 0.0,
        take_profit_trail_pct=0.10,
        strategy_exit_enabled=False,  # backtest replays the strategy itself
    )
    return RiskManager(cfg)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/quanti.db")
    ap.add_argument("--codes", default="")
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    ap.add_argument("--cash", type=float, default=1_000_000.0)
    ap.add_argument("--strategies", default="ma_cross,turtle_breakout,ma_volume,macd_cross")
    args = ap.parse_args()

    db = Database(args.db)
    db.initialize()
    provider = DataProvider(db)

    end = date.fromisoformat(args.end) if args.end else (
        db.get_global_latest_quote_date() or date.today())
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=365)

    basket = [c.strip() for c in args.codes.split(",") if c.strip()] or DEFAULT_BASKET
    # Keep only codes with enough history in the window.
    codes = []
    for c in basket:
        bars = provider.get_daily_bars(c, start, end)
        if len(bars) >= 120:
            codes.append(c)
    if not codes:
        print("没有足够历史的标的,换 --codes 或更早的 --start")
        return 1

    loader = StrategyLoader()
    strategies = {s.name: s for s in loader.load_directory("strategies")}

    print(f"窗口 {start} ~ {end} | {len(codes)} 只标的 | 初始 ¥{args.cash:,.0f}")
    print(f"标的: {','.join(codes)}\n")

    for sname in [s.strip() for s in args.strategies.split(",") if s.strip()]:
        strat_proto = strategies.get(sname)
        if strat_proto is None:
            print(f"[skip] 未知策略 {sname}")
            continue
        print(f"=== {sname} ({getattr(strat_proto, 'name_zh', '')}) ===")
        results = {}
        for label, tp in (("A 无止盈", False), ("B 移动止盈", True)):
            strat = type(strat_proto)()
            strat.init(getattr(strat, "params", {}) or {})
            engine = BacktestEngine(provider, initial_cash=args.cash,
                                    risk_manager=_risk(tp))
            res = engine.run(strat, codes, start, end)
            results[label] = res.metrics
            print(f"{label:10s}{_fmt(res.metrics)} | 交易 {len(res.trades)}")
        a, b = results["A 无止盈"], results["B 移动止盈"]
        if a and b:
            dr = (b.get("total_return", 0) - a.get("total_return", 0)) * 100
            dd = (b.get("max_drawdown", 0) - a.get("max_drawdown", 0)) * 100
            print(f"           Δ收益 {dr:+.2f}pp | Δ最大回撤 {dd:+.2f}pp "
                  f"({'回撤改善' if dd > 0 else '回撤恶化'})")
        print()
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
