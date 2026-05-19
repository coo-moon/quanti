"""Factor registration and computation framework."""

from __future__ import annotations

from typing import Callable

import pandas as pd


class FactorRegistry:
    """Registry for factor computation functions."""

    def __init__(self):
        self._factors: dict[str, Callable[[pd.DataFrame], pd.Series]] = {}

    def register(self, name: str):
        """Decorator to register a factor function."""

        def decorator(func: Callable[[pd.DataFrame], pd.Series]):
            self._factors[name] = func
            return func

        return decorator

    def compute(self, name: str, df: pd.DataFrame) -> pd.Series:
        """Compute a named factor on the given DataFrame."""
        if name not in self._factors:
            raise KeyError(f"Unknown factor: {name}")
        return self._factors[name](df)

    def compute_all(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute all registered factors, return as DataFrame."""
        result = {}
        for name, func in self._factors.items():
            result[name] = func(df)
        return pd.DataFrame(result, index=df.index)

    def list_factors(self) -> list[str]:
        return list(self._factors.keys())


# Global registry instance
default_registry = FactorRegistry()


def register_factor(name: str):
    """Convenience decorator using the default registry."""
    return default_registry.register(name)
