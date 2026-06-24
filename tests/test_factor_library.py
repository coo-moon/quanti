# tests/test_factor_library.py
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quanti.factors.library import (
    FACTOR_EXPRS, as_factor_fn, evaluate_series, momentum_6m,
)


def _bars(closes, turnover=1.0):
    n = len(closes)
    dates = pd.bdate_range(end=pd.Timestamp("2025-01-01"), periods=n)
    return pd.DataFrame({
        "date": [d.date() for d in dates],
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1e6] * n, "turnover": [turnover] * n,
    })


# --- reference implementations (the OLD formulas) to prove equivalence ---
def _cum_return(closes, start_offset, end_offset):
    if len(closes) < start_offset + 1:
        return float("nan")
    p_start = closes.iloc[-start_offset - 1]
    p_end = closes.iloc[-end_offset - 1]
    if p_start <= 0:
        return float("nan")
    return float(p_end / p_start - 1.0)


def _ref(name, bars):
    c = bars["close"]
    if name == "momentum_3m":
        return _cum_return(c, 63, 21)
    if name == "momentum_6m":
        return _cum_return(c, 126, 21)
    if name == "reversal_1w":
        r = _cum_return(c, 5, 0)
        return -r if not np.isnan(r) else r
    if name == "turnover_20d":
        return float("nan") if len(bars) < 20 else -float(bars["turnover"].iloc[-20:].mean())
    if name == "realized_vol_20d":
        if len(c) < 21:
            return float("nan")
        last = c.iloc[-21:]
        rets = np.log(last / last.shift(1)).dropna()
        if len(rets) < 2:
            return float("nan")
        return -float(rets.std() * np.sqrt(252))
    # Fundamental factors read columns absent from these price-only test bars →
    # the DSL yields NaN; reference matches.
    if name in ("value_ep", "value_bp", "value_sp", "dividend_yield", "size",
                "quality_roe", "growth_earnings", "growth_revenue"):
        return float("nan")
    raise KeyError(name)


def test_each_factor_matches_reference():
    rng = np.random.default_rng(7)
    closes = list(10 + np.cumsum(rng.normal(0, 0.2, 200)))
    bars = _bars([abs(c) + 1 for c in closes], turnover=2.3)
    for name, expr in FACTOR_EXPRS.items():
        got = as_factor_fn(expr)(bars)
        exp = _ref(name, bars)
        assert (np.isnan(got) and np.isnan(exp)) or got == pytest.approx(exp, rel=1e-9), \
            f"{name}: dsl={got} ref={exp}"


def test_as_factor_fn_short_history_is_nan():
    bars = _bars([10.0, 10.1, 10.2])
    for expr in FACTOR_EXPRS.values():
        assert np.isnan(as_factor_fn(expr)(bars))


def test_evaluate_series_returns_full_series():
    bars = _bars(list(range(2, 60)))
    s = evaluate_series(momentum_6m, bars)
    assert len(s) == len(bars)


def test_fundamental_factors_signs_and_values():
    """Fundamental factors read merged columns, sign-flipped to higher=better."""
    from quanti.factors.library import (
        dividend_yield, growth_earnings, quality_roe, size, value_bp, value_ep,
    )
    bars = _bars([10.0] * 30)
    bars["pe_ttm"] = 20.0      # earnings yield 1/20
    bars["pb"] = 2.0           # book/price 1/2
    bars["dv_ratio"] = 3.5
    bars["total_mv"] = np.e ** 4   # -log → -4
    bars["roe"] = 15.0
    bars["netprofit_yoy"] = 25.0
    assert as_factor_fn(value_ep)(bars) == pytest.approx(0.05)
    assert as_factor_fn(value_bp)(bars) == pytest.approx(0.5)
    assert as_factor_fn(dividend_yield)(bars) == pytest.approx(3.5)
    assert as_factor_fn(size)(bars) == pytest.approx(-4.0)
    assert as_factor_fn(quality_roe)(bars) == pytest.approx(15.0)
    assert as_factor_fn(growth_earnings)(bars) == pytest.approx(25.0)
    # Loss-maker (pe<0) scores LOW via 1/pe, not high like -pe would.
    bars["pe_ttm"] = -10.0
    assert as_factor_fn(value_ep)(bars) < 0
