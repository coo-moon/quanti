"""Offline LLM decision-layer evaluation (research, read-only).

The LLM judge (agent_mode="llm"/"llm_full") has carried no offline evidence
that its picks beat the mechanical pipeline — docs/2026-07-01 flagged
"融合参数…零回测 PnL 证据" as the gap that must close before llm_full can
be trusted with real money. This module runs that experiment:

  for each past trading day D (ALL inputs use only bars <= D — no lookahead):
    1. candidates = liquid tradable universe as of D (bounded, deterministic)
    2. baseline   = mechanical ranking: ensemble strategy vote blended with
                    the cross-sectional factor panel (production fusion
                    defaults: 0.5 x strategy score + 0.5 x sigmoid(z))
    3. llm        = the LLM judge picks top-K from the SAME candidate list
    4. realized   = forward hfq returns of both baskets vs the candidate
                    mean, over 5/10 trading days

Aggregation is deliberately naive (means + hit rates + agreement): with ~30
days x 5 picks per arm this is a RESEARCH SAMPLE, not a deployable PnL
claim — its job is to show whether the LLM adds signal over the ranker,
not to size a book.

Nothing here trades and nothing writes to the account DB (an optional
decision-log entry is the only write, off by default).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

logger = logging.getLogger(__name__)

#: Production fusion default (factor_blend=0.5, sentiment off) — the eval
#: baseline must match what the mechanical pipeline actually does.
FACTOR_BLEND = 0.5
#: Default pick count per basket.
DEFAULT_K = 5
#: Trading-day spacing between eval days (weekly cadence → ~5x fewer calls
#: than daily, and adjacent days would be near-identical experiments anyway).
DEFAULT_STRIDE = 5

_JSON_ARRAY = re.compile(r"\[.*?\]", re.DOTALL)


@dataclass
class EvalDayResult:
    day: date
    n_candidates: int
    baseline: list[str]
    llm: list[str]
    llm_error: str = ""
    llm_raw: str = ""  # last LLM response (truncated) for offline inspection
    forward: dict[int, dict[str, float | None]] = field(default_factory=dict)


def build_eval_days(provider, end: date | None = None, n_days: int = 30,
                    stride: int = DEFAULT_STRIDE) -> list[date]:
    """Trading days spaced `stride` apart, ending at `end` (default today)."""
    end = end or date.today()
    start = end - timedelta(days=n_days * stride * 2)
    dates = provider.get_trade_dates(start, end)
    if not dates:
        return []
    return dates[-(n_days * stride)::stride][-n_days:]


def resolve_candidates(db, provider, as_of: date, *,
                       max_codes: int = 60,
                       min_adv20_yuan: float = 50_000_000.0) -> list[str]:
    """Liquid tradable universe as of `as_of`, deterministically bounded.

    The UniverseBuilder applies point-in-time liquidity/age filters; we then
    take the first `max_codes` by sorted code so the sample is stable across
    days (a research sample, not an optimizer playground).
    """
    from quanti.agent.universe import resolve_tradable_universe
    params = {"liquidity_filter": True,
              "universe_min_adv20": min_adv20_yuan}
    codes = resolve_tradable_universe(db, provider, pool=None, params=params,
                                      as_of=as_of)
    return sorted(codes)[:max_codes]


def _sigmoid(x: float) -> float:
    import math
    return 1.0 / (1.0 + math.exp(-x))


def mechanical_rank(db, provider, candidates: list[str], as_of: date,
                    strategies_dir: str = "strategies",
                    return_panel: bool = False):
    """Production-fusion ranking of `candidates` as of `as_of` (data <= as_of).

    Returns [(code, final_score)] sorted descending (or (ranked, panel) with
    `return_panel=True` — the judge needs the same features the ranker saw).
    Every candidate gets a score — no-signal codes rank on their factor tilt
    alone, mirroring the runtime include-all extras path.
    """
    from quanti.agent.params import resolve_params
    from quanti.agent.signal_pipeline import collect_signals_per_strategy
    from quanti.factors.cross_sectional import FactorConfig, compute_factor_panel
    from quanti.strategy.loader import StrategyLoader

    strategies = [s for s in StrategyLoader().load_directory(strategies_dir)
                  if getattr(s, "selectable", True)]
    if not strategies:
        return []
    prepared = []
    for s in strategies:
        s.init(resolve_params(db, s.name, None))
        prepared.append((s, 1.0))
    per_strategy, weights = collect_signals_per_strategy(
        prepared, candidates, provider, end=as_of)
    panel = compute_factor_panel(
        provider, db, candidates,
        as_of=as_of, config=FactorConfig(industry_neutralize=False))

    ss_by_code: dict[str, float] = {}
    for strat_name, sigs in per_strategy.items():
        w = weights.get(strat_name, 1.0)
        for sig in sigs:
            prev = ss_by_code.get(sig.stock_code, 0.0)
            ss_by_code[sig.stock_code] = max(prev, w * sig.strength)

    ranked: list[tuple[str, float]] = []
    for code in candidates:
        ss = max(0.0, min(1.0, ss_by_code.get(code, 0.0)))
        fs = 0.0
        if panel is not None and code in panel.index:
            v = panel.loc[code, "composite"]
            if v is not None and v == v:  # NaN guard
                fs = float(v)
        final = (1.0 - FACTOR_BLEND) * ss + FACTOR_BLEND * _sigmoid(fs)
        ranked.append((code, final))
    ranked.sort(key=lambda t: t[1], reverse=True)
    if return_panel:
        return ranked, panel
    return ranked


def forward_returns(provider, codes: list[str], as_of: date,
                    horizons: tuple[int, ...] = (5, 10)) -> dict[str, dict[int, float | None]]:
    """hfq close-to-close forward returns: close[D+h] / close[D] - 1.

    `as_of` must be a trading day (eval days come from the calendar). A code
    missing the D bar or suspended through the horizon yields None for that
    horizon (excluded from the mean, counted in n_avail).
    """
    end = as_of + timedelta(days=max(horizons) * 2 + 7)
    out: dict[str, dict[int, float | None]] = {}
    for code in codes:
        try:
            bars = provider.get_daily_bars(code, as_of, end)
        except Exception as e:  # noqa: BLE001 - one code cannot kill the eval
            logger.debug("fwd bars failed for %s: %s", code, e)
            out[code] = {h: None for h in horizons}
            continue
        closes: list[tuple[date, float]] = []
        for b in bars:
            if b.close and b.close > 0:
                closes.append((b.date, float(b.close)))
        idx = None
        for i, (d, _) in enumerate(closes):
            if d >= as_of:
                idx = i
                break
        per: dict[int, float | None] = {}
        for h in horizons:
            if idx is None or idx + h >= len(closes):
                per[h] = None
                continue
            c0, ch = closes[idx][1], closes[idx + h][1]
            per[h] = (ch / c0 - 1.0) if c0 > 0 else None
        out[code] = per
    return out


def _parse_llm_codes(text: str) -> list[str]:
    """Extract a JSON array of codes from the LLM response (defensive)."""
    if not text:
        return []
    m = _JSON_ARRAY.search(text)
    if not m:
        return []
    try:
        raw = json.loads(m.group(0))
    except (ValueError, TypeError):
        return []
    if not isinstance(raw, list):
        return []
    return [str(c).strip() for c in raw if str(c).strip()]


def llm_picks(llm, db, candidates: list[str], as_of: date, k: int = DEFAULT_K,
              horizon_hint: int = 10,
              ranked: list[tuple[str, float]] | None = None,
              panel=None) -> tuple[list[str], str, str]:
    """Ask the judge LLM to pick top-K from the candidate list.

    Returns (picks, error, raw). Picks are deduped, restricted to the
    candidate set, and capped at K — the model cannot invent names. On an
    unparseable first answer the model gets ONE corrective retry; failure
    after that is an explicit per-day error, never a silent fallback. `raw`
    is the last response text (truncated) for offline inspection.

    FAIRNESS: when `ranked`/`panel` are passed, the model sees the SAME
    features the mechanical ranker used (final score, factor z, industry,
    price) — otherwise the experiment measures stock-name familiarity, not
    judgment over information.
    """
    from quanti.agent.llm_runtime import LLMConfig, _complete_text

    scores = {c: s for c, s in (ranked or [])}

    def _row(code: str) -> str:
        stock = db.get_stock(code)
        name = stock.name if stock else ""
        bits = [f"{code} {name}".rstrip()]
        s = scores.get(code)
        if s is not None:
            bits.append(f"final={s:.3f}")
        fs = None
        ind = ""
        if panel is not None and code in panel.index:
            v = panel.loc[code, "composite"]
            if v is not None and v == v:
                fs = float(v)
            ind = str(panel.loc[code, "industry"] or "")
        if fs is not None:
            bits.append(f"factor={fs:+.2f}")
        if ind:
            bits.append(f"行业={ind}")
        return "  " + " | ".join(bits)

    system = (
        "你是 A 股量化研究员,正在做离线评估。系统给出截至 {as_of} 的候选"
        "股票清单,每只带机械排序的分:final 越高 = 机械基线越看好;"
        "factor 为截面因子 z 分(>0 相对占优)。请基于这些信息选出你认为未来 "
        "{horizon} 个交易日相对表现最好的 {k} 只。你的选择可以与机械排序不同——"
        "这正是被评估的判断力。只输出一个 JSON 字符串数组"
        "(如 [\"600519\", \"000001\"]),不要任何解释或其它文字。"
    ).format(as_of=as_of.isoformat(), horizon=horizon_hint, k=k)
    user = "候选清单:\n" + "\n".join(_row(c) for c in candidates)
    cfg = LLMConfig()
    try:
        resp = _complete_text(llm, system, user, cfg)
    except Exception as e:  # noqa: BLE001 - LLM outages are per-day errors
        return [], f"LLM 调用失败: {e}", ""
    codes = _parse_llm_codes(resp)
    if not codes:
        # One corrective retry — models occasionally wrap the array in prose.
        retry = (user
                 + "\n\n注意:上次输出无法解析。只输出一个 JSON 数组,不要任何其它文字。")
        try:
            resp2 = _complete_text(llm, system, retry, cfg)
        except Exception as e:  # noqa: BLE001
            return [], f"LLM 调用失败: {e}", resp[:400]
        codes2 = _parse_llm_codes(resp2)
        if codes2:
            codes = codes2
            resp = resp2
        else:
            return [], "无法解析 JSON 数组(含一次纠正重试)", resp[:400]
    allowed = set(candidates)
    picks: list[str] = []
    for c in codes:
        if c in allowed and c not in picks:
            picks.append(c)
        if len(picks) >= k:
            break
    if len(picks) < k:
        return picks, f"有效选票不足 k={k}(仅 {len(picks)} 只)", resp[:400]
    return picks, "", resp[:400]


def _basket_return(fwd: dict[str, dict[int, float | None]], codes: list[str],
                   horizon: int) -> float | None:
    vals = [fwd[c][horizon] for c in codes
            if fwd.get(c, {}).get(horizon) is not None]
    return sum(vals) / len(vals) if vals else None


def evaluate(db, provider, llm, *,
             end: date | None = None,
             n_days: int = 30,
             stride: int = DEFAULT_STRIDE,
             k: int = DEFAULT_K,
             horizons: tuple[int, ...] = (5, 10),
             max_codes: int = 60,
             strategies_dir: str = "strategies") -> dict:
    """Run the full replay. Returns the report dict (see module docstring)."""
    days = build_eval_days(provider, end=end, n_days=n_days, stride=stride)
    day_results: list[EvalDayResult] = []
    for d in days:
        try:
            candidates = resolve_candidates(db, provider, d, max_codes=max_codes)
        except Exception as e:  # noqa: BLE001
            logger.warning("eval day %s candidate build failed: %s", d, e)
            continue
        if not candidates:
            continue
        ranked, panel = mechanical_rank(
            db, provider, candidates, d,
            strategies_dir=strategies_dir, return_panel=True)
        baseline = [c for c, _ in ranked[:k]]
        picks, err, raw = llm_picks(llm, db, candidates, d, k=k,
                                    ranked=ranked, panel=panel)
        fwd = forward_returns(provider, candidates, d, horizons=horizons)
        day_results.append(EvalDayResult(
            day=d, n_candidates=len(candidates), baseline=baseline,
            llm=picks, llm_error=err, llm_raw=raw,
            forward={h: {"baseline": _basket_return(fwd, baseline, h),
                         "llm": _basket_return(fwd, picks, h),
                         "candidates_mean": _basket_return(fwd, candidates, h)}
                     for h in horizons}))
        logger.info("eval %s: %d candidates, baseline %d, llm %d (err=%s)",
                    d, len(candidates), len(baseline), len(picks), err or "-")

    return build_report(day_results, horizons=horizons, k=k)


def build_report(day_results: list[EvalDayResult], *, horizons: tuple[int, ...],
                 k: int) -> dict:
    """Aggregate per-day results into the report dict."""
    rows = []
    for r in day_results:
        rows.append({
            "day": r.day.isoformat(),
            "n_candidates": r.n_candidates,
            "baseline": r.baseline,
            "llm": r.llm,
            "llm_error": r.llm_error,
            "llm_raw": r.llm_raw,
            "agreement": (len(set(r.baseline) & set(r.llm)) / k
                          if r.llm else None),
            "forward": r.forward,
        })
    summary: dict = {}
    for h in horizons:
        bl = [r.forward[h]["baseline"] for r in day_results
              if not r.llm_error and r.forward[h]["baseline"] is not None]
        ll = [r.forward[h]["llm"] for r in day_results
              if not r.llm_error and r.forward[h]["llm"] is not None]
        cm = [r.forward[h]["candidates_mean"] for r in day_results
              if not r.llm_error and r.forward[h]["candidates_mean"] is not None]
        cm_mean = (sum(cm) / len(cm)) if cm else None
        summary[str(h) + "d"] = {
            "baseline_mean": (sum(bl) / len(bl)) if bl else None,
            "llm_mean": (sum(ll) / len(ll)) if ll else None,
            "candidates_mean": cm_mean,
            "baseline_n": len(bl),
            "llm_n": len(ll),
            "baseline_beat_rate": (sum(1 for b in bl if cm_mean is not None and b > cm_mean) / len(bl)) if bl else None,
            "llm_beat_rate": (sum(1 for v in ll if cm_mean is not None and v > cm_mean) / len(ll)) if ll else None,
        }
    return {
        "generated_at": date.today().isoformat(),
        "k": k,
        "n_days": len(day_results),
        "n_llm_error_days": sum(1 for r in day_results if r.llm_error),
        "summary": summary,
        "days": rows,
    }
