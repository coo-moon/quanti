"""Risk management module."""

from __future__ import annotations

from dataclasses import dataclass

from quanti.models import Direction, Portfolio, Signal


@dataclass
class RiskConfig:
    """Risk management configuration."""

    max_position_pct: float = 0.10  # Max 10% per stock
    max_industry_pct: float = 0.30  # Max 30% per industry
    max_total_position_pct: float = 0.80  # Max 80% invested
    stop_loss_pct: float = -0.08  # -8% stop loss per stock
    portfolio_stop_loss_pct: float = -0.15  # -15% portfolio drawdown stop
    max_daily_trades: int = 20
    blocked_prefixes: tuple[str, ...] = ("ST", "*ST")  # Block ST stocks


class RiskManager:
    """Independent risk control layer between signals and execution."""

    def __init__(self, config: RiskConfig | None = None):
        self.config = config or RiskConfig()
        self._daily_trade_count = 0

    def check(self, signal: Signal, portfolio: Portfolio) -> tuple[bool, str]:
        """Check if a signal passes risk rules. Returns (allowed, reason)."""
        # Always allow sells
        if signal.direction == Direction.SELL:
            return True, ""

        # Check total position ratio
        total_value = portfolio.total_value
        if total_value > 0:
            position_ratio = portfolio.market_value / total_value
            if position_ratio >= self.config.max_total_position_pct:
                return False, f"Total position ratio {position_ratio:.1%} exceeds limit {self.config.max_total_position_pct:.1%}"

        # Check single stock position ratio
        if signal.stock_code in portfolio.positions:
            pos = portfolio.positions[signal.stock_code]
            if total_value > 0:
                stock_ratio = pos.market_value / total_value
                if stock_ratio >= self.config.max_position_pct:
                    return False, f"Position in {signal.stock_code} at {stock_ratio:.1%} exceeds limit {self.config.max_position_pct:.1%}"

        # Check daily trade limit
        if self._daily_trade_count >= self.config.max_daily_trades:
            return False, f"Daily trade limit ({self.config.max_daily_trades}) reached"

        return True, ""

    def check_stop_loss(self, portfolio: Portfolio) -> list[Signal]:
        """Check positions against stop-loss rules. Returns sell signals for positions to close."""
        signals = []
        for code, pos in portfolio.positions.items():
            if pos.pnl_pct <= self.config.stop_loss_pct:
                signals.append(
                    Signal(
                        stock_code=code,
                        direction=Direction.SELL,
                        strength=1.0,
                        reason=f"Stop loss triggered: {pos.pnl_pct:.1%} <= {self.config.stop_loss_pct:.1%}",
                    )
                )
        return signals

    def reset_daily(self) -> None:
        """Reset daily counters. Call at start of each trading day."""
        self._daily_trade_count = 0

    def record_trade(self) -> None:
        """Record that a trade was executed."""
        self._daily_trade_count += 1
