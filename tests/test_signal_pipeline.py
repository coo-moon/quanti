"""Tests for the signal aggregation pipeline (ensemble + factors + neutral).

Three layers:
  1. fuse_buy_signals: takes per-strategy signals and weights → ranked candidates.
  2. industry_cap: keeps at most N candidates per industry.
  3. Runtime integration: ensemble mode end-to-end produces a strategy_ensemble decision.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from quanti.agent.goal import Goal
from quanti.agent.runtime import AgentRuntime
from quanti.agent.signal_pipeline import (
    FusedCandidate,
    filter_by_threshold,
    fuse_buy_signals,
    industry_cap,
)
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.paper_broker import PaperBroker
from quanti.models import Direction, Signal


# -------------------- fuse_buy_signals -------------------------------------

class TestFuseBuy:
    def test_single_strategy_passes_through(self):
        sigs = {
            "ma_cross": [
                Signal(stock_code="A", direction=Direction.BUY, strength=0.8, reason=""),
                Signal(stock_code="B", direction=Direction.BUY, strength=0.4, reason=""),
            ]
        }
        out = fuse_buy_signals(sigs, {"ma_cross": 1.0},
                               factor_panel=None, factor_blend=0.0)
        codes = [c.code for c in out]
        assert codes == ["A", "B"]  # sorted by score desc
        assert out[0].final_score == pytest.approx(0.8)
        assert out[1].final_score == pytest.approx(0.4)

    def test_two_strategies_stack(self):
        """Both strategies BUY the same code → weighted-sum strength."""
        sigs = {
            "a": [Signal(stock_code="X", direction=Direction.BUY, strength=0.6, reason="")],
            "b": [Signal(stock_code="X", direction=Direction.BUY, strength=0.6, reason="")],
        }
        out = fuse_buy_signals(sigs, {"a": 0.5, "b": 0.5},
                               factor_panel=None, factor_blend=0.0)
        assert len(out) == 1
        # 0.5*0.6 + 0.5*0.6 = 0.6
        assert out[0].final_score == pytest.approx(0.6)

    def test_ignores_sells(self):
        sigs = {
            "a": [
                Signal(stock_code="A", direction=Direction.BUY, strength=0.7, reason=""),
                Signal(stock_code="B", direction=Direction.SELL, strength=0.9, reason=""),
            ]
        }
        out = fuse_buy_signals(sigs, {"a": 1.0})
        assert {c.code for c in out} == {"A"}

    def test_dominant_strategy_is_argmax_weighted(self):
        """dominant_strategy = argmax(weight × strength), not just any voter.
        'b' has lower raw strength but a higher weight → it owns the exit."""
        sigs = {
            "a": [Signal(stock_code="X", direction=Direction.BUY, strength=0.9, reason="")],
            "b": [Signal(stock_code="X", direction=Direction.BUY, strength=0.7, reason="")],
        }
        out = fuse_buy_signals(sigs, {"a": 0.2, "b": 0.8},
                               factor_panel=None, factor_blend=0.0)
        assert len(out) == 1
        # a: 0.2*0.9=0.18 ; b: 0.8*0.7=0.56 → b dominates
        assert out[0].dominant_strategy == "b"
        assert set(out[0].contributing_strategies) == {"a", "b"}
        # And it propagates onto the materialized signal.
        assert out[0].to_signal().entry_strategy == "b"

    def test_factor_panel_blends_in(self):
        """Same strategy_score but different factor scores → different ranks."""
        sigs = {
            "a": [
                Signal(stock_code="A", direction=Direction.BUY, strength=0.5, reason=""),
                Signal(stock_code="B", direction=Direction.BUY, strength=0.5, reason=""),
            ]
        }
        panel = pd.DataFrame({"composite": {"A": 2.0, "B": -2.0},
                              "industry": {"A": "tech", "B": "tech"}})
        out = fuse_buy_signals(sigs, {"a": 1.0},
                               factor_panel=panel, factor_blend=0.5)
        codes = [c.code for c in out]
        # A has positive factor, B has negative; A should rank first.
        assert codes[0] == "A"
        assert out[0].final_score > out[1].final_score


# -------------------- industry_cap -----------------------------------------

class TestIndustryCap:
    def test_caps_to_n_per_industry(self):
        cs = [
            FusedCandidate(code="A1", strategy_score=0.9, factor_score=0,
                           final_score=0.9, industry="bank"),
            FusedCandidate(code="A2", strategy_score=0.8, factor_score=0,
                           final_score=0.8, industry="bank"),
            FusedCandidate(code="A3", strategy_score=0.7, factor_score=0,
                           final_score=0.7, industry="bank"),
            FusedCandidate(code="B1", strategy_score=0.6, factor_score=0,
                           final_score=0.6, industry="tech"),
        ]
        out = industry_cap(cs, n_per_industry=2)
        codes = [c.code for c in out]
        # Best two banks + one tech = A1, A2, B1
        assert codes == ["A1", "A2", "B1"]

    def test_unknown_industry_uncapped(self):
        cs = [
            FusedCandidate(code="X1", strategy_score=0.9, factor_score=0,
                           final_score=0.9, industry=""),
            FusedCandidate(code="X2", strategy_score=0.8, factor_score=0,
                           final_score=0.8, industry=""),
        ]
        out = industry_cap(cs, n_per_industry=1)
        # Empty industry doesn't get capped — we don't responsibly know
        # whether to drop them, so keep all.
        assert len(out) == 2


# -------------------- threshold filter -------------------------------------

class TestThreshold:
    def test_filter(self):
        cs = [
            FusedCandidate(code="A", strategy_score=0, factor_score=0,
                           final_score=0.6),
            FusedCandidate(code="B", strategy_score=0, factor_score=0,
                           final_score=0.2),
            FusedCandidate(code="C", strategy_score=0, factor_score=0,
                           final_score=0.4),
        ]
        out = filter_by_threshold(cs, threshold=0.3)
        codes = {c.code for c in out}
        assert codes == {"A", "C"}


# -------------------- runtime integration ----------------------------------

@pytest.fixture
def ensemble_db(tmp_path):
    """Seed two stocks of different industries with long history so the
    selector has enough data for walk-forward, factor computation, and
    cross-sectional comparison."""
    db = Database(str(tmp_path / "ensemble.db"))
    db.initialize()
    today = pd.Timestamp.today().normalize()
    dates = pd.bdate_range(end=today, periods=250)
    np.random.seed(123)

    db.upsert_stock("000001", "bank-up", "SZ", date(1991, 4, 3), "银行")
    db.upsert_stock("000002", "tech-up", "SZ", date(1991, 4, 3), "科技")

    for code, slope in [("000001", 0.04), ("000002", 0.03)]:
        prices = 10 + np.arange(len(dates)) * slope + np.random.randn(len(dates)) * 0.05
        df = pd.DataFrame({
            "code": code,
            "date": [d.date() for d in dates],
            "open": prices, "high": prices * 1.01, "low": prices * 0.99,
            "close": prices,
            "volume": np.full(len(dates), 5_000_000.0),
            "amount": prices * 5_000_000,
            "turnover": np.full(len(dates), 1.0),
        })
        db.save_daily_quotes(df)
    yield db
    db.close()


class TestRuntimeEnsemble:
    def test_ensemble_mode_logs_strategy_ensemble(self, ensemble_db):
        """When ensemble_enabled=True, the runtime should log a strategy_ensemble decision."""
        provider = DataProvider(ensemble_db)
        broker = PaperBroker(ensemble_db, provider, initial_cash=1_000_000)
        agent = AgentRuntime(ensemble_db, provider, broker,
                             strategies_dir="strategies",
                             screeners_dir="screeners")
        # Enable ensemble. Smaller wf params so it fits the 250-day seed.
        from quanti.agent.goal import save_goal
        save_goal(ensemble_db, Goal(
            target_annual_return=0.20,
            params={"ensemble_enabled": True,
                    "top_k_strategies": 3,
                    "signal_threshold": 0.0,  # so even noisy signals pass
                    "factor_blend": 0.5,
                    "industry_neutral": True,
                    "wf_enabled": True,
                    "wf_n_folds": 2,
                    "wf_warmup_days": 60,
                    "wf_test_days": 14}))
        result = agent.tick()
        # The tick may or may not produce trades depending on whether
        # strategies emit signals on the most-recent bar, but the ensemble
        # decision should always be logged.
        assert result["ok"] is True
        ensemble_decisions = ensemble_db.list_decisions(
            limit=20, kind="strategy_ensemble")
        assert len(ensemble_decisions) >= 1
        assert result["strategy"] == "ensemble"

    def test_equal_weight_installs_fixed_sizer_and_caps_holdings(self, ensemble_db):
        """equal_weight + max_holdings: the ensemble path caps the book to N
        names, installs a FixedSizer(1/N) on the broker, forces signal
        strength to 1.0, and — when 1/N exceeds the per-stock risk cap —
        logs an `equal_weight_capped` warning instead of failing silently."""
        from quanti.agent.goal import save_goal
        from quanti.risk.sizer import FixedSizer, VolTargetSizer

        provider = DataProvider(ensemble_db)
        # Construct with a non-default sizer to prove it's restored on reset.
        original = VolTargetSizer()
        broker = PaperBroker(ensemble_db, provider, initial_cash=1_000_000,
                             sizer=original)
        agent = AgentRuntime(ensemble_db, provider, broker,
                             strategies_dir="strategies",
                             screeners_dir="screeners")
        goal = Goal(
            target_annual_return=0.20,
            params={"ensemble_enabled": True, "top_k_strategies": 3,
                    "signal_threshold": 0.0, "factor_blend": 0.5,
                    "equal_weight": True, "max_holdings": 1,
                    "wf_enabled": True, "wf_n_folds": 2,
                    "wf_warmup_days": 60, "wf_test_days": 14})
        save_goal(ensemble_db, goal)

        signals, name, _ev = agent._ensemble_path(
            goal=goal, candidates=["000001", "000002"])

        assert name == "ensemble"
        assert len(signals) <= 1                       # max_holdings cap
        assert isinstance(broker._sizer, FixedSizer)   # equal-weight installed
        assert agent._equal_weight_active is True
        if signals:
            assert signals[0].strength == 1.0          # conviction overridden
        # N=1 → target 100% >> 10% cap → must warn, not silently idle cash.
        capped = ensemble_db.list_decisions(limit=20, kind="equal_weight_capped")
        assert len(capped) >= 1

        # Switching equal_weight off restores the construction-time sizer.
        agent._set_cycle_sizer(None)
        assert broker._sizer is original
        assert agent._equal_weight_active is False

    def test_legacy_path_unchanged_when_ensemble_disabled(self, ensemble_db):
        """With ensemble_enabled=False (default), runtime should NOT log
        strategy_ensemble — it should go through pick_best like before."""
        provider = DataProvider(ensemble_db)
        broker = PaperBroker(ensemble_db, provider, initial_cash=1_000_000)
        agent = AgentRuntime(ensemble_db, provider, broker,
                             strategies_dir="strategies",
                             screeners_dir="screeners")
        from quanti.agent.goal import save_goal
        save_goal(ensemble_db, Goal(target_annual_return=0.20))
        result = agent.tick()
        assert result["ok"] is True
        ensemble_decisions = ensemble_db.list_decisions(
            limit=20, kind="strategy_ensemble")
        assert len(ensemble_decisions) == 0
        assert result["strategy"] != "ensemble"
