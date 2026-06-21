"""Resolve the params a strategy should run with: tuned (from hyperopt, if
accepted) layered over the goal's params. Single source of truth so the
selector, runtime, and any other init site stay consistent."""

from __future__ import annotations


def resolve_params(db, strategy_name: str, goal) -> dict:
    base = dict(getattr(goal, "params", None) or {})
    tuned = db.get_active_params(strategy_name)
    if tuned:
        base.update(tuned)
    return base
