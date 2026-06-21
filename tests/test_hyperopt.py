from __future__ import annotations

from quanti.agent.hyperopt import build_grid


def test_build_grid_cartesian_product():
    combos, total = build_grid({"a": [1, 2], "b": [10, 20, 30]}, max_combos=64, seed=42)
    assert total == 6 and len(combos) == 6
    assert {"a": 1, "b": 10} in combos and {"a": 2, "b": 30} in combos


def test_build_grid_empty_space():
    assert build_grid({}, max_combos=64, seed=42) == ([], 0)


def test_build_grid_caps_and_is_deterministic():
    space = {"a": list(range(10)), "b": list(range(10))}  # 100 combos
    combos1, total1 = build_grid(space, max_combos=16, seed=42)
    combos2, _ = build_grid(space, max_combos=16, seed=42)
    assert total1 == 100 and len(combos1) == 16
    assert combos1 == combos2  # same seed → same sample
