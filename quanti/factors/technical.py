"""Built-in technical analysis factors."""

from __future__ import annotations

import pandas as pd
import numpy as np


def compute_ma(df: pd.DataFrame, period: int = 20, col: str = "close") -> pd.Series:
    """Simple Moving Average."""
    return df[col].rolling(window=period).mean()


def compute_ema(df: pd.DataFrame, period: int = 20, col: str = "close") -> pd.Series:
    """Exponential Moving Average."""
    return df[col].ewm(span=period, adjust=False).mean()


def compute_rsi(df: pd.DataFrame, period: int = 14, col: str = "close") -> pd.Series:
    """Relative Strength Index."""
    delta = df[col].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def compute_macd(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    col: str = "close",
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD indicator. Returns (macd_line, signal_line, histogram)."""
    ema_fast = df[col].ewm(span=fast, adjust=False).mean()
    ema_slow = df[col].ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def compute_bollinger(
    df: pd.DataFrame, period: int = 20, std_dev: float = 2.0, col: str = "close"
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands. Returns (upper, middle, lower)."""
    middle = df[col].rolling(window=period).mean()
    std = df[col].rolling(window=period).std()
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return upper, middle, lower


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift(1)).abs()
    low_close = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index (Wilder). Trend-STRENGTH, direction-agnostic.

    ADX >= 25 → trending; < 20 → ranging/choppy; 20-25 transitional. Needs
    high/low/close. Uses Wilder smoothing (RMA ≈ ewm alpha=1/period). Returns
    a Series aligned to df; leading values are NaN until enough history.
    """
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)

    rma = lambda s: s.ewm(alpha=1.0 / period, adjust=False).mean()  # noqa: E731
    atr = rma(tr).replace(0, np.nan)
    plus_di = 100.0 * rma(plus_dm) / atr
    minus_di = 100.0 * rma(minus_dm) / atr
    denom = (plus_di + minus_di).replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / denom
    return rma(dx)
