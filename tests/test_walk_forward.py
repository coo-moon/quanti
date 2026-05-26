"""Tests for walk-forward validation.

Two things we want to guarantee:

  1. Folds are constructed correctly (non-overlapping OOS, with warmup tail).
  2. The selector's OOS-driven score actually penalizes a fragile strategy
     that wins one window by luck and loses the others — the whole reason
     we introduced walk-forward.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from quanti.agent.goal import Goal, RiskTolerance
from quanti.agent.selector import StrategyEvaluation, StrategySelector
from quanti.agent.walk_forward import (
    Fold,
    WalkForwardResult,
    make_folds,
    run_walk_forward,
)
from quanti.backtest.engine import BacktestEngine
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.models import BarData, Direction, Signal
from quanti.strategy.base import BaseStrategy


@pytest.fixture
def seeded_long(tmp_path):
    """Seed enough history that walk-forward folds actually fit."""
    db = Database(str(tmp_path / "wf.db"))
    db.initialize()
    db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=300)
    np.random.seed(7)
    prices = 10 + np.arange(len(dates)) * 0.015 + np.random.randn(len(dates)) * 0.05
    df = pd.DataFrame({
        "code": "000001",
        "date": [d.date() for d in dates],
        "open": prices - 0.05,
        "high": prices + 0.15,
        "low": prices - 0.15,
        "close": prices,
        "volume": np.full(len(dates), 1_500_000.0),
        "amount": prices * 1_500_000,
        "turnover": np.full(len(dates), 1.0),
    })
    db.save_daily_quotes(df)
    yield db
    db.close()


class TestMakeFolds:
    def test_n_folds_non_overlapping(self):
        end = date(2026, 5, 25)
        folds = make_folds(end, n_folds=3, warmup_days=120, test_days=21)
        assert len(folds) == 3
        # Newest fold is index 0
        assert folds[0].test_end == end
        # Each fold's test_end should differ by test_days
        deltas = [(folds[i].test_end - folds[i + 1].test_end).days
                  for i in range(len(folds) - 1)]
        assert all(d == 21 for d in deltas)

    def test_warmup_precedes_test(self):
        folds = make_folds(date(2026, 5, 25))
        for f in folds:
            assert f.warmup_start < f.test_start <= f.test_end

    def test_oldest_fold_oldest(self):
        folds = make_folds(date(2026, 5, 25), n_folds=4, test_days=21)
        # folds[3] is the oldest
        assert folds[3].test_end < folds[0].test_end


class _LuckyStrategy(BaseStrategy):
    """Emits one BUY on a specific target date, nothing else.

    Lets us construct a strategy that performs well in one fold only — by
    construction. Walk-forward should penalize it relative to a strategy
    that performs consistently across folds.
    """
    name = "lucky"

    def init(self, config: dict) -> None:
        self.target_date = config.get("target_date")
        self._fired = False

    def on_bar(self, bar: BarData) -> list[Signal]:
        if self._fired or self.target_date is None:
            return []
        if bar.date == self.target_date:
            self._fired = True
            return [Signal(stock_code=bar.code, direction=Direction.BUY,
                           strength=0.5, reason="one-shot")]
        return []


class _AlwaysBuyStrategy(BaseStrategy):
    """Emits a BUY on the first bar it sees (per init). Consistent across folds."""
    name = "always_buy"

    def init(self, config: dict) -> None:
        self._fired = False

    def on_bar(self, bar: BarData) -> list[Signal]:
        if self._fired:
            return []
        self._fired = True
        return [Signal(stock_code=bar.code, direction=Direction.BUY,
                       strength=0.5, reason="opener")]


class TestRunWalkForward:
    def test_basic_aggregation(self, seeded_long):
        provider = DataProvider(seeded_long)
        engine = BacktestEngine(provider=provider, initial_cash=200_000)
        end = date.today()

        def factory():
            s = _AlwaysBuyStrategy()
            s.init({})
            return s

        wf = run_walk_forward(engine, factory, codes=["000001"], end=end,
                              n_folds=3, warmup_days=80, test_days=15)
        assert len(wf.folds) == 3
        # At least some folds should have non-empty metrics on the long seeded series
        with_metrics = [f for f in wf.folds if f.metrics]
        assert len(with_metrics) >= 1

    def test_empty_codes(self):
        provider = DataProvider(Database(":memory:"))
        engine = BacktestEngine(provider=provider, initial_cash=100_000)

        def factory():
            return _AlwaysBuyStrategy()
        wf = run_walk_forward(engine, factory, codes=[], end=date.today())
        # No fold has any data → consistency / returns should be zeros, not crash.
        assert isinstance(wf, WalkForwardResult)


class TestSelectorWFScoring:
    def test_score_uses_oos_when_available(self):
        """A strategy with great IS but terrible OOS should lose to a steady OOS performer."""
        goal = Goal(target_annual_return=0.20, max_drawdown=-0.20,
                    risk_tolerance=RiskTolerance.MEDIUM)

        # IS-good / OOS-bad: looks great on training window, falls apart out of sample.
        fragile = StrategyEvaluation(
            strategy_name="fragile",
            annual_return=0.60, max_drawdown=-0.10, sharpe=2.5, total_trades=80,
            score=0.0,
            oos_annual_return=-0.10, oos_max_drawdown=-0.25, oos_sharpe=-0.5,
            oos_consistency=-0.5, n_folds=3,
        )
        # IS-modest / OOS-steady: less impressive in-sample but holds up out-of-sample.
        steady = StrategyEvaluation(
            strategy_name="steady",
            annual_return=0.15, max_drawdown=-0.12, sharpe=1.0, total_trades=40,
            score=0.0,
            oos_annual_return=0.18, oos_max_drawdown=-0.12, oos_sharpe=1.1,
            oos_consistency=0.7, n_folds=3,
        )
        s_fragile = StrategySelector._score(fragile, goal)
        s_steady = StrategySelector._score(steady, goal)
        assert s_steady > s_fragile, (
            f"steady ({s_steady:.3f}) should beat fragile ({s_fragile:.3f}) "
            f"when scoring uses OOS metrics")

    def test_score_falls_back_to_is_when_no_wf(self):
        """With n_folds=0 (e.g. wf disabled), scoring should match the old IS-only behavior."""
        goal = Goal(target_annual_return=0.20, max_drawdown=-0.20,
                    risk_tolerance=RiskTolerance.MEDIUM)
        ev_no_wf = StrategyEvaluation(
            strategy_name="legacy",
            annual_return=0.20, max_drawdown=-0.10, sharpe=1.5, total_trades=30,
            score=0.0,
            # All WF fields zero / n_folds=0 → IS path taken
        )
        # Same IS numbers, but with WF fields populated (and OOS = IS) →
        # score should be very close but not identical (consistency bonus applies).
        ev_with_wf = StrategyEvaluation(
            strategy_name="modern",
            annual_return=0.20, max_drawdown=-0.10, sharpe=1.5, total_trades=30,
            score=0.0,
            oos_annual_return=0.20, oos_max_drawdown=-0.10, oos_sharpe=1.5,
            oos_consistency=0.5, n_folds=3,
        )
        s_no_wf = StrategySelector._score(ev_no_wf, goal)
        s_with_wf = StrategySelector._score(ev_with_wf, goal)
        # Modern should win because consistency bonus adds value (0.4 * 0.5 = +0.2)
        assert s_with_wf > s_no_wf
        # But not by a huge amount — same return/dd/sharpe inputs.
        assert s_with_wf - s_no_wf == pytest.approx(0.4 * 0.5, abs=1e-6)

    def test_evaluate_respects_wf_disabled(self, seeded_long):
        """When goal.params['wf_enabled']=False, no WF columns are populated."""
        provider = DataProvider(seeded_long)
        selector = StrategySelector(seeded_long, provider,
                                    strategies_dir="strategies",
                                    training_days=200)
        goal = Goal(params={"wf_enabled": False})
        ranking = selector.evaluate(goal, codes=["000001"])
        assert len(ranking) > 0
        assert all(r.n_folds == 0 for r in ranking)

    def test_evaluate_populates_wf_when_enabled(self, seeded_long):
        provider = DataProvider(seeded_long)
        selector = StrategySelector(seeded_long, provider,
                                    strategies_dir="strategies",
                                    training_days=200)
        # Smaller windows so we fit in 300 days of seeded data.
        goal = Goal(params={"wf_enabled": True, "wf_n_folds": 2,
                            "wf_warmup_days": 60, "wf_test_days": 14})
        ranking = selector.evaluate(goal, codes=["000001"])
        assert len(ranking) > 0
        # At least one strategy should have populated WF data.
        any_wf = any(r.n_folds > 0 for r in ranking)
        assert any_wf, "expected at least one strategy with walk-forward folds"


class TestTopK:
    def test_pick_topk_returns_weighted_pairs(self, seeded_long):
        provider = DataProvider(seeded_long)
        selector = StrategySelector(seeded_long, provider,
                                    strategies_dir="strategies",
                                    training_days=200)
        goal = Goal(params={"wf_enabled": True, "wf_n_folds": 2,
                            "wf_warmup_days": 60, "wf_test_days": 14})
        pairs, ranking = selector.pick_topk(goal, codes=["000001"], k=3)
        assert 0 < len(pairs) <= 3
        # Weights sum to 1.0
        total = sum(w for _, w in pairs)
        assert total == pytest.approx(1.0, abs=1e-6)
        # All weights positive (we floor non-positive Sharpes at 0 but
        # softmax avoids exactly 0)
        assert all(w > 0 for _, w in pairs)
