"""MACD Golden/Death Cross Strategy."""

from quanti.models import BarData, Direction, Signal
from quanti.strategy.base import BaseStrategy


class MACDCrossStrategy(BaseStrategy):
    """Buy on MACD golden cross (DIF crosses above DEA), sell on death cross."""

    name = "macd_cross"
    name_zh = "MACD 金叉"
    description = "MACD 金叉(DIF 上穿 DEA)买入,死叉(DIF 下穿 DEA)卖出"
    param_space = {"fast": [8, 12], "slow": [21, 26, 30]}

    def init(self, config: dict) -> None:
        self.fast = config.get("fast", 12)
        self.slow = config.get("slow", 26)
        self.signal_period = config.get("signal", 9)
        self._prices: dict[str, list[float]] = {}

    def on_bar(self, bar: BarData) -> list[Signal]:
        prices = self._prices.setdefault(bar.code, [])
        prices.append(bar.close)

        min_bars = self.slow + self.signal_period + 1
        if len(prices) < min_bars:
            return []

        import pandas as pd

        s = pd.Series(prices)
        ema_fast = s.ewm(span=self.fast, adjust=False).mean()
        ema_slow = s.ewm(span=self.slow, adjust=False).mean()
        dif = ema_fast - ema_slow
        dea = dif.ewm(span=self.signal_period, adjust=False).mean()

        dif_curr, dif_prev = dif.iloc[-1], dif.iloc[-2]
        dea_curr, dea_prev = dea.iloc[-1], dea.iloc[-2]

        signals = []
        if dif_prev <= dea_prev and dif_curr > dea_curr:
            signals.append(
                Signal(
                    stock_code=bar.code,
                    direction=Direction.BUY,
                    strength=0.7,
                    reason=f"MACD golden cross (DIF={dif_curr:.3f})",
                )
            )
        elif dif_prev >= dea_prev and dif_curr < dea_curr:
            signals.append(
                Signal(
                    stock_code=bar.code,
                    direction=Direction.SELL,
                    strength=0.7,
                    reason=f"MACD death cross (DIF={dif_curr:.3f})",
                )
            )
        return signals
