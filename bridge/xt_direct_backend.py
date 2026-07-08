# -*- coding: utf-8 -*-
"""Direct-xtquant backend for the qmt-bridge (roadmap Option A).

Why this exists (vs the vnpy backend)
-------------------------------------
vnpy / vnpy_xt (the Option-B backend) has concrete incompatibilities with
miniQMT that we verified on a live box:

* vnpy_xt's ``XtTdApi`` hardcodes the trade path as ``QMT路径 + "\\userdata"`` —
  miniQMT trading lives under ``userdata_mini`` → it connects to the wrong path;
* its connect ``default_setting`` keys are ``资金账号 / QMT路径 / 账号类型 / 仿真交易``
  (the bridge's old env mapping used the wrong keys);
* the ``仿真交易`` flag gates whether the trade API connects at all and its
  semantics are ambiguous — dangerous under real money.

Driving ``xtquant`` directly sidesteps all of that: it connects via the real
``userdata_mini`` path and uses xttrader's ``STOCK_BUY``/``STOCK_SELL`` order
types (no A-share ``Offset`` ambiguity at all). It needs any Python with
``xtquant`` importable — verified working from a standard Python 3.13 venv with
the PyPI ``xtquant`` (both data via xtdata AND trading via xttrader: connect /
query_account_infos / query_stock_asset all succeed against a live 江海 miniQMT).

    QMT mini client (logged in, account exposed)
        │ xtquant.xttrader (trade) + xtquant.xtdata (data)
    XtDirectBackend  ──dict (bridge contract)──▶  qmt_bridge HTTP ──▶ quanti

The HTTP contract (dict shapes) is identical to :class:`bridge.vnpy_backend.
VnpyBackend`; only the venue plumbing differs. Everything that touches the venue
is import-guarded so this module loads anywhere (``available()`` is False without
xtquant); the bridge then falls back to mock.

Safety: order submission is OFF by default. Set ``QMT_BRIDGE_ALLOW_ORDERS=1`` to
let live orders through — until then ``submit_order`` rejects with a clear
message, so the whole read/snapshot path can be validated against a real account
without any risk of a live order.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime

logger = logging.getLogger("qmt-bridge.xtdirect")

# xtdata (market data) — ships with QMT; importable on the client's python.
try:  # pragma: no cover - QMT box only
    from xtquant import xtdata as _xtdata
    _XTDATA_OK = True
except Exception:  # noqa: BLE001 - not on the QMT box
    _xtdata = None
    _XTDATA_OK = False

# xttrader (trading) + constants.
try:  # pragma: no cover - QMT box only
    from xtquant import xtconstant
    from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
    from xtquant.xttype import StockAccount
    _XTTRADER_OK = True
except Exception:  # noqa: BLE001
    xtconstant = None
    XtQuantTrader = None
    StockAccount = None
    XtQuantTraderCallback = object  # so the callback subclass below still defines
    _XTTRADER_OK = False


def _status_map():  # pragma: no cover - needs xtconstant
    """xttrader order_status int → the bridge's status vocabulary."""
    c = xtconstant
    return {
        c.ORDER_SUCCEEDED: "filled",
        c.ORDER_PART_SUCC: "partial",
        c.ORDER_CANCELED: "cancelled",
        c.ORDER_PART_CANCEL: "cancelled",
        c.ORDER_PARTSUCC_CANCEL: "cancelled",
        c.ORDER_JUNK: "rejected",
        c.ORDER_UNREPORTED: "accepted",
        c.ORDER_WAIT_REPORTING: "accepted",
        c.ORDER_REPORTED: "accepted",
        c.ORDER_REPORTED_CANCEL: "accepted",
    }


class _Callback(XtQuantTraderCallback):  # pragma: no cover - QMT box only
    """Minimal callback: track live/disconnect + freshness. Queries are sync, so
    we don't accumulate order/trade state here — we just keep a liveness clock."""

    def __init__(self, backend):
        self._b = backend

    def on_disconnected(self):
        self._b._mark_disconnected()

    def on_account_status(self, status):
        self._b._touch()

    def on_stock_order(self, order):
        self._b._touch()

    def on_stock_trade(self, trade):
        self._b._touch()


class XtDirectBackend:
    """Drives xtquant (xttrader + xtdata) directly and exposes the bridge
    contract. One instance per process."""

    mode = "xt"

    @staticmethod
    def available() -> bool:
        """True only where both xttrader and xtdata import (the QMT box). xtdata
        is required so positions()/quote() can mark to the live price rather than
        silently falling back to cost basis (which disables stop-loss, audit C5)."""
        return _XTTRADER_OK and _XTDATA_OK

    def __init__(self, account_id: str, userdata_path: str,
                 account_type: str = "STOCK", session: int = 0) -> None:
        self._account_id = str(account_id)
        self._path = userdata_path
        self._account_type = account_type or "STOCK"
        # Fresh session id per process; xttrader rejects duplicate live sessions.
        self._session = int(session) if session else int(time.time())
        self._trader = None
        self._acc = None
        self._coid_results = {}  # client_order_id → result dict (idempotency cache)
        self._lock = threading.RLock()        # guards connected / freshness clock
        self._venue_lock = threading.RLock()  # serializes ALL trader I/O — the
        # HTTP server is threaded and xttrader is not documented thread-safe.
        self.connected = False
        self._last_event_at = None  # monotonic ts of last good event/query
        self._last_reconnect = 0.0
        self._reconnect_cooldown = 5.0
        # Heartbeat: poll the trader on an interval so datafeed freshness (H2)
        # doesn't hinge on quanti's poll cadence. xttrader gives no continuous
        # event stream like vnpy's gateway, so without this the freshness clock
        # goes stale between guard cycles and is_connected() flaps → orders get
        # refused mid-session. A failing heartbeat marks the trader disconnected.
        self._hb_interval = 10.0
        self._hb_stop = None
        self._hb_thread = None
        # Live orders are OFF unless explicitly acknowledged. Read/snapshot works
        # regardless; this only gates submit_order (cancel of an existing order
        # is always allowed — it can only reduce exposure).
        self._allow_orders = os.environ.get("QMT_BRIDGE_ALLOW_ORDERS", "").strip() == "1"

    # ------------------------------------------------------------ liveness
    def _touch(self) -> None:
        with self._lock:
            self._last_event_at = time.monotonic()

    def _mark_disconnected(self) -> None:
        with self._lock:
            self.connected = False
        logger.warning("xtdirect: trader disconnected")

    def data_fresh(self, max_age: float = 30.0) -> bool:
        """True iff a good query/event happened within ``max_age`` seconds — the
        live-feed signal the sticky ``connected`` bool can't give (audit H2)."""
        with self._lock:
            ts = self._last_event_at
        return ts is not None and (time.monotonic() - ts) < max_age

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:  # pragma: no cover - QMT box only
        if not self.available():
            raise RuntimeError("xtquant (xttrader/xtdata) not importable — cannot "
                               "start the direct backend")
        self._connect()
        self._start_heartbeat()

    def _start_heartbeat(self) -> None:  # pragma: no cover - QMT box only
        if self._hb_thread is not None:
            return
        self._hb_stop = threading.Event()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, name="xtdirect-heartbeat", daemon=True)
        self._hb_thread.start()

    def _heartbeat_loop(self) -> None:  # pragma: no cover - QMT box only
        while not self._hb_stop.wait(self._hb_interval):
            try:
                if not self.connected:
                    self._ensure()
                    continue
                with self._venue_lock:
                    a = self._trader.query_stock_asset(self._acc)
                if a is not None:
                    self._touch()
                else:
                    # xtquant sync queries return None (not raise) on a stale /
                    # soft-disconnected session, and on_disconnected is unreliable
                    # for those. Treat None as a dead probe so `connected` flips
                    # False and the next _ensure() rebuilds the trader — otherwise
                    # a silent disconnect wedges connected=True forever (never
                    # self-heals) yet datafeed_ok goes stale, blocking all orders.
                    self._mark_disconnected()
            except Exception:  # noqa: BLE001 - a failing probe == dead feed
                self._mark_disconnected()

    def _connect(self) -> bool:  # pragma: no cover - QMT box only
        """(Re)connect the trader and confirm the account is actually exposed.
        ``connected`` is True only when connect succeeds AND the logged-in account
        is visible to the API — a running-but-not-logged-in client (empty account
        list, audit-verified real failure mode) reads as NOT connected."""
        with self._venue_lock:
            try:
                if self._trader is not None:
                    try:
                        self._trader.stop()
                    except Exception:  # noqa: BLE001
                        pass
                self._session = int(time.time())
                self._trader = XtQuantTrader(self._path, self._session)
                self._trader.register_callback(_Callback(self))
                self._trader.start()
                rc = self._trader.connect()
                if rc != 0:
                    logger.warning("xtdirect: connect() -> %s (QMT trade service "
                                   "not reachable / not logged in)", rc)
                    with self._lock:
                        self.connected = False
                    return False
                self._acc = StockAccount(self._account_id, account_type=self._account_type)
                self._trader.subscribe(self._acc)
                infos = self._trader.query_account_infos() or []
                account_ok = any(
                    str(getattr(a, "account_id", "")) == self._account_id for a in infos)
                with self._lock:
                    self.connected = bool(account_ok)
                    if account_ok:
                        self._last_event_at = time.monotonic()
                if not account_ok:
                    logger.warning("xtdirect: connected but account %s not exposed "
                                   "(is it logged into the QMT trade terminal?)",
                                   self._account_id)
                else:
                    logger.info("xtdirect: connected, account %s live", self._account_id)
                return account_ok
            except Exception as e:  # noqa: BLE001
                logger.exception("xtdirect: connect failed: %s", e)
                with self._lock:
                    self.connected = False
                return False

    def _ensure(self) -> bool:  # pragma: no cover - QMT box only
        with self._lock:
            if self.connected:
                return True
            now = time.monotonic()
            if now - self._last_reconnect < self._reconnect_cooldown:
                return False
            self._last_reconnect = now
        return self._connect()

    def close(self) -> None:  # pragma: no cover
        if self._hb_stop is not None:
            self._hb_stop.set()
        with self._venue_lock:
            if self._trader is not None:
                try:
                    self._trader.stop()
                except Exception:  # noqa: BLE001
                    pass
            with self._lock:
                self.connected = False

    # ------------------------------------------------------------ trading
    def asset(self) -> dict:
        # FAIL-CLOSED: a transient failure must NOT return a fabricated all-zeros
        # asset. total_asset=0 makes QmtBroker's circuit breaker read a -100%
        # drawdown → false flatten/halt (and with orders on, liquidation at the
        # 跌停 floor). Raise instead → bridge returns 500 → the client raises →
        # the guard's try/except skips this tick and retries. "data unavailable"
        # and "account worth 0" must never look the same downstream.
        if not self._ensure():
            raise RuntimeError("xtdirect: trader not connected (asset)")
        with self._venue_lock:
            a = self._trader.query_stock_asset(self._acc)
        if a is None:
            raise RuntimeError("xtdirect: query_stock_asset returned None "
                               "(session/feed down)")
        self._touch()
        return {"cash": round(float(getattr(a, "cash", 0.0)), 2),
                "frozen_cash": round(float(getattr(a, "frozen_cash", 0.0)), 2),
                "market_value": round(float(getattr(a, "market_value", 0.0)), 2),
                "total_asset": round(float(getattr(a, "total_asset", 0.0)), 2)}

    def positions(self) -> dict:
        # FAIL-CLOSED like asset(): don't return an empty book on a transient
        # query failure (that could make flatten think there's nothing to sell,
        # or mask a disconnect). An empty *list* from the venue is a valid "no
        # holdings"; only None / not-connected is a failure → raise.
        if not self._ensure():
            raise RuntimeError("xtdirect: trader not connected (positions)")
        with self._venue_lock:
            poss = self._trader.query_stock_positions(self._acc)
        if poss is None:
            raise RuntimeError("xtdirect: query_stock_positions returned None")
        poss = [p for p in poss if int(getattr(p, "volume", 0)) > 0]
        # Mark to the live price so market_value tracks the quote, not cost —
        # cost-based mv makes QmtBroker derive current_price ≡ avg → pnl≡0 → the
        # per-stock stop-loss never fires on real money (audit C5).
        last_by_code = {}
        if poss:
            try:
                xt_codes = [self._xt_symbol(_bare(p)) for p in poss]
                ticks = _xtdata.get_full_tick(xt_codes) or {}
                for p in poss:
                    t = ticks.get(self._xt_symbol(_bare(p)), {}) or {}
                    last_by_code[_bare(p)] = float(t.get("lastPrice", 0) or 0)
            except Exception:  # noqa: BLE001 - fall back to cost on quote failure
                pass
        out = []
        for p in poss:
            code = _bare(p)
            vol = int(getattr(p, "volume", 0))
            avg = float(getattr(p, "open_price", 0.0) or getattr(p, "avg_price", 0.0))
            sellable = int(getattr(p, "can_use_volume", 0))
            last = last_by_code.get(code, 0.0)
            if last > 0:
                out.append({"code": code, "volume": vol, "can_use_volume": sellable,
                            "avg_price": avg, "last_price": round(last, 3),
                            "market_value": round(vol * last, 2)})
            else:
                # No live quote for this code (feed hiccup / 停牌 / stall) → emit
                # the 0/0 no-quote sentinel so the consumer's stale-quote guard
                # (audit C5) fires and skips+alerts. Masking it with cost basis
                # pins pnl≡0 and silently disables this stock's stop-loss.
                out.append({"code": code, "volume": vol, "can_use_volume": sellable,
                            "avg_price": avg, "last_price": 0.0, "market_value": 0.0})
        self._touch()
        return {"positions": out}

    def submit_order(self, body: dict) -> dict:
        code = str(body.get("code", ""))
        direction = str(body.get("direction", "")).lower()
        volume = int(body.get("volume", 0))
        price = float(body.get("price", 0) or 0)
        if not code or direction not in ("buy", "sell") or volume <= 0:
            return {"ok": False, "order_id": "", "status": "rejected",
                    "filled_volume": 0, "filled_price": 0.0,
                    "msg": "bad order: need code, direction(buy/sell), volume>0"}
        if not self._allow_orders:
            return {"ok": False, "order_id": "", "status": "rejected",
                    "filled_volume": 0, "filled_price": 0.0,
                    "msg": "bridge orders disabled — set QMT_BRIDGE_ALLOW_ORDERS=1 "
                           "to enable live order submission"}
        if price <= 0:
            # quanti always sends a computed limit price; refuse to guess a
            # market-order type (exchange-specific, easy to route wrong).
            return {"ok": False, "order_id": "", "status": "rejected",
                    "filled_volume": 0, "filled_price": 0.0,
                    "msg": "limit price required (price>0); market orders not supported"}
        if not self._ensure():
            return {"ok": False, "order_id": "", "status": "rejected",
                    "filled_volume": 0, "filled_price": 0.0,
                    "msg": "trader not connected"}
        # Idempotency: never submit the same client_order_id twice at the venue.
        # A retry (network hiccup / crash / overnight-queue re-tick) that carries
        # the same coid returns the EXISTING order's status instead of placing a
        # second real order.
        coid = str(body.get("client_order_id", "")).strip()
        if coid:
            prev = self._find_by_coid(coid)
            if prev is not None:
                return prev
        otype = xtconstant.STOCK_BUY if direction == "buy" else xtconstant.STOCK_SELL
        # Stamp the coid into the order remark so the venue itself carries it —
        # that's what makes dedup survive a bridge restart (the in-memory cache is
        # gone, but _find_by_coid re-discovers the order by its remark).
        remark = coid or str(body.get("reason", ""))[:22]
        with self._venue_lock:
            oid = self._trader.order_stock(
                self._acc, self._xt_symbol(code), otype, volume,
                xtconstant.FIX_PRICE, price, "quanti", remark)
        if oid is None or int(oid) < 0:
            return {"ok": False, "order_id": "", "status": "rejected",
                    "filled_volume": 0, "filled_price": 0.0,
                    "msg": "venue rejected order (order_stock returned %s)" % oid}
        # Fill is asynchronous; report submitted and let QmtBroker reconcile off
        # orders()/trades().
        res = {"ok": True, "order_id": str(oid), "status": "submitted",
               "filled_volume": 0, "filled_price": 0.0, "msg": ""}
        if coid:
            self._coid_results[coid] = res
        return res

    def _find_by_coid(self, coid: str):  # pragma: no cover - QMT box only
        """Return a result dict for a client_order_id already submitted, or None
        if the venue definitively has no such order (safe to submit).

        FAIL-CLOSED: if the dedup check itself can't be completed (query returns
        None / raises — the stale-session failure mode the heartbeat documents),
        RAISE rather than return None. Returning None would fall through to a
        fresh order_stock() and place a DUPLICATE real order on exactly the
        restart-retry scenario this dedup exists to prevent. A raised error
        propagates out as HTTP 500 → quanti's POST try/except treats the submit
        as 'unknown, retry later' — no duplicate, no silent miss.

        Fast path: this process's cache. Cross-restart: scan the venue's orders
        for one whose remark == coid (the venue is the durable dedup ledger)."""
        cached = self._coid_results.get(coid)
        if cached is not None:
            return cached
        with self._venue_lock:
            rows = self._trader.query_stock_orders(self._acc)
        if rows is None:
            raise RuntimeError(
                "xtdirect: dedup check failed (query_stock_orders returned None); "
                "refusing to submit to avoid a duplicate order")
        smap = _status_map()
        for o in rows:
            if str(getattr(o, "order_remark", "")) == coid:
                status = smap.get(getattr(o, "order_status", None), "accepted")
                # ok reflects the mapped status: a cancelled/rejected existing
                # order must NOT read as accepted (that would falsely count a
                # daily trade and briefly mirror it as pending).
                res = {"ok": status not in ("cancelled", "rejected"),
                       "order_id": str(getattr(o, "order_id", "")),
                       "status": status,
                       "filled_volume": int(getattr(o, "traded_volume", 0)),
                       "filled_price": float(getattr(o, "traded_price", 0.0)),
                       "msg": "dedup: existing order for client_order_id"}
                self._coid_results[coid] = res
                return res
        return None

    def cancel(self, body: dict) -> dict:
        order_id = str(body.get("order_id", ""))
        if not order_id:
            return {"ok": False, "msg": "missing order_id"}
        if not self._ensure():
            return {"ok": False, "msg": "trader not connected"}
        try:
            with self._venue_lock:
                rc = self._trader.cancel_order_stock(self._acc, int(order_id))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "msg": "cancel error: %s" % e}
        return {"ok": rc == 0, "msg": "cancel sent" if rc == 0 else
                "cancel rejected (rc=%s)" % rc}

    def orders(self) -> dict:
        if not self._ensure():
            raise RuntimeError("xtdirect: trader not connected (orders)")
        smap = _status_map()
        with self._venue_lock:
            rows = self._trader.query_stock_orders(self._acc)
        if rows is None:
            raise RuntimeError("xtdirect: query_stock_orders returned None")
        out = []
        for o in rows:
            otype = getattr(o, "order_type", None)
            out.append({
                "order_id": str(getattr(o, "order_id", "")),
                "code": _bare(o),
                "direction": "buy" if otype == xtconstant.STOCK_BUY else "sell",
                "volume": int(getattr(o, "order_volume", 0)),
                "price": float(getattr(o, "price", 0.0)),
                "status": smap.get(getattr(o, "order_status", None), "accepted"),
                "filled_volume": int(getattr(o, "traded_volume", 0)),
                "filled_price": float(getattr(o, "traded_price", 0.0)),
                "created_at": _fmt_ts(getattr(o, "order_time", 0)),
                # The client_order_id we stamped into the remark at submit — lets
                # QmtBroker reconcile 'submitting' rows back to their venue order.
                "client_order_id": str(getattr(o, "order_remark", "")),
            })
        self._touch()
        return {"orders": out}

    def trades(self) -> dict:
        if not self._ensure():
            raise RuntimeError("xtdirect: trader not connected (trades)")
        with self._venue_lock:
            rows = self._trader.query_stock_trades(self._acc)
        if rows is None:
            raise RuntimeError("xtdirect: query_stock_trades returned None")
        out = []
        for t in rows:
            otype = getattr(t, "order_type", None)
            out.append({
                "trade_id": str(getattr(t, "traded_id", "")),
                "order_id": str(getattr(t, "order_id", "")),
                "code": _bare(t),
                "direction": "buy" if otype == xtconstant.STOCK_BUY else "sell",
                "volume": int(getattr(t, "traded_volume", 0)),
                "price": float(getattr(t, "traded_price", 0.0)),
                "time": _fmt_ts(getattr(t, "traded_time", 0)),
            })
        return {"trades": out}

    # ------------------------------------------------------------ data (xtdata)
    def kline(self, code: str, start: str, end: str, period: str) -> dict:  # pragma: no cover
        if not _XTDATA_OK or not code:
            return {"code": code, "period": period, "bars": []}
        xt_code = self._xt_symbol(code)
        _xtdata.download_history_data(xt_code, period="1d", start_time=start,
                                      end_time=end)
        data = _xtdata.get_market_data_ex(
            ["open", "high", "low", "close", "volume", "amount"],
            [xt_code], period="1d", start_time=start, end_time=end,
            dividend_type="none")
        try:
            back = _xtdata.get_market_data_ex(
                ["close"], [xt_code], period="1d", start_time=start,
                end_time=end, dividend_type="back").get(xt_code)
        except Exception:  # noqa: BLE001 - factor optional; default 1.0
            back = None
        df = data.get(xt_code)
        bars = []
        if df is not None:
            for ts, row in df.iterrows():
                d = str(ts)[:8]
                raw_close = float(row["close"])
                factor = 1.0
                if back is not None and ts in back.index and raw_close > 0:
                    bc = float(back.loc[ts, "close"])
                    if bc > 0:
                        factor = bc / raw_close
                bars.append({
                    "date": "%s-%s-%s" % (d[:4], d[4:6], d[6:8]),
                    "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]), "close": raw_close,
                    "volume": float(row["volume"]), "amount": float(row["amount"]),
                    "adj_factor": factor})
        return {"code": code, "period": period, "bars": bars}

    def stock_list(self) -> dict:  # pragma: no cover
        if not _XTDATA_OK:
            return {"stocks": []}
        out = []
        for xt_code in _xtdata.get_stock_list_in_sector("沪深A股"):
            code = xt_code.split(".")[0]
            ex = "SH" if xt_code.endswith(("SH", "SSE")) else (
                "BJ" if xt_code.endswith("BJ") else "SZ")
            out.append({"code": code, "name": code, "exchange": ex})
        return {"stocks": out}

    def quote(self, code: str) -> dict:  # pragma: no cover
        if not _XTDATA_OK:
            return {"code": code, "last": 0.0}
        xt_code = self._xt_symbol(code)
        tick = (_xtdata.get_full_tick([xt_code]) or {}).get(xt_code, {})
        last = float(tick.get("lastPrice", 0) or 0)
        return {"code": code, "last": last,
                "open": float(tick.get("open", last) or last),
                "high": float(tick.get("high", last) or last),
                "low": float(tick.get("low", last) or last),
                "time": datetime.now().isoformat()}

    @staticmethod
    def _xt_symbol(code: str) -> str:
        suffix = "SH" if code.startswith(("6", "688", "689")) else (
            "BJ" if code.startswith(("8", "4", "920", "92")) else "SZ")
        return "%s.%s" % (code, suffix)


def _bare(obj) -> str:
    """Bare 6-digit code from an xttrader object's ``stock_code`` ('000001.SZ')."""
    return str(getattr(obj, "stock_code", "")).split(".")[0]


def _fmt_ts(ts) -> str:
    """Best-effort ISO string from an xttrader timestamp (epoch s or ms)."""
    try:
        v = int(ts)
    except (TypeError, ValueError):
        return str(ts) if ts else ""
    if v <= 0:
        return ""
    if v > 1_000_000_000_000:  # milliseconds
        v = v // 1000
    try:
        return datetime.fromtimestamp(v).isoformat()
    except (ValueError, OSError, OverflowError):
        return str(ts)
