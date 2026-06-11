"""Market regime detection (v1, observe-only).

Classifies the current market into {trend_up, trend_down, range(震荡),
high_vol} so the agent can later tilt screener/strategy/factor/sizing to fit.
v1 only *observes* (logs a `regime` decision); it does NOT change behavior.

Design — deterministic, causal (no lookahead), zero new data dependency:
  * Market series = an equal-weight synthetic index built from the universe's
    ALREADY-SYNCED closes (no external index fetch — robust to data outages).
  * Trend vs chop = Efficiency Ratio (Kaufman): |net move| / Σ|daily moves|
    over a window. ~1 = clean trend, ~0 = choppy. A more direct trend/range
    discriminator than ADX, and needs only closes. (ADX is available in
    factors.technical.compute_adx for when a clean index OHLC source is wired.)
  * Volatility = 20d realized vol, ranked as a percentile vs the trailing year.
  * Breadth = % of the universe above its MA20 (A-share-friendly confirmation).
  * Hysteresis: in the ER transition zone we hold the previous regime (or break
    the tie with breadth) so the label doesn't whipsaw tick-to-tick.

Never raises into the agent cycle — any failure yields label="unknown".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from quanti.data.provider import DataProvider

logger = logging.getLogger(__name__)

VALID = {"trend_up", "trend_down", "range", "high_vol"}
_ZH = {
    "trend_up": "趋势上行", "trend_down": "趋势下行",
    "range": "震荡", "high_vol": "高波动", "unknown": "未知",
}


@dataclass
class RegimeConfig:
    er_window: int = 20          # Efficiency Ratio lookback
    er_trend: float = 0.50       # ER >= this → trending
    er_range: float = 0.30       # ER < this → ranging/choppy
    vol_window: int = 20         # realized-vol window
    vol_lookback: int = 252      # percentile reference (~1y)
    vol_hi_pct: float = 0.80     # vol percentile >= this → high_vol (overrides)
    breadth_ma: int = 20
    sample: int = 120            # cap stocks used for index + breadth (cost)
    min_stocks: int = 20         # below this, give up (unknown)
    lookback_days: int = 400


@dataclass
class RegimeState:
    label: str
    er: float | None = None
    slope: float = 0.0
    vol_pct: float | None = None
    breadth: float | None = None
    n_obs: int = 0
    asof: date | None = None
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "er": round(self.er, 3) if self.er is not None else None,
            "slope_sign": int(np.sign(self.slope)),
            "vol_pct": round(self.vol_pct, 3) if self.vol_pct is not None else None,
            "breadth": round(self.breadth, 3) if self.breadth is not None else None,
            "n_obs": self.n_obs,
            "asof": self.asof.isoformat() if self.asof else None,
            "note": self.note,
        }

    def summary(self) -> str:
        bits = [f"{_ZH.get(self.label, self.label)} ({self.label})"]
        if self.er is not None:
            bits.append(f"ER={self.er:.2f}")
        if self.vol_pct is not None:
            bits.append(f"vol_pct={self.vol_pct:.2f}")
        if self.breadth is not None:
            bits.append(f"breadth={self.breadth:.2f}")
        return " | ".join(bits)


# ----------------------------------------------------------- metrics

def efficiency_ratio(closes: pd.Series, window: int) -> float | None:
    seg = closes.dropna().iloc[-(window + 1):]
    if len(seg) < window + 1:
        return None
    net = abs(float(seg.iloc[-1]) - float(seg.iloc[0]))
    path = float(seg.diff().abs().sum())
    return (net / path) if path > 0 else 0.0


def realized_vol_pct(closes: pd.Series, window: int, lookback: int) -> float | None:
    rv = closes.pct_change().rolling(window).std()
    cur = rv.iloc[-1] if len(rv) else np.nan
    if pd.isna(cur):
        return None
    hist = rv.iloc[-lookback:].dropna()
    if len(hist) < 20:
        return None
    return float((hist <= cur).mean())


def classify_regime(er, slope, vol_pct, breadth, prev: str,
                    cfg: RegimeConfig) -> str:
    # Volatility spike dominates — de-risk regardless of trend.
    if vol_pct is not None and vol_pct >= cfg.vol_hi_pct:
        return "high_vol"
    if er is None:
        return prev if prev in VALID else "unknown"
    if er >= cfg.er_trend:
        return "trend_up" if slope >= 0 else "trend_down"
    if er < cfg.er_range:
        return "range"
    # Transition zone [er_range, er_trend): break the tie with breadth,
    # otherwise hold the previous regime (hysteresis → no whipsaw).
    if breadth is not None:
        if breadth >= 0.65:
            return "trend_up"
        if breadth <= 0.35:
            return "trend_down"
    return prev if prev in VALID else "range"


# ----------------------------------------------------------- market series

def _load_sample_panel(provider: DataProvider, codes: list[str],
                       as_of: date, cfg: RegimeConfig) -> pd.DataFrame | None:
    start = as_of - timedelta(days=cfg.lookback_days)
    series: dict[str, pd.Series] = {}
    for code in codes[: cfg.sample]:
        df = provider.get_daily_df(code, start, as_of)
        if df is None or df.empty or "close" not in df.columns:
            continue
        s = df.sort_values("date").set_index("date")["close"]
        if len(s) >= cfg.er_window + 5:
            series[code] = s
    if not series:
        return None
    return pd.DataFrame(series)


def equal_weight_index(panel: pd.DataFrame) -> pd.Series:
    """Equal-weight cumulative-return index from a close panel (per-date mean
    of available stocks' daily returns)."""
    mkt_ret = panel.pct_change().mean(axis=1)
    return (1.0 + mkt_ret.fillna(0.0)).cumprod()


def breadth_above_ma(panel: pd.DataFrame, ma: int) -> float | None:
    # Forward-fill first: codes have ragged last-traded dates, so the union's
    # final row is sparse. ffill carries each code's last known close to the
    # common last date, so breadth is measured over all covered names rather
    # than the few that happened to trade on the very last calendar day.
    p = panel.ffill()
    ma_s = p.rolling(ma).mean()
    last_close = p.iloc[-1]
    last_ma = ma_s.iloc[-1]
    valid = last_close.notna() & last_ma.notna()
    if int(valid.sum()) == 0:
        return None
    return float((last_close[valid] > last_ma[valid]).mean())


def last_regime_label(db) -> str:
    try:
        rows = db.list_decisions(limit=1, kind="regime")
        if rows:
            return str((rows[0].get("details") or {}).get("label", "unknown")) or "unknown"
    except Exception:
        pass
    return "unknown"


# ----------------------------------------------------------- orchestrator

def detect_regime(
    provider: DataProvider,
    as_of: date,
    *,
    universe: list[str] | None = None,
    cfg: RegimeConfig | None = None,
    prev_label: str = "unknown",
    market_series_fn=None,
) -> RegimeState:
    """Classify the current regime. `market_series_fn`, when given, returns the
    market close Series directly (tests / a future index source); otherwise a
    synthetic equal-weight index is built from `universe`. Never raises."""
    cfg = cfg or RegimeConfig()
    try:
        breadth = None
        if market_series_fn is not None:
            closes = market_series_fn()
        else:
            panel = _load_sample_panel(provider, list(universe or []), as_of, cfg)
            if panel is None or panel.shape[1] < cfg.min_stocks:
                return RegimeState("unknown", asof=as_of,
                                   note="universe too small for regime")
            closes = equal_weight_index(panel)
            breadth = breadth_above_ma(panel, cfg.breadth_ma)

        if closes is None or len(closes.dropna()) < cfg.er_window + 5:
            return RegimeState("unknown", asof=as_of, note="insufficient history")

        seg = closes.dropna()
        er = efficiency_ratio(closes, cfg.er_window)
        slope = (float(seg.iloc[-1] - seg.iloc[-1 - cfg.er_window])
                 if len(seg) > cfg.er_window else 0.0)
        vol_pct = realized_vol_pct(closes, cfg.vol_window, cfg.vol_lookback)
        label = classify_regime(er, slope, vol_pct, breadth, prev_label, cfg)
        return RegimeState(label=label, er=er, slope=slope, vol_pct=vol_pct,
                           breadth=breadth, n_obs=len(seg), asof=as_of)
    except Exception as e:
        logger.warning("regime detect failed: %s", e)
        return RegimeState("unknown", asof=as_of, note=str(e)[:80])
