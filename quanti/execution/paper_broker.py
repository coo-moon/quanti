"""Paper trading broker.

Accepts Signal objects, applies risk control, simulates fills using the latest
available bar (close price + slippage), and persists everything into the
database (orders, trades, positions, cash). All A-share rules (T+1, 100-share
lots, commission, stamp tax) are honored so live behavior mirrors the backtest.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from quanti.backtest.commission import AShareCommission
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.models import Direction, PriceType, Signal
from quanti.risk.manager import RiskConfig, RiskManager

logger = logging.getLogger(__name__)


@dataclass
class BrokerResult:
    accepted: int = 0
    rejected: int = 0
    filled: int = 0
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
    ) -> None:
        self._db = db
        self._provider = provider
        self._commission = commission or AShareCommission()
        self._slippage = slippage
        self._risk = RiskManager(risk_config)
        # Idempotent — only writes if no row exists.
        self._db.ensure_portfolio(initial_cash)

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
        """Run risk + fill simulation for one signal. Returns True if filled."""
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

    def execute_signals(self, signals: list[Signal],
                        strategy_name: str = "") -> BrokerResult:
        out = BrokerResult()
        for s in signals:
            out.accepted += 1
            if self.execute_signal(s, strategy_name):
                out.filled += 1
            else:
                out.rejected += 1
        # Always re-snapshot after a batch so the dashboard is fresh.
        self.snapshot_portfolio()
        return out

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
        """Generate sell signals for positions that breached the stop-loss."""
        portfolio = self._build_runtime_portfolio()
        # Refresh prices first
        for code, position in portfolio.positions.items():
            quote = self._latest_close(code)
            if quote:
                position.current_price = quote[0]
        sells = self._risk.check_stop_loss(portfolio)
        filled = 0
        for s in sells:
            if self.execute_signal(s, strategy_name="risk_stop_loss"):
                filled += 1
        return filled
