"""Dual Moving Average + Volume Confirmation Strategy."""

from quanti.models import BarData, Direction, Signal
from quanti.strategy.base import BaseStrategy


class MAVolumeStrategy(BaseStrategy):
    """MA crossover confirmed by above-average volume to filter false breakouts."""

    name = "ma_volume"

    def init(self, config: dict) -> None:
        self.short_period = config.get("short_period", 5)
        self.long_period = config.get("long_period", 20)
        self.vol_period = config.get("vol_period", 20)
        self.vol_ratio = config.get("vol_ratio", 1.5)  # Volume must be 1.5x average
        self._prices: dict[str, list[float]] = {}
        self._volumes: dict[str, list[float]] = {}

    def on_bar(self, bar: BarData) -> list[Signal]:
        prices = self._prices.setdefault(bar.code, [])
        volumes = self._volumes.setdefault(bar.code, [])
        prices.append(bar.close)
        volumes.append(bar.volume)

        min_bars = max(self.long_period, self.vol_period) + 1
        if len(prices) < min_bars:
            return []

        # MA crossover check
        short_ma_prev = sum(prices[-(self.short_period + 1) : -1]) / self.short_period
        short_ma_curr = sum(prices[-self.short_period :]) / self.short_period
        long_ma_prev = sum(prices[-(self.long_period + 1) : -1]) / self.long_period
        long_ma_curr = sum(prices[-self.long_period :]) / self.long_period

        # Volume confirmation
        avg_vol = sum(volumes[-self.vol_period :]) / self.vol_period
        vol_confirmed = bar.volume > avg_vol * self.vol_ratio

        signals = []
        if short_ma_prev <= long_ma_prev and short_ma_curr > long_ma_curr and vol_confirmed:
            signals.append(
                Signal(
                    stock_code=bar.code,
                    direction=Direction.BUY,
                    strength=0.85,
                    reason=(
                        f"MA{self.short_period}/MA{self.long_period} golden cross "
                        f"+ volume {bar.volume / avg_vol:.1f}x avg"
                    ),
                )
            )
        elif short_ma_prev >= long_ma_prev and short_ma_curr < long_ma_curr:
            # Sell doesn't require volume confirmation
            signals.append(
                Signal(
                    stock_code=bar.code,
                    direction=Direction.SELL,
                    strength=0.75,
                    reason=f"MA{self.short_period}/MA{self.long_period} death cross",
                )
            )
        return signals
