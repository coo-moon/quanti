"""Slippage models for backtest realism.

The old engine used a flat 0.1% slippage on every fill. That's fine for
small orders in liquid names — but if the strategy gets aggressive (say,
1% of a stock's daily turnover in a single bar) the real fill price is
materially worse. Two models:

  * `FlatSlippage(bps=10)` — backward compatible. Constant cost.

  * `VolumeImpactSlippage(base_bps=5, impact_bps_per_pct=5, alpha=0.5)` —
    square-root market-impact model. Cost grows with the order's share of
    the 20-day average daily turnover (ADV):

      cost_bps = base_bps + impact_bps_per_pct * (participation_pct ** alpha)

    where `participation_pct = 100 * notional / adv20`. At 1% participation
    you pay base + 5 ≈ 10 bps (matches old default); at 10% you pay base +
    ~16 bps; at 100% you pay base + 50 bps. Calibrated to roughly match
    empirical A-share retail-scale impact studies — not precise, but it
    keeps the engine honest about scaling.

ADV is fed in by the engine, which precomputes a rolling 20-bar mean of
each code's `amount` column. If ADV is missing or zero (e.g. brand-new
listing, suspended stock), the model degrades to base_bps + a warning.
"""

from __future__ import annotations

import logging
from typing import Protocol

from quanti.models import Direction

logger = logging.getLogger(__name__)


class SlippageModel(Protocol):
    def adjust(self, *, code: str, price: float, qty: int,
               direction: Direction, adv20: float) -> float:
        """Return the slippage as a fraction (e.g. 0.001 == 10 bps).

        Returned value is a *one-sided* cost; the engine adds it to BUY
        price and subtracts it from SELL price. Always non-negative.
        """
        ...


class FlatSlippage:
    """Constant cost in basis points. Backward-compatible with the old engine."""

    def __init__(self, bps: float = 10.0) -> None:
        if bps < 0:
            raise ValueError(f"slippage bps must be non-negative, got {bps}")
        self._frac = bps / 10000.0

    def adjust(self, *, code: str, price: float, qty: int,
               direction: Direction, adv20: float) -> float:
        return self._frac


class VolumeImpactSlippage:
    """Square-root market-impact model.

    Args:
        base_bps: minimum slippage (spread crossing, etc.). 5 bps default.
        impact_bps_per_pct: bps of impact charged at 1% participation. 5
            default — so 1% of ADV costs 10 bps total (matches the old flat).
        alpha: exponent on participation. 0.5 (square root) is the canonical
            Almgren-Chriss / Kyle answer. Set 1.0 for linear, 0.0 for flat.
        max_bps: hard cap so a single insane order doesn't claim 500% cost.
    """

    def __init__(
        self,
        base_bps: float = 5.0,
        impact_bps_per_pct: float = 5.0,
        alpha: float = 0.5,
        max_bps: float = 300.0,
    ) -> None:
        self._base = base_bps
        self._impact = impact_bps_per_pct
        self._alpha = alpha
        self._max_bps = max_bps

    def adjust(self, *, code: str, price: float, qty: int,
               direction: Direction, adv20: float) -> float:
        notional = max(price, 0) * max(qty, 0)
        if adv20 is None or adv20 <= 0:
            # No ADV data — fall back to base. Warn at debug level so logs
            # don't get flooded; the engine already accepts this gracefully.
            logger.debug(f"slippage: no ADV for {code}, using base {self._base} bps")
            return self._base / 10000.0
        if notional <= 0:
            return self._base / 10000.0
        participation_pct = 100.0 * notional / adv20
        impact_bps = self._impact * (participation_pct ** self._alpha)
        total = min(self._base + impact_bps, self._max_bps)
        return total / 10000.0


def coerce(slippage) -> SlippageModel:
    """Convert legacy float configs to the new model. Used by BacktestEngine."""
    if isinstance(slippage, (int, float)):
        # Legacy: float fraction (e.g. 0.001 == 10 bps).
        return FlatSlippage(bps=float(slippage) * 10000.0)
    return slippage
