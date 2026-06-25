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
from quanti.factors.cross_sectional import DEFAULT_FACTORS, _merge_fundamentals
from quanti.factors.evaluation import factor_ic
from quanti.factors.library import evaluate_series
from quanti.factors.parser import FactorParseError, parse_expr
from quanti.utils.parallel import thread_map

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a quant researcher proposing cross-sectional alpha factors for "
    "A-share daily bars, expressed in a tiny DSL.\n"
    "Allowed data: close, open, high, low, volume, turnover; and fundamentals "
    "(point-in-time): pe, pe_ttm, pb, ps, ps_ttm, total_mv, circ_mv, dv_ratio, "
    "roe, netprofit_yoy, revenue_yoy.\n"
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


def _cross_section(expr, provider, codes, as_of, lookback_days=200,
                   with_fundamentals=False) -> dict:
    """Factor value per code as-of `as_of` (for redundancy correlation).
    Merges PIT fundamentals when `with_fundamentals` so value/quality factors
    produce a real cross-section (else NaN, mirroring the IC path)."""
    vals = {}
    start = as_of - timedelta(days=lookback_days)
    for code in codes:
        bars = provider.get_daily_df(code, start, as_of)
        if bars is None or bars.empty:
            continue
        bars = bars.sort_values("date")
        if with_fundamentals:
            bars = _merge_fundamentals(bars, provider, code, start, as_of).sort_values("date")
        s = evaluate_series(expr, bars)
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
    # Merge PIT fundamentals into IC/cross-section eval ONLY when the DB has them,
    # so LLM-proposed value/quality factors (pe/pb/roe/...) can actually score
    # and be accepted (otherwise they read all-NaN → dropped). No-op + zero extra
    # queries when there are no fundamentals.
    with_fund = db.has_fundamentals()
    oos_start = end - timedelta(days=oos_days)
    # Gap >= fwd_days so the training forward-return labels (shift(-fwd_days)) do not
    # reach into the OOS window — keeps train and OOS truly disjoint.
    train_end = oos_start - timedelta(days=fwd_days * 2 + 3)
    train_start = train_end - timedelta(days=train_days)

    try:
        text = _complete_text(llm, _SYSTEM, _build_user_prompt(n_candidates), cfg)
    except Exception as e:  # noqa: BLE001 - LLM down → graceful skip
        logger.warning("factor mining LLM call failed: %s", e)
        return []

    candidates = parse_llm_factors(text)

    # Parallel: score each candidate's IC + as-of cross-section. Read-only
    # (provider/DB is thread-safe), so one thread per candidate. ponytail:
    # thread_map. The accept/redundancy gate below stays sequential (order-
    # dependent vs already-accepted factors).
    def _score(nm_ex: tuple[str, str]):
        name, expr_str = nm_ex
        try:
            expr = parse_expr(expr_str)
        except FactorParseError as e:
            logger.info("dropping unparseable factor %s: %s", name, e)
            return None
        try:
            train_ic = factor_ic(expr, provider, codes, train_start, train_end,
                                 fwd_days=fwd_days, with_fundamentals=with_fund)
            oos_ic = factor_ic(expr, provider, codes, oos_start, end,
                               fwd_days=fwd_days, with_fundamentals=with_fund)
            xs = _cross_section(expr, provider, codes, end, with_fundamentals=with_fund)
        except Exception as e:  # noqa: BLE001 - one factor's eval error can't kill the batch
            logger.info("factor eval failed for %s: %s", name, e)
            return None
        return (name, expr_str, train_ic, oos_ic, xs)

    scored = [r for r in thread_map(_score, candidates) if r is not None]

    # Sequential gate (redundancy depends on prior accepted) + persist.
    accepted_xs: list[dict] = []
    results: list[MineResult] = []
    for name, expr_str, train_ic, oos_ic, xs in scored:
        reason, accepted = _gate(train_ic, oos_ic, xs, accepted_xs,
                                 oos_ic_threshold, min_train_ic, redundancy_max)
        if accepted:
            accepted_xs.append(xs)
        db.save_generated_factor(name, expr_str, train_ic, oos_ic, accepted)
        results.append(MineResult(name, expr_str, train_ic, oos_ic, accepted, reason))
    return results


def _gate(train_ic, oos_ic, xs, accepted_xs,
          oos_ic_threshold, min_train_ic, redundancy_max) -> tuple[str, bool]:
    """Pure accept/reject given precomputed IC + cross-section `xs`. Redundancy
    is order-dependent (compared against already-accepted xs), so this runs
    sequentially even when the IC scoring that feeds it was parallelized."""
    if np.isnan(train_ic) or abs(train_ic) < min_train_ic:
        return f"train_ic {train_ic:.3f} below {min_train_ic}", False
    if np.isnan(oos_ic) or oos_ic < oos_ic_threshold:
        return f"oos_ic {oos_ic:.3f} below {oos_ic_threshold}", False
    for prev in accepted_xs:
        common = [c for c in xs if c in prev]
        if len(common) >= 5:
            a = pd.Series([xs[c] for c in common]).rank()
            b = pd.Series([prev[c] for c in common]).rank()
            if a.std() and b.std() and abs(np.corrcoef(a, b)[0, 1]) >= redundancy_max:
                return f"redundant (|corr|>={redundancy_max})", False
    return f"accepted (oos_ic={oos_ic:.3f})", True


def rescore_generated_factors(db, provider, codes: list[str], end: date, *,
                              fwd_days: int = 5, oos_ic_threshold: float = 0.03,
                              min_train_ic: float = 0.02, train_days: int = 252,
                              oos_days: int = 63) -> list[MineResult]:
    """Recompute train/OOS rank-IC for every factor already in generated_factors
    and refresh its `accepted` flag against the CURRENT data — for libraries
    mined on thin data and now stale. No LLM; same IC gate as mining (redundancy
    skipped — order-dependent and meaningless for a re-score).

    Preserves each factor's `enabled` toggle: save_generated_factor INSERT OR
    REPLACEs the whole row with enabled defaulting True, which would otherwise
    clobber the user's per-factor choice."""
    with_fund = db.has_fundamentals()
    oos_start = end - timedelta(days=oos_days)
    train_end = oos_start - timedelta(days=fwd_days * 2 + 3)
    train_start = train_end - timedelta(days=train_days)
    # Each factor is independent (redundancy skipped), so one thread per factor;
    # factor_ic is read-only and save_generated_factor commits under the DB lock.
    # ponytail: thread_map.
    def _rescore(row) -> "MineResult | None":
        name, expr_str = row["name"], row["expr_str"]
        try:
            expr = parse_expr(expr_str)
        except FactorParseError:
            return None
        try:
            train_ic = factor_ic(expr, provider, codes, train_start, train_end,
                                 fwd_days=fwd_days, with_fundamentals=with_fund)
            oos_ic = factor_ic(expr, provider, codes, oos_start, end,
                               fwd_days=fwd_days, with_fundamentals=with_fund)
            accepted = (not np.isnan(train_ic) and abs(train_ic) >= min_train_ic
                        and not np.isnan(oos_ic) and oos_ic >= oos_ic_threshold)
            db.save_generated_factor(name, expr_str, train_ic, oos_ic, accepted,
                                     enabled=row["enabled"])
        except Exception as e:  # noqa: BLE001 - one factor's eval error can't kill the batch
            logger.info("rescore failed for %s: %s", name, e)
            return None
        return MineResult(name, expr_str, train_ic, oos_ic, accepted, "rescored")

    return [r for r in thread_map(_rescore, db.list_generated_factors())
            if r is not None]
