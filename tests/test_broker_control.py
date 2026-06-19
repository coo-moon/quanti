"""Tests for the Broker control surface: health + kill switch.

Covers the methods added so a live QmtBroker has the same contract:
is_connected / cancel_order / cancel_all_pending / flatten.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.base import Broker
from quanti.execution.paper_broker import PaperBroker
from quanti.models import Direction, Signal


def _seed(db: Database) -> None:
    db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
    db.upsert_stock("600519", "贵州茅台", "SH", date(2001, 8, 27), "白酒")
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=20)
    np.random.seed(7)
    for code, base in (("000001", 12.0), ("600519", 1600.0)):
        prices = base + np.cumsum(np.random.randn(len(dates)) * 0.05)
        db.save_daily_quotes(pd.DataFrame({
            "code": code,
            "date": [d.date() for d in dates],
            "open": prices - 0.05, "high": prices + 0.1,
            "low": prices - 0.1, "close": prices,
            "volume": np.full(len(dates), 1_000_000.0),
            "amount": prices * 1_000_000,
            "turnover": np.full(len(dates), 1.0),
        }))


@pytest.fixture
def env(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    _seed(db)
    provider = DataProvider(db)
    yield db, provider
    db.close()


def test_paperbroker_satisfies_broker_protocol(env):
    db, provider = env
    broker = PaperBroker(db, provider, initial_cash=500_000)
    # runtime_checkable Protocol — confirms all methods (incl. the new ones)
    # are present, which is the contract a live QmtBroker must also meet.
    assert isinstance(broker, Broker)


def test_is_connected_true_for_paper(env):
    db, provider = env
    broker = PaperBroker(db, provider, initial_cash=500_000)
    assert broker.is_connected() is True


def test_cancel_order_flips_pending_to_cancelled(env):
    db, provider = env
    broker = PaperBroker(db, provider, initial_cash=500_000, fill_mode="pending")
    assert broker.execute_signal(
        Signal("000001", Direction.BUY, 0.5, "buy"), "s") is True
    pending = db.list_orders(status="pending")
    assert len(pending) == 1
    order_id = pending[0]["order_id"]

    assert broker.cancel_order(order_id) is True
    assert db.list_orders(status="pending") == []
    # Cancelling something not pending is a no-op.
    assert broker.cancel_order(order_id) is False
    assert broker.cancel_order("does-not-exist") is False


def test_cancel_all_pending_is_idempotent(env):
    db, provider = env
    broker = PaperBroker(db, provider, initial_cash=500_000, fill_mode="pending")
    broker.execute_signal(Signal("000001", Direction.BUY, 0.3, "b1"), "s")
    broker.execute_signal(Signal("600519", Direction.BUY, 0.3, "b2"), "s")
    assert len(db.list_orders(status="pending")) == 2

    assert broker.cancel_all_pending() == 2
    assert db.list_orders(status="pending") == []
    # Second call finds nothing → 0.
    assert broker.cancel_all_pending() == 0


def test_flatten_exits_all_holdings(env):
    db, provider = env
    broker = PaperBroker(db, provider, initial_cash=500_000, fill_mode="immediate")
    # Seed a holding bought a week ago so T+1 allows selling today.
    old_buy = (pd.Timestamp.today().normalize()
               - pd.tseries.offsets.BDay(7)).date()
    db.upsert_position("000001", 1000, 11.0, 12.0, old_buy)
    assert len(db.list_positions()) == 1

    acted = broker.flatten("test-kill")
    assert acted == 1
    assert db.list_positions() == []
    assert any(t["direction"] == "sell" for t in db.list_trades())


def test_flatten_noop_when_flat(env):
    db, provider = env
    broker = PaperBroker(db, provider, initial_cash=500_000)
    assert broker.flatten() == 0
