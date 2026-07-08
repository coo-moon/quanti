"""QmtBroker — live A-share execution via qmt-bridge (miniQMT / xtquant).

Implements the same :class:`quanti.execution.base.Broker` Protocol as
``PaperBroker`` so the agent runtime switches venues without changing the
decision/risk pipeline. The difference is where truth lives:

  * **Orders** hit a real venue through the localhost ``qmt-bridge`` (see
    ``bridge/qmt_bridge.py``), not a simulated fill engine.
  * **Cash / positions** come from the broker account (``/trader/asset`` +
    ``/trader/positions``), reconciled — the local DB is a *mirror*, the broker
    is the source of truth (live-trading red line).

The ``RiskManager`` hard limits still run **before** every submit — the same
floor PaperBroker enforces; a live order can't bypass it.

Order timing (A 股 has no night session): a signal decided off-hours — e.g. the
16:00 daily agent cycle — CANNOT submit (it would be a guaranteed 废单), so it
QUEUES as a local pending order. The in-session intraday guard's
``try_fill_pending_orders`` then submits it at the next session's open, matching
the paper/backtest next-open fill model so the three chains agree. In-session
signals (e.g. a stop-loss the guard raises) submit to the venue immediately.

Skeleton status (phase ② → ③): this talks to the bridge over the agreed HTTP
contract and is fully exercised against the bridge's *mock* gateway in tests.
The parts that need the real venue are marked ``# NOTE``:
  - fills are reconciled by polling ``/trader/orders`` + ``/trader/trades``;
    the real venue pushes them asynchronously (callbacks) — phase ③ wires the
    bridge to accumulate callbacks and this poller just reads them.
  - sizing is the simple cash%/risk-cap rule (no Sizer/commission yet).
  - ``check_exits`` is stop-loss-only for now (trailing TP + strategy-exit
    replay land with the intraday loop in phase ⑤).
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from typing import Callable, TYPE_CHECKING

from quanti.bridge_client import BridgeClient, DEFAULT_BRIDGE_URL, HttpBridgeClient
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.base import BrokerResult, PendingFillResult
from quanti.execution.exits import (
    compute_atr_ratios, compute_peaks, compute_strategy_exits, load_strategies)
from quanti.models import Direction, Portfolio, Position, PriceType, Signal
from quanti.risk.manager import (
    DRIFT_TRIM_STRATEGY,
    RiskConfig,
    RiskManager,
    risk_config_from_dict,
)
from quanti.risk.sizer import compute_buy_target_value
from quanti.utils.market import (
    board_limit_pct, count_trading_days_between, in_trading_session,
    lot_round_strength, order_decision_date, prev_bar_close,
    session_closed_for_day)

if TYPE_CHECKING:
    from quanti.risk.protections import ProtectionConfig

logger = logging.getLogger(__name__)

# Exits that MUST get filled: RiskManager exits (stop-loss / strategy / trailing
# TP, all tagged 'risk_exit') and the circuit-breaker / kill-switch flatten
# ('kill_switch'). These submit at the limit-down price; normal SELLs don't.
_FORCED_EXIT_STRATEGIES = frozenset({"risk_exit", "kill_switch"})


class QmtBroker:
    """Live broker over qmt-bridge. Structurally implements ``Broker``."""

    def __init__(
        self,
        db: Database,
        provider: DataProvider,
        *,
        client: BridgeClient | None = None,
        bridge_url: str = DEFAULT_BRIDGE_URL,
        initial_cash: float = 1_000_000.0,
        risk_config: RiskConfig | None = None,
        protection_config: ProtectionConfig | None = None,
        slippage: float = 0.001,
        strategies_dir: str = "strategies",
        require_live: bool = False,
        pending_ttl_trading_days: int = 3,
        session_fn: "Callable[[], bool] | None" = None,
    ) -> None:
        self._db = db
        self._provider = provider
        self._client: BridgeClient = client or HttpBridgeClient(bridge_url)
        self._initial_cash = initial_cash
        # When True (real money), a silent mock fallback must read as NOT
        # connected and orders must not submit (audit G1). Default False keeps
        # dev/mock/tests working.
        self._require_live = require_live
        self._risk = RiskManager(risk_config)
        from quanti.risk.protections import ProtectionConfig, ProtectionManager
        self._protections = ProtectionManager(
            protection_config if protection_config is not None
            else ProtectionConfig())
        self._slippage = slippage
        self._strategies_dir = strategies_dir
        self._pending_ttl_days = pending_ttl_trading_days
        # In-session gate: off-hours signals queue (A 股 no night session),
        # in-session ones submit now. Injectable so tests are wall-clock free.
        self._session_fn = session_fn
        self._strategy_cache: dict | None = None  # name → strategy class

    # ----------------------------------------------------------- health
    def is_connected(self) -> bool:
        """True iff the bridge is reachable and reports ok. With ``require_live``
        (real money), ALSO require real ``vnpy`` mode + a connected trader: a
        silent mock fallback (xtquant failed to import on the QMT box) must read
        as NOT connected, else mock 'fills' get mirrored as real trades (G1)."""
        try:
            h = self._client.get("/health")
        except Exception as e:  # noqa: BLE001 - unreachable bridge == down
            logger.warning("qmt-bridge unreachable: %s", e)
            return False
        if not h.get("ok"):
            return False
        if self._require_live:
            # Real money: require a real (non-mock) backend + a connected trader
            # AND a fresh datafeed. Accept either live backend — "vnpy" (vnpy_xt
            # gateway) or "xt" (direct xtquant); only "mock" must read as down, so
            # a silent mock fallback never gets mirrored as real fills (G1).
            # `trader_connected` alone was a sticky bool that never flipped false
            # on a mid-session disconnect, so the guard would keep firing orders
            # into a dead gateway; the bridge now reports datafeed_ok from recent
            # events (H2). A bridge predating this field omits it → default True so
            # we don't regress older bridges.
            return (h.get("mode") in ("vnpy", "xt")
                    and bool(h.get("trader_connected"))
                    and h.get("datafeed_ok", True) is not False)
        return True

    def _order_price(self, code: str, price: float) -> float:
        """Round to the A-share tick (0.01) and clamp into today's price-limit
        band, so the venue can't reject an illegal/over-limit price (audit G4)."""
        p = round(float(price), 2)
        # RAW prev close: the live order price is a raw (不复权) quote, so the
        # limit band MUST be raw too. The hfq default clamps a dividend/split
        # stock's raw order outside the ±10% cage → venue 废单s every order,
        # incl. the forced stop-loss/flatten floor below (H1).
        pc = prev_bar_close(self._provider, code, date.today(), adjust="none")
        if pc and pc > 0:
            lim = board_limit_pct(code)
            p = min(max(p, round(pc * (1 - lim), 2)), round(pc * (1 + lim), 2))
        return p

    # ------------------------------------------------------- reconciliation
    def _reconciled_portfolio(
            self) -> tuple[Portfolio, dict[str, int], set[str]]:
        """Build a Portfolio from the *broker* account — the source of truth —
        plus a per-code sellable (T+1 `can_use_volume`) map and a set of codes
        whose realtime price is stale/unavailable. A lot bought today is frozen,
        so SELLs must be capped at the sellable amount, not the total held;
        callers read the second element for that.

        The third element (``stale``) matters for exits: in live, a code with no
        realtime quote falls back to cost basis (pnl≡0) which would silently
        disable its stop-loss (audit C5). `check_exits` skips + alerts on those
        instead of evaluating a fabricated price."""
        asset = self._client.get("/trader/asset")
        rows = self._client.get("/trader/positions").get("positions", [])
        pf = Portfolio(cash=float(asset.get("cash", 0.0)))
        sellable: dict[str, int] = {}
        stale: set[str] = set()
        for p in rows:
            vol = int(p.get("volume", 0))
            if vol <= 0:
                continue
            avg = float(p.get("avg_price", 0.0))
            cur = self._current_price(p, vol, avg)
            if self._realtime_stale(p):
                stale.add(p["code"])
            stock = self._db.get_stock(p["code"])
            pf.positions[p["code"]] = Position(
                stock_code=p["code"], quantity=vol, avg_cost=avg,
                current_price=cur, buy_date=None,
                industry=stock.industry if stock else "")
            sellable[p["code"]] = int(p.get("can_use_volume", 0))
        return pf, sellable, stale

    def _realtime_stale(self, p: dict) -> bool:
        """True when a live position has NO realtime price (feed down / 停牌 /
        thread stall) so its current_price fell back to cost basis. Only flagged
        under require_live — dev/mock/paper price off stored bars by design."""
        if not self._require_live:
            return False
        last = float(p.get("last_price", 0) or 0)
        mv = float(p.get("market_value", 0) or 0)
        return last <= 0 and mv <= 0

    @staticmethod
    def _current_price(p: dict, vol: int, avg: float) -> float:
        """Current per-share price for a venue position row. Prefer the live
        last price; fall back to market_value/vol, then cost. Reverse-deriving
        from a cost-based market_value used to pin current_price ≡ avg → pnl 0
        → stop-loss never fired (audit C5)."""
        last = float(p.get("last_price", 0) or 0)
        if last > 0:
            return last
        mv = float(p.get("market_value", 0) or 0)
        if mv > 0 and vol:
            return mv / vol
        return avg

    def snapshot_portfolio(self) -> dict:
        """Reconciled cash/positions/pnl from the broker, in the same shape
        PaperBroker returns so the UI/agent are venue-agnostic."""
        asset = self._client.get("/trader/asset")
        rows = self._client.get("/trader/positions").get("positions", [])
        cash = float(asset.get("cash", 0.0))
        market_value = float(asset.get("market_value", 0.0))
        total = float(asset.get("total_asset", cash + market_value))
        enriched: list[dict] = []
        for p in rows:
            vol = int(p.get("volume", 0))
            if vol <= 0:
                continue
            avg = float(p.get("avg_price", 0.0))
            cur = self._current_price(p, vol, avg)
            stock = self._db.get_stock(p["code"])
            enriched.append({
                "code": p["code"],
                "name": stock.name if stock else p["code"],
                "industry": stock.industry if stock else "",
                "quantity": vol, "avg_cost": avg, "current_price": cur,
                "price_date": None, "market_value": cur * vol,
                "sellable": int(p.get("can_use_volume", 0)),  # T+1-free qty
                "pnl": (cur - avg) * vol,
                "pnl_pct": (cur - avg) / avg if avg else 0.0,
            })
        # Persist the equity snapshot (UI equity curve / attribution) only once
        # today's marks are final. An intraday realtime mark is transient: if
        # the process dies before the close overwrite, a phantom peak would
        # fossilize into the day's row. The circuit-breaker HWM rides the
        # monotone portfolio_hwm row instead (enforce_portfolio_stop), so it
        # still sees intraday peaks.
        if session_closed_for_day(datetime.now(), self._provider):
            self._db.save_portfolio_snapshot(date.today(), cash, market_value,
                                             total)
        return {
            "cash": cash, "initial_cash": self._initial_cash,
            "market_value": market_value, "total_value": total,
            "pnl": total - self._initial_cash,
            "pnl_pct": (total - self._initial_cash) / self._initial_cash
                       if self._initial_cash else 0.0,
            "positions": enriched,
            "snapshot_date": date.today().isoformat(),
        }

    # ------------------------------------------------------------ execution
    def execute_signal(self, signal: Signal, strategy_name: str = "") -> bool:
        """Submit one signal. Returns True if it landed (venue accepted it).
        Use `execute_signals` when you need the filled/pending breakdown."""
        landed, _status, _reason = self._submit_signal(signal, strategy_name)
        return landed

    def execute_signals(self, signals: list[Signal],
                        strategy_name: str = "") -> BrokerResult:
        out = BrokerResult()
        for s in signals:
            out.accepted += 1
            landed, status, reason = self._submit_signal(s, strategy_name)
            if not landed:
                out.rejected += 1
                if reason:
                    out.reasons.append(reason)
            elif status == "filled":
                out.filled += 1
            else:  # accepted but resting (live limit not yet filled)
                out.pending += 1
        return out

    def _entry_allowed(self, signal: Signal,
                       portfolio) -> tuple[bool, str, str]:
        """Risk caps + protections gate via the shared helper. Returns
        (ok, reason, reject_kind). Protections only gate BUY."""
        from quanti.risk.protection_context import evaluate_entry
        return evaluate_entry(self._risk, self._protections, self._db,
                              self._provider, signal, portfolio)

    def _submit_signal(self, signal: Signal,
                       strategy_name: str) -> tuple[bool, str, str]:
        """Route one signal: outside a trading session it queues locally
        (A 股无夜市委托 — an off-hours submit is a guaranteed 废单, so the
        16:00 agent cycle's orders must wait for the next session's open,
        matching the paper/backtest next-open fill semantics); in-session it
        submits to the venue now. Returns (landed, status, reason)."""
        if not self._in_session():
            return self._queue_overnight(signal, strategy_name)
        return self._send_now(signal, strategy_name)

    def _in_session(self) -> bool:
        """Whether the market is open right now (injectable for tests)."""
        if self._session_fn is not None:
            return self._session_fn()
        return in_trading_session(datetime.now(), self._provider)

    def _queue_overnight(self, signal: Signal,
                         strategy_name: str) -> tuple[bool, str, str]:
        """Park an off-hours signal as a local pending order (no venue call).
        `try_fill_pending_orders` (intraday guard / next cycle) submits it in
        the next session. Sizing/risk run at submit time against that day's
        reconciled account — not against tonight's stale state. Dedup mirrors
        PaperBroker: one queued order per (code, direction)."""
        for o in self._db.list_orders(limit=500, status="pending"):
            if (o["code"] == signal.stock_code
                    and o["direction"] == signal.direction.value
                    and not (o.get("entry_strategy") or "")):
                return False, "rejected", "duplicate queued order"
        order_id = self._mirror_order(signal, strategy_name, status="pending",
                                      reason=signal.reason)
        self._db.log_decision(
            "order_queued",
            f"排队(实盘) {signal.direction.value} {signal.stock_code} "
            f"(待下一交易时段开盘提交)",
            code=signal.stock_code,
            details={"venue": "qmt", "order_id": order_id,
                     "strategy": strategy_name,
                     "signal_reason": signal.reason,
                     "queued_strength": signal.strength})
        return True, "pending", ""

    def _send_now(self, signal: Signal, strategy_name: str,
                  queued_order_id: str | None = None) -> tuple[bool, str, str]:
        """Risk-check → size → submit. Returns (landed, status, reason) with
        status in {'filled','pending','rejected'}.

        RiskManager runs BEFORE every submit (red line). SELL volume is capped
        at the T+1-sellable quantity (`can_use_volume`) so a lot bought today
        is never over-sold — a frozen position is skipped, not submitted.

        With `queued_order_id`, the submit outcome updates that overnight-
        queued row instead of inserting a fresh mirror row."""
        # Live red line: never submit (nor mirror as filled) against a bridge
        # that isn't truly live — a mock fallback would book phantom real
        # trades (audit G1). No-op when require_live is off (dev/mock/tests).
        if self._require_live and not self.is_connected():
            if queued_order_id:
                # Transient: keep the queued row pending, retry next guard tick.
                return False, "pending", "bridge not live"
            self._mirror_order(signal, strategy_name, status="rejected",
                               reason="bridge not live (mock fallback?)")
            self._db.log_decision(
                "broker_not_live",
                f"拒单(实盘):bridge 非真实连接(疑似 mock 回退) "
                f"{signal.direction.value} {signal.stock_code}",
                code=signal.stock_code, details={"venue": "qmt"})
            return False, "rejected", "bridge not live"

        portfolio, sellable, _ = self._reconciled_portfolio()
        ok, reason, kind = self._entry_allowed(signal, portfolio)
        if not ok:
            self._mirror_order(signal, strategy_name, status="rejected",
                               reason=reason, reuse_order_id=queued_order_id)
            self._db.log_decision(
                kind,
                f"{'风控' if kind == 'risk_reject' else '保护层'}拒绝(实盘) "
                f"{signal.direction.value} {signal.stock_code}: {reason}",
                code=signal.stock_code,
                details={"venue": "qmt", "signal_reason": signal.reason})
            return False, "rejected", reason

        if signal.direction == Direction.BUY:
            volume, price = self._size_buy(signal, portfolio)
            if volume < 100:
                r = "cash/position cap too tight"
                self._mirror_order(signal, strategy_name, status="rejected",
                                   reason=r, reuse_order_id=queued_order_id)
                return False, "rejected", r
        else:  # SELL — only the T+1-sellable portion can leave today
            pos = portfolio.positions.get(signal.stock_code)
            if pos is None or pos.quantity <= 0:
                self._mirror_order(signal, strategy_name, status="rejected",
                                   reason="no position",
                                   reuse_order_id=queued_order_id)
                return False, "rejected", "no position"
            can_sell = min(pos.quantity, sellable.get(signal.stock_code, 0))
            if can_sell <= 0:
                r = "T+1: no sellable quantity today"
                self._mirror_order(signal, strategy_name, status="rejected",
                                   reason=r, reuse_order_id=queued_order_id)
                self._db.log_decision(
                    "order_skipped_t1",
                    f"跳过(实盘) 卖出 {signal.stock_code}: 当日买入 T+1 不可卖",
                    code=signal.stock_code, details={"venue": "qmt"})
                return False, "rejected", r
            # Partial-sell: ONLY the concentration trim (削峰) sells a fraction.
            # Every other SELL (stop/TP/strategy/flatten/manual) fully exits
            # can_sell — including odd lots (1–99 shares from dividends/rights),
            # which must be flattenable. A sub-lot trim is a silent no-op (don't
            # churn a 'rejected' mirror every cycle).
            if strategy_name == DRIFT_TRIM_STRATEGY:
                volume = lot_round_strength(can_sell, signal.strength)
                if volume < 100:
                    if queued_order_id:  # don't leave the queued row pending
                        self._db.update_order_status(
                            queued_order_id, "rejected",
                            reason="trim below one lot (no-op)")
                    return False, "rejected", "trim below one lot (no-op)"
            else:
                volume = can_sell
            if strategy_name in _FORCED_EXIT_STRATEGIES:
                # Forced exit (stop-loss / circuit-breaker flatten) MUST get out.
                # A normal limit near last price won't fill on a fast drop or a
                # 跌停 print — it rests behind the queue. Submit at the day's
                # limit-down price instead: a SELL limit fills at any price ≥ its
                # ask, so the floor crosses any available bid and earns time
                # priority in the call/continuous auction; if 跌停封死 with no
                # bid it still can't fill, but now it's queued first instead of
                # unfillably high. ponytail: feed _order_price a floor sentinel
                # (0.01) and reuse its band-clamp to land exactly on 跌停价 —
                # don't key off the live quote, which may be stale/disconnected.
                price = self._order_price(signal.stock_code, 0.01)
            else:
                # Price off the live quote (like BUY via _latest_price), not the
                # position's current_price — which, before C5, was the cost basis.
                # Falls back to the reconciled current_price if the quote is down.
                last = self._latest_price(signal.stock_code)
                ref = last if last > 0 else pos.current_price
                price = ref * (1 - self._slippage)

        res = self._client.post("/trader/order", {
            "code": signal.stock_code, "direction": signal.direction.value,
            "volume": int(volume),
            "price": self._order_price(signal.stock_code, price),
            "price_type": "limit" if price else "market"})
        accepted = bool(res.get("ok"))
        filled = accepted and res.get("status") == "filled"
        status = "filled" if filled else ("pending" if accepted else "rejected")
        if accepted:
            # Count the order against the daily-trade hard cap — the same floor
            # PaperBroker enforces (paper_broker records on fill). reset_daily()
            # at session start + seeding the count from /trader/trades is phase-③.
            self._risk.record_trade(signal.direction)
        venue_msg = res.get("msg", "")
        # Audit: the decision reason (signal.reason — e.g. 策略离场信号) leads and
        # is never clobbered by the venue message; venue text is appended for
        # troubleshooting. Pre-fix a non-empty msg OVERWROTE signal.reason, so a
        # filled exit's orders row lost which rule (止损/策略离场/止盈) fired.
        self._mirror_order(
            signal, strategy_name, status=status,
            reason=" | ".join(p for p in (signal.reason, venue_msg) if p),
            venue_order_id=res.get("order_id", ""),
            filled_price=float(res.get("filled_price", 0) or 0),
            filled_quantity=int(res.get("filled_volume", 0) or 0),
            quantity=int(volume))
        self._db.log_decision(
            "order_submitted" if accepted else "order_rejected",
            f"{'已报' if accepted else '废单'} {signal.direction.value} "
            f"{signal.stock_code} {volume}股 @ {price:.2f} (实盘)",
            code=signal.stock_code,
            details={"venue": "qmt", "venue_order_id": res.get("order_id", ""),
                     "status": res.get("status"), "strategy": strategy_name,
                     "signal_reason": signal.reason, "msg": venue_msg})
        return accepted, status, (res.get("msg", "") or "")

    def _size_buy(self, signal: Signal, portfolio: Portfolio) -> tuple[int, float]:
        """Simple cash%/risk-cap sizing against *broker* cash. Price from the
        bridge realtime quote, falling back to the latest stored close."""
        price = self._latest_price(signal.stock_code)
        if price <= 0:
            return 0, 0.0
        cash = portfolio.cash
        stock = self._db.get_stock(signal.stock_code)
        # Post-trade single-stock + industry + total caps, centralized in
        # RiskManager so paper/live/backtest can't drift apart.
        room = self._risk.max_additional_buy_value(
            portfolio, signal.stock_code, stock.industry if stock else "")
        # Shared sizing helper (same as paper + backtest) so the paths agree.
        target_value = compute_buy_target_value(
            cash=cash, total_value=portfolio.total_value,
            strength=signal.strength, size_cap=room, code=signal.stock_code)
        lots = int(target_value / (price * 100)) if price > 0 else 0
        return lots * 100, price * (1 + self._slippage)

    def _latest_price(self, code: str) -> float:
        try:
            q = self._client.get("/data/quote", {"code": code})
            last = float(q.get("last", 0) or 0)
            if last > 0:
                return last
        except Exception:  # noqa: BLE001 - fall back below
            pass
        # Live must price on the realtime (xtdata-via-bridge) quote ONLY — never
        # stale tushare daily. No realtime quote → 0.0; downstream prices it as a
        # market order (real execution), not a stale limit.
        if self._require_live:
            return 0.0
        # Paper/dev: RAW (不复权) close is a fine stand-in tradable price.
        bars = self._provider.get_daily_bars(
            code, date(2000, 1, 1), date.today(), adjust="none")
        return float(bars[-1].close) if bars else 0.0

    # ------------------------------------------------ pending / reconcile
    def try_fill_pending_orders(self) -> PendingFillResult:
        """Advance the local pending queue. Two kinds of rows coexist:

          1. Un-submitted overnight orders (entry_strategy empty): queued by
             `_queue_overnight` when the 16:00 agent cycle ran off-hours (A 股
             no night session — an off-hours submit is a guaranteed 废单).
             Submit them NOW at the session open (matching paper/backtest
             next-open semantics), applying TTL + the extreme-gap-up guard.
          2. Already-submitted orders (entry_strategy = venue id): reconcile
             their fill status against the venue order book.

        Only submits inside a trading session; off-hours it just reconciles
        (the queued rows wait). Sizing/risk run at submit time via `_send_now`
        against that session's reconciled account — never tonight's stale one.

        NOTE: real fills arrive via xtquant callbacks (bridge phase ③); this
        poller reads the bridge's reconciled order list."""
        out = PendingFillResult()
        try:
            venue = {o["order_id"]: o
                     for o in self._client.get("/trader/orders").get("orders", [])}
        except Exception as e:  # noqa: BLE001
            logger.warning("reconcile failed (bridge down?): %s", e)
            return out
        self._sync_risk_config()  # pick up live extreme_gap_up_block_pct
        in_session = self._in_session()
        local_pending = self._db.list_orders(limit=1000, status="pending")
        out.scanned = len(local_pending)
        for o in local_pending:
            vid = (o.get("entry_strategy") or "")  # venue id mirrored here
            if not vid:
                # Un-submitted overnight order: submit at open (in-session only).
                if in_session:
                    self._advance_queued(o, out)
                else:
                    out.still_pending += 1
                continue
            v = venue.get(vid)
            if v is None:
                out.still_pending += 1
                continue
            if v.get("status") == "filled":
                self._db.update_order_filled(
                    o["order_id"], "filled",
                    float(v.get("filled_price", 0) or 0),
                    int(v.get("filled_volume", 0) or 0))
                out.filled += 1
            elif v.get("status") in ("cancelled", "rejected"):
                self._db.update_order_status(o["order_id"], v["status"],
                                             reason="venue reconcile")
                out.rejected += 1
            else:
                out.still_pending += 1
        return out

    def _advance_queued(self, o: dict, out: PendingFillResult) -> None:
        """Submit one un-submitted overnight order at the session open, or
        cancel it on TTL / extreme-gap-up. Updates the same row in place."""
        try:
            created_date = order_decision_date(
                datetime.fromisoformat(o.get("created_at", "")), self._provider)
        except (ValueError, TypeError):
            self._db.update_order_status(o["order_id"], "cancelled",
                                         reason="malformed created_at")
            out.expired += 1
            return
        td = count_trading_days_between(created_date, date.today())
        if td > self._pending_ttl_days:
            self._db.update_order_status(
                o["order_id"], "cancelled",
                reason=f"expired after {td} trading days")
            self._db.log_decision(
                "order_expired_pending",
                f"排队超时取消(实盘) {o['direction']} {o['code']} "
                f"({td} 个交易日未提交成交)",
                code=o["code"], details={"venue": "qmt",
                                         "order_id": o["order_id"],
                                         "trading_days_pending": td})
            out.expired += 1
            return
        sig = Signal(
            stock_code=o["code"], direction=Direction(o["direction"]),
            strength=1.0, reason=o.get("reason", "") or "queued fill",
            entry_strategy="")
        strat = o.get("strategy_name", "") or ""
        # Extreme-gap-up guard (BUY only): if the open has gapped up past the
        # threshold vs the prior close, ABANDON the chase — the 5y study shows
        # the >=10% gap-up bucket is a lottery (median -486bps, p5 -19%,
        # negative mean in the recent regime), and waiting for a pullback has
        # no edge either. On the main board a >=10% gap is a limit-up open the
        # venue can't fill anyway; this newly protects 20cm/30cm boards.
        if sig.direction == Direction.BUY:
            g = self._risk.config.extreme_gap_up_block_pct
            if g and g > 0:
                # RAW axis: `last` is a raw realtime quote, so the prev close the
                # gap is measured against must be raw too — an hfq prev-close
                # (default) makes last/pc≈1/f<1 on dividend stocks, so the gap
                # reads negative and the guard silently never fires (H1).
                pc = prev_bar_close(self._provider, o["code"], date.today(),
                                    adjust="none")
                last = self._latest_price(o["code"])
                if pc and pc > 0 and last > 0 and (last / pc - 1.0) >= g:
                    self._db.update_order_status(
                        o["order_id"], "cancelled",
                        reason=f"extreme gap-up {(last/pc-1):.1%} >= {g:.0%}, abandoned")
                    self._db.log_decision(
                        "order_gap_abandoned",
                        f"放弃追高(实盘) 买入 {o['code']}: 开盘跳涨 "
                        f"{(last/pc-1):.1%} ≥ {g:.0%}",
                        code=o["code"], details={"venue": "qmt",
                                                 "order_id": o["order_id"],
                                                 "gap": last / pc - 1.0})
                    out.expired += 1
                    return
        landed, status, _reason = self._send_now(sig, strat,
                                                  queued_order_id=o["order_id"])
        if status == "filled":
            out.filled += 1
        elif landed:  # accepted, resting at venue
            out.still_pending += 1
        elif status == "pending":  # transient (bridge not live) — retry next tick
            out.still_pending += 1
        else:
            out.rejected += 1

    def check_exits(self) -> int:
        """All three exits against reconciled (venue-truth) positions: stop-loss,
        owning-strategy SELL, and trailing take-profit — same RiskManager call
        PaperBroker makes, so live and backtest can't drift apart (P0-1).

        peaks / strategy-sell codes are computed from the local DB mirror
        (buy_date / entry_strategy live there, not on the venue account) and are
        keyed by code; a code absent from the mirror just degrades to plain
        stop-loss. Triggering is still per-tick on the last price, not intraday
        (phase ⑤)."""
        self._sync_risk_config()
        portfolio, _, stale = self._reconciled_portfolio()
        # A stale/absent realtime price fell back to cost basis (pnl≡0), which
        # would silently disable the stop-loss (audit C5). We CANNOT evaluate an
        # exit on a price we don't have — and must not fabricate one (avg → no
        # stop; 0 → a false -100% stop that dumps the book). Drop the name from
        # exit evaluation and raise a decision-log alert so a human can act.
        for code in stale:
            portfolio.positions.pop(code, None)
            self._db.log_decision(
                "stale_quote",
                f"实时行情缺失,跳过离场判定并告警: {code} "
                f"(止损/止盈本轮未评估,请检查行情源/是否停牌)",
                code=code, details={"venue": "qmt", "reason": "no realtime price"})
        positions = self._db.list_positions()
        # raw_axis: venue prices (last_price/avg_price) are raw, not hfq.
        peaks = compute_peaks(self._db, positions, raw_axis=True)
        if self._risk.config.strategy_exit_enabled:
            strategy_sells = compute_strategy_exits(
                self._provider, self._load_strategies(), positions, self._db)
        else:
            strategy_sells = set()
        # ATR ratios key off the venue-truth holdings (a ratio needs only the
        # code + recent history, not buy_date), so the ATR stop covers every
        # real position even if the DB mirror lacks a row for it.
        atr_ratios = (compute_atr_ratios(
            self._provider, [{"code": c} for c in portfolio.positions],
            self._risk.config.atr_stop_n)
            if self._risk.config.atr_stop_k > 0 else {})
        sells = self._risk.check_exits(portfolio, peaks=peaks,
                                       strategy_sell_codes=strategy_sells,
                                       atr_ratios=atr_ratios)
        landed = 0
        for s in sells:
            if self.execute_signal(s, strategy_name="risk_exit"):
                landed += 1
        # Concentration trim (削峰, opt-in): partial sells back to the band edge
        # for names not already fully exited. "drift_trim" is NOT a forced exit,
        # so it prices at a normal limit (not the 跌停 floor).
        trims = self._risk.check_drift_trims(
            portfolio, exclude={s.stock_code for s in sells})
        for s in trims:
            if self.execute_signal(s, strategy_name=DRIFT_TRIM_STRATEGY):
                landed += 1
        return landed

    def _sync_risk_config(self) -> None:
        """Pull runtime risk thresholds from the DB so edits apply without a
        restart (P0-3). When unset (no row), keep the config the broker was
        built with — don't clobber it with bare defaults."""
        overrides = self._db.get_risk_config()
        if overrides:
            self._risk.config = risk_config_from_dict(overrides)

    def _load_strategies(self) -> dict:
        """Lazy-load strategy classes by name (cached), for strategy exits."""
        if self._strategy_cache is None:
            self._strategy_cache = load_strategies(self._strategies_dir)
        return self._strategy_cache

    def pending_orders_detail(self) -> list[dict]:
        """Pending orders for the UI: open venue orders PLUS locally-queued
        off-hours orders not yet submitted (they'll go to the venue at the
        next session open)."""
        out = []
        try:
            orders = self._client.get("/trader/orders").get("orders", [])
        except Exception:  # noqa: BLE001
            orders = []
        for o in orders:
            if o.get("status") not in ("pending", "accepted"):
                continue
            stock = self._db.get_stock(o["code"])
            out.append({
                "order_id": o["order_id"], "code": o["code"],
                "name": stock.name if stock else o["code"],
                "industry": stock.industry if stock else "",
                "direction": o.get("direction", ""),
                "quantity": o.get("volume", 0),
                # venue 的挂单回报不带策略归属(mirror 的 entry_strategy 列被挪用
                # 存 venue_order_id),pending 列表直读 venue,故留空 → UI 显示「—」。
                "entry_strategy": "",
                "reason": "", "created_at": o.get("created_at", ""),
                "expected_fill_date": None, "fill_price_basis": "venue",
                "bar_available": True, "trading_days_pending": None,
                "ttl_trading_days": None})
        # Locally-queued off-hours orders (entry_strategy empty = no venue id
        # yet). These rest in the DB until the next session's open, when
        # try_fill_pending_orders submits them.
        for o in self._db.list_orders(limit=1000, status="pending"):
            if (o.get("entry_strategy") or ""):
                continue  # already submitted — reflected in the venue list
            stock = self._db.get_stock(o["code"])
            out.append({
                "order_id": o["order_id"], "code": o["code"],
                "name": stock.name if stock else o["code"],
                "industry": stock.industry if stock else "",
                "direction": o.get("direction", ""),
                "quantity": o.get("quantity", 0),
                "entry_strategy": o.get("strategy_name", "") or "",
                "reason": o.get("reason", "") or "",
                "created_at": o.get("created_at", ""),
                "expected_fill_date": None, "fill_price_basis": "next-open",
                "bar_available": False, "trading_days_pending": None,
                "ttl_trading_days": self._pending_ttl_days})
        return out

    # -------------------------------------------------------- order control
    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open venue order (``order_id`` is the venue id)."""
        try:
            res = self._client.post("/trader/cancel", {"order_id": order_id})
        except Exception as e:  # noqa: BLE001
            logger.warning("cancel failed: %s", e)
            return False
        return bool(res.get("ok"))

    def cancel_all_pending(self) -> int:
        """Kill switch step 1: cancel every open venue order."""
        try:
            orders = self._client.get("/trader/orders").get("orders", [])
        except Exception:  # noqa: BLE001
            return 0
        n = 0
        for o in orders:
            if o.get("status") in ("pending", "accepted") and \
                    self.cancel_order(o["order_id"]):
                n += 1
        if n:
            self._db.log_decision("kill_switch", f"急停：撤销 {n} 笔实盘挂单",
                                  details={"venue": "qmt", "cancelled": n})
        return n

    def flatten(self, reason: str = "kill-switch") -> int:
        """Kill switch step 2: submit SELL for every sellable holding.

        Each SELL goes through `_submit_signal`, which caps at the T+1-sellable
        quantity and skips fully-frozen lots — so `acted` counts only positions
        an exit was actually submitted for (the contract in base.py).

        Kill-switch SELLs price at the 跌停 floor (not the live quote), so a
        stale-quote name is still flattened — no stale skip here (unlike the
        stop-loss path in check_exits, which needs a real price to decide)."""
        pf, _, _ = self._reconciled_portfolio()
        acted = 0
        for code, pos in pf.positions.items():
            if pos.quantity <= 0:
                continue
            if self.execute_signal(Signal(stock_code=code,
                                          direction=Direction.SELL,
                                          strength=1.0, reason=reason),
                                   strategy_name="kill_switch"):
                acted += 1
        if acted:
            self._db.log_decision("kill_switch",
                                  f"急停：清仓 {acted} 个实盘持仓 ({reason})",
                                  details={"venue": "qmt", "acted": acted})
        return acted

    def enforce_portfolio_stop(self) -> bool:
        """Portfolio drawdown circuit breaker (live): flatten + signal halt when
        equity is down past portfolio_stop_loss_pct from its high-water mark.

        The HWM is the monotone portfolio_hwm row raised on every check, so a
        same-day top-then-drop still trips it (audit G3/L2) AND the intraday
        peak survives a process restart — without ever writing a transient
        realtime mark into the (close-only) snapshot table."""
        self._sync_risk_config()
        prior_peak = self._db.get_peak_total_value()
        snap = self.snapshot_portfolio()  # intraday: returns marks, no persist
        total = snap["total_value"]
        peak = max(prior_peak, total)
        self._db.raise_hwm(peak)
        if not self._risk.check_portfolio_stop(total, peak):
            return False
        self.cancel_all_pending()
        self.flatten("组合回撤熔断")
        dd = (total - peak) / peak if peak else 0.0
        self._db.log_decision(
            "portfolio_stop",
            f"组合回撤熔断(实盘)：净值 {total:,.0f} 自峰值 {peak:,.0f} 回撤 {dd:.1%}，已清仓",
            details={"venue": "qmt", "total_value": total, "peak_value": peak,
                     "drawdown": dd,
                     "limit": self._risk.config.portfolio_stop_loss_pct})
        return True

    # ----------------------------------------------------------- internals
    def _mirror_order(self, signal: Signal, strategy_name: str, *,
                      status: str, reason: str = "",
                      venue_order_id: str = "", filled_price: float = 0,
                      filled_quantity: int = 0,
                      quantity: int = 0,
                      reuse_order_id: str | None = None) -> str:
        """Persist a local mirror row (audit/UI only — broker is truth).

        The venue order id is stashed in ``entry_strategy`` so the reconciler
        can match local rows to the venue book without a schema change.
        `reuse_order_id` updates an existing overnight-queued row in place
        (its submit outcome) instead of inserting a second row."""
        if reuse_order_id:
            self._db.update_order_submitted(
                reuse_order_id, status=status, quantity=quantity,
                venue_order_id=venue_order_id, filled_price=filled_price,
                filled_quantity=filled_quantity,
                reason=reason or signal.reason)
            return reuse_order_id
        order_id = "q_" + uuid.uuid4().hex[:10]
        self._db.insert_order({
            "order_id": order_id, "code": signal.stock_code,
            "direction": signal.direction.value, "quantity": quantity,
            "price_type": PriceType.MARKET.value, "limit_price": 0.0,
            "status": status, "strategy_name": strategy_name,
            "filled_price": filled_price, "filled_quantity": filled_quantity,
            "reason": reason or signal.reason,
            "created_at": datetime.now().isoformat(),
            "filled_at": datetime.now().isoformat() if status == "filled" else None,
            "entry_strategy": venue_order_id,
        })
        return order_id
