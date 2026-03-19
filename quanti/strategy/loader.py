"""Dynamic strategy loader."""

from __future__ import annotations

import importlib.util
import inspect
import logging
from pathlib import Path

from quanti.strategy.base import BaseStrategy

logger = logging.getLogger(__name__)


class StrategyLoader:
    """Loads strategy classes from Python files."""

    def load_directory(self, path: str) -> list[BaseStrategy]:
        """Load all strategies from .py files in a directory."""
        strategies = []
        directory = Path(path)
        if not directory.is_dir():
            logger.warning(f"Strategy directory not found: {path}")
            return strategies

        for py_file in sorted(directory.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                loaded = self._load_file(py_file)
                strategies.extend(loaded)
            except Exception as e:
                logger.warning(f"Failed to load {py_file}: {e}")
        return strategies

    def _load_file(self, path: Path) -> list[BaseStrategy]:
        """Load strategy classes from a single file."""
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            return []
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        strategies = []
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, BaseStrategy)
                and obj is not BaseStrategy
                and not inspect.isabstract(obj)
            ):
                instance = obj()
                strategies.append(instance)
        return strategies
