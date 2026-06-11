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
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from quanti.backtest.commission import AShareCommission
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.models import BarData, Direction, PriceType, Signal
from quanti.risk.manager import RiskConfig, RiskManager
from quanti.risk.sizer import Sizer
from quanti.utils.market import (
    count_trading_days_between,
    next_trading_bar,
)

logger = logging.getLogger(__name__)


@dataclass
class BrokerResult:
    accepted: int = 0
    rejected: int = 0
    filled: int = 0
    pending: int = 0  # NEW: signals that were queued for next-open fill
    reasons: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.reasons is None:
            self.reasons = []


@dataclass
class PendingFillResult:
    """Summary of one try_fill_pending_orders() pass."""
    scanned: int = 0
    filled: int = 0
    rejected: int = 0      # risk re-check failed at fill time
    expired: int = 0       # TTL exceeded without a fillable bar
    still_pending: int = 0
    reasons: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.reasons is None:
            self.reasons = []


class PaperBroker:
    """Stateful paper-trading broker. Single instance per process."""

    def __init__(
        self,
        db: Database,
        provider: DataProvider,
        initial_cash: float = 1_000_000.0,
        commission: AShareCommission | None = None,
        slippage: float = 0.001,
        risk_config: RiskConfig | None = None,
        sizer: Sizer | None = None,
        fill_mode: Literal["pending", "immediate"] = "immediate",
        pending_ttl_trading_days: int = 3,
        fill_price_basis: Literal["open", "close"] = "open",
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
        self._sizer = sizer
        self._fill_mode = fill_mode
        self._pending_ttl_days = pending_ttl_trading_days
        self._fill_basis = fill_price_basis
        # Idempotent — only writes if no row exists.
        self._db.ensure_portfolio(initial_cash)

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
        ok, reason = self._risk.check(signal, portfolio)
        if not ok:
            self._record_order(signal, strategy_name, status="rejected",
                               reason=reason)
            self._db.log_decision(
                "risk_reject",
                f"风控拒绝 {signal.direction.value} {signal.stock_code}: {reason}",
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
        ok, reason = self._risk.check(signal, portfolio)
        if not ok:
            self._record_order(signal, strategy_name, status="rejected",
                               reason=reason)
            self._db.log_decision(
                "risk_reject",
                f"风控拒绝 {signal.direction.value} {signal.stock_code}: {reason}",
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
            # re-run risk, fill at bar.open (+/- slippage).
            sig = Signal(
                stock_code=o["code"],
                direction=Direction(o["direction"]),
                strength=1.0,
                reason=o.get("reason", "") or "pending fill",
            )
            portfolio = self._build_runtime_portfolio()
            ok, reason = self._risk.check(sig, portfolio)
            if not ok:
                self._db.update_order_status(o["order_id"], "rejected",
                                             reason=f"risk at fill: {reason}")
                self._db.log_decision(
                    "risk_reject",
                    f"风控拒绝 (成交时) {sig.direction.value} {sig.stock_code}: {reason}",
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

            max_position_pct = self._risk.config.max_position_pct
            total_value = cash + sum(
                p["quantity"] * (p["current_price"] or p["avg_cost"])
                for p in self._db.list_positions())
            size_cap = total_value * max_position_pct
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
                self._db.upsert_position(signal.stock_code, total_qty, avg,
                                         ref_price,
                                         existing["buy_date"] or bar_date)
            else:
                self._db.upsert_position(signal.stock_code, quantity, price,
                                         ref_price, bar_date)
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
            )
        return portfolio

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
        })
        return order_id

    def _fill_buy(self, signal: Signal, ref_price: float,
                  bar_date: date, strategy_name: str) -> bool:
        state = self._db.get_portfolio_state()
        if state is None:
            return False
        cash = state["cash"]
        price = ref_price * (1 + self._slippage)

        # Size: use signal.strength as % of cash to deploy, capped by single-stock risk.
        max_position_pct = self._risk.config.max_position_pct
        total_value = cash + sum(p["quantity"] * (p["current_price"] or p["avg_cost"])
                                 for p in self._db.list_positions())
        size_cap = total_value * max_position_pct
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
            self._db.upsert_position(signal.stock_code, total_qty, avg,
                                     ref_price, existing["buy_date"] or bar_date)
        else:
            self._db.upsert_position(signal.stock_code, quantity, price,
                                     ref_price, bar_date)

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

    # ----------------------------------------------------------- stop loss
    def check_stop_loss(self) -> int:
        """Generate sell signals for positions that breached the stop-loss.

        In immediate mode the return value is the number of fills (legacy).
        In pending mode it's the number of stops successfully queued — the
        actual fills happen in the next `try_fill_pending_orders` pass once
        a new bar lands. Callers that care about fills vs. queued should
        look at the order log directly.
        """
        portfolio = self._build_runtime_portfolio()
        # Refresh prices first
        for code, position in portfolio.positions.items():
            quote = self._latest_close(code)
            if quote:
                position.current_price = quote[0]
        sells = self._risk.check_stop_loss(portfolio)
        landed = 0
        for s in sells:
            if self.execute_signal(s, strategy_name="risk_stop_loss"):
                landed += 1
        return landed
