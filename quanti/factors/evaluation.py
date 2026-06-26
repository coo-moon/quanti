"""Factor evaluation: cross-sectional rank-IC (information coefficient).

IC measures whether a factor's value at t predicts the forward return t→t+N.
It is a research metric computed on history (the future is known there), so it
legitimately uses forward returns; the factor itself remains ②-look-ahead-safe.
rank-IC = Pearson correlation of cross-sectional ranks (Spearman), computed
with pandas/numpy (no scipy).

`factor_ic` returns the mean IC. `factor_ic_stats` additionally returns a
Newey-West (HAC) t-stat + sample size so the factor-mining gate can run a
proper multiple-testing correction instead of accepting on raw IC."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from quanti.factors.expr import Expr
from quanti.factors.library import evaluate_series

logger = logging.getLogger(__name__)


def rank_ic(factor_vals: dict[str, float], fwd_rets: dict[str, float],
            min_names: int = 5) -> float:
    """Cross-sectional rank IC for one date. NaN if < min_names paired names."""
    codes = [c for c in factor_vals
             if c in fwd_rets
             and not pd.isna(factor_vals[c]) and not pd.isna(fwd_rets[c])]
    if len(codes) < min_names:
        return float("nan")
    f = pd.Series([factor_vals[c] for c in codes]).rank()
    r = pd.Series([fwd_rets[c] for c in codes]).rank()
    if f.std() == 0 or r.std() == 0:
        return float("nan")
    return float(np.corrcoef(f, r)[0, 1])


def _ic_series(expr: Expr, provider, codes: list[str], start: date, end: date,
               *, fwd_days: int, lookback_days: int, min_names: int,
               with_fundamentals: bool) -> list[float]:
    """Per-date cross-sectional rank-IC list over the trading dates in
    [start, end] — the shared core of `factor_ic` and `factor_ic_stats`.

    For each code, evaluate the factor series (② batch) and the forward return
    series once, then assemble each date's cross-section.

    `with_fundamentals=True` merges point-in-time pe/pb/roe/... onto each code's
    bars (via cross_sectional._merge_fundamentals) so fundamental factor
    candidates can actually score — without it they read all-NaN and the gate
    drops them. The merge is PIT-safe (financials via merge_asof on ann_date)."""
    _merge = None
    if with_fundamentals:
        from quanti.factors.cross_sectional import _merge_fundamentals as _merge
    fac_by_code: dict[str, pd.Series] = {}
    fwd_by_code: dict[str, pd.Series] = {}
    fetch_start = start - timedelta(days=lookback_days)
    fetch_end = end + timedelta(days=fwd_days * 3 + 7)  # room for forward return
    for code in codes:
        bars = provider.get_daily_df(code, fetch_start, fetch_end)
        if bars is None or bars.empty or len(bars) < 2:
            continue
        bars = bars.sort_values("date")
        if _merge is not None:
            # re-sort: merge_asof returns date-asc, but keep it explicit so the
            # forward-return shift below is correct regardless of merge path.
            bars = _merge(bars, provider, code, fetch_start, fetch_end).sort_values("date")
        s = evaluate_series(expr, bars)              # date-indexed factor
        closes = bars.set_index("date")["close"].astype(float)
        fwd = closes.shift(-fwd_days) / closes - 1.0  # forward return (research)
        fac_by_code[code] = s
        fwd_by_code[code] = fwd

    if not fac_by_code:
        return []

    all_dates = sorted({d for s in fac_by_code.values() for d in s.index
                        if start <= d <= end})
    ics: list[float] = []
    for d in all_dates:
        fvals = {c: fac_by_code[c].get(d) for c in fac_by_code if d in fac_by_code[c].index}
        rvals = {c: fwd_by_code[c].get(d) for c in fwd_by_code if d in fwd_by_code[c].index}
        ic = rank_ic(fvals, rvals, min_names=min_names)
        if not np.isnan(ic):
            ics.append(ic)
    return ics


def factor_ic(expr: Expr, provider, codes: list[str], start: date, end: date,
              *, fwd_days: int = 5, lookback_days: int = 200,
              min_names: int = 5, with_fundamentals: bool = False) -> float:
    """Mean cross-sectional rank-IC over [start, end]. NaN if no scorable dates."""
    ics = _ic_series(expr, provider, codes, start, end, fwd_days=fwd_days,
                     lookback_days=lookback_days, min_names=min_names,
                     with_fundamentals=with_fundamentals)
    return float(np.mean(ics)) if ics else float("nan")


@dataclass
class ICStats:
    """OOS IC summary for significance testing. `t_stat` is the IC information
    ratio with a Newey-West (Bartlett) HAC correction for the autocorrelation
    induced by overlapping `fwd_days` forward-return windows; `n` = number of
    scorable cross-section dates. NaN t_stat / n=0 means "not testable"."""
    mean_ic: float
    t_stat: float
    n: int


def factor_ic_stats(expr: Expr, provider, codes: list[str], start: date,
                    end: date, *, fwd_days: int = 5, lookback_days: int = 200,
                    min_names: int = 5, with_fundamentals: bool = False,
                    nw_lag: int | None = None) -> ICStats:
    """Mean IC + HAC t-stat + sample size, for multiple-testing-aware gating.

    Same one-pass heavy computation as `factor_ic`, plus the t-stat, so the
    miner can get a p-value without a second evaluation. The Newey-West lag
    defaults to `fwd_days - 1` (the overlap length of the forward-return
    windows) — without it the daily IC series' autocorrelation inflates the
    t-stat and manufactures significance."""
    ics = _ic_series(expr, provider, codes, start, end, fwd_days=fwd_days,
                     lookback_days=lookback_days, min_names=min_names,
                     with_fundamentals=with_fundamentals)
    if not ics:
        return ICStats(float("nan"), float("nan"), 0)
    lag = nw_lag if nw_lag is not None else max(0, fwd_days - 1)
    return ICStats(float(np.mean(ics)), _nw_tstat(ics, lag), len(ics))


def _nw_tstat(ics: list[float], lag: int) -> float:
    """t-stat of mean(ics) using a Newey-West Bartlett-kernel HAC variance.

    lag=0 reduces to the iid t = mean·√n / sd. With lag>0 it adds the
    Bartlett-weighted autocovariances so overlapping forward-return windows
    don't understate the standard error (Lo 2002 applied to the IC series)."""
    n = len(ics)
    if n < 2:
        return float("nan")
    x = np.asarray(ics, dtype=float)
    xc = x - x.mean()
    g0 = float(xc @ xc) / n                       # variance (population)
    # A (near-)constant series is untestable. Use a real tolerance, not >0:
    # float noise leaves g0 ~1e-36 for a constant series, which would otherwise
    # produce a spurious astronomical t-stat (p≈0) and FALSELY pass the gate.
    # Real rank-IC series have variance >> 1e-12.
    if g0 <= 1e-12:
        return float("nan")
    s = g0
    L = max(0, min(lag, n - 1))
    for k in range(1, L + 1):
        gk = float(xc[k:] @ xc[:-k]) / n          # autocovariance at lag k
        s += 2.0 * (1.0 - k / (L + 1)) * gk        # Bartlett weight
    if s <= 0:
        return float("nan")
    se = float(np.sqrt(s / n))                    # SE of the mean
    return float(x.mean() / se) if se > 0 else float("nan")
