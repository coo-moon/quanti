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
