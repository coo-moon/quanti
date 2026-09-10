"""A-share commission, fee and dividend-tax models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from quanti.models import Direction

# Securities-transaction stamp duty (证券交易印花税) is levied on the SELL leg
# only. It was halved from 0.1% (千1) to 0.05% (万5) effective 2023-08-28. A
# backtest spanning that date must switch rates, so `calculate` takes an
# optional trade_date; without one it uses the current (post-halving) default.
_STAMP_HALVED_FROM = date(2023, 8, 28)
_STAMP_RATE_PRE = 0.001   # 千1, before 2023-08-28
_STAMP_RATE_NOW = 0.0005  # 万5, on/after 2023-08-28

# --------------------------------------------------------------------------
# 股息红利差别化个人所得税(红利税)
# --------------------------------------------------------------------------
# 上市公司现金分红在**除息日不扣税**:个人投资者的红利税递延到卖出时,按
# 该笔股票的实际持股期限补缴(财税[2015]101号 / 财税[2012]85号):
#   - 持股 ≤ 1 个月        : 全额计税,税负 20%
#   - 1 个月 < 持股 ≤ 1 年 : 减按 50% 计入,税负 10%
#   - 持股 > 1 年          : 暂免征收,税负 0
# 持股期限按**先进先出(FIFO)**匹配的买入批次逐笔推算,所以同一笔卖出可能
# 拆成多档税率。回测里分红本身已经通过后复权(hfq)价格再投资(见
# DataProvider._apply_adjust),税必须单独从现金里扣——否则所有"分红再投"
# 类策略的收益都被系统性高估。
DIVIDEND_TAX_SHORT_DAYS = 30     # ≤30 天 = 1 个月以内(含)
DIVIDEND_TAX_FREE_DAYS = 365     # >365 天 = 超过 1 年(免)
DIVIDEND_TAX_RATE_SHORT = 0.20   # 1 个月以内
DIVIDEND_TAX_RATE_MID = 0.10     # 1 个月 ~ 1 年
DIVIDEND_TAX_RATE_FREE = 0.0     # 超过 1 年


def dividend_tax_rate(holding_days: int) -> float:
    """持股 `holding_days` 天的红利税税率(0 / 10% / 20% 三档)。"""
    if holding_days < 0:
        raise ValueError(f"holding_days must be >= 0, got {holding_days}")
    if holding_days <= DIVIDEND_TAX_SHORT_DAYS:
        return DIVIDEND_TAX_RATE_SHORT
    if holding_days <= DIVIDEND_TAX_FREE_DAYS:
        return DIVIDEND_TAX_RATE_MID
    return DIVIDEND_TAX_RATE_FREE


@dataclass
class DividendLot:
    """一笔买入批次(FIFO 队列的一节),记录持有期内每股累计收到的税前分红。

    `div_per_share` 只累计**买入之后、卖出之前**的除息事件——A 股红利税是
    "卖出时按持股期限补缴",没收到过分红就没有税。
    """

    buy_date: date
    quantity: int
    div_per_share: float = 0.0


@dataclass
class DividendTaxEvent:
    """一笔卖出里、来自单个买入批次的红利税事件(测试与归因用)。"""

    buy_date: date
    quantity: int
    holding_days: int
    rate: float
    div_per_share: float
    tax: float


@dataclass
class DividendTaxResult:
    """一次卖出应补缴的红利税合计 + 分批次明细。"""

    total: float = 0.0
    events: list[DividendTaxEvent] = field(default_factory=list)


def dividend_tax_events(lots: list[DividendLot], sell_date: date,
                        quantity: int) -> DividendTaxResult:
    """FIFO 匹配 `quantity` 股卖出,返回每档持股期限应补缴的红利税。

    纯函数(不改 `lots`):调用方负责按返回值裁掉已卖出的批次。
    """
    if quantity <= 0:
        return DividendTaxResult()
    remaining = int(quantity)
    total = 0.0
    events: list[DividendTaxEvent] = []
    for lot in sorted(lots, key=lambda x: x.buy_date):  # FIFO:先买先卖
        if remaining <= 0:
            break
        take = min(int(lot.quantity), remaining)
        if take <= 0:
            continue
        remaining -= take
        if lot.div_per_share <= 0:
            continue  # 持有期内没除息 → 无税,但仍要占用 FIFO 额度
        holding_days = (sell_date - lot.buy_date).days
        rate = dividend_tax_rate(holding_days)
        tax = lot.div_per_share * take * rate
        total += tax
        events.append(DividendTaxEvent(
            buy_date=lot.buy_date, quantity=take, holding_days=holding_days,
            rate=rate, div_per_share=lot.div_per_share, tax=tax))
    return DividendTaxResult(total=total, events=events)


class DividendTaxLedger:
    """每票 FIFO 买入批次账本:买入建 lot、除息累分红、卖出算税并扣减。

    BacktestEngine 在传入 `dividend_lookup` 时启用(见 engine.run)。
    与 Position 的区别:Position 只有加权平均成本,推不出每批持股期限,
    而红利税恰恰按批次期限分档——所以必须单独记 lot。
    """

    def __init__(self) -> None:
        self._lots: dict[str, list[DividendLot]] = {}

    def add(self, code: str, buy_date: date, quantity: int) -> None:
        """记录一笔买入(同日多笔合并到同一 lot,期限相同)。"""
        if quantity <= 0:
            return
        lots = self._lots.setdefault(code, [])
        if lots and lots[-1].buy_date == buy_date:
            lots[-1].quantity += int(quantity)
            return
        lots.append(DividendLot(buy_date=buy_date, quantity=int(quantity)))

    def accrue(self, code: str, div_per_share: float,
               ex_date: date | None = None) -> float:
        """除息日累加每股税前分红;返回计入的股数(0 表示没持仓/不含权)。

        `ex_date` 用于剔除"除息日当天才买入"的批次——股权登记日在除息日前
        一天,除息日买入不享有该次分红。
        """
        if div_per_share <= 0:
            return 0.0
        entitled = 0
        for lot in self._lots.get(code, []):
            if ex_date is not None and lot.buy_date >= ex_date:
                continue
            lot.div_per_share += float(div_per_share)
            entitled += lot.quantity
        return float(entitled)

    def sell(self, code: str, sell_date: date, quantity: int) -> DividendTaxResult:
        """卖出 `quantity` 股:算税 + 按 FIFO 扣减批次(可能拆多档)。"""
        lots = self._lots.get(code, [])
        result = dividend_tax_events(lots, sell_date, quantity)
        remaining = int(quantity)
        while remaining > 0 and lots:
            take = min(lots[0].quantity, remaining)
            lots[0].quantity -= take
            remaining -= take
            if lots[0].quantity <= 0:
                lots.pop(0)
        if not lots:
            self._lots.pop(code, None)
        return result

    def lots(self, code: str) -> list[DividendLot]:
        """当前未卖出的批次(拷贝,防止外部改坏账本)。"""
        return [DividendLot(buy_date=x.buy_date, quantity=x.quantity,
                            div_per_share=x.div_per_share)
                for x in self._lots.get(code, [])]

    def open_codes(self) -> set[str]:
        return set(self._lots)


class AShareCommission:
    """Standard A-share commission model.

    Itemized, not a flat per-side bps: broker commission (both sides, with a
    floor), stamp duty (sell only, date-aware), and transfer fee (both sides).
    """

    def __init__(
        self,
        commission_rate: float = 0.00025,  # 万2.5
        min_commission: float = 5.0,       # 5元 下限
        stamp_tax_rate: float = _STAMP_RATE_NOW,  # 万5 (current); see _stamp_rate
        transfer_fee_rate: float = 0.00001,  # 十万分之一(过户费,双边)
    ):
        self.commission_rate = commission_rate
        self.min_commission = min_commission
        self.stamp_tax_rate = stamp_tax_rate
        self.transfer_fee_rate = transfer_fee_rate

    def _stamp_rate(self, trade_date: date | None) -> float:
        """Stamp-duty rate for `trade_date`: the historical 千1 before the
        2023-08-28 halving, else the configured (current 万5) rate. With no
        date, use the configured rate — correct for live/paper (today)."""
        if trade_date is not None and trade_date < _STAMP_HALVED_FROM:
            return _STAMP_RATE_PRE
        return self.stamp_tax_rate

    def calculate(self, price: float, quantity: int, direction: Direction,
                  trade_date: date | None = None) -> float:
        """Total transaction cost for one fill. Pass `trade_date` so historical
        backtests across 2023-08-28 use the correct stamp-duty rate; omit it for
        live/paper (defaults to the current rate)."""
        turnover = price * quantity

        # Broker commission (both buy and sell), 5元 floor.
        commission = max(turnover * self.commission_rate, self.min_commission)

        # Stamp tax — sell only, date-aware.
        stamp_tax = (turnover * self._stamp_rate(trade_date)
                     if direction == Direction.SELL else 0.0)

        # Transfer fee (both sides).
        transfer_fee = turnover * self.transfer_fee_rate

        return commission + stamp_tax + transfer_fee
