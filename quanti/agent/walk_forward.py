"""Walk-forward (rolling out-of-sample) validation for strategies.

The Selector's old in-sample scoring picks the strategy that did best on the
last 365 days — which is almost guaranteed to overfit because that same data
was used to choose. Walk-forward fixes this:

  - Carve the recent history into N non-overlapping test windows.
  - For each window, run the backtest with a warm-up tail before it (so
    indicators like MA(20) / MACD have valid state when the test starts).
  - Compute metrics only on the test slice — that's the OOS performance.
  - Aggregate across folds; consistency across folds is itself a signal
    (a strategy that does +30% one month and -20% the next is *not* better
    than one that does +5% every month, even if their mean is similar).

Strategies in this codebase don't have fittable parameters, so the "train"
segment is really a *warm-up* tail. The label "walk-forward" is kept because
the temporal split and the OOS-only scoring are the part that matters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from quanti.backtest.engine import BacktestEngine
from quanti.backtest.metrics import annualized_sharpe, compute_metrics

logger = logging.getLogger(__name__)

# Pool OOS daily returns across all folds before estimating Sharpe. A Sharpe
# from a single ~15-bar fold has a huge standard error; averaging such per-fold
# Sharpes (the old behavior) just averages noise. Pooling uses every OOS
# observation for one estimate. Below this many pooled obs, Sharpe is unreliable
# → reported as 0 ('no signal') and the selector's min-trades guard takes over.
_MIN_POOLED_OBS = 20


@dataclass
class Fold:
    warmup_start: date
    test_start: date
    test_end: date


@dataclass
class FoldResult:
    fold: Fold
    metrics: dict  # OOS metrics computed on the test slice only
    n_trades_oos: int
    oos_returns: list[float] = field(default_factory=list)  # daily OOS returns


@dataclass
class WalkForwardResult:
    folds: list[FoldResult] = field(default_factory=list)
    oos_annual_return: float = 0.0
    oos_max_drawdown: float = 0.0
    oos_sharpe: float = 0.0
    oos_consistency: float = 0.0  # 1 - CoV of fold returns; high = stable
    total_trades_oos: int = 0

    def as_dict(self) -> dict:
        return {
            "oos_annual_return": self.oos_annual_return,
            "oos_max_drawdown": self.oos_max_drawdown,
            "oos_sharpe": self.oos_sharpe,
            "oos_consistency": self.oos_consistency,
            "total_trades_oos": self.total_trades_oos,
            "n_folds": len(self.folds),
        }


def make_folds(
    end: date,
    n_folds: int = 3,
    warmup_days: int = 120,
    test_days: int = 21,
) -> list[Fold]:
    """Build `n_folds` non-overlapping OOS windows, newest first.

    Fold i has:
      test_end_i   = end - i * test_days
      test_start_i = test_end_i - test_days + 1
      warmup_start = test_start_i - warmup_days

    Newest fold is index 0; oldest is index n_folds-1. This ordering makes
    "fold 0" mean "most recent OOS month", which is the most relevant when
    debugging a recent decision.
    """
    folds: list[Fold] = []
    for i in range(n_folds):
        test_end = end - timedelta(days=i * test_days)
        test_start = test_end - timedelta(days=test_days - 1)
        warmup_start = test_start - timedelta(days=warmup_days)
        folds.append(Fold(warmup_start=warmup_start,
                          test_start=test_start, test_end=test_end))
    return folds


def _slice_oos_curve(equity_curve: pd.Series, fold: Fold) -> pd.Series:
    """Slice the full equity curve down to the OOS test window only."""
    if equity_curve is None or len(equity_curve) == 0:
        return pd.Series(dtype=float)
    # Index is `date` objects. Use boolean mask rather than .loc since the
    # exact test_start may not be a trading day in the curve.
    mask = (equity_curve.index >= fold.test_start) & (equity_curve.index <= fold.test_end)
    return equity_curve[mask]


def run_walk_forward(
    engine: BacktestEngine,
    strategy_factory,
    codes: list[str],
    end: date,
    n_folds: int = 3,
    warmup_days: int = 120,
    test_days: int = 21,
) -> WalkForwardResult:
    """Run k disjoint OOS windows for a single strategy.

    `strategy_factory` is a zero-arg callable returning a fresh BaseStrategy
    instance — needed because strategies hold per-run state (price buffers,
    last-cross flags) that must NOT carry across folds. Pass `lambda: cls()`
    or use partials with config baked in.

    Returns a WalkForwardResult with aggregated OOS metrics. Aggregation:
      - oos_annual_return: mean of fold annual returns (each fold annualized
        from its own slice).
      - oos_max_drawdown: worst (most negative) drawdown across folds.
      - oos_sharpe: mean Sharpe across folds.
      - oos_consistency: 1 - |std/mean| of fold returns. Capped to [-1, 1].
        If mean is ~0, falls back to -|std| so a noisy zero-return strategy
        still gets penalized.
    """
    folds = make_folds(end, n_folds=n_folds,
                       warmup_days=warmup_days, test_days=test_days)
    fold_results: list[FoldResult] = []

    for fold in folds:
        strat = strategy_factory()
        try:
            bt = engine.run(strategy=strat, codes=codes,
                            start=fold.warmup_start, end=fold.test_end)
        except Exception as e:
            logger.warning(f"WF fold {fold.test_start}..{fold.test_end} failed: {e}")
            fold_results.append(FoldResult(fold=fold, metrics={},
                                           n_trades_oos=0))
            continue

        oos_curve = _slice_oos_curve(bt.equity_curve, fold)
        if len(oos_curve) < 5:  # too few OOS bars to score meaningfully
            fold_results.append(FoldResult(fold=fold, metrics={},
                                           n_trades_oos=0))
            continue
        m = compute_metrics(oos_curve)
        n_trades = sum(1 for t in bt.trades
                       if fold.test_start <= t.date <= fold.test_end)
        oos_rets = oos_curve.pct_change().dropna().tolist()
        fold_results.append(FoldResult(fold=fold, metrics=m,
                                       n_trades_oos=n_trades,
                                       oos_returns=oos_rets))

    return _aggregate(fold_results)


def _aggregate(fold_results: list[FoldResult]) -> WalkForwardResult:
    if not fold_results:
        return WalkForwardResult()
    rets = [r.metrics.get("annual_return", 0.0) or 0.0
            for r in fold_results if r.metrics]
    dds = [r.metrics.get("max_drawdown", 0.0) or 0.0
           for r in fold_results if r.metrics]

    if not rets:
        return WalkForwardResult(folds=fold_results)

    mean_ret = float(np.mean(rets))
    # Consistency: 1 - CoV of per-fold returns. Sample std (ddof=1) and require
    # ≥2 folds — std of a single fold is 0/undefined and would feign perfect
    # consistency. If mean is near zero (within 1% absolute), CoV explodes, so
    # fall back to a pure-noise penalty. Clamp final to [-1, 1].
    if len(rets) >= 2:
        std_ret = float(np.std(rets, ddof=1))
        if abs(mean_ret) < 0.01:
            consistency = max(-1.0, -std_ret)
        else:
            consistency = max(-1.0, min(1.0, 1.0 - std_ret / abs(mean_ret)))
    else:
        consistency = 0.0  # one fold → no cross-fold stability signal

    # Sharpe from POOLED OOS daily returns (one estimate over every fold's
    # observations), NOT a mean of noisy per-fold Sharpes.
    pooled: list[float] = []
    for r in fold_results:
        pooled.extend(r.oos_returns)
    pooled_sharpe = (annualized_sharpe(pooled, min_obs=_MIN_POOLED_OBS)
                     if len(pooled) >= _MIN_POOLED_OBS else 0.0)

    return WalkForwardResult(
        folds=fold_results,
        oos_annual_return=mean_ret,
        oos_max_drawdown=float(min(dds)) if dds else 0.0,
        oos_sharpe=pooled_sharpe,
        oos_consistency=consistency,
        total_trades_oos=sum(r.n_trades_oos for r in fold_results),
    )
