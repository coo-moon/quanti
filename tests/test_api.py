"""Tests for FastAPI backend."""

from datetime import date
from unittest.mock import patch

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
