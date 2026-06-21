# Factor Pipeline (Factor DSL, v1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 5 hand-written cross-sectional factor functions with a declarative, look-ahead-proof Python factor DSL (`Ref/Mean/Std/Log/arithmetic`), keeping the cross-sectional pipeline and signal consumer byte-for-byte unchanged (drop-in).

**Architecture:** A small expression-tree DSL (`Expr.evaluate(ctx) -> pd.Series`) whose primitives only ever look backward (`shift(n≥0)`, rolling windows) so look-ahead is structurally impossible. The 5 factors become DSL expressions; an `as_factor_fn(expr)` adapter wraps each into the existing `FactorFn = Callable[[pd.DataFrame], float]` contract, so `compute_factor_panel`, the cross-sectional layer, and `fuse_buy_signals` need zero changes.

**Tech Stack:** Python 3.13, pandas, numpy. No new dependencies.

## Global Constraints

- DSL is **Python objects** (no string parser in v1).
- **Look-ahead is structural**: the only time-shifting primitive is `Ref(expr, n)` with `n >= 0` (negative `n` raises `ValueError`); all windows are trailing `rolling(n)`. No primitive can reference future bars. A factor value at date t depends only on data ≤ t.
- **Drop-in**: the public factor names (`factor_momentum_3m`, … in `quanti/factors/cross_sectional.py`) and `DEFAULT_FACTORS` keep working; `compute_factor_panel`, `_winsorize/_zscore/_industry_demean`, `rank_by_composite`, and `fuse_buy_signals` are NOT modified. Existing `tests/test_factors_cross.py` must stay green unchanged.
- **Behavior-preserving**: each ported factor matches the old formula (incl. the NaN/insufficient-history regime). Equivalence is proven against an inline reference implementation in tests.
- `Std` uses pandas default `ddof=1`. Division guards divide-by-zero (`/0 → NaN`) so price ratios don't produce `inf`.
- No new dependencies. `ruff check` clean; full pytest green.

Spec: `docs/superpowers/specs/2026-06-21-factor-pipeline-design.md`.

---

## File Structure

- **Create** `quanti/factors/expr.py` — the DSL: `EvalContext`, `Expr` (+ operator overloads), `Constant`, `Field` + data terms (`Close/Open/High/Low/Volume/Turnover`), `BinaryOp`, `UnaryOp` (Task 1); time-series ops `Ref/Mean/Std/Sum/Max/Min/Log` (Task 2).
- **Create** `quanti/factors/library.py` — the 5 factor expressions, `as_factor_fn`, `evaluate_series`, `FACTOR_EXPRS` (Task 3).
- **Modify** `quanti/factors/cross_sectional.py` — `factor_*` names become DSL-backed, `DEFAULT_FACTORS` rebuilt from `library`, remove the 5 hand-written bodies + `_cum_return`; pipeline untouched (Task 4).
- **Create** `tests/test_factor_expr.py` (Tasks 1, 2), `tests/test_factor_library.py` (Task 3).
- **Modify** docs (Task 5).

---

## Task 1: DSL core — Expr, EvalContext, terms, arithmetic

**Files:**
- Create: `quanti/factors/expr.py`
- Test: `tests/test_factor_expr.py`

**Interfaces:**
- Produces:
  - `EvalContext(bars: pd.DataFrame)` with `.field(name) -> pd.Series` and `.index`.
  - `Expr` (ABC): `evaluate(ctx) -> pd.Series`; operator overloads `+ - * /` and unary `-`, with scalars auto-wrapped (both operand orders).
  - `Constant(value: float)`, `Field(name: str)`, factories `Close() Open() High() Low() Volume() Turnover()`, `BinaryOp(op, left, right)`, `UnaryOp(op, operand)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factor_expr.py
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quanti.factors.expr import (
    Close, Constant, EvalContext, Expr, Field, Volume,
)


def _ctx(closes, volume=None):
    n = len(closes)
    dates = pd.bdate_range(end=pd.Timestamp("2025-01-01"), periods=n)
    df = pd.DataFrame({
        "date": [d.date() for d in dates],
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": volume if volume is not None else [1.0] * n,
        "turnover": [1.0] * n,
    })
    return EvalContext(df)


def test_field_returns_column_as_series():
    ctx = _ctx([10.0, 11.0, 12.0])
    s = Close().evaluate(ctx)
    assert list(s) == [10.0, 11.0, 12.0]


def test_missing_field_is_all_nan():
    df = pd.DataFrame({"date": [pd.Timestamp("2025-01-01").date()], "close": [1.0]})
    s = Field("turnover").evaluate(EvalContext(df))
    assert s.isna().all()


def test_arithmetic_and_scalar_wrapping():
    ctx = _ctx([10.0, 20.0])
    expr = Close() / 10.0 - 1          # scalar on right
    assert list(expr.evaluate(ctx)) == [0.0, 1.0]
    expr2 = 30.0 - Close()             # scalar on left (__rsub__)
    assert list(expr2.evaluate(ctx)) == [20.0, 10.0]
    expr3 = -Close()                   # unary neg
    assert list(expr3.evaluate(ctx)) == [-10.0, -20.0]


def test_divide_by_zero_is_nan_not_inf():
    ctx = _ctx([1.0, 2.0])
    s = (Constant(1.0) / (Close() - Close())).evaluate(ctx)  # /0
    assert s.isna().all()


def test_evalcontext_sorts_by_date():
    df = pd.DataFrame({
        "date": [pd.Timestamp("2025-01-03").date(), pd.Timestamp("2025-01-01").date()],
        "close": [13.0, 11.0], "open": [0, 0], "high": [0, 0], "low": [0, 0],
        "volume": [1, 1], "turnover": [1, 1]})
    s = Close().evaluate(EvalContext(df))
    assert list(s) == [11.0, 13.0]  # ascending by date
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_factor_expr.py -q`
Expected: FAIL — `ModuleNotFoundError: quanti.factors.expr`.

- [ ] **Step 3: Write the core module**

```python
# quanti/factors/expr.py
"""Declarative, look-ahead-proof factor expression DSL.

Factors are built from composable Expr nodes evaluated over a single stock's
bars: `Expr.evaluate(ctx) -> pd.Series` (one value per bar date). Every
primitive only ever looks BACKWARD (Ref = shift(n>=0), rolling windows end at
the current row), so a factor value at date t depends only on data <= t —
look-ahead is structurally impossible (there is no future-referencing node).

See docs/superpowers/specs/2026-06-21-factor-pipeline-design.md.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np
import pandas as pd

_FIELDS = ("open", "high", "low", "close", "volume", "turnover")


class EvalContext:
    """Holds one stock's bars. Sorts ascending by `date` (and indexes by it
    when present) so evaluated Series are date-aligned; missing columns yield
    all-NaN. The framework only ever passes data <= as_of."""

    def __init__(self, bars: pd.DataFrame) -> None:
        df = bars
        if "date" in df.columns:
            df = df.sort_values("date").set_index("date")
        self._df = df

    def field(self, name: str) -> pd.Series:
        if name not in self._df.columns:
            return pd.Series(np.nan, index=self._df.index, dtype=float)
        return self._df[name].astype(float)

    @property
    def index(self) -> pd.Index:
        return self._df.index


def _to_expr(x) -> "Expr":
    return x if isinstance(x, Expr) else Constant(float(x))


class Expr(ABC):
    @abstractmethod
    def evaluate(self, ctx: EvalContext) -> pd.Series: ...

    def __add__(self, o): return BinaryOp("+", self, _to_expr(o))
    def __radd__(self, o): return BinaryOp("+", _to_expr(o), self)
    def __sub__(self, o): return BinaryOp("-", self, _to_expr(o))
    def __rsub__(self, o): return BinaryOp("-", _to_expr(o), self)
    def __mul__(self, o): return BinaryOp("*", self, _to_expr(o))
    def __rmul__(self, o): return BinaryOp("*", _to_expr(o), self)
    def __truediv__(self, o): return BinaryOp("/", self, _to_expr(o))
    def __rtruediv__(self, o): return BinaryOp("/", _to_expr(o), self)
    def __neg__(self): return UnaryOp("neg", self)


class Constant(Expr):
    def __init__(self, value: float) -> None:
        self.value = float(value)

    def evaluate(self, ctx: EvalContext) -> pd.Series:
        return pd.Series(self.value, index=ctx.index, dtype=float)


class Field(Expr):
    def __init__(self, name: str) -> None:
        self.name = name

    def evaluate(self, ctx: EvalContext) -> pd.Series:
        return ctx.field(self.name)


def Close() -> Field: return Field("close")
def Open() -> Field: return Field("open")
def High() -> Field: return Field("high")
def Low() -> Field: return Field("low")
def Volume() -> Field: return Field("volume")
def Turnover() -> Field: return Field("turnover")


class BinaryOp(Expr):
    def __init__(self, op: str, left: Expr, right: Expr) -> None:
        self.op, self.left, self.right = op, left, right

    def evaluate(self, ctx: EvalContext) -> pd.Series:
        a = self.left.evaluate(ctx)
        b = self.right.evaluate(ctx)
        if self.op == "+": return a + b
        if self.op == "-": return a - b
        if self.op == "*": return a * b
        if self.op == "/": return a / b.replace(0, np.nan)
        raise ValueError(f"unknown binary op {self.op!r}")


class UnaryOp(Expr):
    def __init__(self, op: str, operand: Expr) -> None:
        self.op, self.operand = op, operand

    def evaluate(self, ctx: EvalContext) -> pd.Series:
        v = self.operand.evaluate(ctx)
        if self.op == "neg": return -v
        raise ValueError(f"unknown unary op {self.op!r}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_factor_expr.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
.venv/Scripts/python.exe -m ruff check quanti/factors/expr.py tests/test_factor_expr.py
git add quanti/factors/expr.py tests/test_factor_expr.py
git commit -m "feat(factors): factor expression DSL core (Expr, EvalContext, terms, arithmetic)"
```

---

## Task 2: DSL time-series ops + look-ahead invariant

**Files:**
- Modify: `quanti/factors/expr.py`
- Test: `tests/test_factor_expr.py` (append)

**Interfaces:**
- Consumes: `Expr`, `EvalContext` (Task 1).
- Produces: `Ref(expr, n)` (n≥0; negative raises `ValueError`), `Mean(expr, n)`, `Std(expr, n)`, `Sum(expr, n)`, `Max(expr, n)`, `Min(expr, n)`, `Log(expr)` — all `Expr` subclasses.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_factor_expr.py — append
from quanti.factors.expr import Log, Max, Mean, Min, Ref, Std, Sum


def test_ref_lags_by_n():
    ctx = _ctx([10.0, 11.0, 12.0, 13.0])
    s = Ref(Close(), 1).evaluate(ctx)
    assert np.isnan(s.iloc[0])
    assert list(s.iloc[1:]) == [10.0, 11.0, 12.0]


def test_ref_zero_is_identity():
    ctx = _ctx([10.0, 11.0])
    assert list(Ref(Close(), 0).evaluate(ctx)) == [10.0, 11.0]


def test_ref_negative_raises():
    with pytest.raises(ValueError):
        Ref(Close(), -1)


def test_rolling_ops():
    ctx = _ctx([2.0, 4.0, 6.0, 8.0])
    assert Mean(Close(), 2).evaluate(ctx).iloc[-1] == 7.0      # (6+8)/2
    assert Sum(Close(), 2).evaluate(ctx).iloc[-1] == 14.0
    assert Max(Close(), 3).evaluate(ctx).iloc[-1] == 8.0
    assert Min(Close(), 3).evaluate(ctx).iloc[-1] == 4.0
    # Std ddof=1 of [6,8] = std of sample = 1.41421...
    assert Std(Close(), 2).evaluate(ctx).iloc[-1] == pytest.approx(np.std([6.0, 8.0], ddof=1))


def test_rolling_insufficient_window_is_nan():
    ctx = _ctx([2.0, 4.0])
    assert np.isnan(Mean(Close(), 3).evaluate(ctx).iloc[-1])


def test_log():
    ctx = _ctx([1.0, np.e])
    s = Log(Close()).evaluate(ctx)
    assert s.iloc[0] == pytest.approx(0.0)
    assert s.iloc[1] == pytest.approx(1.0)


def test_no_lookahead_appending_future_bars_does_not_change_past():
    """The structural guarantee: a factor's value at date t is invariant to
    any bars added AFTER t."""
    closes = [10.0, 11.0, 9.0, 12.0, 13.0, 11.0, 14.0, 15.0]
    expr = Mean(Close(), 3) / Ref(Close(), 1) - 1
    base = _ctx(closes)
    s_base = expr.evaluate(base)
    extended = _ctx(closes + [99.0, 1.0, 50.0])  # wild future bars
    s_ext = expr.evaluate(extended)
    # The first len(closes) positions must be identical.
    assert np.allclose(s_base.values, s_ext.values[: len(closes)], equal_nan=True)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_factor_expr.py -q`
Expected: FAIL — `ImportError: cannot import name 'Ref'`.

- [ ] **Step 3: Append the time-series ops**

Add to `quanti/factors/expr.py` (after `UnaryOp`):

```python
class Ref(Expr):
    """Lag by n bars (look BACK n). n>=0; negative would reference the future
    and is forbidden — the core of the no-look-ahead guarantee."""

    def __init__(self, expr: Expr, n: int) -> None:
        if int(n) < 0:
            raise ValueError(f"Ref shift n must be >= 0 (no future refs), got {n}")
        self.expr, self.n = expr, int(n)

    def evaluate(self, ctx: EvalContext) -> pd.Series:
        return self.expr.evaluate(ctx).shift(self.n)


class _Rolling(Expr):
    _agg = ""

    def __init__(self, expr: Expr, n: int) -> None:
        self.expr, self.n = expr, int(n)

    def evaluate(self, ctx: EvalContext) -> pd.Series:
        r = self.expr.evaluate(ctx).rolling(self.n)
        return getattr(r, self._agg)()


class Mean(_Rolling): _agg = "mean"
class Std(_Rolling): _agg = "std"    # pandas default ddof=1
class Sum(_Rolling): _agg = "sum"
class Max(_Rolling): _agg = "max"
class Min(_Rolling): _agg = "min"


class Log(Expr):
    def __init__(self, expr: Expr) -> None:
        self.expr = expr

    def evaluate(self, ctx: EvalContext) -> pd.Series:
        return np.log(self.expr.evaluate(ctx))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_factor_expr.py -q`
Expected: PASS (all).

- [ ] **Step 5: Lint + commit**

```bash
.venv/Scripts/python.exe -m ruff check quanti/factors/expr.py tests/test_factor_expr.py
git add quanti/factors/expr.py tests/test_factor_expr.py
git commit -m "feat(factors): DSL time-series ops (Ref/Mean/Std/Sum/Max/Min/Log) + no-look-ahead test"
```

---

## Task 3: Factor library — 5 expressions + adapter

**Files:**
- Create: `quanti/factors/library.py`
- Test: `tests/test_factor_library.py`

**Interfaces:**
- Consumes: all of `expr` (Tasks 1–2).
- Produces:
  - Expression objects `momentum_3m`, `momentum_6m`, `reversal_1w`, `turnover_20d`, `realized_vol_20d` (all `Expr`).
  - `FACTOR_EXPRS: dict[str, Expr]` mapping factor name → expression.
  - `as_factor_fn(expr: Expr) -> Callable[[pd.DataFrame], float]` — evaluates the expr on bars, returns the last (as-of) value as a float (or NaN).
  - `evaluate_series(expr: Expr, bars: pd.DataFrame) -> pd.Series` — the full date-indexed factor series (batch).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_factor_library.py
from __future__ import annotations

import numpy as np
import pandas as pd

from quanti.factors.library import (
    FACTOR_EXPRS, as_factor_fn, evaluate_series, momentum_6m,
)


def _bars(closes, turnover=1.0):
    n = len(closes)
    dates = pd.bdate_range(end=pd.Timestamp("2025-01-01"), periods=n)
    return pd.DataFrame({
        "date": [d.date() for d in dates],
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1e6] * n, "turnover": [turnover] * n,
    })


# --- reference implementations (the OLD formulas) to prove equivalence ---
def _cum_return(closes, start_offset, end_offset):
    if len(closes) < start_offset + 1:
        return float("nan")
    p_start = closes.iloc[-start_offset - 1]
    p_end = closes.iloc[-end_offset - 1]
    if p_start <= 0:
        return float("nan")
    return float(p_end / p_start - 1.0)


def _ref(name, bars):
    c = bars["close"]
    if name == "momentum_3m": return _cum_return(c, 63, 21)
    if name == "momentum_6m": return _cum_return(c, 126, 21)
    if name == "reversal_1w":
        r = _cum_return(c, 5, 0); return -r if not np.isnan(r) else r
    if name == "turnover_20d":
        return float("nan") if len(bars) < 20 else -float(bars["turnover"].iloc[-20:].mean())
    if name == "realized_vol_20d":
        if len(c) < 21: return float("nan")
        last = c.iloc[-21:]
        rets = np.log(last / last.shift(1)).dropna()
        if len(rets) < 2: return float("nan")
        return -float(rets.std() * np.sqrt(252))
    raise KeyError(name)


def test_each_factor_matches_reference():
    rng = np.random.default_rng(7)
    closes = list(10 + np.cumsum(rng.normal(0, 0.2, 200)))
    bars = _bars([abs(c) + 1 for c in closes], turnover=2.3)
    for name, expr in FACTOR_EXPRS.items():
        got = as_factor_fn(expr)(bars)
        exp = _ref(name, bars)
        assert (np.isnan(got) and np.isnan(exp)) or got == pytest.approx(exp, rel=1e-9), \
            f"{name}: dsl={got} ref={exp}"


def test_as_factor_fn_short_history_is_nan():
    bars = _bars([10.0, 10.1, 10.2])
    for expr in FACTOR_EXPRS.values():
        assert np.isnan(as_factor_fn(expr)(bars))


def test_evaluate_series_returns_full_series():
    bars = _bars(list(range(2, 60)))
    s = evaluate_series(momentum_6m, bars)
    assert len(s) == len(bars)


import pytest  # noqa: E402  (kept at bottom to mirror brief ordering)
```

(Move `import pytest` to the top with the other imports when writing the file — the inline note is just a reminder; ruff requires top-of-file imports.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_factor_library.py -q`
Expected: FAIL — `ModuleNotFoundError: quanti.factors.library`.

- [ ] **Step 3: Write the library**

```python
# quanti/factors/library.py
"""The factor library: the production factors expressed in the DSL, plus the
adapter that exposes them under the existing FactorFn contract so the
cross-sectional pipeline is a drop-in.

Each factor is behavior-equivalent to the prior hand-written implementation
in cross_sectional.py (see tests/test_factor_library.py)."""

from __future__ import annotations

from typing import Callable

import pandas as pd

from quanti.factors.expr import (
    Close, EvalContext, Expr, Log, Mean, Ref, Std, Turnover,
)

_close = Close()

# 3-month momentum, skipping the most recent month (Jegadeesh-Titman):
# Ref(close,21)/Ref(close,63) - 1  ==  close[-22]/close[-64] - 1.
momentum_3m: Expr = Ref(_close, 21) / Ref(_close, 63) - 1
momentum_6m: Expr = Ref(_close, 21) / Ref(_close, 126) - 1
# Short-term reversal, sign-flipped.
reversal_1w: Expr = -(_close / Ref(_close, 5) - 1)
# Low-turnover anomaly, sign-flipped.
turnover_20d: Expr = -Mean(Turnover(), 20)
# Low-vol anomaly, sign-flipped: annualized std of 20 daily log returns.
realized_vol_20d: Expr = -Std(Log(_close / Ref(_close, 1)), 20) * (252 ** 0.5)

FACTOR_EXPRS: dict[str, Expr] = {
    "momentum_3m": momentum_3m,
    "momentum_6m": momentum_6m,
    "reversal_1w": reversal_1w,
    "turnover_20d": turnover_20d,
    "realized_vol_20d": realized_vol_20d,
}


def evaluate_series(expr: Expr, bars: pd.DataFrame) -> pd.Series:
    """Full date-indexed factor series (every bar gets a value). Batch use."""
    return expr.evaluate(EvalContext(bars))


def as_factor_fn(expr: Expr) -> Callable[[pd.DataFrame], float]:
    """Wrap an Expr into the existing FactorFn contract: evaluate on the bars
    and return the as-of (last bar) value as a float (NaN if uncomputable)."""
    def fn(bars: pd.DataFrame) -> float:
        s = expr.evaluate(EvalContext(bars))
        if len(s) == 0:
            return float("nan")
        return float(s.iloc[-1])
    return fn
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_factor_library.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
.venv/Scripts/python.exe -m ruff check quanti/factors/library.py tests/test_factor_library.py
git add quanti/factors/library.py tests/test_factor_library.py
git commit -m "feat(factors): factor library — 5 DSL factors + as_factor_fn adapter, equivalence-tested"
```

---

## Task 4: Drop-in into cross_sectional.py

**Files:**
- Modify: `quanti/factors/cross_sectional.py`
- Test: `tests/test_factors_cross.py` (must stay green; no edits expected)

**Interfaces:**
- Consumes: `FACTOR_EXPRS`/`as_factor_fn` (Task 3).
- Produces: `factor_momentum_3m`, `factor_momentum_6m`, `factor_reversal_1w`, `factor_turnover_20d`, `factor_realized_vol_20d` as DSL-backed `FactorFn`s; `DEFAULT_FACTORS` unchanged in shape.

- [ ] **Step 1: Confirm the current behavior baseline**

Run: `.venv/Scripts/python.exe -m pytest tests/test_factors_cross.py -q`
Expected: PASS (this is the behavior we must preserve through the swap).

- [ ] **Step 2: Replace the hand-written factors with DSL-backed ones**

In `quanti/factors/cross_sectional.py`:

1. Remove the functions `_cum_return`, `factor_momentum_3m`, `factor_momentum_6m`, `factor_reversal_1w`, `factor_turnover_20d`, `factor_realized_vol_20d` (lines ~55–102).
2. Add an import near the top (with the other imports):

```python
from quanti.factors.library import FACTOR_EXPRS, as_factor_fn
```

3. Re-create the public factor names as DSL-backed callables (so existing imports/tests keep working) and rebuild `DEFAULT_FACTORS` from them, replacing the old `DEFAULT_FACTORS` dict:

```python
# DSL-backed factor functions (behavior-equivalent to the prior hand-written
# versions; defined declaratively in quanti.factors.library). Names are kept
# so existing imports (e.g. tests) and DEFAULT_FACTORS stay drop-in.
factor_momentum_3m = as_factor_fn(FACTOR_EXPRS["momentum_3m"])
factor_momentum_6m = as_factor_fn(FACTOR_EXPRS["momentum_6m"])
factor_reversal_1w = as_factor_fn(FACTOR_EXPRS["reversal_1w"])
factor_turnover_20d = as_factor_fn(FACTOR_EXPRS["turnover_20d"])
factor_realized_vol_20d = as_factor_fn(FACTOR_EXPRS["realized_vol_20d"])

DEFAULT_FACTORS: dict[str, FactorFn] = {
    "momentum_3m": factor_momentum_3m,
    "momentum_6m": factor_momentum_6m,
    "reversal_1w": factor_reversal_1w,
    "turnover_20d": factor_turnover_20d,
    "realized_vol_20d": factor_realized_vol_20d,
}
```

Leave the `FactorFn` type alias, `FactorConfig`, `compute_factor_panel`, `_winsorize`, `_zscore`, `_industry_demean`, and `rank_by_composite` exactly as they are. If `numpy as np` becomes unused after deleting the bodies, remove the import (ruff will flag it); if still used elsewhere in the file, keep it.

- [ ] **Step 3: Run the existing cross-sectional tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_factors_cross.py -q`
Expected: PASS — unchanged. `TestSingleFactors` (sign behavior + short-history NaN) and `TestPanel`/`TestComposite` (pipeline ranking) all still hold because the DSL factors are behavior-equivalent and the pipeline is untouched.

- [ ] **Step 4: Lint + commit**

```bash
.venv/Scripts/python.exe -m ruff check quanti/factors/cross_sectional.py
git add quanti/factors/cross_sectional.py
git commit -m "refactor(factors): cross_sectional factors are DSL-backed (drop-in, pipeline unchanged)"
```

---

## Task 5: Full regression + docs

**Files:**
- Modify: `docs/2026-06-20-reference-mature-quant-systems.md`

- [ ] **Step 1: Full Python suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS (previous baseline + the new DSL tests). If a failure traces to the factor swap, fix it; if unrelated/pre-existing, note it.

- [ ] **Step 2: Lint the touched tree**

Run: `.venv/Scripts/python.exe -m ruff check quanti/factors tests`
Expected: clean.

- [ ] **Step 3: Doc touch**

In `docs/2026-06-20-reference-mature-quant-systems.md`, mark item ② (声明式防前视因子 Pipeline) as implemented — one line referencing the spec (`docs/superpowers/specs/2026-06-21-factor-pipeline-design.md`) and plan. Keep it to a line or two.

- [ ] **Step 4: Commit**

```bash
git add docs/2026-06-20-reference-mature-quant-systems.md
git commit -m "docs: mark borrow-list ② (因子 Pipeline DSL) implemented"
```

(Do NOT push / open a PR here — the controller runs the final whole-branch review first, then finishing-a-development-branch.)

---

## Self-Review (completed by plan author)

- **Spec coverage:** §2 DSL (EvalContext/Expr/terms/ops) → Tasks 1–2. §3 structural no-look-ahead (Ref n≥0 raises, rolling-only) + invariant test → Task 2. §4 the 5 factor ports + equivalence → Task 3. §5 drop-in adapter + DEFAULT_FACTORS + pipeline untouched → Task 4. §6 batch `evaluate_series` → Task 3. §7 module layout → Tasks 1/3/4. §8 testing → each task + Task 5. §9 deferred (cross-sectional DSL primitives, string parser, panel vectorization) → out of scope, noted.
- **Type consistency:** `Expr.evaluate(ctx)->Series`, `EvalContext.field/index`, `Ref/Mean/Std/Sum/Max/Min/Log`, `Constant/Field/BinaryOp/UnaryOp` defined in Tasks 1–2 and consumed verbatim in Task 3. `FACTOR_EXPRS`/`as_factor_fn`/`evaluate_series` defined in Task 3, consumed in Task 4. Public `factor_*` names + `DEFAULT_FACTORS` preserved in Task 4 → existing `tests/test_factors_cross.py` imports unchanged.
- **Placeholder scan:** Task 3's test note about moving `import pytest` to the top is a writing instruction, not a code gap. Task 4 conditionally removes the `numpy` import based on remaining usage (ruff-driven), which is a concrete, decidable check, not a placeholder. The core code (Tasks 1–3) is complete.
- **Behavior-preservation guard:** Task 3 proves each DSL factor equals the old formula via an inline reference impl across random data (incl. NaN regimes); Task 4 keeps `tests/test_factors_cross.py` green unchanged — two independent nets that the drop-in didn't change behavior.
```
