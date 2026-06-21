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
    params when present (so ranking reflects what live will trade).

    Two-part assertion:
    1. resolve_params was consulted at all (spy on module-level function).
    2. The factory instantiates a strategy whose short_period attribute equals
       the tuned sentinel 999 — proving the tuned cfg, not goal.params,
       reached init. Verified by calling the factory directly (intercepted from
       run_walk_forward) and inspecting the instance attribute.
       Fails if the factory reverts to ``cfg = goal.params or {}``.
    """
    import quanti.agent.selector as sel

    # Seed an ACCEPTED tuned row for ma_cross with a sentinel value.
    seeded_db.save_optimization(
        "ma_cross",
        {"short_period": 999},
        oos_sharpe=1.0,
        baseline_oos_sharpe=0.0,
        accepted=True,
        n_combos=1,
        universe_size=1,
    )

    # Spy on resolve_params to confirm it was consulted.
    captured_resolve: dict[str, dict] = {}
    real_resolve = sel.resolve_params

    def _spy(db, name, goal):
        out = real_resolve(db, name, goal)
        captured_resolve[name] = out
        return out

    monkeypatch.setattr(sel, "resolve_params", _spy)

    # Intercept run_walk_forward to get access to the factory callable.
    # We call the factory once ourselves to inspect the instance it creates,
    # then delegate to the real run_walk_forward so the rest of evaluate() works.
    factory_instances: list = []
    real_rwf = sel.run_walk_forward

    def _intercepting_rwf(engine, factory, codes, end, **kwargs):
        # Call the factory once to capture what it produces.
        inst = factory()
        factory_instances.append(inst)
        # Delegate so the overall evaluate() result is still valid.
        return real_rwf(engine, factory, codes, end, **kwargs)

    monkeypatch.setattr(sel, "run_walk_forward", _intercepting_rwf)

    provider = DataProvider(seeded_db)
    from quanti.agent.selector import StrategySelector
    selector = StrategySelector(seeded_db, provider,
                                strategies_dir="strategies",
                                training_days=200)
    goal = Goal(params={"wf_enabled": True, "wf_n_folds": 2,
                        "wf_warmup_days": 60, "wf_test_days": 14})
    selector.evaluate(goal, codes=["000001"])

    # 1. resolve_params was consulted by the selector factory.
    assert captured_resolve, "resolve_params was not consulted by the selector factory"

    # 2. At least one factory-produced instance has short_period == 999.
    #    If the factory ignores resolve_params's return and falls back to
    #    goal.params (which has no short_period key), the instance gets the
    #    strategy's default (5), not 999 — and this assertion fails.
    ma_cross_tuned = [
        inst for inst in factory_instances
        if getattr(inst, "name", None) == "ma_cross"
        and getattr(inst, "short_period", None) == 999
    ]
    assert ma_cross_tuned, (
        f"No ma_cross factory instance had short_period=999. "
        f"Got instances: {[(getattr(i, 'name', '?'), getattr(i, 'short_period', '?')) for i in factory_instances]}. "
        "The selector factory is ignoring resolve_params's return value and "
        "falling back to goal.params instead of the tuned cfg."
    )
