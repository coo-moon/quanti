"""Turtle Breakout Strategy (Donchian Channel)."""

from quanti.models import BarData, Direction, Signal
from quanti.strategy.base import BaseStrategy


class TurtleBreakoutStrategy(BaseStrategy):
    """Buy on N-day high breakout, sell on N/2-day low breakdown (classic Turtle)."""

    name = "turtle_breakout"
    name_zh = "海龟突破"
    description = "突破 20 日新高买入,跌破 10 日新低卖出(经典海龟交易法则)"
    param_space = {"entry_period": [10, 20, 55], "exit_period": [5, 10, 20]}

    def init(self, config: dict) -> None:
        self.entry_period = config.get("entry_period", 20)  # Buy on 20-day high
        self.exit_period = config.get("exit_period", 10)  # Sell on 10-day low
        self._highs: dict[str, list[float]] = {}
        self._lows: dict[str, list[float]] = {}
        self._closes: dict[str, list[float]] = {}

    def on_bar(self, bar: BarData) -> list[Signal]:
        highs = self._highs.setdefault(bar.code, [])
        lows = self._lows.setdefault(bar.code, [])
        closes = self._closes.setdefault(bar.code, [])
        highs.append(bar.high)
        lows.append(bar.low)
        closes.append(bar.close)

        if len(highs) < self.entry_period + 1:
            return []

        # Donchian channel
        entry_high = max(highs[-(self.entry_period + 1) : -1])  # Previous N-day high
        exit_low = min(lows[-self.exit_period - 1 : -1]) if len(lows) > self.exit_period else min(lows[:-1])

        signals = []
        # Breakout above N-day high -> buy
        if bar.close > entry_high:
            signals.append(
                Signal(
                    stock_code=bar.code,
                    direction=Direction.BUY,
                    strength=0.8,
                    reason=f"Broke {self.entry_period}-day high ({bar.close:.2f} > {entry_high:.2f})",
                )
            )
        # Breakdown below N/2-day low -> sell
        elif bar.close < exit_low:
            signals.append(
                Signal(
                    stock_code=bar.code,
                    direction=Direction.SELL,
                    strength=0.8,
                    reason=f"Broke {self.exit_period}-day low ({bar.close:.2f} < {exit_low:.2f})",
                )
            )
        return signals
