"""Connors RSI(2) mean-reversion (buy panic in an uptrend).

Borrowed from Larry Connors' "Short Term Trading Strategies That Work" (a staple
in backtrader/quantifiedstrategies write-ups). Only when the long trend is up
(close > long MA) does it buy an extreme short-term oversold (RSI(2) ≤ thresh),
then takes profit when price reclaims a short MA. Distinct from the symmetric,
no-trend-filter RSI(14) strategy already built in. Long-only by construction.
"""

from quanti.models import BarData, Direction, Signal
from quanti.strategy.base import BaseStrategy


class ConnorsRSI2Strategy(BaseStrategy):
    """Buy RSI(2) extreme oversold while close > long MA; sell on reclaim of short MA."""

    name = "connors_rsi2"
    name_zh = "RSI2 顺势抄底"
    description = "大趋势向上(收盘>长期均线)时 RSI(2) 极端超卖买入,收回短期均线上方止盈"
    param_space = {"rsi_period": [2, 3], "buy_thresh": [5, 10, 15], "trend_ma": [150, 200]}

    def init(self, config: dict) -> None:
        self.rsi_period = config.get("rsi_period", 2)
        self.buy_thresh = config.get("buy_thresh", 10)
        self.trend_ma = config.get("trend_ma", 200)
        self.exit_ma = config.get("exit_ma", 5)
        self._prices: dict[str, list[float]] = {}

    def on_bar(self, bar: BarData) -> list[Signal]:
        prices = self._prices.setdefault(bar.code, [])
        prices.append(bar.close)

        if len(prices) < self.trend_ma + 1:
            return []

        import numpy as np

        p = np.array(prices, dtype=float)

        # Simple-average RSI over the short window (period=2 → mean of 2 deltas).
        # ponytail: simple not Wilder — at RSI(2) extremes the gate fires the same.
        deltas = np.diff(p[-(self.rsi_period + 1):])
        avg_g = np.where(deltas > 0, deltas, 0.0).mean()
        avg_l = np.where(deltas < 0, -deltas, 0.0).mean()
        rsi = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1 + avg_g / avg_l)

        ma_long = p[-self.trend_ma:].mean()
        ma_short_prev = p[-(self.exit_ma + 1):-1].mean()
        ma_short = p[-self.exit_ma:].mean()
        close, prev_close = p[-1], p[-2]

        signals = []
        if close > ma_long and rsi <= self.buy_thresh:
            strength = min(1.0, 0.7 + 0.3 * (self.buy_thresh - rsi) / max(self.buy_thresh, 1))
            signals.append(Signal(
                stock_code=bar.code, direction=Direction.BUY, strength=strength,
                reason=f"RSI{self.rsi_period}={rsi:.0f}≤{self.buy_thresh} 且 收盘>{self.trend_ma}MA"))
        elif prev_close <= ma_short_prev and close > ma_short:
            signals.append(Signal(
                stock_code=bar.code, direction=Direction.SELL, strength=0.7,
                reason=f"收回 {self.exit_ma} 日均线上方止盈"))
        return signals
