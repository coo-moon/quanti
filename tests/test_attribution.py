"""Tests for the active-vs-passive attribution guardrail."""

from __future__ import annotations

import numpy as np
import pandas as pd

from quanti.agent.attribution import active_vs_passive
from quanti.data.database import Database
from quanti.data.provider import DataProvider


def _seed(tmp_path, candidate_drift: float, account_path: list[float]):
    """Seed N candidate stocks with a given daily drift + a portfolio-snapshot
    series following `account_path` (total_value per trading day)."""
    db = Database(str(tmp_path / "av.db"))
    db.initialize()
    today = pd.Timestamp.today().normalize()
    dates = list(pd.bdate_range(end=today, periods=40))
    codes = [f"c{i}" for i in range(6)]
    rng = np.random.default_rng(0)
    for code in codes:
        prices = np.array([100 * (1 + candidate_drift) ** i + rng.normal(0, 0.01)
                           for i in range(len(dates))])
        db.save_daily_quotes(pd.DataFrame({
            "code": code, "date": [d.date() for d in dates],
            "open": prices, "high": prices, "low": prices, "close": prices,
            "volume": 1e6, "amount": prices * 1e6, "turnover": 1.0,
        }))
    # Portfolio snapshots over the last len(account_path) trading days.
    snap_dates = [d.date() for d in dates[-len(account_path):]]
    for d, tv in zip(snap_dates, account_path):
        db.save_portfolio_snapshot(d, tv * 0.5, tv * 0.5, tv)
    return db, codes, [d.date() for d in dates]


def test_active_beats_passive_not_flagged(tmp_path):
    # Candidates flat (~0% drift); account climbs 100k→110k → active beats passive.
    db, codes, _ = _seed(tmp_path, 0.0, [100_000, 103_000, 106_000, 110_000])
    try:
        av = active_vs_passive(db, DataProvider(db), codes, window_days=20)
        assert av is not None
        assert av["active"] > av["passive"]
        assert av["lagging"] is False
    finally:
        db.close()


def test_active_lags_passive_is_flagged(tmp_path):
    # Candidates rip (+2%/day); account barely moves → active LAGS passive.
    db, codes, _ = _seed(tmp_path, 0.02, [100_000, 100_500, 100_800, 101_000])
    try:
        av = active_vs_passive(db, DataProvider(db), codes, window_days=20)
        assert av is not None
        assert av["passive"] > av["active"]
        assert av["lagging"] is True
        assert av["n_names"] == len(codes)
    finally:
        db.close()


def test_fail_open_on_thin_snapshot_history(tmp_path):
    # Only one snapshot → cannot compute a window return → None (fail-open).
    db, codes, _ = _seed(tmp_path, 0.0, [100_000])
    try:
        assert active_vs_passive(db, DataProvider(db), codes) is None
    finally:
        db.close()


def test_fail_open_when_too_few_candidate_prices(tmp_path):
    db, codes, _ = _seed(tmp_path, 0.0, [100_000, 101_000, 102_000])
    try:
        # Only 2 real candidates (< min_names default 5) → None.
        assert active_vs_passive(db, DataProvider(db), codes[:2]) is None
    finally:
        db.close()
