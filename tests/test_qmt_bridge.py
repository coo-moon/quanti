"""Tests for the qmt-bridge mock gateway + HTTP plumbing.

Exercises the contract QmtBroker / XtdataAdapter depend on, in mock mode
(xtquant absent), so this runs anywhere — not just on the QMT box.
"""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

from bridge.qmt_bridge import QmtGateway, _make_handler, route


@pytest.fixture
def gw():
    return QmtGateway()


def test_health_is_mock_without_xtquant(gw):
    status, body = route(gw, "GET", "/health", {}, None)
    assert status == 200
    assert body["ok"] is True
    assert body["mode"] == "mock"
    assert body["xtquant"] is False


def test_kline_returns_ohlc_bars(gw):
    status, body = route(gw, "GET", "/data/kline",
                         {"code": ["000001"], "start": ["20240101"],
                          "end": ["20240131"]}, None)
    assert status == 200
    assert body["code"] == "000001"
    assert len(body["bars"]) > 0
    bar = body["bars"][0]
    assert set(bar) >= {"date", "open", "high", "low", "close", "volume", "amount"}
    assert bar["high"] >= bar["low"]


def test_stock_list(gw):
    status, body = route(gw, "GET", "/data/stock_list", {}, None)
    assert status == 200
    assert any(s["code"] == "000001" for s in body["stocks"])


def test_order_fills_and_updates_asset_positions(gw):
    asset0 = route(gw, "GET", "/trader/asset", {}, None)[1]
    status, res = route(gw, "POST", "/trader/order", {},
                        {"code": "000001", "direction": "buy",
                         "volume": 1000, "price": 10.0})
    assert status == 200 and res["ok"] is True
    assert res["status"] == "filled"
    order_id = res["order_id"]

    asset1 = route(gw, "GET", "/trader/asset", {}, None)[1]
    assert asset1["cash"] == pytest.approx(asset0["cash"] - 10.0 * 1000)

    pos = route(gw, "GET", "/trader/positions", {}, None)[1]["positions"]
    assert any(p["code"] == "000001" and p["volume"] == 1000 for p in pos)

    # A filled order can't be cancelled.
    _, cancel = route(gw, "POST", "/trader/cancel", {}, {"order_id": order_id})
    assert cancel["ok"] is False


def test_bad_order_rejected(gw):
    _, res = route(gw, "POST", "/trader/order", {},
                   {"code": "000001", "direction": "sideways", "volume": 100})
    assert res["ok"] is False


def test_sell_with_no_position_rejected_not_phantom_filled(gw):
    # No holding → sell must be rejected, never a 0-volume "filled" + ghost trade.
    _, res = route(gw, "POST", "/trader/order", {},
                   {"code": "000001", "direction": "sell", "volume": 100})
    assert res["ok"] is False and res["status"] == "rejected"
    assert route(gw, "GET", "/trader/trades", {}, None)[1]["trades"] == []


def test_sell_blocked_by_t1_then_allowed_after_settle(gw):
    route(gw, "POST", "/trader/order", {},
          {"code": "000001", "direction": "buy", "volume": 1000, "price": 10.0})
    # Same-day buy is T+1-frozen (can_use stays 0) → sell rejected.
    _, blocked = route(gw, "POST", "/trader/order", {},
                       {"code": "000001", "direction": "sell", "volume": 1000})
    assert blocked["ok"] is False
    gw.settle()  # overnight settlement frees the lot
    _, allowed = route(gw, "POST", "/trader/order", {},
                       {"code": "000001", "direction": "sell", "volume": 1000})
    assert allowed["ok"] is True and allowed["status"] == "filled"


def test_cancel_open_order_succeeds(gw):
    gw._mock_autofill = False  # orders rest as 'accepted' instead of filling
    _, res = route(gw, "POST", "/trader/order", {},
                   {"code": "600519", "direction": "buy", "volume": 100,
                    "price": 1600.0})
    assert res["status"] == "accepted"
    _, cancel = route(gw, "POST", "/trader/cancel", {},
                      {"order_id": res["order_id"]})
    assert cancel["ok"] is True
    orders = route(gw, "GET", "/trader/orders", {}, None)[1]["orders"]
    assert any(o["order_id"] == res["order_id"] and o["status"] == "cancelled"
               for o in orders)


def test_unknown_route_404(gw):
    status, _ = route(gw, "GET", "/nope", {}, None)
    assert status == 404


class _FakeBackend:
    """Stand-in for the vnpy backend so we can test the live delegation path
    without vnpy/xtquant installed."""
    connected = True

    def __init__(self):
        self.calls = []

    def asset(self):
        self.calls.append("asset")
        return {"cash": 5.0, "frozen_cash": 0.0, "market_value": 0.0,
                "total_asset": 5.0}

    def positions(self):
        self.calls.append("positions")
        return {"positions": []}

    def submit_order(self, body):
        self.calls.append(("submit", body))
        return {"ok": True, "order_id": "v-1", "status": "submitted",
                "filled_volume": 0, "filled_price": 0.0, "msg": ""}

    def cancel(self, body):
        self.calls.append(("cancel", body))
        return {"ok": True, "msg": "cancel sent"}

    def orders(self):
        return {"orders": [{"order_id": "v-1", "status": "accepted"}]}

    def trades(self):
        return {"trades": []}

    def kline(self, code, start, end, period):
        return {"code": code, "period": period, "bars": []}

    def stock_list(self):
        return {"stocks": []}

    def quote(self, code):
        return {"code": code, "last": 1.23}


def test_live_backend_delegation():
    """With a backend injected, the gateway leaves mock mode and delegates
    every op to the backend (the vnpy path's wiring, testable without vnpy)."""
    fake = _FakeBackend()
    gw = QmtGateway(backend=fake)
    assert gw.mock is False
    assert route(gw, "GET", "/health", {}, None)[1]["mode"] == "vnpy"
    assert route(gw, "GET", "/trader/asset", {}, None)[1]["cash"] == 5.0
    res = route(gw, "POST", "/trader/order", {},
                {"code": "000001", "direction": "buy",
                 "volume": 100, "price": 10.0})[1]
    assert res["order_id"] == "v-1" and res["status"] == "submitted"
    assert route(gw, "POST", "/trader/cancel", {},
                 {"order_id": "v-1"})[1]["ok"] is True
    assert route(gw, "GET", "/data/quote", {"code": ["000001"]}, None)[1]["last"] == 1.23
    assert "asset" in fake.calls


def test_http_round_trip_health():
    """The actual socket path: handler -> route -> JSON over HTTP."""
    gw = QmtGateway()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(gw))
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5)
        assert r.status_code == 200
        assert r.json()["mode"] == "mock"
        r2 = httpx.post(f"http://127.0.0.1:{port}/trader/order",
                        json={"code": "600519", "direction": "buy",
                              "volume": 100, "price": 1600.0}, timeout=5)
        assert r2.json()["status"] == "filled"
    finally:
        httpd.shutdown()
        httpd.server_close()
