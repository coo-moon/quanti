"""LLM factor mining: LLM proposes factor expressions, a safe parser + rank-IC
gate accept only the predictive, non-redundant ones into the generated_factors
library. The LLM only ADDS candidates; rules (parse whitelist + OOS IC) decide.
On-demand (CLI / async API), never per agent cycle."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from quanti.agent.llm_runtime import LLMConfig, _complete_text
from quanti.factors.cross_sectional import DEFAULT_FACTORS, _merge_fundamentals
from quanti.factors.evaluation import factor_ic, factor_ic_stats
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
                 oos_days: int = 63, fdr_q: float = 0.10,
                 cfg: LLMConfig | None = None
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
            oos = factor_ic_stats(expr, provider, codes, oos_start, end,
                                  fwd_days=fwd_days, with_fundamentals=with_fund)
            xs = _cross_section(expr, provider, codes, end, with_fundamentals=with_fund)
        except Exception as e:  # noqa: BLE001 - one factor's eval error can't kill the batch
            logger.info("factor eval failed for %s: %s", name, e)
            return None
        return (name, expr_str, train_ic, oos, xs)

    scored = [r for r in thread_map(_score, candidates) if r is not None]

    # Multiple-testing gate. The old code accepted EVERY candidate clearing a
    # raw oos_ic floor — best-of-N data-snooping with no correction for how many
    # were tried, which manufactures spurious "winners". Now per batch:
    #   (1) economic floor: train_ic >= min_train_ic (one-sided, same sign as the
    #       OOS IC>0 hypothesis — a train/OOS sign flip is a noise signature, not
    #       a candidate) AND oos mean_ic >= oos_ic_threshold;
    #   (2) Benjamini-Hochberg FDR at `fdr_q` over the floor-passers' one-sided
    #       IC p-values (family size = #floor-passers this batch — a VALID
    #       within-run FDR control);
    #   (3) redundancy vs already-accepted.
    # NOTE: this corrects WITHIN-RUN best-of-N snooping only. Cross-run
    # multiplicity (re-mining many times) is NOT corrected here — that needs a
    # genuine lifetime trial ledger + deflated-Sharpe haircut (a separate item);
    # a prior attempt to fold it into the BH family size via the name-dedup'd
    # library count was invalid (not an FDR level) and unstable (it silently
    # demoted already-accepted factors as the library grew), so it was removed.
    floor_idx: list[int] = []
    pvals: list[float] = []
    for i, (name, expr_str, train_ic, oos, xs) in enumerate(scored):
        if (not np.isnan(train_ic) and train_ic >= min_train_ic
                and not np.isnan(oos.mean_ic) and oos.mean_ic >= oos_ic_threshold):
            floor_idx.append(i)
            pvals.append(_ic_pvalue(oos.t_stat, oos.n, fwd_days))
    m = len(floor_idx)
    fdr_ok = {floor_idx[pos] for pos in _bh_discoveries(pvals, fdr_q, m)}
    floor_set = set(floor_idx)

    accepted_xs: list[dict] = []
    results: list[MineResult] = []
    for i, (name, expr_str, train_ic, oos, xs) in enumerate(scored):
        oos_ic = oos.mean_ic
        p = _ic_pvalue(oos.t_stat, oos.n, fwd_days)
        if i not in floor_set:
            if np.isnan(train_ic) or train_ic < min_train_ic:
                reason = f"train_ic {train_ic:.3f} below {min_train_ic}"
            else:
                reason = f"oos_ic {oos_ic:.3f} below {oos_ic_threshold}"
            accepted = False
        elif i not in fdr_ok:
            reason = (f"failed FDR (p={p:.3f}, q={fdr_q}, m={m}, "
                      f"t={oos.t_stat:.2f}/n={oos.n})")
            accepted = False
        elif _redundant(xs, accepted_xs, redundancy_max):
            reason = f"redundant (|corr|>={redundancy_max})"
            accepted = False
        else:
            accepted = True
            accepted_xs.append(xs)
            reason = f"accepted (oos_ic={oos_ic:.3f}, p={p:.3f}, FDR q={fdr_q}, m={m})"
        db.save_generated_factor(name, expr_str, train_ic, oos_ic, accepted)
        results.append(MineResult(name, expr_str, train_ic, oos_ic, accepted, reason))
    return results


def _ic_pvalue(t_stat: float, n: int, fwd_days: int) -> float:
    """One-sided p-value (H1: IC>0) for the HAC IC t-stat.

    Reference = Student-t with df = effective INDEPENDENT obs − 1, where the
    independent count ≈ n / fwd_days (overlapping fwd_days forward-return windows
    make adjacent daily ICs non-independent). This is heavier-tailed than the
    normal and corrects most of the small-sample over-rejection. It is still
    mildly ANTI-conservative — the estimated HAC variance is itself noisy, so
    realized FDR can modestly exceed the nominal `fdr_q`; treat q as a strictness
    knob, not an exact error rate. NaN/None t (untestable) → p=1.0 (reject)."""
    if t_stat is None or np.isnan(t_stat) or n < 2:
        return 1.0
    df = max(1, int(round(n / max(1, fwd_days))) - 1)
    return _student_t_sf(float(t_stat), df)


def _student_t_sf(t: float, df: int) -> float:
    """Upper-tail P(T > t) for Student-t with df degrees of freedom (no scipy).
    Via the regularized incomplete beta: P(T>|t|) = I_x(df/2, 1/2), x=df/(df+t²)."""
    if df <= 0 or not math.isfinite(t):
        return 1.0
    x = df / (df + t * t)
    half = 0.5 * _betai(0.5 * df, 0.5, x)   # = P(T > |t|)
    return half if t >= 0 else 1.0 - half


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a,b) (Numerical Recipes betacf method)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    bt = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _betacf(a: float, b: float, x: float) -> float:
    MAXIT, EPS, FPMIN = 300, 3e-16, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for mm in range(1, MAXIT + 1):
        m2 = 2 * mm
        aa = mm * (b - mm) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + mm) * (qab + mm) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def _bh_discoveries(pvals: list[float], q: float, n_trials: int) -> set[int]:
    """Benjamini-Hochberg FDR. Returns the indices of `pvals` accepted at level
    `q` for a family of size `n_trials` (≥ len(pvals)). Accept all ranks ≤ the
    largest k with p_(k) ≤ k/m·q."""
    m = max(n_trials, len(pvals), 1)
    order = sorted(range(len(pvals)), key=lambda i: pvals[i])
    k_max = 0
    for rank, idx in enumerate(order, start=1):
        if pvals[idx] <= (rank / m) * q:
            k_max = rank
    return set(order[:k_max])


def _redundant(xs: dict, accepted_xs: list[dict], redundancy_max: float) -> bool:
    """True if `xs`'s cross-section rank-correlates >= redundancy_max with any
    already-accepted factor (order-dependent, so the caller runs it sequentially
    after the batch FDR decision)."""
    for prev in accepted_xs:
        common = [c for c in xs if c in prev]
        if len(common) >= 5:
            a = pd.Series([xs[c] for c in common]).rank()
            b = pd.Series([prev[c] for c in common]).rank()
            if a.std() and b.std() and abs(np.corrcoef(a, b)[0, 1]) >= redundancy_max:
                return True
    return False


def rescore_generated_factors(db, provider, codes: list[str], end: date, *,
                              fwd_days: int = 5, oos_ic_threshold: float = 0.03,
                              min_train_ic: float = 0.02, train_days: int = 252,
                              oos_days: int = 63, fdr_q: float = 0.10
                              ) -> list[MineResult]:
    """Recompute train/OOS rank-IC for every factor in generated_factors and
    refresh its `accepted` flag against CURRENT data — for libraries mined on
    thin data and now stale. No LLM; same multiple-testing gate as mining
    (redundancy skipped — order-dependent and meaningless for a re-score).

    Re-evaluating the WHOLE library at once is itself a large multiple test, so
    the BH-FDR correction matters even more here: family size = the number of
    factors clearing the economic floor. Preserves each factor's `enabled`
    toggle (save_generated_factor would otherwise reset it to True)."""
    with_fund = db.has_fundamentals()
    oos_start = end - timedelta(days=oos_days)
    train_end = oos_start - timedelta(days=fwd_days * 2 + 3)
    train_start = train_end - timedelta(days=train_days)

    # Parallel eval (read-only); the FDR decision + persist run after, over the
    # whole batch. ponytail: thread_map for the eval, sequential for the gate.
    def _eval(row):
        name, expr_str = row["name"], row["expr_str"]
        try:
            expr = parse_expr(expr_str)
        except FactorParseError:
            return None
        try:
            train_ic = factor_ic(expr, provider, codes, train_start, train_end,
                                 fwd_days=fwd_days, with_fundamentals=with_fund)
            oos = factor_ic_stats(expr, provider, codes, oos_start, end,
                                  fwd_days=fwd_days, with_fundamentals=with_fund)
        except Exception as e:  # noqa: BLE001 - one factor's eval error can't kill the batch
            logger.info("rescore failed for %s: %s", name, e)
            return None
        return (name, expr_str, bool(row["enabled"]), train_ic, oos)

    evald = [r for r in thread_map(_eval, db.list_generated_factors()) if r is not None]

    floor_idx: list[int] = []
    pvals: list[float] = []
    for i, (name, expr_str, enabled, train_ic, oos) in enumerate(evald):
        if (not np.isnan(train_ic) and train_ic >= min_train_ic
                and not np.isnan(oos.mean_ic) and oos.mean_ic >= oos_ic_threshold):
            floor_idx.append(i)
            pvals.append(_ic_pvalue(oos.t_stat, oos.n, fwd_days))
    fdr_ok = {floor_idx[pos] for pos in _bh_discoveries(pvals, fdr_q, len(floor_idx))}

    results: list[MineResult] = []
    for i, (name, expr_str, enabled, train_ic, oos) in enumerate(evald):
        accepted = i in fdr_ok
        db.save_generated_factor(name, expr_str, train_ic, oos.mean_ic, accepted,
                                 enabled=enabled)
        results.append(MineResult(name, expr_str, train_ic, oos.mean_ic, accepted,
                                  "rescored"))
    return results
