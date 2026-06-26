"""Look-ahead / data-leakage freeze for the cross-sectional factor panel.

`compute_factor_panel(as_of=t)` drives every live agent decision, so its value
at t must depend ONLY on data dated <= t. The panel already fetches
`get_daily_df(code, start, as_of)`, but nothing pinned that property — and it is
exactly the kind of thing a future refactor (or an LLM-mined factor, now that
the miner has an autonomous accept path) could silently break.

The test (port of freqtrade's lookahead-analysis to a cross-sectional pipeline):
  - HONEST vs CUTOFF: a provider that physically cannot return bars after `t`
    must produce the IDENTICAL panel — if anything reached past `as_of`, the
    cutoff provider would deny that data and the panels would diverge.
  - Negative control (teeth): a provider that LEAKS future bars (ignores the
    end date) must produce a DIFFERENT panel — proving the equality above is
    not vacuous (future data really would change factor values if it leaked in).
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.factors.cross_sectional import FactorConfig, compute_factor_panel


def _seed_db(tmp_path) -> tuple[Database, list, list[str]]:
    """4 stocks × 200 bars (trend / choppy / decline) so factor values are
    non-trivial and a future-data leak visibly changes them."""
    db = Database(str(tmp_path / "leak.db"))
    db.initialize()
    today = pd.Timestamp.today().normalize()
    dates = list(pd.bdate_range(end=today, periods=200))
    np.random.seed(7)
    specs = {
        "A": lambda i: 10 + i * 0.06 + np.random.randn() * 0.02,
        "B": lambda i: 10 + i * 0.07 + np.random.randn() * 0.02,
        "C": lambda i: 10 + np.sin(i / 5) * 1.5 + np.random.randn() * 0.05,
        "D": lambda i: 20 - i * 0.05 + np.random.randn() * 0.02,
    }
    for code, fn in specs.items():
        db.upsert_stock(code, code, "SZ", date(1991, 4, 3), "银行")
        prices = np.array([fn(i) for i in range(len(dates))])
        db.save_daily_quotes(pd.DataFrame({
            "code": code, "date": [d.date() for d in dates],
            "open": prices, "high": prices * 1.01, "low": prices * 0.99,
            "close": prices, "volume": np.full(len(dates), 1e6),
            "amount": prices * 1e6, "turnover": np.full(len(dates), 1.0),
        }))
    return db, [d.date() for d in dates], list(specs)


class _CutoffProvider:
    """Delegates to a real provider but physically cannot return bars after
    `cutoff`. Any look-ahead in compute_factor_panel surfaces as a divergence
    from the real provider (which already only requests <= as_of)."""

    def __init__(self, inner: DataProvider, cutoff: date) -> None:
        self._inner, self._cutoff = inner, cutoff

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get_daily_df(self, code, start, end):
        return self._inner.get_daily_df(code, start, min(end, self._cutoff))


class _LeakyProvider:
    """Negative control: ignores the requested end date and returns bars all
    the way to `far` — i.e. it leaks the future."""

    def __init__(self, inner: DataProvider, far: date) -> None:
        self._inner, self._far = inner, far

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get_daily_df(self, code, start, end):
        return self._inner.get_daily_df(code, start, self._far)


def test_panel_value_is_as_of_truncated(tmp_path):
    db, dates, codes = _seed_db(tmp_path)
    try:
        real = DataProvider(db)
        as_of = dates[139]                  # 60 future bars exist beyond as_of
        cfg = FactorConfig(industry_neutralize=False)
        honest = compute_factor_panel(real, db, codes, as_of=as_of, config=cfg)
        cutoff = compute_factor_panel(_CutoffProvider(real, as_of), db, codes,
                                      as_of=as_of, config=cfg)
        # The panel at t must not depend on any data after t.
        pd.testing.assert_frame_equal(honest.sort_index(), cutoff.sort_index())
    finally:
        db.close()


def test_future_leak_would_be_detected(tmp_path):
    db, dates, codes = _seed_db(tmp_path)
    try:
        real = DataProvider(db)
        as_of = dates[139]
        far = dates[-1]                     # leak ~60 future bars
        cfg = FactorConfig(industry_neutralize=False)
        honest = compute_factor_panel(real, db, codes, as_of=as_of, config=cfg)
        leaky = compute_factor_panel(_LeakyProvider(real, far), db, codes,
                                     as_of=as_of, config=cfg)
        # Teeth: feeding future bars DOES move the composite, so the as-of
        # equality above is a real constraint, not a tautology.
        h = honest["composite"].sort_index().to_numpy()
        lk = leaky["composite"].sort_index().to_numpy()
        assert not np.allclose(h, lk, equal_nan=True)
    finally:
        db.close()
