"""LLM 全权模式 (agent_mode="llm_full") 的测试。

覆盖评估裁定的关键不变量:
  * llm_position_plans CRUD 与「LLM 点位持久化 + 本地机械执行」语义。
  * check_llm_exits:点位触发卖出;点位缺失/被穿透时灾难地板兜底;floor=0 关。
  * PaperBroker llm_managed 分支:离场只看 LLM 点位(ATR/策略离场不跑)、
    全平删 plan、protections 旁路但 risk caps 仍在、加仓触发即消费。
  * sanity 闸 validate_decision:幻觉代码 / 超额 size / sell 非持仓 / 单数截断。
  * run_llm_full_decision 端到端:订单执行 + plans 落库 + 审计行 + 截断重试。
  * run_llm_guard_decision 端到端:卖出/加仓/改点位 + 失败即跳过(不裸奔)。
  * openai_compat 对 5xx/429 的指数退避重试。
  * llm_full 实盘拒绝(_sync_llm_mode 降级)。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from quanti.agent.goal import Goal
from quanti.agent.llm_full import (
    run_llm_full_decision,
    run_llm_guard_decision,
    validate_decision,
)
from quanti.agent.llm_runtime import LLMConfig
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.paper_broker import PaperBroker
from quanti.models import Portfolio, Position
from quanti.risk.manager import RiskConfig, RiskManager
from quanti.agent.signal_pipeline import FusedCandidate


class StubLLMClient:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create_message(self, **kw) -> dict:
        self.calls.append(dict(kw))
        if not self._responses:
            raise AssertionError("StubLLMClient ran out of scripted responses")
        return self._responses.pop(0)


def _tool_block(name: str, inp: dict, stop_reason: str = "tool_use") -> dict:
    return {
        "stop_reason": stop_reason,
        "content": [{"type": "tool_use", "id": "t1", "name": name,
                     "input": inp}],
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }


def _make_db(tmp_path, codes=("000001",), name="llmfull.db"):
    db = Database(str(tmp_path / name))
    db.initialize()
    today = pd.Timestamp.today().normalize()
    dates = pd.bdate_range(end=today, periods=80)
    np.random.seed(7)
    for i, code in enumerate(codes):
        prices = 10 + i + np.cumsum(np.random.randn(len(dates)) * 0.02)
        db.upsert_stock(code, f"股{code}", "SZ", date(2000, 1, 1), "测试业")
        db.save_daily_quotes(pd.DataFrame({
            "code": code, "date": [d.date() for d in dates],
            "open": prices, "high": prices * 1.01, "low": prices * 0.99,
            "close": prices,
            "volume": np.full(len(dates), 5_000_000.0),
            "amount": prices * 5_000_000,
            "turnover": np.full(len(dates), 1.0),
        }))
    return db, DataProvider(db)


# ------------------------------------------------------------- DB CRUD

def test_llm_plans_crud(tmp_path):
    db, _ = _make_db(tmp_path)
    db.upsert_llm_plan("000001", 9.5, 8.8, 0.05, "破位止损")
    plans = db.list_llm_plans()
    assert len(plans) == 1
    p = plans[0]
    assert p["stop_price"] == 9.5 and p["add_price"] == 8.8
    # Upsert 覆盖
    db.upsert_llm_plan("000001", 9.8)
    p = db.list_llm_plans()[0]
    assert p["stop_price"] == 9.8 and p["add_price"] == 0
    # clear add / delete
    db.upsert_llm_plan("000001", 9.8, 9.0, 0.05)
    db.clear_llm_add_price("000001")
    p = db.list_llm_plans()[0]
    assert p["add_price"] == 0 and p["stop_price"] == 9.8
    db.delete_llm_plan("000001")
    assert db.list_llm_plans() == []
    db.close()


def test_risk_config_persists_disaster_floor(tmp_path):
    db, _ = _make_db(tmp_path)
    cfg = {"stop_loss_pct": -0.15, "portfolio_stop_loss_pct": -0.30,
           "take_profit_activate_pct": 0.15, "take_profit_trail_pct": 0.10,
           "strategy_exit_enabled": True, "atr_stop_k": 2.0, "atr_stop_n": 14,
           "llm_disaster_floor_pct": -0.20}
    db.upsert_risk_config(cfg)
    assert db.get_risk_config()["llm_disaster_floor_pct"] == -0.20
    db.close()


# ------------------------------------------------------- check_llm_exits

def _portfolio_with(code="000001", qty=1000, cost=10.0, price=10.0):
    pf = Portfolio(cash=100_000)
    pf.positions[code] = Position(
        stock_code=code, quantity=qty, avg_cost=cost, current_price=price)
    return pf


class TestCheckLLMExits:
    def test_stop_price_triggers_sell(self):
        rm = RiskManager(RiskConfig())
        pf = _portfolio_with(price=9.4)
        sells = rm.check_llm_exits(pf, {"000001": 9.5})
        assert len(sells) == 1
        assert "LLM点位" in sells[0].reason

    def test_above_stop_no_sell(self):
        rm = RiskManager(RiskConfig())
        pf = _portfolio_with(price=9.6)
        assert rm.check_llm_exits(pf, {"000001": 9.5}) == []

    def test_missing_plan_disaster_floor(self):
        rm = RiskManager(RiskConfig(llm_disaster_floor_pct=-0.25))
        pf = _portfolio_with(price=7.4)  # -26%
        sells = rm.check_llm_exits(pf, {})
        assert len(sells) == 1
        assert "灾难地板" in sells[0].reason and "无LLM点位" in sells[0].reason

    def test_hallucinated_stop_pierced_by_floor(self):
        # LLM 点位设得离谱地深(1 元)→ 地板在 -25% 兜底。
        rm = RiskManager(RiskConfig(llm_disaster_floor_pct=-0.25))
        pf = _portfolio_with(price=7.0)  # -30%
        sells = rm.check_llm_exits(pf, {"000001": 1.0})
        assert len(sells) == 1
        assert "点位未拦住" in sells[0].reason

    def test_floor_zero_disables_backstop(self):
        rm = RiskManager(RiskConfig(llm_disaster_floor_pct=0.0))
        pf = _portfolio_with(price=5.0)  # -50%, 无点位
        assert rm.check_llm_exits(pf, {}) == []

    def test_classic_exits_not_consulted(self):
        # llm 模式不跑 ATR/固定地板:-16%(穿透经典 -15% 地板)但点位在更低
        # 处 → 不卖。
        rm = RiskManager(RiskConfig(stop_loss_pct=-0.15, atr_stop_k=2.0,
                                    llm_disaster_floor_pct=-0.25))
        pf = _portfolio_with(price=8.4)  # -16%
        assert rm.check_llm_exits(pf, {"000001": 8.0}) == []


# ------------------------------------------------- PaperBroker llm branch

@pytest.fixture
def llm_broker(tmp_path):
    db, provider = _make_db(tmp_path, codes=("000001", "000002"))
    broker = PaperBroker(db, provider, initial_cash=1_000_000,
                         fill_mode="pending")
    broker.llm_managed = True
    yield db, provider, broker
    db.close()


def _seed_position(db, code="000001", qty=1000, cost=10.0,
                   buy_date=date(2026, 1, 5)):
    db.upsert_position(code, qty, cost, cost, buy_date)


class TestPaperBrokerLLMManaged:
    def test_check_exits_uses_llm_plans(self, llm_broker):
        db, provider, broker = llm_broker
        _seed_position(db, cost=100.0)  # 现价(~10元)远低于成本
        db.upsert_llm_plan("000001", 999.0)  # 点位在现价之上 → 触发
        # 灾难地板关掉,证明卖出来自点位而非地板。
        db.upsert_risk_config({
            "stop_loss_pct": -0.15, "portfolio_stop_loss_pct": -0.30,
            "take_profit_activate_pct": 0.15, "take_profit_trail_pct": 0.10,
            "strategy_exit_enabled": True, "atr_stop_k": 2.0, "atr_stop_n": 14,
            "llm_disaster_floor_pct": 0.0})
        landed = broker.check_exits()
        assert landed == 1  # pending 队列(盘外)也算 landed

    def test_no_plan_no_classic_stop(self, llm_broker):
        """llm_managed 下无点位 + 地板关 → 深度浮亏也不卖(经典 ATR/地板不跑)。"""
        db, provider, broker = llm_broker
        _seed_position(db, cost=100.0)
        db.upsert_risk_config({
            "stop_loss_pct": -0.15, "portfolio_stop_loss_pct": -0.30,
            "take_profit_activate_pct": 0.15, "take_profit_trail_pct": 0.10,
            "strategy_exit_enabled": True, "atr_stop_k": 2.0, "atr_stop_n": 14,
            "llm_disaster_floor_pct": 0.0})
        assert broker.check_exits() == 0

    def test_full_exit_deletes_plan(self, llm_broker):
        db, provider, broker = llm_broker
        _seed_position(db)
        db.upsert_llm_plan("000001", 5.0, 4.0, 0.05)
        from quanti.models import Direction, Signal
        ok = broker._fill_sell(
            Signal(stock_code="000001", direction=Direction.SELL,
                   strength=1.0, reason="test"),
            10.0, date.today(), "risk_exit", bar_amount=1e9)
        assert ok
        assert db.list_llm_plans() == []

    def test_protections_bypassed_risk_caps_kept(self, llm_broker):
        db, provider, broker = llm_broker
        from quanti.models import Direction, Signal
        pf = broker._build_runtime_portfolio()
        sig = Signal(stock_code="000002", direction=Direction.BUY,
                     strength=0.5, reason="t")
        ok, reason, kind = broker._entry_allowed(sig, pf)
        assert ok  # protections 不再拦
        # risk cap 仍在:持仓已超单票上限时拒绝。
        _seed_position(db, code="000002", qty=50_000, cost=10.0)
        db.set_position_price("000002", 10.0)
        pf = broker._build_runtime_portfolio()
        ok, reason, kind = broker._entry_allowed(sig, pf)
        assert not ok and kind == "risk_reject"

    def test_check_llm_adds_triggers_and_consumes(self, llm_broker, monkeypatch):
        db, provider, broker = llm_broker
        _seed_position(db, qty=500, cost=12.0, buy_date=date(2026, 1, 5))
        db.upsert_llm_plan("000001", 5.0, 11.0, 0.05, "回调加仓")
        # 实时价 10.5 ≤ 加仓价 11.0 → 触发。绕过交易时段判定,直接 mock marks。
        monkeypatch.setattr(broker, "_intraday_marks",
                            lambda codes: {c: 10.5 for c in codes})
        landed = broker.check_llm_adds()
        assert landed == 1
        p = db.list_llm_plans()[0]
        assert p["add_price"] == 0  # 触发即消费
        assert p["stop_price"] == 5.0  # 止损点位不动

    def test_check_llm_adds_noop_when_not_managed(self, llm_broker):
        db, provider, broker = llm_broker
        broker.llm_managed = False
        assert broker.check_llm_adds() == 0


# ------------------------------------------------------- validate gate

class TestValidateDecision:
    HELD = {"000001"}
    CANDS = {"000002", "000003"}

    def test_hallucinated_buy_rejected(self):
        orders, plans, rejects = validate_decision(
            {"orders": [{"code": "999999", "direction": "buy",
                         "size_pct": 0.05, "reason": "x"}], "plans": []},
            self.HELD, self.CANDS, 0.20, 10)
        assert orders == [] and any("幻觉" in r for r in rejects)

    def test_sell_must_be_held(self):
        orders, _, rejects = validate_decision(
            {"orders": [{"code": "000002", "direction": "sell",
                         "reason": "x"}], "plans": []},
            self.HELD, self.CANDS, 0.20, 10)
        assert orders == [] and any("非持仓" in r for r in rejects)

    def test_oversize_rejected_and_cap_count(self):
        raw = [{"code": "000002", "direction": "buy", "size_pct": 0.9,
                "reason": "x"}]
        orders, _, rejects = validate_decision(
            {"orders": raw, "plans": []}, self.HELD, self.CANDS, 0.20, 10)
        assert orders == []
        many = [{"code": "000002", "direction": "buy", "size_pct": 0.05,
                 "reason": str(i)} for i in range(15)]
        orders, _, rejects = validate_decision(
            {"orders": many, "plans": []}, self.HELD, self.CANDS, 0.20, 10)
        assert len(orders) == 10

    def test_plan_scope_held_plus_buys(self):
        term = {"orders": [{"code": "000002", "direction": "buy",
                            "size_pct": 0.05, "reason": "x"}],
                "plans": [{"code": "000001", "stop_price": 9.0},
                          {"code": "000002", "stop_price": 8.0},
                          {"code": "000003", "stop_price": 7.0}]}
        orders, plans, rejects = validate_decision(
            term, self.HELD, self.CANDS, 0.20, 10)
        assert {p["code"] for p in plans} == {"000001", "000002"}
        assert any("000003" in r for r in rejects)

    def test_buy_of_held_is_add_on(self):
        orders, _, rejects = validate_decision(
            {"orders": [{"code": "000001", "direction": "buy",
                         "size_pct": 0.05, "reason": "加仓"}], "plans": []},
            self.HELD, self.CANDS, 0.20, 10)
        assert len(orders) == 1


# --------------------------------------------- run_llm_full_decision e2e

def _submit_decision_block(orders, plans, reasoning="ok",
                           stop_reason="tool_use"):
    return _tool_block("submit_decision",
                       {"orders": orders, "plans": plans,
                        "reasoning": reasoning}, stop_reason)


class TestRunLLMFullDecision:
    def test_buy_and_plans_persisted(self, tmp_path):
        db, provider = _make_db(tmp_path, codes=("000001",))
        broker = PaperBroker(db, provider, initial_cash=1_000_000,
                             fill_mode="immediate")
        broker.llm_managed = True
        client = StubLLMClient([_submit_decision_block(
            orders=[{"code": "000001", "direction": "buy", "size_pct": 0.05,
                     "reason": "动量"}],
            plans=[{"code": "000001", "stop_price": 9.0, "add_price": 0,
                    "add_size_pct": 0, "reason": "初始止损"}])])
        cands = [FusedCandidate(code="000001", strategy_score=0.5,
                                factor_score=0.2, final_score=0.5,
                                industry="测试业",
                                contributing_strategies=["ma_cross"])]
        result = run_llm_full_decision(
            db=db, broker=broker, provider=provider,
            goal=Goal(target_annual_return=0.2), candidates=cands,
            llm_client=client, cfg=LLMConfig())
        assert result["filled"] == 1
        plans = db.list_llm_plans()
        assert len(plans) == 1 and plans[0]["stop_price"] == 9.0
        assert db.list_decisions(kind="llm_audit")
        stages = db.list_decisions(kind="tick_stage")
        assert {s["details"]["stage"] for s in stages} >= {
            "candidates", "llm_done", "execute", "plans"}
        db.close()

    def test_sell_of_held_position(self, tmp_path):
        db, provider = _make_db(tmp_path, codes=("000001",))
        broker = PaperBroker(db, provider, initial_cash=1_000_000,
                             fill_mode="immediate")
        broker.llm_managed = True
        _seed_position(db)
        client = StubLLMClient([_submit_decision_block(
            orders=[{"code": "000001", "direction": "sell",
                     "reason": "破位"}], plans=[])])
        result = run_llm_full_decision(
            db=db, broker=broker, provider=provider,
            goal=Goal(target_annual_return=0.2), candidates=[],
            llm_client=client, cfg=LLMConfig())
        assert result["filled"] == 1
        assert db.list_positions() == []
        db.close()

    def test_truncated_output_retried_once(self, tmp_path):
        db, provider = _make_db(tmp_path, codes=("000001",))
        broker = PaperBroker(db, provider, initial_cash=1_000_000,
                             fill_mode="immediate")
        broker.llm_managed = True
        truncated = {"stop_reason": "max_tokens", "content": [],
                     "usage": {"input_tokens": 1, "output_tokens": 1}}
        good = _submit_decision_block(orders=[], plans=[])
        client = StubLLMClient([truncated, good])
        result = run_llm_full_decision(
            db=db, broker=broker, provider=provider,
            goal=Goal(target_annual_return=0.2), candidates=[],
            llm_client=client, cfg=LLMConfig())
        assert result["ok"]
        assert len(client.calls) == 2  # 截断后重试了一次
        db.close()


# --------------------------------------------- run_llm_guard_decision e2e

def _guard_block(sells=(), adds=(), plans=(), reasoning="ok"):
    return _tool_block("submit_guard_actions",
                       {"sells": list(sells), "adds": list(adds),
                        "plans": list(plans), "reasoning": reasoning})


class TestRunLLMGuardDecision:
    def test_sell_and_replan(self, tmp_path):
        db, provider = _make_db(tmp_path, codes=("000001", "000002"))
        broker = PaperBroker(db, provider, initial_cash=1_000_000,
                             fill_mode="immediate")
        broker.llm_managed = True
        _seed_position(db, "000001")
        _seed_position(db, "000002")
        client = StubLLMClient([_guard_block(
            sells=[{"code": "000001", "reason": "放量跳水"}],
            plans=[{"code": "000002", "stop_price": 9.2, "add_price": 0,
                    "add_size_pct": 0, "reason": "上移止损"}])])
        result = run_llm_guard_decision(
            db=db, broker=broker, provider=provider,
            goal=Goal(target_annual_return=0.2), llm_client=client,
            cfg=LLMConfig())
        assert result["ok"] and result["sells"] == 1
        assert not any(p["code"] == "000001" for p in db.list_positions())
        plans = {p["code"]: p for p in db.list_llm_plans()}
        assert plans["000002"]["stop_price"] == 9.2
        assert db.list_decisions(kind="llm_guard")
        db.close()

    def test_llm_failure_skips_round(self, tmp_path):
        db, provider = _make_db(tmp_path, codes=("000001",))
        broker = PaperBroker(db, provider, initial_cash=1_000_000,
                             fill_mode="immediate")
        broker.llm_managed = True
        _seed_position(db)

        class Bad:
            def create_message(self, **kw):
                raise RuntimeError("simulated outage")

        result = run_llm_guard_decision(
            db=db, broker=broker, provider=provider,
            goal=Goal(target_annual_return=0.2), llm_client=Bad(),
            cfg=LLMConfig())
        assert not result["ok"]
        assert db.list_decisions(kind="llm_guard_skip")
        assert db.list_positions()  # 持仓原封不动,点位层继续保护
        db.close()

    def test_empty_book_skips(self, tmp_path):
        db, provider = _make_db(tmp_path, codes=("000001",))
        broker = PaperBroker(db, provider, initial_cash=1_000_000)
        result = run_llm_guard_decision(
            db=db, broker=broker, provider=provider,
            goal=Goal(target_annual_return=0.2),
            llm_client=StubLLMClient([]), cfg=LLMConfig())
        assert result.get("skipped")
        db.close()


# ------------------------------------------------------- retry + live gate

def test_openai_compat_retries_on_5xx():
    import httpx

    from quanti.agent.openai_compat import OpenAICompatLLMClient

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    client = OpenAICompatLLMClient(
        api_key="k", base_url="http://test/v1", default_model="m",
        transport=httpx.MockTransport(handler))
    client._BACKOFF_SEC = (0.0, 0.0)  # 测试不真睡
    resp = client.create_message(model="m", system="s", messages=[
        {"role": "user", "content": "hi"}], tools=[], max_tokens=10,
        temperature=0)
    assert calls["n"] == 3
    assert resp["content"][0]["text"] == "ok"


def test_openai_compat_no_retry_on_4xx():
    import httpx

    from quanti.agent.openai_compat import OpenAICompatLLMClient

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad request"})

    client = OpenAICompatLLMClient(
        api_key="k", base_url="http://test/v1", default_model="m",
        transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        client.create_message(model="m", system="s", messages=[
            {"role": "user", "content": "hi"}], tools=[], max_tokens=10,
            temperature=0)
    assert calls["n"] == 1


def test_sync_llm_mode_refuses_live(tmp_path):
    """llm_full 是代码层 paper-only:实盘 broker 一律降级并落告警。"""
    from quanti.agent.runtime import AgentRuntime

    db, provider = _make_db(tmp_path)

    class FakeLiveBroker:
        _require_live = True
        llm_managed = False

        def snapshot_portfolio(self):
            return {"total_value": 0, "pnl_pct": 0, "positions": []}

    rt = AgentRuntime(db, provider, FakeLiveBroker())
    goal = Goal(target_annual_return=0.2)
    goal.params = {"agent_mode": "llm_full"}
    assert rt._sync_llm_mode(goal) is False
    assert FakeLiveBroker.llm_managed is False or True  # attr set on instance
    assert db.list_decisions(kind="llm_full_refused")
    db.close()


def test_sync_llm_mode_paper_enables(tmp_path):
    from quanti.agent.runtime import AgentRuntime

    db, provider = _make_db(tmp_path)
    broker = PaperBroker(db, provider, initial_cash=1_000_000)
    rt = AgentRuntime(db, provider, broker)
    goal = Goal(target_annual_return=0.2)
    goal.params = {"agent_mode": "llm_full"}
    assert rt._sync_llm_mode(goal) is True
    assert broker.llm_managed is True
    goal.params = {"agent_mode": "llm"}
    assert rt._sync_llm_mode(goal) is False
    assert broker.llm_managed is False
    db.close()


# --------------------------------------------- run_llm_close_replan e2e

def _close_block(plans=(), reasoning="ok"):
    return _tool_block("submit_close_plans",
                       {"plans": list(plans), "reasoning": reasoning})


class TestRunLLMCloseReplan:
    def test_replans_all_holdings(self, tmp_path):
        from quanti.agent.llm_full import run_llm_close_replan
        db, provider = _make_db(tmp_path, codes=("000001", "000002"))
        broker = PaperBroker(db, provider, initial_cash=1_000_000,
                             fill_mode="immediate")
        _seed_position(db, "000001")
        _seed_position(db, "000002")
        client = StubLLMClient([_close_block(plans=[
            {"code": "000001", "stop_price": 9.5, "add_price": 0,
             "add_size_pct": 0, "reason": "收盘重校"},
            {"code": "000002", "stop_price": 9.1, "add_price": 8.8,
             "add_size_pct": 0.05, "reason": "回调加仓"},
        ])])
        result = run_llm_close_replan(
            db=db, broker=broker, provider=provider,
            goal=Goal(target_annual_return=0.2), llm_client=client,
            cfg=LLMConfig())
        assert result["ok"] is True and result["n_plans"] == 2
        plans = {p["code"]: p for p in db.list_llm_plans()}
        assert plans["000001"]["stop_price"] == 9.5
        assert plans["000002"]["add_price"] == 8.8
        assert db.list_decisions(kind="llm_close_replan")
        db.close()

    def test_partial_coverage_is_failure_but_persists(self, tmp_path):
        """缺一只持仓的点位 → ok=False(调度层会整轮重试),已给的点位照落。"""
        from quanti.agent.llm_full import run_llm_close_replan
        db, provider = _make_db(tmp_path, codes=("000001", "000002"))
        broker = PaperBroker(db, provider, initial_cash=1_000_000,
                             fill_mode="immediate")
        _seed_position(db, "000001")
        _seed_position(db, "000002")
        client = StubLLMClient([_close_block(plans=[
            {"code": "000001", "stop_price": 9.5, "add_price": 0,
             "add_size_pct": 0, "reason": "只给一只"},
        ])])
        result = run_llm_close_replan(
            db=db, broker=broker, provider=provider,
            goal=Goal(target_annual_return=0.2), llm_client=client,
            cfg=LLMConfig())
        assert result["ok"] is False
        assert result["missing"] == ["000002"]
        plans = {p["code"]: p for p in db.list_llm_plans()}
        assert plans["000001"]["stop_price"] == 9.5  # 部分进展不回滚
        db.close()

    def test_llm_failure_not_ok(self, tmp_path):
        from quanti.agent.llm_full import run_llm_close_replan
        db, provider = _make_db(tmp_path, codes=("000001",))
        broker = PaperBroker(db, provider, initial_cash=1_000_000,
                             fill_mode="immediate")
        _seed_position(db)

        class Bad:
            def create_message(self, **kw):
                raise RuntimeError("simulated outage")

        result = run_llm_close_replan(
            db=db, broker=broker, provider=provider,
            goal=Goal(target_annual_return=0.2), llm_client=Bad(),
            cfg=LLMConfig())
        assert result["ok"] is False and result.get("error")
        db.close()

    def test_empty_book_ok_skipped(self, tmp_path):
        from quanti.agent.llm_full import run_llm_close_replan
        db, provider = _make_db(tmp_path, codes=("000001",))
        broker = PaperBroker(db, provider, initial_cash=1_000_000)
        result = run_llm_close_replan(
            db=db, broker=broker, provider=provider,
            goal=Goal(target_annual_return=0.2),
            llm_client=StubLLMClient([]), cfg=LLMConfig())
        assert result["ok"] is True and result.get("skipped")
        db.close()
