"""Tests for QmtBroker against the in-process mock bridge gateway.

The fake transport calls the real ``bridge.qmt_bridge`` routing in-process, so
this exercises the full QmtBroker -> bridge contract -> mock fill -> reconcile
loop without sockets or a live QMT environment. T+1 (can_use_volume), batch
counters, venue rejection, cancel-of-open, and the pending reconcile path are
all covered.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from bridge.qmt_bridge import QmtGateway, route
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.base import Broker
from quanti.execution.qmt_broker import QmtBroker
from quanti.models import Direction, Signal
from quanti.risk.manager import RiskConfig, RiskManager


class InProcBridge:
    """BridgeClient that dispatches to a real mock gateway in-process."""

    def __init__(self) -> None:
        self.gw = QmtGateway()

    def get(self, path: str, params: dict | None = None) -> dict:
        return route(self.gw, "GET", path, params or {}, None)[1]

    def post(self, path: str, json: dict | None = None) -> dict:
        return route(self.gw, "POST", path, json or {}, json)[1]


class RejectBridge(InProcBridge):
    """Like InProcBridge but the venue rejects every order submit."""

    def post(self, path: str, json: dict | None = None) -> dict:
        if path == "/trader/order":
            return {"ok": False, "status": "rejected", "order_id": "",
                    "filled_volume": 0, "filled_price": 0.0,
                    "msg": "venue reject"}
        return super().post(path, json)


class DeadBridge:
    def get(self, path, params=None):
        raise ConnectionError("bridge down")

    def post(self, path, json=None):
        raise ConnectionError("bridge down")


class RecordingBridge(InProcBridge):
    """InProcBridge that records every /trader/order payload it submits."""

    def __init__(self) -> None:
        super().__init__()
        self.orders: list[dict] = []

    def post(self, path: str, json: dict | None = None) -> dict:
        if path == "/trader/order":
            self.orders.append(json or {})
        return super().post(path, json)


def _make(db, provider, client=None, **kw):
    return QmtBroker(db, provider, client=client or InProcBridge(),
                     initial_cash=1_000_000, **kw)


@pytest.fixture
def env(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=10)
    px = 10 + np.arange(len(dates)) * 0.1
    db.save_daily_quotes(pd.DataFrame({
        "code": "000001", "date": [d.date() for d in dates],
        "open": px - 0.05, "high": px + 0.1, "low": px - 0.1, "close": px,
        "volume": np.full(len(dates), 1e6), "amount": px * 1e6,
        "turnover": np.ones(len(dates)),
    }))
    provider = DataProvider(db)
    yield db, provider
    db.close()


def test_satisfies_broker_protocol(env):
    db, provider = env
    assert isinstance(_make(db, provider), Broker)


def test_is_connected_true(env):
    db, provider = env
    assert _make(db, provider).is_connected() is True


def test_is_connected_false_when_bridge_down(env):
    db, provider = env
    assert QmtBroker(db, provider, client=DeadBridge()).is_connected() is False


def test_require_live_treats_mock_bridge_as_not_connected(env):
    """G1: with require_live, a bridge in mock mode (xtquant absent) must read
    as NOT connected and refuse to submit — else mock 'fills' would be mirrored
    as real trades."""
    db, provider = env
    broker = _make(db, provider, require_live=True)  # in-proc bridge = mock mode
    assert broker.is_connected() is False            # mock ≠ vnpy
    # A submit is rejected before any venue/local state changes.
    assert broker.execute_signal(
        Signal("000001", Direction.BUY, 0.5, "b"), "s") is False
    assert db.list_trades() == []
    assert any(d["kind"] == "broker_not_live"
               for d in db.list_decisions(limit=10))


def test_order_price_clamps_to_tick_and_limit(env):
    """G4: order price is rounded to the A-share tick (0.01) and clamped into
    today's daily price-limit band, so the venue can't reject it."""
    from quanti.utils.market import prev_bar_close
    db, provider = env
    broker = _make(db, provider)
    pc = prev_bar_close(provider, "000001", date.today())
    assert pc and pc > 0
    lim_hi = round(pc * 1.10, 2)              # main board ±10%
    assert broker._order_price("000001", 999.0) == lim_hi   # clamped up
    assert broker._order_price("000001", 10.123) == 10.12   # rounded to tick


def test_buy_submits_and_reconciles_from_broker(env):
    db, provider = env
    broker = _make(db, provider)
    assert broker.execute_signal(Signal("000001", Direction.BUY, 0.5, "buy"),
                                 "s") is True
    snap = broker.snapshot_portfolio()
    held = [p for p in snap["positions"] if p["code"] == "000001"]
    assert held and held[0]["quantity"] >= 100
    assert snap["cash"] < snap["initial_cash"]
    orders = db.list_orders()
    assert orders and orders[0]["entry_strategy"].startswith("mock-")


def test_execute_signals_reports_filled_count(env):
    db, provider = env
    broker = _make(db, provider)
    result = broker.execute_signals([Signal("000001", Direction.BUY, 0.5, "b")],
                                    "s")
    assert result.accepted == 1
    assert result.filled == 1   # regression guard: was always 0 before fix
    assert result.pending == 0
    assert result.rejected == 0


def test_risk_gate_blocks_before_venue(env):
    db, provider = env
    broker = _make(db, provider)
    # A daily-trade cap of 0 makes the RISK GATE (check(), not sizing) reject
    # any fresh BUY before it can reach the venue — exercising the real reject
    # branch. (The 80% total-position cap was removed.)
    broker._risk = RiskManager(RiskConfig(max_daily_trades=0))
    assert broker.execute_signal(Signal("000001", Direction.BUY, 0.5, "b"),
                                 "s") is False
    # Nothing reached the venue.
    assert broker.snapshot_portfolio()["cash"] == pytest.approx(1_000_000.0)
    # And it was recorded as a risk rejection, not a fill.
    assert db.list_orders()[0]["status"] == "rejected"
    assert any(d["kind"] == "risk_reject" for d in db.list_decisions(limit=10))


def test_daily_trade_cap_enforced(env):
    db, provider = env
    broker = _make(db, provider)
    broker._risk = RiskManager(RiskConfig(max_daily_trades=1))
    # First order counts against the cap (record_trade); second is blocked.
    assert broker.execute_signal(Signal("000001", Direction.BUY, 0.3, "b1"),
                                 "s") is True
    assert broker.execute_signal(Signal("600519", Direction.BUY, 0.3, "b2"),
                                 "s") is False


def test_sell_capped_at_t1_sellable(env):
    db, provider = env
    broker = _make(db, provider)
    # Hold 1000 but only 300 sellable today (700 bought today, frozen).
    broker._client.gw._mock_positions["000001"] = {
        "volume": 1000, "can_use": 300, "avg_price": 10.0}
    assert broker.execute_signal(Signal("000001", Direction.SELL, 1.0, "x"),
                                 "s") is True
    pos = {p["code"]: p for p in broker.snapshot_portfolio()["positions"]}
    # Only the 300 sellable left; 700 remain frozen.
    assert pos["000001"]["quantity"] == 700


def test_flatten_skips_t1_frozen_lot(env):
    db, provider = env
    broker = _make(db, provider)
    # Same-day buy → can_use stays 0 → flatten must NOT sell it.
    broker.execute_signal(Signal("000001", Direction.BUY, 0.5, "buy"), "s")
    assert broker.flatten("kill") == 0
    assert any(p["code"] == "000001" and p["quantity"] > 0
               for p in broker.snapshot_portfolio()["positions"])


def test_flatten_exits_settled_holdings(env):
    db, provider = env
    broker = _make(db, provider)
    broker.execute_signal(Signal("000001", Direction.BUY, 0.5, "buy"), "s")
    broker._client.gw.settle()  # overnight T+1 settlement → sellable
    assert broker.flatten("kill") == 1
    assert all(p["quantity"] == 0 for p in broker.snapshot_portfolio()["positions"]
               if p["code"] == "000001")


def test_venue_rejection_recorded(env):
    db, provider = env
    broker = _make(db, provider, client=RejectBridge())
    assert broker.execute_signal(Signal("000001", Direction.BUY, 0.5, "b"),
                                 "s") is False
    assert db.list_orders()[0]["status"] == "rejected"
    assert any(d["kind"] == "order_rejected" for d in db.list_decisions(limit=10))


def test_try_fill_reconciles_pending_to_filled(env):
    db, provider = env
    broker = _make(db, provider)
    # Seed a local PENDING mirror whose venue id maps to a now-filled venue order.
    broker._mirror_order(Signal("000001", Direction.BUY, 1.0, "x"), "s",
                         status="pending", venue_order_id="mock-1", quantity=100)
    broker._client.gw._mock_orders.append({
        "order_id": "mock-1", "code": "000001", "direction": "buy",
        "volume": 100, "status": "filled", "filled_volume": 100,
        "filled_price": 10.5})
    out = broker.try_fill_pending_orders()
    assert out.filled == 1
    assert db.list_orders(status="pending") == []  # flipped off pending


def test_cancel_all_pending_cancels_open_order(env):
    db, provider = env
    broker = _make(db, provider)
    broker._client.gw._mock_autofill = False  # orders rest as 'accepted'
    assert broker.execute_signal(Signal("000001", Direction.BUY, 0.5, "b"),
                                 "s") is True
    assert broker.cancel_all_pending() == 1
    # Venue order is now cancelled.
    orders = broker._client.get("/trader/orders")["orders"]
    assert all(o["status"] == "cancelled" for o in orders)


def test_check_exits_runs_clean(env):
    db, provider = env
    assert _make(db, provider).check_exits() == 0


def test_compute_atr_ratios(env):
    """P1-1: ATR/close ratio is a small positive, dimensionless number."""
    from quanti.execution.exits import compute_atr_ratios
    _db, provider = env
    ratios = compute_atr_ratios(provider, [{"code": "000001"}], n=5)
    assert "000001" in ratios and 0 < ratios["000001"] < 1


def test_qmt_check_exits_fires_atr_stop(env):
    """P1-1 live wiring: with atr_stop_k>0 the QMT exit path injects ATR ratios
    so a calm name past its (tighter) ATR stop is sold even when it hasn't
    breached the flat stop."""
    db, provider = env
    broker = _make(db, provider,
                   risk_config=RiskConfig(stop_loss_pct=-0.50, atr_stop_k=2.0,
                                          atr_stop_n=5))
    # Live ~24.43 vs cost 26 → ~-6%; flat stop -50% won't fire, but the ATR
    # stop (calm 000001, ratio≈1% → ≈-2%) will.
    broker._client.gw._mock_positions["000001"] = {
        "volume": 1000, "can_use": 1000, "avg_price": 26.0}
    assert broker.check_exits() == 1


def test_reconciled_current_price_uses_live_quote_not_cost(env):
    """C5: the reconciled current_price must reflect the live last price, not be
    reverse-derived from a cost-based market_value (which pinned it to avg_cost
    → pnl always 0)."""
    db, provider = env
    broker = _make(db, provider)
    broker._client.gw._mock_positions["000001"] = {
        "volume": 1000, "can_use": 1000, "avg_price": 30.0}
    pf, _ = broker._reconciled_portfolio()
    pos = pf.positions["000001"]
    # Mock live quote for 000001 (~24.43) ≠ cost 30.0 → a real (negative) pnl.
    assert pos.current_price != pytest.approx(pos.avg_cost)
    assert pos.pnl_pct < -0.08


def test_check_exits_fires_stop_loss_on_loss(env):
    """C5 regression: a holding whose live price is past the -8% stop triggers a
    SELL via check_exits and is sold down at the venue. Before the fix the
    stop-loss never fired live because pnl was structurally 0. The masking test
    (no positions → 0) is kept above; this exercises the loss path."""
    db, provider = env
    broker = _make(db, provider)
    # Hold 1000 sh at cost 30.0, fully T+1-settled. Mock live quote ~24.43
    # (~-18.6%) is well past the -8% per-stock stop.
    broker._client.gw._mock_positions["000001"] = {
        "volume": 1000, "can_use": 1000, "avg_price": 30.0}

    landed = broker.check_exits()
    assert landed == 1, "stop-loss exit should have fired and landed at venue"
    remaining = {p["code"]: p for p in
                 broker._client.get("/trader/positions")["positions"]}
    assert remaining.get("000001", {}).get("volume", 0) == 0  # sold out


def test_qmt_protection_blocks_buy(tmp_path):
    from datetime import datetime, timedelta

    from quanti.risk.protections import ProtectionConfig

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    provider = DataProvider(db)
    today = date.today()
    iso = lambda d: datetime(d.year, d.month, d.day, 15, 0).isoformat()  # noqa: E731
    for i, c in enumerate(["000001", "000002", "000003"]):
        d = today - timedelta(days=i + 1)
        db.insert_order({
            "order_id": f"o{i}", "code": c, "direction": "sell",
            "quantity": 100, "price_type": "market", "limit_price": 0.0,
            "status": "filled", "strategy_name": "risk_exit",
            "filled_price": 9.0, "filled_quantity": 100,
            "reason": "止损 -10% ≤ -8%", "created_at": iso(d),
            "filled_at": iso(d), "entry_strategy": "",
        })
    broker = QmtBroker(db, provider, client=InProcBridge(),
                       protection_config=ProtectionConfig(
                           sg_lookback_days=10, sg_trade_limit=3,
                           sg_lock_days=10, max_drawdown_enabled=False))
    ok, reason, kind = broker._entry_allowed(
        Signal("600519", Direction.BUY, 1.0, "buy"),
        broker._reconciled_portfolio()[0])
    assert ok is False and kind == "protection_block"


# --- P0-2: forced exits submit at the limit-down price (must get out) -------

def _limit_down(provider, code: str) -> float:
    from quanti.utils.market import board_limit_pct, prev_bar_close
    pc = prev_bar_close(provider, code, date.today())
    return round(pc * (1 - board_limit_pct(code)), 2)


def test_forced_stop_loss_submits_at_limit_down(env):
    """A stop-loss exit (strategy 'risk_exit') must submit at today's 跌停价 so
    it crosses any bid and earns time priority, not rest as an unfillable limit
    near last price."""
    db, provider = env
    client = RecordingBridge()
    broker = _make(db, provider, client=client)
    # cost 30, live ~24.43 → ~-18.6%, past the -8% stop; fully T+1-settled.
    broker._client.gw._mock_positions["000001"] = {
        "volume": 1000, "can_use": 1000, "avg_price": 30.0}
    assert broker.check_exits() == 1
    sells = [o for o in client.orders if o["direction"] == "sell"]
    assert sells and sells[-1]["price"] == _limit_down(provider, "000001")


def test_kill_switch_flatten_submits_at_limit_down(env):
    """The circuit-breaker / kill-switch flatten ('kill_switch') is also a forced
    exit and must price at 跌停价."""
    db, provider = env
    client = RecordingBridge()
    broker = _make(db, provider, client=client)
    broker._client.gw._mock_positions["000001"] = {
        "volume": 1000, "can_use": 1000, "avg_price": 10.0}
    assert broker.flatten("组合回撤熔断") == 1
    sells = [o for o in client.orders if o["direction"] == "sell"]
    assert sells and sells[-1]["price"] == _limit_down(provider, "000001")


def test_normal_sell_prices_near_last_not_limit_down(env):
    """A non-forced SELL (e.g. a strategy rebalance) keeps the near-last limit —
    it must NOT be floored to 跌停价."""
    db, provider = env
    client = RecordingBridge()
    broker = _make(db, provider, client=client)
    broker._client.gw._mock_positions["000001"] = {
        "volume": 1000, "can_use": 1000, "avg_price": 10.0}
    assert broker.execute_signal(
        Signal("000001", Direction.SELL, 1.0, "rebalance"), "ma_cross") is True
    sells = [o for o in client.orders if o["direction"] == "sell"]
    assert sells and sells[-1]["price"] > _limit_down(provider, "000001")


# --- P0-1: live QMT now runs all three exits, incl. trailing take-profit ----

def test_check_exits_fires_trailing_tp_live(env, monkeypatch):
    """Before P0-1 the live QMT check_exits passed peaks={} so the trailing
    take-profit never fired. Now peaks come from the DB mirror; a settled winner
    that has retraced ≥10% from its post-entry high exits."""
    db, provider = env
    broker = _make(db, provider)
    # cost 20, live ~24.43 → +22% (TP armed, NOT a stop-loss).
    broker._client.gw._mock_positions["000001"] = {
        "volume": 1000, "can_use": 1000, "avg_price": 20.0}
    # DB mirror supplies buy_date + a post-entry peak (35) well above live
    # (24.43) → ~-30% retrace, past the 10% trail.
    monkeypatch.setattr(broker._db, "list_positions", lambda: [
        {"code": "000001", "buy_date": date(2000, 1, 1), "entry_strategy": ""}])
    monkeypatch.setattr(broker._db, "get_high_water", lambda code, since: 35.0)
    assert broker.check_exits() == 1  # trailing TP fired and landed
