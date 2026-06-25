"""thread_map + BacktestEngine.clone() — the parallel-backtest primitives."""

from __future__ import annotations

from quanti.utils.parallel import thread_map


def test_thread_map_preserves_order_and_handles_small():
    assert thread_map(lambda x: x * 2, []) == []          # empty: no pool
    assert thread_map(lambda x: x * 2, [3]) == [6]         # single: inline
    assert thread_map(lambda x: x * x, [1, 2, 3, 4, 5]) == [1, 4, 9, 16, 25]


def test_engine_clone_is_independent():
    """clone() must hand out FRESH RiskManager / ProtectionManager (run()
    mutates the risk manager's daily counters), or concurrent run()s race."""
    from quanti.backtest.engine import BacktestEngine
    from quanti.risk.manager import RiskConfig, RiskManager
    from quanti.risk.protections import ProtectionManager

    eng = BacktestEngine(provider=None,
                         risk_manager=RiskManager(RiskConfig(stop_loss_pct=-0.05)),
                         protection_manager=ProtectionManager())
    c = eng.clone()
    assert c is not eng
    assert c._risk is not eng._risk                   # not shared
    assert c._protections is not eng._protections
    assert c._risk.config.stop_loss_pct == -0.05      # config preserved
    assert c._provider is eng._provider               # provider reused (thread-safe)
