"""Dynamic screener loader."""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

from quanti.screener.base import BaseScreener

logger = logging.getLogger(__name__)


class ScreenerLoader:
    """Load screener plugins from a directory."""

    def load_directory(self, directory: str | Path) -> list[BaseScreener]:
        """Scan a directory for BaseScreener subclasses and return instances."""
        directory = Path(directory)
        if not directory.is_dir():
            return []

        screeners: list[BaseScreener] = []
        for py_file in sorted(directory.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(py_file.stem, py_file)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, BaseScreener)
                        and attr is not BaseScreener
                    ):
                        screeners.append(attr())
            except Exception as e:
                logger.warning(f"Failed to load screener from {py_file}: {e}")
        return screeners
