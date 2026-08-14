"""Tests for the strategy health gate (breaker/deep_loss exclusion)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from quanti.agent import strategy_gate
from quanti.data.database import Database
from quanti.models import Direction, Signal
from quanti.strategy.base import BaseStrategy


class _BuyEveryBar(BaseStrategy):
    name = "buy_every_bar"

    def init(self, config):
        pass

    def on_bar(self, bar):
        return [Signal(stock_code=bar.code, direction=Direction.BUY,
                       strength=1.0, reason="test")]


class _Market:
    """Minimal provider over a synthetic market for one code."""

    def __init__(self, prices):
        self._prices = prices
        self._dates = [d.date() for d in
                       pd.bdate_range("2024-01-02", periods=len(prices))]

    def get_trade_dates(self, start, end):
        return [d for d in self._dates if start <= d <= end]

    def get_daily_bars(self, code, start, end, adjust="hfq"):
        from quanti.models import BarData
        out = []
        for d, p in zip(self._dates, self._prices):
            if start <= d <= end and p > 0:
                out.append(BarData(code=code, date=d, open=p, high=p * 1.01,
                                   low=p * 0.99, close=p, volume=1e6,
                                   amount=p * 1e6, turnover=1.0))
        return out

    def get_daily_df(self, code, start, end):
        n = len(self._prices)
        df = pd.DataFrame({
            "date": self._dates, "open": self._prices,
            "high": [p * 1.01 for p in self._prices],
            "low": [p * 0.99 for p in self._prices],
            "close": self._prices,
            "volume": [1e6] * n, "amount": [p * 1e6 for p in self._prices],
            "turnover": [1.0] * n,
        })
        return df[(df["date"] >= start) & (df["date"] <= end)]


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "g.db"))
    d.initialize()
    yield d
    d.close()


class _FakeLoader:
    """Stand-in for StrategyLoader. compute_gate instantiates the class, so
    strategies are injected through the module-level _FAKE_STRATEGIES."""

    def __init__(self, strategies=None):
        self._strategies = strategies if strategies is not None else _FAKE_STRATEGIES

    def load_directory(self, strategies_dir):
        return list(self._strategies)


_FAKE_STRATEGIES: list[BaseStrategy] = []


def test_gate_breaker_verdict(db, monkeypatch):
    """A market that collapses hard enough trips the -30% portfolio breaker
    under DEFAULT risk (ATR stops on) → verdict breaker, persisted."""
    prices = [10.0 - 0.25 * i for i in range(60)]
    provider = _Market(prices)
    monkeypatch.setattr(strategy_gate, "_resolve_universe",
                        lambda db, provider, as_of, max_codes: ["000001"])
    _FAKE_STRATEGIES[:] = [_BuyEveryBar()]
    monkeypatch.setattr(strategy_gate, "StrategyLoader", _FakeLoader)
    report = strategy_gate.compute_gate(db, provider, end=date(2025, 12, 31))
    assert "buy_every_bar" in report
    g = report["buy_every_bar"]
    assert g["verdict"] == "breaker", g
    assert g["halted"] is True
    assert g["halted_at"]
    # persisted and excluded
    assert "buy_every_bar" in strategy_gate.excluded_names(db)


def test_gate_pass_verdict(db, monkeypatch):
    """A gentle uptrend with the always-buy strategy → pass, not excluded."""
    prices = [10.0 + 0.02 * i for i in range(60)]
    provider = _Market(prices)
    monkeypatch.setattr(strategy_gate, "_resolve_universe",
                        lambda db, provider, as_of, max_codes: ["000001"])
    _FAKE_STRATEGIES[:] = [_BuyEveryBar()]
    monkeypatch.setattr(strategy_gate, "StrategyLoader", _FakeLoader)
    report = strategy_gate.compute_gate(db, provider, end=date(2025, 12, 31))
    g = report["buy_every_bar"]
    assert g["verdict"] == "pass", g
    assert "buy_every_bar" not in strategy_gate.excluded_names(db)


def test_gate_deep_loss_verdict(db, monkeypatch):
    """No breaker but deeply negative Sharpe → deep_loss, excluded."""
    class _FakeResult:
        halted = False
        halted_at = None
        metrics = {"sharpe_ratio": -0.9, "max_drawdown": -0.15}
        trades = []

    class _FakeEngine:
        def __init__(self, *a, **kw):
            pass

        def run(self, strategy, codes, start, end):
            return _FakeResult()

    monkeypatch.setattr(strategy_gate, "_resolve_universe",
                        lambda db, provider, as_of, max_codes: ["000001"])
    _FAKE_STRATEGIES[:] = [_BuyEveryBar()]
    monkeypatch.setattr(strategy_gate, "StrategyLoader", _FakeLoader)
    monkeypatch.setattr(strategy_gate, "BacktestEngine", _FakeEngine)
    report = strategy_gate.compute_gate(db, _Market([10.0] * 60),
                                        end=date(2025, 12, 31))
    g = report["buy_every_bar"]
    assert g["verdict"] == "deep_loss", g
    assert "buy_every_bar" in strategy_gate.excluded_names(db)


def test_gate_skip_on_backtest_failure(db, monkeypatch):
    class _BoomEngine:
        def __init__(self, *a, **kw):
            pass

        def run(self, strategy, codes, start, end):
            raise RuntimeError("boom")

    monkeypatch.setattr(strategy_gate, "_resolve_universe",
                        lambda db, provider, as_of, max_codes: ["000001"])
    _FAKE_STRATEGIES[:] = [_BuyEveryBar()]
    monkeypatch.setattr(strategy_gate, "StrategyLoader", _FakeLoader)
    monkeypatch.setattr(strategy_gate, "BacktestEngine", _BoomEngine)
    report = strategy_gate.compute_gate(db, _Market([10.0] * 60),
                                        end=date(2025, 12, 31))
    g = report["buy_every_bar"]
    assert g["verdict"] == ""  # no verdict → stays eligible
    assert "buy_every_bar" not in strategy_gate.excluded_names(db)


def test_gate_persistence_roundtrip(db):
    db.save_strategy_gate("a", date(2026, 8, 14), "breaker", "熔断",
                          sharpe=-1.0, max_drawdown=-0.4, halted=True)
    db.save_strategy_gate("b", date(2026, 8, 14), "pass", "ok",
                          sharpe=0.3, max_drawdown=-0.1, halted=False)
    gate = db.load_strategy_gate()
    assert gate["a"]["verdict"] == "breaker"
    assert gate["a"]["halted"] is True
    assert gate["b"]["verdict"] == "pass"
    assert strategy_gate.excluded_names(db) == {"a"}


def test_selector_drops_gated_strategies(db, monkeypatch):
    """The selector must not rank a gated strategy — walk-forward can never
    re-admit an account-killer."""
    from quanti.agent.selector import StrategySelector
    db.save_strategy_gate("ma_cross", date(2026, 8, 14), "breaker", "熔断",
                          sharpe=-1.0, max_drawdown=-0.4, halted=True)
    sel = StrategySelector(db, _Market([10.0] * 30))
    names = [s.name for s in sel._gated_candidates()]
    assert "ma_cross" not in names
    assert "macd_cross" in names  # not gated → still eligible

