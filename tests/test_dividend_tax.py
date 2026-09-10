"""红利税模型(三档税率 + 跨档 FIFO 拆分)与引擎接线。

A 股红利税是**卖出时**按 FIFO 批次持股期限补缴(除息日不扣税),所以只用
"卖出价 × 税率"是算不出来的:必须逐批记"这笔股票在持有期内收到过多少税前
分红"。这个文件同时钉住纯函数与引擎现金路径。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from quanti.backtest.commission import (
    DIVIDEND_TAX_RATE_FREE,
    DIVIDEND_TAX_RATE_MID,
    DIVIDEND_TAX_RATE_SHORT,
    DividendLot,
    DividendTaxLedger,
    dividend_tax_events,
    dividend_tax_rate,
)
from quanti.backtest.engine import BacktestEngine
from quanti.data.database import Database
from quanti.data.provider import DataProvider


class TestRateTiers:
    """三档税率:≤1 月 20% / 1 月~1 年 10% / >1 年免。"""

    def test_short_tier_is_20pct(self):
        assert dividend_tax_rate(0) == DIVIDEND_TAX_RATE_SHORT
        assert dividend_tax_rate(30) == DIVIDEND_TAX_RATE_SHORT  # 含 1 个月

    def test_mid_tier_is_10pct(self):
        assert dividend_tax_rate(31) == DIVIDEND_TAX_RATE_MID
        assert dividend_tax_rate(365) == DIVIDEND_TAX_RATE_MID  # 含 1 年

    def test_long_tier_is_free(self):
        assert dividend_tax_rate(366) == DIVIDEND_TAX_RATE_FREE
        assert dividend_tax_rate(2000) == DIVIDEND_TAX_RATE_FREE

    def test_negative_days_rejected(self):
        with pytest.raises(ValueError):
            dividend_tax_rate(-1)


class TestFifoSplit:
    def test_fifo_splits_one_sell_across_three_tiers(self):
        """一笔卖出跨三个批次 → 免 / 10% / 20% 三档同时出现(FIFO 顺序)。"""
        lots = [
            # 2024-01-01 买入:到 2025-06-20 已 536 天 → 免
            DividendLot(buy_date=date(2024, 1, 1), quantity=100, div_per_share=0.5),
            # 2025-01-01 买入:170 天 → 10%
            DividendLot(buy_date=date(2025, 1, 1), quantity=100, div_per_share=0.3),
            # 2025-06-01 买入:19 天 → 20%
            DividendLot(buy_date=date(2025, 6, 1), quantity=100, div_per_share=0.2),
        ]
        res = dividend_tax_events(lots, sell_date=date(2025, 6, 20), quantity=250)
        assert [e.rate for e in res.events] == [0.0, 0.10, 0.20]
        assert [e.quantity for e in res.events] == [100, 100, 50]
        assert res.total == pytest.approx(0.5 * 100 * 0.0 + 0.3 * 100 * 0.10
                                          + 0.2 * 50 * 0.20)

    def test_no_dividend_no_tax(self):
        lots = [DividendLot(date(2025, 6, 1), 100)]  # 持有期内没除息
        res = dividend_tax_events(lots, date(2025, 6, 5), 100)
        assert res.total == 0.0 and res.events == []

    def test_partial_sell_keeps_fifo_order_and_does_not_mutate(self):
        lots = [DividendLot(date(2025, 6, 1), 100, 0.2),
                DividendLot(date(2025, 6, 10), 100, 0.2)]
        res = dividend_tax_events(lots, date(2025, 6, 20), 150)
        # 纯函数:不改输入(lots 的扣减是 Ledger 的职责)
        assert [x.quantity for x in lots] == [100, 100]
        assert [e.quantity for e in res.events] == [100, 50]
        assert res.total == pytest.approx(0.2 * 150 * 0.20)


class TestLedger:
    def test_accrue_skips_lots_bought_on_ex_date(self):
        """除息日当天买入不享有该次分红(股权登记日在前一天)。"""
        led = DividendTaxLedger()
        led.add("600001", date(2025, 6, 10), 100)
        led.add("600001", date(2025, 6, 12), 200)   # 除息日当天买
        entitled = led.accrue("600001", 0.5, ex_date=date(2025, 6, 12))
        assert entitled == 100
        lots = led.lots("600001")
        assert [x.div_per_share for x in lots] == [0.5, 0.0]

    def test_sell_consumes_lots_and_accumulates_tax(self):
        led = DividendTaxLedger()
        led.add("600001", date(2025, 6, 1), 100)
        led.add("600001", date(2025, 6, 10), 100)
        led.accrue("600001", 0.2, ex_date=date(2025, 6, 11))
        res = led.sell("600001", date(2025, 6, 20), 150)
        assert res.total == pytest.approx(0.2 * 150 * 0.20)  # 全部 ≤1 月
        left = led.lots("600001")
        assert [(x.buy_date, x.quantity) for x in left] == [(date(2025, 6, 10), 50)]
        led.sell("600001", date(2025, 6, 21), 50)  # 卖光即清账
        assert led.lots("600001") == []

    def test_same_day_buys_merge_into_one_lot(self):
        led = DividendTaxLedger()
        led.add("600001", date(2025, 6, 1), 100)
        led.add("600001", date(2025, 6, 1), 100)
        assert len(led.lots("600001")) == 1
        assert led.lots("600001")[0].quantity == 200


class _BuyThenSellStrategy:
    """首根 bar 满仓买入,`sell_on` 当天清仓 —— 只为测引擎的红利税现金路径。"""

    name = "tax_probe"
    selectable = False

    def __init__(self, sell_on: date):
        self._sell_on = sell_on
        self._done = False
        from quanti.risk.sizer import FixedSizer
        self.preferred_sizer = FixedSizer(max_pct=1.0)

    def init(self, config):
        pass

    def on_bar(self, bar):
        from quanti.models import Direction, Signal
        if bar.date == self._sell_on:
            return [Signal(stock_code=bar.code, direction=Direction.SELL,
                           strength=1.0, reason="test sell")]
        if self._done:
            return []
        self._done = True
        return [Signal(stock_code=bar.code, direction=Direction.BUY,
                       strength=1.0, reason="test buy")]


def _flat_market(tmp_path, name: str, bars: int = 40):
    db = Database(str(tmp_path / name))
    db.initialize()
    dates = pd.bdate_range("2024-01-02", periods=bars)
    df = pd.DataFrame({
        "code": "000001", "date": [d.date() for d in dates],
        "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
        "volume": 1e6, "amount": 1e7, "turnover": 1.0,
    })
    db.save_daily_quotes(df)
    return db, [d.date() for d in dates]


def test_engine_withholds_dividend_tax_at_sell(tmp_path):
    """除息日入账、卖出补缴:持股 <1 月 → 20%,金额 = 每股分红 × 股数 × 20%。

    价格序列用后复权(hfq)口径,分红已在价内;红利税是唯一额外的现金流出,
    必须真的从现金里扣掉,否则回测高估分红再投类策略。
    """
    db, dates = _flat_market(tmp_path, "tax.db")
    ex_date = dates[2]
    sell_on = dates[10]              # 持股 8 个交易日 ≈ 12 天 → 20% 档
    provider = DataProvider(db)
    results = {}
    for tag, lookup in (("taxed", {ex_date: {"000001": 0.5}}), ("clean", None)):
        strategy = _BuyThenSellStrategy(sell_on=sell_on)
        strategy.init({})
        engine = BacktestEngine(provider=provider, initial_cash=100_000.0,
                                dividend_lookup=lookup)
        results[tag] = engine.run(strategy, ["000001"],
                                  start=dates[0], end=dates[-1])
    taxed, clean = results["taxed"], results["clean"]

    bought = [t for t in taxed.trades if t.direction.name == "BUY"][0]
    expect = 0.5 * bought.quantity * 0.20
    assert expect > 0
    assert taxed.dividend_tax_total == pytest.approx(expect)
    sells = [t for t in taxed.trades if t.direction.name == "SELL"]
    assert sells and sells[0].dividend_tax == pytest.approx(expect)
    # 未接线分红时零变化(老回测数字不动)
    assert clean.dividend_tax_total == 0.0
    assert clean.dividend_cash_total == 0.0
    # 税后现金正好少一个税额(同价格、同成交,只有红利税不同)
    assert (clean.equity_curve.iloc[-1] - taxed.equity_curve.iloc[-1]
            == pytest.approx(expect))
    assert taxed.dividend_cash_total == pytest.approx(0.5 * bought.quantity)
    db.close()


def test_engine_defers_tax_to_sale_for_over_one_year_hold(tmp_path):
    """持股 >1 年免征:同一次除息,长期持有版本不缴税(递延规则)。"""
    db = Database(str(tmp_path / "long.db"))
    db.initialize()
    dates = pd.bdate_range("2023-01-02", periods=330)
    df = pd.DataFrame({
        "code": "000001", "date": [d.date() for d in dates],
        "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
        "volume": 1e6, "amount": 1e7, "turnover": 1.0,
    })
    db.save_daily_quotes(df)
    dl = [d.date() for d in dates]
    ex_date = dl[50]
    strategy = _BuyThenSellStrategy(sell_on=dl[300])  # 持股 ~250 交易日 > 1 年
    strategy.init({})
    res = BacktestEngine(
        provider=DataProvider(db), initial_cash=100_000.0,
        dividend_lookup={ex_date: {"000001": 0.5}},
    ).run(strategy, ["000001"], start=dl[0], end=dl[-1])
    assert res.dividend_tax_total == 0.0
    assert res.dividend_cash_total > 0  # 分红确实入账了,只是免税
    db.close()


def test_halt_liquidation_still_pays_dividend_tax(tmp_path):
    """组合回撤熔断清仓也走真实的红利税路径:熔断卖出同样按持股期限补缴,
    否则"最坏路径"的税被漏掉、回测高估(任务书点名要覆盖的路径)。"""
    from quanti.risk.manager import RiskConfig, RiskManager

    db = Database(str(tmp_path / "halt.db"))
    db.initialize()
    dates = pd.bdate_range("2024-01-02", periods=8)
    closes = [10.0, 10.0, 10.0, 10.0, 10.0, 7.0, 7.0, 7.0]
    df = pd.DataFrame({
        "code": "000001", "date": [d.date() for d in dates],
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": 1e6, "amount": [c * 1e6 for c in closes], "turnover": 1.0,
    })
    db.save_daily_quotes(df)
    dl = [d.date() for d in dates]
    strategy = _BuyThenSellStrategy(sell_on=dl[-1])   # 不会走到:熔断先清仓
    strategy.init({})
    risk = RiskManager(RiskConfig(
        max_position_pct=1.0, max_industry_pct=1.0,
        stop_loss_pct=-0.99, atr_stop_k=0.0, take_profit_activate_pct=0.0,
        portfolio_stop_loss_pct=-0.15))
    res = BacktestEngine(
        provider=DataProvider(db), initial_cash=100_000.0,
        risk_manager=risk,
        dividend_lookup={dl[3]: {"000001": 0.5}},
    ).run(strategy, ["000001"], start=dl[0], end=dl[-1])

    assert res.halted, "组合回撤熔断应触发"
    halted_sells = [t for t in res.trades if t.strategy == "portfolio_stop"]
    assert halted_sells, "熔断必须真的清仓(而不是只置个标志)"
    assert res.dividend_tax_total > 0
    assert halted_sells[0].dividend_tax == pytest.approx(
        res.dividend_tax_total)
    db.close()
