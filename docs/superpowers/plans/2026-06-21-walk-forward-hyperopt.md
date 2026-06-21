# Walk-Forward Hyperopt (v1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add on-demand walk-forward parameter optimization: grid-search each strategy's params on a train window, validate the winner out-of-sample, persist tuned params only when they beat the strategy's defaults OOS, and have the agent/selector/backtest use tuned params when present — surfaced in the Agent UI.

**Architecture:** A standalone `HyperOptimizer` (orchestrates a `BacktestEngine`) does A1: search-on-train → validate-OOS (reusing `run_walk_forward`) → accept-only-if-beats-default. Results persist in a new `strategy_params` table. A single `resolve_params(db, name, goal)` helper layers tuned params over `goal.params` at every `strategy.init()` site. Triggered by a CLI command and an async API job (reusing the existing `sync_jobs` progress mechanism); the Agent view gets an optimize card.

**Tech Stack:** Python 3.13, stdlib `itertools`/`random`, existing `BacktestEngine` + `run_walk_forward` + `compute_metrics`, SQLite via `Database`, FastAPI, Vue 3 + axios. No new Python or JS dependencies.

## Global Constraints

- Optimization is **on-demand only** (CLI + async API). It does NOT run per agent cycle.
- Search is **dependency-free grid** with a `max_combos` cap (default 64); over-cap grids are randomly sampled with a fixed `seed` (default 42) and the dropped count is `log`-ged (no silent truncation).
- Anti-overfit structure is **A1**: pick best combo on a TRAIN window `[train_start, train_end]`; validate that combo AND the default `{}` config on the OOS folds; accept the tuned combo only if it passes guards and beats default. Train and OOS (including warmup) must NOT overlap: `train_end = (earliest fold).warmup_start - 1 day`.
- Defaults: `train_days=365`, `n_folds=3`, `warmup_days=120`, `test_days=21`, `max_combos=64`, `accept_margin=0.1`, `min_folds=2`, `min_trades_oos=5`, `seed=42`.
- Accept gate: `accepted = (len(tuned.folds) >= min_folds) AND (tuned.total_trades_oos >= min_trades_oos) AND (tuned.oos_sharpe > default.oos_sharpe + accept_margin) AND (tuned.oos_sharpe > 0)`. Otherwise keep defaults.
- The OOS validation baseline (the "default" config) is `{}` — the strategy's built-in `init()` defaults. Each `param_space` should include those defaults as candidate values.
- Tuned params persist per-account (the trading DB, same place as `agent_goal`).
- `resolve_params(db, name, goal) = {**(goal.params or {}), **(db.get_active_params(name) or {})}` — tuned overrides goal defaults; must be applied at EVERY `strategy.init()` site (selector + runtime).
- Keep the existing test suite green. `ruff check .` clean for branch-touched Python; `cd web && npm run build` clean for frontend changes.

Spec: `docs/superpowers/specs/2026-06-21-walk-forward-hyperopt-design.md`.

---

## File Structure

- **Modify** `quanti/strategy/base.py` — add `param_space: dict[str, list] = {}` to `BaseStrategy` (Task 1).
- **Modify** `strategies/{ma_cross,ma_volume,macd_cross,bollinger_band,rsi_ob_os,turtle_breakout}.py` — declare `param_space` (Task 1).
- **Create** `quanti/agent/hyperopt.py` — `build_grid`, `OptimizeResult`, `HyperOptimizer` (Tasks 2–3).
- **Modify** `quanti/data/database.py` — `strategy_params` table + `save_optimization`/`get_active_params`/`list_optimization_results` (Task 4).
- **Create** `quanti/agent/params.py` — `resolve_params` (Task 5).
- **Modify** `quanti/agent/selector.py` + `quanti/agent/runtime.py` — use `resolve_params` at every init site (Task 6).
- **Modify** `quanti/cli.py` — `optimize` subcommand (Task 7).
- **Modify** `quanti/api/routes.py` — async optimize job + status + tuned-params endpoints (Task 8).
- **Modify** `web/src/api/client.ts` — optimize types + calls (Task 9).
- **Modify** `web/src/views/Agent.vue` — optimize card + polling + badge (Task 10).
- **Modify** docs + full regression (Task 11).

---

## Task 1: Strategy parameter spaces

**Files:**
- Modify: `quanti/strategy/base.py`
- Modify: `strategies/ma_cross.py`, `strategies/ma_volume.py`, `strategies/macd_cross.py`, `strategies/bollinger_band.py`, `strategies/rsi_ob_os.py`, `strategies/turtle_breakout.py`
- Test: `tests/test_param_space.py` (new)

**Interfaces:**
- Produces: `BaseStrategy.param_space: dict[str, list]` (default `{}`); each of the 6 strategies overrides it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_param_space.py
from __future__ import annotations

import itertools

from quanti.strategy.loader import StrategyLoader


def test_param_spaces_declared_sane():
    strats = {s.name: s for s in StrategyLoader().load_directory("strategies")}
    expected = {
        "ma_cross": {"short_period", "long_period"},
        "ma_volume": {"short_period", "long_period", "vol_ratio"},
        "macd_cross": {"fast", "slow"},
        "bollinger_band": {"period", "std_dev"},
        "rsi_ob_os": {"period", "oversold", "overbought"},
        "turtle_breakout": {"entry_period", "exit_period"},
    }
    for name, keys in expected.items():
        s = strats[name]
        space = type(s).param_space
        assert set(space.keys()) == keys, f"{name} param_space keys"
        # Every space stays within the 64-combo cap.
        n = 1
        for vals in space.values():
            assert isinstance(vals, list) and len(vals) >= 1
            n *= len(vals)
        assert n <= 64, f"{name} grid {n} exceeds cap"


def test_base_default_param_space_empty():
    from quanti.strategy.base import BaseStrategy
    assert BaseStrategy.param_space == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_param_space.py -q`
Expected: FAIL — `AttributeError: ... param_space` (not declared yet).

- [ ] **Step 3: Add the attribute and the six spaces**

In `quanti/strategy/base.py`, add to `BaseStrategy` (after `description`):

```python
    param_space: dict[str, list] = {}
    """Optional grid for walk-forward hyperopt: param name → candidate values.
    Empty = not tuned (strategy always uses init() defaults). Include the
    built-in defaults as candidates so the default config is in the grid."""
```

Add a class-level `param_space` to each strategy (defaults are members of each grid):

```python
# strategies/ma_cross.py — in MACrossStrategy
    param_space = {"short_period": [3, 5, 8, 10], "long_period": [20, 30, 60]}
# strategies/ma_volume.py — in MAVolumeStrategy
    param_space = {"short_period": [5, 8], "long_period": [20, 30], "vol_ratio": [1.2, 1.5, 2.0]}
# strategies/macd_cross.py — in MACDCrossStrategy
    param_space = {"fast": [8, 12], "slow": [21, 26, 30]}
# strategies/bollinger_band.py — in BollingerBandStrategy
    param_space = {"period": [15, 20, 25], "std_dev": [1.5, 2.0, 2.5]}
# strategies/rsi_ob_os.py — in RSIOverboughtOversoldStrategy
    param_space = {"period": [7, 14], "oversold": [20, 30], "overbought": [70, 80]}
# strategies/turtle_breakout.py — in TurtleBreakoutStrategy
    param_space = {"entry_period": [10, 20, 55], "exit_period": [5, 10, 20]}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_param_space.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
ruff check quanti/strategy/base.py strategies/ tests/test_param_space.py
git add quanti/strategy/base.py strategies/ tests/test_param_space.py
git commit -m "feat(strategy): declare param_space grids for hyperopt"
```

---

## Task 2: Grid builder

**Files:**
- Create: `quanti/agent/hyperopt.py`
- Test: `tests/test_hyperopt.py` (new)

**Interfaces:**
- Produces: `build_grid(param_space: dict[str, list], max_combos: int, seed: int) -> tuple[list[dict], int]` — returns `(combos, total_before_cap)`; each combo is a full param dict; over-cap grids are seed-sampled to `max_combos`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hyperopt.py
from __future__ import annotations

from quanti.agent.hyperopt import build_grid


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hyperopt.py -q`
Expected: FAIL — `ModuleNotFoundError: quanti.agent.hyperopt`.

- [ ] **Step 3: Create the module with `build_grid`**

```python
# quanti/agent/hyperopt.py
"""Walk-forward parameter optimization (hyperopt) for strategies.

A1 design: grid-search each strategy's `param_space` on a TRAIN window, then
validate the winning combo AND the default config out-of-sample (reusing
run_walk_forward); accept the tuned combo only if it beats the default OOS by
a margin and passes the fold/trade guards. On-demand (CLI + async API), never
per agent cycle. See docs/superpowers/specs/2026-06-21-walk-forward-hyperopt-design.md.
"""

from __future__ import annotations

import itertools
import logging
import random
from dataclasses import dataclass

logger = logging.getLogger(__name__)


def build_grid(param_space: dict[str, list], max_combos: int,
               seed: int) -> tuple[list[dict], int]:
    """Cartesian product of `param_space` → list of full param dicts.

    Returns (combos, total_before_cap). If the product exceeds `max_combos`,
    randomly sample `max_combos` of them with a fixed seed (reproducible) and
    log the dropped count — never silently truncate."""
    if not param_space:
        return [], 0
    keys = list(param_space.keys())
    value_lists = [param_space[k] for k in keys]
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*value_lists)]
    total = len(combos)
    if total > max_combos:
        combos = random.Random(seed).sample(combos, max_combos)
        logger.info("hyperopt grid capped: %d → %d combos (sampled, seed=%d)",
                    total, max_combos, seed)
    return combos, total
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_hyperopt.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
ruff check quanti/agent/hyperopt.py tests/test_hyperopt.py
git add quanti/agent/hyperopt.py tests/test_hyperopt.py
git commit -m "feat(agent): hyperopt grid builder with seeded cap"
```

---

## Task 3: HyperOptimizer (train → OOS validate → accept gate)

**Files:**
- Modify: `quanti/agent/hyperopt.py`
- Test: `tests/test_hyperopt.py` (append)

**Interfaces:**
- Consumes: `build_grid` (Task 2); `BacktestEngine.run` (existing); `run_walk_forward` + `make_folds` from `quanti.agent.walk_forward`; `compute_metrics` from `quanti.backtest.metrics`; `BaseStrategy.param_space`/`.name`.
- Produces:
  - `OptimizeResult(strategy_name: str, accepted: bool, chosen_params: dict, default_params: dict, tuned_oos_sharpe: float, default_oos_sharpe: float, n_combos_tried: int, n_combos_total: int, reason: str)`
  - `HyperOptimizer(engine, *, train_days=365, n_folds=3, warmup_days=120, test_days=21, max_combos=64, accept_margin=0.1, min_folds=2, min_trades_oos=5, seed=42)` with `optimize(strategy_cls, codes, end) -> OptimizeResult` and `optimize_all(strategy_classes, codes, end, progress=None) -> list[OptimizeResult]` (progress callback `progress(done:int, total:int, name:str)`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_hyperopt.py — append
from datetime import date

import pandas as pd

from quanti.agent.hyperopt import HyperOptimizer, OptimizeResult
from quanti.agent import walk_forward as wf
from quanti.backtest import metrics as bt_metrics


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
    def run(self, strategy, codes, start, end):
        level = 1.0 + 0.1 * getattr(strategy, "p", 1)
        curve = pd.Series([100.0, 100.0 * level], index=[start, end])
        return type("R", (), {"equity_curve": curve, "trades": []})()


def test_optimize_skips_empty_param_space(monkeypatch):
    class NoSpace(_Strat):
        param_space = {}
    r = HyperOptimizer(_Engine()).optimize(NoSpace, ["000001"], date(2026, 6, 1))
    assert r.accepted is False and r.n_combos_total == 0 and "param_space" in r.reason


def test_optimize_accepts_when_tuned_beats_default(monkeypatch):
    # Make OOS validation return a high sharpe for the tuned combo (p=3) and a
    # low one for the default ({}), with enough folds/trades to pass guards.
    def fake_wf(engine, factory, codes, end, **kw):
        inst = factory()
        p = getattr(inst, "p", 1)
        sharpe = 2.0 if p == 3 else 0.1  # default ({}) → p=1 → 0.1
        return wf.WalkForwardResult(
            folds=[wf.FoldResult(fold=None, metrics={"sharpe_ratio": sharpe},
                                 n_trades_oos=10)] * 3,
            oos_sharpe=sharpe, total_trades_oos=30)
    monkeypatch.setattr("quanti.agent.hyperopt.run_walk_forward", fake_wf)
    r = HyperOptimizer(_Engine()).optimize(_Strat, ["000001"], date(2026, 6, 1))
    assert r.accepted is True
    assert r.chosen_params == {"p": 3}
    assert r.tuned_oos_sharpe == 2.0 and r.default_oos_sharpe == 0.1


def test_optimize_rejects_when_tuned_not_better(monkeypatch):
    def fake_wf(engine, factory, codes, end, **kw):
        return wf.WalkForwardResult(
            folds=[wf.FoldResult(fold=None, metrics={"sharpe_ratio": 0.5},
                                 n_trades_oos=10)] * 3,
            oos_sharpe=0.5, total_trades_oos=30)  # tuned == default → no margin
    monkeypatch.setattr("quanti.agent.hyperopt.run_walk_forward", fake_wf)
    r = HyperOptimizer(_Engine()).optimize(_Strat, ["000001"], date(2026, 6, 1))
    assert r.accepted is False  # 0.5 not > 0.5 + 0.1


def test_optimize_rejects_on_thin_trades(monkeypatch):
    def fake_wf(engine, factory, codes, end, **kw):
        inst = factory(); p = getattr(inst, "p", 1)
        sharpe = 2.0 if p == 3 else 0.1
        return wf.WalkForwardResult(
            folds=[wf.FoldResult(fold=None, metrics={"sharpe_ratio": sharpe},
                                 n_trades_oos=1)] * 3,
            oos_sharpe=sharpe, total_trades_oos=2)  # < min_trades_oos=5
    monkeypatch.setattr("quanti.agent.hyperopt.run_walk_forward", fake_wf)
    r = HyperOptimizer(_Engine()).optimize(_Strat, ["000001"], date(2026, 6, 1))
    assert r.accepted is False and "trades" in r.reason


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_hyperopt.py -q`
Expected: FAIL — `ImportError: cannot import name 'HyperOptimizer'`.

- [ ] **Step 3: Implement `OptimizeResult` + `HyperOptimizer`**

Append to `quanti/agent/hyperopt.py` (add imports at top of file):

```python
from datetime import date, timedelta

from quanti.agent.walk_forward import make_folds, run_walk_forward
from quanti.backtest.metrics import compute_metrics


@dataclass
class OptimizeResult:
    strategy_name: str
    accepted: bool
    chosen_params: dict
    default_params: dict
    tuned_oos_sharpe: float
    default_oos_sharpe: float
    n_combos_tried: int
    n_combos_total: int
    reason: str


class HyperOptimizer:
    def __init__(self, engine, *, train_days: int = 365, n_folds: int = 3,
                 warmup_days: int = 120, test_days: int = 21,
                 max_combos: int = 64, accept_margin: float = 0.1,
                 min_folds: int = 2, min_trades_oos: int = 5,
                 seed: int = 42) -> None:
        self._engine = engine
        self.train_days = train_days
        self.n_folds = n_folds
        self.warmup_days = warmup_days
        self.test_days = test_days
        self.max_combos = max_combos
        self.accept_margin = accept_margin
        self.min_folds = min_folds
        self.min_trades_oos = min_trades_oos
        self.seed = seed

    def _train_window(self, end: date) -> tuple[date, date]:
        """Train window strictly before the OOS folds (incl. their warmup)."""
        folds = make_folds(end, n_folds=self.n_folds,
                           warmup_days=self.warmup_days, test_days=self.test_days)
        earliest_warmup = min(f.warmup_start for f in folds)
        train_end = earliest_warmup - timedelta(days=1)
        train_start = train_end - timedelta(days=self.train_days)
        return train_start, train_end

    def _factory(self, strategy_cls, cfg: dict):
        def make():
            inst = strategy_cls()
            inst.init(dict(cfg))
            return inst
        return make

    def optimize(self, strategy_cls, codes: list[str],
                 end: date) -> OptimizeResult:
        name = getattr(strategy_cls, "name", strategy_cls.__name__)
        space = dict(getattr(strategy_cls, "param_space", {}) or {})
        if not space:
            return OptimizeResult(name, False, {}, {}, 0.0, 0.0, 0, 0,
                                  "no param_space — not tuned")
        combos, total = build_grid(space, self.max_combos, self.seed)
        train_start, train_end = self._train_window(end)

        # 1) Search on TRAIN: best combo by train Sharpe.
        best_combo, best_train = None, float("-inf")
        for combo in combos:
            try:
                bt = self._engine.run(strategy=self._factory(strategy_cls, combo)(),
                                      codes=codes, start=train_start, end=train_end)
            except Exception as e:  # noqa: BLE001 - one combo failing != fatal
                logger.warning("hyperopt train run failed %s %s: %s", name, combo, e)
                continue
            curve = getattr(bt, "equity_curve", None)
            m = compute_metrics(curve) if curve is not None and len(curve) > 1 else {}
            sharpe = float(m.get("sharpe_ratio", 0.0) or 0.0)
            if sharpe > best_train:
                best_train, best_combo = sharpe, combo
        if best_combo is None:
            return OptimizeResult(name, False, {}, {}, 0.0, 0.0, len(combos),
                                  total, "no valid train result")

        # 2) Validate the best combo AND the default ({}) on OOS folds.
        def _wf(cfg: dict):
            return run_walk_forward(
                self._engine, self._factory(strategy_cls, cfg), codes, end,
                n_folds=self.n_folds, warmup_days=self.warmup_days,
                test_days=self.test_days)
        tuned = _wf(best_combo)
        default = _wf({})

        # 3) Accept gate.
        folds_ok = len(tuned.folds) >= self.min_folds
        trades_ok = tuned.total_trades_oos >= self.min_trades_oos
        beats = tuned.oos_sharpe > default.oos_sharpe + self.accept_margin
        positive = tuned.oos_sharpe > 0
        accepted = folds_ok and trades_ok and beats and positive
        if accepted:
            verdict = "accepted"
        elif not folds_ok:
            verdict = f"rejected: folds {len(tuned.folds)} < {self.min_folds}"
        elif not trades_ok:
            verdict = f"rejected: trades {tuned.total_trades_oos} < {self.min_trades_oos}"
        else:
            verdict = "rejected: did not beat default → keep default"
        reason = (f"tuned_oos={tuned.oos_sharpe:.2f} vs default={default.oos_sharpe:.2f}"
                  f" (margin {self.accept_margin}); {verdict}")
        return OptimizeResult(name, accepted, dict(best_combo), {},
                              float(tuned.oos_sharpe), float(default.oos_sharpe),
                              len(combos), total, reason)

    def optimize_all(self, strategy_classes, codes: list[str], end: date,
                     progress=None) -> list[OptimizeResult]:
        results: list[OptimizeResult] = []
        total = len(strategy_classes)
        for i, cls in enumerate(strategy_classes):
            if progress:
                progress(i, total, getattr(cls, "name", cls.__name__))
            results.append(self.optimize(cls, codes, end))
        if progress:
            progress(total, total, "")
        return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_hyperopt.py -q`
Expected: PASS (all).

- [ ] **Step 5: Lint + commit**

```bash
ruff check quanti/agent/hyperopt.py tests/test_hyperopt.py
git add quanti/agent/hyperopt.py tests/test_hyperopt.py
git commit -m "feat(agent): HyperOptimizer — train search, OOS validate, accept-beats-default"
```

---

## Task 4: Persist tuned params

**Files:**
- Modify: `quanti/data/database.py` (CREATE TABLE block ~line 340; add methods near `get_sync_job` ~line 660)
- Test: `tests/test_hyperopt_db.py` (new)

**Interfaces:**
- Produces:
  - `Database.save_optimization(strategy_name, params: dict, oos_sharpe: float, baseline_oos_sharpe: float, accepted: bool, n_combos: int, universe_size: int) -> None` (upsert).
  - `Database.get_active_params(strategy_name) -> dict | None` (parsed `params_json` only when `accepted=1`, else `None`).
  - `Database.list_optimization_results() -> list[dict]` (all rows; keys: `strategy_name, params, oos_sharpe, baseline_oos_sharpe, accepted, n_combos, universe_size, tuned_at`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hyperopt_db.py
from __future__ import annotations

from quanti.data.database import Database


def test_save_get_list_optimization(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.save_optimization("ma_cross", {"short_period": 8, "long_period": 30},
                         oos_sharpe=1.5, baseline_oos_sharpe=0.4, accepted=True,
                         n_combos=12, universe_size=100)
    db.save_optimization("rsi_ob_os", {"period": 7}, oos_sharpe=0.2,
                         baseline_oos_sharpe=0.3, accepted=False, n_combos=8,
                         universe_size=100)
    # Accepted → params returned; rejected → None.
    assert db.get_active_params("ma_cross") == {"short_period": 8, "long_period": 30}
    assert db.get_active_params("rsi_ob_os") is None
    assert db.get_active_params("never_tuned") is None
    rows = {r["strategy_name"]: r for r in db.list_optimization_results()}
    assert rows["ma_cross"]["accepted"] is True
    assert rows["rsi_ob_os"]["accepted"] is False
    assert rows["ma_cross"]["oos_sharpe"] == 1.5


def test_save_optimization_upserts(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.save_optimization("ma_cross", {"short_period": 5}, 1.0, 0.5, True, 12, 100)
    db.save_optimization("ma_cross", {"short_period": 10}, 2.0, 0.5, True, 12, 100)
    assert db.get_active_params("ma_cross") == {"short_period": 10}
    assert len(db.list_optimization_results()) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hyperopt_db.py -q`
Expected: FAIL — `AttributeError: ... save_optimization`.

- [ ] **Step 3: Add the table and methods**

In `quanti/data/database.py`, inside the schema-creation block (where the other `CREATE TABLE IF NOT EXISTS` statements live, e.g. right after the `agent_decisions` table), add:

```python
            CREATE TABLE IF NOT EXISTS strategy_params (
                strategy_name TEXT PRIMARY KEY,
                params_json TEXT NOT NULL,
                oos_sharpe REAL,
                baseline_oos_sharpe REAL,
                accepted INTEGER NOT NULL,
                n_combos INTEGER,
                universe_size INTEGER,
                tuned_at TEXT NOT NULL
            );
```

Add the methods (near `get_sync_job`):

```python
    def save_optimization(self, strategy_name: str, params: dict,
                          oos_sharpe: float, baseline_oos_sharpe: float,
                          accepted: bool, n_combos: int,
                          universe_size: int) -> None:
        import json
        from datetime import datetime
        self.conn.execute(
            "INSERT OR REPLACE INTO strategy_params "
            "(strategy_name, params_json, oos_sharpe, baseline_oos_sharpe, "
            " accepted, n_combos, universe_size, tuned_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (strategy_name, json.dumps(params), float(oos_sharpe),
             float(baseline_oos_sharpe), 1 if accepted else 0, int(n_combos),
             int(universe_size), datetime.now().isoformat()),
        )
        self.conn.commit()

    def get_active_params(self, strategy_name: str) -> dict | None:
        import json
        row = self.conn.execute(
            "SELECT params_json, accepted FROM strategy_params WHERE strategy_name=?",
            (strategy_name,),
        ).fetchone()
        if row is None or not row[1]:
            return None
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            return None

    def list_optimization_results(self) -> list[dict]:
        import json
        rows = self.conn.execute(
            "SELECT strategy_name, params_json, oos_sharpe, baseline_oos_sharpe, "
            "accepted, n_combos, universe_size, tuned_at FROM strategy_params "
            "ORDER BY strategy_name",
        ).fetchall()
        out = []
        for r in rows:
            try:
                params = json.loads(r[1])
            except (ValueError, TypeError):
                params = {}
            out.append({"strategy_name": r[0], "params": params,
                        "oos_sharpe": r[2], "baseline_oos_sharpe": r[3],
                        "accepted": bool(r[4]), "n_combos": r[5],
                        "universe_size": r[6], "tuned_at": r[7]})
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_hyperopt_db.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
ruff check quanti/data/database.py tests/test_hyperopt_db.py
git add quanti/data/database.py tests/test_hyperopt_db.py
git commit -m "feat(db): strategy_params store for tuned params"
```

---

## Task 5: `resolve_params` helper

**Files:**
- Create: `quanti/agent/params.py`
- Test: `tests/test_resolve_params.py` (new)

**Interfaces:**
- Consumes: `Database.get_active_params` (Task 4); a `goal` object with a `.params` dict.
- Produces: `resolve_params(db, strategy_name: str, goal) -> dict` = `{**(goal.params or {}), **(db.get_active_params(name) or {})}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_resolve_params.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_resolve_params.py -q`
Expected: FAIL — `ModuleNotFoundError: quanti.agent.params`.

- [ ] **Step 3: Implement**

```python
# quanti/agent/params.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_resolve_params.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
ruff check quanti/agent/params.py tests/test_resolve_params.py
git add quanti/agent/params.py tests/test_resolve_params.py
git commit -m "feat(agent): resolve_params — layer tuned params over goal params"
```

---

## Task 6: Wire `resolve_params` into selector + runtime

**Files:**
- Modify: `quanti/agent/selector.py` (factory at `:153-157`)
- Modify: `quanti/agent/runtime.py` (`strat.init`/`strategy.init` at `:476`, `:765`, `:791`, `:810`)
- Test: `tests/test_resolve_params.py` (append integration spy)

**Interfaces:**
- Consumes: `resolve_params` (Task 5).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_resolve_params.py — append
def test_selector_factory_uses_tuned_params(monkeypatch, tmp_path):
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
    # Drive one evaluate() cycle via the module's existing test harness (reuse
    # the provider/goal/candidates setup already in tests for the selector),
    # with wf_enabled so the factory is built. Then:
    # ... selector.evaluate(goal, codes, candidates) ...
    assert captured  # resolve_params was consulted per strategy
```

Note: wire into the selector's existing test setup (there is walk-forward/selector coverage in `tests/test_walk_forward.py` / `tests/test_agent.py`). The assertion is that `resolve_params` is consulted in the factory path. If a full `evaluate()` is awkward, assert via a minimal invocation the module supports.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_resolve_params.py -q`
Expected: FAIL — `AttributeError: module 'quanti.agent.selector' has no attribute 'resolve_params'`.

- [ ] **Step 3: Wire it in**

`quanti/agent/selector.py` — add import near the top:

```python
from quanti.agent.params import resolve_params
```

At `selector.py:153-157`, change the factory's config from `goal.params` to the resolved per-strategy params:

```python
                    cls = type(strat)
                    cfg = resolve_params(self._db, strat.name, goal)
                    def factory(_cls=cls, _cfg=cfg) -> BaseStrategy:
                        inst = _cls()
                        inst.init(dict(_cfg))
                        return inst
```

(`self._db` — confirm the Selector holds a db handle; if the attribute is named differently, use that. If the Selector has no db, thread one in from its constructor — check how `StrategySelector` is built in `runtime.py`.)

`quanti/agent/runtime.py` — add import near the top:

```python
from quanti.agent.params import resolve_params
```

Replace each strategy-init config with the resolved params (the runtime has `self._db` / a db handle and the `goal`):

- `:476` — in the pick_topk loop, change `strat.init(dict(params))` to `strat.init(resolve_params(self._db, strat.name, goal))`.
- `:765` — change `strategy.init(goal.params or {})` to `strategy.init(resolve_params(self._db, strategy.name, goal))`.
- `:791` — same replacement.
- `:810` — same replacement.

(Confirm the db attribute name on `AgentRuntime` — it is the `db` passed to `AgentRuntime(db, provider, broker)`; use whatever instance attribute holds it, e.g. `self._db`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_resolve_params.py tests/test_agent.py tests/test_walk_forward.py -q`
Expected: PASS. (No tuned params in those tests → `resolve_params` returns `goal.params` → behavior unchanged.)

- [ ] **Step 5: Lint + commit**

```bash
ruff check quanti/agent/selector.py quanti/agent/runtime.py tests/test_resolve_params.py
git add quanti/agent/selector.py quanti/agent/runtime.py tests/test_resolve_params.py
git commit -m "feat(agent): selector + runtime use resolve_params at every init site"
```

---

## Task 7: CLI `optimize` command

**Files:**
- Modify: `quanti/cli.py` (`cmd_optimize` near `cmd_backtest` ~line 55; subparser + dispatch in `main` ~line 234/287)
- Test: `tests/test_cli_optimize.py` (new)

**Interfaces:**
- Consumes: `HyperOptimizer.optimize_all` (Task 3); `Database.save_optimization` (Task 4); `StrategyLoader`; `BacktestEngine`.
- Produces: `quanti.cli.cmd_optimize(args)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_optimize.py
from __future__ import annotations

import types
from datetime import date

import quanti.cli as cli


def test_cmd_optimize_persists_results(tmp_path, monkeypatch):
    db_path = tmp_path / "paper.db"
    monkeypatch.setenv("QUANTI_DB", str(db_path))  # if cli honors it; else patch _open_db

    from quanti.data.database import Database
    real_db = Database(str(db_path)); real_db.initialize()
    real_db.ensure_portfolio(1_000_000)
    monkeypatch.setattr(cli, "_open_db", lambda: Database(str(db_path)))

    # Stub the optimizer so the test is fast and deterministic.
    from quanti.agent import hyperopt
    def fake_all(self, classes, codes, end, progress=None):
        return [hyperopt.OptimizeResult("ma_cross", True, {"short_period": 8},
                                        {}, 1.5, 0.4, 12, 12, "accepted")]
    monkeypatch.setattr(hyperopt.HyperOptimizer, "optimize_all", fake_all)

    args = types.SimpleNamespace(universe=None, end=date.today().isoformat(),
                                 cash=1_000_000)
    cli.cmd_optimize(args)

    out = Database(str(db_path)).get_active_params("ma_cross")
    assert out == {"short_period": 8}
```

(If `_open_db` doesn't exist or differs, adapt the patch to however `cli.py` opens the DB — it already uses `_open_db()` in `cmd_backtest`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli_optimize.py -q`
Expected: FAIL — `AttributeError: module 'quanti.cli' has no attribute 'cmd_optimize'`.

- [ ] **Step 3: Implement `cmd_optimize` + register the subcommand**

Add to `quanti/cli.py` (near `cmd_backtest`):

```python
def cmd_optimize(args):
    """Walk-forward hyperopt over all candidate strategies; persist tuned params."""
    from datetime import date

    from quanti.agent.goal import load_goal
    from quanti.agent.hyperopt import HyperOptimizer
    from quanti.backtest.engine import BacktestEngine
    from quanti.data.provider import DataProvider
    from quanti.risk.manager import RiskConfig, RiskManager
    from quanti.strategy.loader import StrategyLoader

    db = _open_db()
    provider = DataProvider(db)
    goal = load_goal(db)
    pool = args.universe or goal.universe_pool
    codes = ([s.code for s in db.get_pool_stocks(pool)] if pool
             else [s.code for s in db.list_stocks()])
    max_universe = int((goal.params or {}).get("selector_max_universe", 100))
    codes = codes[:max(20, max_universe)]
    end = date.fromisoformat(args.end) if args.end else date.today()

    classes = [type(s) for s in StrategyLoader().load_directory("strategies")]
    engine = BacktestEngine(provider=provider, initial_cash=args.cash,
                            risk_manager=RiskManager(RiskConfig()))
    results = HyperOptimizer(engine).optimize_all(classes, codes, end)

    for r in results:
        db.save_optimization(r.strategy_name, r.chosen_params, r.tuned_oos_sharpe,
                             r.default_oos_sharpe, r.accepted, r.n_combos_tried,
                             len(codes))
        flag = "✓ 采纳" if r.accepted else "· 默认"
        logger.info("%s %-16s tuned=%.2f default=%.2f combos=%d/%d — %s",
                    flag, r.strategy_name, r.tuned_oos_sharpe,
                    r.default_oos_sharpe, r.n_combos_tried, r.n_combos_total,
                    r.reason)
    db.close()
```

(Confirm `db.get_pool_stocks` / `db.list_stocks` return objects with `.code` — adapt to the actual API used elsewhere in cli/routes; `cmd_backtest` and routes show how stocks/pools are read.)

In `main()`, register the subparser (after the `backtest` block, ~line 240):

```python
    # optimize
    opt_parser = subparsers.add_parser("optimize", help="走查式参数寻优(hyperopt)")
    opt_parser.add_argument("--universe", type=str, default=None,
                            help="股票池名；留空用 goal.universe_pool 或全市场")
    opt_parser.add_argument("--end", type=str, default=None,
                            help="优化截止日 YYYY-MM-DD；默认今天")
    opt_parser.add_argument("--cash", type=float, default=1_000_000)
```

And in the dispatch chain (after `elif cmd == "backtest":`):

```python
    elif cmd == "optimize":
        cmd_optimize(args)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cli_optimize.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
ruff check quanti/cli.py tests/test_cli_optimize.py
git add quanti/cli.py tests/test_cli_optimize.py
git commit -m "feat(cli): quanti optimize — run hyperopt + persist"
```

---

## Task 8: Async optimize API + status + tuned-params endpoints

**Files:**
- Modify: `quanti/api/routes.py` (near the quotes async job ~line 117-204; reuse `create_sync_job`/`update_sync_job`/`get_sync_job`)
- Test: `tests/test_api_optimize.py` (new)

**Interfaces:**
- Consumes: `HyperOptimizer` (Task 3); `Database.save_optimization`/`list_optimization_results`/`create_sync_job`/`update_sync_job`/`get_sync_job`.
- Produces:
  - `POST /agent/optimize/async` → `{job_id}`.
  - `GET /agent/optimize/status?job_id=` → `{job_id, current, total, status, current_strategy, results}` (results = `list_optimization_results()`).
  - `GET /agent/tuned-params` → `list_optimization_results()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_optimize.py
from __future__ import annotations

from fastapi.testclient import TestClient

from quanti.api.app import create_app


def test_tuned_params_endpoint_lists_rows(tmp_path, monkeypatch):
    app = create_app(initial_cash=1_000_000, autostart_agent=False)
    client = TestClient(app)
    # Seed a row via the app's db (the app exposes its db on state — match how
    # other tests reach it; otherwise hit the optimize flow). At minimum:
    r = client.get("/api/agent/tuned-params")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_optimize_async_returns_job_and_status(monkeypatch):
    # Stub HyperOptimizer.optimize_all to be instant + deterministic.
    from quanti.agent import hyperopt
    def fake_all(self, classes, codes, end, progress=None):
        if progress:
            progress(1, 1, "ma_cross")
        return [hyperopt.OptimizeResult("ma_cross", True, {"short_period": 8},
                                        {}, 1.5, 0.4, 12, 12, "accepted")]
    monkeypatch.setattr(hyperopt.HyperOptimizer, "optimize_all", fake_all)

    app = create_app(initial_cash=1_000_000, autostart_agent=False)
    client = TestClient(app)
    r = client.post("/api/agent/optimize/async")
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    # Poll until done (the executor task is quick with the stub).
    import time
    for _ in range(50):
        s = client.get("/api/agent/optimize/status", params={"job_id": job_id}).json()
        if s.get("status") in ("done", "completed", "error"):
            break
        time.sleep(0.05)
    assert s["status"] in ("done", "completed")
    assert any(x["strategy_name"] == "ma_cross" for x in s["results"])
```

(Adapt the polling/status-string to the project's job conventions seen in `_run_quotes_sync` / `get_quotes_sync_status`. Use the same `status` vocabulary as the quotes job — match it exactly.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_api_optimize.py -q`
Expected: FAIL — 404 on the new routes.

- [ ] **Step 3: Implement the endpoints**

In `quanti/api/routes.py`, mirror the quotes async pattern (`sync_quotes_async` / `_run_quotes_sync` / `get_quotes_sync_status`). Add:

```python
@router.post("/agent/optimize/async")
async def optimize_async(request: Request):
    """Start an on-demand hyperopt run. Returns a job_id immediately; progress
    via /agent/optimize/status. Heavy work runs in a thread executor so the
    event loop isn't blocked (same shape as the quotes async sync)."""
    db = request.app.state.db
    import uuid
    job_id = f"opt_{str(uuid.uuid4())[:8]}"
    db.create_sync_job(job_id, "_optimize", 0)
    asyncio.create_task(_run_optimize(job_id, request.app.state))
    return {"job_id": job_id}


async def _run_optimize(job_id: str, state) -> None:
    from datetime import date

    from quanti.agent.goal import load_goal
    from quanti.agent.hyperopt import HyperOptimizer
    from quanti.backtest.engine import BacktestEngine
    from quanti.data.provider import DataProvider
    from quanti.risk.manager import RiskConfig, RiskManager
    from quanti.strategy.loader import StrategyLoader

    db = state.db
    loop = asyncio.get_event_loop()

    def work() -> None:
        goal = load_goal(db)
        provider = DataProvider(db)
        pool = goal.universe_pool
        codes = ([s.code for s in db.get_pool_stocks(pool)] if pool
                 else [s.code for s in db.list_stocks()])
        max_universe = int((goal.params or {}).get("selector_max_universe", 100))
        codes = codes[:max(20, max_universe)]
        classes = [type(s) for s in StrategyLoader().load_directory("strategies")]
        db.update_sync_job(job_id, 0, "running", {})
        engine = BacktestEngine(provider=provider, initial_cash=1_000_000,
                                risk_manager=RiskManager(RiskConfig()))

        def progress(done: int, total: int, name: str) -> None:
            # total may be 0 at create time; set it on first call.
            db.update_sync_job(job_id, done, "running", {"current_strategy": name})

        opt = HyperOptimizer(engine)
        results = []
        total = len(classes)
        for i, cls in enumerate(classes):
            progress(i, total, getattr(cls, "name", cls.__name__))
            r = opt.optimize(cls, codes, date.today())
            db.save_optimization(r.strategy_name, r.chosen_params,
                                 r.tuned_oos_sharpe, r.default_oos_sharpe,
                                 r.accepted, r.n_combos_tried, len(codes))
            results.append(r)
        db.update_sync_job(job_id, total, "done", {})

    try:
        await loop.run_in_executor(None, work)
    except Exception as e:  # noqa: BLE001
        db.update_sync_job(job_id, 0, "error", {"error": str(e)})


@router.get("/agent/optimize/status")
async def optimize_status(job_id: str, request: Request):
    db = request.app.state.db
    job = db.get_sync_job(job_id)
    if job is None:
        return {"error": f"Job '{job_id}' not found"}
    return {
        "job_id": job["job_id"], "current": job["current"],
        "total": job["total"], "status": job["status"],
        "current_strategy": job["errors"].get("current_strategy", ""),
        "results": db.list_optimization_results(),
    }


@router.get("/agent/tuned-params")
async def tuned_params(request: Request):
    return request.app.state.db.list_optimization_results()
```

Notes for the implementer:
- Match how other routes access the db (`request.app.state.db` vs a dependency) — copy the exact pattern used by neighboring handlers (e.g. `sync_quotes_async`).
- `create_sync_job(job_id, "_optimize", 0)` starts with total 0; `update_sync_job` carries `total` only via the `current`/`status` columns — if the job row needs `total` set, call an update once `total` is known (the quotes job sets total at create; here set it inside `work()` by re-creating or accept total stays as the count via the status payload — keep it consistent with the quotes job's column usage).
- The DB connection is shared with the event loop's other handlers; the executor thread does blocking DB writes. The DB layer already materializes results under a lock — but confirm `update_sync_job`/`save_optimization` from a worker thread is safe with the app's connection (the bootstrap-stocks pattern opens a fresh `_open_db()` per thread; if writes from the executor conflict, open a thread-local DB inside `work()` the same way).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_api_optimize.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
ruff check quanti/api/routes.py tests/test_api_optimize.py
git add quanti/api/routes.py tests/test_api_optimize.py
git commit -m "feat(api): async optimize job + status + tuned-params endpoints"
```

---

## Task 9: Frontend API client

**Files:**
- Modify: `web/src/api/client.ts`

**Interfaces:**
- Produces: `OptimizeResultItem`, `OptimizeStatus` interfaces; `runOptimizeAsync()`, `fetchOptimizeStatus(jobId)`, `fetchTunedParams()`.

- [ ] **Step 1: Add the types and calls**

Append to `web/src/api/client.ts` (before `export default api;`):

```ts
// --- Hyperopt / tuned params ---
export interface OptimizeResultItem {
  strategy_name: string;
  params: Record<string, unknown>;
  oos_sharpe: number;
  baseline_oos_sharpe: number;
  accepted: boolean;
  n_combos: number;
  universe_size: number;
  tuned_at: string;
}

export interface OptimizeStatus {
  job_id: string;
  current: number;
  total: number;
  status: string; // "running" | "done" | "error"
  current_strategy: string;
  results: OptimizeResultItem[];
}

export const runOptimizeAsync = () =>
  api.post<{ job_id: string }>("/agent/optimize/async");

export const fetchOptimizeStatus = (jobId: string) =>
  api.get<OptimizeStatus>("/agent/optimize/status", { params: { job_id: jobId } });

export const fetchTunedParams = () =>
  api.get<OptimizeResultItem[]>("/agent/tuned-params");
```

- [ ] **Step 2: Verify type-check passes**

Run: `cd web && npm run build`
Expected: build succeeds (no TS errors).

- [ ] **Step 3: Commit**

```bash
git add web/src/api/client.ts
git commit -m "feat(web): hyperopt API client (optimize job + tuned params)"
```

---

## Task 10: Agent view optimize card

**Files:**
- Modify: `web/src/views/Agent.vue`

**Interfaces:**
- Consumes: `runOptimizeAsync`, `fetchOptimizeStatus`, `fetchTunedParams` (Task 9).

- [ ] **Step 1: Add the optimize card + polling + badge**

In `web/src/views/Agent.vue`:

1. Import the new client calls alongside the existing imports (`fetchAgentStatus`, …):
   ```ts
   import { runOptimizeAsync, fetchOptimizeStatus, fetchTunedParams,
            type OptimizeResultItem } from "../api/client";
   ```
2. Add reactive state (mirror the existing polling state for sync jobs in this codebase — see how a job is polled with `setInterval` + a status call; reuse that pattern):
   ```ts
   const tuned = ref<OptimizeResultItem[]>([]);
   const optimizing = ref(false);
   const optProgress = ref<{ current: number; total: number; strategy: string }>(
     { current: 0, total: 0, strategy: "" });
   let optTimer: number | undefined;

   const loadTuned = async () => { tuned.value = (await fetchTunedParams()).data; };
   const startOptimize = async () => {
     optimizing.value = true;
     const { data } = await runOptimizeAsync();
     const jobId = data.job_id;
     optTimer = window.setInterval(async () => {
       const s = (await fetchOptimizeStatus(jobId)).data;
       optProgress.value = { current: s.current, total: s.total, strategy: s.current_strategy };
       tuned.value = s.results;
       if (s.status === "done" || s.status === "error") {
         window.clearInterval(optTimer);
         optimizing.value = false;
       }
     }, 1500);
   };
   ```
   Call `loadTuned()` in the existing `onMounted`/init alongside `fetchAgentStatus()`, and clear `optTimer` in `onUnmounted`.
3. Add a card in the template near the "最近策略评估" card:
   ```html
   <div class="card">
     <div class="card-header">
       <h2>参数优化</h2>
       <button class="btn-secondary" :disabled="optimizing" @click="startOptimize">
         {{ optimizing ? `优化中 ${optProgress.current}/${optProgress.total} ${optProgress.strategy}` : "运行优化" }}
       </button>
     </div>
     <table v-if="tuned.length">
       <thead><tr><th>策略</th><th>默认 OOS</th><th>调优 OOS</th><th>采纳</th><th>参数</th><th>组合</th><th>时间</th></tr></thead>
       <tbody>
         <tr v-for="t in tuned" :key="t.strategy_name">
           <td>{{ t.strategy_name }}</td>
           <td>{{ t.baseline_oos_sharpe?.toFixed(2) }}</td>
           <td>{{ t.oos_sharpe?.toFixed(2) }}</td>
           <td>{{ t.accepted ? "✓" : "—" }}</td>
           <td>{{ t.accepted ? JSON.stringify(t.params) : "默认" }}</td>
           <td>{{ t.n_combos }}</td>
           <td>{{ t.tuned_at?.slice(0, 16).replace("T", " ") }}</td>
         </tr>
       </tbody>
     </table>
     <p v-else class="muted">尚未优化。点击"运行优化"在样本外验证各策略参数。</p>
   </div>
   ```
4. Add a "已调优" badge in the existing "最近策略评估" table: compute a set of accepted strategy names and show a small tag next to matching rows:
   ```ts
   const tunedNames = computed(() =>
     new Set(tuned.value.filter(t => t.accepted).map(t => t.strategy_name)));
   ```
   In the evaluations `<tr v-for="e in agent.last_evaluations">`, add next to the name:
   ```html
   <span v-if="tunedNames.has(e.strategy_name)" class="count-badge">已调优</span>
   ```

(Follow the file's existing `<script setup>` style, `ref`/`computed`/`onMounted`/`onUnmounted` usage, and CSS classes already defined — `card`, `card-header`, `btn-secondary`, `count-badge`, `muted`. Don't invent new global styles unless needed.)

- [ ] **Step 2: Verify type-check / build passes**

Run: `cd web && npm run build`
Expected: build succeeds.

- [ ] **Step 3: Manual smoke (optional, if dev server available)**

Run the stack, open `/agent`, click 运行优化, confirm the progress label advances and the results table fills. (No frontend unit-test infra in this repo — verify via build + manual.)

- [ ] **Step 4: Commit**

```bash
git add web/src/views/Agent.vue
git commit -m "feat(web): Agent view optimize card + tuned badge"
```

---

## Task 11: Full regression + docs

**Files:**
- Modify: `docs/2026-06-20-reference-mature-quant-systems.md` (mark ⑤ done) — optional one-line.

- [ ] **Step 1: Full Python suite**

Run: `python -m pytest -q`
Expected: PASS (previous baseline + all new hyperopt tests). If any failure traces to the resolve_params wiring, fix it; if unrelated/pre-existing, note it.

- [ ] **Step 2: Lint + frontend build**

Run: `ruff check quanti tests` and `cd web && npm run build`
Expected: both clean.

- [ ] **Step 3: Doc touch**

In `docs/2026-06-20-reference-mature-quant-systems.md`, mark item ⑤ as implemented (one line referencing the spec/plan).

- [ ] **Step 4: Commit**

```bash
git add docs/2026-06-20-reference-mature-quant-systems.md
git commit -m "docs: mark borrow-list ⑤ (走查调参) implemented"
```

- [ ] **Step 5: Push + PR**

```bash
git push -u origin feat/strategy-hyperopt
# open PR feat/strategy-hyperopt -> main
```

---

## Self-Review (completed by plan author)

- **Spec coverage:** §2 trigger (on-demand) → Tasks 7, 8. §3 A1 core (train→OOS validate→accept gate, no-overlap split) → Task 3. §4 param_space → Task 1. §5 persistence → Task 4. §6 resolve_params + every init site → Tasks 5, 6. §7 UI → Tasks 9, 10. §8 backend interfaces (HyperOptimizer, CLI, async API, tuned-params) → Tasks 3, 7, 8. §9 defaults → Task 3 constructor defaults + Global Constraints. §10 cost/caps/logging → Task 2 (cap+log), Task 7/8 (universe cap). §11 testing → each task's tests + Task 11 regression.
- **Type consistency:** `OptimizeResult` fields defined in Task 3 are consumed verbatim in Tasks 7 (`r.chosen_params`, `r.tuned_oos_sharpe`, …) and 8. `save_optimization`/`get_active_params`/`list_optimization_results` signatures defined in Task 4 are consumed in Tasks 6 (`get_active_params` via resolve_params), 7, 8. `resolve_params(db, name, goal)` defined Task 5, consumed Task 6. Frontend `OptimizeResultItem`/`OptimizeStatus` (Task 9) consumed in Task 10. Endpoint paths (`/agent/optimize/async`, `/agent/optimize/status`, `/agent/tuned-params`) consistent across Tasks 8–10.
- **Placeholder scan:** Integration tasks (6, 7, 8, 10) carry "confirm the actual attribute/pattern" notes (db handle name, job status vocabulary, frontend polling style) rather than blanks — each names the existing code to mirror. The core logic tasks (1–5) contain complete code.
- **Known soft spots flagged for the implementer:** the exact db-access pattern in routes, the Selector's db attribute, and the frontend job-polling idiom must be matched to existing code — each is called out in its task.
