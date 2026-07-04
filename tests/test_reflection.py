"""Tests for outcome-keyed reflection memory.

Covers FIFO realized-return reconstruction, relevance-ranked retrieval
(code-level then industry-level), context rendering, and the end-to-end
wiring through run_llm_decision. No network.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from quanti.agent.goal import Goal
from quanti.agent.llm_runtime import LLMConfig, build_context_message, run_llm_decision
from quanti.agent.reflection import (
    build_reflections,
    format_reflections,
    realized_trips,
)
from quanti.agent.signal_pipeline import FusedCandidate
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.paper_broker import PaperBroker


def _t(code, direction, qty, price, tdate, comm=0.0):
    return {"code": code, "direction": direction, "quantity": qty,
            "price": price, "commission": comm, "trade_date": tdate,
            "created_at": f"{tdate}T09:30:00"}


# ----- FIFO realized return ---------------------------------------------

class TestRealizedTrips:
    def test_single_round_trip(self):
        trips = realized_trips([
            _t("600000", "buy", 100, 10.0, "2026-01-01"),
            _t("600000", "sell", 100, 12.0, "2026-01-11"),
        ])
        assert len(trips) == 1
        assert trips[0]["realized_return"] == pytest.approx(0.2)
        assert trips[0]["holding_days"] == 10
        assert trips[0]["qty"] == 100

    def test_fifo_multi_lot(self):
        trips = realized_trips([
            _t("X", "buy", 100, 10.0, "2026-01-01"),
            _t("X", "buy", 100, 20.0, "2026-01-02"),
            _t("X", "sell", 150, 30.0, "2026-01-05"),
        ])
        assert len(trips) == 1
        # matched cost = (100*10 + 50*20) / 150 = 13.333...
        assert trips[0]["realized_return"] == pytest.approx((30 - 40 / 3) / (40 / 3), rel=1e-3)
        assert trips[0]["qty"] == 150

    def test_commission_nets_into_return(self):
        # buy 100@10 (+10 comm → 10.1/sh), sell 100@10 (-10 comm → 9.9/sh) → loss
        trips = realized_trips([
            _t("X", "buy", 100, 10.0, "2026-01-01", comm=10.0),
            _t("X", "sell", 100, 10.0, "2026-01-02", comm=10.0),
        ])
        assert trips[0]["realized_return"] < 0

    def test_open_position_yields_no_trip(self):
        assert realized_trips([_t("X", "buy", 100, 10.0, "2026-01-01")]) == []


# ----- build_reflections -------------------------------------------------

@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "refl.db"))
    d.initialize()
    yield d
    d.close()


def _insert_trade(db, tid, code, direction, qty, price, tdate, comm=0.0):
    db.conn.execute(
        "INSERT INTO trades (trade_id, order_id, code, direction, quantity, "
        "price, commission, strategy_name, trade_date, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tid, "", code, direction, qty, price, comm, "", tdate,
         f"{tdate}T09:30:00"))
    db.conn.commit()


class TestBuildReflections:
    def test_code_level(self, db):
        db.upsert_stock("600000", "浦发", "SH", date(1999, 11, 10), "银行")
        _insert_trade(db, "t1", "600000", "buy", 100, 10.0, "2026-01-01")
        _insert_trade(db, "t2", "600000", "sell", 100, 11.0, "2026-01-06")
        cands = [FusedCandidate(code="600000", strategy_score=0.5, factor_score=0.0,
                                final_score=0.5, industry="银行")]
        items = build_reflections(db, cands)
        code_items = [i for i in items if i["scope"] == "code"]
        assert code_items and code_items[0]["key"] == "600000"
        assert code_items[0]["avg_return"] == pytest.approx(0.1)
        assert "600000" in format_reflections(items)

    def test_industry_level_aggregates_peers(self, db):
        db.upsert_stock("600000", "浦发", "SH", date(1999, 11, 10), "银行")
        db.upsert_stock("600036", "招商", "SH", date(2002, 4, 9), "银行")
        # Candidate is 600000; the *traded* peer 600036 (same industry) feeds
        # the industry-level lesson.
        _insert_trade(db, "t1", "600036", "buy", 100, 10.0, "2026-01-01")
        _insert_trade(db, "t2", "600036", "sell", 100, 9.0, "2026-01-06")  # -10%
        cands = [FusedCandidate(code="600000", strategy_score=0.5, factor_score=0.0,
                                final_score=0.5, industry="银行")]
        items = build_reflections(db, cands)
        ind = [i for i in items if i["scope"] == "industry"]
        assert ind and ind[0]["key"] == "银行"
        assert ind[0]["avg_return"] == pytest.approx(-0.1)
        # 600000 itself has no trades → no code-level lesson.
        assert not [i for i in items if i["scope"] == "code"]

    def test_empty_when_no_trades(self, db):
        cands = [FusedCandidate(code="600000", strategy_score=0.5, factor_score=0.0,
                                final_score=0.5, industry="银行")]
        assert build_reflections(db, cands) == []

    def test_fifo_survives_large_trade_history(self, db):
        # Regression: build_reflections used to FIFO-match over only the
        # newest 500 trades (ORDER BY created_at DESC LIMIT). Once history
        # grew past the window, the oldest buy legs were cut off and sells
        # matched a later lot — or nothing — so the avg/last returns fed to
        # the LLM were wrong. FIFO needs the full history.
        db.upsert_stock("600000", "浦发", "SH", date(1999, 11, 10), "银行")
        _insert_trade(db, "t-buy", "600000", "buy", 100, 10.0, "2026-01-01")
        # 600 filler buys on another name push the buy leg out of any
        # recent-N window (distinct created_at keeps DESC order stable).
        db.conn.executemany(
            "INSERT INTO trades (trade_id, order_id, code, direction, quantity, "
            "price, commission, strategy_name, trade_date, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(f"f{i}", "", "000002", "buy", 100, 5.0, 0.0, "", "2026-01-02",
              f"2026-01-02T10:00:00.{i:06d}") for i in range(600)])
        db.conn.commit()
        _insert_trade(db, "t-sell", "600000", "sell", 100, 11.0, "2026-01-05")

        cands = [FusedCandidate(code="600000", strategy_score=0.5, factor_score=0.0,
                                final_score=0.5, industry="银行")]
        items = build_reflections(db, cands)
        code_items = [i for i in items if i["scope"] == "code"
                      and i["key"] == "600000"]
        assert code_items, "sell must still match its buy lot beyond 500 trades"
        assert code_items[0]["avg_return"] == pytest.approx(0.1)


# ----- context rendering -------------------------------------------------

def test_context_includes_reflections():
    msg = build_context_message(
        Goal(), {"total_value": 0, "cash": 0, "positions": []},
        candidates=[], recent_decisions=[],
        reflections=[{"text": "600000: 历史已平仓 2 笔, 平均 +5.0%"}])
    assert "历史经验" in msg
    assert "600000: 历史已平仓 2 笔" in msg


# ----- end-to-end wiring -------------------------------------------------

@pytest.fixture
def llm_setup(tmp_path):
    db = Database(str(tmp_path / "refl_e2e.db"))
    db.initialize()
    today = pd.Timestamp.today().normalize()
    dates = pd.bdate_range(end=today, periods=80)
    np.random.seed(5)
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


class StubLLMClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create_message(self, **kw):
        self.calls.append(kw)
        return self._responses.pop(0)


def test_reflection_enabled_flows_into_result(llm_setup):
    db, _, broker = llm_setup
    # A prior closed round-trip on the candidate name.
    _insert_trade(db, "t1", "000001", "buy", 100, 10.0, "2026-01-01")
    _insert_trade(db, "t2", "000001", "sell", 100, 11.0, "2026-01-06")
    client = StubLLMClient([{
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "p1", "name": "propose_orders",
                     "input": {"orders": [], "reasoning": "观望"}}],
        "usage": {"input_tokens": 50, "output_tokens": 10},
    }])
    cands = [FusedCandidate(code="000001", strategy_score=0.7, factor_score=0.5,
                            final_score=0.7, industry="银行")]
    cfg = LLMConfig(reflection_enabled=True)
    res = run_llm_decision(db=db, broker=broker, goal=Goal(),
                           candidates=cands, llm_client=client, cfg=cfg)
    assert any(r["scope"] == "code" and r["key"] == "000001"
               for r in res["reflections"])
