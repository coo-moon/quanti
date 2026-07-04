"""Tests for walk-forward validation.

Two things we want to guarantee:

  1. Folds are constructed correctly (non-overlapping OOS, with warmup tail).
  2. The selector's OOS-driven score actually penalizes a fragile strategy
     that wins one window by luck and loses the others — the whole reason
     we introduced walk-forward.
"""

from __future__ import annotations

import math
import types
from datetime import date

import numpy as np
import pandas as pd
import pytest

from quanti.agent.goal import Goal, RiskTolerance
from quanti.agent.selector import StrategyEvaluation, StrategySelector
from quanti.backtest.metrics import annualized_sharpe
from quanti.agent.walk_forward import (
    Fold,
    FoldResult,
    WalkForwardResult,
    _aggregate,
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

    def test_history_start_tiles_full_span(self):
        # history_start overrides n_folds: tile [start+warmup, end] with
        # test_days blocks so walk-forward eats the whole available history.
        end = date(2026, 7, 3)
        start = date(2016, 6, 27)  # ~10 years
        folds = make_folds(end, history_start=start, warmup_days=120,
                           test_days=126, max_folds=40)
        span_days = (end - start).days - 120
        assert len(folds) == span_days // 126
        assert len(folds) > 20  # ~28 half-year blocks over 10y — far past 3
        # Newest still ends at `end`; oldest fold's warmup never predates data.
        assert folds[0].test_end == end
        assert folds[-1].warmup_start >= start

    def test_history_start_respects_max_folds_cap(self):
        end = date(2026, 7, 3)
        start = date(2000, 1, 1)  # absurdly long → cap must bite
        folds = make_folds(end, history_start=start, test_days=63, max_folds=10)
        assert len(folds) == 10

    def test_history_start_none_is_backward_compatible(self):
        end = date(2026, 5, 25)
        assert make_folds(end) == make_folds(end, history_start=None)
        assert len(make_folds(end)) == 3  # unchanged default


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
            oos_consistency=0.5, n_folds=3, oos_trades=30,  # enough to trust OOS
        )
        s_no_wf = StrategySelector._score(ev_no_wf, goal)
        s_with_wf = StrategySelector._score(ev_with_wf, goal)
        # Modern should win because consistency bonus adds value (0.4 * 0.5 = +0.2)
        assert s_with_wf > s_no_wf
        # But not by a huge amount — same return/dd/sharpe inputs.
        assert s_with_wf - s_no_wf == pytest.approx(0.4 * 0.5, abs=1e-6)

    def test_pooled_sharpe_and_consistency(self):
        """oos_sharpe is computed from POOLED OOS returns across folds, and
        consistency uses sample std with a ≥2-fold guard (audit E1/E5)."""
        f = Fold(date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1))
        steady = [0.01] * 15  # steady positive daily returns
        r1 = FoldResult(fold=f, metrics={"annual_return": 0.1, "max_drawdown": -0.02},
                        n_trades_oos=6, oos_returns=steady)
        r2 = FoldResult(fold=f, metrics={"annual_return": 0.1, "max_drawdown": -0.03},
                        n_trades_oos=6, oos_returns=steady)
        agg = _aggregate([r1, r2])
        assert agg.oos_sharpe > 0          # 30 pooled obs, steady → real Sharpe
        assert agg.oos_consistency == pytest.approx(1.0)  # equal fold returns
        assert agg.total_trades_oos == 12

    def test_thin_pool_sharpe_is_zero_and_single_fold_no_consistency(self):
        """Too few pooled obs → Sharpe unreliable → 0; a lone fold gives no
        cross-fold consistency signal → 0 (not a fake 1.0)."""
        f = Fold(date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1))
        r = FoldResult(fold=f, metrics={"annual_return": 0.5, "max_drawdown": -0.01},
                       n_trades_oos=2, oos_returns=[0.05, 0.05, 0.05])
        agg = _aggregate([r])
        assert agg.oos_sharpe == 0.0
        assert agg.oos_consistency == 0.0

    def test_single_populated_fold_sharpe_untrusted(self):
        """Independent-sample guardrail: even with plenty of pooled obs, a
        Sharpe from a SINGLE populated OOS block is not an independent-cycle
        signal → reported 0. Needs ≥2 populated folds."""
        f = Fold(date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1))
        many = [0.01] * 40  # 40 obs, well past _MIN_POOLED_OBS
        agg = _aggregate([FoldResult(fold=f, metrics={"annual_return": 0.1},
                                     n_trades_oos=8, oos_returns=many)])
        assert agg.n_populated_folds == 1
        assert agg.oos_sharpe == 0.0  # single block → untrusted

    def test_n_populated_folds_counts_only_folds_with_returns(self):
        f = Fold(date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1))
        good = FoldResult(fold=f, metrics={"annual_return": 0.1},
                          n_trades_oos=6, oos_returns=[0.01] * 15)
        empty = FoldResult(fold=f, metrics={}, n_trades_oos=0, oos_returns=[])
        agg = _aggregate([good, good, empty])
        assert agg.n_populated_folds == 2
        assert agg.as_dict()["n_populated_folds"] == 2
        assert agg.oos_sharpe > 0  # 2 populated blocks, 30 obs → trusted

    def test_score_ignores_oos_sharpe_below_min_trades(self):
        """A WF result with too few OOS trades must not earn the Sharpe or
        consistency score components — its Sharpe is sampling noise (E1)."""
        goal = Goal(target_annual_return=0.20, max_drawdown=-0.20,
                    risk_tolerance=RiskTolerance.MEDIUM)
        base = dict(strategy_name="x", annual_return=0.2, max_drawdown=-0.1,
                    sharpe=0.0, total_trades=30, score=0.0,
                    oos_annual_return=0.2, oos_max_drawdown=-0.1,
                    oos_sharpe=2.0, oos_consistency=0.8, n_folds=3)
        thin = StrategyEvaluation(**base, oos_trades=3)    # below min → ignored
        rich = StrategyEvaluation(**base, oos_trades=50)   # trusted
        s_thin = StrategySelector._score(thin, goal)
        s_rich = StrategySelector._score(rich, goal)
        # MEDIUM: w_sharpe=0.5, w_consistency=0.4 → gap = 0.5*2.0 + 0.4*0.8.
        assert s_rich - s_thin == pytest.approx(0.5 * 2.0 + 0.4 * 0.8, abs=1e-6)

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
        goal = Goal(params={"wf_enabled": True, "wf_full_history": False,
                            "wf_n_folds": 2,
                            "wf_warmup_days": 60, "wf_test_days": 14})
        ranking = selector.evaluate(goal, codes=["000001"])
        assert len(ranking) > 0
        # At least one strategy should have populated WF data.
        any_wf = any(r.n_folds > 0 for r in ranking)
        assert any_wf, "expected at least one strategy with walk-forward folds"

    def test_full_history_gate_skips_short_span_dead_band(self, tmp_path):
        """A history too short for ≥2 half-year blocks must NOT take the
        full-history path (which would yield a single fold the guardrail then
        zeroes). It falls back to the fixed n_folds path instead — never the
        degenerate 1-fold result. Regression for the gate/guard mismatch."""
        db = Database(str(tmp_path / "short.db"))
        db.initialize()
        db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
        # ~200 business days ≈ 280 calendar days: past the old warmup+test gate
        # (246) but short of warmup+2·test (372) at default 120/126.
        dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=200)
        prices = 10 + np.arange(len(dates)) * 0.02
        db.save_daily_quotes(pd.DataFrame({
            "code": "000001", "date": [d.date() for d in dates],
            "open": prices, "high": prices * 1.01, "low": prices * 0.99,
            "close": prices, "volume": np.full(len(dates), 1e6),
            "amount": prices * 1e6, "turnover": np.full(len(dates), 1.0)}))
        provider = DataProvider(db)
        selector = StrategySelector(db, provider, strategies_dir="strategies",
                                    training_days=150)
        ranking = selector.evaluate(Goal(params={"wf_enabled": True}),
                                    codes=["000001"])
        db.close()
        assert ranking
        # Fixed path → n_folds default (3); the buggy gate gave the degenerate 1.
        assert all(r.n_folds != 1 for r in ranking)

    def test_evaluate_full_history_spans_many_folds(self, seeded_long):
        """Default wf_full_history=True tiles the whole seeded span into many
        OOS blocks instead of the fixed 3 — that's 'eat the full history'."""
        provider = DataProvider(seeded_long)
        selector = StrategySelector(seeded_long, provider,
                                    strategies_dir="strategies",
                                    training_days=200)
        # full_history default on; small blocks so the 300-day seed yields many.
        goal = Goal(params={"wf_enabled": True,
                            "wf_warmup_days": 40, "wf_test_days": 20})
        ranking = selector.evaluate(goal, codes=["000001"])
        assert ranking
        assert max(r.n_folds for r in ranking) > 3, (
            "full-history walk-forward should tile far more than the old 3 folds")


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


class TestDSRGate:
    """Deflated-Sharpe overlay: a best-of-N lucky Sharpe gets its capital
    concentration stripped (revert to equal weight); a genuine edge keeps it.

    Calibration hinges on feeding DSR a per-period (non-annualized) Sharpe and
    the real observation count — the selector stores annualized Sharpe, so a
    ×√252 mis-calibration here would fire the gate on everything.
    """

    @staticmethod
    def _ev(name: str, returns) -> StrategyEvaluation:
        r = [float(x) for x in returns]
        ann = annualized_sharpe(r)  # what the selector actually stores
        return StrategyEvaluation(
            strategy_name=name, annual_return=0.1, max_drawdown=-0.05,
            sharpe=ann, total_trades=len(r), score=0.0,
            oos_annual_return=0.1, oos_max_drawdown=-0.05, oos_sharpe=ann,
            oos_consistency=0.0, n_folds=3, oos_trades=len(r),
            n_obs=len(r), oos_returns=r)

    @classmethod
    def _noise_ranking(cls, seed: int, n: int = 20):
        rng = np.random.default_rng(seed)
        evs = [cls._ev(f"n{i}", rng.normal(0.0, 0.01, 60)) for i in range(n)]
        evs.sort(key=lambda e: e.oos_sharpe, reverse=True)  # lucky one first
        for rank, e in enumerate(evs):
            e.score = 100.0 - rank
        return evs

    @classmethod
    def _edge_ranking(cls, seed: int, n: int = 20):
        rng = np.random.default_rng(seed)
        noise = [cls._ev(f"n{i}", rng.normal(0.0, 0.01, 60)) for i in range(n - 1)]
        # Unambiguous edge: sr≈0.6/period over 250 obs → DSR≈0.998 even after
        # the N-trial haircut (a 0.4/120-obs edge lands ~0.67 — realistically
        # borderline, but too flaky to hang a unit test on).
        edge = cls._ev("edge", rng.normal(0.006, 0.01, 250))
        ranking = [edge] + noise
        for rank, e in enumerate(ranking):
            e.score = 100.0 - rank  # edge is the rank winner
        return ranking

    # -- calibration: the estimator itself --------------------------------
    def test_dsr_low_for_best_of_n_noise(self):
        ranking = self._noise_ranking(seed=42)
        dsr = StrategySelector._winner_dsr(ranking[0], ranking, wf_enabled=True)
        assert dsr is not None
        assert dsr["dsr"] < 0.9, f"noise best-of-N DSR should be low, got {dsr['dsr']:.3f}"

    def test_dsr_high_for_true_edge(self):
        ranking = self._edge_ranking(seed=7)
        dsr = StrategySelector._winner_dsr(ranking[0], ranking, wf_enabled=True)
        assert dsr["dsr"] > 0.9, f"true edge DSR should be high, got {dsr['dsr']:.3f}"

    def test_per_obs_sharpe_deannualizes(self):
        """The load-bearing calibration knob: the scalar fallback must divide
        the stored *annualized* Sharpe by √252. An annualized 15.87 is a
        per-period 1.0; feeding 15.87 into per-period DSR math is nonsense."""
        from quanti.backtest.overfit import sharpe_per_obs
        scalar = StrategyEvaluation(  # no oos_returns → scalar fallback
            "s", 0.1, -0.05, 0.0, 30, 0.0, oos_sharpe=math.sqrt(252.0),
            n_folds=3, oos_trades=30, n_obs=200)
        assert StrategySelector._per_obs_sharpe(scalar, wf_enabled=True) == \
            pytest.approx(1.0)
        # Returns path uses the series directly (no ÷√252 double-count).
        series = StrategyEvaluation(
            "s2", 0.1, -0.05, 0.0, 30, 0.0, n_folds=3, oos_trades=30,
            oos_returns=[0.01, -0.005, 0.008, 0.002, 0.01])
        assert StrategySelector._per_obs_sharpe(series, wf_enabled=True) == \
            pytest.approx(sharpe_per_obs(series.oos_returns))

    # -- wiring: does the gate actually move the weights? -----------------
    def _run_pick_topk(self, monkeypatch, ranking, params):
        selector = StrategySelector(None, None)
        cands = [types.SimpleNamespace(name=e.strategy_name) for e in ranking]
        monkeypatch.setattr(selector, "load_candidates", lambda: cands)
        monkeypatch.setattr(selector, "evaluate", lambda *a, **k: ranking)
        pairs, _ = selector.pick_topk(Goal(params=params), codes=["x"], k=3)
        return pairs

    def test_gate_reverts_noise_to_equal_weight(self, monkeypatch):
        ranking = self._noise_ranking(seed=42)
        # Gate OFF: lucky winner keeps a concentrated (unequal) softmax weight.
        w_off = [w for _, w in self._run_pick_topk(
            monkeypatch, ranking, {"dsr_gate": False})]
        assert max(w_off) - min(w_off) > 1e-3, "gate off → softmax stays concentrated"
        # Gate ON: low DSR → revert to equal weight.
        w_on = [w for _, w in self._run_pick_topk(
            monkeypatch, ranking, {"dsr_gate": True})]
        assert all(abs(w - 1 / 3) < 1e-9 for w in w_on), \
            f"gate on + low DSR → equal weight, got {w_on}"

    def test_gate_keeps_true_edge_weight(self, monkeypatch):
        ranking = self._edge_ranking(seed=7)
        pairs = self._run_pick_topk(monkeypatch, ranking, {"dsr_gate": True})
        weights = {s.name: w for s, w in pairs}
        assert weights["edge"] == max(weights.values())
        assert weights["edge"] > 1 / 3 + 1e-3, \
            f"high-DSR edge should keep concentrated weight, got {weights}"


def test_selector_engine_enables_protections(monkeypatch, seeded_long):
    """The Selector's backtest engine must carry a ProtectionManager so its
    strategy ranking matches how live trades execute (backtest≡live)."""
    import quanti.agent.selector as sel

    captured: dict = {}
    real_engine = sel.BacktestEngine

    def _spy(*args, **kwargs):
        captured["protection_manager"] = kwargs.get("protection_manager")
        return real_engine(*args, **kwargs)

    monkeypatch.setattr(sel, "BacktestEngine", _spy)

    provider = DataProvider(seeded_long)
    selector = StrategySelector(seeded_long, provider,
                                strategies_dir="strategies",
                                training_days=200)
    goal = Goal(params={"wf_enabled": False})
    selector.evaluate(goal, codes=["000001"])

    assert captured.get("protection_manager") is not None, (
        "Selector must pass protection_manager=ProtectionManager() to BacktestEngine"
    )
