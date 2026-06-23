# quanti/factors/parser.py
"""Safe parser: LLM factor expression string -> ② Expr.

NEVER eval/exec. Uses ast.parse + a strict whitelist recursive descent: only
the DSL's data fields, the time-series functions, arithmetic, unary minus, and
numeric constants are allowed. Anything else raises FactorParseError. This is
the security boundary for LLM-generated factors (⑥)."""

from __future__ import annotations

import ast

from quanti.factors.expr import (
    Close, Constant, Expr, Field, High, Log, Low, Max, Mean, Min, Open, Ref,
    Std, Sum, Turnover, Volume,
)
from quanti.factors.expr import FUNDAMENTAL_FIELDS

MAX_LEN = 400
MAX_DEPTH = 25


class FactorParseError(ValueError):
    """Raised when an expression string is malformed or uses anything outside
    the DSL whitelist."""


_FIELDS = {"close": Close, "open": Open, "high": High, "low": Low,
           "volume": Volume, "turnover": Turnover}
# Fundamental fields (point-in-time merged into the panel) — generic Field
# nodes; missing columns evaluate to NaN, so a factor referencing them on a
# universe without fundamentals just drops out (no crash).
_FIELDS.update({name: (lambda n=name: Field(n)) for name in FUNDAMENTAL_FIELDS})
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
