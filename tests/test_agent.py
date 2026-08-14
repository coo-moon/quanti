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


@pytest.fixture
def test_strategies_dir(tmp_path):
    """Materialize a test-only strategies directory with an e2e_force_buy
    strategy that emits a BUY on every bar. Lets the suite exercise the full
    Agent → PaperBroker path without relying on real strategy crossover
    events happening to land in the recent 3-day window of synthetic data.
    """
    strat_dir = tmp_path / "strategies"
    strat_dir.mkdir()
    (strat_dir / "e2e_force_buy.py").write_text(
        '"""Test-only: emit a BUY on every bar (agent dedup keeps one)."""\n'
        "from quanti.models import BarData, Direction, Signal\n"
        "from quanti.strategy.base import BaseStrategy\n"
        "\n\n"
        "class ForceBuyStrategy(BaseStrategy):\n"
        '    name = "e2e_force_buy"\n'
        "    def init(self, config: dict) -> None:\n"
        '        self.strength = config.get("strength", 0.15)\n'
        "    def on_bar(self, bar: BarData) -> list[Signal]:\n"
        "        return [Signal(stock_code=bar.code, direction=Direction.BUY,\n"
        '                       strength=self.strength, reason="e2e force buy")]\n'
    )
    return str(strat_dir)


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

    def test_score_rewards_target_hit(self):
        """A balanced strategy that hits the target with low drawdown must
        beat a high-Sharpe-but-under-target strategy at MEDIUM tolerance.
        This was the original "balanced strategies can never win" bug."""
        from quanti.agent.selector import StrategyEvaluation
        from quanti.agent.goal import RiskTolerance

        target = Goal(target_annual_return=0.20, max_drawdown=-0.20,
                      risk_tolerance=RiskTolerance.MEDIUM)
        balanced = StrategyEvaluation("balanced", 0.20, -0.05, 1.5, 40, 0)
        conservative = StrategyEvaluation("conservative", 0.10, -0.04, 1.8, 15, 0)
        score_balanced = StrategySelector._score(balanced, target)
        score_conservative = StrategySelector._score(conservative, target)
        assert score_balanced > score_conservative, (
            f"balanced ({score_balanced:.3f}) should beat conservative "
            f"({score_conservative:.3f}) at MEDIUM when balanced hits target")

    def test_score_penalizes_dd_breach_even_at_high_risk(self):
        """A strategy that breaches the user's stated max_drawdown must lose
        to one that respects it — even when the user said HIGH risk
        tolerance. The drawdown ceiling is an explicit user contract, not
        a soft suggestion."""
        from quanti.agent.selector import StrategyEvaluation
        from quanti.agent.goal import RiskTolerance

        goal = Goal(target_annual_return=0.20, max_drawdown=-0.20,
                    risk_tolerance=RiskTolerance.HIGH)
        breacher = StrategyEvaluation("breacher", 0.45, -0.30, 0.9, 80, 0)
        respecter = StrategyEvaluation("respecter", 0.25, -0.15, 1.0, 50, 0)
        s_breach = StrategySelector._score(breacher, goal)
        s_respect = StrategySelector._score(respecter, goal)
        assert s_respect > s_breach, (
            f"respecter ({s_respect:.3f}) should beat breacher "
            f"({s_breach:.3f}) even at HIGH because dd breach is real")

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

    def test_tick_dedupes_same_code_same_direction(self, seeded, test_strategies_dir):
        """Multiple bars within the recent window must not multiply orders."""
        provider = DataProvider(seeded)
        broker = PaperBroker(seeded, provider, initial_cash=200_000)
        agent = AgentRuntime(seeded, provider, broker,
                             strategies_dir=test_strategies_dir,
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

    def test_tick_filters_sell_without_position(self, seeded, test_strategies_dir):
        """SELL signals for codes we don't hold should not produce rejected
        broker orders — they're strategy-state echoes, not real decisions."""
        provider = DataProvider(seeded)
        broker = PaperBroker(seeded, provider, initial_cash=200_000)
        agent = AgentRuntime(seeded, provider, broker,
                             strategies_dir=test_strategies_dir,
                             screeners_dir="strategies")
        # We never bought anything, but pin a strategy that may emit historical sells.
        save_goal(seeded, Goal(strategy_name="ma_cross",
                               target_annual_return=0.15))
        agent.tick()
        rejected = [o for o in seeded.list_orders()
                    if o["status"] == "rejected" and o["reason"] == "no position"]
        assert rejected == [], f"unexpected 'no position' rejections: {rejected}"

    def test_selector_cache_skips_repick(self, seeded):
        """When strategy is auto-picked, the Selector should only re-evaluate
        once per `selector_reselect_interval_sec`. Subsequent ticks within
        the window reuse the cached choice, avoiding a 6-strategy backtest."""
        provider = DataProvider(seeded)
        broker = PaperBroker(seeded, provider, initial_cash=200_000)
        # Force re-pick interval to be very long so the cache stays valid.
        agent = AgentRuntime(seeded, provider, broker,
                             strategies_dir="strategies",
                             screeners_dir="strategies",
                             selector_reselect_interval_sec=60 * 60 * 24)
        # No pinned strategy → auto-select path
        save_goal(seeded, Goal(target_annual_return=0.15))
        # Tick 1 should trigger a strategy_pick
        agent.tick()
        picks_after_1 = seeded.list_decisions(limit=100, kind="strategy_pick")
        # Tick 2 should reuse cached pick — no new strategy_pick decision
        agent.tick()
        picks_after_2 = seeded.list_decisions(limit=100, kind="strategy_pick")
        assert len(picks_after_2) == len(picks_after_1), \
            f"expected cache hit, but got new strategy_pick (1: {len(picks_after_1)}, 2: {len(picks_after_2)})"

    def test_selector_cache_expires(self, seeded):
        """Force a 0-second reselect interval → every tick must re-pick."""
        provider = DataProvider(seeded)
        broker = PaperBroker(seeded, provider, initial_cash=200_000)
        agent = AgentRuntime(seeded, provider, broker,
                             strategies_dir="strategies",
                             screeners_dir="strategies",
                             selector_reselect_interval_sec=0)
        save_goal(seeded, Goal(target_annual_return=0.15))
        agent.tick()
        n1 = len(seeded.list_decisions(limit=100, kind="strategy_pick"))
        agent.tick()
        n2 = len(seeded.list_decisions(limit=100, kind="strategy_pick"))
        assert n2 > n1, f"expected cache miss, but stale cache used (1: {n1}, 2: {n2})"

    def test_shutdown_does_not_disable_goal(self, seeded, test_strategies_dir):
        """Process-shutdown must NOT flip goal.enabled to False — otherwise the
        agent would never auto-resume on the next server start (defeats the
        whole point of 'set goal and walk away')."""
        provider = DataProvider(seeded)
        broker = PaperBroker(seeded, provider, initial_cash=200_000)
        agent = AgentRuntime(seeded, provider, broker,
                             strategies_dir=test_strategies_dir,
                             screeners_dir="strategies")
        save_goal(seeded, Goal(strategy_name="e2e_force_buy",
                               target_annual_return=0.15, enabled=True))
        agent.start()
        # User intent: keep it running across restarts.
        assert load_goal(seeded).enabled is True
        agent.shutdown()  # simulate SIGTERM
        # Critical invariant: goal.enabled survived shutdown.
        assert load_goal(seeded).enabled is True

    def test_stop_disables_goal_and_logs(self, seeded, test_strategies_dir):
        """User-initiated stop must disable the goal (so we don't auto-resume)
        AND only log a decision if the agent was actually running."""
        provider = DataProvider(seeded)
        broker = PaperBroker(seeded, provider, initial_cash=200_000)
        agent = AgentRuntime(seeded, provider, broker,
                             strategies_dir=test_strategies_dir,
                             screeners_dir="strategies")
        save_goal(seeded, Goal(strategy_name="e2e_force_buy",
                               target_annual_return=0.15, enabled=True))
        # Stop without ever starting: should be a quiet no-op (no decision row)
        prior = len(seeded.list_decisions(limit=100, kind="agent_stop"))
        agent.stop()
        assert len(seeded.list_decisions(limit=100, kind="agent_stop")) == prior
        # Now start, then stop: should log exactly one agent_stop
        agent.start()
        agent.stop()
        stops = seeded.list_decisions(limit=100, kind="agent_stop")
        assert len(stops) == prior + 1
        # Goal should now be disabled.
        assert load_goal(seeded).enabled is False

    def test_status_reports_uptime_and_interval(self, seeded, test_strategies_dir):
        """status() exposes started_at (set on start, cleared on stop) and the
        tick cadence, so the UI can show running time + next-tick."""
        provider = DataProvider(seeded)
        broker = PaperBroker(seeded, provider, initial_cash=200_000)
        agent = AgentRuntime(seeded, provider, broker,
                             strategies_dir=test_strategies_dir,
                             screeners_dir="strategies",
                             tick_interval_sec=3600)
        # Before start: no started_at, interval still reported.
        s0 = agent.status()
        assert s0.started_at is None
        assert s0.tick_interval_sec == 3600
        save_goal(seeded, Goal(strategy_name="e2e_force_buy",
                               target_annual_return=0.15, enabled=True))
        agent.start()
        s1 = agent.status()
        assert s1.started_at is not None  # stamped on start
        agent.stop()
        assert agent.status().started_at is None  # cleared on stop

    def test_prune_decisions(self, tmp_path):
        """prune_decisions should drop rows older than the retention window."""
        db = Database(str(tmp_path / "prune.db"))
        db.initialize()
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

    def test_safe_cycle_swallows_db_log_failure(self, seeded, monkeypatch):
        """If a tick fails AND the error-log write also fails, _safe_cycle
        must NOT raise — otherwise the agent thread would die and the loop
        would stop."""
        provider = DataProvider(seeded)
        broker = PaperBroker(seeded, provider, initial_cash=200_000)
        agent = AgentRuntime(seeded, provider, broker,
                             strategies_dir="strategies",
                             screeners_dir="strategies")
        save_goal(seeded, Goal(strategy_name="ma_cross",
                               target_annual_return=0.15))

        # Force _run_one_cycle to fail
        def bad_cycle(*a, **kw):
            raise RuntimeError("simulated tick failure")
        monkeypatch.setattr(agent, "_run_one_cycle", bad_cycle)

        # Also force log_decision to fail (mimic full disk / locked DB)
        def bad_log(*a, **kw):
            raise RuntimeError("simulated DB log failure")
        monkeypatch.setattr(seeded, "log_decision", bad_log)

        # Must not raise
        agent._safe_cycle()

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


class TestStaleDataGate:
    """数据陈旧闸:大面积陈旧拦新信号,单只陈旧不拦。"""

    def _mk_db(self, tmp_path, n_stocks: int, stale_codes: set[str]):
        db = Database(str(tmp_path / "gate.db"))
        db.initialize()
        today = pd.Timestamp.today().normalize()
        cal = pd.bdate_range(end=today, periods=10)
        db.save_trade_calendar([d.date() for d in cal])
        for i in range(n_stocks):
            code = f"00000{i}" if i < 10 else f"0000{i}"
            db.upsert_stock(code, f"股票{i}", "SZ", date(2000, 1, 1), "行业")
            end = today - pd.Timedelta(days=30) if code in stale_codes else today
            dates = pd.bdate_range(end=end, periods=30)
            px = 10 + np.arange(len(dates)) * 0.01
            db.save_daily_quotes(pd.DataFrame({
                "code": code, "date": [d.date() for d in dates],
                "open": px, "high": px + 0.1, "low": px - 0.1, "close": px,
                "volume": np.full(len(dates), 1e6), "amount": px * 1e6,
                "turnover": np.ones(len(dates)),
            }))
        return db

    @pytest.fixture(autouse=True)
    def _no_network(self, monkeypatch):
        # _ensure_recent_data 不许在测试里真去拉数据源
        monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
        monkeypatch.delenv("QUANTI_DATA_SOURCE", raising=False)

    def test_systemic_staleness_blocks_new_signals(self, tmp_path):
        db = self._mk_db(tmp_path, 2, stale_codes={"000000", "000001"})
        provider = DataProvider(db)
        broker = PaperBroker(db, provider)
        agent = AgentRuntime(db, provider, broker,
                             strategies_dir="strategies",
                             screeners_dir="screeners")
        save_goal(db, Goal(strategy_name="ma_cross",
                           target_annual_return=0.15))
        result = agent.tick()
        assert result["ok"] is False
        assert "数据陈旧闸" in result["reason"]
        kinds = {d["kind"] for d in db.list_decisions(limit=20)}
        assert "stale_data_skip" in kinds
        assert "cycle" not in kinds  # 没往下生成新信号
        db.close()

    def test_single_stale_stock_does_not_block(self, tmp_path):
        db = self._mk_db(tmp_path, 10, stale_codes={"000009"})
        provider = DataProvider(db)
        broker = PaperBroker(db, provider)
        agent = AgentRuntime(db, provider, broker,
                             strategies_dir="strategies",
                             screeners_dir="screeners")
        save_goal(db, Goal(strategy_name="ma_cross",
                           target_annual_return=0.15))
        result = agent.tick()
        kinds = {d["kind"] for d in db.list_decisions(limit=30)}
        assert "stale_data_skip" not in kinds
        assert result["ok"] is True
        db.close()
