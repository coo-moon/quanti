"""Tests for the agent: Goal CRUD, StrategySelector ranking, AgentRuntime tick."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from quanti.agent.goal import Goal, RiskTolerance, default_goal, load_goal, save_goal
from quanti.agent.runtime import AgentRuntime
from quanti.agent.selector import StrategySelector
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.paper_broker import PaperBroker


@pytest.fixture
def seeded(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=120)
    # Smooth uptrend so MA cross strategies have a fighting chance
    np.random.seed(123)
    prices = 10 + np.arange(len(dates)) * 0.02 + np.random.randn(len(dates)) * 0.05
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


class TestGoal:
    def test_default_goal(self):
        g = default_goal()
        assert isinstance(g, Goal)
        assert g.target_annual_return > 0
        assert g.max_drawdown < 0

    def test_persist_roundtrip(self, seeded):
        g = Goal(target_annual_return=0.30, max_drawdown=-0.15,
                 risk_tolerance=RiskTolerance.HIGH,
                 universe_pool="test_pool",
                 enabled=True)
        save_goal(seeded, g)
        loaded = load_goal(seeded)
        assert loaded.target_annual_return == 0.30
        assert loaded.max_drawdown == -0.15
        assert loaded.risk_tolerance is RiskTolerance.HIGH
        assert loaded.universe_pool == "test_pool"
        assert loaded.enabled is True


class TestStrategySelector:
    def test_evaluates_all_builtin_strategies(self, seeded):
        provider = DataProvider(seeded)
        selector = StrategySelector(seeded, provider,
                                    strategies_dir="strategies",
                                    training_days=120)
        goal = Goal()
        ranking = selector.evaluate(goal, codes=["000001"])
        assert len(ranking) > 0
        names = {r.strategy_name for r in ranking}
        # Built-ins under strategies/
        assert "ma_cross" in names

    def test_pick_best_returns_strategy(self, seeded):
        provider = DataProvider(seeded)
        selector = StrategySelector(seeded, provider,
                                    strategies_dir="strategies",
                                    training_days=120)
        strat, ranking = selector.pick_best(Goal(), codes=["000001"])
        assert strat is not None
        assert len(ranking) >= 1
        assert ranking[0].score >= ranking[-1].score


class TestAgentRuntime:
    def test_tick_runs_full_cycle(self, seeded):
        provider = DataProvider(seeded)
        broker = PaperBroker(seeded, provider, initial_cash=200_000)
        agent = AgentRuntime(seeded, provider, broker,
                             strategies_dir="strategies",
                             screeners_dir="screeners")
        # Pin a strategy so we don't depend on selector picking one for synthetic data
        save_goal(seeded, Goal(strategy_name="ma_cross",
                               target_annual_return=0.15))
        result = agent.tick()
        assert result["ok"] is True
        # Decision log captured the cycle
        decisions = seeded.list_decisions(limit=20)
        kinds = {d["kind"] for d in decisions}
        assert "cycle" in kinds

    def test_tick_dedupes_same_code_same_direction(self, seeded):
        """Multiple bars within the recent window must not multiply orders."""
        provider = DataProvider(seeded)
        broker = PaperBroker(seeded, provider, initial_cash=200_000)
        agent = AgentRuntime(seeded, provider, broker,
                             strategies_dir="/tmp/qe2e/strategies",
                             screeners_dir="strategies")
        save_goal(seeded, Goal(strategy_name="e2e_force_buy",
                               target_annual_return=0.15))
        result = agent.tick()
        assert result["ok"] is True
        # e2e_force_buy emits on every bar; should still produce one buy per code.
        buys = [t for t in seeded.list_trades() if t["direction"] == "buy"]
        codes_bought = [t["code"] for t in buys]
        # Each code should appear at most once.
        assert len(codes_bought) == len(set(codes_bought)), \
            f"duplicate buys in same tick: {codes_bought}"

    def test_tick_filters_sell_without_position(self, seeded):
        """SELL signals for codes we don't hold should not produce rejected
        broker orders — they're strategy-state echoes, not real decisions."""
        provider = DataProvider(seeded)
        broker = PaperBroker(seeded, provider, initial_cash=200_000)
        agent = AgentRuntime(seeded, provider, broker,
                             strategies_dir="/tmp/qe2e/strategies",
                             screeners_dir="strategies")
        # We never bought anything, but pin a strategy that may emit historical sells.
        save_goal(seeded, Goal(strategy_name="ma_cross",
                               target_annual_return=0.15))
        agent.tick()
        rejected = [o for o in seeded.list_orders()
                    if o["status"] == "rejected" and o["reason"] == "no position"]
        assert rejected == [], f"unexpected 'no position' rejections: {rejected}"

    def test_prune_decisions(self, tmp_path):
        """prune_decisions should drop rows older than the retention window."""
        db = Database(str(tmp_path / "prune.db")); db.initialize()
        # Insert a fresh row + an artificially old row.
        db.log_decision("trade", "fresh")
        from datetime import datetime, timedelta
        old_ts = (datetime.now() - timedelta(days=200)).isoformat()
        db.conn.execute(
            "INSERT INTO agent_decisions (ts, kind, code, summary, details_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (old_ts, "trade", "", "ancient", "{}"))
        db.conn.commit()
        assert len(db.list_decisions(limit=10)) == 2
        removed = db.prune_decisions(older_than_days=90)
        assert removed == 1
        rows = db.list_decisions(limit=10)
        assert len(rows) == 1
        assert rows[0]["summary"] == "fresh"
        # Calling again is a no-op.
        assert db.prune_decisions(older_than_days=90) == 0
        db.close()

    def test_tick_empty_universe(self, tmp_path):
        db = Database(str(tmp_path / "empty.db"))
        db.initialize()
        provider = DataProvider(db)
        broker = PaperBroker(db, provider)
        agent = AgentRuntime(db, provider, broker,
                             strategies_dir="strategies",
                             screeners_dir="screeners")
        result = agent.tick()
        assert result["ok"] is False
        assert "宇宙" in result["reason"]
        db.close()
