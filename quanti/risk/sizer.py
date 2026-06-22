"""Position sizing strategies.

The legacy PaperBroker sizes positions as `strength × cash`, capped by the
RiskManager's per-stock %. That treats every stock the same despite hugely
different volatility profiles — a sleepy bank stock with 15% annual vol gets
the same capital as a meme-tech with 60% annual vol, even though the second
one is 4× as risky.

This module adds vol-targeting: each position is sized so that, in
expectation, it contributes the same to portfolio volatility. A portfolio
of 10 such positions targeting 18% annual vol works out to roughly 5% per
position on average, with low-vol names getting more and high-vol names
getting less. All sized weights are still hard-capped by the RiskManager's
single-stock limit — the sizer informs, doesn't override.

The legacy behavior is preserved as `FixedSizer`; PaperBroker continues to
use its inline logic when no sizer is supplied, so existing callers don't
see any change.
"""

from __future__ import annotations

import math
from typing import Protocol

from quanti.models import BarData


class Sizer(Protocol):
    def target_weight(
        self,
        *,
        code: str,
        signal_strength: float,
        recent_bars: list[BarData],
        portfolio_total_value: float,
    ) -> float:
        """Return target weight as a fraction of portfolio total value (0-1).

        Implementations should clamp to [0, max_pct] internally; the broker
        applies its own RiskManager-driven cap on top.
        """
        ...


class FixedSizer:
    """Equal-weight sizing scaled by signal strength.

    weight = max_pct × signal_strength, where signal_strength ∈ [0, 1].
    A strength of 1.0 deploys the per-stock cap; 0.5 deploys half.
    """

    def __init__(self, max_pct: float = 0.10) -> None:
        if not 0 < max_pct <= 1.0:
            raise ValueError(f"max_pct must be in (0, 1], got {max_pct}")
        self._max_pct = max_pct

    def target_weight(
        self,
        *,
        code: str,
        signal_strength: float,
        recent_bars: list[BarData],
        portfolio_total_value: float,
    ) -> float:
        s = max(0.0, min(1.0, signal_strength))
        return self._max_pct * s


class VolTargetSizer:
    """Inverse-volatility sizing targeting a portfolio-level vol budget.

    For each position:

        weight_i = clip( (target_vol / (n × σ_i)) × strength,
                         floor, max_pct )

    where σ_i is the annualized realized volatility from the last
    `lookback_days` bars, `n` is the intended position count, and
    `target_vol` is the portfolio-level annual vol target.

    If a stock has too little history (< `min_bars` bars), falls back to
    half of max_pct so we don't size new listings too aggressively.

    Args:
        target_portfolio_vol: portfolio-level annual vol target. 0.18 = 18%,
            which is roughly the long-term vol of the CSI 300; setting much
            higher meaningfully raises tail risk.
        lookback_days: window for realized vol estimation.
        max_pct: hard cap per position (matches RiskManager.max_position_pct
            for consistency; defaults to 10%).
        n_target_positions: intended portfolio breadth. With n=10 and
            target=18%, average weight comes out to ~5% per name.
        min_bars: minimum bars required before vol can be estimated; below
            this we use the conservative fallback.
        weight_floor: never size a non-zero signal below this fraction —
            otherwise extreme high-vol names get sized to essentially zero.
    """

    def __init__(
        self,
        target_portfolio_vol: float = 0.18,
        lookback_days: int = 60,
        max_pct: float = 0.10,
        n_target_positions: int = 10,
        min_bars: int = 20,
        weight_floor: float = 0.01,
    ) -> None:
        self._tgt = target_portfolio_vol
        self._lb = lookback_days
        self._max_pct = max_pct
        self._n = n_target_positions
        self._min_bars = min_bars
        self._floor = weight_floor

    def target_weight(
        self,
        *,
        code: str,
        signal_strength: float,
        recent_bars: list[BarData],
        portfolio_total_value: float,
    ) -> float:
        s = max(0.0, min(1.0, signal_strength))
        if s == 0:
            return 0.0

        if len(recent_bars) < self._min_bars:
            return self._max_pct * 0.5 * s  # Conservative until we have data

        closes = [float(b.close) for b in recent_bars[-self._lb:]]
        if len(closes) < 2:
            return self._max_pct * 0.5 * s

        returns: list[float] = []
        for i in range(1, len(closes)):
            if closes[i - 1] > 0 and closes[i] > 0:
                returns.append(math.log(closes[i] / closes[i - 1]))
        if not returns:
            return self._max_pct * 0.5 * s

        mean_r = sum(returns) / len(returns)
        var = sum((r - mean_r) ** 2 for r in returns) / max(len(returns) - 1, 1)
        ann_vol = math.sqrt(var) * math.sqrt(252)

        if ann_vol <= 0.001:
            # Vol effectively zero (flatlined stock, synthetic data) — use cap.
            return self._max_pct * s

        raw_w = (self._tgt / ann_vol) / self._n
        scaled = raw_w * s
        return min(self._max_pct, max(self._floor * s, scaled))


def compute_buy_target_value(
    *,
    cash: float,
    total_value: float,
    strength: float,
    size_cap: float,
    code: str,
    sizer: "Sizer | None" = None,
    recent_bars: list[BarData] | None = None,
    cash_buffer: float = 0.95,
) -> float:
    """Single source of truth for how much 元 a BUY deploys.

    Shared by the backtest engine and the live brokers (paper + QMT) so the
    three execution paths can't drift apart on sizing — the divergence the
    2026-06-22 audit flagged as C2 (the backtest ignored ``signal.strength``
    and deployed ~2.5x what live does).

    - **Without a sizer:** ``cash * cash_buffer * clamp(strength, 0.1, 1.0)``,
      then capped by ``size_cap``.
    - **With a sizer:** ``min(total_value * target_weight, size_cap,
      cash * cash_buffer)``.

    ``size_cap`` is the post-trade hard-cap room from
    ``RiskManager.max_additional_buy_value`` (single-stock + industry caps).
    Returns a 元 value *before* lot-rounding and the final affordability check.
    """
    if sizer is not None:
        target_w = sizer.target_weight(
            code=code,
            signal_strength=strength,
            recent_bars=recent_bars or [],
            portfolio_total_value=total_value,
        )
        return max(0.0, min(total_value * target_w, size_cap, cash * cash_buffer))
    cash_cap = cash * cash_buffer * max(min(strength, 1.0), 0.1)
    return max(0.0, min(cash_cap, size_cap))
