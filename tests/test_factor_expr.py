from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quanti.factors.expr import (
    Close, Constant, EvalContext, Field,
    Log, Max, Mean, Min, Ref, Std, Sum,
)


def _ctx(closes, volume=None):
    n = len(closes)
    dates = pd.bdate_range(end=pd.Timestamp("2025-01-01"), periods=n)
    df = pd.DataFrame({
        "date": [d.date() for d in dates],
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": volume if volume is not None else [1.0] * n,
        "turnover": [1.0] * n,
    })
    return EvalContext(df)


def test_field_returns_column_as_series():
    ctx = _ctx([10.0, 11.0, 12.0])
    s = Close().evaluate(ctx)
    assert list(s) == [10.0, 11.0, 12.0]


def test_missing_field_is_all_nan():
    df = pd.DataFrame({"date": [pd.Timestamp("2025-01-01").date()], "close": [1.0]})
    s = Field("turnover").evaluate(EvalContext(df))
    assert s.isna().all()


def test_arithmetic_and_scalar_wrapping():
    ctx = _ctx([10.0, 20.0])
    expr = Close() / 10.0 - 1          # scalar on right
    assert list(expr.evaluate(ctx)) == [0.0, 1.0]
    expr2 = 30.0 - Close()             # scalar on left (__rsub__)
    assert list(expr2.evaluate(ctx)) == [20.0, 10.0]
    expr3 = -Close()                   # unary neg
    assert list(expr3.evaluate(ctx)) == [-10.0, -20.0]


def test_divide_by_zero_is_nan_not_inf():
    ctx = _ctx([1.0, 2.0])
    s = (Constant(1.0) / (Close() - Close())).evaluate(ctx)  # /0
    assert s.isna().all()


def test_evalcontext_sorts_by_date():
    df = pd.DataFrame({
        "date": [pd.Timestamp("2025-01-03").date(), pd.Timestamp("2025-01-01").date()],
        "close": [13.0, 11.0], "open": [0, 0], "high": [0, 0], "low": [0, 0],
        "volume": [1, 1], "turnover": [1, 1]})
    s = Close().evaluate(EvalContext(df))
    assert list(s) == [11.0, 13.0]  # ascending by date


def test_ref_lags_by_n():
    ctx = _ctx([10.0, 11.0, 12.0, 13.0])
    s = Ref(Close(), 1).evaluate(ctx)
    assert np.isnan(s.iloc[0])
    assert list(s.iloc[1:]) == [10.0, 11.0, 12.0]


def test_ref_zero_is_identity():
    ctx = _ctx([10.0, 11.0])
    assert list(Ref(Close(), 0).evaluate(ctx)) == [10.0, 11.0]


def test_ref_negative_raises():
    with pytest.raises(ValueError):
        Ref(Close(), -1)


def test_rolling_ops():
    ctx = _ctx([2.0, 4.0, 6.0, 8.0])
    assert Mean(Close(), 2).evaluate(ctx).iloc[-1] == 7.0      # (6+8)/2
    assert Sum(Close(), 2).evaluate(ctx).iloc[-1] == 14.0
    assert Max(Close(), 3).evaluate(ctx).iloc[-1] == 8.0
    assert Min(Close(), 3).evaluate(ctx).iloc[-1] == 4.0
    # Std ddof=1 of [6,8] = std of sample = 1.41421...
    assert Std(Close(), 2).evaluate(ctx).iloc[-1] == pytest.approx(np.std([6.0, 8.0], ddof=1))


def test_rolling_insufficient_window_is_nan():
    ctx = _ctx([2.0, 4.0])
    assert np.isnan(Mean(Close(), 3).evaluate(ctx).iloc[-1])


def test_log():
    ctx = _ctx([1.0, np.e])
    s = Log(Close()).evaluate(ctx)
    assert s.iloc[0] == pytest.approx(0.0)
    assert s.iloc[1] == pytest.approx(1.0)


def test_no_lookahead_appending_future_bars_does_not_change_past():
    """The structural guarantee: a factor's value at date t is invariant to
    any bars added AFTER t."""
    closes = [10.0, 11.0, 9.0, 12.0, 13.0, 11.0, 14.0, 15.0]
    expr = Mean(Close(), 3) / Ref(Close(), 1) - 1
    base = _ctx(closes)
    s_base = expr.evaluate(base)
    extended = _ctx(closes + [99.0, 1.0, 50.0])  # wild future bars
    s_ext = expr.evaluate(extended)
    # The first len(closes) positions must be identical.
    assert np.allclose(s_base.values, s_ext.values[: len(closes)], equal_nan=True)
