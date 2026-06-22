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
from typing import TYPE_CHECKING

from quanti.bridge_client import BridgeClient, DEFAULT_BRIDGE_URL, HttpBridgeClient
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.base import BrokerResult, PendingFillResult
from quanti.models import Direction, Portfolio, Position, PriceType, Signal
from quanti.risk.manager import RiskConfig, RiskManager
from quanti.risk.sizer import compute_buy_target_value

if TYPE_CHECKING:
    from quanti.risk.protections import ProtectionConfig

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._db = db
        self._provider = provider
        self._client: BridgeClient = client or HttpBridgeClient(bridge_url)
        self._initial_cash = initial_cash
        self._risk = RiskManager(risk_config)
        from quanti.risk.protections import ProtectionConfig, ProtectionManager
        self._protections = ProtectionManager(
            protection_config if protection_config is not None
            else ProtectionConfig())
        self._slippage = slippage
        self._strategies_dir = strategies_dir

    # ----------------------------------------------------------- health
    def is_connected(self) -> bool:
        """True iff the bridge is reachable and reports ok. (A stricter live
        gate also requires ``trader_connected``; left permissive while the
        bridge runs in mock mode.)"""
        try:
            h = self._client.get("/health")
        except Exception as e:  # noqa: BLE001 - unreachable bridge == down
            logger.warning("qmt-bridge unreachable: %s", e)
            return False
        return bool(h.get("ok"))

    # ------------------------------------------------------- reconciliation
    def _reconciled_portfolio(self) -> tuple[Portfolio, dict[str, int]]:
        """Build a Portfolio from the *broker* account — the source of truth —
        plus a per-code sellable (T+1 `can_use_volume`) map. A lot bought today
        is frozen, so SELLs must be capped at the sellable amount, not the total
        held; callers read the second element for that."""
        asset = self._client.get("/trader/asset")
        rows = self._client.get("/trader/positions").get("positions", [])
        pf = Portfolio(cash=float(asset.get("cash", 0.0)))
        sellable: dict[str, int] = {}
        for p in rows:
            vol = int(p.get("volume", 0))
            if vol <= 0:
                continue
            avg = float(p.get("avg_price", 0.0))
            cur = self._current_price(p, vol, avg)
            stock = self._db.get_stock(p["code"])
            pf.positions[p["code"]] = Position(
                stock_code=p["code"], quantity=vol, avg_cost=avg,
                current_price=cur, buy_date=None,
                industry=stock.industry if stock else "")
            sellable[p["code"]] = int(p.get("can_use_volume", 0))
        return pf, sellable

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
                "quantity": vol, "avg_cost": avg, "current_price": cur,
                "price_date": None, "market_value": cur * vol,
                "sellable": int(p.get("can_use_volume", 0)),  # T+1-free qty
                "pnl": (cur - avg) * vol,
                "pnl_pct": (cur - avg) / avg if avg else 0.0,
            })
        # Persist the equity snapshot so the portfolio drawdown high-water mark
        # accumulates for the circuit breaker (and the UI equity curve).
        self._db.save_portfolio_snapshot(date.today(), cash, market_value, total)
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
        """Risk-check → size → submit. Returns (landed, status, reason) with
        status in {'filled','pending','rejected'}.

        RiskManager runs BEFORE every submit (red line). SELL volume is capped
        at the T+1-sellable quantity (`can_use_volume`) so a lot bought today
        is never over-sold — a frozen position is skipped, not submitted."""
        portfolio, sellable = self._reconciled_portfolio()
        ok, reason, kind = self._entry_allowed(signal, portfolio)
        if not ok:
            self._mirror_order(signal, strategy_name, status="rejected",
                               reason=reason)
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
                                   reason=r)
                return False, "rejected", r
        else:  # SELL — only the T+1-sellable portion can leave today
            pos = portfolio.positions.get(signal.stock_code)
            if pos is None or pos.quantity <= 0:
                self._mirror_order(signal, strategy_name, status="rejected",
                                   reason="no position")
                return False, "rejected", "no position"
            can_sell = min(pos.quantity, sellable.get(signal.stock_code, 0))
            if can_sell <= 0:
                r = "T+1: no sellable quantity today"
                self._mirror_order(signal, strategy_name, status="rejected",
                                   reason=r)
                self._db.log_decision(
                    "order_skipped_t1",
                    f"跳过(实盘) 卖出 {signal.stock_code}: 当日买入 T+1 不可卖",
                    code=signal.stock_code, details={"venue": "qmt"})
                return False, "rejected", r
            volume = can_sell
            # Price off the live quote (like BUY via _latest_price), not the
            # position's current_price — which, before C5, was the cost basis.
            # Falls back to the reconciled current_price if the quote is down.
            last = self._latest_price(signal.stock_code)
            ref = last if last > 0 else pos.current_price
            price = ref * (1 - self._slippage)

        res = self._client.post("/trader/order", {
            "code": signal.stock_code, "direction": signal.direction.value,
            "volume": int(volume), "price": round(float(price), 3),
            "price_type": "limit" if price else "market"})
        accepted = bool(res.get("ok"))
        filled = accepted and res.get("status") == "filled"
        status = "filled" if filled else ("pending" if accepted else "rejected")
        if accepted:
            # Count the order against the daily-trade hard cap — the same floor
            # PaperBroker enforces (paper_broker records on fill). reset_daily()
            # at session start + seeding the count from /trader/trades is phase-③.
            self._risk.record_trade()
        self._mirror_order(
            signal, strategy_name, status=status,
            reason=res.get("msg", "") or signal.reason,
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
                     "msg": res.get("msg", "")})
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
        except Exception:  # noqa: BLE001 - fall back to stored close
            pass
        bars = self._provider.get_daily_bars(
            code, date(2000, 1, 1), date.today())
        return float(bars[-1].close) if bars else 0.0

    # ------------------------------------------------ pending / reconcile
    def try_fill_pending_orders(self) -> PendingFillResult:
        """Reconcile the local order mirror against the venue's order book.

        NOTE: real fills arrive via xtquant callbacks (bridge phase ③); this
        poller reads the bridge's reconciled order list. In mock mode orders
        fill at submit, so there's typically nothing pending to advance."""
        out = PendingFillResult()
        try:
            venue = {o["order_id"]: o
                     for o in self._client.get("/trader/orders").get("orders", [])}
        except Exception as e:  # noqa: BLE001
            logger.warning("reconcile failed (bridge down?): %s", e)
            return out
        local_pending = self._db.list_orders(limit=1000, status="pending")
        out.scanned = len(local_pending)
        for o in local_pending:
            vid = (o.get("entry_strategy") or "")  # venue id mirrored here
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

    def check_exits(self) -> int:
        """Stop-loss exits against reconciled positions (phase ⑤ adds trailing
        TP + owning-strategy replay + intraday triggering)."""
        portfolio, _ = self._reconciled_portfolio()
        sells = self._risk.check_exits(portfolio, peaks={},
                                       strategy_sell_codes=set())
        landed = 0
        for s in sells:
            if self.execute_signal(s, strategy_name="risk_exit"):
                landed += 1
        return landed

    def pending_orders_detail(self) -> list[dict]:
        """Open orders at the venue, shaped for the UI."""
        try:
            orders = self._client.get("/trader/orders").get("orders", [])
        except Exception:  # noqa: BLE001
            return []
        out = []
        for o in orders:
            if o.get("status") not in ("pending", "accepted"):
                continue
            stock = self._db.get_stock(o["code"])
            out.append({
                "order_id": o["order_id"], "code": o["code"],
                "name": stock.name if stock else o["code"],
                "direction": o.get("direction", ""),
                "quantity": o.get("volume", 0),
                "reason": "", "created_at": o.get("created_at", ""),
                "expected_fill_date": None, "fill_price_basis": "venue",
                "bar_available": True, "trading_days_pending": None,
                "ttl_trading_days": None})
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
        an exit was actually submitted for (the contract in base.py)."""
        pf, _ = self._reconciled_portfolio()
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

        Reads the high-water mark BEFORE snapshot_portfolio() overwrites today's
        snapshot row, so a same-day top-then-drop still trips it (audit G3/L2)."""
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
                      quantity: int = 0) -> str:
        """Persist a local mirror row (audit/UI only — broker is truth).

        The venue order id is stashed in ``entry_strategy`` so the reconciler
        can match local rows to the venue book without a schema change."""
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
