"""Cross-sectional factors.

The existing strategy library is purely time-series (each stock looked at
in isolation). Cross-sectional factors compare stocks *against each other*
on the same date — which is the main source of alpha in A-share academic
literature (Hou-Xue-Zhang, Liu-Stambaugh-Yuan 2019, etc).

Factors here are deliberately a small starter set chosen for robustness in
the A-share daily-bar regime, not exhaustive coverage:

  * `momentum_6m` — 6-month momentum, lagging the last month to dodge the
    short-term reversal effect. Standard Jegadeesh-Titman with the
    "skip the most recent month" convention.
  * `momentum_3m` — same idea on a shorter window.
  * `reversal_1w` — short-term reversal. Negative of last-week return.
  * `turnover_20d` — high recent turnover predicts low future return in A
    shares (high attention → already priced). Sign-flipped to "higher is
    better".
  * `realized_vol_20d` — low-vol anomaly. Sign-flipped.

The pipeline:
  raw factor values → 99% winsorize → cross-sectional z-score
  → optional industry demean → equal-weight composite.

Output is a DataFrame indexed by code, with one column per factor plus a
`composite` column and `industry`. All values in [-3, 3]-ish range; the
composite is what downstream code (Selector, signal ranker, LLM agent)
should use as the "is this stock relatively attractive" score.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

import numpy as np
import pandas as pd

from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.factors.library import FACTOR_EXPRS, as_factor_fn

logger = logging.getLogger(__name__)


# A FactorFn takes a code's bars (sorted asc by date) and returns a single
# scalar — the factor value for that code on the most recent date in `bars`.
# Returning np.nan signals "not computable" (insufficient history, etc).
FactorFn = Callable[[pd.DataFrame], float]


# ---------------------------------------------------------- factor library

# DSL-backed factor functions (behavior-equivalent to the prior hand-written
# versions; defined declaratively in quanti.factors.library). Names are kept
# so existing imports (e.g. tests) and DEFAULT_FACTORS stay drop-in.
factor_momentum_3m = as_factor_fn(FACTOR_EXPRS["momentum_3m"])
factor_momentum_6m = as_factor_fn(FACTOR_EXPRS["momentum_6m"])
factor_reversal_1w = as_factor_fn(FACTOR_EXPRS["reversal_1w"])
factor_turnover_20d = as_factor_fn(FACTOR_EXPRS["turnover_20d"])
factor_realized_vol_20d = as_factor_fn(FACTOR_EXPRS["realized_vol_20d"])

DEFAULT_FACTORS: dict[str, FactorFn] = {
    "momentum_3m": factor_momentum_3m,
    "momentum_6m": factor_momentum_6m,
    "reversal_1w": factor_reversal_1w,
    "turnover_20d": factor_turnover_20d,
    "realized_vol_20d": factor_realized_vol_20d,
}


# --------------------------------------------------------- pipeline

@dataclass
class FactorConfig:
    factors: dict[str, FactorFn] | None = None       # default uses DEFAULT_FACTORS
    weights: dict[str, float] | None = None          # equal-weight if None
    winsorize_pct: float = 0.01                      # 1%/99% winsorization
    industry_neutralize: bool = True
    lookback_days: int = 200                         # bars to fetch per code

    def resolved(self) -> dict[str, FactorFn]:
        return self.factors if self.factors is not None else dict(DEFAULT_FACTORS)


def _winsorize(s: pd.Series, pct: float) -> pd.Series:
    """Clip extreme values to the [pct, 1-pct] cross-sectional quantiles.

    Done before z-scoring so a single fat-tail outlier (e.g. a stock with
    a -90% reversal that's actually a corporate-action artifact) doesn't
    crush every other stock's z-score.
    """
    if s.dropna().empty:
        return s
    lo = s.quantile(pct)
    hi = s.quantile(1 - pct)
    return s.clip(lower=lo, upper=hi)


def _zscore(s: pd.Series) -> pd.Series:
    valid = s.dropna()
    if len(valid) < 2:
        return s * 0.0
    mu = valid.mean()
    sd = valid.std()
    if sd == 0 or np.isnan(sd):
        return s * 0.0
    return (s - mu) / sd


def _industry_demean(panel: pd.DataFrame, value_col: str) -> pd.Series:
    """Subtract the per-industry mean from `value_col`. Stocks with unknown
    industry are passed through unchanged.

    Industry-neutralization removes systematic exposure to sector betas —
    if "all banks are cheap" the composite shouldn't just buy banks; it
    should pick the *most* attractive bank against its peers.
    """
    if "industry" not in panel.columns or value_col not in panel.columns:
        return panel[value_col]
    out = panel[value_col].copy()
    for ind, sub in panel.groupby("industry", dropna=True):
        if not ind or len(sub) < 2:
            continue
        out.loc[sub.index] = sub[value_col] - sub[value_col].mean()
    return out


def compute_factor_panel(
    provider: DataProvider,
    db: Database,
    codes: list[str],
    as_of: date | None = None,
    config: FactorConfig | None = None,
    include_generated: bool = False,
) -> pd.DataFrame:
    """Compute the cross-sectional factor panel for `codes` as of `as_of`.

    Returns a DataFrame indexed by code with one column per factor
    (z-scored, optionally industry-demeaned), plus a `composite` column
    and an `industry` column. Codes lacking enough history are dropped.

    Heavy work happens here: each code loads bars, each factor crunches
    rolling stats. For ~200 stocks × 5 factors this is sub-second; for
    universes in the thousands we'd want to push this into vectorized
    panel computation. Defer that until needed.
    """
    cfg = config or FactorConfig()
    as_of = as_of or date.today()
    factor_fns = dict(cfg.resolved())
    if include_generated:
        factor_fns.update(db.load_active_factor_fns())
    start = as_of - timedelta(days=cfg.lookback_days)

    rows: dict[str, dict[str, float]] = {}
    for code in codes:
        bars_df = provider.get_daily_df(code, start, as_of)
        if bars_df.empty or len(bars_df) < 21:
            continue
        bars_df = bars_df.sort_values("date").reset_index(drop=True)
        row: dict[str, float] = {}
        for name, fn in factor_fns.items():
            try:
                row[name] = float(fn(bars_df))
            except Exception as e:
                logger.debug(f"factor {name} failed for {code}: {e}")
                row[name] = float("nan")
        stock = db.get_stock(code)
        row["industry"] = stock.industry if stock and stock.industry else ""
        rows[code] = row

    if not rows:
        return pd.DataFrame()

    panel = pd.DataFrame.from_dict(rows, orient="index")

    # Per-factor: winsorize → zscore → industry-demean.
    factor_cols = [c for c in panel.columns if c != "industry"]
    for col in factor_cols:
        panel[col] = _winsorize(panel[col], cfg.winsorize_pct)
        panel[col] = _zscore(panel[col])
        if cfg.industry_neutralize:
            panel[col] = _industry_demean(panel, col)

    # Composite: weighted mean across factors. NaN-safe: a factor missing
    # for a single stock doesn't disqualify it, the others carry weight.
    weights = cfg.weights or {c: 1.0 for c in factor_cols}
    w_arr = np.array([weights.get(c, 0.0) for c in factor_cols])
    if w_arr.sum() > 0:
        w_arr = w_arr / w_arr.sum()
    else:
        w_arr = np.ones(len(factor_cols)) / max(len(factor_cols), 1)

    # Use masked mean to gracefully handle per-stock NaN factor values.
    raw = panel[factor_cols].values
    mask = ~np.isnan(raw)
    weighted = np.where(mask, raw, 0.0) * w_arr
    eff_w = mask * w_arr
    eff_w_sum = eff_w.sum(axis=1)
    composite = np.where(eff_w_sum > 0,
                         weighted.sum(axis=1) / np.maximum(eff_w_sum, 1e-12),
                         np.nan)
    panel["composite"] = composite

    return panel


def rank_by_composite(panel: pd.DataFrame, top_n: int | None = None,
                      ) -> list[tuple[str, float]]:
    """Return `[(code, score), ...]` sorted descending by composite.

    Useful for piping into a signal-ranker or for the LLM agent's "show me
    the top N candidates" prompt context.
    """
    if panel.empty or "composite" not in panel.columns:
        return []
    sub = panel[["composite"]].dropna().sort_values("composite", ascending=False)
    out = [(idx, float(row["composite"])) for idx, row in sub.iterrows()]
    if top_n is not None:
        out = out[:top_n]
    return out
