# LLM Factor Mining (v1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A closed loop where an LLM proposes factor expressions, a safe parser turns them into the (Task-②) DSL, a train/OOS rank-IC gate accepts only the genuinely predictive ones into a self-evolving library, and accepted factors join the composite only when a per-account switch (UI-visible, live-default-off) is on.

**Architecture:** LLM text → `parse_expr` (AST whitelist, never `eval`) → ② `Expr` → `factor_ic` (rank-IC, train filter + OOS gate + redundancy) → `generated_factors` table → `compute_factor_panel(include_generated=…)` merges accepted+enabled factors when the account's `use_generated_factors` flag is on. Triggered on-demand by CLI + an async API job; surfaced in the Agent view with a master switch + per-factor toggles.

**Tech Stack:** Python 3.13, stdlib `ast`, pandas+numpy (rank-IC without scipy), the ② factor DSL, FastAPI, Vue 3. No new dependencies.

**Dependency:** This branch is based on `feat/factor-pipeline` (the ② DSL: `quanti/factors/expr.py` with `Close/Open/High/Low/Volume/Turnover`, `Ref/Mean/Std/Sum/Max/Min/Log`, `Constant`, arithmetic; and `quanti/factors/library.py` with `as_factor_fn`, `evaluate_series`).

## Global Constraints

- **LLM generates factor EXPRESSIONS only** — never strategy/Python code. The only thing executed is `parse_expr` (AST whitelist); **no `eval`/`exec`** anywhere.
- Parser whitelist: names `close/open/high/low/volume/turnover`; functions `Ref/Mean/Std/Sum/Max/Min/Log`; ops `+ - * /` and unary `-`; numeric constants. Window args must be **positive int constants**. Everything else (attributes, subscripts, `**`, calls to other names, comprehensions, lambdas, kwargs) raises `FactorParseError`. Bounded length + depth.
- Acceptance gate (exact): `accepted = (abs(train_ic) >= min_train_ic) AND (oos_ic >= oos_ic_threshold) AND (max abs cross-sectional corr with each already-accepted factor < redundancy_max)`. Otherwise not accepted (still persisted with reason).
- rank-IC = Pearson correlation of cross-sectional **ranks** (no scipy); forward return uses future closes — legitimate as a research/evaluation metric; the factor itself stays ②-look-ahead-safe.
- Accepted factors are stored; they affect a given account's trading composite ONLY when that account's `goal.params["use_generated_factors"]` is True (**default False / live-off**) AND the per-factor `enabled` flag is on. Both switches are UI-visible.
- Mining is on-demand (CLI + async API), never per agent cycle. LLM unavailable (no key / not installed) → graceful skip.
- Defaults: `fwd_days=5`, `oos_ic_threshold=0.03`, `min_train_ic=0.02`, `redundancy_max=0.7`, `n_candidates=10`, universe cap = `selector_max_universe`(100).
- Keep existing tests green; `ruff check` clean; `cd web && npm run build` clean.

Spec: `docs/superpowers/specs/2026-06-21-llm-factor-mining-design.md`.

---

## File Structure

- **Create** `quanti/factors/parser.py` — `parse_expr` + `FactorParseError` (Task 1).
- **Create** `quanti/factors/evaluation.py` — `factor_ic` + rank-IC helper (Task 2).
- **Modify** `quanti/data/database.py` — `generated_factors` table + CRUD + `load_active_factor_fns` (Task 3).
- **Modify** `quanti/factors/cross_sectional.py` + `quanti/agent/runtime.py` — `include_generated` flag + wire the account switch (Task 4).
- **Create** `quanti/agent/factor_miner.py` — `mine_factors` + `MineResult` + prompt (Task 5).
- **Modify** `quanti/cli.py` — `mine-factors` command (Task 6).
- **Modify** `quanti/api/routes.py` — async mine job + status + generated-factors list/toggle (Task 7).
- **Modify** `web/src/api/client.ts` (Task 8), `web/src/views/Agent.vue` (Task 9).
- **Modify** docs (Task 10).

---

## Task 1: Safe factor expression parser

**Files:**
- Create: `quanti/factors/parser.py`
- Test: `tests/test_factor_parser.py`

**Interfaces:**
- Consumes: ② `expr` nodes.
- Produces: `parse_expr(s: str) -> Expr`; `FactorParseError(ValueError)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_factor_parser.py
from __future__ import annotations

import pandas as pd
import pytest

from quanti.factors.expr import EvalContext
from quanti.factors.parser import FactorParseError, parse_expr


def _ctx(closes):
    n = len(closes)
    dates = pd.bdate_range(end=pd.Timestamp("2025-01-01"), periods=n)
    return EvalContext(pd.DataFrame({
        "date": [d.date() for d in dates],
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1.0] * n, "turnover": [1.0] * n}))


def test_parses_valid_expression_equivalently():
    expr = parse_expr("Ref(close, 21) / Ref(close, 63) - 1")
    ctx = _ctx([float(i) + 1 for i in range(80)])
    from quanti.factors.expr import Close, Ref
    ref = Ref(Close(), 21) / Ref(Close(), 63) - 1
    assert expr.evaluate(ctx).equals(ref.evaluate(ctx))


def test_parses_nested_funcs_and_neg_and_scalars():
    expr = parse_expr("-Std(Log(close / Ref(close, 1)), 20) * 16")
    ctx = _ctx([float(i) + 1 for i in range(40)])
    s = expr.evaluate(ctx)
    assert len(s) == 40  # evaluates without error


@pytest.mark.parametrize("bad", [
    "__import__('os').system('rm -rf /')",
    "os.system('x')",
    "close.__class__",
    "open(close)",            # 'open' is a field name, not callable
    "Foo(close, 5)",          # unknown function
    "Ref(close, -1)",         # negative window
    "Ref(close, 5.5)",        # non-int window
    "Ref(close, n)",          # non-constant window
    "[close for x in close]", # comprehension
    "close ** 2",             # power operator not allowed
    "Mean(close, 5, 6)",      # wrong arity
    "Ref(close, n=5)",        # kwargs
    "price",                  # unknown name
])
def test_rejects_unsafe_or_unknown(bad):
    with pytest.raises(FactorParseError):
        parse_expr(bad)


def test_rejects_overlong():
    with pytest.raises(FactorParseError):
        parse_expr("close+" * 500 + "close")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_factor_parser.py -q`
Expected: FAIL — `ModuleNotFoundError: quanti.factors.parser`.

- [ ] **Step 3: Write the parser**

```python
# quanti/factors/parser.py
"""Safe parser: LLM factor expression string -> ② Expr.

NEVER eval/exec. Uses ast.parse + a strict whitelist recursive descent: only
the DSL's data fields, the time-series functions, arithmetic, unary minus, and
numeric constants are allowed. Anything else raises FactorParseError. This is
the security boundary for LLM-generated factors (⑥)."""

from __future__ import annotations

import ast

from quanti.factors.expr import (
    Close, Constant, Expr, High, Log, Low, Max, Mean, Min, Open, Ref, Std,
    Sum, Turnover, Volume,
)

MAX_LEN = 400
MAX_DEPTH = 25


class FactorParseError(ValueError):
    """Raised when an expression string is malformed or uses anything outside
    the DSL whitelist."""


_FIELDS = {"close": Close, "open": Open, "high": High, "low": Low,
           "volume": Volume, "turnover": Turnover}
_WINDOW_FUNCS = {"Ref": Ref, "Mean": Mean, "Std": Std, "Sum": Sum,
                 "Max": Max, "Min": Min}
_UNARY_FUNCS = {"Log": Log}


def parse_expr(s: str) -> Expr:
    if not isinstance(s, str):
        raise FactorParseError("expression must be a string")
    s = s.strip()
    if not s or len(s) > MAX_LEN:
        raise FactorParseError(f"expression empty or too long (>{MAX_LEN})")
    try:
        tree = ast.parse(s, mode="eval")
    except SyntaxError as e:
        raise FactorParseError(f"syntax error: {e}") from e
    return _build(tree.body, 0)


def _window_int(node: ast.AST) -> int:
    if not isinstance(node, ast.Constant) or isinstance(node.value, bool) \
            or not isinstance(node.value, int):
        raise FactorParseError("window must be an integer constant")
    if node.value < 1:
        raise FactorParseError("window must be a positive integer")
    return int(node.value)


def _build(node: ast.AST, depth: int) -> Expr:
    if depth > MAX_DEPTH:
        raise FactorParseError("expression too deeply nested")

    if isinstance(node, ast.Name):
        ctor = _FIELDS.get(node.id)
        if ctor is None:
            raise FactorParseError(f"unknown name: {node.id!r}")
        return ctor()

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise FactorParseError(f"unsupported constant: {node.value!r}")
        return Constant(float(node.value))

    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.USub):
            return -_build(node.operand, depth + 1)
        if isinstance(node.op, ast.UAdd):
            return _build(node.operand, depth + 1)
        raise FactorParseError("unsupported unary operator")

    if isinstance(node, ast.BinOp):
        left = _build(node.left, depth + 1)
        right = _build(node.right, depth + 1)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        raise FactorParseError("unsupported binary operator (only + - * /)")

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise FactorParseError("only direct function calls allowed")
        if node.keywords:
            raise FactorParseError("keyword arguments not allowed")
        name = node.func.id
        if name in _WINDOW_FUNCS:
            if len(node.args) != 2:
                raise FactorParseError(f"{name} takes (expr, window)")
            return _WINDOW_FUNCS[name](_build(node.args[0], depth + 1),
                                       _window_int(node.args[1]))
        if name in _UNARY_FUNCS:
            if len(node.args) != 1:
                raise FactorParseError(f"{name} takes (expr)")
            return _UNARY_FUNCS[name](_build(node.args[0], depth + 1))
        raise FactorParseError(f"unknown function: {name!r}")

    raise FactorParseError(f"unsupported syntax: {type(node).__name__}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_factor_parser.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
.venv/Scripts/python.exe -m ruff check quanti/factors/parser.py tests/test_factor_parser.py
git add quanti/factors/parser.py tests/test_factor_parser.py
git commit -m "feat(factors): safe AST-whitelist parser for LLM factor expressions"
```

---

## Task 2: rank-IC evaluator

**Files:**
- Create: `quanti/factors/evaluation.py`
- Test: `tests/test_factor_evaluation.py`

**Interfaces:**
- Consumes: ② `Expr`, `evaluate_series` (`quanti.factors.library`); `DataProvider.get_daily_df`.
- Produces:
  - `rank_ic(factor_vals: dict[str, float], fwd_rets: dict[str, float]) -> float` — cross-sectional rank IC of one date's cross-section.
  - `factor_ic(expr, provider, codes, start, end, *, fwd_days=5, lookback_days=200, min_names=5) -> float` — mean rank-IC over eval dates in [start, end]; NaN if too few.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_factor_evaluation.py
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from quanti.factors.evaluation import factor_ic, rank_ic
from quanti.factors.expr import Close, Ref


def test_rank_ic_perfect_and_zero():
    # factor ranks exactly match forward-return ranks → IC = 1.
    fac = {"a": 1.0, "b": 2.0, "c": 3.0}
    fwd = {"a": 0.1, "b": 0.2, "c": 0.3}
    assert rank_ic(fac, fwd) == pytest.approx(1.0)
    # reversed → -1.
    assert rank_ic(fac, {"a": 0.3, "b": 0.2, "c": 0.1}) == pytest.approx(-1.0)


def test_rank_ic_too_few_names_is_nan():
    assert np.isnan(rank_ic({"a": 1.0}, {"a": 0.1}))


class _Provider:
    """Returns per-code synthetic bars where momentum predicts forward return
    for the 'good' codes (so a momentum factor has positive IC)."""
    def __init__(self, data):  # data: code -> list[float] closes
        self._data = data
        n = max(len(v) for v in data.values())
        self._dates = [d.date() for d in pd.bdate_range(end=pd.Timestamp("2025-06-01"), periods=n)]

    def get_daily_df(self, code, start, end):
        closes = self._data.get(code, [])
        df = pd.DataFrame({"date": self._dates[:len(closes)],
                           "open": closes, "high": closes, "low": closes,
                           "close": closes, "volume": [1.0]*len(closes),
                           "turnover": [1.0]*len(closes)})
        return df[(df["date"] >= start) & (df["date"] <= end)]


def test_factor_ic_positive_for_predictive_factor():
    # Build 6 codes; trending-up codes keep trending (momentum predictive).
    rng = np.random.default_rng(0)
    data = {}
    for i in range(6):
        drift = 0.5 if i < 3 else -0.5
        data[f"c{i}"] = list(100 + np.cumsum(np.full(150, drift) + rng.normal(0, 0.1, 150)))
    prov = _Provider(data)
    expr = Ref(Close(), 1) / Ref(Close(), 21) - 1   # ~1m momentum
    ic = factor_ic(expr, prov, list(data), date(2025, 1, 1), date(2025, 5, 1),
                   fwd_days=5, min_names=4)
    assert ic > 0


import pytest  # at top in the real file
```

(Move `import pytest` to the top imports when writing the file.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_factor_evaluation.py -q`
Expected: FAIL — `ModuleNotFoundError: quanti.factors.evaluation`.

- [ ] **Step 3: Write the evaluator**

```python
# quanti/factors/evaluation.py
"""Factor evaluation: cross-sectional rank-IC (information coefficient).

IC measures whether a factor's value at t predicts the forward return t→t+N.
It is a research metric computed on history (the future is known there), so it
legitimately uses forward returns; the factor itself remains ②-look-ahead-safe.
rank-IC = Pearson correlation of cross-sectional ranks (Spearman), computed
with pandas/numpy (no scipy)."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd

from quanti.factors.expr import Expr
from quanti.factors.library import evaluate_series

logger = logging.getLogger(__name__)


def rank_ic(factor_vals: dict[str, float], fwd_rets: dict[str, float],
            min_names: int = 5) -> float:
    """Cross-sectional rank IC for one date. NaN if < min_names paired names."""
    codes = [c for c in factor_vals
             if c in fwd_rets
             and not pd.isna(factor_vals[c]) and not pd.isna(fwd_rets[c])]
    if len(codes) < min_names:
        return float("nan")
    f = pd.Series([factor_vals[c] for c in codes]).rank()
    r = pd.Series([fwd_rets[c] for c in codes]).rank()
    if f.std() == 0 or r.std() == 0:
        return float("nan")
    return float(np.corrcoef(f, r)[0, 1])


def factor_ic(expr: Expr, provider, codes: list[str], start: date, end: date,
              *, fwd_days: int = 5, lookback_days: int = 200,
              min_names: int = 5) -> float:
    """Mean cross-sectional rank-IC over the trading dates in [start, end].

    For each code, evaluate the factor series (② batch) and the forward return
    series once, then assemble each date's cross-section. NaN if no scorable
    dates."""
    fac_by_code: dict[str, pd.Series] = {}
    fwd_by_code: dict[str, pd.Series] = {}
    fetch_start = start - timedelta(days=lookback_days)
    fetch_end = end + timedelta(days=fwd_days * 3 + 7)  # room for forward return
    for code in codes:
        bars = provider.get_daily_df(code, fetch_start, fetch_end)
        if bars is None or bars.empty or len(bars) < 2:
            continue
        bars = bars.sort_values("date")
        s = evaluate_series(expr, bars)              # date-indexed factor
        closes = bars.set_index("date")["close"].astype(float)
        fwd = closes.shift(-fwd_days) / closes - 1.0  # forward return (research)
        fac_by_code[code] = s
        fwd_by_code[code] = fwd

    if not fac_by_code:
        return float("nan")

    all_dates = sorted({d for s in fac_by_code.values() for d in s.index
                        if start <= d <= end})
    ics: list[float] = []
    for d in all_dates:
        fvals = {c: fac_by_code[c].get(d) for c in fac_by_code if d in fac_by_code[c].index}
        rvals = {c: fwd_by_code[c].get(d) for c in fwd_by_code if d in fwd_by_code[c].index}
        ic = rank_ic(fvals, rvals, min_names=min_names)
        if not np.isnan(ic):
            ics.append(ic)
    return float(np.mean(ics)) if ics else float("nan")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_factor_evaluation.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
.venv/Scripts/python.exe -m ruff check quanti/factors/evaluation.py tests/test_factor_evaluation.py
git add quanti/factors/evaluation.py tests/test_factor_evaluation.py
git commit -m "feat(factors): rank-IC evaluator for factor quality"
```

---

## Task 3: generated_factors persistence

**Files:**
- Modify: `quanti/data/database.py` (CREATE TABLE block; methods near `strategy_params`/`get_sync_job`)
- Test: `tests/test_generated_factors_db.py`

**Interfaces:**
- Consumes: `parse_expr` (Task 1), `as_factor_fn` (②).
- Produces:
  - `save_generated_factor(name, expr_str, train_ic, oos_ic, accepted, enabled=True) -> None` (upsert).
  - `list_generated_factors() -> list[dict]` (keys: name, expr_str, train_ic, oos_ic, accepted(bool), enabled(bool), created_at).
  - `set_factor_enabled(name, enabled: bool) -> None`.
  - `load_active_factor_fns() -> dict[str, FactorFn]` — for rows with accepted=1 AND enabled=1, `{name: as_factor_fn(parse_expr(expr_str))}`; rows whose expr fails to parse are skipped (logged).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_generated_factors_db.py
from __future__ import annotations

from quanti.data.database import Database


def _db(tmp_path):
    db = Database(str(tmp_path / "g.db")); db.initialize(); return db


def test_save_list_toggle_and_load_active(tmp_path):
    db = _db(tmp_path)
    db.save_generated_factor("llm_mom", "Ref(close,21)/Ref(close,126)-1",
                             train_ic=0.05, oos_ic=0.04, accepted=True)
    db.save_generated_factor("llm_bad", "-Mean(turnover,20)",
                             train_ic=0.01, oos_ic=0.0, accepted=False)
    rows = {r["name"]: r for r in db.list_generated_factors()}
    assert rows["llm_mom"]["accepted"] is True and rows["llm_mom"]["enabled"] is True
    assert rows["llm_bad"]["accepted"] is False
    # active = accepted & enabled
    active = db.load_active_factor_fns()
    assert set(active) == {"llm_mom"}
    # toggling enabled off removes it from active
    db.set_factor_enabled("llm_mom", False)
    assert db.load_active_factor_fns() == {}


def test_load_active_skips_unparseable(tmp_path):
    db = _db(tmp_path)
    db.save_generated_factor("broken", "os.system('x')", 0.1, 0.1, accepted=True)
    assert db.load_active_factor_fns() == {}  # parse fails → skipped, no crash


def test_save_is_upsert(tmp_path):
    db = _db(tmp_path)
    db.save_generated_factor("f", "close", 0.1, 0.1, accepted=True)
    db.save_generated_factor("f", "-close", 0.2, 0.2, accepted=True)
    rows = db.list_generated_factors()
    assert len(rows) == 1 and rows[0]["expr_str"] == "-close"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_generated_factors_db.py -q`
Expected: FAIL — `AttributeError: save_generated_factor`.

- [ ] **Step 3: Add the table + methods**

In `quanti/data/database.py`, add to the schema block (near `strategy_params`):

```python
            CREATE TABLE IF NOT EXISTS generated_factors (
                name TEXT PRIMARY KEY,
                expr_str TEXT NOT NULL,
                train_ic REAL,
                oos_ic REAL,
                accepted INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
```

Add the methods (near `strategy_params` methods):

```python
    def save_generated_factor(self, name: str, expr_str: str, train_ic: float,
                              oos_ic: float, accepted: bool,
                              enabled: bool = True) -> None:
        from datetime import datetime
        self.conn.execute(
            "INSERT OR REPLACE INTO generated_factors "
            "(name, expr_str, train_ic, oos_ic, accepted, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, expr_str, _nan_to_none(train_ic), _nan_to_none(oos_ic),
             1 if accepted else 0, 1 if enabled else 0,
             datetime.now().isoformat()),
        )
        self.conn.commit()

    def list_generated_factors(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT name, expr_str, train_ic, oos_ic, accepted, enabled, "
            "created_at FROM generated_factors ORDER BY oos_ic DESC").fetchall()
        return [{"name": r[0], "expr_str": r[1], "train_ic": r[2],
                 "oos_ic": r[3], "accepted": bool(r[4]), "enabled": bool(r[5]),
                 "created_at": r[6]} for r in rows]

    def set_factor_enabled(self, name: str, enabled: bool) -> None:
        self.conn.execute(
            "UPDATE generated_factors SET enabled=? WHERE name=?",
            (1 if enabled else 0, name))
        self.conn.commit()

    def load_active_factor_fns(self) -> dict:
        from quanti.factors.library import as_factor_fn
        from quanti.factors.parser import FactorParseError, parse_expr
        rows = self.conn.execute(
            "SELECT name, expr_str FROM generated_factors "
            "WHERE accepted=1 AND enabled=1").fetchall()
        out = {}
        for name, expr_str in rows:
            try:
                out[name] = as_factor_fn(parse_expr(expr_str))
            except FactorParseError:
                logger.warning("skipping unparseable generated factor %s: %s",
                               name, expr_str)
        return out
```

Add a module-level helper near the top of `database.py` (or reuse an existing one) if `_nan_to_none` doesn't exist:

```python
def _nan_to_none(x):
    import math
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)
```

(Ensure `logger` exists in `database.py`; it uses logging elsewhere. If not, add `import logging; logger = logging.getLogger(__name__)`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_generated_factors_db.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
.venv/Scripts/python.exe -m ruff check quanti/data/database.py tests/test_generated_factors_db.py
git add quanti/data/database.py tests/test_generated_factors_db.py
git commit -m "feat(db): generated_factors store + load_active_factor_fns"
```

---

## Task 4: Wire generated factors into the panel (gated)

**Files:**
- Modify: `quanti/factors/cross_sectional.py` (`compute_factor_panel` signature ~131)
- Modify: `quanti/agent/runtime.py` (`_compute_fused_candidates`, the panel call ~482)
- Test: `tests/test_factors_cross.py` (append) — generated factor enters the panel when included

**Interfaces:**
- Consumes: `Database.load_active_factor_fns` (Task 3).
- Produces: `compute_factor_panel(provider, db, codes, as_of=None, config=None, include_generated: bool = False)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factors_cross.py — append (reuses the panel_db fixture)
def test_generated_factor_enters_panel_only_when_included(panel_db):
    from quanti.data.provider import DataProvider
    from quanti.factors.cross_sectional import compute_factor_panel
    provider = DataProvider(panel_db)
    # An accepted+enabled generated factor.
    panel_db.save_generated_factor("llm_x", "-Mean(close,5)", 0.05, 0.04,
                                   accepted=True)
    codes = ["A_trend", "B_trend", "C_choppy", "D_decline"]
    base = compute_factor_panel(provider, panel_db, codes)
    incl = compute_factor_panel(provider, panel_db, codes, include_generated=True)
    assert "llm_x" not in base.columns
    assert "llm_x" in incl.columns
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_factors_cross.py::test_generated_factor_enters_panel_only_when_included -q`
Expected: FAIL — `compute_factor_panel() got an unexpected keyword argument 'include_generated'`.

- [ ] **Step 3: Implement**

In `quanti/factors/cross_sectional.py`, change `compute_factor_panel`'s signature to add `include_generated: bool = False`, and build the factor-fn map at the top of the body:

```python
def compute_factor_panel(
    provider: DataProvider,
    db: Database,
    codes: list[str],
    as_of: date | None = None,
    config: FactorConfig | None = None,
    include_generated: bool = False,
) -> pd.DataFrame:
```

Inside, where it currently does `factor_fns = cfg.resolved()`, replace with:

```python
    factor_fns = dict(cfg.resolved())
    if include_generated:
        factor_fns.update(db.load_active_factor_fns())
```

Everything else (per-code loop over `factor_fns`, winsorize/zscore/industry-demean/composite) is unchanged — generated factors flow through the same pipeline automatically.

In `quanti/agent/runtime.py`, at the panel call (~482), pass the per-account switch:

```python
        panel = compute_factor_panel(
            self._provider, self._db, candidates,
            config=FactorConfig(industry_neutralize=industry_neutral),
            include_generated=bool(params.get("use_generated_factors", False)))
```

(`params` is already `goal.params or {}` in that method.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_factors_cross.py tests/test_agent.py -q`
Expected: PASS. (Existing panel tests unchanged: `include_generated` defaults False; the agent path defaults the switch False.)

- [ ] **Step 5: Lint + commit**

```bash
.venv/Scripts/python.exe -m ruff check quanti/factors/cross_sectional.py quanti/agent/runtime.py tests/test_factors_cross.py
git add quanti/factors/cross_sectional.py quanti/agent/runtime.py tests/test_factors_cross.py
git commit -m "feat(factors): compute_factor_panel include_generated gated by account switch"
```

---

## Task 5: The factor miner

**Files:**
- Create: `quanti/agent/factor_miner.py`
- Test: `tests/test_factor_miner.py`

**Interfaces:**
- Consumes: `parse_expr`/`FactorParseError` (Task 1); `factor_ic` (Task 2); ② `evaluate_series`/`as_factor_fn`; `Database.save_generated_factor`/`list_generated_factors`; `_complete_text`/`LLMConfig` (`quanti.agent.llm_runtime`).
- Produces:
  - `MineResult(name, expr_str, train_ic, oos_ic, accepted, reason)` dataclass.
  - `parse_llm_factors(text: str) -> list[tuple[str, str]]` — parse `name: expr` lines from LLM output.
  - `mine_factors(llm, db, provider, codes, end, *, n_candidates=10, fwd_days=5, oos_ic_threshold=0.03, min_train_ic=0.02, redundancy_max=0.7, train_days=252, oos_days=63, cfg=None) -> list[MineResult]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factor_miner.py
from __future__ import annotations

from datetime import date

import pandas as pd

from quanti.agent.factor_miner import MineResult, mine_factors, parse_llm_factors


def test_parse_llm_factors_extracts_name_expr_lines():
    text = ("Here are factors:\n"
            "mom_fast: Ref(close, 5) / Ref(close, 20) - 1\n"
            "vol_low: -Std(close, 20)\n"
            "garbage line without colon\n")
    out = parse_llm_factors(text)
    assert ("mom_fast", "Ref(close, 5) / Ref(close, 20) - 1") in out
    assert ("vol_low", "-Std(close, 20)") in out
    assert len(out) == 2


class _LLM:
    def __init__(self, text): self._text = text
    def create_message(self, **kw):
        return {"content": [{"type": "text", "text": self._text}],
                "stop_reason": "end_turn", "usage": {}}


class _Provider:
    def __init__(self, data):
        self._data = data
        n = max(len(v) for v in data.values())
        self._dates = [d.date() for d in pd.bdate_range(end=pd.Timestamp("2025-06-01"), periods=n)]
    def get_daily_df(self, code, start, end):
        c = self._data.get(code, [])
        df = pd.DataFrame({"date": self._dates[:len(c)], "open": c, "high": c,
                           "low": c, "close": c, "volume": [1.0]*len(c),
                           "turnover": [1.0]*len(c)})
        return df[(df["date"] >= start) & (df["date"] <= end)]


def _seed_provider():
    import numpy as np
    rng = np.random.default_rng(1)
    data = {}
    for i in range(6):
        drift = 0.5 if i < 3 else -0.5
        data[f"c{i}"] = list(100 + np.cumsum(np.full(180, drift) + rng.normal(0, 0.1, 180)))
    return _Provider(data), list(data)


def test_mine_accepts_predictive_and_rejects_unparseable(tmp_path):
    from quanti.data.database import Database
    db = Database(str(tmp_path / "m.db")); db.initialize()
    provider, codes = _seed_provider()
    # One predictive momentum factor + one unparseable.
    llm = _LLM("good_mom: Ref(close,1)/Ref(close,21)-1\n"
               "evil: __import__('os').system('x')\n")
    results = mine_factors(llm, db, provider, codes, date(2025, 5, 20),
                           n_candidates=5, oos_ic_threshold=0.0, min_train_ic=0.0)
    by = {r.name: r for r in results}
    assert "good_mom" in by  # parsed + scored
    assert "evil" not in by  # unparseable → dropped before scoring
    # persisted
    saved = {r["name"] for r in db.list_generated_factors()}
    assert "good_mom" in saved


def test_mine_graceful_when_llm_returns_nothing(tmp_path):
    from quanti.data.database import Database
    db = Database(str(tmp_path / "m.db")); db.initialize()
    provider, codes = _seed_provider()
    results = mine_factors(_LLM(""), db, provider, codes, date(2025, 5, 20))
    assert results == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_factor_miner.py -q`
Expected: FAIL — `ModuleNotFoundError: quanti.agent.factor_miner`.

- [ ] **Step 3: Write the miner**

```python
# quanti/agent/factor_miner.py
"""LLM factor mining: LLM proposes factor expressions, a safe parser + rank-IC
gate accept only the predictive, non-redundant ones into the generated_factors
library. The LLM only ADDS candidates; rules (parse whitelist + OOS IC) decide.
On-demand (CLI / async API), never per agent cycle."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from quanti.agent.llm_runtime import LLMConfig, _complete_text
from quanti.factors.cross_sectional import DEFAULT_FACTORS
from quanti.factors.evaluation import factor_ic
from quanti.factors.library import evaluate_series
from quanti.factors.parser import FactorParseError, parse_expr

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a quant researcher proposing cross-sectional alpha factors for "
    "A-share daily bars, expressed in a tiny DSL.\n"
    "Allowed data: close, open, high, low, volume, turnover.\n"
    "Allowed functions: Ref(x, n) lag n bars; Mean(x, n); Std(x, n); Sum(x, n); "
    "Max(x, n); Min(x, n); Log(x). Operators: + - * / and unary minus. "
    "Integer windows only. No ** and no other names/functions.\n"
    "Higher factor value must mean 'more attractive' (sign-flip mean-reverting "
    "ideas). Propose DIVERSE ideas, not variations of one.\n"
    "Output ONLY lines of `name: expression`, nothing else."
)


@dataclass
class MineResult:
    name: str
    expr_str: str
    train_ic: float
    oos_ic: float
    accepted: bool
    reason: str


def parse_llm_factors(text: str) -> list[tuple[str, str]]:
    """Extract `name: expression` pairs from LLM output, one per line."""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip().lstrip("-*0123456789. ").strip()
        if ":" not in line:
            continue
        name, expr = line.split(":", 1)
        name, expr = name.strip(), expr.strip()
        if name and expr and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
            out.append((name, expr))
    return out


def _build_user_prompt(n: int) -> str:
    examples = "\n".join(f"{k}: <expr>" for k in list(DEFAULT_FACTORS)[:3])
    return (f"Existing factors (names only, don't repeat their ideas):\n"
            f"{examples}\n\nPropose {n} new factor expressions.")


def _cross_section(expr, provider, codes, as_of, lookback_days=200) -> dict:
    """Factor value per code as-of `as_of` (for redundancy correlation)."""
    vals = {}
    for code in codes:
        bars = provider.get_daily_df(code, as_of - timedelta(days=lookback_days), as_of)
        if bars is None or bars.empty:
            continue
        s = evaluate_series(expr, bars.sort_values("date"))
        if len(s) and not pd.isna(s.iloc[-1]):
            vals[code] = float(s.iloc[-1])
    return vals


def mine_factors(llm, db, provider, codes: list[str], end: date, *,
                 n_candidates: int = 10, fwd_days: int = 5,
                 oos_ic_threshold: float = 0.03, min_train_ic: float = 0.02,
                 redundancy_max: float = 0.7, train_days: int = 252,
                 oos_days: int = 63, cfg: LLMConfig | None = None
                 ) -> list[MineResult]:
    cfg = cfg or LLMConfig()
    oos_start = end - timedelta(days=oos_days)
    train_end = oos_start - timedelta(days=1)
    train_start = train_end - timedelta(days=train_days)

    try:
        text = _complete_text(llm, _SYSTEM, _build_user_prompt(n_candidates), cfg)
    except Exception as e:  # noqa: BLE001 - LLM down → graceful skip
        logger.warning("factor mining LLM call failed: %s", e)
        return []

    candidates = parse_llm_factors(text)
    # Accepted factors' as-of cross-sections, for redundancy checks.
    accepted_xs: list[dict] = []
    results: list[MineResult] = []
    for name, expr_str in candidates:
        try:
            expr = parse_expr(expr_str)
        except FactorParseError as e:
            logger.info("dropping unparseable factor %s: %s", name, e)
            continue
        train_ic = factor_ic(expr, provider, codes, train_start, train_end,
                             fwd_days=fwd_days)
        oos_ic = factor_ic(expr, provider, codes, oos_start, end,
                           fwd_days=fwd_days)
        reason, accepted = _gate(expr, provider, codes, end, train_ic, oos_ic,
                                 accepted_xs, oos_ic_threshold, min_train_ic,
                                 redundancy_max)
        if accepted:
            accepted_xs.append(_cross_section(expr, provider, codes, end))
        db.save_generated_factor(name, expr_str, train_ic, oos_ic, accepted)
        results.append(MineResult(name, expr_str, train_ic, oos_ic, accepted, reason))
    return results


def _gate(expr, provider, codes, end, train_ic, oos_ic, accepted_xs,
          oos_ic_threshold, min_train_ic, redundancy_max) -> tuple[str, bool]:
    if np.isnan(train_ic) or abs(train_ic) < min_train_ic:
        return f"train_ic {train_ic:.3f} below {min_train_ic}", False
    if np.isnan(oos_ic) or oos_ic < oos_ic_threshold:
        return f"oos_ic {oos_ic:.3f} below {oos_ic_threshold}", False
    xs = _cross_section(expr, provider, codes, end)
    for prev in accepted_xs:
        common = [c for c in xs if c in prev]
        if len(common) >= 5:
            a = pd.Series([xs[c] for c in common]).rank()
            b = pd.Series([prev[c] for c in common]).rank()
            if a.std() and b.std() and abs(np.corrcoef(a, b)[0, 1]) >= redundancy_max:
                return f"redundant (|corr|>={redundancy_max})", False
    return f"accepted (oos_ic={oos_ic:.3f})", True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_factor_miner.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
.venv/Scripts/python.exe -m ruff check quanti/agent/factor_miner.py tests/test_factor_miner.py
git add quanti/agent/factor_miner.py tests/test_factor_miner.py
git commit -m "feat(agent): LLM factor miner — propose → parse → IC gate → persist"
```

---

## Task 6: CLI `mine-factors`

**Files:**
- Modify: `quanti/cli.py` (`cmd_mine_factors` near `cmd_optimize`/`cmd_backtest`; subparser + dispatch in `main`)
- Test: `tests/test_cli_mine.py`

**Interfaces:**
- Consumes: `mine_factors` (Task 5); `_build_llm_client`-style provider selection.
- Produces: `quanti.cli.cmd_mine_factors(args)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_mine.py
from __future__ import annotations

import types
from datetime import date

import quanti.cli as cli


def test_cmd_mine_factors_persists(tmp_path, monkeypatch):
    from quanti.data.database import Database
    dbp = str(tmp_path / "paper.db")
    real = Database(dbp); real.initialize(); real.ensure_portfolio(1_000_000)
    monkeypatch.setattr(cli, "_open_db", lambda: Database(dbp))

    from quanti.agent import factor_miner
    def fake_mine(llm, db, provider, codes, end, **kw):
        db.save_generated_factor("cli_f", "-Mean(close,5)", 0.05, 0.04, True)
        return [factor_miner.MineResult("cli_f", "-Mean(close,5)", 0.05, 0.04, True, "ok")]
    monkeypatch.setattr(factor_miner, "mine_factors", fake_mine)
    # stub the LLM client builder so no network/key needed
    monkeypatch.setattr(cli, "_build_mining_llm", lambda params: object(), raising=False)

    args = types.SimpleNamespace(universe=None, n=5, end=date.today().isoformat(),
                                 cash=1_000_000)
    cli.cmd_mine_factors(args)
    assert any(r["name"] == "cli_f" for r in Database(dbp).list_generated_factors())
```

(Adapt the LLM-client stub to however `cmd_mine_factors` builds its client — see Step 3; the brief shows a `_build_mining_llm` helper.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cli_mine.py -q`
Expected: FAIL — `AttributeError: cmd_mine_factors`.

- [ ] **Step 3: Implement**

Add to `quanti/cli.py`:

```python
def _build_mining_llm(params: dict):
    """Pick the LLM client by goal.params['llm_provider'] (mirrors runtime)."""
    provider = str(params.get("llm_provider", "anthropic")).lower()
    if provider in ("deepseek", "openai_compat"):
        from quanti.agent.openai_compat import DeepSeekLLMClient
        return DeepSeekLLMClient()
    from quanti.agent.llm_runtime import AnthropicLLMClient
    return AnthropicLLMClient()


def cmd_mine_factors(args):
    """LLM factor mining: propose → IC gate → persist to generated_factors."""
    from datetime import date

    from quanti.agent.factor_miner import mine_factors
    from quanti.agent.goal import load_goal
    from quanti.data.provider import DataProvider

    db = _open_db()
    provider = DataProvider(db)
    goal = load_goal(db)
    params = goal.params or {}
    pool = args.universe or goal.universe_pool
    codes = ([s.code for s in db.get_pool_stocks(pool)] if pool
             else [s.code for s in db.list_stocks()])
    codes = codes[:max(20, int(params.get("selector_max_universe", 100)))]
    end = date.fromisoformat(args.end) if args.end else date.today()
    try:
        llm = _build_mining_llm(params)
    except Exception as e:  # noqa: BLE001
        logger.error("LLM unavailable for mining: %s", e)
        db.close(); return
    results = mine_factors(llm, db, provider, codes, end, n_candidates=args.n)
    for r in results:
        flag = "✓ 采纳" if r.accepted else "· 弃"
        logger.info("%s %-16s train_ic=%.3f oos_ic=%.3f — %s",
                    flag, r.name, r.train_ic, r.oos_ic, r.reason)
    db.close()
```

In `main()`, register (after the `optimize`/`backtest` blocks):

```python
    mine_parser = subparsers.add_parser("mine-factors", help="LLM 因子挖掘")
    mine_parser.add_argument("--universe", type=str, default=None)
    mine_parser.add_argument("--n", type=int, default=10)
    mine_parser.add_argument("--end", type=str, default=None)
    mine_parser.add_argument("--cash", type=float, default=1_000_000)
```

and dispatch: `elif cmd == "mine-factors": cmd_mine_factors(args)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cli_mine.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
.venv/Scripts/python.exe -m ruff check quanti/cli.py tests/test_cli_mine.py
git add quanti/cli.py tests/test_cli_mine.py
git commit -m "feat(cli): quanti mine-factors — LLM factor mining"
```

---

## Task 7: Async mine API + generated-factor endpoints

**Files:**
- Modify: `quanti/api/routes.py` (mirror the optimize async job from the hyperopt feature pattern: `create_sync_job`/`update_sync_job`/`get_sync_job` + `loop.run_in_executor`)
- Test: `tests/test_api_mine.py`

**Interfaces:**
- Consumes: `mine_factors` (Task 5); `Database.list_generated_factors`/`set_factor_enabled`.
- Produces:
  - `POST /agent/mine-factors/async` → `{job_id}` (job created with the candidate count as total).
  - `GET /agent/mine-factors/status?job_id=` → `{job_id, current, total, status, results}` (results = `list_generated_factors()`).
  - `GET /factors/generated` → `list_generated_factors()`.
  - `POST /factors/generated/{name}/enabled` (body `{enabled: bool}`) → `{ok, name, enabled}` via `set_factor_enabled`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_mine.py
from __future__ import annotations

from fastapi.testclient import TestClient

from quanti.api.app import create_app


def test_generated_endpoints_and_toggle(monkeypatch):
    app = create_app(initial_cash=1_000_000, autostart_agent=False)
    db = app.state.db
    db.save_generated_factor("f1", "-Mean(close,5)", 0.05, 0.04, accepted=True)
    client = TestClient(app)
    r = client.get("/api/factors/generated")
    assert r.status_code == 200 and any(x["name"] == "f1" for x in r.json())
    t = client.post("/api/factors/generated/f1/enabled", json={"enabled": False})
    assert t.status_code == 200 and t.json()["enabled"] is False
    assert all(not x["enabled"] for x in client.get("/api/factors/generated").json()
               if x["name"] == "f1")


def test_mine_async_lifecycle(monkeypatch):
    from quanti.agent import factor_miner
    def fake_mine(llm, db, provider, codes, end, **kw):
        db.save_generated_factor("af", "-Std(close,20)", 0.05, 0.04, True)
        return [factor_miner.MineResult("af", "-Std(close,20)", 0.05, 0.04, True, "ok")]
    monkeypatch.setattr(factor_miner, "mine_factors", fake_mine)

    app = create_app(initial_cash=1_000_000, autostart_agent=False)
    client = TestClient(app)
    jid = client.post("/api/agent/mine-factors/async").json()["job_id"]
    import time
    for _ in range(60):
        s = client.get("/api/agent/mine-factors/status", params={"job_id": jid}).json()
        if s.get("status") in ("done", "error"):
            break
        time.sleep(0.05)
    assert s["status"] == "done"
    assert any(x["name"] == "af" for x in s["results"])
```

(Match the db-access pattern + status vocabulary to the existing optimize/quotes async jobs in routes.py. If the mining LLM client can't be built in tests, the fake `mine_factors` short-circuits before any real client use; ensure the worker builds the client lazily and the stubbed `mine_factors` is what runs.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api_mine.py -q`
Expected: FAIL — 404s.

- [ ] **Step 3: Implement**

In `quanti/api/routes.py`, add the endpoints mirroring the existing `optimize_async`/`_run_optimize`/`optimize_status` trio (same `sync_jobs` + `loop.run_in_executor` + worker pattern). The mine worker:
- builds codes from the goal universe (capped at `selector_max_universe`),
- builds the LLM client via the same provider-selection logic (lazy; on failure set job status `error`),
- creates the job with `total = n_candidates`,
- calls `factor_miner.mine_factors(...)` in the executor,
- on completion `update_sync_job(job_id, len(results), "done", {})`.

```python
@router.post("/agent/mine-factors/async")
async def mine_factors_async(request: Request):
    db = request.app.state.db
    import uuid
    n = 10
    job_id = f"mine_{str(uuid.uuid4())[:8]}"
    db.create_sync_job(job_id, "_mine", n)
    asyncio.create_task(_run_mine(job_id, request.app.state, n))
    return {"job_id": job_id}


async def _run_mine(job_id: str, state, n: int) -> None:
    from datetime import date

    from quanti.agent import factor_miner
    from quanti.agent.goal import load_goal
    from quanti.data.provider import DataProvider

    db = state.db
    loop = asyncio.get_event_loop()

    def work() -> None:
        goal = load_goal(db)
        params = goal.params or {}
        provider = DataProvider(db)
        pool = goal.universe_pool
        codes = ([s.code for s in db.get_pool_stocks(pool)] if pool
                 else [s.code for s in db.list_stocks()])
        codes = codes[:max(20, int(params.get("selector_max_universe", 100)))]
        # provider-selection mirrors runtime._build_llm_client
        prov = str(params.get("llm_provider", "anthropic")).lower()
        if prov in ("deepseek", "openai_compat"):
            from quanti.agent.openai_compat import DeepSeekLLMClient
            llm = DeepSeekLLMClient()
        else:
            from quanti.agent.llm_runtime import AnthropicLLMClient
            llm = AnthropicLLMClient()
        db.update_sync_job(job_id, 0, "running", {})
        results = factor_miner.mine_factors(llm, db, provider, codes,
                                            date.today(), n_candidates=n)
        db.update_sync_job(job_id, len(results), "done", {})

    try:
        await loop.run_in_executor(None, work)
    except Exception as e:  # noqa: BLE001
        db.update_sync_job(job_id, 0, "error", {"error": str(e)})


@router.get("/agent/mine-factors/status")
async def mine_factors_status(job_id: str, request: Request):
    db = request.app.state.db
    job = db.get_sync_job(job_id)
    if job is None:
        return {"error": f"Job '{job_id}' not found"}
    return {"job_id": job["job_id"], "current": job["current"],
            "total": job["total"], "status": job["status"],
            "results": db.list_generated_factors()}


@router.get("/factors/generated")
async def generated_factors(request: Request):
    return request.app.state.db.list_generated_factors()


class _EnabledBody(BaseModel):  # use the same BaseModel import the file already has
    enabled: bool


@router.post("/factors/generated/{name}/enabled")
async def set_generated_enabled(name: str, body: _EnabledBody, request: Request):
    request.app.state.db.set_factor_enabled(name, body.enabled)
    return {"ok": True, "name": name, "enabled": body.enabled}
```

Match `request.app.state.db`, `asyncio`, and `BaseModel` to the file's existing imports/conventions.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api_mine.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
.venv/Scripts/python.exe -m ruff check quanti/api/routes.py tests/test_api_mine.py
git add quanti/api/routes.py tests/test_api_mine.py
git commit -m "feat(api): async mine-factors job + generated-factor list/toggle"
```

---

## Task 8: Frontend API client

**Files:**
- Modify: `web/src/api/client.ts`

**Interfaces:**
- Produces: `GeneratedFactor`, `MineStatus` interfaces; `runMineAsync()`, `fetchMineStatus(jobId)`, `fetchGeneratedFactors()`, `setFactorEnabled(name, enabled)`. (The master switch reuses the existing `updateGoal({params})`.)

- [ ] **Step 1: Add types + calls**

Append to `web/src/api/client.ts` (before `export default api;`):

```ts
// --- LLM factor mining ---
export interface GeneratedFactor {
  name: string;
  expr_str: string;
  train_ic: number | null;
  oos_ic: number | null;
  accepted: boolean;
  enabled: boolean;
  created_at: string;
}

export interface MineStatus {
  job_id: string;
  current: number;
  total: number;
  status: string; // "running" | "done" | "error"
  results: GeneratedFactor[];
}

export const runMineAsync = () =>
  api.post<{ job_id: string }>("/agent/mine-factors/async");
export const fetchMineStatus = (jobId: string) =>
  api.get<MineStatus>("/agent/mine-factors/status", { params: { job_id: jobId } });
export const fetchGeneratedFactors = () =>
  api.get<GeneratedFactor[]>("/factors/generated");
export const setFactorEnabled = (name: string, enabled: boolean) =>
  api.post<{ ok: boolean; name: string; enabled: boolean }>(
    `/factors/generated/${encodeURIComponent(name)}/enabled`, { enabled });
```

- [ ] **Step 2: Verify build**

Run: `cd web && npm run build`
Expected: success (0 TS errors).

- [ ] **Step 3: Commit**

```bash
git add web/src/api/client.ts
git commit -m "feat(web): LLM factor-mining API client"
```

---

## Task 9: Agent view factor-mining card

**Files:**
- Modify: `web/src/views/Agent.vue`

**Interfaces:**
- Consumes: Task 8 client calls; existing `fetchGoal`/`updateGoal` for the master switch.

- [ ] **Step 1: Add the card**

In `web/src/views/Agent.vue` add an «因子挖掘» card near the strategy-evaluation card. Mirror the existing async-job polling idiom already in this file (the same `setInterval` + status-fetch pattern used elsewhere). Concretely:

1. Imports: `runMineAsync, fetchMineStatus, fetchGeneratedFactors, setFactorEnabled, type GeneratedFactor` + ensure `fetchGoal`/`updateGoal` are imported.
2. State + actions:
   ```ts
   const generated = ref<GeneratedFactor[]>([]);
   const mining = ref(false);
   const mineProgress = ref<{ current: number; total: number }>({ current: 0, total: 0 });
   let mineTimer: number | undefined;
   const useGenerated = ref(false);  // master switch (goal.params.use_generated_factors)

   const loadGenerated = async () => { generated.value = (await fetchGeneratedFactors()).data; };
   const loadMaster = async () => {
     const g = (await fetchGoal()).data;
     useGenerated.value = Boolean((g.params || {})["use_generated_factors"]);
   };
   const toggleMaster = async () => {
     const g = (await fetchGoal()).data;
     const params = { ...(g.params || {}), use_generated_factors: useGenerated.value };
     await updateGoal({ params });
   };
   const toggleFactor = async (f: GeneratedFactor) => {
     await setFactorEnabled(f.name, f.enabled); await loadGenerated();
   };
   const startMine = async () => {
     mining.value = true;
     try {
       const jid = (await runMineAsync()).data.job_id;
       mineTimer = window.setInterval(async () => {
         const s = (await fetchMineStatus(jid)).data;
         mineProgress.value = { current: s.current, total: s.total };
         generated.value = s.results;
         if (s.status === "done" || s.status === "error") {
           window.clearInterval(mineTimer); mining.value = false;
         }
       }, 2000);
     } catch (e) { console.error(e); mining.value = false; }
   };
   ```
   Call `loadGenerated()` + `loadMaster()` in the existing `onMounted`; clear `mineTimer` in `onUnmounted`.
3. Template card:
   ```html
   <div class="card">
     <div class="card-header">
       <h2>因子挖掘 (LLM)</h2>
       <button class="btn-secondary" :disabled="mining" @click="startMine">
         {{ mining ? `挖掘中 ${mineProgress.current}/${mineProgress.total}` : "运行挖掘" }}
       </button>
     </div>
     <label class="master-toggle">
       <input type="checkbox" v-model="useGenerated" @change="toggleMaster" />
       本账户实盘启用生成因子（默认关；开启后已采纳且启用的因子参与下单排名）
     </label>
     <table v-if="generated.length">
       <thead><tr><th>因子</th><th>表达式</th><th>训练IC</th><th>OOS IC</th><th>采纳</th><th>启用</th><th>生效中</th></tr></thead>
       <tbody>
         <tr v-for="f in generated" :key="f.name">
           <td>{{ f.name }}</td>
           <td><code>{{ f.expr_str }}</code></td>
           <td>{{ f.train_ic?.toFixed(3) }}</td>
           <td>{{ f.oos_ic?.toFixed(3) }}</td>
           <td>{{ f.accepted ? "✓" : "—" }}</td>
           <td><input type="checkbox" v-model="f.enabled" @change="toggleFactor(f)" /></td>
           <td>{{ f.accepted && f.enabled && useGenerated ? "● 生效" : "—" }}</td>
         </tr>
       </tbody>
     </table>
     <p v-else class="muted">尚未挖掘。点击"运行挖掘"让 LLM 提因子，IC 闸门筛选后入库。</p>
   </div>
   ```
   Reuse existing CSS classes (`card`, `card-header`, `btn-secondary`, `muted`); add a minimal `.master-toggle` style only if needed.

- [ ] **Step 2: Verify build**

Run: `cd web && npm run build`
Expected: success.

- [ ] **Step 3: Commit**

```bash
git add web/src/views/Agent.vue
git commit -m "feat(web): Agent view LLM factor-mining card + master/per-factor switches"
```

---

## Task 10: Full regression + docs

**Files:**
- Modify: `docs/2026-06-20-reference-mature-quant-systems.md`

- [ ] **Step 1: Full Python suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS (baseline + new mining tests). Fix any regression traced to this branch; report otherwise.

- [ ] **Step 2: Lint + frontend build**

Run: `.venv/Scripts/python.exe -m ruff check quanti tests` and `cd web && npm run build`
Expected: both clean.

- [ ] **Step 3: Doc touch**

In `docs/2026-06-20-reference-mature-quant-systems.md`, mark item ⑥ (LLM 因子挖掘闭环) as implemented — one line referencing the spec + plan, noting "factors-only; strategy generation deferred to declarative-DSL route".

- [ ] **Step 4: Commit**

```bash
git add docs/2026-06-20-reference-mature-quant-systems.md
git commit -m "docs: mark borrow-list ⑥ (LLM 因子挖掘) implemented"
```

(Do NOT push / open a PR here — the controller runs the final whole-branch review first.)

---

## Self-Review (completed by plan author)

- **Spec coverage:** §2 parser → Task 1. §3 IC → Task 2. §4 miner (prompt→parse→gate→persist) → Task 5. §5 persistence + `include_generated` gating + account switch → Tasks 3, 4. §6 CLI + async API + UI → Tasks 6, 7, 9; client → Task 8; the master switch (`use_generated_factors`) UI-visible via Task 9 + per-factor toggle via Tasks 7/9; "生效中" status → Task 9. §7 safety (whitelist, OOS gate, redundancy, caps, graceful LLM-skip) → Tasks 1, 5. §8 defaults → Task 5 signature + Global Constraints. §9 tests → each task + Task 10. §10 deferred (strategy DSL, no code-gen) → out of scope, noted.
- **Type consistency:** `parse_expr`/`FactorParseError` (Task 1) consumed in Tasks 3, 5. `factor_ic`/`rank_ic` (Task 2) consumed in Task 5. `save_generated_factor`/`list_generated_factors`/`set_factor_enabled`/`load_active_factor_fns` (Task 3) consumed in Tasks 4, 5, 7. `compute_factor_panel(..., include_generated)` (Task 4) consumed by runtime (Task 4). `MineResult`/`mine_factors`/`parse_llm_factors` (Task 5) consumed in Tasks 6, 7. Endpoint paths (`/agent/mine-factors/async|status`, `/factors/generated`, `/factors/generated/{name}/enabled`) consistent Tasks 7–9. `GeneratedFactor`/`MineStatus` (Task 8) consumed in Task 9.
- **Placeholder scan:** integration tasks (6/7) carry "match the existing optimize/quotes async pattern / db-access / BaseModel import" notes — concrete existing code to mirror, not blanks. Novel modules (1/2/3/5) have complete code. Task 2/test note moves `import pytest` to top (writing instruction). No TBD/forbidden patterns.
- **Safety re-check:** the only execution path for LLM output is `parse_expr` (AST whitelist, no eval/exec); accepted factors never touch live trading unless BOTH the per-account master switch and the per-factor enable are on (default off live); mining is on-demand only.
```
