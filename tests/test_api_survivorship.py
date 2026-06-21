"""Survivorship-free switch on POST /api/backtest/run."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
from httpx import ASGITransport, AsyncClient

from quanti.api.app import create_app
from quanti.data.database import Database
from quanti.data.provider import DataProvider


def _seed_bars(db, code, start="2021-01-04", periods=260):
    dates = pd.bdate_range(start, periods=periods)
    np.random.seed(7)
    prices = 10 + np.cumsum(np.random.randn(periods) * 0.1)
    db.save_daily_quotes(pd.DataFrame({
        "code": code,
        "date": [d.date() for d in dates],
        "open": prices - 0.1, "high": prices + 0.3, "low": prices - 0.3,
        "close": prices,
        "volume": np.random.randint(500000, 2000000, periods).astype(float),
        "amount": prices * 1_000_000, "turnover": 0.0,
    }))
    db.save_trade_calendar([d.date() for d in dates])


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    d.upsert_stock("000001", "在市", "SZ", date(2010, 1, 1), "银行")
    d.upsert_stock("600001", "退市", "SH", date(2000, 1, 1), "",
                   delist_date=date(2022, 6, 1))
    _seed_bars(d, "000001")
    _seed_bars(d, "600001")
    yield d
    d.close()


@pytest.fixture
def client(db):
    provider = DataProvider(db)
    app = create_app(db=db, provider=provider, strategies_dir="strategies")
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test"), db


@pytest.mark.asyncio
async def test_backtest_survivorship_free_consults_pit_universe(client, monkeypatch):
    ac, db = client
    captured = {}
    real = db.point_in_time_universe

    def spy(start, end):
        codes = real(start, end)
        captured["codes"] = codes
        return codes

    monkeypatch.setattr(db, "point_in_time_universe", spy)

    async with ac as c:
        # The universe substitution happens right after date parsing, BEFORE
        # strategy resolution — so the spy fires even with a bogus strategy
        # name (route then returns {"error": ...} with status 200). That keeps
        # the test independent of whatever strategies/ ships.
        r = await c.post("/api/backtest/run", json={
            "strategy_name": "does-not-exist",
            "codes": [],
            "start": "2021-06-01",
            "end": "2022-01-01",
            "survivorship_free": True,
            "max_universe": 300,
        })
    assert r.status_code == 200
    # The PIT universe for this window includes the delisted 600001.
    assert captured["codes"] == ["000001", "600001"]


@pytest.mark.asyncio
async def test_backtest_survivorship_free_caps_universe(client, monkeypatch):
    ac, db = client
    import quanti.api.routes as mod

    captured = {}

    class FakeResult:
        metrics = {}
        skip_reason = ""
        trades = []
        equity_curve = {}

    class FakeEngine:
        def __init__(self, **kwargs):
            pass

        def run(self, strategy, codes, start, end):
            captured["codes"] = list(codes)
            return FakeResult()

    class FakeStrategy:
        name = "dummy"

        def init(self, params):
            pass

    class FakeLoader:
        def load_directory(self, _d):
            return [FakeStrategy()]

    monkeypatch.setattr(mod, "BacktestEngine", FakeEngine)
    monkeypatch.setattr(mod, "StrategyLoader", FakeLoader)

    async with ac as c:
        r = await c.post("/api/backtest/run", json={
            "strategy_name": "dummy",
            "codes": [],
            "start": "2021-06-01",
            "end": "2022-01-01",
            "survivorship_free": True,
            "max_universe": 1,
        })
    assert r.status_code == 200
    # PIT universe for this window is ["000001", "600001"]; cap=1 keeps only the first.
    assert captured["codes"] == ["000001"]
