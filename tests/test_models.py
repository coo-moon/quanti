"""Tests for core domain models."""

from datetime import date

import pytest

from quanti.models import (
    BarData,
    Direction,
    Order,
    OrderStatus,
    Portfolio,
    Position,
    Signal,
    StockInfo,
)


class TestStockInfo:
    def test_symbol(self):
        stock = StockInfo(
            code="000001", name="平安银行", exchange="SZ", list_date=date(1991, 4, 3)
        )
        assert stock.symbol == "000001.SZ"

    def test_frozen(self):
        stock = StockInfo(
            code="000001", name="平安银行", exchange="SZ", list_date=date(1991, 4, 3)
        )
        try:
            stock.code = "000002"
            assert False, "Should be frozen"
        except AttributeError:
            pass


class TestBarData:
    def test_creation(self):
        bar = BarData(
            code="000001",
            date=date(2024, 1, 2),
            open=10.0,
            high=10.5,
            low=9.8,
            close=10.2,
            volume=1_000_000,
            amount=10_200_000,
        )
        assert bar.close == 10.2


class TestSignal:
    def test_creation(self):
        signal = Signal(
            stock_code="000001",
            direction=Direction.BUY,
            strength=0.8,
            reason="MA golden cross",
        )
        assert signal.direction == Direction.BUY
        assert signal.strength == 0.8


class TestOrder:
    def test_defaults(self):
        order = Order(
            stock_code="000001", direction=Direction.BUY, quantity=100
        )
        assert order.status == OrderStatus.PENDING
        assert order.filled_price == 0.0


class TestPosition:
    def test_pnl(self):
        pos = Position(
            stock_code="000001", quantity=1000, avg_cost=10.0, current_price=12.0
        )
        assert pos.market_value == 12_000.0
        assert pos.pnl == 2_000.0
        assert pos.pnl_pct == pytest.approx(0.2)

    def test_zero_cost_pnl(self):
        pos = Position(stock_code="000001", quantity=0, avg_cost=0.0, current_price=10.0)
        assert pos.pnl_pct == 0.0


class TestPortfolio:
    def test_total_value(self):
        portfolio = Portfolio(
            cash=100_000.0,
            positions={
                "000001": Position(
                    stock_code="000001", quantity=1000, avg_cost=10.0, current_price=12.0
                ),
            },
        )
        assert portfolio.market_value == 12_000.0
        assert portfolio.total_value == 112_000.0

    def test_empty_portfolio(self):
        portfolio = Portfolio(cash=100_000.0)
        assert portfolio.market_value == 0.0
        assert portfolio.total_value == 100_000.0
