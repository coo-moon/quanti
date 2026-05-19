"""A-share commission and fee models."""

from __future__ import annotations

from quanti.models import Direction


class AShareCommission:
    """Standard A-share commission model."""

    def __init__(
        self,
        commission_rate: float = 0.00025,  # 万2.5
        min_commission: float = 5.0,
        stamp_tax_rate: float = 0.001,  # 千1 (卖出)
        transfer_fee_rate: float = 0.00001,  # 十万分之一
    ):
        self.commission_rate = commission_rate
        self.min_commission = min_commission
        self.stamp_tax_rate = stamp_tax_rate
        self.transfer_fee_rate = transfer_fee_rate

    def calculate(self, price: float, quantity: int, direction: Direction) -> float:
        """Calculate total transaction cost."""
        turnover = price * quantity

        # Broker commission (both buy and sell)
        commission = max(turnover * self.commission_rate, self.min_commission)

        # Stamp tax (sell only)
        stamp_tax = turnover * self.stamp_tax_rate if direction == Direction.SELL else 0.0

        # Transfer fee
        transfer_fee = turnover * self.transfer_fee_rate

        return commission + stamp_tax + transfer_fee
