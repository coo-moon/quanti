"""Tests for strategy engine."""

from datetime import date


from quanti.models import BarData, Direction, Signal
from quanti.strategy.base import BaseStrategy
from quanti.strategy.loader import StrategyLoader


class SimpleTestStrategy(BaseStrategy):
    """A trivial strategy for testing."""

    name = "test_strategy"

    def init(self, config: dict) -> None:
        self.threshold = config.get("threshold", 10.0)

    def on_bar(self, bar: BarData) -> list[Signal]:
        if bar.close > self.threshold:
            return [
                Signal(
                    stock_code=bar.code,
                    direction=Direction.BUY,
                    strength=0.8,
                    reason="price above threshold",
                )
            ]
        return []


class TestBaseStrategy:
    def test_strategy_init(self):
        strategy = SimpleTestStrategy()
        strategy.init({"threshold": 15.0})
        assert strategy.threshold == 15.0

    def test_on_bar_generates_signal(self):
        strategy = SimpleTestStrategy()
        strategy.init({"threshold": 10.0})
        bar = BarData(
            code="000001",
            date=date(2024, 1, 2),
            open=10.0,
            high=11.0,
            low=9.5,
            close=10.5,
            volume=1_000_000,
            amount=10_500_000,
        )
        signals = strategy.on_bar(bar)
        assert len(signals) == 1
        assert signals[0].direction == Direction.BUY

    def test_on_bar_no_signal(self):
        strategy = SimpleTestStrategy()
        strategy.init({"threshold": 15.0})
        bar = BarData(
            code="000001",
            date=date(2024, 1, 2),
            open=10.0,
            high=11.0,
            low=9.5,
            close=10.5,
            volume=1_000_000,
            amount=10_500_000,
        )
        signals = strategy.on_bar(bar)
        assert len(signals) == 0


class TestStrategyLoader:
    def test_load_from_directory(self, tmp_path):
        strategy_file = tmp_path / "my_strategy.py"
        strategy_file.write_text(
            '''
from quanti.strategy.base import BaseStrategy
from quanti.models import BarData, Signal, Direction

class MyStrategy(BaseStrategy):
    name = "my_strategy"

    def init(self, config):
        pass

    def on_bar(self, bar):
        return []
'''
        )
        loader = StrategyLoader()
        strategies = loader.load_directory(str(tmp_path))
        assert len(strategies) == 1
        assert strategies[0].name == "my_strategy"
