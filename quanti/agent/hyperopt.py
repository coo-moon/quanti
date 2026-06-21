"""Walk-forward parameter optimization (hyperopt) for strategies.

A1 design: grid-search each strategy's `param_space` on a TRAIN window, then
validate the winning combo AND the default config out-of-sample (reusing
run_walk_forward); accept the tuned combo only if it beats the default OOS by
a margin and passes the fold/trade guards. On-demand (CLI + async API), never
per agent cycle. See docs/superpowers/specs/2026-06-21-walk-forward-hyperopt-design.md.
"""

from __future__ import annotations

import itertools
import logging
import random

logger = logging.getLogger(__name__)


def build_grid(param_space: dict[str, list], max_combos: int,
               seed: int) -> tuple[list[dict], int]:
    """Cartesian product of `param_space` → list of full param dicts.

    Returns (combos, total_before_cap). If the product exceeds `max_combos`,
    randomly sample `max_combos` of them with a fixed seed (reproducible) and
    log the dropped count — never silently truncate."""
    if not param_space:
        return [], 0
    keys = list(param_space.keys())
    value_lists = [param_space[k] for k in keys]
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*value_lists)]
    total = len(combos)
    if total > max_combos:
        combos = random.Random(seed).sample(combos, max_combos)
        logger.info("hyperopt grid capped: %d → %d combos (sampled, seed=%d)",
                    total, max_combos, seed)
    return combos, total
