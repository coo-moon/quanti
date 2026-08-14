"""AShareCommission:分项费用与印花税 2023-08-28 减半切换——钱的路径,专属测试。"""

from __future__ import annotations

from datetime import date

import pytest

from quanti.backtest.commission import AShareCommission
from quanti.models import Direction


@pytest.fixture
def fee():
    return AShareCommission()


def test_buy_has_no_stamp_tax(fee):
    # 10 元 × 10000 股 = 10万成交额:佣金 25(万2.5)+ 过户费 1(十万分之一)
    assert fee.calculate(10.0, 10_000, Direction.BUY) == pytest.approx(26.0)


def test_min_commission_floor(fee):
    # 1000 元成交额:按万2.5 应 0.25 元,但有 5 元下限;过户费 0.01
    assert fee.calculate(10.0, 100, Direction.BUY) == pytest.approx(5.01)


def test_sell_stamp_rate_before_halving(fee):
    # 2023-08-28 前:印花税千1。10万 × 0.001 = 100;佣金 25;过户 1
    got = fee.calculate(10.0, 10_000, Direction.SELL, trade_date=date(2023, 8, 25))
    assert got == pytest.approx(126.0)


def test_sell_stamp_rate_on_and_after_halving(fee):
    # 切换日当天即用新率万5:10万 × 0.0005 = 50
    for d in (date(2023, 8, 28), date(2024, 1, 2)):
        assert fee.calculate(10.0, 10_000, Direction.SELL,
                             trade_date=d) == pytest.approx(76.0)


def test_sell_without_date_uses_current_rate(fee):
    # 无日期 = live/paper(今天),用当前万5
    assert fee.calculate(10.0, 10_000, Direction.SELL) == pytest.approx(76.0)
