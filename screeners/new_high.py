"""New High Screener - find stocks making N-day highs."""

from quanti.models import BarData
from quanti.screener.base import BaseScreener


class NewHighScreener(BaseScreener):
    """Select stocks breaking N-day highs (momentum)."""

    name = "new_high"
    name_zh = "创新高"
    description = "筛选创 N 日(默认 60 日)新高的强势股票(动量突破)"

    def init(self, config: dict) -> None:
        self.period = config.get("period", 60)

    def screen(self, code: str, bars: list[BarData]) -> float:
        if len(bars) < self.period:
            return 0.0

        latest_close = bars[-1].close
        prev_high = max(b.high for b in bars[-self.period : -1])

        # Current close must be at or near the period high
        if latest_close < prev_high:
            return 0.0

        # Score by how much it exceeds the previous high
        excess = (latest_close - prev_high) / prev_high
        score = min(excess * 20 + 0.5, 1.0)
        return round(score, 3)
