"""Trading goal definition + DB persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from quanti.data.database import Database


class RiskTolerance(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class Goal:
    """User-defined trading objective. Agent optimizes against it."""

    target_annual_return: float = 0.20          # 20% CAGR
    max_drawdown: float = -0.20                 # tolerated drawdown (negative)
    risk_tolerance: RiskTolerance = RiskTolerance.MEDIUM
    universe_pool: str = ""                     # pool name to trade from; "" = all
    screener_name: str = ""                     # optional pre-filter
    strategy_name: str = ""                     # "" = let selector pick
    params: dict[str, Any] = field(default_factory=dict)
    rebalance_freq: str = "daily"               # daily / weekly
    enabled: bool = False                       # agent loop active

    def to_db(self) -> dict:
        return {
            "target_annual_return": self.target_annual_return,
            "max_drawdown": self.max_drawdown,
            "risk_tolerance": self.risk_tolerance.value
                              if isinstance(self.risk_tolerance, RiskTolerance)
                              else self.risk_tolerance,
            "universe_pool": self.universe_pool,
            "screener_name": self.screener_name,
            "strategy_name": self.strategy_name,
            "params": self.params,
            "rebalance_freq": self.rebalance_freq,
            "enabled": self.enabled,
        }

    @classmethod
    def from_db(cls, row: dict) -> "Goal":
        return cls(
            target_annual_return=row["target_annual_return"],
            max_drawdown=row["max_drawdown"],
            risk_tolerance=RiskTolerance(row["risk_tolerance"]),
            universe_pool=row.get("universe_pool", ""),
            screener_name=row.get("screener_name", ""),
            strategy_name=row.get("strategy_name", ""),
            params=row.get("params", {}),
            rebalance_freq=row.get("rebalance_freq", "daily"),
            enabled=bool(row.get("enabled", False)),
        )


def default_goal() -> Goal:
    return Goal(
        target_annual_return=0.20,
        max_drawdown=-0.20,
        risk_tolerance=RiskTolerance.MEDIUM,
        rebalance_freq="daily",
        enabled=False,
    )


def load_goal(db: Database) -> Goal:
    row = db.get_agent_goal()
    if row is None:
        g = default_goal()
        db.upsert_agent_goal(g.to_db())
        return g
    return Goal.from_db(row)


def save_goal(db: Database, goal: Goal) -> None:
    db.upsert_agent_goal(goal.to_db())
