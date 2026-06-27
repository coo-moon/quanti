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

# Point-in-time fundamentals merged into the factor panel (cross_sectional):
# daily_basic valuation + financial-statement indicators (by ann_date). Missing
# columns evaluate to NaN, so these are safe to reference on any universe.
FUNDAMENTAL_FIELDS = (
    "pe", "pe_ttm", "pb", "ps", "ps_ttm", "total_mv", "circ_mv", "dv_ratio",
    "roe", "netprofit_yoy", "revenue_yoy",
)
_FIELDS = ("open", "high", "low", "close", "volume", "turnover",
           *FUNDAMENTAL_FIELDS)


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
        if self.op == "+":
            return a + b
        if self.op == "-":
            return a - b
        if self.op == "*":
            return a * b
        if self.op == "/":
            return a / b.replace(0, np.nan)
        raise ValueError(f"unknown binary op {self.op!r}")


class UnaryOp(Expr):
    def __init__(self, op: str, operand: Expr) -> None:
        self.op, self.operand = op, operand

    def evaluate(self, ctx: EvalContext) -> pd.Series:
        v = self.operand.evaluate(ctx)
        if self.op == "neg":
            return -v
        raise ValueError(f"unknown unary op {self.op!r}")


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


class Mean(_Rolling):
    _agg = "mean"


class Std(_Rolling):
    _agg = "std"  # pandas default ddof=1


class Sum(_Rolling):
    _agg = "sum"


class Max(_Rolling):
    _agg = "max"


class Min(_Rolling):
    _agg = "min"


class Log(Expr):
    def __init__(self, expr: Expr) -> None:
        self.expr = expr

    def evaluate(self, ctx: EvalContext) -> pd.Series:
        # log is undefined for x <= 0 → NaN, NOT -inf. A 0/negative input (e.g.
        # a 0-close suspended bar in Log(close/Ref(close,1)), or an LLM-mined
        # Log(volume) on a 0-volume day) would otherwise emit a "divide by zero
        # in log" RuntimeWarning AND inject -inf that silently corrupts every
        # downstream rolling stat / z-score / IC. Mask to NaN (properly excluded).
        s = self.expr.evaluate(ctx)
        return np.log(s.where(s > 0))
