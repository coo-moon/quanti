"""Tests for the PaperBroker (live-mirror execution layer)."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.paper_broker import PaperBroker
from quanti.models import Direction, Signal
from quanti.risk.manager import RiskConfig


@pytest.fixture
def setup(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=15)
    np.random.seed(7)
    prices = 10 + np.cumsum(np.random.randn(len(dates)) * 0.05)
    df = pd.DataFrame({
        "code": "000001",
        "date": [d.date() for d in dates],
        "open": prices - 0.05,
        "high": prices + 0.1,
        "low": prices - 0.1,
        "close": prices,
        "volume": np.full(len(dates), 1_000_000.0),
        "amount": prices * 1_000_000,
        "turnover": np.full(len(dates), 1.0),
    })
    db.save_daily_quotes(df)
    provider = DataProvider(db)
    broker = PaperBroker(db, provider, initial_cash=200_000)
    yield db, provider, broker
    db.close()


def test_buy_persists_position_and_trade(setup):
    db, _, broker = setup
    sig = Signal(stock_code="000001", direction=Direction.BUY, strength=0.5,
                 reason="test")
    assert broker.execute_signal(sig, "test_strategy") is True
    positions = db.list_positions()
    assert len(positions) == 1
    assert positions[0]["code"] == "000001"
    assert positions[0]["quantity"] >= 100
    assert positions[0]["quantity"] % 100 == 0
    state = db.get_portfolio_state()
    assert state is not None
    assert state["cash"] < state["initial_cash"]
    trades = db.list_trades()
    assert len(trades) == 1
    assert trades[0]["direction"] == "buy"
    orders = db.list_orders()
    assert orders[0]["status"] == "filled"


def test_t_plus_one_blocks_same_day_sell(setup):
    db, _, broker = setup
    buy = Signal(stock_code="000001", direction=Direction.BUY, strength=0.4,
                 reason="t1 buy")
    broker.execute_signal(buy, "test")
    sell = Signal(stock_code="000001", direction=Direction.SELL, strength=1.0,
                  reason="same day sell")
    assert broker.execute_signal(sell, "test") is False
    # Position still here
    assert len(db.list_positions()) == 1


def test_risk_rejects_oversize_position(setup):
    db, provider, _ = setup
    tight = RiskConfig(max_position_pct=0.001, max_total_position_pct=0.001)
    broker = PaperBroker(db, provider, initial_cash=200_000, risk_config=tight)
    sig = Signal(stock_code="000001", direction=Direction.BUY,
                 strength=0.9, reason="oversized")
    assert broker.execute_signal(sig, "test") is False
    rejected_orders = [o for o in db.list_orders() if o["status"] == "rejected"]
    assert len(rejected_orders) == 1
    decisions = db.list_decisions(kind="risk_reject")
    assert len(decisions) == 1


def test_snapshot_records_history(setup):
    db, _, broker = setup
    broker.execute_signal(
        Signal(stock_code="000001", direction=Direction.BUY,
               strength=0.4, reason="snap"),
        "test",
    )
    snap = broker.snapshot_portfolio()
    assert snap["total_value"] > 0
    assert any(p["code"] == "000001" for p in snap["positions"])
    snaps = db.get_portfolio_snapshots()
    assert len(snaps) >= 1


def test_paper_broker_satisfies_broker_protocol(setup):
    """PaperBroker must structurally implement the Broker interface the
    runtime depends on — guards against drift when QmtBroker is added."""
    from quanti.execution.base import Broker
    _, _, broker = setup
    assert isinstance(broker, Broker)
    for m in ("execute_signal", "execute_signals", "try_fill_pending_orders",
              "check_exits", "snapshot_portfolio", "pending_orders_detail"):
        assert callable(getattr(broker, m)), f"missing {m}"


def test_industry_cap_limits_same_industry_buy(tmp_path):
    """A buy in a sector already near the 30% industry cap is limited by the
    *industry* room, not the (looser) 10% single-stock cap — proving the
    industry concentration limit is actually enforced at sizing."""
    from quanti.risk.manager import RiskConfig

    db = Database(str(tmp_path / "ind.db"))
    db.initialize()
    db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
    db.upsert_stock("600000", "浦发银行", "SH", date(1999, 11, 10), "银行")
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=10)
    for code in ("000001", "600000"):
        px = np.full(len(dates), 10.0)
        db.save_daily_quotes(pd.DataFrame({
            "code": code, "date": [d.date() for d in dates],
            "open": px, "high": px, "low": px, "close": px,
            "volume": np.full(len(dates), 1e6), "amount": np.full(len(dates), 1e7),
            "turnover": np.ones(len(dates))}))
    provider = DataProvider(db)
    broker = PaperBroker(db, provider, initial_cash=100_000, fill_mode="immediate",
                         risk_config=RiskConfig(max_position_pct=0.10,
                                                max_industry_pct=0.30))
    # Seed 28% already in 银行 (2800 sh @ 10 = 28k; cash 72k → total 100k).
    db.update_cash(72_000.0)
    db.upsert_position("000001", 2800, 10.0, 10.0, date(2020, 1, 1))

    # Buy another 银行 name: industry room = 30% - 28% = 2% (~2k), far under the
    # 10% single-stock cap (~10k) — so the fill is industry-limited.
    broker.execute_signal(Signal("600000", Direction.BUY, 1.0, "x"), "s")
    pos = {p["code"]: p for p in db.list_positions()}
    assert "600000" in pos
    assert pos["600000"]["quantity"] * 10.0 <= 2_000 * 1.05  # industry-capped


def _seed_drawdown_env(tmp_path, name, mark_price):
    db = Database(str(tmp_path / name))
    db.initialize()
    db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=8)
    px = np.full(len(dates), mark_price)
    db.save_daily_quotes(pd.DataFrame({
        "code": "000001", "date": [d.date() for d in dates],
        "open": px, "high": px, "low": px, "close": px,
        "volume": np.full(len(dates), 1e6),
        "amount": np.full(len(dates), mark_price * 1e6),
        "turnover": np.ones(len(dates))}))
    return db


def test_portfolio_stop_flattens_on_drawdown(tmp_path):
    """>15% drawdown from the equity high-water mark trips the circuit breaker:
    positions are flattened and it returns True (runtime then halts)."""
    from quanti.risk.manager import RiskConfig
    db = _seed_drawdown_env(tmp_path, "ps.db", 7.5)  # position marks to 7.5
    provider = DataProvider(db)
    broker = PaperBroker(db, provider, initial_cash=100_000, fill_mode="immediate",
                         risk_config=RiskConfig(portfolio_stop_loss_pct=-0.15))
    db.save_portfolio_snapshot(date.today(), 100_000.0, 0.0, 100_000.0)  # peak
    db.update_cash(20_000.0)
    db.upsert_position("000001", 8000, 10.0, 7.5, date(2020, 1, 1))  # mv 60k
    # total = 20k + 60k = 80k vs peak 100k → -20% → fire.
    assert broker.enforce_portfolio_stop() is True
    assert db.list_positions() == []


def test_portfolio_stop_holds_within_tolerance(tmp_path):
    from quanti.risk.manager import RiskConfig
    db = _seed_drawdown_env(tmp_path, "ps2.db", 9.8)
    provider = DataProvider(db)
    broker = PaperBroker(db, provider, initial_cash=100_000, fill_mode="immediate",
                         risk_config=RiskConfig(portfolio_stop_loss_pct=-0.15))
    db.save_portfolio_snapshot(date.today(), 100_000.0, 0.0, 100_000.0)
    db.update_cash(5_000.0)
    db.upsert_position("000001", 9000, 10.0, 9.8, date(2020, 1, 1))  # mv 88.2k
    # total = 5k + 88.2k = 93.2k vs peak 100k → -6.8% → hold.
    assert broker.enforce_portfolio_stop() is False
    assert len(db.list_positions()) == 1
