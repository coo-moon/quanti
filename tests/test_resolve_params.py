from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quanti.agent.params import resolve_params
from quanti.agent.goal import Goal
from quanti.data.database import Database
from quanti.data.provider import DataProvider


class _Goal:
    def __init__(self, params):
        self.params = params


class _DB:
    def __init__(self, active):
        self._active = active
    def get_active_params(self, name):
        return self._active.get(name)


def test_tuned_overrides_goal_params():
    db = _DB({"ma_cross": {"short_period": 8}})
    goal = _Goal({"short_period": 5, "long_period": 20})
    assert resolve_params(db, "ma_cross", goal) == {"short_period": 8, "long_period": 20}


def test_no_tuned_returns_goal_params():
    db = _DB({})
    goal = _Goal({"short_period": 5})
    assert resolve_params(db, "ma_cross", goal) == {"short_period": 5}


def test_none_goal_params_safe():
    db = _DB({"x": {"a": 1}})
    goal = _Goal(None)
    assert resolve_params(db, "x", goal) == {"a": 1}


@pytest.fixture
def seeded_db(tmp_path):
    """Seed enough history for selector.evaluate() to run."""
    from datetime import date
    db = Database(str(tmp_path / "rp.db"))
    db.initialize()
    db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=300)
    np.random.seed(42)
    prices = 10 + np.arange(len(dates)) * 0.01 + np.random.randn(len(dates)) * 0.05
    df = pd.DataFrame({
        "code": "000001",
        "date": [d.date() for d in dates],
        "open": prices - 0.05,
        "high": prices + 0.15,
        "low": prices - 0.15,
        "close": prices,
        "volume": np.full(len(dates), 1_500_000.0),
        "amount": prices * 1_500_000,
        "turnover": np.full(len(dates), 1.0),
    })
    db.save_daily_quotes(df)
    yield db
    db.close()


def test_selector_factory_uses_tuned_params(monkeypatch, seeded_db):
    """The Selector's walk-forward factory must init strategies with tuned
    params when present (so ranking reflects what live will trade)."""
    import quanti.agent.selector as sel
    captured = {}
    real = sel.resolve_params

    def _spy(db, name, goal):
        out = real(db, name, goal)
        captured[name] = out
        return out

    monkeypatch.setattr(sel, "resolve_params", _spy)

    provider = DataProvider(seeded_db)
    from quanti.agent.selector import StrategySelector
    selector = StrategySelector(seeded_db, provider,
                                strategies_dir="strategies",
                                training_days=200)
    goal = Goal(params={"wf_enabled": True, "wf_n_folds": 2,
                        "wf_warmup_days": 60, "wf_test_days": 14})
    selector.evaluate(goal, codes=["000001"])
    assert captured, "resolve_params was not consulted by the selector factory"
