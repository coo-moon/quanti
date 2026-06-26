"""A-share commission and fee models."""

from __future__ import annotations

from datetime import date

from quanti.models import Direction

# Securities-transaction stamp duty (证券交易印花税) is levied on the SELL leg
# only. It was halved from 0.1% (千1) to 0.05% (万5) effective 2023-08-28. A
# backtest spanning that date must switch rates, so `calculate` takes an
# optional trade_date; without one it uses the current (post-halving) default.
_STAMP_HALVED_FROM = date(2023, 8, 28)
_STAMP_RATE_PRE = 0.001   # 千1, before 2023-08-28
_STAMP_RATE_NOW = 0.0005  # 万5, on/after 2023-08-28


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
