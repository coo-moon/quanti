"""Paper trading broker.

Two fill modes:

  * `fill_mode="pending"` (default, 2026-06-01 onward): every signal is
    queued as a PENDING order. `try_fill_pending_orders()` later fills
    them at the OPEN of the next available trading bar — modelling
    real A-share semantics where orders submitted off-hours go into the
    next-day open auction. Eliminates the misleading "18:49 卖出"
    timestamps from before, and forces T+1 by construction (signals
    can't fill on the same bar they were generated for).

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
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from quanti.risk.protections import ProtectionConfig

from quanti.backtest.commission import AShareCommission
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.base import BrokerResult, PendingFillResult
from quanti.models import BarData, Direction, PriceType, Signal
from quanti.risk.manager import RiskConfig, RiskManager
from quanti.risk.sizer import Sizer
from quanti.utils.market import (
    count_trading_days_between,
    next_trading_bar,
    next_trading_day,
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
    ) -> None:
        """Args:
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
        self._strategy_cache: dict | None = None  # name → strategy instance
        # Idempotent — only writes if no row exists.
        self._db.ensure_portfolio(initial_cash)

    def _entry_allowed(self, signal, portfolio):
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

    # ------------------------------------------------------------ portfolio
    def snapshot_portfolio(self) -> dict:
        """Mark all positions to the latest close, persist a snapshot, return summary."""
        state = self._db.get_portfolio_state() or self._db.ensure_portfolio(0.0)
        cash = state["cash"]
        positions = self._db.list_positions()
        market_value = 0.0
        enriched: list[dict] = []
        latest_d: date | None = None
        for pos in positions:
            quote = self._latest_close(pos["code"])
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
        """Process one signal. Returns True iff the signal landed (either
        filled in immediate mode, or successfully queued in pending mode).

        Pending mode does NOT count as a "fill" in the legacy semantic —
        callers that only care about real fills should consult `BrokerResult`
        from `execute_signals` and check `filled` rather than the bool.
        """
        if self._fill_mode == "immediate":
            return self._execute_signal_immediate(signal, strategy_name)
        return self._queue_pending_signal(signal, strategy_name)

    def execute_signals(self, signals: list[Signal],
                        strategy_name: str = "") -> BrokerResult:
        out = BrokerResult()
        for s in signals:
            out.accepted += 1
            landed = self.execute_signal(s, strategy_name)
            if not landed:
                out.rejected += 1
                continue
            if self._fill_mode == "immediate":
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

        quote = self._latest_close(signal.stock_code)
        if quote is None:
            self._record_order(signal, strategy_name, status="rejected",
                               reason="no market data")
            return False
        ref_price, bar_date = quote

        if signal.direction == Direction.BUY:
            return self._fill_buy(signal, ref_price, bar_date, strategy_name)
        return self._fill_sell(signal, ref_price, bar_date, strategy_name)

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
                created_date = datetime.fromisoformat(created_at).date()
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

            # Pick the price.
            ref_price = float(bar.open if self._fill_basis == "open"
                              else bar.close)
            filled = self._fill_pending(sig, ref_price, bar.date, o)
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
                created_date = datetime.fromisoformat(created_at).date()
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

    def enforce_portfolio_stop(self) -> bool:
        """Portfolio drawdown circuit breaker: if equity is down past
        `portfolio_stop_loss_pct` from its high-water mark, cancel pending +
        flatten everything. Returns True iff it fired (caller halts the agent)."""
        snap = self.snapshot_portfolio()  # also persists today's snapshot
        total = snap["total_value"]
        peak = max(self._db.get_peak_total_value(), total)
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
                      bar_date: date, order_row: dict) -> bool:
        """Fill an existing pending order row at the given price.

        Mirrors _fill_buy / _fill_sell but updates the existing order row
        instead of inserting a new one. On any failure (no cash, T+1,
        no position), the order is moved to 'rejected'.
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
            if self._sizer is not None:
                recent = self._recent_bars(signal.stock_code)
                target_w = self._sizer.target_weight(
                    code=signal.stock_code, signal_strength=signal.strength,
                    recent_bars=recent, portfolio_total_value=total_value)
                sizer_value = total_value * target_w
                target_value = min(sizer_value, size_cap, cash * 0.95)
            else:
                cash_cap = cash * 0.95 * max(min(signal.strength, 1.0), 0.1)
                target_value = min(cash_cap, size_cap)

            commission_est = self._commission.calculate(price, 100, Direction.BUY)
            affordable_lots = int(target_value / (price * 100 + commission_est))
            if affordable_lots < 1:
                self._db.update_order_status(
                    order_id, "rejected",
                    reason="cash or position cap too tight")
                return False
            quantity = affordable_lots * 100
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
                # entry_strategy=None preserves the original owner on add-ons.
                self._db.upsert_position(signal.stock_code, total_qty, avg,
                                         ref_price,
                                         existing["buy_date"] or bar_date)
            else:
                self._db.upsert_position(signal.stock_code, quantity, price,
                                         ref_price, bar_date,
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
            self._risk.record_trade()
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
        # T+1: if the position was opened on the same day the fill bar
        # is for, the SELL can't execute. In pending mode this is rare
        # (queued SELLs always wait for next bar), but a manual same-day
        # BUY immediate-fill could create this edge case.
        if pos["buy_date"] and isinstance(pos["buy_date"], date) \
                and pos["buy_date"] == bar_date:
            self._db.update_order_status(order_id, "rejected",
                                         reason="T+1 restriction at fill")
            return False

        quantity = pos["quantity"]
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
        self._risk.record_trade()
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
                  bar_date: date, strategy_name: str) -> bool:
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
        if self._sizer is not None:
            # Sizer-driven path: convert target portfolio weight to a notional
            # cap. The single-stock RiskManager cap still applies on top so
            # the sizer can't push past risk limits.
            recent = self._recent_bars(signal.stock_code)
            target_w = self._sizer.target_weight(
                code=signal.stock_code,
                signal_strength=signal.strength,
                recent_bars=recent,
                portfolio_total_value=total_value,
            )
            sizer_value = total_value * target_w
            target_value = min(sizer_value, size_cap, cash * 0.95)
        else:
            cash_cap = cash * 0.95 * max(min(signal.strength, 1.0), 0.1)
            target_value = min(cash_cap, size_cap)
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
            # entry_strategy=None preserves the original owner on add-ons.
            self._db.upsert_position(signal.stock_code, total_qty, avg,
                                     ref_price, existing["buy_date"] or bar_date)
        else:
            self._db.upsert_position(signal.stock_code, quantity, price,
                                     ref_price, bar_date,
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
        self._risk.record_trade()
        self._db.log_decision(
            "trade",
            f"买入 {signal.stock_code} {quantity}股 @ {price:.2f}",
            code=signal.stock_code,
            details={"strategy": strategy_name, "reason": signal.reason,
                     "commission": commission},
        )
        return True

    def _fill_sell(self, signal: Signal, ref_price: float,
                   bar_date: date, strategy_name: str) -> bool:
        positions = {p["code"]: p for p in self._db.list_positions()}
        pos = positions.get(signal.stock_code)
        if pos is None or pos["quantity"] <= 0:
            self._record_order(signal, strategy_name,
                               status="rejected", reason="no position")
            return False
        # T+1: cannot sell if bought today
        if pos["buy_date"] and isinstance(pos["buy_date"], date) and pos["buy_date"] == bar_date:
            self._record_order(signal, strategy_name,
                               status="rejected", reason="T+1 restriction")
            return False

        quantity = pos["quantity"]
        price = ref_price * (1 - self._slippage)
        revenue = price * quantity
        commission = self._commission.calculate(price, quantity, Direction.SELL)
        net = revenue - commission

        state = self._db.get_portfolio_state()
        if state is None:
            return False
        self._db.update_cash(state["cash"] + net)
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
        self._risk.record_trade()
        self._db.log_decision(
            "trade",
            f"卖出 {signal.stock_code} {quantity}股 @ {price:.2f}",
            code=signal.stock_code,
            details={"strategy": strategy_name, "reason": signal.reason,
                     "pnl": (price - pos["avg_cost"]) * quantity},
        )
        return True

    # ----------------------------------------------------------- exits
    def check_exits(self) -> int:
        """Generate sell signals for holdings that hit an exit rule:
        stop-loss, owning-strategy SELL, or trailing take-profit.

        Computes the two inputs RiskManager.check_exits needs — each
        holding's post-entry peak (for the trailing take-profit) and the set
        of codes whose owning strategy now says SELL — then queues/fills the
        resulting sells. Return value matches check_stop_loss: fills in
        immediate mode, queued count in pending mode.
        """
        portfolio = self._build_runtime_portfolio()
        # Refresh prices first.
        for code, position in portfolio.positions.items():
            quote = self._latest_close(code)
            if quote:
                position.current_price = quote[0]

        positions = self._db.list_positions()
        peaks = self._compute_peaks(positions)
        strategy_sells = self._compute_strategy_exits(positions)

        sells = self._risk.check_exits(portfolio, peaks=peaks,
                                       strategy_sell_codes=strategy_sells)
        landed = 0
        for s in sells:
            if self.execute_signal(s, strategy_name="risk_exit"):
                landed += 1
        return landed

    # Back-compat alias — older callers / tests may still call this name.
    def check_stop_loss(self) -> int:
        return self.check_exits()

    def _compute_peaks(self, positions: list[dict]) -> dict[str, float]:
        """Per-code highest high since buy_date (post-entry peak)."""
        peaks: dict[str, float] = {}
        for p in positions:
            bd = p.get("buy_date")
            if bd is None:
                continue
            hw = self._db.get_high_water(p["code"], bd)
            if hw is not None:
                peaks[p["code"]] = hw
        return peaks

    def _compute_strategy_exits(self, positions: list[dict]) -> set[str]:
        """Replay each holding's owning entry-strategy over its recent bars;
        return codes whose latest bar emits a SELL. Defaults-only params (v1)
        — close enough for an exit gate, and never raises into the cycle."""
        if not self._risk.config.strategy_exit_enabled:
            return set()
        out: set[str] = set()
        strategies = self._load_strategies()
        if not strategies:
            return out
        end = date.today()
        start = end - timedelta(days=400)
        for p in positions:
            name = p.get("entry_strategy") or ""
            strat_cls = strategies.get(name)
            if strat_cls is None:
                continue
            try:
                bars = self._provider.get_daily_bars(p["code"], start, end)
                if not bars:
                    continue
                strat = strat_cls()
                strat.init(getattr(strat, "params", {}) or {})
                last_signals: list = []
                for bar in bars:
                    last_signals = strat.on_bar(bar) or []
                if any(s.direction == Direction.SELL
                       and s.stock_code == p["code"] for s in last_signals):
                    out.add(p["code"])
            except Exception as e:
                logger.debug("strategy-exit replay skipped for %s/%s: %s",
                             p["code"], name, e)
        return out

    def _load_strategies(self) -> dict:
        """Lazy-load strategy classes by name (cached). Returns {} if the
        loader/dir is unavailable so exits degrade to stop-loss + TP only."""
        if self._strategy_cache is not None:
            return self._strategy_cache
        cache: dict = {}
        try:
            from quanti.strategy.loader import StrategyLoader
            for s in StrategyLoader().load_directory(self._strategies_dir):
                cache[s.name] = type(s)
        except Exception as e:
            logger.debug("strategy load for exits failed: %s", e)
        self._strategy_cache = cache
        return cache
