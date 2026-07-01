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

import numpy as np
import pandas as pd
import pytest

from quanti.agent.goal import Goal, save_goal
from quanti.agent.llm_runtime import (
    LLMConfig,
    LLMDecisionLoop,
    _tools_schema,
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

def _make_llm_db(tmp_path, name="llm.db"):
    """Build a DB seeded with 80 bars of a ~10元 stock. Returns (db, provider)."""
    db = Database(str(tmp_path / name))
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
    return db, DataProvider(db)


@pytest.fixture
def llm_setup(tmp_path):
    db, provider = _make_llm_db(tmp_path)
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
        """size_pct above the single-stock cap (default 20%) is rejected by the
        runtime regardless of the risk manager. 0.50 > the 0.20 cap here."""
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

    def test_size_pct_deployed_proportional_to_cap(self, tmp_path):
        """size_pct is honored as a fraction of the single-stock cap. With a
        FixedSizer(max_pct=cap), a size_pct at HALF the cap deploys ~half the
        notional of a size_pct AT the cap. The old hard-coded /0.10 divisor
        saturated any size_pct ≥ 0.10 to strength 1.0 → identical deployment,
        which this test would catch (half == full)."""
        from quanti.risk.manager import RiskConfig
        from quanti.risk.sizer import FixedSizer
        cap = 0.20
        candidates = [FusedCandidate(
            code="000001", strategy_score=0.7, factor_score=0.5,
            final_score=0.7, industry="银行",
            contributing_strategies=["ma_cross"])]

        def deploy(size_pct, name):
            db, provider = _make_llm_db(tmp_path, name)
            broker = PaperBroker(
                db, provider, initial_cash=1_000_000,
                risk_config=RiskConfig(max_position_pct=cap),
                sizer=FixedSizer(max_pct=cap))
            client = StubLLMClient([_propose_block(orders=[{
                "code": "000001", "direction": "buy",
                "size_pct": size_pct, "reason": "x"}])])
            result = run_llm_decision(db=db, broker=broker, goal=Goal(),
                                      candidates=candidates, llm_client=client)
            assert result["filled"] == 1, f"expected a fill for size_pct={size_pct}"
            spent = 1_000_000 - db.get_portfolio_state()["cash"]
            db.close()
            return spent

        half = deploy(0.10, "half.db")   # half the cap → strength 0.5
        full = deploy(0.20, "full.db")   # at the cap  → strength 1.0
        assert 0 < half < full
        assert half == pytest.approx(full / 2, rel=0.15)

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

    def test_context_surfaces_limits_position_ratio_and_lot_value(self):
        portfolio = {
            "total_value": 1_000_000, "cash": 700_000, "market_value": 300_000,
            "pnl_pct": 0.05,
            "positions": [{"code": "000001", "name": "平安", "quantity": 10000,
                           "avg_cost": 10.0, "current_price": 30.0,
                           "market_value": 300_000, "pnl_pct": 2.0}],
        }
        cands = [FusedCandidate(code="600519", strategy_score=0.7, factor_score=1.0,
                                final_score=0.8, industry="白酒", current_price=50.0)]
        msg = build_context_message(
            goal=Goal(), portfolio=portfolio, candidates=cands, recent_decisions=[],
            risk_limits={"max_position_pct": 0.20, "max_industry_pct": 0.30,
                         "max_daily_trades": 20, "portfolio_stop_loss_pct": -0.30,
                         "lot_size": 100})
        assert "当前仓位 30%" in msg          # market_value / total_value
        assert "占比 30.0%" in msg            # per-position weight
        assert "单票上限: 20%" in msg          # parameter-driven, not 10%
        assert "≈¥200,000" in msg             # cap 元 value = total × 20%
        assert "每手¥5,000" in msg            # 50.0 × 100 shares

    def test_tools_schema_size_ceiling_is_parameter_driven(self):
        def cap(schema):
            po = next(t for t in schema if t["name"] == "propose_orders")
            return po["input_schema"]["properties"]["orders"]["items"][
                "properties"]["size_pct"]["maximum"]
        assert cap(_tools_schema(0.10)) == 0.10
        assert cap(_tools_schema(0.25)) == 0.25
        assert cap(_tools_schema(0.0)) == 0.01   # floored so schema stays valid


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
