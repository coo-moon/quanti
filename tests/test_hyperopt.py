from __future__ import annotations

from datetime import date

import pandas as pd

from quanti.agent.hyperopt import HyperOptimizer, build_grid
from quanti.agent import walk_forward as wf


def test_build_grid_cartesian_product():
    combos, total = build_grid({"a": [1, 2], "b": [10, 20, 30]}, max_combos=64, seed=42)
    assert total == 6 and len(combos) == 6
    assert {"a": 1, "b": 10} in combos and {"a": 2, "b": 30} in combos


def test_build_grid_empty_space():
    assert build_grid({}, max_combos=64, seed=42) == ([], 0)


def test_build_grid_caps_and_is_deterministic():
    space = {"a": list(range(10)), "b": list(range(10))}  # 100 combos
    combos1, total1 = build_grid(space, max_combos=16, seed=42)
    combos2, _ = build_grid(space, max_combos=16, seed=42)
    assert total1 == 100 and len(combos1) == 16
    assert combos1 == combos2  # same seed → same sample


class _Strat:
    name = "fake"
    param_space = {"p": [1, 2, 3]}
    def init(self, cfg):
        self.p = cfg.get("p", 1)
    def on_bar(self, bar):
        return []


class _Engine:
    """Stub engine: run() returns an object with an equity_curve whose level
    encodes the param so train-search has a clear winner."""
    def clone(self):
        return self  # stateless stub — safe to reuse across the grid threads

    def run(self, strategy, codes, start, end):
        level = 1.0 + 0.1 * getattr(strategy, "p", 1)
        curve = pd.Series([100.0, 100.0 * level], index=[start, end])
        return type("R", (), {"equity_curve": curve, "trades": []})()


def test_optimize_skips_empty_param_space(monkeypatch):
    class NoSpace(_Strat):
        param_space = {}
    r = HyperOptimizer(_Engine()).optimize(NoSpace, ["000001"], date(2026, 6, 1))
    assert r.accepted is False and r.n_combos_total == 0 and "param_space" in r.reason


# Train-search ranks combos by compute_metrics(curve)["sharpe_ratio"]. The stub
# _Engine encodes the param `p` in the curve's last value (higher p → higher),
# so we stub compute_metrics to read that last value — making the train winner
# deterministically the highest-p combo (p=3), without depending on real
# Sharpe math over a 2-point curve.
def _patch_train_score(monkeypatch):
    monkeypatch.setattr("quanti.agent.hyperopt.compute_metrics",
                        lambda c: {"sharpe_ratio": float(c.iloc[-1])})


def test_optimize_accepts_when_tuned_beats_default(monkeypatch):
    _patch_train_score(monkeypatch)  # train winner = p=3
    # OOS validation: high sharpe for the tuned combo (p=3), low for default ({}→p=1).
    def fake_wf(engine, factory, codes, end, **kw):
        inst = factory()
        p = getattr(inst, "p", 1)
        sharpe = 2.0 if p == 3 else 0.1
        return wf.WalkForwardResult(
            folds=[wf.FoldResult(fold=None, metrics={"sharpe_ratio": sharpe},
                                 n_trades_oos=10)] * 3,
            oos_sharpe=sharpe, total_trades_oos=30, n_populated_folds=3)
    monkeypatch.setattr("quanti.agent.hyperopt.run_walk_forward", fake_wf)
    r = HyperOptimizer(_Engine()).optimize(_Strat, ["000001"], date(2026, 6, 1))
    assert r.accepted is True
    assert r.chosen_params == {"p": 3}
    assert r.tuned_oos_sharpe == 2.0 and r.default_oos_sharpe == 0.1


def test_optimize_rejects_when_tuned_not_better(monkeypatch):
    _patch_train_score(monkeypatch)
    def fake_wf(engine, factory, codes, end, **kw):
        return wf.WalkForwardResult(
            folds=[wf.FoldResult(fold=None, metrics={"sharpe_ratio": 0.5},
                                 n_trades_oos=10)] * 3,
            oos_sharpe=0.5, total_trades_oos=30,  # tuned == default → no margin
            n_populated_folds=3)
    monkeypatch.setattr("quanti.agent.hyperopt.run_walk_forward", fake_wf)
    r = HyperOptimizer(_Engine()).optimize(_Strat, ["000001"], date(2026, 6, 1))
    assert r.accepted is False  # 0.5 not > 0.5 + 0.1


def test_optimize_rejects_on_thin_trades(monkeypatch):
    _patch_train_score(monkeypatch)
    def fake_wf(engine, factory, codes, end, **kw):
        inst = factory()
        p = getattr(inst, "p", 1)
        sharpe = 2.0 if p == 3 else 0.1
        return wf.WalkForwardResult(
            folds=[wf.FoldResult(fold=None, metrics={"sharpe_ratio": sharpe},
                                 n_trades_oos=1)] * 3,
            oos_sharpe=sharpe, total_trades_oos=2,  # < min_trades_oos=5
            n_populated_folds=3)  # populated so the reject is on trades, not folds
    monkeypatch.setattr("quanti.agent.hyperopt.run_walk_forward", fake_wf)
    r = HyperOptimizer(_Engine()).optimize(_Strat, ["000001"], date(2026, 6, 1))
    assert r.accepted is False and "trades" in r.reason


def test_optimize_rejects_when_populated_folds_thin(monkeypatch):
    """The fold guard must count *populated* folds, not requested ones.

    run_walk_forward appends a FoldResult for every fold (empty ones included),
    so len(tuned.folds) == n_folds always and `len(folds) >= min_folds` was
    vacuous. A result that beats the default on Sharpe/trades but whose OOS
    blocks are almost all empty (only 1 populated fold) must still be rejected
    on the fold guard — otherwise the independent-sample floor is toothless."""
    _patch_train_score(monkeypatch)  # train winner = p=3
    def fake_wf(engine, factory, codes, end, **kw):
        p = getattr(factory(), "p", 1)
        if p == 3:  # tuned: strong Sharpe + plenty trades, but 1 populated fold
            return wf.WalkForwardResult(
                folds=[wf.FoldResult(fold=None, metrics={}, n_trades_oos=0)] * 6,
                oos_sharpe=2.0, total_trades_oos=30, n_populated_folds=1)
        return wf.WalkForwardResult(  # default baseline (comfortably populated)
            folds=[wf.FoldResult(fold=None, metrics={}, n_trades_oos=0)] * 6,
            oos_sharpe=0.1, total_trades_oos=30, n_populated_folds=6)
    monkeypatch.setattr("quanti.agent.hyperopt.run_walk_forward", fake_wf)
    r = HyperOptimizer(_Engine()).optimize(_Strat, ["000001"], date(2026, 6, 1))
    assert r.accepted is False
    assert "folds 1 < 2" in r.reason  # rejected on the populated-fold guard


def test_optimize_train_oos_no_overlap():
    # train_end must be strictly before the earliest fold's warmup_start.
    opt = HyperOptimizer(_Engine(), train_days=365, n_folds=3,
                         warmup_days=120, test_days=21)
    end = date(2026, 6, 1)
    folds = wf.make_folds(end, n_folds=3, warmup_days=120, test_days=21)
    earliest = min(f.warmup_start for f in folds)
    train_start, train_end = opt._train_window(end)
    assert train_end < earliest
    assert train_start < train_end
