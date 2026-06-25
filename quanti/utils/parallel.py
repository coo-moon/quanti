"""Tiny stdlib thread-pool helper for the agent's parallel sections (selector
per-strategy, hyperopt grid, factor IC scoring).

Threads, not processes: the shared Database is thread-safe via an RLock'd
connection (check_same_thread=False), so we avoid per-process SQLite/connection
churn. The GIL caps pure-Python speedup, but DB reads and pandas/numpy sections
release it and overlap. ponytail: ThreadPoolExecutor, not a custom pool.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor


def thread_map(fn: Callable, items: Iterable, *, workers: int | None = None) -> list:
    """Map ``fn`` over ``items`` across threads, preserving input order.

    Runs inline (no pool spawned) for 0 or 1 items. ``fn`` should handle its
    own exceptions — an uncaught one propagates when results are collected.
    """
    items = list(items)
    if len(items) <= 1:
        return [fn(x) for x in items]
    w = workers or min(len(items), (os.cpu_count() or 4))
    with ThreadPoolExecutor(max_workers=w) as ex:
        return list(ex.map(fn, items))
