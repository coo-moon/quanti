"""Base strategy interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from quanti.models import BarData, Signal


class BaseStrategy(ABC):
    """Base class for all trading strategies."""

    name: str = "unnamed"

    @abstractmethod
    def init(self, config: dict) -> None:
        """Initialize strategy with configuration parameters."""

    @abstractmethod
    def on_bar(self, bar: BarData) -> list[Signal]:
        """Process a new bar and optionally generate signals."""

    def on_factor(self, factors: pd.DataFrame) -> list[Signal]:
        """Process factor cross-section data. Override for factor-based strategies."""
        return []
