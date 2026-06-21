"""The factor library: the production factors expressed in the DSL, plus the
adapter that exposes them under the existing FactorFn contract so the
cross-sectional pipeline is a drop-in.

Each factor is behavior-equivalent to the prior hand-written implementation
in cross_sectional.py (see tests/test_factor_library.py)."""

from __future__ import annotations

from typing import Callable

import pandas as pd

from quanti.factors.expr import (
    Close, EvalContext, Expr, Log, Mean, Ref, Std, Turnover,
)

_close = Close()

# 3-month momentum, skipping the most recent month (Jegadeesh-Titman):
# Ref(close,21)/Ref(close,63) - 1  ==  close[-22]/close[-64] - 1.
momentum_3m: Expr = Ref(_close, 21) / Ref(_close, 63) - 1
momentum_6m: Expr = Ref(_close, 21) / Ref(_close, 126) - 1
# Short-term reversal, sign-flipped.
reversal_1w: Expr = -(_close / Ref(_close, 5) - 1)
# Low-turnover anomaly, sign-flipped.
turnover_20d: Expr = -Mean(Turnover(), 20)
# Low-vol anomaly, sign-flipped: annualized std of 20 daily log returns.
realized_vol_20d: Expr = -Std(Log(_close / Ref(_close, 1)), 20) * (252 ** 0.5)

FACTOR_EXPRS: dict[str, Expr] = {
    "momentum_3m": momentum_3m,
    "momentum_6m": momentum_6m,
    "reversal_1w": reversal_1w,
    "turnover_20d": turnover_20d,
    "realized_vol_20d": realized_vol_20d,
}


def evaluate_series(expr: Expr, bars: pd.DataFrame) -> pd.Series:
    """Full date-indexed factor series (every bar gets a value). Batch use."""
    return expr.evaluate(EvalContext(bars))


def as_factor_fn(expr: Expr) -> Callable[[pd.DataFrame], float]:
    """Wrap an Expr into the existing FactorFn contract: evaluate on the bars
    and return the as-of (last bar) value as a float (NaN if uncomputable)."""
    def fn(bars: pd.DataFrame) -> float:
        s = expr.evaluate(EvalContext(bars))
        if len(s) == 0:
            return float("nan")
        return float(s.iloc[-1])
    return fn
