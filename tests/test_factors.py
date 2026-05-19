"""Tests for factor engine."""

import pandas as pd
import numpy as np
import pytest

from quanti.factors.registry import FactorRegistry, register_factor
from quanti.factors.technical import compute_ma, compute_rsi, compute_macd


@pytest.fixture
def sample_df():
    """50 days of synthetic price data."""
    np.random.seed(42)
    n = 50
    close = 10 + np.cumsum(np.random.randn(n) * 0.1)
    return pd.DataFrame(
        {
            "close": close,
            "high": close + np.random.rand(n) * 0.5,
            "low": close - np.random.rand(n) * 0.5,
            "volume": np.random.randint(100000, 1000000, n).astype(float),
        }
    )


class TestFactorRegistry:
    def test_register_and_list(self):
        registry = FactorRegistry()

        @registry.register("test_factor")
        def test_factor(df: pd.DataFrame) -> pd.Series:
            return df["close"].pct_change()

        assert "test_factor" in registry.list_factors()

    def test_compute_factor(self, sample_df):
        registry = FactorRegistry()

        @registry.register("returns")
        def returns(df: pd.DataFrame) -> pd.Series:
            return df["close"].pct_change()

        result = registry.compute("returns", sample_df)
        assert isinstance(result, pd.Series)
        assert len(result) == len(sample_df)

    def test_compute_unknown_factor_raises(self, sample_df):
        registry = FactorRegistry()
        with pytest.raises(KeyError):
            registry.compute("nonexistent", sample_df)


class TestTechnicalFactors:
    def test_ma(self, sample_df):
        result = compute_ma(sample_df, period=5)
        assert len(result) == len(sample_df)
        assert pd.isna(result.iloc[3])  # Not enough data yet
        assert not pd.isna(result.iloc[4])  # 5th value should exist

    def test_rsi(self, sample_df):
        result = compute_rsi(sample_df, period=14)
        assert len(result) == len(sample_df)
        # RSI values should be between 0 and 100 where they exist
        valid = result.dropna()
        assert (valid >= 0).all() and (valid <= 100).all()

    def test_macd(self, sample_df):
        macd_line, signal_line, histogram = compute_macd(sample_df)
        assert len(macd_line) == len(sample_df)
        assert len(signal_line) == len(sample_df)
        assert len(histogram) == len(sample_df)
