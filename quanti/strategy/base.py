"""Base strategy interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from quanti.models import BarData, Signal


class BaseStrategy(ABC):
    """Base class for all trading strategies.

    `name` is the stable identifier — used as DB key, cache key, Goal field,
    MCP/CLI argument. NEVER change a published strategy's name unless you
    intend to break every stored Goal that pinned it.

    `name_zh` and `description` are display-only and safe to edit freely.
    Frontends render `name_zh` as the user-facing label and submit `name`
    as the value.
    """

    name: str = "unnamed"
    name_zh: str = ""       # display label, optional but recommended
    description: str = ""   # 1-line summary of what it does
    param_space: dict[str, list] = {}
    """Optional grid for walk-forward hyperopt: param name → candidate values.
    Empty = not tuned (strategy always uses init() defaults). Include the
    built-in defaults as candidates so the default config is in the grid."""
    selectable: bool = True
    """False = the selector's automatic pool skips this strategy (it can still
    be pinned explicitly via Goal.strategy_name or run from scripts). For
    strategies whose execution assumptions (e.g. portfolio-weight strength +
    FixedSizer) don't fit the per-stock signal path the selector evaluates."""

    @abstractmethod
    def init(self, config: dict) -> None:
        """Initialize strategy with configuration parameters."""

    @abstractmethod
    def on_bar(self, bar: BarData) -> list[Signal]:
        """Process a new bar and optionally generate signals."""

    def on_factor(self, factors: pd.DataFrame) -> list[Signal]:
        """Process factor cross-section data. Override for factor-based strategies."""
        return []
