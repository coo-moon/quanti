"""Factor evaluation: cross-sectional rank-IC (information coefficient).

IC measures whether a factor's value at t predicts the forward return t→t+N.
It is a research metric computed on history (the future is known there), so it
legitimately uses forward returns; the factor itself remains ②-look-ahead-safe.
rank-IC = Pearson correlation of cross-sectional ranks (Spearman), computed
with pandas/numpy (no scipy)."""

from __future__ import annotations

import logging
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


def factor_ic(expr: Expr, provider, codes: list[str], start: date, end: date,
              *, fwd_days: int = 5, lookback_days: int = 200,
              min_names: int = 5, with_fundamentals: bool = False) -> float:
    """Mean cross-sectional rank-IC over the trading dates in [start, end].

    For each code, evaluate the factor series (② batch) and the forward return
    series once, then assemble each date's cross-section. NaN if no scorable
    dates.

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
        return float("nan")

    all_dates = sorted({d for s in fac_by_code.values() for d in s.index
                        if start <= d <= end})
    ics: list[float] = []
    for d in all_dates:
        fvals = {c: fac_by_code[c].get(d) for c in fac_by_code if d in fac_by_code[c].index}
        rvals = {c: fwd_by_code[c].get(d) for c in fwd_by_code if d in fwd_by_code[c].index}
        ic = rank_ic(fvals, rvals, min_names=min_names)
        if not np.isnan(ic):
            ics.append(ic)
    return float(np.mean(ics)) if ics else float("nan")
