"""Volume Breakout Screener - find stocks with unusual volume surge."""

from quanti.models import BarData
from quanti.screener.base import BaseScreener


class VolumeBreakoutScreener(BaseScreener):
    """Select stocks with volume significantly above average (potential breakout)."""

    name = "volume_breakout"
    description = "放量突破选股：筛选成交量显著放大的股票"

    def init(self, config: dict) -> None:
        self.vol_period = config.get("vol_period", 20)
        self.vol_ratio = config.get("vol_ratio", 2.0)  # Volume must be 2x average
        self.price_change_min = config.get("price_change_min", 0.01)  # At least 1% up

    def screen(self, code: str, bars: list[BarData]) -> float:
        if len(bars) < self.vol_period + 1:
            return 0.0

        latest = bars[-1]
        prev_close = bars[-2].close
        price_change = (latest.close - prev_close) / prev_close

        # Must be going up
        if price_change < self.price_change_min:
            return 0.0

        # Volume check
        avg_vol = sum(b.volume for b in bars[-self.vol_period - 1 : -1]) / self.vol_period
        if avg_vol == 0:
            return 0.0

        vol_ratio = latest.volume / avg_vol
        if vol_ratio < self.vol_ratio:
            return 0.0

        # Score based on volume multiplier (capped at 5x)
        score = min(vol_ratio / 5.0, 1.0) * 0.6 + min(price_change / 0.05, 1.0) * 0.4
        return round(min(score, 1.0), 3)
