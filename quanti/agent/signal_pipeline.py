"""Signal aggregation across an ensemble of strategies + factor overlay.

When the Selector returns top-K strategies (not just the winner), we need
to combine their per-code signals into a single ordered list of BUYs to
hand the broker. This module is the consolidation layer:

  1. Each (strategy, weight) pair produces a set of BUY signals for the
     candidate universe.
  2. Same code's BUY signals stack by `weight × strength` into a single
     strategy_score in [0, 1].
  3. Multiply by the cross-sectional `composite` factor score (mapped to
     [0, 1] via a sigmoid) to get a `final_score` in [0, 1].
  4. Optional industry-neutral cap: limit to N positions per industry.
  5. Filter by a `final_score` threshold.

SELL signals are NOT consolidated — they always pass through, because:
  - Stop-loss sells are RiskManager territory, not signal territory.
  - "Strategy A says sell" but "Strategy B says buy" → still sell to free
    capital; the BUY can recompete next cycle.

This makes the runtime's job mechanical: feed in top-K strategies and a
factor panel, get back a single, prioritized list of orders to dispatch.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from quanti.data.provider import DataProvider
from quanti.models import Direction, Signal
from quanti.strategy.base import BaseStrategy

logger = logging.getLogger(__name__)


@dataclass
class FusedCandidate:
    """A buy candidate after fusion across strategies + factor overlay."""

    code: str
    strategy_score: float       # weighted strength across strategies, ∈ [0, 1]
    factor_score: float         # cross-sectional composite (z-units), often [-3, 3]
    final_score: float          # combined score used for ranking, ∈ [0, 1]
    sentiment_score: float = 0.0  # LLM news sentiment ∈ [-1, 1]; 0 = neutral/none
    contributing_strategies: list[str] = field(default_factory=list)
    dominant_strategy: str = ""  # argmax(weight×strength); owns the exit
    industry: str = ""

    def to_signal(self, reason: str = "", strength: float | None = None) -> Signal:
        """Materialize into a Signal that PaperBroker can ingest.

        `strength` defaults to `final_score` so the broker's sizer (whether
        FixedSizer or VolTargetSizer) can scale further by conviction. Pass an
        explicit `strength` (e.g. 1.0) for equal-weight mode, where every name
        should deploy the same target weight regardless of conviction.
        """
        contrib = ",".join(self.contributing_strategies)
        sent_str = f" sent={self.sentiment_score:+.2f}" if self.sentiment_score else ""
        msg = reason or (f"ensemble[{contrib}] strat={self.strategy_score:.2f} "
                         f"factor={self.factor_score:+.2f}{sent_str} "
                         f"final={self.final_score:.2f}")
        return Signal(stock_code=self.code, direction=Direction.BUY,
                      strength=self.final_score if strength is None else strength,
                      reason=msg, entry_strategy=self.dominant_strategy)


def _sigmoid(x: float, k: float = 1.0) -> float:
    return 1.0 / (1.0 + math.exp(-k * x))


def _industry_for(panel: pd.DataFrame | None, code: str) -> str:
    if panel is None or panel.empty or "industry" not in panel.columns:
        return ""
    if code not in panel.index:
        return ""
    v = panel.loc[code, "industry"]
    return str(v) if v else ""


def _factor_score(panel: pd.DataFrame | None, code: str) -> float:
    if panel is None or panel.empty or "composite" not in panel.columns:
        return 0.0
    if code not in panel.index:
        return 0.0
    v = panel.loc[code, "composite"]
    if v is None or pd.isna(v):
        return 0.0
    return float(v)


def fuse_buy_signals(
    per_strategy_signals: dict[str, list[Signal]],
    strategy_weights: dict[str, float],
    factor_panel: pd.DataFrame | None = None,
    factor_blend: float = 0.5,
    sentiment_scores: dict[str, float] | None = None,
    sentiment_blend: float = 0.0,
) -> list[FusedCandidate]:
    """Combine BUY signals across an ensemble into ranked candidates.

    Args:
        per_strategy_signals: { strategy_name → list of Signal objects }.
            SELLs are ignored (handled elsewhere). Duplicates within a
            strategy take the max strength.
        strategy_weights: { strategy_name → weight }, e.g. softmax over OOS
            Sharpe. Should sum to ~1 but doesn't have to — we normalize.
        factor_panel: optional cross-sectional factor panel from
            `quanti.factors.cross_sectional.compute_factor_panel`.
            When provided, blends the composite z-score into final_score.
        factor_blend: weight on factor overlay vs pure strategy ensemble.
            0.0 = ignore factors; 1.0 = factor only. Default 0.5 balances
            "what the strategies vote" with "what the factor model says".
        sentiment_scores: optional { code → news sentiment ∈ [-1, 1] } from
            the news/sentiment analyst. Codes absent here are treated as
            neutral. Only consulted when sentiment_blend > 0.
        sentiment_blend: weight on the sentiment overlay, carved from the
            strategy weight alongside factor_blend. Default 0.0 = off, which
            makes this function behave exactly as the prior 2-way blend.

    Returns:
        list of FusedCandidate sorted descending by final_score.
    """
    # Normalize weights
    w_sum = sum(strategy_weights.values()) or 1.0
    weights = {k: v / w_sum for k, v in strategy_weights.items()}

    # Per-code: aggregate max-strength per strategy, then weighted sum.
    by_code_per_strat: dict[str, dict[str, float]] = defaultdict(dict)
    for strat_name, sigs in per_strategy_signals.items():
        for s in sigs:
            if s.direction != Direction.BUY:
                continue
            prev = by_code_per_strat[s.stock_code].get(strat_name, 0.0)
            by_code_per_strat[s.stock_code][strat_name] = max(prev, s.strength)

    # Resolve blend weights once. factor_blend / sentiment_blend carve weight
    # away from the strategy ensemble; if together they'd exceed 1.0 we scale
    # them down so the strategy vote never goes negative. With the defaults
    # (sentiment_blend=0) this reduces exactly to the prior 2-way blend.
    fb = max(0.0, min(1.0, factor_blend))
    sb = max(0.0, min(1.0, sentiment_blend))
    if fb + sb > 1.0:
        scale = 1.0 / (fb + sb)
        fb, sb = fb * scale, sb * scale
    strat_w = max(0.0, 1.0 - fb - sb)

    candidates: list[FusedCandidate] = []
    for code, per_strat in by_code_per_strat.items():
        # strategy_score: weighted sum, capped at 1.0
        ss = sum(weights.get(name, 0.0) * strength
                 for name, strength in per_strat.items())
        ss = max(0.0, min(1.0, ss))

        # Dominant strategy: the single biggest weighted contributor. This is
        # the strategy that most justified the buy, so it's the natural owner
        # of the exit (replay its on_bar on the holding). Every candidate has
        # ≥1 contributor (the code only got here via a strategy BUY), so this
        # is always well-defined.
        dominant = max(per_strat.items(),
                       key=lambda kv: weights.get(kv[0], 0.0) * kv[1])[0]

        fs = _factor_score(factor_panel, code)
        # Map factor z (typically [-3, 3]) to [0, 1] via sigmoid.
        fs_norm = _sigmoid(fs, k=1.0)

        # Sentiment ∈ [-1, 1] → [0, 1]; missing/neutral → 0.5 (no tilt). Only
        # consulted when the blend actually weights it, so a candidate's
        # recorded sentiment_score honestly reflects what moved its ranking.
        sent = 0.0
        if sb > 0 and sentiment_scores:
            sent = max(-1.0, min(1.0, float(sentiment_scores.get(code, 0.0) or 0.0)))
        sent_norm = 0.5 * (sent + 1.0)

        final = strat_w * ss + fb * fs_norm + sb * sent_norm

        candidates.append(FusedCandidate(
            code=code, strategy_score=ss, factor_score=fs,
            sentiment_score=sent, final_score=final,
            contributing_strategies=sorted(per_strat.keys()),
            dominant_strategy=dominant,
            industry=_industry_for(factor_panel, code),
        ))

    candidates.sort(key=lambda c: c.final_score, reverse=True)
    return candidates


def industry_cap(
    candidates: list[FusedCandidate],
    n_per_industry: int = 2,
) -> list[FusedCandidate]:
    """Keep at most `n_per_industry` candidates per industry, preserving order.

    Candidates with empty industry are kept as-is (treated as a single
    "unknown" bucket but un-capped, since we can't responsibly limit them).
    """
    seen: dict[str, int] = defaultdict(int)
    out: list[FusedCandidate] = []
    for c in candidates:
        ind = c.industry
        if not ind:
            out.append(c)
            continue
        if seen[ind] < n_per_industry:
            out.append(c)
            seen[ind] += 1
    return out


def filter_by_threshold(
    candidates: list[FusedCandidate],
    threshold: float = 0.30,
) -> list[FusedCandidate]:
    return [c for c in candidates if c.final_score >= threshold]


def filter_affordable(
    candidates: list[FusedCandidate],
    total_value: float,
    max_position_pct: float,
    held_value: dict[str, float],
    price_of,
    lot_size: int = 100,
    fee_headroom: float = 1.003,
) -> tuple[list[FusedCandidate], list[str]]:
    """Drop candidates whose one-lot cost exceeds the single-stock cap room.

    A股 trades in 100-share lots, so a name whose one lot costs more than the
    per-stock cap room (``total_value × max_position_pct − already-held``) sizes
    to 0 lots and gets rejected anyway (``affordable_lots < 1``). Feeding it to
    the LLM/ensemble is pure noise — worse, the LLM is blind to the cash/lot
    constraint and kept re-picking unaffordable high-priced names. Room mirrors
    RiskManager.max_additional_buy_value's single-stock term; industry room is
    left to the actual sizing step.

    ``price_of(code) -> latest close`` is injected so this stays pure and
    unit-testable. A code priced ≤ 0 (no quote on disk) is KEPT and left for
    downstream steps. ``fee_headroom`` pads the lot cost ~0.3% so a name that
    only clears the cap before commission isn't passed through to be rejected
    later. Returns ``(kept, dropped_codes)``.
    """
    kept: list[FusedCandidate] = []
    dropped: list[str] = []
    for c in candidates:
        room = total_value * max_position_pct - held_value.get(c.code, 0.0)
        price = price_of(c.code)
        if price <= 0 or price * lot_size * fee_headroom <= room:
            kept.append(c)
        else:
            dropped.append(c.code)
    return kept, dropped


# ---------------------------------------------------------- runtime helper

def collect_signals_per_strategy(
    strategies_with_weights: list[tuple[BaseStrategy, float]],
    candidates: list[str],
    provider: DataProvider,
    end: date | None = None,
    lookback_days: int = 365,
    recent_window_days: int = 3,
) -> tuple[dict[str, list[Signal]], dict[str, float]]:
    """Run each strategy over the candidate universe and collect BUY signals.

    Mirrors the dedup/recency logic in `runtime._run_one_cycle` but is
    factored out so the ensemble path can reuse it cleanly.

    Returns `(per_strategy_signals, weights)`.
    """
    end = end or date.today()
    start = end - timedelta(days=lookback_days)
    recent_cutoff = end - timedelta(days=recent_window_days)

    per_strategy: dict[str, list[Signal]] = {}
    weights: dict[str, float] = {}

    # Fetch each code's bars ONCE — strategies share the same history window,
    # so re-reading per strategy was K× redundant DB/provider work per tick.
    bars_by_code = {code: provider.get_daily_bars(code, start, end)
                    for code in candidates}

    for strat, weight in strategies_with_weights:
        latest: dict[tuple[str, str], tuple[date, Signal]] = {}
        for code in candidates:
            bars = bars_by_code[code]
            for bar in bars:
                if bar.date < recent_cutoff:
                    strat.on_bar(bar)
                    continue
                produced = strat.on_bar(bar)
                for sig in produced:
                    key = (sig.stock_code, sig.direction.value)
                    prior = latest.get(key)
                    if prior is None or bar.date >= prior[0]:
                        latest[key] = (bar.date, sig)
        per_strategy[strat.name] = [v[1] for v in latest.values()]
        weights[strat.name] = weight

    return per_strategy, weights
