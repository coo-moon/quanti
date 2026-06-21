"""Bollinger Band Mean-Reversion Strategy."""

from quanti.models import BarData, Direction, Signal
from quanti.strategy.base import BaseStrategy


class BollingerBandStrategy(BaseStrategy):
    """Buy when price touches lower band, sell when it touches upper band."""

    name = "bollinger_band"
    name_zh = "布林带突破"
    description = "价格触及布林带下轨(超卖)→ 买入,触及上轨(超买)→ 卖出"
    param_space = {"period": [15, 20, 25], "std_dev": [1.5, 2.0, 2.5]}

    def init(self, config: dict) -> None:
        self.period = config.get("period", 20)
        self.std_dev = config.get("std_dev", 2.0)
        self._prices: dict[str, list[float]] = {}

    def on_bar(self, bar: BarData) -> list[Signal]:
        prices = self._prices.setdefault(bar.code, [])
        prices.append(bar.close)

        if len(prices) < self.period + 1:
            return []

        import numpy as np

        window_curr = prices[-self.period :]
        window_prev = prices[-(self.period + 1) : -1]

        ma_curr = np.mean(window_curr)
        std_curr = np.std(window_curr, ddof=1)
        upper_curr = ma_curr + self.std_dev * std_curr
        lower_curr = ma_curr - self.std_dev * std_curr

        ma_prev = np.mean(window_prev)
        std_prev = np.std(window_prev, ddof=1)
        lower_prev = ma_prev - self.std_dev * std_prev
        upper_prev = ma_prev + self.std_dev * std_prev

        close_curr = prices[-1]
        close_prev = prices[-2]

        signals = []
        # Price crosses below lower band -> buy (mean reversion)
        if close_prev >= lower_prev and close_curr < lower_curr:
            signals.append(
                Signal(
                    stock_code=bar.code,
                    direction=Direction.BUY,
                    strength=0.75,
                    reason=f"Price broke below lower Bollinger Band ({close_curr:.2f} < {lower_curr:.2f})",
                )
            )
        # Price crosses above upper band -> sell
        elif close_prev <= upper_prev and close_curr > upper_curr:
            signals.append(
                Signal(
                    stock_code=bar.code,
                    direction=Direction.SELL,
                    strength=0.75,
                    reason=f"Price broke above upper Bollinger Band ({close_curr:.2f} > {upper_curr:.2f})",
                )
            )
        return signals
