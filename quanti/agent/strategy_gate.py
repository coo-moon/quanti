"""Strategy health gate — long-horizon sanity backtest per loadable strategy.

The walk-forward selector ranks strategies on recent OOS Sharpe, but a
strategy that looks fine in a short fold can still be an ACCOUNT-KILLER over
longer horizons: under the default risk budget (-30% portfolio breaker + ATR
stops) the 2026-08-14 sizer study showed every built-in strategy tripping
the breaker within 1-2 years — and the erratum showed the post-halt tail was
being misread as "dormancy". This gate runs a 2-year backtest per strategy
under the DEFAULT risk config and labels it:

  * breaker   — the portfolio circuit breaker tripped mid-run: under default
    risk this strategy rides a -30% drawdown. EXCLUDE from selection.
  * deep_loss — no breaker, but annualized Sharpe < threshold: persistent
    loser. EXCLUDE.
  * pass      — nothing alarming; keep.

Computed once a day by the bg-sync hook (persisted to strategy_gate), read
by the selector at pick time. A strategy with insufficient data is skipped
(no verdict) and stays eligible — the gate only ever excludes on evidence.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from quanti.backtest.engine import BacktestEngine
from quanti.risk.manager import RiskConfig, RiskManager
from quanti.strategy.loader import StrategyLoader

logger = logging.getLogger(__name__)

#: Long-horizon window for the sanity backtest.
DEFAULT_LOOKBACK_DAYS = 730
#: Annualized-Sharpe floor: below this (with no breaker) a strategy is a
#: persistent loser, not a regime victim.
DEFAULT_SHARPE_THRESHOLD = -0.5
#: Universe cap per strategy backtest (same spirit as selector_max_universe).
DEFAULT_MAX_CODES = 100

GATE_VERDICTS = ("pass", "breaker", "deep_loss")


def _resolve_universe(db, provider, as_of: date, max_codes: int) -> list[str]:
    from quanti.agent.universe import resolve_tradable_universe
    params = {"liquidity_filter": True}
    codes = resolve_tradable_universe(db, provider, pool=None, params=params,
                                      as_of=as_of)
    return codes[:max_codes] if len(codes) > max_codes else codes


def compute_gate(db, provider, *,
                 strategies_dir: str = "strategies",
                 end: date | None = None,
                 lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                 sharpe_threshold: float = DEFAULT_SHARPE_THRESHOLD,
                 max_codes: int = DEFAULT_MAX_CODES) -> dict[str, dict]:
    """Run the sanity backtest per strategy and persist today verdicts.

    Returns {strategy: {verdict, reason, sharpe, max_drawdown, halted,
    halted_at}}. Never raises: a broken strategy backtest yields a skip
    (no verdict) rather than killing the gate.
    """
    end = end or date.today()
    start = end - timedelta(days=lookback_days)
    strategies = [s for s in StrategyLoader().load_directory(strategies_dir)]
    if not strategies:
        return {}
    try:
        codes = _resolve_universe(db, provider, end, max_codes)
    except Exception as e:  # noqa: BLE001
        logger.warning("strategy gate universe failed: %s", e)
        codes = []
    report: dict[str, dict] = {}
    engine = BacktestEngine(provider=provider, initial_cash=1_000_000.0,
                            risk_manager=RiskManager(RiskConfig()))
    for strat in strategies:
        entry: dict = {"verdict": "", "reason": "", "sharpe": None,
                       "max_drawdown": None, "halted": False,
                       "halted_at": None}
        try:
            strat.init({})
            res = engine.run(strat, codes, start, end)
        except Exception as e:  # noqa: BLE001 - one strategy cannot kill the gate
            logger.warning("strategy gate backtest failed for %s: %s",
                           strat.name, e)
            report[strat.name] = entry  # no verdict → stays eligible
            continue
        m = res.metrics or {}
        sharpe = float(m.get("sharpe_ratio", 0) or 0)
        dd = float(m.get("max_drawdown", 0) or 0)
        entry.update({"sharpe": sharpe, "max_drawdown": dd,
                      "halted": res.halted,
                      "halted_at": (res.halted_at.isoformat()
                                    if res.halted_at else None)})
        if res.halted:
            entry["verdict"] = "breaker"
            entry["reason"] = ("组合熔断停摆于 " + str(entry["halted_at"])
                               + ", 默认风险下 " + str(lookback_days)
                               + " 天窗口触发 -30% 回撤熔断")
        elif sharpe < sharpe_threshold:
            entry["verdict"] = "deep_loss"
            entry["reason"] = ("年化夏普 %.2f < 阈值 %s, 无熔断但持续亏损"
                               % (sharpe, sharpe_threshold))
        else:
            entry["verdict"] = "pass"
            entry["reason"] = ("年化夏普 %.2f, 回撤 %.1f%%, 未触熔断"
                               % (sharpe, dd * 100))
        db.save_strategy_gate(strat.name, end, entry["verdict"],
                              entry["reason"], sharpe, dd, res.halted)
        report[strat.name] = entry
        logger.info("strategy gate %s: %s (sharpe=%.2f halted=%s)",
                    strat.name, entry["verdict"], sharpe, res.halted)
    return report


def excluded_names(db) -> set[str]:
    """Strategies the selector must drop: gate verdict breaker or deep_loss."""
    gate = db.load_strategy_gate()
    return {name for name, g in gate.items()
            if g.get("verdict") in ("breaker", "deep_loss")}


def format_gate(report: dict) -> str:
    """Human-readable rendering for the strategy-gate CLI."""
    lines = ["策略健康闸门 (%d 个策略)" % len(report)]
    for name, g in report.items():
        mark = "✅" if g["verdict"] == "pass" else "⚠️"
        v = g["verdict"] or "skip(无判定,保持可选用)"
        lines.append("  %s %s: %s — %s" % (mark, name, v, g["reason"]))
    return "\n".join(lines)

