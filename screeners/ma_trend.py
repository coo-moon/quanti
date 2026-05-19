"""MA Trend Screener - find stocks in bullish moving average alignment."""

from quanti.models import BarData
from quanti.screener.base import BaseScreener


class MATrendScreener(BaseScreener):
    """Select stocks where short MA > medium MA > long MA (bullish alignment)."""

    name = "ma_trend"
    description = "均线多头选股：筛选短期>中期>长期均线多头排列的股票"

    def init(self, config: dict) -> None:
        self.short_period = config.get("short_period", 5)
        self.mid_period = config.get("mid_period", 20)
        self.long_period = config.get("long_period", 60)

    def screen(self, code: str, bars: list[BarData]) -> float:
        if len(bars) < self.long_period:
            return 0.0

        closes = [b.close for b in bars]

        ma_short = sum(closes[-self.short_period :]) / self.short_period
        ma_mid = sum(closes[-self.mid_period :]) / self.mid_period
        ma_long = sum(closes[-self.long_period :]) / self.long_period

        # Bullish alignment: short > mid > long
        if not (ma_short > ma_mid > ma_long):
            return 0.0

        # Score by separation degree
        sep_short_mid = (ma_short - ma_mid) / ma_mid
        sep_mid_long = (ma_mid - ma_long) / ma_long

        # Also check price is above short MA
        price = closes[-1]
        if price < ma_short:
            return 0.0

        score = min((sep_short_mid + sep_mid_long) * 10, 1.0)
        return round(max(score, 0.1), 3)
