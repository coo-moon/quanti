"""RSI Overbought/Oversold Strategy."""

from quanti.models import BarData, Direction, Signal
from quanti.strategy.base import BaseStrategy


class RSIOverboughtOversoldStrategy(BaseStrategy):
    """Buy when RSI drops below oversold threshold, sell when it rises above overbought."""

    name = "rsi_ob_os"
    name_zh = "RSI 超买超卖"
    description = "RSI < 30 触发买入(超卖反弹),RSI > 70 触发卖出(超买回落)"
    param_space = {"period": [7, 14], "oversold": [20, 30], "overbought": [70, 80]}

    def init(self, config: dict) -> None:
        self.period = config.get("period", 14)
        self.oversold = config.get("oversold", 30)
        self.overbought = config.get("overbought", 70)
        self._prices: dict[str, list[float]] = {}

    def on_bar(self, bar: BarData) -> list[Signal]:
        prices = self._prices.setdefault(bar.code, [])
        prices.append(bar.close)

        if len(prices) < self.period + 2:
            return []

        # Compute RSI for last two bars
        import numpy as np

        deltas = np.diff(prices[-(self.period + 2) :])
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        def _rsi(g, loss):
            avg_g = np.mean(g)
            avg_l = np.mean(loss)
            if avg_l == 0:
                return 100.0
            return 100.0 - 100.0 / (1 + avg_g / avg_l)

        rsi_prev = _rsi(gains[: self.period], losses[: self.period])
        rsi_curr = _rsi(gains[-self.period :], losses[-self.period :])

        signals = []
        if rsi_prev >= self.oversold and rsi_curr < self.oversold:
            signals.append(
                Signal(
                    stock_code=bar.code,
                    direction=Direction.BUY,
                    strength=0.8,
                    reason=f"RSI entered oversold zone ({rsi_curr:.1f} < {self.oversold})",
                )
            )
        elif rsi_prev <= self.overbought and rsi_curr > self.overbought:
            signals.append(
                Signal(
                    stock_code=bar.code,
                    direction=Direction.SELL,
                    strength=0.8,
                    reason=f"RSI entered overbought zone ({rsi_curr:.1f} > {self.overbought})",
                )
            )
        return signals
