"""Unit tests for the shared daily price-limit (涨跌停) tradability gate.

These pin the pure functions in quanti/utils/market.py that both the backtest
engine and the live brokers use, so 'what can fill' is defined in one place
(audit C3). Board limits: main board 10%, STAR/ChiNext 20%, BSE 30%.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.models import BarData, Direction
from quanti.utils.market import (
    board_limit_pct,
    prev_bar_close,
    tradable_at_close,
    tradable_at_open,
)


def _bar(code: str, open_: float, close: float = 0.0) -> BarData:
    return BarData(code=code, date=date(2024, 1, 2), open=open_,
                   high=max(open_, close) + 0.1, low=min(open_, close) - 0.1,
                   close=close or open_, volume=1e6, amount=1e7, turnover=1.0)


class TestBoardLimitPct:
    def test_main_board(self):
        assert board_limit_pct("600519") == 0.10
        assert board_limit_pct("000001") == 0.10

    def test_star_and_chinext(self):
        assert board_limit_pct("688981") == 0.20
        assert board_limit_pct("300750") == 0.20
        assert board_limit_pct("301001") == 0.20

    def test_beijing_exchange(self):
        assert board_limit_pct("830799") == 0.30
        assert board_limit_pct("430047") == 0.30
        assert board_limit_pct("920002") == 0.30


class TestTradableAtOpen:
    def test_buy_blocked_at_limit_up(self):
        # prev 10.00, main board +10% = 11.00; open 11.00 is the lock.
        assert tradable_at_open(Direction.BUY, _bar("600000", 11.00), 10.00) is False

    def test_buy_allowed_below_limit_up(self):
        assert tradable_at_open(Direction.BUY, _bar("600000", 10.90), 10.00) is True

    def test_sell_blocked_at_limit_down(self):
        assert tradable_at_open(Direction.SELL, _bar("600000", 9.00), 10.00) is False

    def test_sell_allowed_above_limit_down(self):
        assert tradable_at_open(Direction.SELL, _bar("600000", 9.10), 10.00) is True

    def test_chinext_wider_band(self):
        # +15% open: blocked on main board (10%), allowed on ChiNext (20%).
        assert tradable_at_open(Direction.BUY, _bar("600000", 11.50), 10.00) is False
        assert tradable_at_open(Direction.BUY, _bar("300001", 11.50), 10.00) is True

    def test_no_prev_close_allows(self):
        assert tradable_at_open(Direction.BUY, _bar("600000", 99.0), None) is True
        assert tradable_at_open(Direction.BUY, _bar("600000", 99.0), 0.0) is True


class TestTradableAtClose:
    def test_gates_on_close_not_open(self):
        # Open normal (10.0) but close sealed limit-up (11.0): close-gate blocks
        # BUY, open-gate would not.
        bar = _bar("600000", open_=10.0, close=11.0)
        assert tradable_at_close(Direction.BUY, bar, 10.0) is False
        assert tradable_at_open(Direction.BUY, bar, 10.0) is True


def test_prev_bar_close(tmp_path):
    db = Database(str(tmp_path / "pc.db"))
    db.initialize()
    dates = pd.bdate_range("2024-01-02", periods=5)
    df = pd.DataFrame({
        "code": "600000", "date": [d.date() for d in dates],
        "open": [10, 11, 12, 13, 14], "high": [10, 11, 12, 13, 14],
        "low": [10, 11, 12, 13, 14], "close": [10.0, 11.0, 12.0, 13.0, 14.0],
        "volume": 1e6, "amount": 1e7, "turnover": 1.0})
    db.save_daily_quotes(df)
    provider = DataProvider(db)
    fill_date = dates[4].date()  # 5th bar
    # Prior close is the 4th bar's close = 13.0.
    assert prev_bar_close(provider, "600000", fill_date) == 13.0
    # No bar before the first → None.
    assert prev_bar_close(provider, "600000", dates[0].date()) is None
    db.close()
