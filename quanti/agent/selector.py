"""Pick the best strategy for a given universe + Goal.

Strategy is "best" when its backtest over the recent training window scores
highest against the Goal. The score uses CAGR distance to target, max
drawdown vs the cap, and Sharpe, weighted by risk tolerance.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

from quanti.agent.goal import Goal, RiskTolerance
from quanti.backtest.engine import BacktestEngine
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.strategy.base import BaseStrategy
from quanti.strategy.loader import StrategyLoader

logger = logging.getLogger(__name__)


@dataclass
class StrategyEvaluation:
    strategy_name: str
    annual_return: float
    max_drawdown: float
    sharpe: float
    total_trades: int
    score: float

    def as_dict(self) -> dict:
        return {
            "strategy_name": self.strategy_name,
            "annual_return": self.annual_return,
            "max_drawdown": self.max_drawdown,
            "sharpe": self.sharpe,
            "total_trades": self.total_trades,
            "score": self.score,
        }


class StrategySelector:
    """Backtest each candidate strategy on a code universe and pick the best."""

    def __init__(
        self,
        db: Database,
        provider: DataProvider,
        strategies_dir: str | Path = "strategies",
        training_days: int = 365,
        initial_cash: float = 1_000_000.0,
    ) -> None:
        self._db = db
        self._provider = provider
        self._strategies_dir = str(strategies_dir)
        self._training_days = training_days
        self._initial_cash = initial_cash

    # --------------------------------------------------------------- API
    def load_candidates(self) -> list[BaseStrategy]:
        loader = StrategyLoader()
        return loader.load_directory(self._strategies_dir)

    def evaluate(self, goal: Goal, codes: list[str],
                 candidates: Iterable[BaseStrategy] | None = None,
                 ) -> list[StrategyEvaluation]:
        candidates = list(candidates) if candidates is not None else self.load_candidates()
        if not candidates:
            return []
        if not codes:
            return []

        end = date.today()
        start = end - timedelta(days=self._training_days)
        engine = BacktestEngine(provider=self._provider,
                                initial_cash=self._initial_cash)

        results: list[StrategyEvaluation] = []
        # Cap universe so this stays snappy; selection accuracy matters more
        # than evaluating thousands of stocks every cycle.
        capped = codes[:50]
        for strat in candidates:
            try:
                strat.init(goal.params or {})
                bt = engine.run(strategy=strat, codes=capped,
                                start=start, end=end)
                m = bt.metrics or {}
                ev = StrategyEvaluation(
                    strategy_name=strat.name,
                    annual_return=float(m.get("annual_return", 0) or 0),
                    max_drawdown=float(m.get("max_drawdown", 0) or 0),
                    sharpe=float(m.get("sharpe_ratio", 0) or 0),
                    total_trades=len(bt.trades),
                    score=0.0,
                )
                ev.score = self._score(ev, goal)
                results.append(ev)
            except Exception as e:
                logger.warning(f"Selector backtest failed for {strat.name}: {e}")
                results.append(StrategyEvaluation(
                    strategy_name=strat.name, annual_return=0,
                    max_drawdown=0, sharpe=0, total_trades=0, score=-999,
                ))
        results.sort(key=lambda r: r.score, reverse=True)
        return results

    def pick_best(self, goal: Goal, codes: list[str],
                  ) -> tuple[BaseStrategy | None, list[StrategyEvaluation]]:
        candidates = self.load_candidates()
        if not candidates:
            return None, []
        ranking = self.evaluate(goal, codes, candidates)
        if not ranking:
            return None, []
        winner_name = ranking[0].strategy_name
        for s in candidates:
            if s.name == winner_name:
                return s, ranking
        return candidates[0], ranking

    # ------------------------------------------------------------ scoring
    @staticmethod
    def _score(ev: StrategyEvaluation, goal: Goal) -> float:
        """Composite score: higher is better.

        Weights shift with risk tolerance:
          - LOW  cares most about drawdown
          - HIGH cares most about hitting the return target
        """
        tol = goal.risk_tolerance
        if isinstance(tol, str):
            tol = RiskTolerance(tol)
        return_gap = ev.annual_return - goal.target_annual_return
        dd_breach = min(ev.max_drawdown - goal.max_drawdown, 0.0)  # negative = breach
        # Weights
        if tol is RiskTolerance.LOW:
            w_ret, w_dd, w_sharpe = 0.3, 1.8, 0.6
        elif tol is RiskTolerance.HIGH:
            w_ret, w_dd, w_sharpe = 1.2, 0.6, 0.4
        else:
            w_ret, w_dd, w_sharpe = 0.8, 1.0, 0.5
        # Penalize zero-trade strategies — they technically don't lose money
        # but they also can't earn it.
        activity = 1.0 if ev.total_trades > 0 else -1.0
        return (w_ret * return_gap) + (w_dd * dd_breach) + (w_sharpe * ev.sharpe) + activity
