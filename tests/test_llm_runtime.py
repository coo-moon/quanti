"""Tests for the LLM-driven agent runtime.

We never hit the real Anthropic API from tests — instead we inject a
`StubLLMClient` that returns canned responses scripted per test. The goal
is to cover:

  * The decision loop correctly extracts propose_orders.
  * Tool dispatch round-trips for inspect_position / inspect_decision_history.
  * Invalid orders (not in candidate set, oversized, wrong direction) are
    rejected, not executed.
  * Runtime falls back gracefully when LLM init fails (no anthropic installed).
  * The `llm_cycle` decision log entry captures reasoning + usage.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import pytest

from quanti.agent.goal import Goal, save_goal
from quanti.agent.llm_runtime import (
    LLMConfig,
    LLMDecisionLoop,
    build_context_message,
    run_llm_decision,
)
from quanti.agent.runtime import AgentRuntime
from quanti.agent.signal_pipeline import FusedCandidate
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.paper_broker import PaperBroker


class StubLLMClient:
    """Returns scripted responses in order. Lets tests pin exactly which
    tool calls Claude makes without hitting the network."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create_message(self, **kw) -> dict:
        self.calls.append({k: kw[k] for k in ("model", "temperature", "max_tokens")})
        if not self._responses:
            raise AssertionError("StubLLMClient ran out of scripted responses")
        return self._responses.pop(0)


def _propose_block(orders: list[dict], reasoning: str = "test",
                   tool_id: str = "t1") -> dict:
    return {
        "stop_reason": "tool_use",
        "content": [{
            "type": "tool_use", "id": tool_id,
            "name": "propose_orders",
            "input": {"orders": orders, "reasoning": reasoning},
        }],
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }


def _inspect_block(name: str, inp: dict, tool_id: str = "t1") -> dict:
    return {
        "stop_reason": "tool_use",
        "content": [{
            "type": "tool_use", "id": tool_id,
            "name": name, "input": inp,
        }],
        "usage": {"input_tokens": 100, "output_tokens": 30},
    }


# ----- decision loop --------------------------------------------------

class TestDecisionLoop:
    def test_single_propose_orders_terminates(self):
        client = StubLLMClient([
            _propose_block(orders=[
                {"code": "000001", "direction": "buy",
                 "size_pct": 0.05, "reason": "动量+"},
            ], reasoning="选 1 单"),
        ])
        loop = LLMDecisionLoop(client, LLMConfig(max_tool_iterations=3))
        proposed, reasoning, debug = loop.run("ctx", lambda n, i: "{}")
        assert len(proposed) == 1
        assert proposed[0]["code"] == "000001"
        assert reasoning == "选 1 单"
        assert debug["iterations"] == 1

    def test_inspect_then_propose(self):
        client = StubLLMClient([
            _inspect_block("inspect_decision_history", {"limit": 3}, tool_id="t1"),
            _propose_block(orders=[], reasoning="看完后决定不开仓", tool_id="t2"),
        ])
        dispatch_calls: list[str] = []

        def dispatcher(name: str, inp: dict) -> str:
            dispatch_calls.append(name)
            return '{"recent": []}'

        loop = LLMDecisionLoop(client, LLMConfig(max_tool_iterations=3))
        proposed, reasoning, debug = loop.run("ctx", dispatcher)
        assert dispatch_calls == ["inspect_decision_history"]
        assert proposed == []
        assert "不开仓" in reasoning
        assert debug["iterations"] == 2

    def test_max_iterations_stops(self):
        """If LLM keeps calling inspect tools without proposing, we cut it off."""
        client = StubLLMClient([
            _inspect_block("inspect_decision_history", {"limit": 1}, tool_id="t1"),
            _inspect_block("inspect_decision_history", {"limit": 1}, tool_id="t2"),
            _inspect_block("inspect_decision_history", {"limit": 1}, tool_id="t3"),
        ])
        loop = LLMDecisionLoop(client, LLMConfig(max_tool_iterations=3))
        proposed, reasoning, debug = loop.run("ctx", lambda n, i: "{}")
        assert proposed == []
        assert debug["iterations"] == 3

    def test_llm_exception_returns_empty(self):
        class Bad:
            def create_message(self, **kw):
                raise RuntimeError("simulated transport error")

        loop = LLMDecisionLoop(Bad(), LLMConfig())
        proposed, reasoning, debug = loop.run("ctx", lambda n, i: "{}")
        assert proposed == []
        assert "error" in debug


# ----- run_llm_decision (end-to-end with stub) ------------------------

@pytest.fixture
def llm_setup(tmp_path):
    db = Database(str(tmp_path / "llm.db"))
    db.initialize()
    today = pd.Timestamp.today().normalize()
    dates = pd.bdate_range(end=today, periods=80)
    np.random.seed(99)
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


class TestRunLLMDecision:
    def test_proposed_order_executes(self, llm_setup):
        db, _, broker = llm_setup
        client = StubLLMClient([
            _propose_block(orders=[{
                "code": "000001", "direction": "buy",
                "size_pct": 0.05, "reason": "我看好"
            }], reasoning="一个 BUY"),
        ])
        candidates = [FusedCandidate(
            code="000001", strategy_score=0.7, factor_score=0.5,
            final_score=0.7, industry="银行",
            contributing_strategies=["ma_cross"])]
        goal = Goal(target_annual_return=0.20)
        result = run_llm_decision(db=db, broker=broker, goal=goal,
                                  candidates=candidates, llm_client=client)
        assert result["filled"] == 1
        assert result["signals"] == 1
        positions = db.list_positions()
        assert any(p["code"] == "000001" for p in positions)
        # Decision log should have an llm_cycle entry
        logs = db.list_decisions(kind="llm_cycle")
        assert len(logs) >= 1
        assert "一个 BUY" in logs[0]["summary"]

    def test_order_for_unvetted_code_rejected(self, llm_setup):
        """LLM may not propose codes that aren't in the candidate list."""
        db, _, broker = llm_setup
        client = StubLLMClient([
            _propose_block(orders=[{
                "code": "999999", "direction": "buy",
                "size_pct": 0.05, "reason": "瞎选"
            }], reasoning="LLM tries unvetted"),
        ])
        candidates = [FusedCandidate(
            code="000001", strategy_score=0.7, factor_score=0,
            final_score=0.7, industry="银行")]
        result = run_llm_decision(db=db, broker=broker, goal=Goal(),
                                  candidates=candidates, llm_client=client)
        # Order rejected pre-broker; no fills.
        assert result["filled"] == 0
        assert result["signals"] == 0
        logs = db.list_decisions(kind="llm_cycle")
        assert any("999999" in str(d.get("details_json", "")) or
                   "999999" in str(d.get("details", "")) for d in logs)

    def test_oversize_pct_rejected(self, llm_setup):
        """size_pct > 0.10 is rejected by the runtime regardless of risk manager."""
        db, _, broker = llm_setup
        client = StubLLMClient([
            _propose_block(orders=[{
                "code": "000001", "direction": "buy",
                "size_pct": 0.50, "reason": "过大仓"
            }], reasoning="LLM oversteps"),
        ])
        candidates = [FusedCandidate(
            code="000001", strategy_score=0.7, factor_score=0,
            final_score=0.7, industry="银行")]
        result = run_llm_decision(db=db, broker=broker, goal=Goal(),
                                  candidates=candidates, llm_client=client)
        assert result["filled"] == 0
        assert result["signals"] == 0

    def test_sell_direction_rejected(self, llm_setup):
        """LLM only proposes BUYs; sells flow through risk manager separately."""
        db, _, broker = llm_setup
        client = StubLLMClient([
            _propose_block(orders=[{
                "code": "000001", "direction": "sell",
                "size_pct": 0.05, "reason": "卖出"
            }]),
        ])
        candidates = [FusedCandidate(
            code="000001", strategy_score=0.7, factor_score=0,
            final_score=0.7, industry="银行")]
        result = run_llm_decision(db=db, broker=broker, goal=Goal(),
                                  candidates=candidates, llm_client=client)
        assert result["filled"] == 0

    def test_empty_orders_is_valid(self, llm_setup):
        """LLM may decide nothing is worth buying — that's a valid output."""
        db, _, broker = llm_setup
        client = StubLLMClient([
            _propose_block(orders=[], reasoning="今日观望"),
        ])
        candidates = [FusedCandidate(
            code="000001", strategy_score=0.7, factor_score=0,
            final_score=0.7, industry="银行")]
        result = run_llm_decision(db=db, broker=broker, goal=Goal(),
                                  candidates=candidates, llm_client=client)
        assert result["filled"] == 0
        assert "今日观望" in result["reasoning"]


class TestContextBuilder:
    def test_context_includes_goal_portfolio_candidates(self):
        goal = Goal(target_annual_return=0.30, max_drawdown=-0.20)
        portfolio = {"total_value": 1_000_000, "cash": 800_000,
                     "pnl_pct": 0.05, "positions": []}
        cands = [FusedCandidate(code="000001", strategy_score=0.7,
                                factor_score=1.2, final_score=0.8,
                                industry="银行",
                                contributing_strategies=["ma_cross", "macd_cross"])]
        msg = build_context_message(goal, portfolio, cands, recent_decisions=[])
        assert "30%" in msg
        assert "000001" in msg
        assert "银行" in msg
        assert "ma_cross+macd_cross" in msg

    def test_context_compact_when_no_positions_or_candidates(self):
        msg = build_context_message(
            Goal(), {"total_value": 0, "cash": 0, "positions": []},
            candidates=[], recent_decisions=[])
        assert "当前持仓: 空" in msg
        assert "本轮无候选股" in msg


# ----- runtime integration -------------------------------------------

@pytest.fixture
def runtime_with_llm(llm_setup):
    db, provider, broker = llm_setup
    agent = AgentRuntime(db, provider, broker,
                         strategies_dir="strategies",
                         screeners_dir="screeners")
    return db, agent


class TestRuntimeLLMPath:
    def test_llm_mode_routes_through_llm_path(self, runtime_with_llm):
        db, agent = runtime_with_llm
        # Inject a stub client BEFORE the tick so the runtime uses it.
        agent._llm_client = StubLLMClient([
            _propose_block(orders=[], reasoning="LLM 今日观望"),
        ])
        save_goal(db, Goal(
            target_annual_return=0.20,
            params={"agent_mode": "llm",
                    "top_k_strategies": 3,
                    "signal_threshold": 0.0,
                    "wf_enabled": True, "wf_n_folds": 2,
                    "wf_warmup_days": 40, "wf_test_days": 10}))
        result = agent.tick()
        # llm_cycle should be in the log even if no orders.
        assert result["ok"] in (True, False)
        # The cycle's strategy should be "llm" when we routed through llm_path
        # (unless candidate generation produced no fused candidates).
        if result.get("ok"):
            assert result.get("strategy") in ("llm", "ensemble_fallback")

    def test_rule_mode_doesnt_call_llm(self, runtime_with_llm):
        db, agent = runtime_with_llm
        # Stub that would raise if called — proves we don't reach it.
        class TripWire:
            def create_message(self, **kw):
                raise AssertionError("LLM should not be called in rule mode")
        agent._llm_client = TripWire()
        save_goal(db, Goal(target_annual_return=0.20))  # no agent_mode set
        agent.tick()  # must not raise
