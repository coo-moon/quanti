"""Paper trading broker.

Two fill modes:

  * `fill_mode="pending"` (default, 2026-06-01 onward): signals queue as
    PENDING orders; `try_fill_pending_orders()` later fills them at the
    OPEN of the next available trading bar — modelling real A-share
    semantics where orders submitted off-hours go into the next-day open
    auction. Eliminates the misleading "18:49 卖出" timestamps from
    before, and forces T+1 by construction. EXCEPTION (live-mirror): an
    in-session SELL with a usable realtime quote fills immediately at
    that price, exactly like a live market sell (see execute_signal_ex);
    T+1 still holds — a wholly-frozen holding falls back to the queue,
    and a partially-frozen one fills its settled shares now with the
    frozen remainder re-queued for the next open (_sell_now).

  * `fill_mode="immediate"` (legacy): each signal fills synchronously
    against the most-recent close price. Kept for the backtest engine,
    for unit tests, and as an opt-out for users who want the old behavior.

All A-share rules (T+1, 100-share lots, commission, stamp tax) are honored
in both modes; the difference is *when* and at *what price* the fill happens.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Callable, Literal

if TYPE_CHECKING:
    from quanti.risk.protections import ProtectionConfig

from quanti.backtest.commission import AShareCommission
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.base import BrokerResult, PendingFillResult
from quanti.execution.exits import (
    compute_atr_ratios, compute_peaks, compute_strategy_exits, load_strategies)
from quanti.models import BarData, Direction, PriceType, Signal
from quanti.risk.manager import (
    DRIFT_TRIM_STRATEGY,
    RiskConfig,
    RiskManager,
    risk_config_from_dict,
)
from quanti.risk.sizer import Sizer, compute_buy_target_value
from quanti.utils.market import (
    board_limit_pct,
    count_trading_days_between,
    lot_round_strength,
    max_fill_shares,
    next_trading_bar,
    next_trading_day,
    order_decision_date,
    prev_bar_close,
    tradable_at_close,
    tradable_at_open,
)

logger = logging.getLogger(__name__)


class PaperBroker:
    """Stateful paper-trading broker. Single instance per process.

    Structurally implements the `Broker` protocol (quanti.execution.base) —
    the agent runtime depends on that interface, so a live `QmtBroker` can
    drop in without touching the decision/risk pipeline.
    """

    def __init__(
        self,
        db: Database,
        provider: DataProvider,
        initial_cash: float = 1_000_000.0,
        commission: AShareCommission | None = None,
        slippage: float = 0.001,
        risk_config: RiskConfig | None = None,
        protection_config: ProtectionConfig | None = None,
        sizer: Sizer | None = None,
        fill_mode: Literal["pending", "immediate"] = "immediate",
        pending_ttl_trading_days: int = 3,
        fill_price_basis: Literal["open", "close"] = "open",
        strategies_dir: str = "strategies",
        realtime_quote_fn: Callable[[list[str]], dict[str, float]] | None = None,
    ) -> None:
        """Args:
            protection_config: ProtectionManager thresholds; None → default
                ProtectionConfig() (enabled, fail-open with no history).
            sizer: Optional position sizer.
            fill_mode: "pending" (default) — signals are queued and filled
                at the OPEN of the next trading bar via `try_fill_pending_orders`.
                "immediate" — legacy synchronous fill at the latest close.
                Tests and the backtest path use "immediate"; production runtime
                uses "pending" so timestamps + T+1 reflect reality.
            pending_ttl_trading_days: pending orders past this many trading
                days without a fillable bar are auto-cancelled.
            fill_price_basis: which price of the next bar to fill at.
                "open" matches realistic open-auction behavior.
            realtime_quote_fn: optional batch last-price source
                (codes → {code: price}) used to mark positions DURING trading
                sessions, so the intraday guard sees today's prices instead of
                yesterday's close. Marks only — fills keep their bar-based
                pricing. None (tests/backtests) → daily-close marks as before.
        """
        self._db = db
        self._provider = provider
        self._commission = commission or AShareCommission()
        self._slippage = slippage
        self._risk = RiskManager(risk_config)
        from quanti.risk.protections import ProtectionConfig, ProtectionManager
        self._protections = ProtectionManager(
            protection_config if protection_config is not None
            else ProtectionConfig())
        self._sizer = sizer
        self._fill_mode = fill_mode
        self._pending_ttl_days = pending_ttl_trading_days
        self._fill_basis = fill_price_basis
        self._strategies_dir = strategies_dir
        self._realtime_quote_fn = realtime_quote_fn
        self._strategy_cache: dict | None = None  # name → strategy instance
        # Idempotent — only writes if no row exists.
        self._db.ensure_portfolio(initial_cash)

    def set_sizer(self, sizer: Sizer | None) -> None:
        """Swap the position sizer at runtime (None → legacy cash%/risk-cap).

        Lets the agent runtime toggle equal-weight sizing per goal without
        rebuilding the broker."""
        self._sizer = sizer

    def _entry_allowed(self, signal: Signal,
                       portfolio) -> tuple[bool, str, str]:
        """Risk caps + protections gate for an entry, via the shared helper.
        Returns (ok, reason, reject_kind). Protections only gate BUY."""
        from quanti.risk.protection_context import evaluate_entry
        return evaluate_entry(self._risk, self._protections, self._db,
                              self._provider, signal, portfolio)

    def _recent_bars(self, code: str, days: int = 90) -> list[BarData]:
        """Fetch the most recent `days` of bars for vol-targeting / impact.

        Slightly more history than `_latest_close` so a vol-target sizer
        has 60+ bars to estimate σ. Cheap because the DB is local.
        """
        end = date.today()
        start = end - timedelta(days=days)
        return self._provider.get_daily_bars(code, start, end)

    # ------------------------------------------------------------------ price
    def _latest_close(self, code: str) -> tuple[float, date] | None:
        """Return (close, date) for the most recent bar we have on disk."""
        end = date.today()
        start = end - timedelta(days=30)
        bars = self._provider.get_daily_bars(code, start, end)
        if not bars:
            return None
        last = bars[-1]
        return last.close, last.date

    def _latest_bar(self, code: str) -> BarData | None:
        """Most recent full bar on disk (for the immediate-fill tradability
        gate, which needs the open to detect a limit-locked session)."""
        end = date.today()
        start = end - timedelta(days=30)
        bars = self._provider.get_daily_bars(code, start, end)
        return bars[-1] if bars else None

    def _intraday_marks(self, codes: list[str]) -> dict[str, float]:
        """In-session realtime last-price overlay for position marks, on the
        hfq axis (raw quote × latest adj_factor) so it's comparable with
        avg_cost / daily-close marks, which all live on that axis.

        Returns {} without a quote source, outside trading sessions, in
        immediate fill mode, or on fetch failure (warned) — callers then fall
        back to the daily close, so those paths behave exactly as before.
        Immediate mode is excluded on purpose: it prices fills off the latest
        daily bar, so realtime marks would let an exit decide at today's
        price but fill at yesterday's close.

        In pending mode these marks price BOTH position marks AND in-session
        SELL fills (execute_signal_ex → _sell_now — a live market sell, same
        as xtdata on the live account); BUYs stay bar-based (next open). The
        quote source only returns prices PRINTED today (tencent_quotes drops
        stale last-trade timestamps), so a suspended/halted name is simply
        absent — its mark stays on the daily close and its sells queue; a
        fill can never price off a previous day. The defense against a
        glitched same-day quote moving money is the ±35% band filter below.

        Known limits, accepted as the live-mirror tradeoff: quotes are a
        single source sampled once per guard tick, so a WITHIN-band bad print
        (say -8%) can fire a stop and become its fill price irreversibly; and
        on an ex-dividend day the stored adj_factor lags one bar, so the hfq
        mark dips by the dividend ratio until today's bar lands. If either
        bites in practice, the fix is two consecutive same-direction samples
        before selling — state we deliberately don't carry yet.

        NOTE this makes paper stops intraday-touch AND same-day-filled (like
        live), not the close-confirmed semantics of the 4h tick / backtest —
        a name that pierces the stop intraday exits that day at the realtime
        price even if the close recovers.
        """
        if (self._realtime_quote_fn is None or not codes
                or self._fill_mode != "pending"):
            return {}
        from quanti.utils.market import in_trading_session
        if not in_trading_session(datetime.now(), self._provider):
            return {}
        try:
            raw = self._realtime_quote_fn(codes)
        except Exception as e:  # noqa: BLE001 - stale marks beat a dead guard/UI
            logger.warning(
                "realtime marks unavailable, falling back to daily close: %s", e)
            return {}
        out: dict[str, float] = {}
        tomorrow = date.today() + timedelta(days=1)
        for code, price in raw.items():
            if not price or price <= 0:
                continue
            ref = self._db.get_latest_quote_before(code, tomorrow)
            if ref is None:
                continue  # no bar on disk → no factor/band reference: skip
            raw_close, factor = ref
            # Bad-tick guard: a real print can't sit outside ±~30% (widest
            # A-share daily band, 北交所) of the last raw close. One glitched
            # quote must not fire a stop or the circuit breaker. Slightly
            # loose (0.35) so a stale-by-one-day close can't reject a real
            # limit move.
            if raw_close > 0 and abs(price / raw_close - 1.0) > 0.35:
                logger.warning(
                    "discarding out-of-band quote %s=%.2f (last close %.2f)",
                    code, price, raw_close)
                continue
            out[code] = price * (factor if factor > 0 else 1.0)
        return out

    # ------------------------------------------------------------ portfolio
    def snapshot_portfolio(self) -> dict:
        """Mark all positions to the latest close, persist a snapshot, return summary."""
        state = self._db.get_portfolio_state() or self._db.ensure_portfolio(0.0)
        cash = state["cash"]
        positions = self._db.list_positions()
        market_value = 0.0
        enriched: list[dict] = []
        latest_d: date | None = None
        # In-session realtime overlay (paper intraday guard); {} off-session.
        marks = self._intraday_marks([p["code"] for p in positions])
        for pos in positions:
            live = marks.get(pos["code"])
            quote = ((live, date.today()) if live
                     else self._latest_close(pos["code"]))
            price = quote[0] if quote else pos["current_price"] or pos["avg_cost"]
            if quote:
                self._db.set_position_price(pos["code"], price)
                if latest_d is None or quote[1] > latest_d:
                    latest_d = quote[1]
            mv = price * pos["quantity"]
            market_value += mv
            stock = self._db.get_stock(pos["code"])
            enriched.append({
                **pos,
                "name": stock.name if stock else pos["code"],
                "current_price": price,
                # Market date the current_price reflects (the bar we marked
                # to), NOT the DB row's updated_at. None when we have no bar
                # on disk and fell back to avg_cost.
                "price_date": quote[1].isoformat() if quote else None,
                "market_value": mv,
                "pnl": (price - pos["avg_cost"]) * pos["quantity"],
                "pnl_pct": (price - pos["avg_cost"]) / pos["avg_cost"]
                           if pos["avg_cost"] else 0.0,
            })

        total = cash + market_value
        snap_d = latest_d or date.today()
        # In-session (realtime-marked) snapshots are transient — do NOT
        # persist them. portfolio_snapshots must stay a close-marked daily
        # series: an intraday spike written here would inflate the drawdown
        # high-water mark (get_peak_total_value = table MAX, rows are never
        # rewritten once the day passes) and pollute attribution/protection
        # equity series. The circuit breaker still sees the realtime total
        # via this method's return value.
        if not marks:
            self._db.save_portfolio_snapshot(snap_d, cash, market_value, total)
        return {
            "cash": cash,
            "initial_cash": state["initial_cash"],
            "market_value": market_value,
            "total_value": total,
            "pnl": total - state["initial_cash"],
            "pnl_pct": (total - state["initial_cash"]) / state["initial_cash"]
                       if state["initial_cash"] else 0.0,
            "positions": enriched,
            "snapshot_date": snap_d.isoformat(),
        }

    # ------------------------------------------------------------ execution
    def execute_signal(self, signal: Signal, strategy_name: str = "") -> bool:
        """Process one signal. Returns True iff the signal landed (filled or
        successfully queued). Callers that need to know which, use
        `execute_signal_ex` / `execute_signals`."""
        return self.execute_signal_ex(signal, strategy_name) != "rejected"

    def execute_signal_ex(self, signal: Signal, strategy_name: str = "") -> str:
        """Process one signal → "filled" | "pending" | "rejected".

        Pending mode mirrors a live venue: an in-session SELL with a usable
        realtime quote fills NOW at that price (as a live market sell would);
        it falls back to the queue (fill at next open — an order resting at
        the venue) when there's no quote, the name is limit-down locked, or
        the whole holding is still T+1-frozen today. BUYs and off-session
        signals queue as before.
        """
        if self._fill_mode == "immediate":
            return ("filled"
                    if self._execute_signal_immediate(signal, strategy_name)
                    else "rejected")
        if signal.direction == Direction.SELL:
            price = self._intraday_marks([signal.stock_code]).get(signal.stock_code)
            if price and not self._limit_down_locked(signal.stock_code, price):
                pos = next((p for p in self._db.list_positions()
                            if p["code"] == signal.stock_code), None)
                if pos is None or self._sellable_qty(pos, date.today()) > 0:
                    # pos None → _sell_now records the "no position" reject
                    # now, instead of parking a doomed pending order.
                    return ("filled"
                            if self._sell_now(signal, strategy_name, price)
                            else "rejected")
        landed = self._queue_pending_signal(signal, strategy_name)
        return "pending" if landed else "rejected"

    def execute_signals(self, signals: list[Signal],
                        strategy_name: str = "") -> BrokerResult:
        out = BrokerResult()
        for s in signals:
            out.accepted += 1
            status = self.execute_signal_ex(s, strategy_name)
            if status == "rejected":
                out.rejected += 1
            elif status == "filled":
                out.filled += 1
            else:
                out.pending += 1
        # Always re-snapshot after a batch so the dashboard is fresh.
        self.snapshot_portfolio()
        return out

    # ------------------------------ immediate (synchronous) execution path
    def _execute_signal_immediate(self, signal: Signal,
                                  strategy_name: str) -> bool:
        """Old behavior: fill synchronously against latest close. Used by
        the backtest path and unit tests."""
        portfolio = self._build_runtime_portfolio()
        ok, reason, kind = self._entry_allowed(signal, portfolio)
        if not ok:
            self._record_order(signal, strategy_name, status="rejected",
                               reason=reason)
            self._db.log_decision(
                kind,
                f"{'风控' if kind == 'risk_reject' else '保护层'}拒绝 "
                f"{signal.direction.value} {signal.stock_code}: {reason}",
                code=signal.stock_code,
                details={"signal_reason": signal.reason},
            )
            return False

        bar = self._latest_bar(signal.stock_code)
        if bar is None:
            self._record_order(signal, strategy_name, status="rejected",
                               reason="no market data")
            return False
        ref_price, bar_date = float(bar.close), bar.date

        # Tradability gate: can't buy a limit-up lock or sell a limit-down lock
        # (incl. 一字板). Immediate mode fills at the close, so gate on the
        # close; no queue, so reject rather than defer (audit C3).
        pc = prev_bar_close(self._provider, signal.stock_code, bar.date)
        if not tradable_at_close(signal.direction, bar, pc):
            r = "涨跌停板 当日不可成交"
            self._record_order(signal, strategy_name, status="rejected", reason=r)
            self._db.log_decision(
                "order_skipped_limit",
                f"跳过 {signal.direction.value} {signal.stock_code}: {r}",
                code=signal.stock_code,
                details={"bar_date": bar.date.isoformat(), "open": bar.open,
                         "prev_close": pc})
            return False

        if signal.direction == Direction.BUY:
            return self._fill_buy(signal, ref_price, bar_date, strategy_name,
                                  bar_amount=float(bar.amount or 0))
        return self._fill_sell(signal, ref_price, bar_date, strategy_name,
                               bar_amount=float(bar.amount or 0))

    # ------------------------------ pending (queued) execution path
    def _queue_pending_signal(self, signal: Signal,
                              strategy_name: str) -> bool:
        """Queue a signal as a PENDING order. Risk is checked at queue
        time AND again at fill time — early rejection saves writing rows
        for hopeless signals, but late rejection catches portfolio drift
        between queue and fill.

        Dedup: if there's already a pending order for the same (code,
        direction), the new signal is dropped to avoid stacked orders.
        Different directions on the same code are allowed (a SELL can
        queue while a BUY is still pending; the SELL just won't fill
        until the BUY does and a position exists).
        """
        # Early risk gate
        portfolio = self._build_runtime_portfolio()
        ok, reason, kind = self._entry_allowed(signal, portfolio)
        if not ok:
            self._record_order(signal, strategy_name, status="rejected",
                               reason=reason)
            self._db.log_decision(
                kind,
                f"{'风控' if kind == 'risk_reject' else '保护层'}拒绝 "
                f"{signal.direction.value} {signal.stock_code}: {reason}",
                code=signal.stock_code,
                details={"signal_reason": signal.reason, "stage": "queue"},
            )
            return False

        # Dedup against current pending queue
        for o in self._db.list_orders(limit=500, status="pending"):
            if o["code"] == signal.stock_code and o["direction"] == signal.direction.value:
                # Already have a pending order for this code+direction.
                # Drop silently — no row, no log spam — so a strategy
                # that emits the same signal every tick doesn't pollute
                # the orders table.
                return False

        # Reject SELL with no position immediately (cheap check).
        if signal.direction == Direction.SELL:
            held = {p["code"] for p in self._db.list_positions()}
            if signal.stock_code not in held:
                self._record_order(signal, strategy_name,
                                   status="rejected", reason="no position")
                return False

        order_id = self._record_order(signal, strategy_name,
                                      status="pending", reason=signal.reason)
        self._db.log_decision(
            "order_queued",
            f"挂单 {signal.direction.value} {signal.stock_code} (待下一交易日开盘成交)",
            code=signal.stock_code,
            details={"order_id": order_id, "strategy": strategy_name,
                     "signal_reason": signal.reason,
                     "queued_strength": signal.strength},
        )
        return True

    # ------------------------------ pending fill scanner
    def try_fill_pending_orders(self) -> PendingFillResult:
        """Scan all pending orders, fill any whose next-trading-bar is
        now available. Expire ones that have waited too long.

        Should be called at the START of each agent tick (before new
        signals queue) so today's pending fills affect the cash/position
        state seen by today's new signal generation.
        """
        out = PendingFillResult()
        pending = self._db.list_orders(limit=1000, status="pending")
        out.scanned = len(pending)
        today = date.today()

        for o in pending:
            created_at = o.get("created_at", "")
            try:
                # Decision-data day, not the wall-clock date: the 23:30 agent
                # cycle stamps orders past midnight, and dating those rows by
                # wall clock would push the fill a full extra trading day out
                # (next_trading_bar is strictly-greater). TTL ages from the
                # same day, so a spilled-over order also expires one day
                # sooner — consistent with how old its data actually is.
                created_date = order_decision_date(
                    datetime.fromisoformat(created_at), self._provider)
            except (ValueError, TypeError):
                # Bad row, can't reason about TTL — cancel and move on.
                self._db.update_order_status(o["order_id"], "cancelled",
                                             reason="malformed created_at")
                out.expired += 1
                continue

            bar = next_trading_bar(self._provider, o["code"], created_date)
            if bar is None:
                # No newer bar yet. Check TTL.
                td = count_trading_days_between(created_date, today)
                if td > self._pending_ttl_days:
                    self._db.update_order_status(o["order_id"], "cancelled",
                                                 reason=f"expired after {td} trading days")
                    self._db.log_decision(
                        "order_expired_pending",
                        f"挂单超时取消 {o['direction']} {o['code']} ({td} 个交易日未成交)",
                        code=o["code"],
                        details={"order_id": o["order_id"],
                                 "trading_days_pending": td})
                    out.expired += 1
                else:
                    out.still_pending += 1
                continue

            # We have a fillable bar. Build a Signal from the order row,
            # re-run risk, fill at bar.open (+/- slippage). Carry the order's
            # entry_strategy so it lands on the position at fill.
            sig = Signal(
                stock_code=o["code"],
                direction=Direction(o["direction"]),
                strength=1.0,
                reason=o.get("reason", "") or "pending fill",
                entry_strategy=o.get("entry_strategy", "") or "",
            )
            portfolio = self._build_runtime_portfolio()
            ok, reason, kind = self._entry_allowed(sig, portfolio)
            if not ok:
                self._db.update_order_status(o["order_id"], "rejected",
                                             reason=f"{kind}: {reason}")
                self._db.log_decision(
                    kind,
                    f"{'风控' if kind == 'risk_reject' else '保护层'}拒绝 (成交时) "
                    f"{sig.direction.value} {sig.stock_code}: {reason}",
                    code=sig.stock_code,
                    details={"order_id": o["order_id"], "stage": "fill"})
                out.rejected += 1
                out.reasons.append(reason)
                continue

            # Tradability gate: a BUY can't fill into a limit-up lock and a
            # SELL can't fill into a limit-down lock (incl. 一字板). Keep the
            # order pending across locked days within the TTL — exactly what
            # the backtest engine does, so paper/live and backtest agree (C3).
            pc = prev_bar_close(self._provider, o["code"], bar.date)
            _gate = (tradable_at_open if self._fill_basis == "open"
                     else tradable_at_close)
            if not _gate(sig.direction, bar, pc):
                td = count_trading_days_between(created_date, today)
                if td > self._pending_ttl_days:
                    self._db.update_order_status(
                        o["order_id"], "cancelled",
                        reason=f"limit-locked {td} trading days")
                    self._db.log_decision(
                        "order_expired_pending",
                        f"挂单超时取消 {o['direction']} {o['code']} "
                        f"(涨跌停板 {td} 个交易日未能成交)",
                        code=o["code"],
                        details={"order_id": o["order_id"],
                                 "trading_days_pending": td,
                                 "reason": "limit_locked"})
                    out.expired += 1
                else:
                    out.still_pending += 1
                continue

            # Pick the price.
            ref_price = float(bar.open if self._fill_basis == "open"
                              else bar.close)
            filled = self._fill_pending(sig, ref_price, bar.date, o,
                                        bar_amount=float(bar.amount or 0))
            if filled:
                out.filled += 1
            else:
                # _fill_pending sets the status to rejected itself on cash/T+1
                # failure. Count as rejected here.
                out.rejected += 1
        return out

    def pending_orders_detail(self) -> list[dict]:
        """Enrich each pending order with its fill timeline, for the UI.

        Per order we report when it was queued, the trading day its fill
        bar belongs to (T+1 open by construction), whether that bar is
        already on disk (so it fills on the next tick) or we're still
        waiting on the data feed, how many trading days it has waited, and
        the TTL after which it auto-cancels. Read-only — no state change.
        """
        today = date.today()
        out: list[dict] = []
        for o in self._db.list_orders(limit=1000, status="pending"):
            created_at = o.get("created_at", "")
            try:
                # Same decision-day attribution as try_fill_pending_orders,
                # so the advertised fill date matches what the scanner does.
                created_date = order_decision_date(
                    datetime.fromisoformat(created_at), self._provider)
            except (ValueError, TypeError):
                created_date = None

            expected_fill_date: str | None = None
            bar_available = False
            days_pending: int | None = None
            if created_date is not None:
                bar = next_trading_bar(self._provider, o["code"], created_date)
                if bar is not None:
                    expected_fill_date = bar.date.isoformat()
                    bar_available = True
                else:
                    # Data feed hasn't caught up; estimate the next session.
                    expected_fill_date = next_trading_day(created_date).isoformat()
                days_pending = count_trading_days_between(created_date, today)

            stock = self._db.get_stock(o["code"])
            out.append({
                "order_id": o["order_id"],
                "code": o["code"],
                "name": stock.name if stock else o["code"],
                "direction": o["direction"],
                "quantity": o["quantity"],
                "reason": o.get("reason", "") or "",
                # 进场策略 = signal.entry_strategy(ensemble/LLM 路径记录的主导策略,
                # 与离场重放 compute_strategy_exits 用的是同一字段)。不是 strategy_name
                # —— LLM 模式 strategy_name="llm",entry_strategy 才是真正的归属策略
                # (supertrend/turtle…);纯因子/sentiment 驱动则为空 → UI 显示「—」。
                "entry_strategy": o.get("entry_strategy", "") or "",
                "created_at": created_at,
                "expected_fill_date": expected_fill_date,
                "fill_price_basis": self._fill_basis,  # "open" → 次日开盘价
                "bar_available": bar_available,
                "trading_days_pending": days_pending,
                "ttl_trading_days": self._pending_ttl_days,
            })
        return out

    # ----------------------------------------------------------- health
    def is_connected(self) -> bool:
        """Paper broker has no external venue — always 'connected'.

        Exists so the runtime can gate on `broker.is_connected()` uniformly;
        QmtBroker overrides this with a real bridge/QMT health check."""
        return True

    # ------------------------------------------------------- order control
    def cancel_order(self, order_id: str) -> bool:
        """Cancel a single still-pending order. Returns False if the order
        isn't pending (already filled / cancelled / unknown)."""
        pending = {o["order_id"]
                   for o in self._db.list_orders(limit=1000, status="pending")}
        if order_id not in pending:
            return False
        self._db.update_order_status(order_id, "cancelled",
                                     reason="cancelled by user")
        self._db.log_decision(
            "order_cancelled", f"撤单 {order_id}",
            details={"order_id": order_id})
        return True

    def cancel_all_pending(self) -> int:
        """Kill switch, step 1: cancel every pending order. Returns count.

        Idempotent — a second call finds nothing pending and returns 0."""
        pending = self._db.list_orders(limit=1000, status="pending")
        for o in pending:
            self._db.update_order_status(o["order_id"], "cancelled",
                                         reason="kill-switch: cancel all")
        if pending:
            self._db.log_decision(
                "kill_switch", f"急停：撤销 {len(pending)} 笔挂单",
                details={"cancelled": len(pending)})
        return len(pending)

    def flatten(self, reason: str = "kill-switch") -> int:
        """Kill switch, step 2: submit SELL orders for all holdings.

        Routed through `execute_signals`, so RiskManager and T+1 still apply
        (a lot bought today won't sell and is skipped). Returns the number of
        positions an exit was submitted for (filled in immediate mode, queued
        in pending mode)."""
        positions = [p for p in self._db.list_positions() if p["quantity"] > 0]
        if not positions:
            return 0
        sells = [Signal(stock_code=p["code"], direction=Direction.SELL,
                        strength=1.0, reason=reason) for p in positions]
        result = self.execute_signals(sells, strategy_name="kill_switch")
        acted = result.filled + result.pending
        self._db.log_decision(
            "kill_switch",
            f"急停：清仓 {acted}/{len(sells)} 个持仓 ({reason})",
            details={"positions": len(sells), "acted": acted,
                     "filled": result.filled, "pending": result.pending})
        return acted

    def _limit_down_locked(self, code: str, hfq_price: float) -> bool:
        """A market sell can't fill into a limit-down lock. Judge on the raw
        quote (hfq mark ÷ factor) vs the last raw close with the stock's band
        — ST names (±5%) by name prefix, else the board band by code."""
        ref = self._db.get_latest_quote_before(code, date.today() + timedelta(days=1))
        if ref is None or ref[0] <= 0:
            return False
        raw_close, factor = ref
        raw_now = hfq_price / (factor if factor > 0 else 1.0)
        stock = self._db.get_stock(code)
        band = (0.05 if stock and "ST" in (stock.name or "")
                else board_limit_pct(code))
        return raw_now <= raw_close * (1 - band) + 1e-9

    def _sell_now(self, signal: Signal, strategy_name: str,
                  price: float) -> bool:
        """Fill a SELL immediately at the given in-session realtime mark (hfq
        axis; slippage/commission/T+1 via the normal _fill_sell path) — what a
        live market sell does. Yesterday's bar turnover proxies today's B1
        participation cap (there's no intraday turnover feed here).

        Full-exit sells (anything but the 削峰 trim) that fill also supersede
        SELLs resting in the queue (no double-fill tomorrow) and re-queue any
        remainder the fill couldn't take (T+1-frozen lot / B1 cap), so the
        exit intent still lands at the next open — the kill-switch/breaker
        must never silently strand part of a position. Partial-intent trims
        leave resting orders alone, like a live market trim would.
        """
        bar = self._latest_bar(signal.stock_code)
        filled = self._fill_sell(signal, price, date.today(), strategy_name,
                                 bar_amount=float(bar.amount or 0) if bar else 0.0)
        full_exit = (strategy_name != DRIFT_TRIM_STRATEGY
                     and signal.strength >= 1.0)
        if filled and full_exit:
            for o in self._db.list_orders(limit=1000, status="pending"):
                if o["code"] == signal.stock_code and o["direction"] == "sell":
                    self._db.update_order_status(
                        o["order_id"], "cancelled",
                        reason="superseded by market sell")
            left = next((p for p in self._db.list_positions()
                         if p["code"] == signal.stock_code), None)
            if left is not None and left["quantity"] > 0:
                self._queue_pending_signal(signal, strategy_name)
        return filled

    def enforce_portfolio_stop(self) -> bool:
        """Portfolio drawdown circuit breaker: if equity is down past
        `portfolio_stop_loss_pct` from its high-water mark, cancel pending +
        flatten everything. Returns True iff it fired (caller halts the agent).

        The high-water mark is read BEFORE snapshot_portfolio() persists today's
        row: snapshot writes today's (possibly drawn-down) value via
        INSERT-OR-REPLACE keyed by date, which would otherwise overwrite an
        earlier, higher same-day peak and deflate the peak — the breaker would
        then never fire on a same-day top-then-drop (audit G3/L2)."""
        self._sync_risk_config()
        prior_peak = self._db.get_peak_total_value()
        snap = self.snapshot_portfolio()  # persists today's snapshot (overwrite)
        total = snap["total_value"]
        peak = max(prior_peak, total)
        if not self._risk.check_portfolio_stop(total, peak):
            return False
        self.cancel_all_pending()
        self.flatten("组合回撤熔断")
        dd = (total - peak) / peak if peak else 0.0
        self._db.log_decision(
            "portfolio_stop",
            f"组合回撤熔断：净值 {total:,.0f} 自峰值 {peak:,.0f} 回撤 {dd:.1%} "
            f"≤ {self._risk.config.portfolio_stop_loss_pct:.0%}，已清仓",
            details={"total_value": total, "peak_value": peak,
                     "drawdown": dd,
                     "limit": self._risk.config.portfolio_stop_loss_pct})
        return True

    def _fill_pending(self, signal: Signal, ref_price: float,
                      bar_date: date, order_row: dict,
                      bar_amount: float = 0.0) -> bool:
        """Fill an existing pending order row at the given price.

        Mirrors _fill_buy / _fill_sell but updates the existing order row
        instead of inserting a new one. On any failure (no cash, T+1,
        no position), the order is moved to 'rejected'. `bar_amount` (成交额,
        元) caps the single-bar fill to a share of turnover (B1).
        """
        order_id = order_row["order_id"]
        strategy_name = order_row.get("strategy_name", "") or ""

        if signal.direction == Direction.BUY:
            state = self._db.get_portfolio_state()
            if state is None:
                self._db.update_order_status(order_id, "rejected",
                                             reason="no portfolio")
                return False
            cash = state["cash"]
            price = ref_price * (1 + self._slippage)

            total_value = cash + sum(
                p["quantity"] * (p["current_price"] or p["avg_cost"])
                for p in self._db.list_positions())
            size_cap = self._risk.max_additional_buy_value(
                self._build_runtime_portfolio(), signal.stock_code,
                self._stock_industry(signal.stock_code))
            recent = (self._recent_bars(signal.stock_code)
                      if self._sizer is not None else None)
            target_value = compute_buy_target_value(
                cash=cash, total_value=total_value, strength=signal.strength,
                size_cap=size_cap, code=signal.stock_code,
                sizer=self._sizer, recent_bars=recent)

            commission_est = self._commission.calculate(price, 100, Direction.BUY)
            affordable_lots = int(target_value / (price * 100 + commission_est))
            if affordable_lots < 1:
                self._db.update_order_status(
                    order_id, "rejected",
                    reason="cash or position cap too tight")
                return False
            quantity = affordable_lots * 100
            # Capacity cap (B1): ≤ participation of the bar's turnover.
            vcap = max_fill_shares(bar_amount, price)
            if vcap is not None:
                quantity = min(quantity, vcap)
            if quantity < 100:
                self._db.update_order_status(
                    order_id, "rejected", reason="bar volume cap too tight")
                return False
            cost = price * quantity
            commission = self._commission.calculate(price, quantity, Direction.BUY)
            if cost + commission > cash:
                quantity -= 100
                if quantity <= 0:
                    self._db.update_order_status(order_id, "rejected",
                                                 reason="cash too low")
                    return False
                cost = price * quantity
                commission = self._commission.calculate(price, quantity, Direction.BUY)

            new_cash = cash - cost - commission
            self._db.update_cash(new_cash)
            existing = next((p for p in self._db.list_positions()
                             if p["code"] == signal.stock_code), None)
            if existing:
                total_qty = existing["quantity"] + quantity
                avg = (existing["avg_cost"] * existing["quantity"]
                       + price * quantity) / total_qty
                # Today's frozen lot = prior same-day frozen (if any) + this buy;
                # a frozen lot from an earlier day has settled, so it resets.
                prev_frozen = (existing.get("frozen_qty") or 0) \
                    if existing.get("frozen_date") == bar_date else 0
                # entry_strategy=None preserves the original owner on add-ons.
                self._db.upsert_position(signal.stock_code, total_qty, avg,
                                         ref_price,
                                         existing["buy_date"] or bar_date,
                                         frozen_qty=int(prev_frozen) + quantity,
                                         frozen_date=bar_date)
            else:
                self._db.upsert_position(signal.stock_code, quantity, price,
                                         ref_price, bar_date,
                                         frozen_qty=quantity, frozen_date=bar_date,
                                         entry_strategy=signal.entry_strategy)
            self._db.update_order_filled(order_id, "filled", price, quantity)
            trade_id = "t_" + uuid.uuid4().hex[:10]
            self._db.insert_trade({
                "trade_id": trade_id, "order_id": order_id,
                "code": signal.stock_code, "direction": "buy",
                "quantity": quantity, "price": price,
                "commission": commission, "strategy_name": strategy_name,
                "trade_date": bar_date.isoformat(),
            })
            self._risk.record_trade(signal.direction)
            self._db.log_decision(
                "order_filled_pending",
                f"挂单成交 买入 {signal.stock_code} {quantity}股 @ {price:.2f} "
                f"(开盘价 {bar_date.isoformat()})",
                code=signal.stock_code,
                details={"order_id": order_id, "strategy": strategy_name,
                         "fill_price": price, "fill_bar_date": bar_date.isoformat(),
                         "commission": commission})
            return True

        # SELL pending
        positions = {p["code"]: p for p in self._db.list_positions()}
        pos = positions.get(signal.stock_code)
        if pos is None or pos["quantity"] <= 0:
            self._db.update_order_status(order_id, "rejected",
                                         reason="no position at fill")
            return False
        # T+1: only the settled portion (holding minus today's frozen lot) can
        # be sold; a same-day add-on stays frozen. Caps the SELL like the live
        # venue's can_use_volume instead of dumping the whole position (F1).
        quantity = self._sellable_qty(pos, bar_date)
        if quantity <= 0:
            self._db.update_order_status(order_id, "rejected",
                                         reason="T+1 restriction at fill")
            return False
        # Partial-sell: ONLY the concentration trim (削峰) sells a fraction; every
        # other SELL (stop/TP/strategy/flatten/manual) fully exits regardless of
        # its strength (some strategies emit closing SELLs with strength<1.0).
        # Sub-lot trim → 0 → the <100 check below rejects (no-op).
        if strategy_name == DRIFT_TRIM_STRATEGY:
            quantity = lot_round_strength(quantity, signal.strength)
        # Capacity cap (B1): can't dump more than participation of turnover in
        # one bar. Remainder carries; a re-emitted SELL clears it next session.
        vcap = max_fill_shares(bar_amount, ref_price)
        if vcap is not None:
            quantity = min(quantity, vcap)
        if quantity < 100:
            self._db.update_order_status(order_id, "rejected",
                                         reason="bar volume cap too tight")
            return False

        price = ref_price * (1 - self._slippage)
        revenue = price * quantity
        commission = self._commission.calculate(price, quantity, Direction.SELL)
        net = revenue - commission

        state = self._db.get_portfolio_state()
        if state is None:
            self._db.update_order_status(order_id, "rejected",
                                         reason="no portfolio")
            return False
        self._db.update_cash(state["cash"] + net)
        remaining = pos["quantity"] - quantity
        if remaining > 0:
            # Keep ONLY the pre-existing today-frozen lot frozen — never freeze
            # the settled shares we chose not to sell. A partial 削峰 trim of a
            # settled position must leave the rest sellable (so a same-session
            # stop isn't blocked); a capacity-capped exit likewise keeps trying.
            kept = (int(pos.get("frozen_qty") or 0)
                    if pos.get("frozen_date") == bar_date else 0)
            self._db.upsert_position(
                signal.stock_code, remaining, pos["avg_cost"],
                pos["current_price"] or pos["avg_cost"], pos["buy_date"],
                frozen_qty=min(kept, remaining), frozen_date=bar_date)
        else:
            self._db.delete_position(signal.stock_code)
        self._db.update_order_filled(order_id, "filled", price, quantity)
        trade_id = "t_" + uuid.uuid4().hex[:10]
        self._db.insert_trade({
            "trade_id": trade_id, "order_id": order_id,
            "code": signal.stock_code, "direction": "sell",
            "quantity": quantity, "price": price,
            "commission": commission, "strategy_name": strategy_name,
            "trade_date": bar_date.isoformat(),
        })
        self._risk.record_trade(signal.direction)
        self._db.log_decision(
            "order_filled_pending",
            f"挂单成交 卖出 {signal.stock_code} {quantity}股 @ {price:.2f} "
            f"(开盘价 {bar_date.isoformat()})",
            code=signal.stock_code,
            details={"order_id": order_id, "strategy": strategy_name,
                     "fill_price": price, "fill_bar_date": bar_date.isoformat(),
                     "pnl": (price - pos["avg_cost"]) * quantity})
        return True

    # ----------------------------------------------------------- internals
    def _build_runtime_portfolio(self):
        """Convert DB state into a Portfolio dataclass for the risk manager."""
        from quanti.models import Portfolio, Position
        state = self._db.get_portfolio_state() or self._db.ensure_portfolio(0.0)
        portfolio = Portfolio(cash=state["cash"])
        for pos in self._db.list_positions():
            portfolio.positions[pos["code"]] = Position(
                stock_code=pos["code"], quantity=pos["quantity"],
                avg_cost=pos["avg_cost"],
                current_price=pos["current_price"] or pos["avg_cost"],
                buy_date=pos["buy_date"],
                industry=self._stock_industry(pos["code"]),
            )
        return portfolio

    def _stock_industry(self, code: str) -> str:
        stock = self._db.get_stock(code)
        return stock.industry if stock else ""

    @staticmethod
    def _sellable_qty(pos: dict, bar_date: date) -> int:
        """T+1-sellable quantity = holding minus the lot bought *today* (still
        frozen). Mirrors QmtBroker's `can_use_volume` so a same-day add-on can't
        be over-sold (audit F1). A frozen lot dated before today has settled."""
        frozen = pos.get("frozen_qty") or 0
        if pos.get("frozen_date") != bar_date:
            frozen = 0
        return int(pos["quantity"]) - int(frozen)

    def _record_order(self, signal: Signal, strategy_name: str, *,
                      status: str, reason: str = "",
                      filled_price: float = 0, filled_quantity: int = 0,
                      quantity: int | None = None) -> str:
        order_id = "o_" + uuid.uuid4().hex[:10]
        self._db.insert_order({
            "order_id": order_id,
            "code": signal.stock_code,
            "direction": signal.direction.value,
            "quantity": quantity if quantity is not None else 0,
            "price_type": PriceType.MARKET.value,
            "limit_price": 0.0,
            "status": status,
            "strategy_name": strategy_name,
            "filled_price": filled_price,
            "filled_quantity": filled_quantity,
            "reason": reason or signal.reason,
            "created_at": datetime.now().isoformat(),
            "filled_at": datetime.now().isoformat() if status == "filled" else None,
            "entry_strategy": signal.entry_strategy,
        })
        return order_id

    def _fill_buy(self, signal: Signal, ref_price: float,
                  bar_date: date, strategy_name: str,
                  bar_amount: float = 0.0) -> bool:
        state = self._db.get_portfolio_state()
        if state is None:
            return False
        cash = state["cash"]
        price = ref_price * (1 + self._slippage)

        # Size by deployable cash, capped by the hard risk limits. size_cap is
        # the post-trade single-stock + industry + total cap (the real
        # enforcement point); max_position_pct is kept for the reject reason.
        max_position_pct = self._risk.config.max_position_pct
        total_value = cash + sum(p["quantity"] * (p["current_price"] or p["avg_cost"])
                                 for p in self._db.list_positions())
        size_cap = self._risk.max_additional_buy_value(
            self._build_runtime_portfolio(), signal.stock_code,
            self._stock_industry(signal.stock_code))
        # Shared sizing helper (same as pending fill + backtest + QMT) so the
        # paths can't drift. The single-stock RiskManager cap still applies on
        # top so a sizer can't push past risk limits.
        recent = (self._recent_bars(signal.stock_code)
                  if self._sizer is not None else None)
        target_value = compute_buy_target_value(
            cash=cash, total_value=total_value, strength=signal.strength,
            size_cap=size_cap, code=signal.stock_code,
            sizer=self._sizer, recent_bars=recent)
        commission_est = self._commission.calculate(price, 100, Direction.BUY)
        affordable_lots = int(target_value / (price * 100 + commission_est))
        if affordable_lots < 1:
            # Distinguish risk-driven rejection (one lot already exceeds the
            # per-stock cap) from genuine cash starvation.
            if size_cap < (price * 100 + commission_est):
                reason = (f"Position cap {max_position_pct:.1%} of portfolio "
                          f"smaller than a 100-share lot")
                self._record_order(signal, strategy_name,
                                   status="rejected", reason=reason)
                self._db.log_decision(
                    "risk_reject",
                    f"风控拒绝 buy {signal.stock_code}: {reason}",
                    code=signal.stock_code,
                    details={"signal_reason": signal.reason,
                             "size_cap": size_cap, "price": price})
                return False
            self._record_order(signal, strategy_name,
                               status="rejected", reason="cash too low")
            return False
        quantity = affordable_lots * 100
        # Capacity cap (B1): ≤ participation of the bar's turnover.
        vcap = max_fill_shares(bar_amount, price)
        if vcap is not None:
            quantity = min(quantity, vcap)
        if quantity < 100:
            self._record_order(signal, strategy_name, status="rejected",
                               reason="bar volume cap too tight")
            return False
        cost = price * quantity
        commission = self._commission.calculate(price, quantity, Direction.BUY)
        if cost + commission > cash:
            quantity -= 100
            if quantity <= 0:
                self._record_order(signal, strategy_name,
                                   status="rejected", reason="cash too low")
                return False
            cost = price * quantity
            commission = self._commission.calculate(price, quantity, Direction.BUY)

        new_cash = cash - cost - commission
        self._db.update_cash(new_cash)

        existing = next((p for p in self._db.list_positions()
                         if p["code"] == signal.stock_code), None)
        if existing:
            total_qty = existing["quantity"] + quantity
            avg = (existing["avg_cost"] * existing["quantity"] + price * quantity) / total_qty
            prev_frozen = (existing.get("frozen_qty") or 0) \
                if existing.get("frozen_date") == bar_date else 0
            # entry_strategy=None preserves the original owner on add-ons.
            self._db.upsert_position(signal.stock_code, total_qty, avg,
                                     ref_price, existing["buy_date"] or bar_date,
                                     frozen_qty=int(prev_frozen) + quantity,
                                     frozen_date=bar_date)
        else:
            self._db.upsert_position(signal.stock_code, quantity, price,
                                     ref_price, bar_date,
                                     frozen_qty=quantity, frozen_date=bar_date,
                                     entry_strategy=signal.entry_strategy)

        order_id = self._record_order(
            signal, strategy_name,
            status="filled", filled_price=price,
            filled_quantity=quantity, quantity=quantity,
        )
        trade_id = "t_" + uuid.uuid4().hex[:10]
        self._db.insert_trade({
            "trade_id": trade_id, "order_id": order_id,
            "code": signal.stock_code, "direction": "buy",
            "quantity": quantity, "price": price,
            "commission": commission, "strategy_name": strategy_name,
            "trade_date": bar_date.isoformat(),
        })
        self._risk.record_trade(signal.direction)
        self._db.log_decision(
            "trade",
            f"买入 {signal.stock_code} {quantity}股 @ {price:.2f}",
            code=signal.stock_code,
            details={"strategy": strategy_name, "reason": signal.reason,
                     "commission": commission},
        )
        return True

    def _fill_sell(self, signal: Signal, ref_price: float,
                   bar_date: date, strategy_name: str,
                   bar_amount: float = 0.0) -> bool:
        positions = {p["code"]: p for p in self._db.list_positions()}
        pos = positions.get(signal.stock_code)
        if pos is None or pos["quantity"] <= 0:
            self._record_order(signal, strategy_name,
                               status="rejected", reason="no position")
            return False
        # T+1: only the settled portion can be sold today; today's frozen lot
        # stays. Caps like the live venue's can_use_volume (audit F1).
        quantity = self._sellable_qty(pos, bar_date)
        if quantity <= 0:
            self._record_order(signal, strategy_name,
                               status="rejected", reason="T+1 restriction")
            return False
        # Partial-sell: only the 削峰 trim sells a fraction; all other SELLs
        # fully exit (their strength is a confidence score, not a sell fraction).
        if strategy_name == DRIFT_TRIM_STRATEGY:
            quantity = lot_round_strength(quantity, signal.strength)
        # Capacity cap (B1): ≤ participation of the bar's turnover; remainder
        # carries in the position and is re-sold next session.
        vcap = max_fill_shares(bar_amount, ref_price)
        if vcap is not None:
            quantity = min(quantity, vcap)
        if quantity < 100:
            self._record_order(signal, strategy_name, status="rejected",
                               reason="bar volume cap too tight")
            return False

        price = ref_price * (1 - self._slippage)
        revenue = price * quantity
        commission = self._commission.calculate(price, quantity, Direction.SELL)
        net = revenue - commission

        state = self._db.get_portfolio_state()
        if state is None:
            return False
        self._db.update_cash(state["cash"] + net)
        remaining = pos["quantity"] - quantity
        if remaining > 0:
            # Keep only the pre-existing today-frozen lot frozen; leave the
            # settled trim remainder sellable (don't block a same-session exit).
            kept = (int(pos.get("frozen_qty") or 0)
                    if pos.get("frozen_date") == bar_date else 0)
            self._db.upsert_position(
                signal.stock_code, remaining, pos["avg_cost"],
                pos["current_price"] or pos["avg_cost"], pos["buy_date"],
                frozen_qty=min(kept, remaining), frozen_date=bar_date)
        else:
            self._db.delete_position(signal.stock_code)

        order_id = self._record_order(
            signal, strategy_name,
            status="filled", filled_price=price,
            filled_quantity=quantity, quantity=quantity,
        )
        trade_id = "t_" + uuid.uuid4().hex[:10]
        self._db.insert_trade({
            "trade_id": trade_id, "order_id": order_id,
            "code": signal.stock_code, "direction": "sell",
            "quantity": quantity, "price": price,
            "commission": commission, "strategy_name": strategy_name,
            "trade_date": bar_date.isoformat(),
        })
        self._risk.record_trade(signal.direction)
        self._db.log_decision(
            "trade",
            f"卖出 {signal.stock_code} {quantity}股 @ {price:.2f}",
            code=signal.stock_code,
            details={"strategy": strategy_name, "reason": signal.reason,
                     "pnl": (price - pos["avg_cost"]) * quantity},
        )
        return True

    # ----------------------------------------------------------- exits
    def _sync_risk_config(self) -> None:
        """Pull runtime risk thresholds from the DB so edits apply without a
        restart (P0-3). When unset (no row), keep the config the broker was
        built with — don't clobber it with bare defaults."""
        overrides = self._db.get_risk_config()
        if overrides:
            self._risk.config = risk_config_from_dict(overrides)

    def check_exits(self) -> int:
        """Generate sell signals for holdings that hit an exit rule:
        stop-loss, owning-strategy SELL, or trailing take-profit.

        Computes the two inputs RiskManager.check_exits needs — each
        holding's post-entry peak (for the trailing take-profit) and the set
        of codes whose owning strategy now says SELL — then queues/fills the
        resulting sells. Return value matches check_stop_loss: fills in
        immediate mode, queued count in pending mode.
        """
        self._sync_risk_config()
        portfolio = self._build_runtime_portfolio()
        # Refresh prices first — realtime marks in-session (so the intraday
        # guard catches today's stop hits), else latest daily close.
        marks = self._intraday_marks(list(portfolio.positions))
        for code, position in portfolio.positions.items():
            price = marks.get(code)
            if price is None:
                quote = self._latest_close(code)
                price = quote[0] if quote else None
            if price:
                position.current_price = price

        positions = self._db.list_positions()
        peaks = self._compute_peaks(positions)
        strategy_sells = self._compute_strategy_exits(positions)
        atr_ratios = (compute_atr_ratios(self._provider, positions,
                                         self._risk.config.atr_stop_n)
                      if self._risk.config.atr_stop_k > 0 else {})

        sells = self._risk.check_exits(portfolio, peaks=peaks,
                                       strategy_sell_codes=strategy_sells,
                                       atr_ratios=atr_ratios)
        landed = 0
        for s in sells:
            if self.execute_signal(s, strategy_name="risk_exit"):
                landed += 1
        # Concentration trim (削峰, opt-in) — partial sells for names whose
        # weight drifted past the band, excluding names already fully exited
        # above. Rides the same sell path (T+1 / 涨跌停 / lots / partial-sell).
        trims = self._risk.check_drift_trims(
            portfolio, exclude={s.stock_code for s in sells})
        for s in trims:
            if self.execute_signal(s, strategy_name=DRIFT_TRIM_STRATEGY):
                landed += 1
        return landed

    # Back-compat alias — older callers / tests may still call this name.
    def check_stop_loss(self) -> int:
        return self.check_exits()

    def _compute_peaks(self, positions: list[dict]) -> dict[str, float]:
        """Per-code post-entry peak — shared with QmtBroker via exits.py."""
        return compute_peaks(self._db, positions)

    def _compute_strategy_exits(self, positions: list[dict]) -> dict[str, str]:
        """{code: owning entry-strategy name} for holdings now flagging SELL."""
        if not self._risk.config.strategy_exit_enabled:
            return {}
        return compute_strategy_exits(
            self._provider, self._load_strategies(), positions, self._db)

    def _load_strategies(self) -> dict:
        """Lazy-load strategy classes by name (cached). Returns {} if the
        loader/dir is unavailable so exits degrade to stop-loss + TP only."""
        if self._strategy_cache is None:
            self._strategy_cache = load_strategies(self._strategies_dir)
        return self._strategy_cache
