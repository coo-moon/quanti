from __future__ import annotations

from quanti.agent.params import resolve_params


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
