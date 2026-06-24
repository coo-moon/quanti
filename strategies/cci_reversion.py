"""CCI (Commodity Channel Index) reversion.

Borrowed from backtrader / classic TA. CCI measures the typical price's deviation
from its mean, normalised by mean absolute deviation — a different oscillator
family from RSI (gain/loss) and Bollinger (std). Buy when CCI climbs back up
through -100 (leaving oversold), sell when it drops back through +100. Long-only.
"""

from quanti.models import BarData, Direction, Signal
from quanti.strategy.base import BaseStrategy


class CCIReversionStrategy(BaseStrategy):
    """Buy on CCI crossing up through the lower band, sell on crossing down through upper."""

    name = "cci_reversion"
    name_zh = "CCI 反转"
    description = "CCI 由超卖区上穿 -100 买入,由超买区下穿 +100 卖出(典型价偏离振荡)"
    param_space = {"period": [14, 20], "upper": [100, 150], "lower": [-100, -150]}

    def init(self, config: dict) -> None:
        self.period = config.get("period", 20)
        self.upper = config.get("upper", 100)
        self.lower = config.get("lower", -100)
        self.factor = 0.015  # Lambert's constant — fixed by convention, not tuned
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

        n = self.period
        if len(closes) < n + 1:
            return []

        import numpy as np

        tp = (np.array(highs) + np.array(lows) + np.array(closes)) / 3.0

        def _cci(window: "np.ndarray") -> float:
            m = window.mean()
            mad = np.abs(window - m).mean()
            return 0.0 if mad == 0 else (window[-1] - m) / (self.factor * mad)

        cci_curr = _cci(tp[-n:])
        cci_prev = _cci(tp[-(n + 1):-1])

        signals = []
        if cci_prev <= self.lower and cci_curr > self.lower:
            signals.append(Signal(
                stock_code=bar.code, direction=Direction.BUY, strength=0.7,
                reason=f"CCI 上穿 {self.lower}(脱离超卖, CCI={cci_curr:.0f})"))
        elif cci_prev >= self.upper and cci_curr < self.upper:
            signals.append(Signal(
                stock_code=bar.code, direction=Direction.SELL, strength=0.7,
                reason=f"CCI 下穿 {self.upper}(脱离超买, CCI={cci_curr:.0f})"))
        return signals
