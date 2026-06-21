"""Moving Average Crossover Strategy."""

from quanti.models import BarData, Direction, Signal
from quanti.strategy.base import BaseStrategy


class MACrossStrategy(BaseStrategy):
    """Buy when short MA crosses above long MA, sell when it crosses below."""

    name = "ma_cross"
    name_zh = "均线交叉"
    description = "短期 MA 上穿长期 MA → 金叉买入;短期 MA 下穿长期 MA → 死叉卖出"
    param_space = {"short_period": [3, 5, 8, 10], "long_period": [20, 30, 60]}

    def init(self, config: dict) -> None:
        self.short_period = config.get("short_period", 5)
        self.long_period = config.get("long_period", 20)
        self._prices: dict[str, list[float]] = {}

    def on_bar(self, bar: BarData) -> list[Signal]:
        prices = self._prices.setdefault(bar.code, [])
        prices.append(bar.close)

        if len(prices) < self.long_period + 1:
            return []

        short_ma_prev = sum(prices[-(self.short_period + 1) : -1]) / self.short_period
        short_ma_curr = sum(prices[-self.short_period :]) / self.short_period
        long_ma_prev = sum(prices[-(self.long_period + 1) : -1]) / self.long_period
        long_ma_curr = sum(prices[-self.long_period :]) / self.long_period

        signals = []
        # Golden cross: short MA crosses above long MA
        if short_ma_prev <= long_ma_prev and short_ma_curr > long_ma_curr:
            signals.append(
                Signal(
                    stock_code=bar.code,
                    direction=Direction.BUY,
                    strength=0.7,
                    reason=f"MA{self.short_period} crossed above MA{self.long_period}",
                )
            )
        # Death cross: short MA crosses below long MA
        elif short_ma_prev >= long_ma_prev and short_ma_curr < long_ma_curr:
            signals.append(
                Signal(
                    stock_code=bar.code,
                    direction=Direction.SELL,
                    strength=0.7,
                    reason=f"MA{self.short_period} crossed below MA{self.long_period}",
                )
            )

        return signals
