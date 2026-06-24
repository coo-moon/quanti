"""SuperTrend Strategy (ATR-adaptive trend follower).

Borrowed from freqtrade/jesse community strategies. A volatility-adaptive trend
line: bands = (high+low)/2 ± multiplier*ATR with the classic band-lock recursion,
flipping long/short as price crosses it. Distinct from the MA-cross / Donchian /
Bollinger strategies already built in — the band width scales with volatility.
Long-only here (BUY on flip-up, SELL on flip-down); short side dropped.
"""

from quanti.models import BarData, Direction, Signal
from quanti.strategy.base import BaseStrategy


class SuperTrendStrategy(BaseStrategy):
    """Buy when price flips above the SuperTrend line, sell when it flips below."""

    name = "supertrend"
    name_zh = "超级趋势"
    description = "ATR 自适应趋势线:收盘上穿翻多买入、下穿翻空卖出(轨宽随波动放缩)"
    param_space = {"period": [7, 10, 14], "multiplier": [2.0, 3.0, 4.0]}

    def init(self, config: dict) -> None:
        self.period = config.get("period", 10)
        self.multiplier = config.get("multiplier", 3.0)
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

        if len(closes) < self.period + 2:
            return []

        import numpy as np

        h, lo, c = (np.array(x, dtype=float) for x in (highs, lows, closes))
        # ATR = simple rolling mean of true range.
        # ponytail: SMA-ATR not Wilder — simpler, flip-equivalent for daily bars.
        prev_c = np.concatenate(([c[0]], c[:-1]))
        tr = np.maximum(h - lo, np.maximum(np.abs(h - prev_c), np.abs(lo - prev_c)))
        atr = np.full(len(c), np.nan)
        for i in range(self.period - 1, len(c)):
            atr[i] = tr[i - self.period + 1 : i + 1].mean()

        hl2 = (h + lo) / 2.0
        upper = hl2 + self.multiplier * atr
        lower = hl2 - self.multiplier * atr

        # Recompute final bands + direction from scratch each bar — deterministic,
        # so there is no persisted cross-bar state and look-ahead is impossible.
        f_up, f_lo = upper.copy(), lower.copy()
        direction = np.ones(len(c))  # +1 long / -1 short
        start = self.period - 1
        for i in range(start + 1, len(c)):
            f_up[i] = upper[i] if (upper[i] < f_up[i - 1] or c[i - 1] > f_up[i - 1]) else f_up[i - 1]
            f_lo[i] = lower[i] if (lower[i] > f_lo[i - 1] or c[i - 1] < f_lo[i - 1]) else f_lo[i - 1]
            if c[i] > f_up[i - 1]:
                direction[i] = 1
            elif c[i] < f_lo[i - 1]:
                direction[i] = -1
            else:
                direction[i] = direction[i - 1]

        signals = []
        if direction[-2] < 0 and direction[-1] > 0:
            signals.append(Signal(
                stock_code=bar.code, direction=Direction.BUY, strength=0.8,
                reason=f"SuperTrend 翻多 (ATR{self.period}×{self.multiplier})"))
        elif direction[-2] > 0 and direction[-1] < 0:
            signals.append(Signal(
                stock_code=bar.code, direction=Direction.SELL, strength=0.8,
                reason=f"SuperTrend 翻空 (ATR{self.period}×{self.multiplier})"))
        return signals
