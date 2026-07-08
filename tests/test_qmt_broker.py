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
    # Default to in-session so submit-path tests are wall-clock-free; the
    # overnight-queue tests override session_fn=lambda: False.
    kw.setdefault("session_fn", lambda: True)
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


def test_live_latest_price_never_falls_back_to_tushare(env):
    """require_live: with no realtime (xtdata-via-bridge) quote, price is 0.0
    (→ market order downstream), NEVER the stale tushare daily close. Paper/dev
    may use the daily close as a stand-in."""
    db, provider = env

    class _NoQuote(InProcBridge):
        def get(self, path, params=None):
            if path == "/data/quote":
                return {"last": 0.0}            # realtime quote unavailable
            return super().get(path, params)

    assert QmtBroker(db, provider, client=_NoQuote(),
                     require_live=True)._latest_price("000001") == 0.0
    assert QmtBroker(db, provider, client=_NoQuote()
                     )._latest_price("000001") > 0   # paper: tushare close OK


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
    assert held[0]["industry"] == "银行"   # industry surfaced for the UI
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
    pf, _, _ = broker._reconciled_portfolio()
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


def test_peaks_raw_axis_divides_by_latest_factor(env):
    """Live venue prices (last_price/avg_price) are raw, so QMT's peaks must
    be re-expressed on today's raw axis: hfq peak ÷ latest adj_factor. The
    bare hfq peak would make an ex-dividend gap read as a retrace."""
    from quanti.execution.exits import compute_peaks
    db, _ = env
    today = pd.Timestamp.today().normalize()
    db.save_daily_quotes(pd.DataFrame([
        # pre-div bar: raw high 13.0, factor 1.0 → hfq high 13.0
        {"code": "000002", "date": (today - pd.Timedelta(days=1)).date(),
         "open": 12.8, "high": 13.0, "low": 12.5, "close": 12.6,
         "volume": 1e6, "amount": 1.26e7, "turnover": 1.0, "adj_factor": 1.0},
        # ex-div bar: raw ~9, factor 1.4 → hfq ~12.7 (no new high)
        {"code": "000002", "date": today.date(),
         "open": 9.0, "high": 9.1, "low": 8.9, "close": 9.0,
         "volume": 1e6, "amount": 9e6, "turnover": 1.0, "adj_factor": 1.4},
    ]))
    peaks = compute_peaks(db, [{"code": "000002", "buy_date": date(2000, 1, 1)}],
                          raw_axis=True)
    assert peaks["000002"] == pytest.approx(13.0 / 1.4)


# --- Circuit-breaker HWM: intraday marks must not fossilize; peak survives ---
# a restart via the monotone portfolio_hwm row (not the snapshot table).

class AssetBridge(InProcBridge):
    """InProcBridge with a controllable flat /trader/asset (no positions)."""

    def __init__(self, total: float) -> None:
        super().__init__()
        self.total = total

    def get(self, path: str, params: dict | None = None) -> dict:
        if path == "/trader/asset":
            return {"cash": self.total, "market_value": 0.0,
                    "total_asset": self.total}
        if path == "/trader/positions":
            return {"positions": []}
        return super().get(path, params)


def test_intraday_snapshot_not_persisted(env, monkeypatch):
    db, provider = env
    monkeypatch.setattr("quanti.execution.qmt_broker.session_closed_for_day",
                        lambda *a, **k: False)
    broker = _make(db, provider, client=AssetBridge(1_000_000))
    snap = broker.snapshot_portfolio()
    assert snap["total_value"] == pytest.approx(1_000_000)
    assert db.get_portfolio_snapshots() == []


def test_postclose_snapshot_persisted(env, monkeypatch):
    db, provider = env
    monkeypatch.setattr("quanti.execution.qmt_broker.session_closed_for_day",
                        lambda *a, **k: True)
    broker = _make(db, provider, client=AssetBridge(1_000_000))
    broker.snapshot_portfolio()
    rows = db.get_portfolio_snapshots()
    assert len(rows) == 1
    assert rows[0]["total_value"] == pytest.approx(1_000_000)


def test_portfolio_stop_hwm_survives_restart(env, tmp_path, monkeypatch):
    """盘中冲高 → 进程死掉(该日无收盘覆写)→ 重启后熔断仍以真实盘中峰为
    高水位触发,且幻影峰值没有写进 equity curve。"""
    db, provider = env
    monkeypatch.setattr("quanti.execution.qmt_broker.session_closed_for_day",
                        lambda *a, **k: False)
    cfg = RiskConfig(portfolio_stop_loss_pct=-0.10)
    bridge = AssetBridge(1_100_000)
    broker = _make(db, provider, client=bridge, risk_config=cfg)
    assert broker.enforce_portfolio_stop() is False  # at the peak: no trip
    assert db.get_portfolio_snapshots() == []        # peak not in equity curve
    assert db.get_peak_total_value() == pytest.approx(1_100_000)

    # "restart": a fresh Database over the same file
    db2 = Database(str(tmp_path / "t.db"))
    db2.initialize()
    try:
        assert db2.get_peak_total_value() == pytest.approx(1_100_000)
        bridge.total = 950_000  # -13.6% from the intraday peak
        broker2 = _make(db2, DataProvider(db2), client=bridge, risk_config=cfg)
        assert broker2.enforce_portfolio_stop() is True
    finally:
        db2.close()


def test_raise_hwm_is_monotone_and_reset_clears_it(env):
    db, _provider = env
    db.raise_hwm(2_000_000)
    db.raise_hwm(1_500_000)  # lower: no-op
    assert db.get_peak_total_value() == pytest.approx(2_000_000)
    db.reset_portfolio(1_000_000)
    assert db.get_peak_total_value() == 0.0


# --- H1: live order-price band is on the RAW axis (dividend/split safe) ------

def test_order_price_band_uses_raw_axis_on_dividend_stock(tmp_path):
    """Regression (H1): _order_price clamps a raw live order into the RAW
    price-limit band, not the hfq (back-adjusted) one. On a dividend/split stock
    (adj_factor>1) the hfq prev-close is inflated, so an hfq band would clamp
    every raw order outside the ±10% cage → the venue 废单s ALL orders, incl. the
    forced stop-loss/flatten floor (the stop can't get out on real money)."""
    db = Database(str(tmp_path / "d.db"))
    db.initialize()
    db.upsert_stock("600000", "浦发银行", "SH", date(1999, 11, 10), "银行")
    today = pd.Timestamp.today().normalize()
    # Prior bar: RAW close 10.0, adj_factor 1.25 → hfq close 12.5.
    db.save_daily_quotes(pd.DataFrame([
        {"code": "600000", "date": (today - pd.Timedelta(days=1)).date(),
         "open": 9.9, "high": 10.1, "low": 9.8, "close": 10.0,
         "volume": 1e6, "amount": 1e7, "turnover": 1.0, "adj_factor": 1.25},
    ]))
    provider = DataProvider(db)
    broker = _make(db, provider)
    try:
        # RAW band = [9.0, 11.0]. A raw limit-up order (11.0) stays 11.0.
        # (The old hfq band [11.25, 13.75] would wrongly clamp it UP to 11.25 →
        # above the venue's raw ±10% cage → 废单.)
        assert broker._order_price("600000", 11.0) == pytest.approx(11.0)
        # Forced-exit floor lands on the RAW 跌停 (9.0), where a sell can fill —
        # not hfq 11.25 (above market, unfillable on a fast drop).
        assert broker._order_price("600000", 0.01) == pytest.approx(9.0)
    finally:
        db.close()


# --- H2: live stale-quote must not silently disable the stop-loss -----------

class StaleFeedBridge:
    """A 'live' (vnpy-mode, connected, datafeed_ok) bridge whose held position
    has NO realtime price when `last_price=0` — a per-stock 停牌 / dead feed while
    the gateway itself is alive. Records submitted orders."""

    def __init__(self, last_price: float = 0.0) -> None:
        self.last_price = last_price
        self.orders: list[dict] = []

    def get(self, path: str, params: dict | None = None) -> dict:
        if path == "/health":
            return {"ok": True, "mode": "vnpy", "trader_connected": True,
                    "datafeed_ok": True}
        if path == "/trader/asset":
            return {"cash": 100_000.0, "market_value": 0.0,
                    "total_asset": 100_000.0}
        if path == "/trader/positions":
            mv = 1000 * self.last_price  # 0 when the feed is down
            return {"positions": [{"code": "000001", "volume": 1000,
                                   "can_use_volume": 1000, "avg_price": 30.0,
                                   "last_price": self.last_price,
                                   "market_value": mv}]}
        if path == "/trader/orders":
            return {"orders": []}
        if path == "/data/quote":
            return {"last": self.last_price}
        return {}

    def post(self, path: str, json: dict | None = None) -> dict:
        if path == "/trader/order":
            self.orders.append(json or {})
            return {"ok": True, "status": "filled", "order_id": "x1",
                    "filled_price": (json or {}).get("price", 0),
                    "filled_volume": (json or {}).get("volume", 0)}
        return {"ok": True}


def test_check_exits_skips_and_alerts_on_stale_quote(env):
    """H2: in live, a held position with no realtime price must NOT be exit-
    evaluated on a fabricated (cost-basis) price — that pinned pnl≡0 and silently
    disabled its stop-loss. check_exits skips it and logs a `stale_quote` alert
    instead of firing a bogus (or missing) stop."""
    db, provider = env
    bridge = StaleFeedBridge(last_price=0.0)          # feed down for 000001
    broker = QmtBroker(db, provider, client=bridge, require_live=True,
                       session_fn=lambda: True)
    assert broker.check_exits() == 0                  # no fabricated stop fired
    assert bridge.orders == []                        # nothing submitted
    assert any(d["kind"] == "stale_quote"
               for d in db.list_decisions(limit=10))  # human alerted


def test_check_exits_fires_stop_when_quote_is_fresh(env):
    """Contrast: with a REAL realtime price past the stop, the same live path
    DOES fire — proving the stale skip is specific to a missing quote, not a
    blanket 'never stop in live'."""
    db, provider = env
    bridge = StaleFeedBridge(last_price=24.0)         # real price, -20% vs cost 30
    broker = QmtBroker(db, provider, client=bridge, require_live=True,
                       session_fn=lambda: True)
    assert broker.check_exits() == 1
    assert any(o["direction"] == "sell" for o in bridge.orders)
    assert not any(d["kind"] == "stale_quote"
                   for d in db.list_decisions(limit=10))


# --- overnight queue → submit at open (A 股 no night session) --------------

def test_offhours_order_queues_then_submits_at_open(env):
    """The 16:00 daily cycle runs off-hours: an order must QUEUE locally (no
    venue call — off-hours submits are guaranteed 废单) and only hit the venue
    when the guard advances it in-session, matching paper/backtest next-open."""
    from quanti.utils.market import prev_bar_close
    db, provider = env
    pc = prev_bar_close(provider, "000001", date.today())

    class FlatBridge(InProcBridge):
        # Realtime quote ≈ prior close (no gap) so the gap guard stays quiet;
        # the plain mock quote is a fixed synthetic ~24 that would false-trip it.
        def get(self, path, params=None):
            if path == "/data/quote":
                return {"last": pc, "open": pc}
            return super().get(path, params)

    sess = {"open": False}
    broker = _make(db, provider, client=FlatBridge(),
                   session_fn=lambda: sess["open"])
    landed = broker.execute_signal(Signal("000001", Direction.BUY, 0.5, "b"), "s")
    assert landed is True
    pend = db.list_orders(status="pending")
    assert len(pend) == 1
    assert (pend[0]["entry_strategy"] or "") == ""   # no venue id yet
    assert db.list_trades() == []                    # nothing hit the venue
    assert any(d["kind"] == "order_queued" for d in db.list_decisions(limit=10))

    sess["open"] = True                              # market opens
    out = broker.try_fill_pending_orders()
    assert out.filled == 1
    filled = [o for o in db.list_orders() if o["status"] == "filled"]
    assert len(filled) == 1
    assert filled[0]["entry_strategy"].startswith("mock-")  # venue id now set
    assert filled[0]["quantity"] >= 100
    held = [p for p in broker.snapshot_portfolio()["positions"]
            if p["code"] == "000001"]
    assert held and held[0]["quantity"] >= 100


def test_offhours_queue_deduped(env):
    """A repeated off-hours BUY for the same code doesn't stack a second
    queued order."""
    db, provider = env
    broker = _make(db, provider, session_fn=lambda: False)
    assert broker.execute_signal(Signal("000001", Direction.BUY, 0.5, "b"), "s")
    assert broker.execute_signal(
        Signal("000001", Direction.BUY, 0.5, "b"), "s") is False
    assert len(db.list_orders(status="pending")) == 1


def test_extreme_gap_up_abandoned_at_open(env):
    """A queued BUY whose open has gapped up past the guard threshold is
    ABANDONED at submit time — never sent to the venue."""
    from quanti.utils.market import prev_bar_close
    db, provider = env
    pc = prev_bar_close(provider, "000001", date.today())
    assert pc and pc > 0

    class GapBridge(InProcBridge):
        def get(self, path, params=None):
            if path == "/data/quote":
                return {"last": round(pc * 1.12, 2), "open": round(pc * 1.12, 2)}
            return super().get(path, params)

    sess = {"open": False}
    broker = _make(db, provider, client=GapBridge(),
                   session_fn=lambda: sess["open"])
    assert broker.execute_signal(Signal("000001", Direction.BUY, 0.5, "b"), "s")
    sess["open"] = True
    out = broker.try_fill_pending_orders()
    assert out.filled == 0
    assert db.list_trades() == []                    # never reached the venue
    o = db.list_orders()[0]
    assert o["status"] == "cancelled"
    assert "gap-up" in o["reason"].lower()
    assert any(d["kind"] == "order_gap_abandoned"
               for d in db.list_decisions(limit=10))


# --- is_connected accepts either live backend, rejects mock -----------------

class _ModeBridge:
    """Minimal bridge client whose /health reports a chosen backend mode."""

    def __init__(self, mode: str) -> None:
        self._mode = mode

    def get(self, path: str, params: dict | None = None) -> dict:
        if path == "/health":
            return {"ok": True, "mode": self._mode,
                    "trader_connected": True, "datafeed_ok": True}
        return {}

    def post(self, path: str, json: dict | None = None) -> dict:
        return {"ok": True}


def test_is_connected_accepts_xt_and_vnpy_rejects_mock(env):
    """require_live gate must accept both real backends — 'xt' (direct xtquant)
    and 'vnpy' (vnpy_xt) — while a silent 'mock' fallback still reads as down."""
    db, provider = env

    def broker(mode):
        return QmtBroker(db, provider, client=_ModeBridge(mode),
                         require_live=True, session_fn=lambda: True)

    assert broker("xt").is_connected() is True
    assert broker("vnpy").is_connected() is True
    assert broker("mock").is_connected() is False


# --- observation-period per-order notional cap (BUY only) -------------------

def test_max_order_notional_rejects_oversized_buy(env):
    """A BUY whose notional exceeds the cap is rejected before the venue, with
    an audit decision — the observation-period blast-radius guard."""
    db, provider = env
    rec = RecordingBridge()
    broker = _make(db, provider, client=rec, max_order_notional=1000.0)
    landed = broker.execute_signal(Signal("000001", Direction.BUY, 0.5, "b"), "s")
    assert landed is False
    assert rec.orders == []                        # never reached the venue
    o = db.list_orders()[0]
    assert o["status"] == "rejected" and "名义额" in o["reason"]
    assert any(d["kind"] == "order_notional_capped"
               for d in db.list_decisions(limit=10))


def test_max_order_notional_allows_within_cap(env):
    """A generous cap lets the BUY through to the venue (control)."""
    db, provider = env
    rec = RecordingBridge()
    broker = _make(db, provider, client=rec, max_order_notional=1e9)
    landed = broker.execute_signal(Signal("000001", Direction.BUY, 0.5, "b"), "s")
    assert landed is True
    assert len(rec.orders) == 1                    # reached the venue


def test_no_cap_by_default(env):
    """Unset (0) cap = disabled: a normal BUY reaches the venue unimpeded."""
    db, provider = env
    rec = RecordingBridge()
    broker = _make(db, provider, client=rec)       # no max_order_notional
    assert broker.execute_signal(Signal("000001", Direction.BUY, 0.5, "b"), "s") is True
    assert len(rec.orders) == 1


def test_max_live_exposure_rejects_buy(env):
    """Total-exposure cap: a BUY that would push held market value past the cap
    is rejected before the venue, with an order_exposure_capped alert."""
    db, provider = env
    rec = RecordingBridge()
    broker = _make(db, provider, client=rec, max_live_exposure=1000.0)
    assert broker.execute_signal(Signal("000001", Direction.BUY, 0.5, "b"), "s") is False
    assert rec.orders == []
    assert any(d["kind"] == "order_exposure_capped"
               for d in db.list_decisions(limit=10))


def test_max_live_exposure_allows_within_cap(env):
    """A generous total-exposure cap lets the BUY through (control)."""
    db, provider = env
    rec = RecordingBridge()
    broker = _make(db, provider, client=rec, max_live_exposure=1e9)
    assert broker.execute_signal(Signal("000001", Direction.BUY, 0.5, "b"), "s") is True
    assert len(rec.orders) == 1


# --- G2: seed daily open-count from the venue at live startup ----------------

def test_seeds_daily_open_count_from_venue(env):
    """A live QmtBroker seeds today's open-count from /trader/trades on startup
    so a mid-day restart doesn't reset the daily cap to 0 (audit G2). Only
    today's BUYs count; SELLs and prior days are excluded."""
    db, provider = env
    today = date.today().isoformat()

    class SeedBridge(InProcBridge):
        def get(self, path: str, params: dict | None = None) -> dict:
            if path == "/trader/trades":
                return {"trades": [
                    {"direction": "buy", "time": today + "T10:00:00"},
                    {"direction": "buy", "time": today + "T10:01:00"},
                    {"direction": "sell", "time": today + "T10:02:00"},   # exit
                    {"direction": "buy", "time": "2020-01-01T10:00:00"},   # old
                ]}
            return super().get(path, params)

    broker = QmtBroker(db, provider, client=SeedBridge(),
                       require_live=True, session_fn=lambda: True)
    assert broker._risk._daily_trade_count == 2      # 2 today-buys seeded


def test_no_daily_seed_when_not_live(env):
    """Non-live (dev/paper/tests) doesn't call the venue on construct."""
    db, provider = env
    broker = QmtBroker(db, provider, client=InProcBridge(), session_fn=lambda: True)
    assert broker._risk._daily_trade_count == 0


# --- order idempotency: no duplicate real orders on retry / crash -----------

def test_queued_order_resolved_not_resubmitted(env):
    """A queued overnight order, once submitted, must resolve its OWN row (stamp
    the venue id) so a later reconcile tick doesn't re-drive → re-submit it.
    Pre-fix the queued row stayed 'pending' with no venue id and _advance_queued
    re-POSTed it every tick (duplicate real order)."""
    db, provider = env
    rec = RecordingBridge()
    sess = {"open": False}
    broker = _make(db, provider, client=rec, session_fn=lambda: sess["open"],
                   risk_config=RiskConfig(extreme_gap_up_block_pct=0.0))
    broker.execute_signal(Signal("000001", Direction.BUY, 0.5, "b"), "s")  # queues
    assert rec.orders == [] and len(db.list_orders(status="pending")) == 1
    sess["open"] = True
    broker.try_fill_pending_orders()          # submits once
    broker.try_fill_pending_orders()          # 2nd tick must NOT re-submit
    assert len(rec.orders) == 1, len(rec.orders)
    # No un-submitted queued row lingers (row was resolved, not orphaned pending).
    assert all(o.get("entry_strategy")
               for o in db.list_orders(status="pending"))


def test_submit_unknown_result_no_crash_no_orphan(env):
    """An exception on the venue POST is UNKNOWN, not failed: no crash, a mirror
    row already exists (mirror-before-POST → no orphan), and an
    order_submit_unknown alert is logged. Not blindly re-sent in this call."""
    db, provider = env

    class FlakyBridge(InProcBridge):
        def post(self, path: str, json: dict | None = None) -> dict:
            if path == "/trader/order":
                raise ConnectionError("timeout mid-submit")
            return super().post(path, json)

    broker = _make(db, provider, client=FlakyBridge())     # in-session
    broker.execute_signal(Signal("000001", Direction.BUY, 0.5, "b"), "s")
    rows = db.list_orders()
    assert len(rows) == 1                                   # mirror-before-POST
    assert rows[0]["status"] == "submitting"               # attempted, unconfirmed
    assert any(d["kind"] == "order_submit_unknown"
               for d in db.list_decisions(limit=10))
    # A reconcile tick must NOT ghost-cancel it: 'submitting' is outside the
    # overnight-queue driver (which only cancels never-attempted 'pending' rows),
    # so an order that may be live at the venue isn't silently abandoned locally.
    broker.try_fill_pending_orders()
    assert db.list_orders()[0]["status"] == "submitting"   # untouched


# --- startup/tick reconcile of 'submitting' (unknown-result) rows -----------

def test_reconcile_resolves_submitting_row_from_venue(env):
    """A 'submitting' row (POST result was unknown) is resolved on the next tick
    by matching the venue order carrying its client_order_id: the outcome +
    venue id are stamped so a landed order regains its full local mirror."""
    db, provider = env
    db.insert_order({"order_id": "q_coid1", "code": "000001", "direction": "buy",
                     "quantity": 100, "price_type": "limit", "limit_price": 10.0,
                     "status": "submitting", "strategy_name": "s",
                     "reason": "unknown", "entry_strategy": ""})

    class ReconcileBridge(InProcBridge):
        def get(self, path: str, params: dict | None = None) -> dict:
            if path == "/trader/orders":
                return {"orders": [{
                    "order_id": "v-99", "code": "000001", "direction": "buy",
                    "volume": 100, "price": 10.0, "status": "filled",
                    "filled_volume": 100, "filled_price": 10.0,
                    "created_at": "", "client_order_id": "q_coid1"}]}
            return super().get(path, params)

    _make(db, provider, client=ReconcileBridge()).try_fill_pending_orders()
    row = next(o for o in db.list_orders() if o["order_id"] == "q_coid1")
    assert row["status"] == "filled"
    assert row["entry_strategy"] == "v-99"      # venue id stamped


def test_reconcile_leaves_unmatched_submitting(env):
    """A 'submitting' row with NO matching venue order is LEFT submitting — never
    cancelled on a possibly-incomplete venue list (conservative / safe)."""
    db, provider = env
    db.insert_order({"order_id": "q_x", "code": "000001", "direction": "buy",
                     "quantity": 100, "price_type": "limit", "status": "submitting",
                     "entry_strategy": ""})
    _make(db, provider, client=InProcBridge()).try_fill_pending_orders()  # venue empty
    row = next(o for o in db.list_orders() if o["order_id"] == "q_x")
    assert row["status"] == "submitting"        # untouched


# --- live-order arm/disarm switch (UI-toggled, DB-backed) -------------------

class _LiveRecordingBridge(RecordingBridge):
    """Reports a live (xt) /health so require_live's is_connected() passes, but
    otherwise delegates to the in-proc mock gateway (records /trader/order)."""

    def get(self, path: str, params: dict | None = None) -> dict:
        if path == "/health":
            return {"ok": True, "mode": "xt", "trader_connected": True,
                    "datafeed_ok": True, "orders_allowed": True}
        return super().get(path, params)


def test_disarmed_rejects_live_buy(env):
    """Live + DISARMED (default) → BUY rejected as observation; nothing reaches
    the venue; an order_disarmed alert is logged."""
    db, provider = env
    rec = _LiveRecordingBridge()
    broker = _make(db, provider, client=rec, require_live=True)
    assert db.get_live_orders_armed() is False              # default disarmed
    landed = broker.execute_signal(Signal("000001", Direction.BUY, 0.5, "b"), "s")
    assert landed is False
    assert rec.orders == []                                  # never submitted
    assert any(d["kind"] == "order_disarmed" for d in db.list_decisions(limit=10))


def test_armed_lets_live_buy_through(env):
    """Once armed, the live BUY reaches the venue."""
    db, provider = env
    rec = _LiveRecordingBridge()
    broker = _make(db, provider, client=rec, require_live=True)
    db.set_live_orders_armed(True)
    landed = broker.execute_signal(Signal("000001", Direction.BUY, 0.5, "b"), "s")
    assert landed is True
    assert len(rec.orders) == 1                              # reached the venue


def test_disarm_never_blocks_sell(env):
    """The arm gate is BUY-only — a SELL is never rejected for being disarmed
    (an exit/stop-loss must always get out)."""
    db, provider = env
    rec = _LiveRecordingBridge()
    broker = _make(db, provider, client=rec, require_live=True)  # disarmed
    # No position → SELL rejected as 'no position', NOT 'order_disarmed'.
    landed = broker.execute_signal(Signal("000001", Direction.SELL, 1.0, "x"), "s")
    assert landed is False
    assert not any(d["kind"] == "order_disarmed" for d in db.list_decisions(limit=10))
