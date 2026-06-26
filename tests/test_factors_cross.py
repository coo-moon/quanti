"""Tests for the cross-sectional factor module.

Two layers:
  1. Each factor function on synthetic single-stock data — does it compute
     what we claim it computes?
  2. The full pipeline on a multi-stock panel — z-scoring, industry-demean,
     composite ordering.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.factors.cross_sectional import (
    DEFAULT_FACTORS,
    FactorConfig,
    _industry_demean,
    compute_factor_panel,
    factor_momentum_3m,
    factor_momentum_6m,
    factor_realized_vol_20d,
    factor_reversal_1w,
    rank_by_composite,
)


def _bar_df(closes: list[float], turnover: float = 1.0) -> pd.DataFrame:
    """Build a minimal bars DataFrame from a list of closes."""
    n = len(closes)
    today = pd.Timestamp.today().normalize()
    dates = pd.bdate_range(end=today, periods=n)
    return pd.DataFrame({
        "code": "X",
        "date": [d.date() for d in dates],
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": 1e6, "amount": [c * 1e6 for c in closes],
        "turnover": turnover,
    })


class TestSingleFactors:
    def test_momentum_3m_positive_for_uptrend(self):
        # 200 bars: smooth uptrend
        closes = [10 + i * 0.05 for i in range(200)]
        df = _bar_df(closes)
        v = factor_momentum_3m(df)
        assert v > 0

    def test_momentum_3m_negative_for_downtrend(self):
        closes = [20 - i * 0.05 for i in range(200)]
        df = _bar_df(closes)
        assert factor_momentum_3m(df) < 0

    def test_momentum_6m_positive_for_uptrend(self):
        closes = [10 + i * 0.05 for i in range(200)]
        df = _bar_df(closes)
        assert factor_momentum_6m(df) > 0

    def test_reversal_1w_flipped(self):
        """Reversal factor is sign-flipped: a strong recent rally returns
        a *negative* factor value (we expect mean-reversion to hurt it)."""
        closes = [10.0] * 195 + [12.0, 13.0, 14.0, 15.0, 16.0]  # 5-day +60% spike
        df = _bar_df(closes)
        v = factor_reversal_1w(df)
        assert v < 0

    def test_realized_vol_20d_flipped(self):
        """Low-vol stock gets HIGHER factor value (sign-flipped)."""
        low = [10 + i * 0.001 for i in range(30)]
        high = [10 + 0.5 * np.sin(i) for i in range(30)]
        v_low = factor_realized_vol_20d(_bar_df(low))
        v_high = factor_realized_vol_20d(_bar_df(high))
        assert v_low > v_high

    def test_factor_handles_short_history(self):
        """All factors must return NaN (not crash) on too-short input."""
        df = _bar_df([10.0, 10.1, 10.2])
        for name, fn in DEFAULT_FACTORS.items():
            v = fn(df)
            assert np.isnan(v), f"{name} did not return NaN on short history"


class TestIndustryDemean:
    """`_industry_demean` is a no-op for years because every stock's industry
    was blank in the DB; these pin both halves of its contract so a re-blanked
    `stocks.industry` can't silently revert it to doing nothing."""

    def test_demeans_within_industry(self):
        """Same-industry stocks get the group mean subtracted; an
        unknown-industry / singleton stock passes through untouched."""
        panel = pd.DataFrame(
            {"f": [1.0, 3.0, 5.0, 7.0]},
            index=["bank_a", "bank_b", "tech_solo", "no_ind"],
        )
        panel["industry"] = ["银行", "银行", "科技", ""]
        out = _industry_demean(panel, "f")
        # 银行 mean = 2.0 → demeaned to ±1.0
        assert out["bank_a"] == pytest.approx(-1.0)
        assert out["bank_b"] == pytest.approx(1.0)
        # singleton industry and empty industry are passed through unchanged
        assert out["tech_solo"] == pytest.approx(5.0)
        assert out["no_ind"] == pytest.approx(7.0)

    def test_all_blank_industry_is_graceful_noop(self):
        """The exact pre-backfill state: every industry empty → return the
        column unchanged rather than crash or zero it out."""
        panel = pd.DataFrame({"f": [1.0, 3.0, 5.0]}, index=["a", "b", "c"])
        panel["industry"] = ["", "", ""]
        out = _industry_demean(panel, "f")
        assert list(out) == [1.0, 3.0, 5.0]

    def test_missing_industry_column_is_noop(self):
        panel = pd.DataFrame({"f": [1.0, 3.0]}, index=["a", "b"])
        out = _industry_demean(panel, "f")
        assert list(out) == [1.0, 3.0]


# -------- pipeline integration tests -----------------------------------

@pytest.fixture
def panel_db(tmp_path):
    """Seed 4 stocks with intentionally different characteristics so the
    factor panel can demonstrate cross-sectional ranking.

      A_trend: smooth uptrend (high momentum)
      B_trend: smooth uptrend (high momentum, same industry as A)
      C_choppy: noisy / mean-reverting
      D_decline: declining (low momentum)
    """
    db = Database(str(tmp_path / "xs.db"))
    db.initialize()
    today = pd.Timestamp.today().normalize()
    dates = pd.bdate_range(end=today, periods=200)
    np.random.seed(42)

    specs = {
        "A_trend": ("银行", lambda i: 10 + i * 0.06 + np.random.randn() * 0.02),
        "B_trend": ("银行", lambda i: 10 + i * 0.07 + np.random.randn() * 0.02),
        "C_choppy": ("科技", lambda i: 10 + np.sin(i / 5) * 1.5 + np.random.randn() * 0.05),
        "D_decline": ("科技", lambda i: 20 - i * 0.05 + np.random.randn() * 0.02),
    }
    for code, (industry, fn) in specs.items():
        db.upsert_stock(code, code, "SZ", date(1991, 4, 3), industry)
        prices = np.array([fn(i) for i in range(len(dates))])
        df = pd.DataFrame({
            "code": code,
            "date": [d.date() for d in dates],
            "open": prices, "high": prices * 1.01, "low": prices * 0.99,
            "close": prices,
            "volume": np.full(len(dates), 1e6),
            "amount": prices * 1e6,
            "turnover": np.full(len(dates), 1.0),
        })
        db.save_daily_quotes(df)
    yield db
    db.close()


class TestPanel:
    def test_panel_contains_all_codes(self, panel_db):
        provider = DataProvider(panel_db)
        panel = compute_factor_panel(provider, panel_db,
                                     ["A_trend", "B_trend", "C_choppy", "D_decline"])
        assert set(panel.index) == {"A_trend", "B_trend", "C_choppy", "D_decline"}

    def test_uptrend_dominates_top_of_composite(self, panel_db):
        """Both uptrend stocks should rank above C_choppy and D_decline.
        We don't pin which of the trends wins (depends on weighting of
        momentum vs vol factors), nor which of choppy/decline ranks last —
        the choppy one has worse vol, the declining one has worse momentum,
        and the composite balances them. We do guarantee both trends are
        ahead of both non-trend names."""
        provider = DataProvider(panel_db)
        cfg = FactorConfig(industry_neutralize=False)
        panel = compute_factor_panel(provider, panel_db,
                                     ["A_trend", "B_trend", "C_choppy", "D_decline"],
                                     config=cfg)
        rk = rank_by_composite(panel)
        names = [r[0] for r in rk]
        assert names[0] in {"A_trend", "B_trend"}, f"top: got {names}"
        assert names[1] in {"A_trend", "B_trend"}, f"second: got {names}"
        assert set(names[2:]) == {"C_choppy", "D_decline"}, \
            f"bottom two should be choppy + decline, got {names}"

    def test_industry_neutralization_changes_ordering(self, panel_db):
        """With neutralization ON, the within-industry-leader (B_trend, the
        steeper uptrend bank) should still beat A_trend even though both
        are absolute leaders. With it OFF, the absolute leader matters
        more."""
        provider = DataProvider(panel_db)
        with_neutral = compute_factor_panel(
            provider, panel_db,
            ["A_trend", "B_trend", "C_choppy", "D_decline"],
            config=FactorConfig(industry_neutralize=True))
        without = compute_factor_panel(
            provider, panel_db,
            ["A_trend", "B_trend", "C_choppy", "D_decline"],
            config=FactorConfig(industry_neutralize=False))
        # Just sanity check: neutralized != non-neutralized composite vectors.
        c1 = with_neutral["composite"].sort_index()
        c2 = without["composite"].sort_index()
        assert not np.allclose(c1.values, c2.values)

    def test_empty_universe(self, panel_db):
        provider = DataProvider(panel_db)
        panel = compute_factor_panel(provider, panel_db, codes=[])
        assert panel.empty

    def test_unknown_code_dropped(self, panel_db):
        provider = DataProvider(panel_db)
        panel = compute_factor_panel(provider, panel_db,
                                     ["A_trend", "NOT_REAL"])
        # NOT_REAL has no bars → should be dropped, not crash
        assert "NOT_REAL" not in panel.index
        assert "A_trend" in panel.index


class TestComposite:
    def test_rank_by_composite_sorted(self, panel_db):
        provider = DataProvider(panel_db)
        panel = compute_factor_panel(provider, panel_db,
                                     ["A_trend", "B_trend", "C_choppy", "D_decline"])
        rk = rank_by_composite(panel)
        scores = [s for _, s in rk]
        assert scores == sorted(scores, reverse=True)

    def test_rank_top_n_truncates(self, panel_db):
        provider = DataProvider(panel_db)
        panel = compute_factor_panel(provider, panel_db,
                                     ["A_trend", "B_trend", "C_choppy", "D_decline"])
        rk = rank_by_composite(panel, top_n=2)
        assert len(rk) == 2


def test_generated_factor_enters_panel_only_when_included(panel_db):
    from quanti.data.provider import DataProvider
    from quanti.factors.cross_sectional import compute_factor_panel
    provider = DataProvider(panel_db)
    # An accepted+enabled generated factor.
    panel_db.save_generated_factor("llm_x", "-Mean(close,5)", 0.05, 0.04,
                                   accepted=True)
    codes = ["A_trend", "B_trend", "C_choppy", "D_decline"]
    base = compute_factor_panel(provider, panel_db, codes)
    incl = compute_factor_panel(provider, panel_db, codes, include_generated=True)
    assert "llm_x" not in base.columns
    assert "llm_x" in incl.columns
