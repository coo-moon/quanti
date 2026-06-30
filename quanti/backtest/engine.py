"""Event-driven backtesting engine.

Fill model (since 2026-06-20 audit fix): a signal generated from bar *t* fills at
the OPEN of the next available trading bar — never at the same bar's close. This
removes the same-bar look-ahead (you cannot observe the close and trade at it)
and makes the backtest agree with the live path (PaperBroker pending mode also
fills at next-bar open). T+1 is enforced *by construction*: a buy and any sell of
it can never fill on the same day. Untradeable opens (limit-up for buys /
limit-down for sells, or a suspended/no-bar day) are skipped and the signal
waits, up to a short TTL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from quanti.backtest.commission import AShareCommission
from quanti.backtest.metrics import compute_metrics
from quanti.backtest.slippage import FlatSlippage, SlippageModel, coerce
from quanti.data.provider import DataProvider
from quanti.models import BarData, Direction, Portfolio, Position, Signal
from quanti.risk.manager import (
    DRIFT_TRIM_STRATEGY,
    RiskManager,
    STOP_LOSS_REASON_PREFIX,
)
from quanti.risk.protections import ProtectionContext, ProtectionManager
from quanti.risk.sizer import Sizer, compute_buy_target_value
from quanti.strategy.base import BaseStrategy
from quanti.utils.market import lot_round_strength, max_fill_shares, tradable_at_open

# A signal waits at most this many trading bars for a fillable (tradable, has a
# bar) day before being abandoned — mirrors PaperBroker's pending TTL.
_PENDING_TTL_BARS = 3


@dataclass
class TradeRecord:
    """Record of an executed trade."""

    date: date
    stock_code: str
    direction: Direction
    quantity: int
    price: float
    commission: float
    strategy: str
    reason: str = ""  # signal reason — for exits: 止损 / 移动止盈 / 策略离场


@dataclass
class BacktestResult:
    """Results from a backtest run."""

    equity_curve: pd.Series
    trades: list[TradeRecord]
    metrics: dict
    daily_positions: list[dict] = field(default_factory=list)
    skipped_signals: int = 0
    skip_reason: str = ""


class BacktestEngine:
    """Event-driven backtesting engine with A-share rules."""

    def __init__(
        self,
        provider: DataProvider,
        initial_cash: float = 1_000_000.0,
        commission: AShareCommission | None = None,
        slippage: SlippageModel | float | None = None,
        risk_manager: RiskManager | None = None,
        protection_manager: ProtectionManager | None = None,
        sizer: Sizer | None = None,
    ):
        """Args:
            slippage: A `SlippageModel` (FlatSlippage / VolumeImpactSlippage),
                or a float for backward-compat (interpreted as flat fraction
                — e.g. 0.001 == 10 bps). Default: `FlatSlippage(bps=10)` to
                MATCH the live/paper PaperBroker's flat 0.1% — so the backtest
                that ranks strategies fills at the same cost the paper/live
                path uses (audit C4). Pass `VolumeImpactSlippage()` explicitly
                for capacity stress-testing (penalizes large orders); the
                per-bar volume cap (separate) is the place to bound capacity.
            sizer: Optional `Sizer`. Buys are sized by the SAME shared
                `compute_buy_target_value` the live brokers use, so backtest
                and live agree. With no sizer (the production default), a buy
                deploys ``cash*0.95*clamp(signal.strength, 0.1, 1.0)`` capped
                by the per-stock cap — matching PaperBroker's no-sizer path.
        """
        self._provider = provider
        self._initial_cash = initial_cash
        self._commission = commission or AShareCommission()
        if slippage is None:
            self._slippage: SlippageModel = FlatSlippage(bps=10.0)
        else:
            self._slippage = coerce(slippage)
        self._risk = risk_manager
        self._protections = protection_manager
        self._sizer = sizer
        # ADV20 cache (per-run); populated at the top of run().
        self._adv20: dict[str, dict[date, float]] = {}
        # All bars (per-run); populated at the top of run() so the sizer can
        # estimate vol point-in-time without re-querying the provider.
        self._all_bars: dict[str, list[BarData]] = {}

    def clone(self) -> "BacktestEngine":
        """A fresh engine with the same config but its OWN per-run caches AND
        OWN RiskManager / ProtectionManager instances. run() mutates
        self._all_bars / self._adv20 and the risk manager's daily counters, so
        threads must each clone() rather than share one engine. Provider /
        commission / slippage / sizer are stateless (or thread-safe) and reused.
        """
        return BacktestEngine(
            provider=self._provider,
            initial_cash=self._initial_cash,
            commission=self._commission,
            slippage=self._slippage,
            risk_manager=RiskManager(self._risk.config) if self._risk else None,
            protection_manager=(ProtectionManager(self._protections.config)
                                 if self._protections else None),
            sizer=self._sizer,
        )

    def run(
        self,
        strategy: BaseStrategy,
        codes: list[str],
        start: date,
        end: date,
    ) -> BacktestResult:
        """Run backtest for a strategy on given stocks."""
        portfolio = Portfolio(cash=self._initial_cash)
        trades: list[TradeRecord] = []
        equity_values: dict[date, float] = {}
        # Post-entry peak (highest high since entry) per held code, for the
        # trailing take-profit in RiskManager.check_exits.
        peaks: dict[str, float] = {}
        skipped_signals = 0
        # Signals generated on a bar, waiting to fill at the NEXT bar's open.
        # Each: {"signal", "gen_idx", "strategy_name"}.
        pending: list[dict] = []

        # Load all data upfront
        all_bars: dict[str, list[BarData]] = {}
        all_dates: set[date] = set()
        for code in codes:
            bars = self._provider.get_daily_bars(code, start, end)
            all_bars[code] = bars
            for bar in bars:
                all_dates.add(bar.date)
        self._all_bars = all_bars

        sorted_dates = sorted(all_dates)

        # Portfolio drawdown circuit breaker state (mirrors the live path):
        # equity high-water mark + a one-way halt latch once -15% from peak.
        peak_equity = portfolio.total_value
        halted = False

        def _bt_td(a: date, b: date) -> int:
            """Trading-day distance over (a, b] on the backtest's own calendar."""
            if a >= b:
                return 0
            return sum(1 for d in sorted_dates if a < d <= b)

        # Precompute rolling 20-bar ADV (average daily turnover, in 元) per
        # (code, date) for the slippage model, and the prior close per
        # (code, date) for the price-limit tradability gate.
        adv20: dict[str, dict[date, float]] = {}
        prev_close: dict[str, dict[date, float | None]] = {}
        for code, bars in all_bars.items():
            amounts = [float(b.amount or 0) for b in bars]
            adv20[code] = {}
            prev_close[code] = {}
            last_close: float | None = None
            for i, bar in enumerate(bars):
                window = amounts[max(0, i - 19): i + 1]
                adv20[code][bar.date] = sum(window) / len(window) if window else 0.0
                prev_close[code][bar.date] = last_close
                last_close = bar.close
        self._adv20 = adv20

        # Precompute per-(code, date) ATR(n)/close ratio for the ATR-adaptive
        # stop, ONLY when armed (atr_stop_k>0). A ratio is adjust-agnostic, so
        # it's the same number the live path computes; done once here keeps the
        # per-day exit check an O(1) lookup. See RiskManager.check_exits.
        atr_ratio: dict[str, dict[date, float]] = {}
        if self._risk is not None and self._risk.config.atr_stop_k > 0:
            from quanti.factors.technical import compute_atr
            n = self._risk.config.atr_stop_n
            for code, bars in all_bars.items():
                if len(bars) < n + 1:
                    continue
                df = pd.DataFrame({"high": [b.high for b in bars],
                                   "low": [b.low for b in bars],
                                   "close": [b.close for b in bars]})
                ratios = (compute_atr(df, n) / df["close"]).tolist()
                atr_ratio[code] = {b.date: r for b, r in zip(bars, ratios)
                                   if r == r}  # r==r drops NaN warm-up bars

        for idx, current_date in enumerate(sorted_dates):
            if self._risk is not None:
                self._risk.reset_daily()

            # Collect bars for today
            today_bars: dict[str, BarData] = {}
            for code in codes:
                for bar in all_bars[code]:
                    if bar.date == current_date:
                        today_bars[code] = bar
                        break

            # 1) Fill yesterday's pending signals at TODAY's open (sells first
            #    so cash frees up before buys). Next-open + T+1 by construction.
            pending = self._fill_pending(
                pending, idx, current_date, today_bars, prev_close,
                portfolio, trades, peaks)

            # 2) Mark held positions to today's close; track post-entry peaks.
            for code, bar in today_bars.items():
                if code in portfolio.positions:
                    portfolio.positions[code].current_price = bar.close
                    peaks[code] = max(peaks.get(code, 0.0), bar.high)
            for code in list(peaks):
                if code not in portfolio.positions:
                    peaks.pop(code, None)

            # Per-day protection lock (global; only blocks new BUYs). Same pure
            # ProtectionManager as live, fed from in-memory facts bounded to the
            # fact window so the scan stays cheap on long backtests.
            buy_locked = False
            if self._protections is not None and self._protections.config.enabled:
                cfg = self._protections.config
                sg_span = cfg.sg_lock_days + cfg.sg_lookback_days
                md_span = cfg.md_lock_days + cfg.md_lookback_days
                sl_dates = [t.date for t in trades
                            if t.strategy == "risk_exit"
                            and t.reason.startswith(STOP_LOSS_REASON_PREFIX)
                            and _bt_td(t.date, current_date) <= sg_span]
                eq = sorted(equity_values.items())[-md_span:]
                eq.append((current_date, portfolio.total_value))
                # Trailing returns per held name for the correlation guard
                # (only when enabled) — point-in-time via _recent_bars_asof.
                h_rets: dict[str, list[float]] = {}
                if cfg.correlation_guard_enabled:
                    for hcode in portfolio.positions:
                        bars = self._recent_bars_asof(
                            hcode, current_date)[-(cfg.cg_lookback_days + 1):]
                        cl = [float(b.close) for b in bars if b.close]
                        rets = [cl[i] / cl[i - 1] - 1
                                for i in range(1, len(cl)) if cl[i - 1] > 0]
                        if rets:
                            h_rets[hcode] = rets
                ctx = ProtectionContext(
                    today=current_date, stop_loss_exit_dates=sl_dates,
                    equity_series=eq, trading_days_between=_bt_td,
                    holdings_returns=h_rets)
                allowed, _reason = self._protections.check_entry(ctx)
                buy_locked = not allowed

            # 3) Generate today's signals → queue for next-open fill.
            pending_keys = {(p["signal"].stock_code, p["signal"].direction)
                            for p in pending}

            def _queue(sig: Signal, sname: str) -> None:
                key = (sig.stock_code, sig.direction)
                if key in pending_keys:  # dedup: one order per code+direction
                    # A full/forced SELL supersedes a still-pending partial 削峰
                    # trim for the same code, so a carried (capacity-capped) trim
                    # can't shadow a stop / strategy / portfolio-stop exit.
                    if sig.direction == Direction.SELL and sname != DRIFT_TRIM_STRATEGY:
                        for i, p in enumerate(pending):
                            if (p["signal"].stock_code == sig.stock_code
                                    and p["signal"].direction == Direction.SELL
                                    and p["strategy_name"] == DRIFT_TRIM_STRATEGY):
                                pending[i] = {"signal": sig, "gen_idx": idx,
                                              "strategy_name": sname}
                                break
                    return
                pending.append({"signal": sig, "gen_idx": idx,
                                "strategy_name": sname})
                pending_keys.add(key)

            # Portfolio drawdown circuit breaker — mirrors the live path
            # (PaperBroker.enforce_portfolio_stop / runtime tick). Track an
            # equity high-water mark; once equity draws down past
            # portfolio_stop_loss_pct (default -15%), queue a flatten at the
            # next open and HALT — no new entries or strategy trading for the
            # rest of the run. Without this the backtest rides drawdowns the
            # live agent would have cut, mis-stating tail risk (audit C1).
            peak_equity = max(peak_equity, portfolio.total_value)
            if (not halted and self._risk is not None
                    and self._risk.check_portfolio_stop(
                        portfolio.total_value, peak_equity)):
                halted = True
                for hc in list(portfolio.positions):
                    if hc in today_bars:
                        _queue(Signal(stock_code=hc, direction=Direction.SELL,
                                      strength=1.0, reason="组合回撤熔断"),
                               "portfolio_stop")

            if not halted:
                # Risk-driven exits (stop-loss + trailing take-profit). The
                # strategy's OWN sells still flow through on_bar, so we leave
                # strategy_sell_codes empty to avoid double-counting.
                if self._risk is not None:
                    atr_r = {c: atr_ratio[c][current_date]
                             for c in portfolio.positions
                             if c in atr_ratio and current_date in atr_ratio[c]}
                    exit_sells = self._risk.check_exits(
                        portfolio, peaks=peaks, atr_ratios=atr_r)
                    for sl in exit_sells:
                        if sl.stock_code in today_bars:
                            _queue(sl, "risk_exit")
                    # Concentration trim (削峰, opt-in) — backtest parity with
                    # the live brokers; partial sells for names past the band,
                    # excluding names already fully exited above.
                    for tr in self._risk.check_drift_trims(
                            portfolio, exclude={s.stock_code for s in exit_sells}):
                        if tr.stock_code in today_bars:
                            _queue(tr, DRIFT_TRIM_STRATEGY)

                for code, bar in today_bars.items():
                    for signal in strategy.on_bar(bar):
                        if signal.stock_code not in today_bars:
                            continue  # no price data for the target stock
                        if signal.direction == Direction.BUY and buy_locked:
                            skipped_signals += 1
                            continue
                        if self._risk is not None:
                            ok, _ = self._risk.check(signal, portfolio)
                            if not ok:
                                skipped_signals += 1
                                continue
                        _queue(signal, strategy.name)

            # 4) Record equity (marked to today's close).
            equity_values[current_date] = portfolio.total_value

        equity_curve = pd.Series(equity_values).sort_index()
        metrics = compute_metrics(equity_curve) if len(equity_curve) > 1 else {}

        skip_reason = ""
        if skipped_signals > 0 and len(trades) == 0:
            skip_reason = f"策略产生了 {skipped_signals} 个信号但均未成交，可能是初始资金不足（当前 {self._initial_cash:,.0f} 元）"

        return BacktestResult(
            equity_curve=equity_curve,
            trades=trades,
            metrics=metrics,
            skipped_signals=skipped_signals,
            skip_reason=skip_reason,
        )

    def _fill_pending(
        self,
        pending: list[dict],
        idx: int,
        current_date: date,
        today_bars: dict[str, BarData],
        prev_close: dict[str, dict[date, float | None]],
        portfolio: Portfolio,
        trades: list[TradeRecord],
        peaks: dict[str, float],
    ) -> list[dict]:
        """Fill due pending signals at today's open; return the still-pending
        list. Sells fill before buys (free cash first). A signal waits across
        suspended / limit-locked days up to `_PENDING_TTL_BARS`."""
        ordered = ([p for p in pending if p["signal"].direction == Direction.SELL]
                   + [p for p in pending if p["signal"].direction == Direction.BUY])
        survivors: list[dict] = []
        for p in ordered:
            sig = p["signal"]
            code = sig.stock_code
            expired = (idx - p["gen_idx"]) > _PENDING_TTL_BARS
            bar = today_bars.get(code)
            if bar is None:  # suspended today — wait (within TTL)
                if not expired:
                    survivors.append(p)
                continue
            pc = prev_close.get(code, {}).get(current_date)
            if not tradable_at_open(sig.direction, bar, pc):  # limit-locked
                if not expired:
                    survivors.append(p)
                continue
            filled = self._process_signal(sig, bar, portfolio, trades,
                                          current_date, p["strategy_name"])
            # Drop the post-entry peak only on a FULL exit; a partial 削峰 trim
            # leaves shares held, so keep their trailing-TP peak intact.
            if (filled and sig.direction == Direction.SELL
                    and code not in portfolio.positions):
                peaks.pop(code, None)
            # Retry an unfilled BUY (transient cash shortfall) within the TTL;
            # everything else (filled, or a sell with no position) is dropped.
            if not filled and sig.direction == Direction.BUY and not expired:
                survivors.append(p)
        return survivors

    def _process_signal(
        self,
        signal: Signal,
        bar: BarData,
        portfolio: Portfolio,
        trades: list[TradeRecord],
        current_date: date,
        strategy_name: str,
    ) -> bool:
        code = signal.stock_code

        if signal.direction == Direction.BUY:
            filled = self._execute_buy(code, bar, portfolio, trades, current_date,
                                       strategy_name, signal.reason,
                                       signal.strength)
        elif signal.direction == Direction.SELL:
            filled = self._execute_sell(code, bar, portfolio, trades, current_date,
                                        strategy_name, signal.reason,
                                        signal.strength)
        else:
            return False
        if filled and self._risk is not None:
            self._risk.record_trade(signal.direction)
        return filled

    def _recent_bars_asof(self, code: str, as_of: date) -> list[BarData]:
        """Bars for `code` with date <= as_of, for the sizer's vol estimate —
        point-in-time correct (never sees future bars)."""
        return [b for b in self._all_bars.get(code, []) if b.date <= as_of]

    def _execute_buy(
        self,
        code: str,
        bar: BarData,
        portfolio: Portfolio,
        trades: list[TradeRecord],
        current_date: date,
        strategy_name: str,
        reason: str = "",
        strength: float = 1.0,
    ) -> bool:
        adv = self._adv20.get(code, {}).get(current_date, 0.0)

        # Two-pass slippage: estimate qty under base slippage, then re-compute
        # the actual fill price at that qty. Fill at the bar's OPEN (next-open
        # model), not its close.
        est_frac = self._slippage.adjust(
            code=code, price=bar.open, qty=100,
            direction=Direction.BUY, adv20=adv)
        price_est = bar.open * (1 + est_frac)

        # Size via the SAME shared helper the live brokers use, so backtest and
        # live agree (audit C2). size_cap = the post-trade single-stock /
        # industry cap (industry is "" — not available in the backtest provider,
        # so the single-stock cap still binds). No more arbitrary 10000-share
        # cap, and signal.strength now scales the buy exactly as live does.
        size_cap = float("inf")
        if self._risk is not None:
            size_cap = self._risk.max_additional_buy_value(portfolio, code, "")
        recent = (self._recent_bars_asof(code, current_date)
                  if self._sizer is not None else None)
        target_value = compute_buy_target_value(
            cash=portfolio.cash, total_value=portfolio.total_value,
            strength=strength, size_cap=size_cap, code=code,
            sizer=self._sizer, recent_bars=recent)
        commission_est = self._commission.calculate(price_est, 100, Direction.BUY)
        affordable = int(target_value / (price_est * 100 + commission_est)) * 100

        if affordable < 100:
            return False  # Not enough cash / capped out

        quantity = affordable

        # Capacity cap: don't fill more than `participation` of the bar's
        # turnover in one bar (audit B1; 成交额-based, A3-safe). Remainder isn't
        # acquired this bar — a realistic limit on instant fills in thin names.
        cap = max_fill_shares(bar.amount, bar.open)
        if cap is not None:
            quantity = min(quantity, cap)
        if quantity < 100:
            return False

        # Now apply the real slippage at the actual quantity.
        real_frac = self._slippage.adjust(
            code=code, price=bar.open, qty=quantity,
            direction=Direction.BUY, adv20=adv)
        price = bar.open * (1 + real_frac)
        cost = price * quantity
        commission = self._commission.calculate(price, quantity, Direction.BUY)
        total_cost = cost + commission

        # Final affordability check (real slippage may push us over).
        if total_cost > portfolio.cash:
            # Shrink qty to fit. Conservative: drop 100-share lots one at a
            # time until we're back under cash. Costs at most a few iterations.
            while quantity >= 100 and total_cost > portfolio.cash:
                quantity -= 100
                cost = price * quantity
                commission = self._commission.calculate(price, quantity, Direction.BUY)
                total_cost = cost + commission
            if quantity < 100:
                return False

        portfolio.cash -= total_cost

        if code in portfolio.positions:
            pos = portfolio.positions[code]
            total_qty = pos.quantity + quantity
            pos.avg_cost = (pos.avg_cost * pos.quantity + price * quantity) / total_qty
            pos.quantity = total_qty
        else:
            portfolio.positions[code] = Position(
                stock_code=code,
                quantity=quantity,
                avg_cost=price,
                current_price=bar.close,
                buy_date=current_date,
            )

        trades.append(
            TradeRecord(
                date=current_date,
                stock_code=code,
                direction=Direction.BUY,
                quantity=quantity,
                price=price,
                commission=commission,
                strategy=strategy_name,
                reason=reason,
            )
        )
        return True

    def _execute_sell(
        self,
        code: str,
        bar: BarData,
        portfolio: Portfolio,
        trades: list[TradeRecord],
        current_date: date,
        strategy_name: str,
        reason: str = "",
        strength: float = 1.0,
    ) -> bool:
        if code not in portfolio.positions:
            return False

        pos = portfolio.positions[code]
        # T+1 safety net: a lot bought today can't be sold today. The next-open
        # fill model already guarantees this, but guard anyway.
        if pos.buy_date == current_date:
            return False

        # Partial-sell: ONLY the concentration trim (削峰) sells a fraction; every
        # other SELL fully exits regardless of strength (some strategies emit
        # closing SELLs with strength<1.0). Then the capacity cap (B1) limits
        # delivery; remainder carries + re-fires.
        quantity = (lot_round_strength(pos.quantity, strength)
                    if strategy_name == DRIFT_TRIM_STRATEGY else pos.quantity)
        cap = max_fill_shares(bar.amount, bar.open)
        if cap is not None:
            quantity = min(quantity, cap)
        if quantity < 100:
            return False  # too illiquid / sub-lot trim — retry next bar
        adv = self._adv20.get(code, {}).get(current_date, 0.0)
        slip_frac = self._slippage.adjust(
            code=code, price=bar.open, qty=quantity,
            direction=Direction.SELL, adv20=adv)
        price = bar.open * (1 - slip_frac)

        revenue = price * quantity
        commission = self._commission.calculate(price, quantity, Direction.SELL,
                                                trade_date=current_date)
        net_revenue = revenue - commission

        portfolio.cash += net_revenue
        if quantity >= pos.quantity:
            del portfolio.positions[code]
        else:
            pos.quantity -= quantity

        trades.append(
            TradeRecord(
                date=current_date,
                stock_code=code,
                direction=Direction.SELL,
                quantity=quantity,
                price=price,
                commission=commission,
                strategy=strategy_name,
                reason=reason,
            )
        )
        return True
