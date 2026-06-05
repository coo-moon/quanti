"""Tests for the risk-debate triad (aggressive/neutral/conservative).

Each reviewer returns keep_pct ∈ [0,1] per order; aggregation follows the
goal's risk tolerance. The triad can only shrink/veto sizes, never inflate
them. All scripted via StubLLMClient — no network.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from quanti.agent.goal import Goal, RiskTolerance
from quanti.agent.llm_runtime import LLMConfig, run_llm_decision, run_risk_debate
from quanti.agent.signal_pipeline import FusedCandidate
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.paper_broker import PaperBroker


class StubLLMClient:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create_message(self, **kw) -> dict:
        self.calls.append(kw)
        if not self._responses:
            raise AssertionError("StubLLMClient ran out of scripted responses")
        return self._responses.pop(0)


def _risk_block(reviews: list[dict]) -> dict:
    return {
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "r1",
                     "name": "submit_risk_review", "input": {"reviews": reviews}}],
        "usage": {"input_tokens": 20, "output_tokens": 10},
    }


def _propose_block(orders: list[dict], reasoning: str = "t") -> dict:
    return {
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "p1", "name": "propose_orders",
                     "input": {"orders": orders, "reasoning": reasoning}}],
        "usage": {"input_tokens": 50, "output_tokens": 20},
    }


_FLAT_PORTFOLIO = {"total_value": 1_000_000, "cash": 1_000_000,
                   "pnl_pct": 0.0, "positions": []}


# ----- aggregation math --------------------------------------------------

class TestRunRiskDebate:
    def _triad(self, a: float, n: float, c: float):
        return StubLLMClient([
            _risk_block([{"code": "A", "keep_pct": a}]),
            _risk_block([{"code": "A", "keep_pct": n}]),
            _risk_block([{"code": "A", "keep_pct": c}]),
        ])

    def test_low_tolerance_takes_min(self):
        client = self._triad(1.0, 0.6, 0.2)
        keep = run_risk_debate(client, [{"code": "A", "size_pct": 0.08}],
                               _FLAT_PORTFOLIO, Goal(risk_tolerance=RiskTolerance.LOW),
                               LLMConfig())
        assert keep["A"] == pytest.approx(0.2)
        assert len(client.calls) == 3

    def test_high_tolerance_takes_max(self):
        client = self._triad(1.0, 0.6, 0.2)
        keep = run_risk_debate(client, [{"code": "A", "size_pct": 0.08}],
                               _FLAT_PORTFOLIO, Goal(risk_tolerance=RiskTolerance.HIGH),
                               LLMConfig())
        assert keep["A"] == pytest.approx(1.0)

    def test_medium_tolerance_takes_mean(self):
        client = self._triad(1.0, 0.6, 0.2)
        keep = run_risk_debate(client, [{"code": "A", "size_pct": 0.08}],
                               _FLAT_PORTFOLIO, Goal(risk_tolerance=RiskTolerance.MEDIUM),
                               LLMConfig())
        assert keep["A"] == pytest.approx((1.0 + 0.6 + 0.2) / 3)

    def test_missing_code_defaults_to_keep_full(self):
        # Aggressive + conservative omit A → default 1.0; only neutral cuts.
        client = StubLLMClient([
            _risk_block([]),
            _risk_block([{"code": "A", "keep_pct": 0.5}]),
            _risk_block([]),
        ])
        keep = run_risk_debate(client, [{"code": "A", "size_pct": 0.05}],
                               _FLAT_PORTFOLIO, Goal(risk_tolerance=RiskTolerance.MEDIUM),
                               LLMConfig())
        assert keep["A"] == pytest.approx((1.0 + 0.5 + 1.0) / 3)

    def test_degrades_on_error(self):
        class Bad:
            def create_message(self, **kw):
                raise RuntimeError("transport error")
        keep = run_risk_debate(Bad(), [{"code": "A", "size_pct": 0.05}],
                               _FLAT_PORTFOLIO, Goal(), LLMConfig())
        assert keep == {}


# ----- end-to-end through run_llm_decision -------------------------------

@pytest.fixture
def llm_setup(tmp_path):
    db = Database(str(tmp_path / "risk.db"))
    db.initialize()
    today = pd.Timestamp.today().normalize()
    dates = pd.bdate_range(end=today, periods=80)
    np.random.seed(11)
    prices = 10 + np.cumsum(np.random.randn(len(dates)) * 0.05)
    db.upsert_stock("000001", "test-stock", "SZ", date(1991, 4, 3), "银行")
    df = pd.DataFrame({
        "code": "000001",
        "date": [d.date() for d in dates],
        "open": prices, "high": prices * 1.01, "low": prices * 0.99,
        "close": prices,
        "volume": np.full(len(dates), 5_000_000.0),
        "amount": prices * 5_000_000,
        "turnover": np.full(len(dates), 1.0),
    })
    db.save_daily_quotes(df)
    provider = DataProvider(db)
    broker = PaperBroker(db, provider, initial_cash=1_000_000)
    yield db, provider, broker
    db.close()


class TestRiskDebateEndToEnd:
    def _cand(self):
        return [FusedCandidate(code="000001", strategy_score=0.7, factor_score=0.5,
                               final_score=0.7, industry="银行",
                               contributing_strategies=["ma_cross"])]

    def test_veto_blocks_fill(self, llm_setup):
        db, _, broker = llm_setup
        client = StubLLMClient([
            _propose_block([{"code": "000001", "direction": "buy",
                             "size_pct": 0.08, "reason": "buy"}], reasoning="买"),
            _risk_block([{"code": "000001", "keep_pct": 0.0}]),
            _risk_block([{"code": "000001", "keep_pct": 0.0}]),
            _risk_block([{"code": "000001", "keep_pct": 0.0}]),
        ])
        cfg = LLMConfig(risk_debate_enabled=True)
        res = run_llm_decision(db=db, broker=broker,
                               goal=Goal(risk_tolerance=RiskTolerance.MEDIUM),
                               candidates=self._cand(), llm_client=client, cfg=cfg)
        assert res["filled"] == 0
        assert res["signals"] == 0
        assert res["risk_review"]["000001"] == pytest.approx(0.0)
        assert len(client.calls) == 4  # 1 manager + 3 risk reviewers

    def test_cut_keeps_smaller_order(self, llm_setup):
        db, _, broker = llm_setup
        client = StubLLMClient([
            _propose_block([{"code": "000001", "direction": "buy",
                             "size_pct": 0.08, "reason": "buy"}], reasoning="买"),
            _risk_block([{"code": "000001", "keep_pct": 0.5}]),
            _risk_block([{"code": "000001", "keep_pct": 0.5}]),
            _risk_block([{"code": "000001", "keep_pct": 0.5}]),
        ])
        cfg = LLMConfig(risk_debate_enabled=True)
        res = run_llm_decision(db=db, broker=broker,
                               goal=Goal(risk_tolerance=RiskTolerance.MEDIUM),
                               candidates=self._cand(), llm_client=client, cfg=cfg)
        assert res["risk_review"]["000001"] == pytest.approx(0.5)
        assert res["filled"] == 1  # 0.08 * 0.5 = 0.04 ≥ floor → still buys

    def test_off_by_default_no_risk_calls(self, llm_setup):
        db, _, broker = llm_setup
        client = StubLLMClient([
            _propose_block([{"code": "000001", "direction": "buy",
                             "size_pct": 0.05, "reason": "b"}], reasoning="买"),
        ])
        res = run_llm_decision(db=db, broker=broker, goal=Goal(),
                               candidates=self._cand(), llm_client=client)
        assert res["risk_review"] == {}
        assert res["filled"] == 1
        assert len(client.calls) == 1
