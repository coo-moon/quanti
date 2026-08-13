# tests/test_param_space.py
from __future__ import annotations

from quanti.strategy.loader import StrategyLoader


def test_param_spaces_declared_sane():
    strats = {s.name: s for s in StrategyLoader().load_directory("strategies")}
    expected = {
        "ma_cross": {"short_period", "long_period"},
        "macd_cross": {"fast", "slow"},
        "bollinger_band": {"period", "std_dev"},
        "rsi_ob_os": {"period", "oversold", "overbought"},
        "turtle_breakout": {"entry_period", "exit_period"},
    }
    for name, keys in expected.items():
        s = strats[name]
        space = type(s).param_space
        assert set(space.keys()) == keys, f"{name} param_space keys"
        # Every space stays within the 64-combo cap.
        n = 1
        for vals in space.values():
            assert isinstance(vals, list) and len(vals) >= 1
            n *= len(vals)
        assert n <= 64, f"{name} grid {n} exceeds cap"


def test_base_default_param_space_empty():
    from quanti.strategy.base import BaseStrategy
    assert BaseStrategy.param_space == {}
