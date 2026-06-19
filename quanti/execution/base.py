"""Broker interface — the contract the agent runtime executes against.

`PaperBroker` is the simulated implementation; a future `QmtBroker` (miniQMT
via xtquant) will implement the same surface so the runtime can switch venues
without changing the decision/risk pipeline. The runtime depends on this
`Broker` Protocol, not on any concrete broker.

The value types (`BrokerResult`, `PendingFillResult`) live here because they're
part of the contract — what `execute_signals` / `try_fill_pending_orders`
return — not implementation detail of any one broker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from quanti.models import Signal


@dataclass
class BrokerResult:
    """Outcome of a batch submit (`execute_signals`)."""
    accepted: int = 0
    rejected: int = 0
    filled: int = 0
    pending: int = 0  # signals queued for next-open fill (pending mode)
    reasons: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.reasons is None:
            self.reasons = []


@dataclass
class PendingFillResult:
    """Summary of one `try_fill_pending_orders()` pass."""
    scanned: int = 0
    filled: int = 0
    rejected: int = 0      # risk re-check failed at fill time
    expired: int = 0       # TTL exceeded without a fillable bar
    still_pending: int = 0
    reasons: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.reasons is None:
            self.reasons = []


@runtime_checkable
class Broker(Protocol):
    """What the agent runtime needs from a broker, paper or live.

    A live broker (QmtBroker) implements the same methods; the difference is
    that submits hit a real venue and `snapshot_portfolio` reflects the
    broker account (reconciled), not a local simulation.
    """

    def execute_signal(self, signal: Signal, strategy_name: str = "") -> bool:
        """Submit one signal. Returns True if it landed (filled in immediate
        mode, or successfully queued in pending mode)."""
        ...

    def execute_signals(self, signals: list[Signal],
                        strategy_name: str = "") -> BrokerResult:
        """Submit a batch; returns per-batch counters."""
        ...

    def try_fill_pending_orders(self) -> PendingFillResult:
        """Advance the pending-order queue (fill what's now fillable, expire
        what's too old). Called at the start of each tick."""
        ...

    def check_exits(self) -> int:
        """Run exit rules (stop-loss / strategy exit / take-profit) and
        submit the resulting sells. Returns the count acted on."""
        ...

    def snapshot_portfolio(self) -> dict:
        """Current cash / positions / market value / pnl as a dict."""
        ...

    def pending_orders_detail(self) -> list[dict]:
        """Pending orders enriched with their fill timeline (for the UI)."""
        ...

    # ------------------------------------------------------------ health
    def is_connected(self) -> bool:
        """Whether the broker can currently submit/query orders.

        PaperBroker is always connected (no external venue). A live broker
        (QmtBroker) returns False when the qmt-bridge / QMT client / trading
        account is unreachable — the runtime should refuse to trade rather
        than silently queue orders that will never reach the venue.
        """
        ...

    # -------------------------------------------------------- order control
    def cancel_order(self, order_id: str) -> bool:
        """Cancel a single still-open order. Returns True if it was cancelled
        (or already terminal in a way that needs no action). For paper this
        flips a PENDING order to CANCELLED; for live it sends a venue cancel
        and reconciles on the callback."""
        ...

    def cancel_all_pending(self) -> int:
        """Cancel every still-open order. Returns the count cancelled.

        First half of the kill switch — stop anything from filling. Safe to
        call repeatedly (idempotent: already-terminal orders are skipped)."""
        ...

    def flatten(self, reason: str = "kill-switch") -> int:
        """Submit exit (SELL) orders for all current holdings. Returns the
        number of positions acted on.

        Second half of the kill switch — get out of the market. Goes through
        the normal submit path so RiskManager and venue rules still apply;
        T+1 unsellable lots are simply skipped by the broker."""
        ...
