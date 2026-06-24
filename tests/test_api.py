"""Tests for FastAPI backend."""

from datetime import date

import numpy as np
import pandas as pd
import pytest
from httpx import ASGITransport, AsyncClient

from quanti.api.app import create_app
from quanti.data.database import Database
from quanti.data.provider import DataProvider


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    database.initialize()
    # Seed some data
    database.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
    dates = pd.bdate_range("2024-01-02", periods=30)
    np.random.seed(42)
    prices = 10 + np.cumsum(np.random.randn(30) * 0.1)
    df = pd.DataFrame(
        {
            "code": "000001",
            "date": [d.date() for d in dates],
            "open": prices - 0.1,
            "high": prices + 0.3,
            "low": prices - 0.3,
            "close": prices,
            "volume": np.random.randint(500000, 2000000, 30).astype(float),
            "amount": prices * 1_000_000,
            "turnover": np.random.rand(30) * 3,
        }
    )
    database.save_daily_quotes(df)
    database.save_trade_calendar([d.date() for d in dates])
    yield database
    database.close()


@pytest.fixture
def app(db):
    provider = DataProvider(db)
    return create_app(db=db, provider=provider, strategies_dir="strategies")


@pytest.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health(self, client):
        response = await client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestMetaEndpoint:
    @pytest.mark.asyncio
    async def test_meta_defaults_paper(self, client):
        r = await client.get("/api/meta")
        assert r.status_code == 200
        assert r.json() == {"account": "paper", "is_live": False}

    @pytest.mark.asyncio
    async def test_meta_reflects_live_account(self, db, monkeypatch):
        monkeypatch.setenv("QUANTI_ACCOUNT", "live")
        app = create_app(db=db, provider=DataProvider(db),
                         strategies_dir="strategies")
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            body = (await c.get("/api/meta")).json()
        assert body == {"account": "live", "is_live": True}


class TestStockEndpoints:
    @pytest.mark.asyncio
    async def test_list_stocks(self, client):
        response = await client.get("/api/stocks")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["code"] == "000001"

    @pytest.mark.asyncio
    async def test_get_stock_quotes(self, client):
        response = await client.get(
            "/api/stocks/000001/quotes",
            params={"start": "2024-01-01", "end": "2024-02-28"},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) > 0


class TestBacktestEndpoint:
    @pytest.mark.asyncio
    async def test_run_backtest(self, client):
        response = await client.post(
            "/api/backtest/run",
            json={
                "strategy_name": "ma_cross",
                "codes": ["000001"],
                "start": "2024-01-01",
                "end": "2024-02-28",
                "initial_cash": 100000,
                "params": {"short_period": 5, "long_period": 20},
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "metrics" in data
        assert "trades" in data


class TestRiskAuditEndpoint:
    @pytest.mark.asyncio
    async def test_audit_shape_and_parity(self, client):
        r = await client.get("/api/risk/audit")
        assert r.status_code == 200
        d = r.json()
        assert d["account"] == "paper"
        assert d["exits"]["stop_loss"]["threshold"] == -0.08
        assert d["exits"]["portfolio_circuit_breaker"]["threshold"] == -0.15
        chans = {c["channel"]: c for c in d["channel_parity"]}
        assert len(chans) == 3
        # P0-1: live QMT now matches backtest/paper on the overlays it can run.
        assert chans["实盘 QMT"]["trailing_tp"] is True
        assert chans["实盘 QMT"]["strategy_exit"] is True
        assert d["guard"]["enabled"] is True
        assert isinstance(d["recent_exits"], list)

    @pytest.mark.asyncio
    async def test_audit_lists_recent_risk_exit(self, client, db):
        from datetime import datetime
        now = datetime.now().isoformat()
        db.insert_order({
            "order_id": "o1", "code": "000001", "direction": "sell",
            "quantity": 100, "price_type": "limit", "limit_price": 9.0,
            "status": "filled", "strategy_name": "risk_exit",
            "filled_price": 9.0, "filled_quantity": 100,
            "reason": "止损 -10% ≤ -8%", "created_at": now, "filled_at": now,
            "entry_strategy": "",
        })
        r = await client.get("/api/risk/audit")
        exits = r.json()["recent_exits"]
        assert any(e["code"] == "000001" and e["kind"] == "stop_loss"
                   for e in exits)


def test_exit_kind_classification():
    from quanti.api.routes import _exit_kind
    assert _exit_kind({"reason": "止损 -10% ≤ -8%",
                       "strategy_name": "risk_exit"}) == "stop_loss"
    assert _exit_kind({"reason": "移动止盈 +20% 自峰值回撤-11%",
                       "strategy_name": "risk_exit"}) == "trailing_tp"
    assert _exit_kind({"reason": "策略离场信号",
                       "strategy_name": "risk_exit"}) == "strategy_exit"
    assert _exit_kind({"reason": "组合回撤熔断",
                       "strategy_name": "kill_switch"}) == "circuit_breaker"
