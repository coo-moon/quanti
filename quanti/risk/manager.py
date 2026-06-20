"""Risk management module."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

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

    # --- Exit overlays (see check_exits) ---
    take_profit_activate_pct: float = 0.15
    """Arm the trailing take-profit once a position is up at least this much.
    Below it, only the stop-loss governs. 0 disables take-profit entirely."""
    take_profit_trail_pct: float = 0.10
    """Once armed, exit if the price retraces this fraction from its post-entry
    peak. Lets winners run but locks in gains on a meaningful reversal."""
    strategy_exit_enabled: bool = True
    """Exit a holding when its owning entry-strategy emits a SELL on the
    latest bar (structure-based exit, coherent with why we bought)."""


class RiskManager:
    """Independent risk control layer between signals and execution."""

    def __init__(self, config: RiskConfig | None = None):
        self.config = config or RiskConfig()
        self._daily_trade_count = 0
        # Calendar day the count belongs to. Live/paper never call reset_daily(),
        # so the count auto-rolls when the real date changes — otherwise the cap
        # became a permanent lifetime lock after `max_daily_trades` trades.
        self._count_day: date | None = None

    def _roll_day_if_needed(self) -> None:
        """Reset the daily counter when the calendar day has changed."""
        today = date.today()
        if self._count_day != today:
            self._daily_trade_count = 0
            self._count_day = today

    def check(self, signal: Signal, portfolio: Portfolio) -> tuple[bool, str]:
        """Check if a signal passes risk rules. Returns (allowed, reason)."""
        self._roll_day_if_needed()
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

    def max_additional_buy_value(self, portfolio: Portfolio, code: str,
                                 industry: str = "") -> float:
        """Largest 元 value addable to `code` without breaching the single-stock,
        industry, or total-position caps — computed POST-trade (room up to the
        ceiling, net of what's already held). 0.0 when any cap is already at its
        limit. This is the real enforcement point for the hard caps; callers
        size buys against it. Pass `industry=""` to skip the industry cap (e.g.
        when industry data isn't available, as in the backtest)."""
        total = portfolio.total_value
        if total <= 0:
            return 0.0
        cfg = self.config
        held = portfolio.positions.get(code)
        stock_mv = held.market_value if held else 0.0
        stock_room = total * cfg.max_position_pct - stock_mv
        total_room = total * cfg.max_total_position_pct - portfolio.market_value
        if industry:
            ind_mv = sum(p.market_value for p in portfolio.positions.values()
                         if p.industry == industry)
            ind_room = total * cfg.max_industry_pct - ind_mv
        else:
            ind_room = float("inf")
        return max(0.0, min(stock_room, ind_room, total_room))

    def check_portfolio_stop(self, total_value: float, peak_value: float) -> bool:
        """True when equity has drawn down from its high-water mark past
        `portfolio_stop_loss_pct` (e.g. -15%). Portfolio-level circuit breaker
        — the caller flattens everything and halts the agent."""
        if peak_value <= 0:
            return False
        return ((total_value - peak_value) / peak_value
                <= self.config.portfolio_stop_loss_pct)

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

    def check_exits(
        self,
        portfolio: Portfolio,
        peaks: dict[str, float] | None = None,
        strategy_sell_codes: set[str] | None = None,
    ) -> list[Signal]:
        """Decide which holdings to close, combining three exit reasons.

        Pure logic — the caller supplies `peaks` (per-code post-entry high,
        for the trailing take-profit) and `strategy_sell_codes` (codes whose
        owning strategy says SELL); this method just applies the thresholds.
        One SELL per code, priority: stop-loss > strategy-exit > take-profit.

        Falls back to plain stop-loss when peaks/strategy info aren't given,
        so existing callers keep working.
        """
        peaks = peaks or {}
        strategy_sell_codes = strategy_sell_codes or set()
        cfg = self.config
        signals: list[Signal] = []
        for code, pos in portfolio.positions.items():
            # 1. Stop-loss — highest priority, always on.
            if pos.pnl_pct <= cfg.stop_loss_pct:
                signals.append(Signal(
                    stock_code=code, direction=Direction.SELL, strength=1.0,
                    reason=f"止损 {pos.pnl_pct:.1%} ≤ {cfg.stop_loss_pct:.1%}"))
                continue
            # 2. Strategy-coherent exit — the owning strategy flipped to SELL.
            if cfg.strategy_exit_enabled and code in strategy_sell_codes:
                signals.append(Signal(
                    stock_code=code, direction=Direction.SELL, strength=1.0,
                    reason="策略离场信号"))
                continue
            # 3. Trailing take-profit — armed above activate, exit on retrace.
            if cfg.take_profit_activate_pct > 0 and pos.pnl_pct >= cfg.take_profit_activate_pct:
                peak = peaks.get(code)
                if peak and pos.current_price > 0:
                    drawdown = (pos.current_price - peak) / peak
                    if drawdown <= -cfg.take_profit_trail_pct:
                        signals.append(Signal(
                            stock_code=code, direction=Direction.SELL, strength=1.0,
                            reason=(f"移动止盈 浮盈{pos.pnl_pct:+.1%} 自峰值回撤"
                                    f"{drawdown:.1%}")))
        return signals

    def reset_daily(self) -> None:
        """Reset daily counters. Backtest calls this per simulated day; live/
        paper rely on the auto-roll in `_roll_day_if_needed`."""
        self._daily_trade_count = 0
        self._count_day = date.today()

    def record_trade(self) -> None:
        """Record that a trade was executed."""
        self._roll_day_if_needed()
        self._daily_trade_count += 1
