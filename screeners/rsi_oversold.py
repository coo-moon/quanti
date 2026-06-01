"""RSI Oversold Screener - find stocks entering oversold territory."""

import numpy as np

from quanti.models import BarData
from quanti.screener.base import BaseScreener


class RSIOversoldScreener(BaseScreener):
    """Select stocks with RSI below oversold threshold (potential bounce)."""

    name = "rsi_oversold"
    name_zh = "RSI 超卖"
    description = "筛选 RSI 低于阈值的超卖股票(反弹候选)"

    def init(self, config: dict) -> None:
        self.period = config.get("period", 14)
        self.threshold = config.get("threshold", 30)

    def screen(self, code: str, bars: list[BarData]) -> float:
        if len(bars) < self.period + 1:
            return 0.0

        closes = [b.close for b in bars]
        deltas = np.diff(closes[-self.period - 1 :])
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        avg_gain = np.mean(gains)
        avg_loss = np.mean(losses)
        if avg_loss == 0:
            return 0.0

        rs = avg_gain / avg_loss
        rsi = 100.0 - 100.0 / (1 + rs)

        if rsi < self.threshold:
            # Score higher the more oversold (RSI 0 -> score 1.0, RSI 30 -> score ~0.3)
            return round(1.0 - rsi / self.threshold, 3)
        return 0.0
