from __future__ import annotations

import pandas as pd

from quanti.factors.expr import (
    Close, Constant, EvalContext, Field,
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
