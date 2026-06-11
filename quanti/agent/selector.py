"""Pick the best strategy for a given universe + Goal.

Two evaluation modes:

  * **walk-forward** (default since 2026-05-25): the strategy is backtested
    on N disjoint out-of-sample windows. The score is dominated by OOS
    metrics + a consistency bonus across folds. This is what you want when
    you're choosing between strategies for real money.

  * **in-sample** (legacy, opt-in via `goal.params["wf_enabled"]=False`):
    backtest the strategy on the most recent `training_days`. Faster but
    overfits — kept for backwards compatibility and for the case where the
    universe doesn't have enough history for walk-forward.

The "best" score blends:
  - return distance to the user's target_annual_return,
  - drawdown vs the user's max_drawdown ceiling,
  - Sharpe,
  - OOS consistency (only in walk-forward mode),
  - weighted by RiskTolerance.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

from quanti.agent.goal import Goal, RiskTolerance
from quanti.agent.walk_forward import run_walk_forward
from quanti.backtest.engine import BacktestEngine
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.strategy.base import BaseStrategy
from quanti.strategy.loader import StrategyLoader

logger = logging.getLogger(__name__)


@dataclass
class StrategyEvaluation:
    strategy_name: str
    annual_return: float            # IS / single-window return
    max_drawdown: float
    sharpe: float
    total_trades: int
    score: float
    # Walk-forward fields. Populated only when walk_forward ran successfully.
    # `oos_annual_return` of 0.0 with `n_folds=0` means "no WF data available".
    oos_annual_return: float = 0.0
    oos_max_drawdown: float = 0.0
    oos_sharpe: float = 0.0
    oos_consistency: float = 0.0
    n_folds: int = 0

    def as_dict(self) -> dict:
        return {
            "strategy_name": self.strategy_name,
            "annual_return": self.annual_return,
            "max_drawdown": self.max_drawdown,
            "sharpe": self.sharpe,
            "total_trades": self.total_trades,
            "score": self.score,
            "oos_annual_return": self.oos_annual_return,
            "oos_max_drawdown": self.oos_max_drawdown,
            "oos_sharpe": self.oos_sharpe,
            "oos_consistency": self.oos_consistency,
            "n_folds": self.n_folds,
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

        params = goal.params or {}
        wf_enabled = bool(params.get("wf_enabled", True))
        n_folds = int(params.get("wf_n_folds", 3))
        warmup_days = int(params.get("wf_warmup_days", 120))
        test_days = int(params.get("wf_test_days", 21))

        end = date.today()
        # In-sample window still computed for tie-break and as a fallback.
        is_start = end - timedelta(days=self._training_days)
        engine = BacktestEngine(provider=self._provider,
                                initial_cash=self._initial_cash)

        results: list[StrategyEvaluation] = []
        # Cap universe so each Selector cycle stays bounded. Default raised
        # from 50 → 100 in 2026-05 (P4): 50 was statistically too thin for
        # walk-forward to discriminate strategies — many fold splits ended
        # up with zero trades. 100 doubles backtest cost but gives the
        # ranking real signal. Override via goal.params["selector_max_universe"].
        max_universe = int(params.get("selector_max_universe", 100))
        capped = codes[:max(20, max_universe)]
        for strat in candidates:
            try:
                # In-sample baseline (always computed, cheap).
                strat.init(goal.params or {})
                is_bt = engine.run(strategy=strat, codes=capped,
                                   start=is_start, end=end)
                m = is_bt.metrics or {}
                ev = StrategyEvaluation(
                    strategy_name=strat.name,
                    annual_return=float(m.get("annual_return", 0) or 0),
                    max_drawdown=float(m.get("max_drawdown", 0) or 0),
                    sharpe=float(m.get("sharpe_ratio", 0) or 0),
                    total_trades=len(is_bt.trades),
                    score=0.0,
                )

                if wf_enabled:
                    # Fresh instance per fold via factory. `copy.copy` is
                    # enough because BaseStrategy state is cleared in init().
                    cls = type(strat)
                    cfg = goal.params or {}
                    def factory(_cls=cls, _cfg=cfg) -> BaseStrategy:
                        inst = _cls()
                        inst.init(dict(_cfg))
                        return inst
                    wf = run_walk_forward(
                        engine, factory, capped, end,
                        n_folds=n_folds, warmup_days=warmup_days,
                        test_days=test_days,
                    )
                    ev.oos_annual_return = wf.oos_annual_return
                    ev.oos_max_drawdown = wf.oos_max_drawdown
                    ev.oos_sharpe = wf.oos_sharpe
                    ev.oos_consistency = wf.oos_consistency
                    ev.n_folds = len(wf.folds)

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

    def pick_topk(self, goal: Goal, codes: list[str], k: int = 3,
                  ) -> tuple[list[tuple[BaseStrategy, float]], list[StrategyEvaluation]]:
        """Return top-K strategies with softmax-normalized weights.

        Weights derive from `oos_sharpe` (or IS sharpe if WF disabled),
        floored at 0 to prevent losing strategies from getting weight. A
        single positive-Sharpe strategy gets weight 1.0; multiple compete via
        softmax with temperature 0.5 so the best gets ~60-70% weight rather
        than 100%.
        """
        candidates = self.load_candidates()
        if not candidates:
            return [], []
        ranking = self.evaluate(goal, codes, candidates)
        if not ranking:
            return [], []

        params = goal.params or {}
        wf_enabled = bool(params.get("wf_enabled", True))

        top = ranking[:k]
        sharpes = [
            max(0.0, ev.oos_sharpe if wf_enabled and ev.n_folds > 0 else ev.sharpe)
            for ev in top
        ]
        total = sum(sharpes)
        if total <= 0:
            # All non-positive: fall back to score rank.
            weights = [1.0 / len(top)] * len(top)
        else:
            # Soft-temperature weighting so the top doesn't get 100%.
            import math
            temp = 0.5
            exps = [math.exp(s / temp) for s in sharpes]
            ze = sum(exps)
            weights = [e / ze for e in exps]

        pairs: list[tuple[BaseStrategy, float]] = []
        by_name = {s.name: s for s in candidates}
        for ev, w in zip(top, weights):
            strat = by_name.get(ev.strategy_name)
            if strat is not None:
                pairs.append((strat, w))
        return pairs, ranking

    # ------------------------------------------------------------ scoring
    @staticmethod
    def _score(ev: StrategyEvaluation, goal: Goal) -> float:
        """Composite score: higher is better.

        Uses OOS metrics when walk-forward data is available, falling back to
        IS otherwise. The component shape and weights are unchanged from the
        original — only the *inputs* change. Plus a consistency bonus that
        rewards low fold-to-fold variance.
        """
        tol = goal.risk_tolerance
        if isinstance(tol, str):
            tol = RiskTolerance(tol)

        # Pick which numbers feed the score. WF available → use OOS.
        if ev.n_folds > 0:
            ann_return = ev.oos_annual_return
            max_dd = ev.oos_max_drawdown
            sharpe = ev.oos_sharpe
        else:
            ann_return = ev.annual_return
            max_dd = ev.max_drawdown
            sharpe = ev.sharpe

        # Normalize return relative to the target. A strategy that exactly
        # hits target earns 1.0; one that returns half earns 0.5; one that
        # doubles is capped at 1.5 so lottery-style outliers can't bury
        # solid-but-balanced picks.
        target = max(goal.target_annual_return, 0.01)
        return_score = max(min(ann_return / target, 1.5), -1.0)

        # Normalize drawdown relative to the user-stated ceiling. Positive
        # when comfortably within tolerance, 0 right at the limit, negative
        # when breaching.
        dd_ceiling = abs(goal.max_drawdown) if goal.max_drawdown != 0 else 0.20
        dd_score = (max_dd - goal.max_drawdown) / dd_ceiling
        # Clamp so a 10× breach doesn't dominate — at -2 a strategy is already
        # losing badly regardless.
        dd_score = max(min(dd_score, 1.5), -2.0)

        if tol is RiskTolerance.LOW:
            w_ret, w_dd, w_sharpe = 0.3, 1.8, 0.6
        elif tol is RiskTolerance.HIGH:
            w_ret, w_dd, w_sharpe = 1.2, 0.6, 0.4
        else:
            w_ret, w_dd, w_sharpe = 0.8, 1.0, 0.5

        # Consistency bonus only when WF ran. A strategy that does 10% every
        # fold is preferred over one that does 30% / -10% / 30% / -10% even
        # if their mean matches — drawdown timing risk is real money.
        w_consistency = 0.4 if ev.n_folds > 0 else 0.0

        # `total_trades` is the IS count; a strategy that didn't trade at all
        # in IS but had OOS trades is rare-but-possible (e.g. WF found a
        # window where indicators warmed up enough). Use the more permissive
        # signal: any activity in either window counts.
        any_activity = ev.total_trades > 0 or (ev.n_folds > 0 and ev.oos_annual_return != 0)
        activity = 1.0 if any_activity else -1.0

        return (w_ret * return_score
                + w_dd * dd_score
                + w_sharpe * sharpe
                + w_consistency * ev.oos_consistency
                + activity)
