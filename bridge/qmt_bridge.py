"""qmt-bridge — localhost HTTP gateway to QMT / miniQMT (``xtquant``).

Why this exists
---------------
``xtquant`` is bundled with the QMT client and pinned to *its* Python version,
which is almost never the same interpreter quanti runs on. So we isolate it in
its own process: this bridge runs on the QMT-bundled Python, ``import``s
``xtquant``, and exposes trading + market-data over plain localhost HTTP.
``quanti`` (any Python) then talks to it via ``QmtBroker`` / ``XtdataAdapter``.

    QMT client (logged in, miniQMT mode)
        │  xtquant (xttrader / xtdata)
    qmt_bridge.py  ──HTTP──>  quanti (QmtBroker, XtdataAdapter)

The HTTP layer is **stdlib only**; the live backend (see ``vnpy_backend.py``) is
what touches the venue. quanti is never imported here, so the bridge drops onto
whatever interpreter the QMT stack requires.

Two backends, one HTTP contract
-------------------------------
* **vnpy backend** (``VnpyBackend``) — when ``vnpy`` + a QMT gateway (``vnpy_xt``)
  are installed (the Windows/QMT box). Drives vnpy's mature gateway headless and
  translates its events into the contract below. (Option B of the live-trading
  roadmap: don't hand-roll the xtquant order/fill/reconnect glue — reuse vnpy's.)
* **mock backend** — anywhere else: deterministic synthetic asset / positions /
  orders / k-line so the whole quanti↔bridge chain is runnable and testable
  before the real QMT environment exists.

The HTTP contract is identical in both modes; only the backend differs. Bits of
the vnpy backend that depend on the exact gateway build are marked ``# VERIFY``.

Run:  python bridge/qmt_bridge.py --host 127.0.0.1 --port 18099
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("qmt-bridge")

BRIDGE_VERSION = "0.1"

# xtquant is only importable on the QMT box. Absence => mock mode.
try:  # pragma: no cover - depends on the QMT environment
    import xtquant.xtdata as _xtdata  # type: ignore  # noqa: F401
    from xtquant.xttrader import XtQuantTrader  # type: ignore  # noqa: F401

    XTQUANT_AVAILABLE = True
except Exception:  # noqa: BLE001 - any import failure means "not on QMT box"
    XTQUANT_AVAILABLE = False

# Live backend = vnpy's QMT gateway (Option B). Importing VnpyBackend never
# fails — it guards its own vnpy import — so this works in mock mode too.
try:
    from bridge.vnpy_backend import VnpyBackend
except ImportError:  # when run as a script from inside bridge/
    from vnpy_backend import VnpyBackend


def _connect_setting_from_env() -> dict:  # pragma: no cover - QMT box only
    """vnpy gateway connect dict, built from env vars. VERIFY the exact keys
    against the installed vnpy_xt gateway's `default_setting`."""
    import os
    return {
        "账号": os.environ.get("QMT_ACCOUNT", ""),
        "miniQMT路径": os.environ.get("QMT_USERDATA_MINI", ""),
        "session_id": os.environ.get("QMT_SESSION_ID", "0"),
    }


def _today() -> date:
    return datetime.now().date()


class QmtGateway:
    """Implements each bridge operation against xtquant, or a mock when absent.

    One instance per process. Trading methods take/return plain dicts shaped
    exactly like the HTTP JSON so :func:`route` is a thin pass-through and unit
    tests can call these directly without a socket.
    """

    def __init__(self, backend: "VnpyBackend | None" = None) -> None:
        # Live backend = vnpy QMT gateway when available (or an injected fake in
        # tests). Absent → mock mode. The mock state below is always set so the
        # synthetic engine is usable for dev/tests regardless of mode.
        if backend is None and VnpyBackend.available():  # pragma: no cover - QMT box
            backend = VnpyBackend(_connect_setting_from_env())
            backend.start()
        self._backend = backend
        self.mock = backend is None
        # --- mock state (only used in mock mode) ---
        self._mock_cash = 1_000_000.0
        self._mock_positions: dict[str, dict] = {}
        self._mock_orders: list[dict] = []
        self._mock_trades: list[dict] = []
        self._seq = 0
        # When False, mock orders rest as "accepted" instead of filling at
        # submit — lets tests exercise the cancel / pending-reconcile paths.
        self._mock_autofill = True

    # ------------------------------------------------------------ health
    def health(self) -> dict:
        return {
            "ok": True,
            "xtquant": XTQUANT_AVAILABLE,
            "vnpy": VnpyBackend.available(),
            "trader_connected": bool(
                self._backend and getattr(self._backend, "connected", False)),
            "mode": "mock" if self.mock else "vnpy",
            "version": BRIDGE_VERSION,
        }

    # ------------------------------------------------------------ trading
    def asset(self) -> dict:
        if self.mock:
            mv = sum(p["volume"] * p["avg_price"]
                     for p in self._mock_positions.values())
            return {"cash": round(self._mock_cash, 2), "frozen_cash": 0.0,
                    "market_value": round(mv, 2),
                    "total_asset": round(self._mock_cash + mv, 2)}
        return self._backend.asset()

    def positions(self) -> dict:
        if self.mock:
            out = []
            for code, p in self._mock_positions.items():
                if p["volume"] <= 0:
                    continue
                last = float(self.quote(code).get("last", 0) or 0)
                cur = last if last > 0 else p["avg_price"]
                out.append({
                    "code": code, "volume": p["volume"],
                    "can_use_volume": p["can_use"], "avg_price": p["avg_price"],
                    "last_price": round(cur, 3),
                    "market_value": round(p["volume"] * cur, 2)})
            return {"positions": out}
        return self._backend.positions()

    def submit_order(self, body: dict) -> dict:
        code = str(body.get("code", ""))
        direction = str(body.get("direction", "")).lower()
        volume = int(body.get("volume", 0))
        price = float(body.get("price", 0) or 0)
        if not code or direction not in ("buy", "sell") or volume <= 0:
            return {"ok": False, "order_id": "", "status": "rejected",
                    "msg": "bad order: need code, direction(buy/sell), volume>0"}
        if self.mock:
            if self._mock_autofill:
                return self._mock_fill(code, direction, volume, price)
            return self._mock_accept(code, direction, volume, price)
        return self._backend.submit_order(body)

    def cancel(self, body: dict) -> dict:
        order_id = str(body.get("order_id", ""))
        if self.mock:
            for o in self._mock_orders:
                if o["order_id"] == order_id and o["status"] in (
                        "pending", "accepted"):
                    o["status"] = "cancelled"
                    return {"ok": True, "msg": "cancelled"}
            return {"ok": False, "msg": "order not cancellable (unknown/filled)"}
        return self._backend.cancel(body)

    def orders(self) -> dict:
        if self.mock:
            return {"orders": list(self._mock_orders)}
        return self._backend.orders()

    def trades(self) -> dict:
        if self.mock:
            return {"trades": list(self._mock_trades)}
        return self._backend.trades()

    # ------------------------------------------------------------ data
    def kline(self, code: str, start: str, end: str, period: str) -> dict:
        if not code:
            return {"code": code, "period": period, "bars": []}
        if self.mock:
            return {"code": code, "period": period,
                    "bars": _mock_bars(code, start, end)}
        return self._backend.kline(code, start, end, period)

    def stock_list(self) -> dict:
        if self.mock:
            return {"stocks": _MOCK_STOCKS}
        return self._backend.stock_list()

    def quote(self, code: str) -> dict:
        if self.mock:
            last = round(10 + (sum(ord(c) for c in code) % 50) * 0.37, 2)
            return {"code": code, "last": last, "open": last, "high": last,
                    "low": last, "time": datetime.now().isoformat(),
                    "bid": last, "ask": last}
        return self._backend.quote(code)

    # ------------------------------------------------- mock fill engine
    def _mock_fill(self, code: str, direction: str, volume: int,
                   price: float) -> dict:
        """Mock venue: fill the whole order immediately at the given (or a
        synthetic) price, and reflect it in mock positions / cash / trades."""
        self._seq += 1
        order_id = f"mock-{self._seq}"
        fill_price = price or self.quote(code)["last"]
        pos = self._mock_positions.setdefault(
            code, {"volume": 0, "can_use": 0, "avg_price": 0.0})
        if direction == "buy":
            cost = fill_price * volume
            new_vol = pos["volume"] + volume
            pos["avg_price"] = (
                (pos["avg_price"] * pos["volume"] + cost) / new_vol
                if new_vol else fill_price)
            pos["volume"] = new_vol
            # T+1: today's buy adds to volume but NOT can_use (frozen today).
            self._mock_cash -= cost
        else:  # sell — only the T+1-sellable (can_use) portion can fill
            sellable = min(volume, pos["can_use"])
            if sellable <= 0:
                # No sellable inventory (e.g. all bought today, or no holding):
                # reject loudly — never a phantom 0-volume "filled" sell.
                return {"ok": False, "order_id": "", "status": "rejected",
                        "filled_volume": 0, "filled_price": 0.0,
                        "msg": "no sellable position (T+1 frozen)"}
            pos["volume"] -= sellable
            pos["can_use"] -= sellable
            self._mock_cash += fill_price * sellable
            volume = sellable
        now = datetime.now().isoformat()
        self._mock_orders.append({
            "order_id": order_id, "code": code, "direction": direction,
            "volume": volume, "price": fill_price, "status": "filled",
            "filled_volume": volume, "filled_price": fill_price,
            "created_at": now})
        self._mock_trades.append({
            "trade_id": f"t-{self._seq}", "order_id": order_id, "code": code,
            "direction": direction, "volume": volume, "price": fill_price,
            "time": now})
        return {"ok": True, "order_id": order_id, "status": "filled",
                "filled_volume": volume, "filled_price": fill_price, "msg": ""}

    def _mock_accept(self, code: str, direction: str, volume: int,
                     price: float) -> dict:
        """Record an order that rests open (status 'accepted') without filling
        — used when auto-fill is off, so the cancel / pending-reconcile paths
        can be exercised. No cash/position change until/unless it later fills."""
        self._seq += 1
        order_id = f"mock-{self._seq}"
        self._mock_orders.append({
            "order_id": order_id, "code": code, "direction": direction,
            "volume": volume, "price": price, "status": "accepted",
            "filled_volume": 0, "filled_price": 0.0,
            "created_at": datetime.now().isoformat()})
        return {"ok": True, "order_id": order_id, "status": "accepted",
                "filled_volume": 0, "filled_price": price, "msg": ""}

    def settle(self) -> None:
        """Test/dev helper: simulate overnight T+1 settlement so every held lot
        becomes sellable (can_use = volume). Real settlement is the venue's job."""
        for p in self._mock_positions.values():
            p["can_use"] = p["volume"]


_MOCK_STOCKS = [
    {"code": "000001", "name": "平安银行", "exchange": "SZ"},
    {"code": "600519", "name": "贵州茅台", "exchange": "SH"},
    {"code": "300750", "name": "宁德时代", "exchange": "SZ"},
]


def _mock_bars(code: str, start: str, end: str) -> list[dict]:
    """Deterministic synthetic daily bars across [start, end] business days."""
    try:
        s = datetime.strptime(start, "%Y%m%d").date() if start else _today() - timedelta(days=30)
        e = datetime.strptime(end, "%Y%m%d").date() if end else _today()
    except ValueError:
        s, e = _today() - timedelta(days=30), _today()
    base = 10 + (sum(ord(c) for c in code) % 50) * 0.37
    bars: list[dict] = []
    d = s
    i = 0
    while d <= e:
        if d.weekday() < 5:  # business days only
            px = round(base + (i % 7) * 0.1 - (i % 3) * 0.05, 2)
            bars.append({
                "date": d.isoformat(), "open": round(px - 0.05, 2),
                "high": round(px + 0.1, 2), "low": round(px - 0.1, 2),
                "close": px, "volume": 1_000_000.0 + (i % 5) * 1000,
                "amount": round(px * 1_000_000, 2)})
            i += 1
        d += timedelta(days=1)
    return bars


# ----------------------------------------------------------------- routing

def route(gw: QmtGateway, method: str, path: str,
          query: dict, body: dict | None) -> tuple[int, dict]:
    """Pure dispatch: (gateway, request) -> (http_status, json_dict).

    Kept free of socket plumbing so it's unit-testable directly.
    """
    try:
        if method == "GET":
            if path == "/health":
                return 200, gw.health()
            if path == "/trader/asset":
                return 200, gw.asset()
            if path == "/trader/positions":
                return 200, gw.positions()
            if path == "/trader/orders":
                return 200, gw.orders()
            if path == "/trader/trades":
                return 200, gw.trades()
            if path == "/data/kline":
                return 200, gw.kline(_q(query, "code"), _q(query, "start"),
                                     _q(query, "end"),
                                     _q(query, "period") or "1d")
            if path == "/data/stock_list":
                return 200, gw.stock_list()
            if path == "/data/quote":
                return 200, gw.quote(_q(query, "code"))
        elif method == "POST":
            if path == "/trader/order":
                return 200, gw.submit_order(body or {})
            if path == "/trader/cancel":
                return 200, gw.cancel(body or {})
        return 404, {"ok": False, "msg": f"no route: {method} {path}"}
    except NotImplementedError as e:
        return 501, {"ok": False, "msg": str(e)}
    except Exception as e:  # noqa: BLE001 - never crash the bridge on one request
        logger.exception("bridge error on %s %s", method, path)
        return 500, {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def _q(query: dict, key: str) -> str:
    v = query.get(key)
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


def _make_handler(gw: QmtGateway):
    class Handler(BaseHTTPRequestHandler):
        def _respond(self, method: str) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            body = None
            if method == "POST":
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    self._send(400, {"ok": False, "msg": "invalid JSON body"})
                    return
            status, payload = route(gw, method, parsed.path, query, body)
            self._send(status, payload)

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            self._respond("GET")

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            self._respond("POST")

        def log_message(self, fmt, *args) -> None:  # quiet by default
            logger.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


def serve(host: str = "127.0.0.1", port: int = 18099) -> None:
    gw = QmtGateway()
    httpd = ThreadingHTTPServer((host, port), _make_handler(gw))
    mode = "MOCK (xtquant not found)" if gw.mock else "LIVE (xtquant)"
    logger.info("qmt-bridge v%s listening on http://%s:%d  [%s]",
                BRIDGE_VERSION, host, port, mode)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="qmt-bridge: localhost HTTP "
                                             "gateway to QMT/xtquant")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18099)
    args = ap.parse_args()
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
