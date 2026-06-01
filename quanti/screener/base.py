"""Base screener interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from quanti.models import BarData


class BaseScreener(ABC):
    """Base class for all stock screeners.

    `name` is the stable identifier (Goal field, MCP/CLI args). Don't rename.
    `name_zh` and `description` are display-only and edit-safe.
    """

    name: str = "unnamed"
    name_zh: str = ""       # display label, optional but recommended
    description: str = ""

    @abstractmethod
    def init(self, config: dict) -> None:
        """Initialize screener with configuration parameters."""

    @abstractmethod
    def screen(self, code: str, bars: list[BarData]) -> float:
        """Score a stock from 0 to 1. 0 = skip, >0 = recommendation strength."""
