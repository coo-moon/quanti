"""Tests for the Bull/Bear debate layer in llm_runtime.

The debate runs N Bull→Bear text exchanges over the candidate context, then
the existing judgment loop (research-manager role) makes the propose_orders
call. All scripted through a StubLLMClient — no network, no anthropic.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from quanti.agent.goal import Goal
from quanti.agent.llm_runtime import (
    LLMConfig,
    run_debate,
    run_llm_decision,
)
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


def _text_block(text: str) -> dict:
    return {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 20, "output_tokens": 10},
    }


def _propose_block(orders: list[dict], reasoning: str = "t") -> dict:
    return {
        "stop_reason": "tool_use",
        "content": [{
            "type": "tool_use", "id": "p1", "name": "propose_orders",
            "input": {"orders": orders, "reasoning": reasoning},
        }],
        "usage": {"input_tokens": 50, "output_tokens": 20},
    }


# ----- run_debate (unit) -------------------------------------------------

class TestRunDebate:
    def test_one_round_collects_bull_and_bear(self):
        client = StubLLMClient([_text_block("看多 000001"), _text_block("看空 000001")])
        transcript, rounds = run_debate(
            client, "CTX", LLMConfig(debate_enabled=True, debate_rounds=1))
        assert len(rounds) == 1
        assert rounds[0]["bull"] == "看多 000001"
        assert rounds[0]["bear"] == "看空 000001"
        assert "第 1 轮" in transcript
        assert "看多 000001" in transcript and "看空 000001" in transcript
        assert len(client.calls) == 2  # one Bull + one Bear

    def test_two_rounds_make_four_calls(self):
        client = StubLLMClient([
            _text_block("b1"), _text_block("s1"),
            _text_block("b2"), _text_block("s2"),
        ])
        transcript, rounds = run_debate(client, "CTX", LLMConfig(debate_rounds=2))
        assert len(rounds) == 2
        assert len(client.calls) == 4
        assert "第 2 轮" in transcript

    def test_degrades_on_error(self):
        class Bad:
            def create_message(self, **kw):
                raise RuntimeError("transport error")
        transcript, rounds = run_debate(Bad(), "CTX", LLMConfig(debate_rounds=1))
        assert transcript == ""
        assert rounds == []


# ----- run_llm_decision with debate (end-to-end, stubbed) ----------------

@pytest.fixture
def llm_setup(tmp_path):
    db = Database(str(tmp_path / "debate.db"))
    db.initialize()
    today = pd.Timestamp.today().normalize()
    dates = pd.bdate_range(end=today, periods=80)
    np.random.seed(7)
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


class TestRunLLMDecisionDebate:
    def test_debate_then_propose_executes(self, llm_setup):
        db, _, broker = llm_setup
        client = StubLLMClient([
            _text_block("多头:看好 000001 动量"),
            _text_block("空头:注意银行板块回撤"),
            _propose_block([{"code": "000001", "direction": "buy",
                             "size_pct": 0.05, "reason": "综合看多"}],
                           reasoning="经辩论后买入"),
        ])
        cands = [FusedCandidate(code="000001", strategy_score=0.7,
                                factor_score=0.5, final_score=0.7,
                                industry="银行",
                                contributing_strategies=["ma_cross"])]
        cfg = LLMConfig(debate_enabled=True, debate_rounds=1)
        result = run_llm_decision(db=db, broker=broker, goal=Goal(),
                                  candidates=cands, llm_client=client, cfg=cfg)
        assert result["filled"] == 1
        assert len(result["debate"]) == 1
        assert result["debate"][0]["bull"] == "多头:看好 000001 动量"
        # Bull + Bear + judgment = 3 calls.
        assert len(client.calls) == 3
        # Debate is persisted in the decision log.
        logs = db.list_decisions(kind="llm_cycle")
        assert logs and logs[0]["details"].get("debate_rounds")

    def test_no_debate_by_default_single_call(self, llm_setup):
        db, _, broker = llm_setup
        client = StubLLMClient([_propose_block([], reasoning="观望")])
        cands = [FusedCandidate(code="000001", strategy_score=0.7,
                                factor_score=0.0, final_score=0.7,
                                industry="银行")]
        # Default cfg → debate off.
        result = run_llm_decision(db=db, broker=broker, goal=Goal(),
                                  candidates=cands, llm_client=client)
        assert result["debate"] == []
        assert len(client.calls) == 1
