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
