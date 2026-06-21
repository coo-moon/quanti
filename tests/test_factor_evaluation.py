# tests/test_factor_evaluation.py
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from quanti.factors.evaluation import factor_ic, rank_ic
from quanti.factors.expr import Close, Ref


def test_rank_ic_perfect_and_zero():
    # factor ranks exactly match forward-return ranks → IC = 1.
    fac = {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0, "e": 5.0}
    fwd = {"a": 0.1, "b": 0.2, "c": 0.3, "d": 0.4, "e": 0.5}
    assert rank_ic(fac, fwd) == pytest.approx(1.0)
    # reversed → -1.
    assert rank_ic(fac, {"a": 0.5, "b": 0.4, "c": 0.3, "d": 0.2, "e": 0.1}) == pytest.approx(-1.0)


def test_rank_ic_too_few_names_is_nan():
    assert np.isnan(rank_ic({"a": 1.0}, {"a": 0.1}))


class _Provider:
    """Returns per-code synthetic bars where momentum predicts forward return
    for the 'good' codes (so a momentum factor has positive IC)."""
    def __init__(self, data):  # data: code -> list[float] closes
        self._data = data
        n = max(len(v) for v in data.values())
        self._dates = [d.date() for d in pd.bdate_range(end=pd.Timestamp("2025-06-01"), periods=n)]

    def get_daily_df(self, code, start, end):
        closes = self._data.get(code, [])
        df = pd.DataFrame({"date": self._dates[:len(closes)],
                           "open": closes, "high": closes, "low": closes,
                           "close": closes, "volume": [1.0]*len(closes),
                           "turnover": [1.0]*len(closes)})
        return df[(df["date"] >= start) & (df["date"] <= end)]


def test_factor_ic_positive_for_predictive_factor():
    # Build 6 codes; trending-up codes keep trending (momentum predictive).
    rng = np.random.default_rng(0)
    data = {}
    for i in range(6):
        drift = 0.5 if i < 3 else -0.5
        data[f"c{i}"] = list(100 + np.cumsum(np.full(150, drift) + rng.normal(0, 0.1, 150)))
    prov = _Provider(data)
    expr = Ref(Close(), 1) / Ref(Close(), 21) - 1   # ~1m momentum
    ic = factor_ic(expr, prov, list(data), date(2025, 1, 1), date(2025, 5, 1),
                   fwd_days=5, min_names=4)
    assert ic > 0
