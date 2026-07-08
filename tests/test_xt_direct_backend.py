# -*- coding: utf-8 -*-
"""Unit tests for the direct-xtquant bridge backend (bridge/xt_direct_backend.py).

xtquant only imports on the QMT box, so we monkeypatch the module's venue globals
(`xtconstant`, `_xtdata`) and inject a fake xttrader client — the mapping/gating
logic is then testable anywhere, mirroring how test_qmt_bridge fakes the vnpy
backend.
"""

import types

import pytest

import bridge.xt_direct_backend as xd
from bridge.qmt_bridge import QmtGateway, route


class FakeConst:
    STOCK_BUY = 23
    STOCK_SELL = 24
    FIX_PRICE = 11
    ORDER_SUCCEEDED = 56
    ORDER_PART_SUCC = 55
    ORDER_CANCELED = 54
    ORDER_PART_CANCEL = 53
    ORDER_PARTSUCC_CANCEL = 52
    ORDER_JUNK = 57
    ORDER_UNREPORTED = 48
    ORDER_WAIT_REPORTING = 49
    ORDER_REPORTED = 50
    ORDER_REPORTED_CANCEL = 51


class FakeXtdata:
    def __init__(self, last=11.0):
        self.last = last

    def get_full_tick(self, codes):
        return {c: {"lastPrice": self.last} for c in codes}


class FakeTrader:
    def __init__(self, asset=None, positions=None, orders=None, trades=None,
                 order_ret=42, cancel_ret=0):
        self._asset = asset
        self._positions = positions or []
        self._orders = orders or []
        self._trades = trades or []
        self._order_ret = order_ret
        self._cancel_ret = cancel_ret
        self.submitted = []
        self.cancelled = []

    def query_stock_asset(self, acc):
        return self._asset

    def query_stock_positions(self, acc):
        return self._positions

    def query_stock_orders(self, acc):
        return self._orders

    def query_stock_trades(self, acc):
        return self._trades

    def order_stock(self, acc, code, otype, vol, ptype, price, sname, remark):
        self.submitted.append(dict(code=code, otype=otype, vol=vol, ptype=ptype,
                                    price=price, sname=sname, remark=remark))
        return self._order_ret

    def cancel_order_stock(self, acc, oid):
        self.cancelled.append(oid)
        return self._cancel_ret


def _mk(monkeypatch, trader, allow_orders=False):
    monkeypatch.setattr(xd, "xtconstant", FakeConst)
    monkeypatch.setattr(xd, "_xtdata", FakeXtdata())
    monkeypatch.setattr(xd, "_XTDATA_OK", True)
    b = xd.XtDirectBackend("85530137", "path")
    b._trader = trader
    b._acc = object()
    b.connected = True          # so _ensure() short-circuits (no real connect)
    b._allow_orders = allow_orders
    return b


# --- order submission gating (the safety guard) -----------------------------

def test_submit_disabled_by_default(monkeypatch):
    t = FakeTrader()
    b = _mk(monkeypatch, t, allow_orders=False)
    res = b.submit_order({"code": "000001", "direction": "buy",
                          "volume": 100, "price": 10.0})
    assert res["ok"] is False and res["status"] == "rejected"
    assert "disabled" in res["msg"]
    assert t.submitted == []          # nothing reached the venue


def test_submit_enabled_sends_limit_order(monkeypatch):
    t = FakeTrader(order_ret=42)
    b = _mk(monkeypatch, t, allow_orders=True)
    res = b.submit_order({"code": "000001", "direction": "buy",
                          "volume": 100, "price": 10.5})
    assert res["ok"] is True and res["status"] == "submitted"
    assert res["order_id"] == "42"
    assert len(t.submitted) == 1
    s = t.submitted[0]
    assert s["code"] == "000001.SZ" and s["otype"] == FakeConst.STOCK_BUY
    assert s["ptype"] == FakeConst.FIX_PRICE and s["price"] == 10.5


def test_submit_requires_limit_price(monkeypatch):
    t = FakeTrader()
    b = _mk(monkeypatch, t, allow_orders=True)
    res = b.submit_order({"code": "000001", "direction": "sell",
                          "volume": 100, "price": 0})
    assert res["ok"] is False and "limit price" in res["msg"]
    assert t.submitted == []


def test_submit_rejects_bad_input(monkeypatch):
    t = FakeTrader()
    b = _mk(monkeypatch, t, allow_orders=True)
    assert b.submit_order({"code": "", "direction": "buy",
                           "volume": 100, "price": 1})["ok"] is False
    assert b.submit_order({"code": "000001", "direction": "buy",
                           "volume": 0, "price": 1})["ok"] is False


def test_venue_reject_returns_rejected(monkeypatch):
    t = FakeTrader(order_ret=-1)
    b = _mk(monkeypatch, t, allow_orders=True)
    res = b.submit_order({"code": "600519", "direction": "sell",
                          "volume": 100, "price": 1500.0})
    assert res["ok"] is False and res["status"] == "rejected"
    assert t.submitted[0]["otype"] == FakeConst.STOCK_SELL


# --- read paths --------------------------------------------------------------

def test_asset_mapping(monkeypatch):
    asset = types.SimpleNamespace(cash=100.0, frozen_cash=1.0,
                                  market_value=200.0, total_asset=301.0)
    b = _mk(monkeypatch, FakeTrader(asset=asset))
    assert b.asset() == {"cash": 100.0, "frozen_cash": 1.0,
                         "market_value": 200.0, "total_asset": 301.0}


def test_positions_marked_to_live_price(monkeypatch):
    pos = types.SimpleNamespace(stock_code="000001.SZ", volume=1000,
                                can_use_volume=500, open_price=10.0)
    b = _mk(monkeypatch, FakeTrader(positions=[pos]))
    out = b.positions()["positions"]
    assert len(out) == 1
    p = out[0]
    # last_price is the live quote (11.0), NOT cost (10.0) — audit C5.
    assert p["code"] == "000001" and p["last_price"] == 11.0
    assert p["market_value"] == 11000.0        # 1000 * 11.0
    assert p["avg_price"] == 10.0 and p["can_use_volume"] == 500


def test_positions_emit_stale_sentinel_when_no_quote(monkeypatch):
    """No live quote → emit the 0/0 sentinel (NOT cost basis) so the consumer's
    C5 stale-quote guard fires and the per-stock stop-loss isn't silently
    disabled during a market-data outage."""
    pos = types.SimpleNamespace(stock_code="000001.SZ", volume=100,
                                can_use_volume=0, open_price=10.0)
    b = _mk(monkeypatch, FakeTrader(positions=[pos]))
    monkeypatch.setattr(xd, "_xtdata", FakeXtdata(last=0.0))  # dead feed
    p = b.positions()["positions"][0]
    assert p["last_price"] == 0.0 and p["market_value"] == 0.0
    assert p["avg_price"] == 10.0              # cost still reported for reference


def test_asset_fails_closed_on_none(monkeypatch):
    """A transient query miss must RAISE, not return fabricated zeros — a
    total_asset=0 would make the circuit breaker misread a -100% drawdown."""
    b = _mk(monkeypatch, FakeTrader(asset=None))
    with pytest.raises(Exception):
        b.asset()


def test_positions_fails_closed_on_none(monkeypatch):
    """query_stock_positions returning None is a failure (≠ empty list) → raise
    rather than report an empty book (which could make flatten think there's
    nothing to sell)."""
    t = FakeTrader()
    t._positions = None
    b = _mk(monkeypatch, t)
    with pytest.raises(Exception):
        b.positions()


def test_orders_status_mapping(monkeypatch):
    mk = lambda st, ot=23: types.SimpleNamespace(
        order_id=1, stock_code="000001.SZ", order_type=ot, order_volume=100,
        price=10.0, order_status=st, traded_volume=0, traded_price=0.0,
        order_time=0)
    orders = [mk(FakeConst.ORDER_SUCCEEDED), mk(FakeConst.ORDER_PART_SUCC),
              mk(FakeConst.ORDER_CANCELED), mk(FakeConst.ORDER_JUNK),
              mk(FakeConst.ORDER_REPORTED), mk(FakeConst.ORDER_SUCCEEDED, ot=24)]
    b = _mk(monkeypatch, FakeTrader(orders=orders))
    got = [o["status"] for o in b.orders()["orders"]]
    assert got == ["filled", "partial", "cancelled", "rejected", "accepted", "filled"]
    assert b.orders()["orders"][0]["direction"] == "buy"
    assert b.orders()["orders"][5]["direction"] == "sell"


def test_cancel_delegates(monkeypatch):
    t = FakeTrader(cancel_ret=0)
    b = _mk(monkeypatch, t)
    assert b.cancel({"order_id": "42"})["ok"] is True
    assert t.cancelled == [42]
    assert b.cancel({"order_id": ""})["ok"] is False


def test_data_fresh_tracks_queries(monkeypatch):
    asset = types.SimpleNamespace(cash=1.0, frozen_cash=0.0,
                                  market_value=0.0, total_asset=1.0)
    b = _mk(monkeypatch, FakeTrader(asset=asset))
    b._last_event_at = None
    assert b.data_fresh() is False       # nothing yet
    b.asset()                            # a successful query touches the clock
    assert b.data_fresh() is True


# --- bridge wiring: the gateway reports the direct backend's mode -----------

class _FakeXtGatewayBackend:
    mode = "xt"
    connected = True

    def data_fresh(self, max_age=30.0):
        return True

    def asset(self):
        return {"cash": 1.0, "frozen_cash": 0.0, "market_value": 0.0,
                "total_asset": 1.0}


def test_gateway_health_reports_xt_mode():
    gw = QmtGateway(backend=_FakeXtGatewayBackend())
    h = route(gw, "GET", "/health", {}, None)[1]
    assert gw.mock is False
    assert h["mode"] == "xt"
    assert h["trader_connected"] is True
    assert h["datafeed_ok"] is True


def test_submit_dedups_by_client_order_id(monkeypatch):
    """A repeat client_order_id must NOT call order_stock a second time — the
    coid is stamped into the venue order remark and the in-process cache returns
    the first result (idempotency)."""
    t = FakeTrader(order_ret=42)
    b = _mk(monkeypatch, t, allow_orders=True)
    body = {"code": "000001", "direction": "buy", "volume": 100,
            "price": 10.5, "client_order_id": "c1"}
    r1 = b.submit_order(dict(body))
    r2 = b.submit_order(dict(body))               # same coid → dedup
    assert r1["order_id"] == "42" == r2["order_id"]
    assert len(t.submitted) == 1                   # order_stock called ONCE
    assert t.submitted[0]["remark"] == "c1"        # coid stamped into remark
    # a different coid does submit again
    b.submit_order({**body, "client_order_id": "c2"})
    assert len(t.submitted) == 2


def test_dedup_check_fails_closed_on_query_none(monkeypatch):
    """If the venue orders query can't complete (None on a stale session), the
    dedup check must RAISE (fail-closed) — never fall through to a fresh
    order_stock, which would place a DUPLICATE real order on a restart-retry."""
    t = FakeTrader(order_ret=42)
    t._orders = None                       # stale session → query returns None
    b = _mk(monkeypatch, t, allow_orders=True)
    with pytest.raises(Exception):
        b.submit_order({"code": "000001", "direction": "buy", "volume": 100,
                        "price": 10.5, "client_order_id": "fresh"})
    assert t.submitted == []               # nothing placed


def test_dedup_hit_on_cancelled_order_returns_not_ok(monkeypatch):
    """A dedup hit on an already cancelled/rejected venue order returns ok=False
    (not a false 'accepted' that would bump the daily-trade count)."""
    cancelled = types.SimpleNamespace(
        order_id=7, order_remark="c9", order_status=FakeConst.ORDER_CANCELED,
        traded_volume=0, traded_price=0.0)
    t = FakeTrader(orders=[cancelled], order_ret=42)
    b = _mk(monkeypatch, t, allow_orders=True)
    res = b.submit_order({"code": "000001", "direction": "buy", "volume": 100,
                          "price": 10.5, "client_order_id": "c9"})
    assert res["ok"] is False and res["status"] == "cancelled"
    assert t.submitted == []               # deduped: no re-submit
