"""KDJ (Stochastic) golden/death cross.

The most recognisable A-share timing oscillator. RSV = where close sits in the
N-day [low, high] range; K and D are its smoothed lines. Buy on a low-zone golden
cross (K up through D), sell on a high-zone death cross. Distinct from RSI
(gain/loss based) and Bollinger (std based): this uses high/low range position.
Long-only.
"""

from quanti.models import BarData, Direction, Signal
from quanti.strategy.base import BaseStrategy


class KDJCrossStrategy(BaseStrategy):
    """Buy on low-zone K-over-D golden cross, sell on high-zone death cross."""

    name = "kdj_cross"
    name_zh = "KDJ 金叉死叉"
    description = "低位 K 上穿 D(金叉)买入,高位 K 下穿 D(死叉)卖出(随机指标 KDJ)"
    param_space = {"period": [9, 14], "oversold": [20, 30], "overbought": [70, 80]}

    def init(self, config: dict) -> None:
        self.period = config.get("period", 9)
        self.oversold = config.get("oversold", 20)
        self.overbought = config.get("overbought", 80)
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

        # Recompute K/D from scratch each bar (seeded at 50) — deterministic, no
        # persisted cross-bar state, structurally no look-ahead.
        k, d = 50.0, 50.0
        ks: list[float] = []
        ds: list[float] = []
        for i in range(n - 1, len(closes)):
            ll = min(lows[i - n + 1 : i + 1])
            hh = max(highs[i - n + 1 : i + 1])
            rsv = 50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100.0
            k = (2 * k + rsv) / 3.0
            d = (2 * d + k) / 3.0
            ks.append(k)
            ds.append(d)

        if len(ks) < 2:
            return []
        prev_k, prev_d, k, d = ks[-2], ds[-2], ks[-1], ds[-1]

        signals = []
        if prev_k <= prev_d and k > d and d < self.oversold:
            signals.append(Signal(
                stock_code=bar.code, direction=Direction.BUY, strength=0.75,
                reason=f"KDJ 低位金叉 (K={k:.0f}>D={d:.0f}, D<{self.oversold})"))
        elif prev_k >= prev_d and k < d and d > self.overbought:
            signals.append(Signal(
                stock_code=bar.code, direction=Direction.SELL, strength=0.75,
                reason=f"KDJ 高位死叉 (K={k:.0f}<D={d:.0f}, D>{self.overbought})"))
        return signals
