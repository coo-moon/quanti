"""Tests for the factor IC drift watcher: snapshot persistence (mine +
rescore), decay/rejection/unmonitored detection, and the CLI rendering."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from quanti.agent.factor_miner import mine_factors, rescore_generated_factors
from quanti.agent.factor_watch import (
    format_watch,
    watch_factor_drift,
)
from quanti.data.database import Database


# ---------------------------------------------------------------- fixtures
class _LLM:
    def __init__(self, text):
        self._text = text

    def create_message(self, **kw):
        return {"content": [{"type": "text", "text": self._text}],
                "stop_reason": "end_turn", "usage": {}}


class _Provider:
    def __init__(self, data):
        self._data = data
        n = max(len(v) for v in data.values())
        self._dates = [d.date() for d in pd.bdate_range(
            end=pd.Timestamp("2025-06-01"), periods=n)]

    def get_daily_df(self, code, start, end):
        c = self._data.get(code, [])
        df = pd.DataFrame({"date": self._dates[:len(c)], "open": c, "high": c,
                           "low": c, "close": c, "volume": [1.0] * len(c),
                           "turnover": [1.0] * len(c)})
        return df[(df["date"] >= start) & (df["date"] <= end)]


def _seed_provider():
    rng = np.random.default_rng(1)
    data = {}
    for i in range(6):
        drift = 0.5 if i < 3 else -0.5
        data[f"c{i}"] = list(100 + np.cumsum(
            np.full(180, drift) + rng.normal(0, 0.1, 180)))
    return _Provider(data), list(data)


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "w.db"))
    d.initialize()
    yield d
    d.close()


def _snap(name: str, day: date, ic: float, accepted: bool, db) -> None:
    db.save_factor_ic_snapshot(name, day, train_ic=ic, oos_ic=ic,
                               oos_t=2.0, oos_n=40, accepted=accepted)


# ------------------------------------------------- snapshot persistence
def test_snapshot_roundtrip(db):
    d1 = date(2026, 7, 1)
    d2 = date(2026, 7, 2)
    _snap("a", d1, 0.05, True, db)
    _snap("a", d2, 0.06, True, db)
    hist = db.list_factor_ic_history("a")
    assert [h["as_of"] for h in hist] == [d2, d1]  # newest first
    assert hist[0]["oos_ic"] == 0.06
    assert hist[0]["accepted"] is True
    assert hist[0]["oos_n"] == 40
    # same-day re-save overwrites (rescore may run twice in one day)
    _snap("a", d2, 0.07, True, db)
    assert len(db.list_factor_ic_history("a")) == 2
    assert db.list_factor_ic_history("a")[0]["oos_ic"] == 0.07


def test_snapshot_accepts_nan(db):
    db.save_factor_ic_snapshot("a", date(2026, 7, 1), float("nan"),
                               float("nan"), float("nan"), 0, False)
    row = db.list_factor_ic_history("a")[0]
    assert row["oos_ic"] is None  # NaN → NULL on disk


def test_mine_persists_baseline_snapshot(db):
    provider, codes = _seed_provider()
    llm = _LLM("mom_a: Ref(close,1)/Ref(close,21)-1\n")
    results = mine_factors(llm, db, provider, codes, date(2025, 5, 20),
                           n_candidates=5, oos_ic_threshold=0.0,
                           min_train_ic=0.0)
    names = {r.name for r in results}
    assert "mom_a" in names
    hist = db.list_factor_ic_history("mom_a")
    assert len(hist) == 1  # baseline snapshot at mine time
    assert hist[0]["accepted"] == next(
        r.accepted for r in results if r.name == "mom_a")


def test_rescore_persists_snapshot(db):
    provider, codes = _seed_provider()
    db.save_generated_factor("mom_a", "Ref(close,1)/Ref(close,21)-1",
                             0.05, 0.05, accepted=True)
    results = rescore_generated_factors(db, provider, codes, date(2025, 5, 20),
                                        oos_ic_threshold=0.0,
                                        min_train_ic=0.0)
    by = {r.name: r for r in results}
    assert "mom_a" in by
    hist = db.list_factor_ic_history("mom_a")
    assert len(hist) == 1
    assert hist[0]["as_of"] == date(2025, 5, 20)
    assert hist[0]["accepted"] == by["mom_a"].accepted


# ------------------------------------------------------------- drift watch
def test_watch_flags_decayed(db):
    day = date(2026, 7, 1)
    db.save_generated_factor("edge", "close", 0.05, 0.05, accepted=True)
    for i in range(8):
        _snap("edge", day + timedelta(days=i), 0.06, True, db)
    for i in range(8, 11):
        _snap("edge", day + timedelta(days=i), 0.01, True, db)
    report = watch_factor_drift(db)
    assert "edge" in report["decayed"]
    assert report["ok"] is False
    entry = next(f for f in report["factors"] if f["name"] == "edge")
    assert entry["status"] == "decayed"
    assert entry["baseline"] == pytest.approx(0.06, abs=1e-9)
    assert entry["recent"] == pytest.approx(0.01, abs=1e-9)


def test_watch_flags_newly_rejected(db):
    day = date(2026, 7, 1)
    db.save_generated_factor("edge", "close", 0.05, 0.05, accepted=True)
    for i in range(5):
        _snap("edge", day + timedelta(days=i), 0.05, True, db)
    _snap("edge", day + timedelta(days=5), 0.005, False, db)
    report = watch_factor_drift(db)
    assert "edge" in report["newly_rejected"]
    entry = next(f for f in report["factors"] if f["name"] == "edge")
    assert entry["status"] == "rejected"


def test_watch_healthy(db):
    day = date(2026, 7, 1)
    db.save_generated_factor("edge", "close", 0.05, 0.05, accepted=True)
    for i in range(10):
        _snap("edge", day + timedelta(days=i), 0.05, True, db)
    report = watch_factor_drift(db)
    assert report["ok"] is True
    assert report["decayed"] == []
    entry = next(f for f in report["factors"] if f["name"] == "edge")
    assert entry["status"] == "healthy"


def test_watch_insufficient_history_not_flagged(db):
    day = date(2026, 7, 1)
    db.save_generated_factor("edge", "close", 0.05, 0.05, accepted=True)
    for i in range(3):
        _snap("edge", day + timedelta(days=i), 0.01, True, db)
    report = watch_factor_drift(db)
    assert report["decayed"] == []
    assert report["ok"] is True
    entry = next(f for f in report["factors"] if f["name"] == "edge")
    assert entry["status"] == "insufficient"


def test_watch_newly_accepted_not_decayed(db):
    day = date(2026, 7, 1)
    db.save_generated_factor("edge", "close", 0.05, 0.05, accepted=True)
    for i in range(4):
        _snap("edge", day + timedelta(days=i), 0.0, False, db)
    _snap("edge", day + timedelta(days=4), 0.08, True, db)
    report = watch_factor_drift(db)
    assert "edge" in report["newly_accepted"]
    assert "edge" not in report["decayed"]
    entry = next(f for f in report["factors"] if f["name"] == "edge")
    assert entry["status"] == "newly_accepted"


def test_watch_unmonitored_accepted_without_history(db):
    db.save_generated_factor("legacy", "close", 0.05, 0.05, accepted=True)
    report = watch_factor_drift(db)
    assert "legacy" in report["unmonitored"]
    assert report["ok"] is False
    entry = next(f for f in report["factors"] if f["name"] == "legacy")
    assert entry["status"] == "unmonitored"


def test_watch_empty_library(db):
    report = watch_factor_drift(db)
    assert report["ok"] is True
    assert report["factors"] == []


def test_format_watch_renders_problems(db):
    day = date(2026, 7, 1)
    db.save_generated_factor("edge", "close", 0.05, 0.05, accepted=True)
    for i in range(8):
        _snap("edge", day + timedelta(days=i), 0.06, True, db)
    for i in range(8, 11):
        _snap("edge", day + timedelta(days=i), 0.01, True, db)
    text = format_watch(watch_factor_drift(db))
    assert "edge" in text
    assert "decayed" in text

