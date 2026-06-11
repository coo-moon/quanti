"""Event-driven backtesting engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from quanti.backtest.commission import AShareCommission
from quanti.backtest.metrics import compute_metrics
from quanti.backtest.slippage import SlippageModel, VolumeImpactSlippage, coerce
from quanti.data.provider import DataProvider
from quanti.models import BarData, Direction, Portfolio, Position, Signal
from quanti.risk.manager import RiskManager
from quanti.strategy.base import BaseStrategy


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
    ):
        """Args:
            slippage: A `SlippageModel` (FlatSlippage / VolumeImpactSlippage),
                or a float for backward-compat (interpreted as flat fraction
                — e.g. 0.001 == 10 bps). Default: `VolumeImpactSlippage()`,
                which calibrates to ~10 bps at 1% participation — equivalent
                to the old 0.1% flat for small orders, but penalizes the
                kind of large orders that lie about real fills in backtest.
        """
        self._provider = provider
        self._initial_cash = initial_cash
        self._commission = commission or AShareCommission()
        if slippage is None:
            self._slippage: SlippageModel = VolumeImpactSlippage()
        else:
            self._slippage = coerce(slippage)
        self._risk = risk_manager
        # ADV20 cache (per-run); populated at the top of run().
        self._adv20: dict[str, dict[date, float]] = {}

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
        # Track T+1: stocks bought today cannot be sold today
        bought_today: set[str] = set()
        skipped_signals = 0

        # Load all data upfront
        all_bars: dict[str, list[BarData]] = {}
        all_dates: set[date] = set()
        for code in codes:
            bars = self._provider.get_daily_bars(code, start, end)
            all_bars[code] = bars
            for bar in bars:
                all_dates.add(bar.date)

        sorted_dates = sorted(all_dates)

        # Precompute rolling 20-bar ADV (average daily turnover, in 元) per
        # (code, date). The slippage model uses this to scale impact with the
        # order's participation rate. Missing/zero turnover → adv20 stays 0
        # and the slippage model gracefully degrades to base bps.
        adv20: dict[str, dict[date, float]] = {}
        for code, bars in all_bars.items():
            amounts = [float(b.amount or 0) for b in bars]
            adv20[code] = {}
            for i, bar in enumerate(bars):
                window = amounts[max(0, i - 19): i + 1]
                adv20[code][bar.date] = sum(window) / len(window) if window else 0.0
        self._adv20 = adv20

        for current_date in sorted_dates:
            bought_today.clear()

            # Collect bars for today
            today_bars: dict[str, BarData] = {}
            for code in codes:
                for bar in all_bars[code]:
                    if bar.date == current_date:
                        today_bars[code] = bar
                        break

            # Update position prices
            for code, bar in today_bars.items():
                if code in portfolio.positions:
                    portfolio.positions[code].current_price = bar.close

            # Risk-driven sells first (stop-loss) so cash frees up before buys
            if self._risk is not None:
                self._risk.reset_daily()
                for sl in self._risk.check_stop_loss(portfolio):
                    sl_bar = today_bars.get(sl.stock_code)
                    if sl_bar is None:
                        continue
                    self._process_signal(sl, sl_bar, portfolio, trades,
                                         current_date, bought_today, "risk_stop_loss")

            # Generate signals from strategy
            for code, bar in today_bars.items():
                signals = strategy.on_bar(bar)
                for signal in signals:
                    # Use the bar matching the signal's stock_code, not the triggering bar
                    signal_bar = today_bars.get(signal.stock_code, bar)
                    if signal_bar.code != signal.stock_code:
                        continue  # Skip if we don't have price data for the target stock
                    if self._risk is not None:
                        ok, _ = self._risk.check(signal, portfolio)
                        if not ok:
                            skipped_signals += 1
                            continue
                    executed = self._process_signal(
                        signal, signal_bar, portfolio, trades, current_date, bought_today, strategy.name
                    )
                    if not executed:
                        skipped_signals += 1
                    elif self._risk is not None:
                        self._risk.record_trade()

            # Record equity
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

    def _process_signal(
        self,
        signal: Signal,
        bar: BarData,
        portfolio: Portfolio,
        trades: list[TradeRecord],
        current_date: date,
        bought_today: set[str],
        strategy_name: str,
    ) -> bool:
        code = signal.stock_code

        if signal.direction == Direction.BUY:
            return self._execute_buy(code, bar, portfolio, trades, current_date, bought_today, strategy_name)
        elif signal.direction == Direction.SELL:
            # T+1 rule: cannot sell stocks bought today
            if code in bought_today:
                return False
            return self._execute_sell(code, bar, portfolio, trades, current_date, strategy_name)
        return False

    def _execute_buy(
        self,
        code: str,
        bar: BarData,
        portfolio: Portfolio,
        trades: list[TradeRecord],
        current_date: date,
        bought_today: set[str],
        strategy_name: str,
    ) -> bool:
        adv = self._adv20.get(code, {}).get(current_date, 0.0)

        # Two-pass slippage: estimate qty under base slippage, then re-compute
        # the actual fill price at that qty. The volume-impact model has a
        # tiny self-consistency loop (bigger qty → bigger slippage → smaller
        # affordable qty) — one iteration of fixed-point is close enough for
        # backtest purposes.
        est_frac = self._slippage.adjust(
            code=code, price=bar.close, qty=100,
            direction=Direction.BUY, adv20=adv)
        price_est = bar.close * (1 + est_frac)

        max_spend = portfolio.cash * 0.95  # Keep 5% cash buffer
        commission_est = self._commission.calculate(price_est, 100, Direction.BUY)
        affordable = int(max_spend / (price_est * 100 + commission_est)) * 100

        if affordable < 100:
            return False  # Not enough cash

        quantity = min(affordable, 10000)  # Cap at 10000 shares per trade

        # Now apply the real slippage at the actual quantity.
        real_frac = self._slippage.adjust(
            code=code, price=bar.close, qty=quantity,
            direction=Direction.BUY, adv20=adv)
        price = bar.close * (1 + real_frac)
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
        bought_today.add(code)

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
    ) -> bool:
        if code not in portfolio.positions:
            return False

        pos = portfolio.positions[code]
        quantity = pos.quantity
        adv = self._adv20.get(code, {}).get(current_date, 0.0)
        slip_frac = self._slippage.adjust(
            code=code, price=bar.close, qty=quantity,
            direction=Direction.SELL, adv20=adv)
        price = bar.close * (1 - slip_frac)

        revenue = price * quantity
        commission = self._commission.calculate(price, quantity, Direction.SELL)
        net_revenue = revenue - commission

        portfolio.cash += net_revenue
        del portfolio.positions[code]

        trades.append(
            TradeRecord(
                date=current_date,
                stock_code=code,
                direction=Direction.SELL,
                quantity=quantity,
                price=price,
                commission=commission,
                strategy=strategy_name,
            )
        )
        return True
