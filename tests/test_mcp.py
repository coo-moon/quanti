"""Tests for the MCP stdio server's tool dispatcher."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from quanti.data.database import Database
from quanti.mcp_server import QuantiContext, _handle_tool_call, _tool_specs


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    db_path = str(tmp_path / "mcp.db")
    monkeypatch.chdir(tmp_path)  # so any relative "strategies" path is harmless
    # Pre-seed before QuantiContext opens its own connection.
    db = Database(db_path)
    db.initialize()
    db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=80)
    np.random.seed(7)
    prices = 10 + np.cumsum(np.random.randn(len(dates)) * 0.05)
    df = pd.DataFrame({
        "code": "000001",
        "date": [d.date() for d in dates],
        "open": prices - 0.05, "high": prices + 0.1, "low": prices - 0.1,
        "close": prices, "volume": np.full(len(dates), 1_000_000.0),
        "amount": prices * 1_000_000, "turnover": np.full(len(dates), 1.0),
    })
    db.save_daily_quotes(df)
    db.close()
    return QuantiContext(db_path=db_path,
                         strategies_dir="/Users/coo/source/quanti/strategies",
                         screeners_dir="/Users/coo/source/quanti/screeners")


def test_tool_specs_contain_essentials():
    names = {t["name"] for t in _tool_specs()}
    for n in ["set_goal", "get_goal", "agent_start", "agent_tick",
              "get_portfolio", "place_order", "list_decisions", "run_backtest"]:
        assert n in names


def test_get_goal_returns_default(ctx):
    result = _handle_tool_call(ctx, "get_goal", {})
    assert "target_annual_return" in result


def test_set_goal_then_get(ctx):
    _handle_tool_call(ctx, "set_goal", {"target_annual_return": 0.35,
                                        "risk_tolerance": "high"})
    g = _handle_tool_call(ctx, "get_goal", {})
    assert g["target_annual_return"] == 0.35
    assert g["risk_tolerance"] == "high"


def test_place_order_buy_then_portfolio(ctx):
    res = _handle_tool_call(ctx, "place_order", {
        "code": "000001", "direction": "buy", "strength": 0.3,
    })
    assert res["filled"] is True
    p = _handle_tool_call(ctx, "get_portfolio", {})
    assert any(pos["code"] == "000001" for pos in p["positions"])


def test_list_strategies(ctx):
    result = _handle_tool_call(ctx, "list_strategies", {})
    names = {s["name"] for s in result}
    assert "ma_cross" in names
