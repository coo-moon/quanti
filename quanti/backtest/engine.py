"""Event-driven backtesting engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from quanti.backtest.commission import AShareCommission
from quanti.backtest.metrics import compute_metrics
from quanti.data.provider import DataProvider
from quanti.models import BarData, Direction, Order, OrderStatus, Portfolio, Position, Signal
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


class BacktestEngine:
    """Event-driven backtesting engine with A-share rules."""

    def __init__(
        self,
        provider: DataProvider,
        initial_cash: float = 1_000_000.0,
        commission: AShareCommission | None = None,
        slippage: float = 0.001,  # 0.1%
    ):
        self._provider = provider
        self._initial_cash = initial_cash
        self._commission = commission or AShareCommission()
        self._slippage = slippage

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

        # Load all data upfront
        all_bars: dict[str, list[BarData]] = {}
        all_dates: set[date] = set()
        for code in codes:
            bars = self._provider.get_daily_bars(code, start, end)
            all_bars[code] = bars
            for bar in bars:
                all_dates.add(bar.date)

        sorted_dates = sorted(all_dates)

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

            # Generate signals from strategy
            for code, bar in today_bars.items():
                signals = strategy.on_bar(bar)
                for signal in signals:
                    self._process_signal(
                        signal, bar, portfolio, trades, current_date, bought_today, strategy.name
                    )

            # Record equity
            equity_values[current_date] = portfolio.total_value

        equity_curve = pd.Series(equity_values).sort_index()
        metrics = compute_metrics(equity_curve) if len(equity_curve) > 1 else {}

        return BacktestResult(
            equity_curve=equity_curve,
            trades=trades,
            metrics=metrics,
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
    ) -> None:
        code = signal.stock_code

        if signal.direction == Direction.BUY:
            self._execute_buy(code, bar, portfolio, trades, current_date, bought_today, strategy_name)
        elif signal.direction == Direction.SELL:
            # T+1 rule: cannot sell stocks bought today
            if code in bought_today:
                return
            self._execute_sell(code, bar, portfolio, trades, current_date, strategy_name)

    def _execute_buy(
        self,
        code: str,
        bar: BarData,
        portfolio: Portfolio,
        trades: list[TradeRecord],
        current_date: date,
        bought_today: set[str],
        strategy_name: str,
    ) -> None:
        # Apply slippage
        price = bar.close * (1 + self._slippage)

        # Calculate affordable quantity (round down to 100)
        max_spend = portfolio.cash * 0.95  # Keep 5% cash buffer
        commission_est = self._commission.calculate(price, 100, Direction.BUY)
        affordable = int(max_spend / (price * 100 + commission_est)) * 100

        if affordable < 100:
            return  # Not enough cash

        quantity = min(affordable, 10000)  # Cap at 10000 shares per trade
        cost = price * quantity
        commission = self._commission.calculate(price, quantity, Direction.BUY)
        total_cost = cost + commission

        if total_cost > portfolio.cash:
            return

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

    def _execute_sell(
        self,
        code: str,
        bar: BarData,
        portfolio: Portfolio,
        trades: list[TradeRecord],
        current_date: date,
        strategy_name: str,
    ) -> None:
        if code not in portfolio.positions:
            return

        pos = portfolio.positions[code]
        quantity = pos.quantity
        price = bar.close * (1 - self._slippage)

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
