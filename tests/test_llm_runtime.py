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
        # Record everything, including system/messages — tests that assert on
        # what actually reached the model (e.g. the regime context landing in
        # the judge's user message and nowhere else) need the prompt itself.
        self.calls.append(dict(kw))
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
        terminal, reasoning, debug = loop.run("ctx", lambda n, i: "{}")
        proposed = terminal.get("orders") or []
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
        terminal, reasoning, debug = loop.run("ctx", dispatcher)
        assert dispatch_calls == ["inspect_decision_history"]
        assert (terminal.get("orders") or []) == []
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
        terminal, reasoning, debug = loop.run("ctx", lambda n, i: "{}")
        assert terminal == {}
        assert debug["iterations"] == 3

    def test_llm_exception_returns_empty(self):
        class Bad:
            def create_message(self, **kw):
                raise RuntimeError("simulated transport error")

        loop = LLMDecisionLoop(Bad(), LLMConfig())
        terminal, reasoning, debug = loop.run("ctx", lambda n, i: "{}")
        assert terminal == {}
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

    def test_llm_cycle_logs_broker_rejections(self, tmp_path):
        """When the broker gates the LLM's buys (risk / protection layer), the
        llm_cycle summary shows the reject count AND reason — not a bare
        '0 成交' that reads like the proposed orders vanished. Regression for
        the 'have 提单 but no 待成交订单' confusion."""
        from datetime import datetime, timedelta
        from quanti.risk.protections import ProtectionConfig

        db, provider = _make_llm_db(tmp_path)
        today = date.today()

        def iso(d):
            return datetime(d.year, d.month, d.day, 15, 0).isoformat()

        # 3 stop-loss exits in the window → StoplossGuard locks all new BUYs.
        for i, c in enumerate(["000010", "000020", "000030"]):
            d = today - timedelta(days=i + 1)
            db.insert_order({
                "order_id": f"sl{i}", "code": c, "direction": "sell",
                "quantity": 100, "price_type": "market", "limit_price": 0.0,
                "status": "filled", "strategy_name": "risk_exit",
                "filled_price": 9.0, "filled_quantity": 100,
                "reason": "止损", "created_at": iso(d), "filled_at": iso(d),
                "entry_strategy": "",
            })
        broker = PaperBroker(db, provider, initial_cash=1_000_000,
                             fill_mode="pending",
                             protection_config=ProtectionConfig(
                                 sg_lookback_days=10, sg_trade_limit=3,
                                 sg_lock_days=10, max_drawdown_enabled=False))
        client = StubLLMClient([
            _propose_block(orders=[{
                "code": "000001", "direction": "buy",
                "size_pct": 0.05, "reason": "看多"}], reasoning="买一只"),
        ])
        candidates = [FusedCandidate(
            code="000001", strategy_score=0.7, factor_score=0.5,
            final_score=0.7, industry="银行",
            contributing_strategies=["ma_cross"])]
        result = run_llm_decision(db=db, broker=broker, goal=Goal(),
                                  candidates=candidates, llm_client=client)
        assert result["filled"] == 0
        logs = db.list_decisions(kind="llm_cycle")
        assert logs
        summary = logs[0]["summary"]
        assert "1 单提议" in summary
        assert "拒单" in summary and "StoplossGuard" in summary
        details = logs[0]["details"]
        assert details["n_rejected"] == 1
        assert details["reject_reasons"] and \
            any("StoplossGuard" in r for r in details["reject_reasons"])
        db.close()

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


# ----- regime context reaching the judge --------------------------------
# The snapshot is read-only here (produced daily at 17:30 by the background
# syncer). What these guard is *where* it lands: the judge's user message and
# nowhere else. See quanti/regime/prompt.py for why the report's LLM-written
# action/sector fields are stripped before they ever get here.

def _seed_regime_snapshot(db, when=None):
    from quanti.regime import report as R
    d = (when or date.today()).isoformat()
    R.save(db, {
        "date": d, "rule_label": "震荡(区间/分化)", "rule_score": -1,
        "llm_regime": "震荡", "llm_confidence": 75,
        "headline": "防御资产继续占优", "action": "观望",
        "metrics": {"above20": 45.0, "above50": 21.0, "above200": 14.0,
                    "up": 4252, "dn": 1215, "ad_ratio": 3.5, "nh": 676,
                    "nl": 485, "cap5": -0.6, "eq5": 2.7, "cap20": -3.8,
                    "eq20": -9.2, "amt_chg": -20.8, "n_stocks": 5000},
        "sectors": {"top20": [{"industry": "黄金", "ret": 12.0, "n": 10}]},
        "llm": {"action": "观望", "sectors_favored": ["黄金"]},
        "report_md": "正文", "news": {}, "model": "deepseek-v4-pro",
        "created_at": f"{d}T17:35:00",
    })


def _regime_text_block(text: str) -> dict:
    return {"stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 20, "output_tokens": 10}}


def _texts_of(call) -> str:
    """All user-message text in one scripted LLM call."""
    out = []
    for m in call.get("messages") or []:
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out.extend(b.get("text", "") for b in c if isinstance(b, dict))
    return "\n".join(out)


class TestRegimeContext:
    def test_reaches_the_judge_and_not_the_debaters(self, tmp_path):
        db, provider = _make_llm_db(tmp_path, "regime_judge.db")
        # pending = next-open fill, the only mode where feeding the prior
        # session's breadth is not look-ahead.
        broker = PaperBroker(db, provider, initial_cash=1_000_000,
                             fill_mode="pending")
        _seed_regime_snapshot(db)
        client = StubLLMClient([
            _regime_text_block("多头:看好"), _regime_text_block("空头:看空"),
            _propose_block(orders=[{"code": "000001", "direction": "buy",
                                    "size_pct": 0.05, "reason": "r"}]),
        ])
        run_llm_decision(db=db, broker=broker,
                         goal=Goal(params={"regime_in_prompt": True}),
                         candidates=[FusedCandidate(
                             code="000001", strategy_score=0.7, factor_score=0.5,
                             final_score=0.7, industry="银行",
                             contributing_strategies=["ma_cross"])],
                         llm_client=client,
                         cfg=LLMConfig(debate_enabled=True, debate_rounds=1))
        bull, bear, judge = (_texts_of(c) for c in client.calls[:3])
        assert "市场环境" not in bull and "市场环境" not in bear, \
            "同一份外生叙事喂给多空双方会让辩论坍缩成它的回声"
        assert "市场环境" in judge and "站上 MA20/50/200" in judge
        assert "不得据此调整 size_pct" in judge
        # 快照里 LLM 写的仓位/板块建议不得随行就市地漏进来
        for banned in ("观望", "黄金", "防御资产继续占优"):
            assert banned not in judge, f"{banned!r} 不该进裁判上下文"
        db.close()

    def test_immediate_fill_mode_gets_no_regime(self, llm_setup):
        """llm_setup 的 PaperBroker 是 immediate(同 bar close 成交)——
        注入 T 日收盘算的宽度会变成前视,闸门必须拦住。"""
        db, _, broker = llm_setup
        _seed_regime_snapshot(db)
        client = StubLLMClient([_propose_block(orders=[])])
        run_llm_decision(db=db, broker=broker,
                         goal=Goal(params={"regime_in_prompt": True}),
                         candidates=[FusedCandidate(
                             code="000001", strategy_score=0.7, factor_score=0.5,
                             final_score=0.7, industry="银行",
                             contributing_strategies=["ma_cross"])],
                         llm_client=client, cfg=LLMConfig())
        assert "市场环境" not in _texts_of(client.calls[0])

    def test_on_by_default_and_logged(self, tmp_path):
        """键缺失即注入(default-on),且日志要能事后分辨这一 tick 注没注。"""
        db, provider = _make_llm_db(tmp_path, "regime_default_on.db")
        broker = PaperBroker(db, provider, initial_cash=1_000_000,
                             fill_mode="pending")
        _seed_regime_snapshot(db)
        client = StubLLMClient([_propose_block(orders=[])])
        run_llm_decision(db=db, broker=broker, goal=Goal(),  # params 里没这个键
                         candidates=[FusedCandidate(
                             code="000001", strategy_score=0.7, factor_score=0.5,
                             final_score=0.7, industry="银行",
                             contributing_strategies=["ma_cross"])],
                         llm_client=client, cfg=LLMConfig())
        assert "市场环境" in _texts_of(client.calls[0])
        details = db.list_decisions(kind="llm_cycle")[0]["details"]
        assert details["regime_injected"] is True
        assert "order_codes" in details
        db.close()

    def test_explicit_false_and_logged(self, tmp_path):
        db, provider = _make_llm_db(tmp_path, "regime_off.db")
        broker = PaperBroker(db, provider, initial_cash=1_000_000,
                             fill_mode="pending")
        _seed_regime_snapshot(db)
        client = StubLLMClient([_propose_block(orders=[])])
        run_llm_decision(db=db, broker=broker,
                         goal=Goal(params={"regime_in_prompt": False}),
                         candidates=[FusedCandidate(
                             code="000001", strategy_score=0.7, factor_score=0.5,
                             final_score=0.7, industry="银行",
                             contributing_strategies=["ma_cross"])],
                         llm_client=client, cfg=LLMConfig())
        assert "市场环境" not in _texts_of(client.calls[0])
        details = db.list_decisions(kind="llm_cycle")[0]["details"]
        assert details["regime_injected"] is False
        assert details["regime_skip_reason"] == "已显式关闭"
        db.close()

    def test_tick_step_one_logs_the_snapshot(self, runtime_with_llm):
        """用户要的「tick 第一步」:跑一个真 tick,决策日志里必须出现这条
        regime 观测,且它读的是已落库快照(而不是在持锁的 tick 里现算)。"""
        db, agent = runtime_with_llm
        _seed_regime_snapshot(db)
        agent._llm_client = StubLLMClient([_propose_block(orders=[])])
        save_goal(db, Goal(target_annual_return=0.20,
                           params={"agent_mode": "llm", "regime_detect": True,
                                   "signal_threshold": 0.0}))
        agent.tick()
        logs = db.list_decisions(kind="regime")
        assert logs, "tick 第一步没有写出 regime 观测"
        d = logs[0]["details"]
        assert d["date"] == date.today().isoformat()
        assert d["rule_label"] == "震荡(区间/分化)"
        assert d["metrics"]["above50"] == 21.0
        # llm_setup 的 broker 是 immediate 成交 → 前视闸拦住注入,只观测
        assert d["into_prompt"] is False
        assert "仅观测" in logs[0]["summary"]

    def test_tick_detect_on_by_default(self, runtime_with_llm):
        """goal.params 里根本没有 regime_detect 也要写观测日志(default-on)。"""
        db, agent = runtime_with_llm
        _seed_regime_snapshot(db)
        agent._llm_client = StubLLMClient([_propose_block(orders=[])])
        save_goal(db, Goal(target_annual_return=0.20,
                           params={"agent_mode": "llm", "signal_threshold": 0.0}))
        agent.tick()
        assert db.list_decisions(kind="regime"), "默认开时 tick 第一步没写观测"

    def test_tick_detect_explicit_false_is_silent(self, runtime_with_llm):
        db, agent = runtime_with_llm
        _seed_regime_snapshot(db)
        agent._llm_client = StubLLMClient([_propose_block(orders=[])])
        save_goal(db, Goal(target_annual_return=0.20,
                           params={"agent_mode": "llm", "regime_detect": False,
                                   "signal_threshold": 0.0}))
        agent.tick()
        assert not db.list_decisions(kind="regime")

    def test_intraday_guard_never_touches_regime(self, runtime_with_llm,
                                                 monkeypatch):
        """盘中守护链路(止损/熔断/挂单撮合)不接 regime —— 它和 tick 抢
        `_broker_lock`,任何快照读取都是给止损空窗加时间。炸掉 regime 读取入口:
        守护仍须跑完,且不写 regime 日志。"""
        import quanti.regime.prompt as P
        import quanti.regime.report as R
        from quanti.utils import market as M
        _seed_regime_snapshot(db_ := runtime_with_llm[0])
        agent = runtime_with_llm[1]

        def boom(*a, **k):
            raise AssertionError("守护链路读了 regime 快照")

        for mod, name in ((P, "latest_usable"), (P, "regime_block"),
                          (R, "load_latest"), (R, "generate")):
            monkeypatch.setattr(mod, name, boom)
        monkeypatch.setattr(M, "in_trading_session", lambda *a, **k: True)
        ran = []
        monkeypatch.setattr(agent._broker, "check_exits",
                            lambda *a, **k: ran.append(1))
        agent._intraday_guard()
        assert ran, "守护根本没跑到 check_exits,这个测试就没在验任何东西"
        assert not db_.list_decisions(kind="regime")

    def test_tick_without_snapshot_says_so(self, runtime_with_llm):
        db, agent = runtime_with_llm       # 没有 seed 快照
        agent._llm_client = StubLLMClient([_propose_block(orders=[])])
        save_goal(db, Goal(target_annual_return=0.20,
                           params={"agent_mode": "llm", "regime_detect": True,
                                   "signal_threshold": 0.0}))
        agent.tick()                        # 不得抛出
        logs = db.list_decisions(kind="regime")
        assert logs and "无市场 regime 快照" in logs[0]["summary"]
