# tests/test_factor_parser.py
from __future__ import annotations

import pandas as pd
import pytest

from quanti.factors.expr import EvalContext
from quanti.factors.parser import FactorParseError, parse_expr


def _ctx(closes):
    n = len(closes)
    dates = pd.bdate_range(end=pd.Timestamp("2025-01-01"), periods=n)
    return EvalContext(pd.DataFrame({
        "date": [d.date() for d in dates],
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1.0] * n, "turnover": [1.0] * n}))


def test_parses_valid_expression_equivalently():
    expr = parse_expr("Ref(close, 21) / Ref(close, 63) - 1")
    ctx = _ctx([float(i) + 1 for i in range(80)])
    from quanti.factors.expr import Close, Ref
    ref = Ref(Close(), 21) / Ref(Close(), 63) - 1
    assert expr.evaluate(ctx).equals(ref.evaluate(ctx))


def test_parses_nested_funcs_and_neg_and_scalars():
    expr = parse_expr("-Std(Log(close / Ref(close, 1)), 20) * 16")
    ctx = _ctx([float(i) + 1 for i in range(40)])
    s = expr.evaluate(ctx)
    assert len(s) == 40  # evaluates without error


@pytest.mark.parametrize("bad", [
    "__import__('os').system('rm -rf /')",
    "os.system('x')",
    "close.__class__",
    "open(close)",            # 'open' is a field name, not callable
    "Foo(close, 5)",          # unknown function
    "Ref(close, -1)",         # negative window
    "Ref(close, 5.5)",        # non-int window
    "Ref(close, n)",          # non-constant window
    "[close for x in close]", # comprehension
    "close ** 2",             # power operator not allowed
    "Mean(close, 5, 6)",      # wrong arity
    "Ref(close, n=5)",        # kwargs
    "price",                  # unknown name
])
def test_rejects_unsafe_or_unknown(bad):
    with pytest.raises(FactorParseError):
        parse_expr(bad)


def test_rejects_overlong():
    with pytest.raises(FactorParseError):
        parse_expr("close+" * 500 + "close")
