"""vnpy-backed live implementation for the qmt-bridge (Option B).

Instead of hand-rolling the xtquant order state machine / async-fill callbacks /
reconnection inside the bridge, we drive **VeighNa (vnpy)**'s mature QMT gateway
headless (no GUI) and translate its event objects into the bridge's existing
HTTP-contract dicts. The quanti side (QmtBroker, XtdataAdapter, the HTTP
contract) is unchanged — only the bridge's *backend* changes.

    QMT mini client (logged in)
        │ xtquant
    vnpy MainEngine + vnpy_xt XtGateway  ──events──▶  VnpyBackend
                                                          │ dict (bridge contract)
                                                      qmt_bridge HTTP ──▶ quanti

Split of responsibilities:
  * Trading (asset / positions / order / cancel / orders / trades) → vnpy
    gateway: orders submit synchronously and **fill asynchronously** via
    EVENT_TRADE/EVENT_ORDER, which we accumulate; the bridge then exposes the
    reconciled state (QmtBroker.try_fill_pending_orders already expects exactly
    this submit-then-reconcile flow).
  * Market data (kline / stock_list / quote) → xtdata directly (vnpy_xt's
    datafeed is itself a thin xtdata wrapper; no need to route data through the
    gateway).

NOT verifiable off the QMT box: vnpy + vnpy_xt + xtquant + a logged-in miniQMT
client are Windows-only. Imports are guarded, so this module loads anywhere
(``available()`` is False without them) and the bridge falls back to mock mode.
Bits that depend on the exact gateway build are marked ``# VERIFY``.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

logger = logging.getLogger("qmt-bridge.vnpy")

# --- vnpy core + QMT gateway (Windows/QMT box only) ---------------------------
try:  # pragma: no cover - QMT box only
    from vnpy.event import EventEngine
    from vnpy.trader.constant import (
        Direction, Exchange, Offset, OrderType, Status,
    )
    from vnpy.trader.engine import MainEngine
    from vnpy.trader.event import (
        EVENT_ACCOUNT, EVENT_ORDER, EVENT_POSITION, EVENT_TRADE,
    )
    from vnpy.trader.object import CancelRequest, OrderRequest

    _VNPY_OK = True
except Exception:  # noqa: BLE001 - not on the QMT box
    _VNPY_OK = False

try:  # pragma: no cover - official 迅投 gateway, else community fallback
    from vnpy_xt import XtGateway as _XtGateway  # type: ignore
    _XT_OK = True
except Exception:  # noqa: BLE001
    try:
        from vnpy_qmt import QmtGateway as _XtGateway  # type: ignore
        _XT_OK = True
    except Exception:  # noqa: BLE001
        _XtGateway = None
        _XT_OK = False

try:  # pragma: no cover - xtdata ships with QMT
    import xtquant.xtdata as _xtdata  # type: ignore
    _XTDATA_OK = True
except Exception:  # noqa: BLE001
    _xtdata = None
    _XTDATA_OK = False


def _exchange_of(code: str):  # pragma: no cover - needs vnpy enums
    """A-share code → vnpy Exchange. 6xx/688/689 → SSE; 8x/4x/920 → BSE; else SZSE."""
    if code.startswith(("688", "689", "6")):
        return Exchange.SSE
    if code.startswith(("8", "4", "920", "92")):
        return Exchange.BSE
    return Exchange.SZSE


def _bridge_status(status) -> str:  # pragma: no cover - needs vnpy Status
    """Map vnpy Status → the bridge's order-status vocabulary."""
    return {
        Status.SUBMITTING: "accepted",
        Status.NOTTRADED: "accepted",
        Status.PARTTRADED: "partial",
        Status.ALLTRADED: "filled",
        Status.CANCELLED: "cancelled",
        Status.REJECTED: "rejected",
    }.get(status, "accepted")


class VnpyBackend:
    """Drives a headless vnpy QMT gateway and exposes the bridge contract.

    `setting` is the vnpy gateway connect dict (账户ID / userdata_mini 路径 / …);
    its exact keys depend on the gateway build — see vnpy_xt docs and fill in on
    the box. All event handlers run on vnpy's EventEngine thread, so shared state
    is guarded by a lock.
    """

    GATEWAY_NAME = "XT"

    @staticmethod
    def available() -> bool:
        """True only where vnpy + QMT gateway + xtdata are all importable (the
        QMT box). xtdata is required too: without it positions()/quote() can't
        mark to realtime and would silently fall back to cost basis (stops never
        fire), so we degrade to mock — which reads as NOT connected (G1)."""
        return _VNPY_OK and _XT_OK and _XTDATA_OK

    def __init__(self, setting: dict, gateway_name: str = GATEWAY_NAME) -> None:
        self._setting = setting or {}
        self._gw = gateway_name
        self._lock = threading.RLock()
        self._accounts: dict = {}
        self._positions: dict = {}   # vt_positionid → PositionData
        self._orders: dict = {}      # vt_orderid → OrderData
        self._trades: dict = {}      # vt_tradeid → TradeData
        self._engine = None
        self._main = None
        self.connected = False
        # Monotonic timestamp of the last gateway event. `connected` alone was a
        # sticky bool that never flipped false on a mid-session disconnect; the
        # gateway pushes account/position events continuously while live, so a
        # stalled clock here means the feed is dead even if `connected` is True.
        self._last_event_at: float | None = None

    def _touch(self) -> None:
        self._last_event_at = time.monotonic()

    def data_fresh(self, max_age: float = 30.0) -> bool:
        """True iff a gateway event arrived within `max_age` seconds — the live
        liveness signal `connected` can't give (it never flips on disconnect).
        Best-effort: verify the real cadence on the QMT box (# VERIFY, H2)."""
        with self._lock:
            ts = self._last_event_at
        return ts is not None and (time.monotonic() - ts) < max_age

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:  # pragma: no cover - QMT box only
        if not self.available():
            raise RuntimeError(
                "vnpy / vnpy_xt not installed — cannot start the live backend")
        self._engine = EventEngine()
        self._main = MainEngine(self._engine)
        self._main.add_gateway(_XtGateway, self._gw)
        self._engine.register(EVENT_ACCOUNT, self._on_account)
        self._engine.register(EVENT_POSITION, self._on_position)
        self._engine.register(EVENT_ORDER, self._on_order)
        self._engine.register(EVENT_TRADE, self._on_trade)
        # VERIFY: connect-setting keys are gateway-specific (account id, the
        # userdata_mini path, session id). See vnpy_xt docs.
        self._main.connect(self._setting, self._gw)
        self.connected = True
        logger.info("vnpy backend connected via gateway %s", self._gw)

    def close(self) -> None:  # pragma: no cover
        if self._main is not None:
            self._main.close()
        self.connected = False

    # ------------------------------------------------------------ handlers
    def _on_account(self, event) -> None:  # pragma: no cover
        with self._lock:
            self._accounts[event.data.accountid] = event.data
            self._touch()

    def _on_position(self, event) -> None:  # pragma: no cover
        with self._lock:
            self._positions[event.data.vt_positionid] = event.data
            self._touch()

    def _on_order(self, event) -> None:  # pragma: no cover
        with self._lock:
            self._orders[event.data.vt_orderid] = event.data
            self._touch()

    def _on_trade(self, event) -> None:  # pragma: no cover
        with self._lock:
            self._trades[event.data.vt_tradeid] = event.data
            self._touch()

    # ------------------------------------------------------------ trading
    def asset(self) -> dict:  # pragma: no cover - QMT box only
        with self._lock:
            accts = list(self._accounts.values())
        cash = sum(getattr(a, "available", 0.0) for a in accts)
        frozen = sum(getattr(a, "frozen", 0.0) for a in accts)
        total = sum(getattr(a, "balance", 0.0) for a in accts)
        return {"cash": round(cash, 2), "frozen_cash": round(frozen, 2),
                "market_value": round(total - cash - frozen, 2),
                "total_asset": round(total, 2)}

    def positions(self) -> dict:  # pragma: no cover - QMT box only
        out = []
        with self._lock:
            poss = [p for p in self._positions.values()
                    if int(getattr(p, "volume", 0)) > 0]
        # Batch the realtime last prices so market_value / current_price track
        # the live quote, not cost. Reporting cost-based market_value made
        # QmtBroker reverse-derive current_price ≡ avg_cost → pnl always 0 →
        # the per-stock stop-loss never fired on real money (audit C5).
        last_by_code: dict[str, float] = {}
        if _XTDATA_OK and poss:
            try:
                xt_codes = [self._xt_symbol(p.symbol) for p in poss]
                ticks = _xtdata.get_full_tick(xt_codes) or {}
                for p in poss:
                    t = ticks.get(self._xt_symbol(p.symbol), {}) or {}
                    last_by_code[p.symbol] = float(t.get("lastPrice", 0) or 0)
            except Exception:  # noqa: BLE001 - fall back to cost on quote failure
                pass
        for p in poss:
            vol = int(getattr(p, "volume", 0))
            avg = float(getattr(p, "price", 0.0))
            # yd_volume = yesterday's holding = the T+1-sellable quantity.
            sellable = int(getattr(p, "yd_volume", 0))
            last = last_by_code.get(p.symbol, 0.0)
            cur = last if last > 0 else avg
            out.append({
                "code": p.symbol, "volume": vol,
                "can_use_volume": sellable, "avg_price": avg,
                "last_price": round(cur, 3),
                "market_value": round(vol * cur, 2)})
        return {"positions": out}

    def submit_order(self, body: dict) -> dict:  # pragma: no cover - QMT box only
        code = str(body.get("code", ""))
        direction = str(body.get("direction", "")).lower()
        volume = int(body.get("volume", 0))
        price = float(body.get("price", 0) or 0)
        if not code or direction not in ("buy", "sell") or volume <= 0:
            return {"ok": False, "order_id": "", "status": "rejected",
                    "msg": "bad order: need code, direction(buy/sell), volume>0"}
        # A-share spot: buy = open long, sell = close long. VERIFY offset
        # semantics against the gateway (some treat stock as Offset.NONE).
        req = OrderRequest(
            symbol=code, exchange=_exchange_of(code),
            direction=Direction.LONG if direction == "buy" else Direction.SHORT,
            offset=Offset.OPEN if direction == "buy" else Offset.CLOSE,
            type=OrderType.LIMIT if price else OrderType.MARKET,
            volume=volume, price=price, reference="quanti")
        vt_orderid = self._main.send_order(req, self._gw)
        if not vt_orderid:
            return {"ok": False, "order_id": "", "status": "rejected",
                    "msg": "gateway rejected order"}
        # Fill arrives asynchronously via EVENT_TRADE/EVENT_ORDER; the bridge
        # reports it submitted now and QmtBroker.try_fill_pending_orders
        # reconciles it to filled later off orders()/trades().
        return {"ok": True, "order_id": vt_orderid, "status": "submitted",
                "filled_volume": 0, "filled_price": 0.0, "msg": ""}

    def cancel(self, body: dict) -> dict:  # pragma: no cover - QMT box only
        order_id = str(body.get("order_id", ""))
        with self._lock:
            order = self._orders.get(order_id)
        if order is None:
            return {"ok": False, "msg": "unknown order id"}
        req = CancelRequest(orderid=order.orderid, symbol=order.symbol,
                            exchange=order.exchange)
        self._main.cancel_order(req, self._gw)
        return {"ok": True, "msg": "cancel sent"}

    def orders(self) -> dict:  # pragma: no cover - QMT box only
        with self._lock:
            orders = list(self._orders.values())
        return {"orders": [{
            "order_id": o.vt_orderid, "code": o.symbol,
            "direction": "buy" if o.direction == Direction.LONG else "sell",
            "volume": int(o.volume), "price": float(o.price),
            "status": _bridge_status(o.status),
            "filled_volume": int(getattr(o, "traded", 0)),
            "filled_price": float(getattr(o, "price", 0.0)),
            "created_at": getattr(o, "datetime", None).isoformat()
            if getattr(o, "datetime", None) else "",
        } for o in orders]}

    def trades(self) -> dict:  # pragma: no cover - QMT box only
        with self._lock:
            trades = list(self._trades.values())
        return {"trades": [{
            "trade_id": t.vt_tradeid, "order_id": t.vt_orderid, "code": t.symbol,
            "direction": "buy" if t.direction == Direction.LONG else "sell",
            "volume": int(t.volume), "price": float(t.price),
            "time": getattr(t, "datetime", None).isoformat()
            if getattr(t, "datetime", None) else "",
        } for t in trades]}

    # ------------------------------------------------------------ data (xtdata)
    def kline(self, code: str, start: str, end: str, period: str) -> dict:  # pragma: no cover
        if not _XTDATA_OK or not code:
            return {"code": code, "period": period, "bars": []}
        xt_code = self._xt_symbol(code)
        # VERIFY field/period names against the installed xtdata build.
        _xtdata.download_history_data(xt_code, period="1d", start_time=start,
                                      end_time=end)
        # RAW (不复权) OHLCV.
        data = _xtdata.get_market_data_ex(
            ["open", "high", "low", "close", "volume", "amount"],
            [xt_code], period="1d", start_time=start, end_time=end,
            dividend_type="none")
        # Back-adjusted (hfq) close, for adj_factor = back_close / raw_close.
        # # VERIFY 'back' is listing-anchored (window-independent) on the
        # installed build, else use xtdata.get_divid_factors.
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
                d = str(ts)[:8]  # 'YYYYMMDD'
                raw_close = float(row["close"])
                factor = 1.0
                if back is not None and ts in back.index and raw_close > 0:
                    bc = float(back.loc[ts, "close"])
                    if bc > 0:
                        factor = bc / raw_close
                bars.append({
                    "date": f"{d[:4]}-{d[4:6]}-{d[6:8]}",
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
            ex = "SH" if xt_code.endswith(("SH", "SSE")) else "SZ"
            out.append({"code": code, "name": code, "exchange": ex})
        return {"stocks": out}

    def quote(self, code: str) -> dict:  # pragma: no cover
        if not _XTDATA_OK:
            return {"code": code, "last": 0.0}
        xt_code = self._xt_symbol(code)
        tick = (_xtdata.get_full_tick([xt_code]) or {}).get(xt_code, {})
        last = float(tick.get("lastPrice", 0) or 0)
        return {"code": code, "last": last, "open": float(tick.get("open", last) or last),
                "high": float(tick.get("high", last) or last),
                "low": float(tick.get("low", last) or last),
                "time": datetime.now().isoformat()}

    @staticmethod
    def _xt_symbol(code: str) -> str:  # pragma: no cover
        suffix = "SH" if code.startswith(("6", "688", "689")) else (
            "BJ" if code.startswith(("8", "4", "920", "92")) else "SZ")
        return f"{code}.{suffix}"
