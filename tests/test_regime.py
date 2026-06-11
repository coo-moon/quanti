"""Tests for v1 regime detection (deterministic, offline)."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from quanti.agent.regime import (
    RegimeConfig,
    breadth_above_ma,
    classify_regime,
    detect_regime,
    efficiency_ratio,
    equal_weight_index,
)
from quanti.factors.technical import compute_adx

CFG = RegimeConfig()


# ----- compute_adx (library) ---------------------------------------------

def _ohlc(closes: np.ndarray, band: float = 0.005) -> pd.DataFrame:
    return pd.DataFrame({"high": closes * (1 + band),
                         "low": closes * (1 - band),
                         "close": closes})


class TestADX:
    def test_trend_higher_than_chop(self):
        trend = compute_adx(_ohlc(np.linspace(10, 25, 150)))
        chop = compute_adx(_ohlc(10 + np.sin(np.arange(150) * 0.6)))
        assert trend.iloc[-1] > chop.iloc[-1]
        assert trend.iloc[-1] > 40       # clean trend → very high ADX
        assert chop.iloc[-1] < 40


# ----- efficiency ratio --------------------------------------------------

class TestEfficiencyRatio:
    def test_clean_trend_near_one(self):
        er = efficiency_ratio(pd.Series(np.linspace(10, 20, 60)), 20)
        assert er > 0.95

    def test_chop_near_zero(self):
        er = efficiency_ratio(pd.Series(10 + np.sin(np.arange(60) * 0.7)), 20)
        assert er < 0.3

    def test_too_short_is_none(self):
        assert efficiency_ratio(pd.Series([1, 2, 3]), 20) is None


# ----- classifier (pure) -------------------------------------------------

class TestClassify:
    def test_trend_up_and_down(self):
        assert classify_regime(0.7, +1, 0.5, None, "unknown", CFG) == "trend_up"
        assert classify_regime(0.7, -1, 0.5, None, "unknown", CFG) == "trend_down"

    def test_range(self):
        assert classify_regime(0.1, 0, 0.5, None, "unknown", CFG) == "range"

    def test_high_vol_overrides_even_strong_trend(self):
        assert classify_regime(0.9, +1, 0.85, None, "unknown", CFG) == "high_vol"

    def test_transition_holds_previous(self):
        # ER in [0.30, 0.50) with no breadth → hysteresis: keep prev.
        assert classify_regime(0.4, +1, 0.5, None, "trend_up", CFG) == "trend_up"
        assert classify_regime(0.4, +1, 0.5, None, "unknown", CFG) == "range"

    def test_transition_breadth_tiebreak(self):
        assert classify_regime(0.4, 0, 0.5, 0.70, "unknown", CFG) == "trend_up"
        assert classify_regime(0.4, 0, 0.5, 0.20, "unknown", CFG) == "trend_down"


# ----- detect_regime via injected market series --------------------------

class TestDetectInjected:
    def test_trend_up(self):
        rs = detect_regime(None, date(2026, 6, 10),
                           market_series_fn=lambda: pd.Series(np.linspace(10, 20, 150)))
        assert rs.label == "trend_up"
        assert rs.er > 0.9

    def test_range(self):
        rs = detect_regime(None, date(2026, 6, 10),
                           market_series_fn=lambda: pd.Series(10 + np.sin(np.arange(200) * 0.6)))
        assert rs.label == "range"

    def test_high_vol(self):
        rets = np.concatenate([np.full(240, 0.001), np.tile([0.06, -0.06], 10)])
        closes = pd.Series(100 * np.cumprod(1 + rets))
        rs = detect_regime(None, date(2026, 6, 10), market_series_fn=lambda: closes)
        assert rs.label == "high_vol"

    def test_short_series_unknown(self):
        rs = detect_regime(None, date(2026, 6, 10),
                           market_series_fn=lambda: pd.Series([1, 2, 3]))
        assert rs.label == "unknown"

    def test_fn_raises_degrades_to_unknown(self):
        def boom():
            raise RuntimeError("market source down")
        rs = detect_regime(None, date(2026, 6, 10), market_series_fn=boom)
        assert rs.label == "unknown"


# ----- panel helpers + universe path -------------------------------------

class FakeProvider:
    """Returns a date/close DataFrame per code from canned data."""

    def __init__(self, panel_closes: dict[str, list]):
        self._dates = [date(2026, 1, 1) + timedelta(days=i)
                       for i in range(len(next(iter(panel_closes.values()))))]
        self._data = panel_closes

    def get_daily_df(self, code, start, end):
        closes = self._data.get(code, [])
        return pd.DataFrame({"date": self._dates, "close": closes})


class TestPanelAndUniversePath:
    def test_equal_weight_index_and_breadth(self):
        panel = pd.DataFrame({"A": [10, 11, 12, 13, 14],
                              "B": [20, 19, 21, 22, 23]})
        idx = equal_weight_index(panel)
        assert len(idx) == 5 and idx.iloc[-1] > idx.iloc[0]   # net up
        b = breadth_above_ma(panel, 2)
        assert b is not None and 0.0 <= b <= 1.0

    def test_universe_path_builds_synthetic_index(self):
        # 25 codes, 80 rising bars each → trend_up, breadth computed.
        base = np.linspace(10, 18, 80)
        codes = {f"60{i:04d}": list(base + i * 0.01) for i in range(25)}
        provider = FakeProvider(codes)
        rs = detect_regime(provider, date(2026, 6, 10),
                           universe=list(codes.keys()))
        assert rs.label in {"trend_up", "trend_down", "range", "high_vol"}
        assert rs.breadth is not None           # universe path computes breadth
        assert rs.n_obs >= 50

    def test_universe_too_small_unknown(self):
        provider = FakeProvider({"600001": list(np.linspace(10, 12, 40))})
        rs = detect_regime(provider, date(2026, 6, 10), universe=["600001"])
        assert rs.label == "unknown"   # below min_stocks
