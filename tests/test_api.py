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
        monkeypatch.setenv("QUANTI_LIVE_ACK", "I_KNOW_REAL_MONEY")  # H3: real-money ack
        monkeypatch.setenv("QUANTI_ALLOW_NON_CN_TZ", "1")           # tz-independent test
        app = create_app(db=db, provider=DataProvider(db),
                         strategies_dir="strategies")
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            body = (await c.get("/api/meta")).json()
        assert body == {"account": "live", "is_live": True}


class TestLiveControlEndpoints:
    @pytest.mark.asyncio
    async def test_paper_status_and_arm_refused(self, client):
        """Paper: not live-capable, disarmed, no bridge; arming is refused."""
        s = (await client.get("/api/live/status")).json()
        assert s["is_live"] is False and s["live_capable"] is False
        assert s["orders_armed"] is False and s["bridge"] is None
        r = await client.post("/api/live/orders-armed", json={"armed": True})
        assert r.status_code == 400                     # can't arm on paper

    @pytest.mark.asyncio
    async def test_live_arm_toggle(self, db, monkeypatch):
        monkeypatch.setenv("QUANTI_ACCOUNT", "live")
        monkeypatch.setenv("QUANTI_LIVE_ACK", "I_KNOW_REAL_MONEY")
        monkeypatch.setenv("QUANTI_ALLOW_NON_CN_TZ", "1")
        app = create_app(db=db, provider=DataProvider(db),
                         strategies_dir="strategies")
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            s = (await c.get("/api/live/status")).json()
            assert s["is_live"] is True and s["live_capable"] is True
            assert s["orders_armed"] is False           # disarmed by default
            r = (await c.post("/api/live/orders-armed", json={"armed": True})).json()
            assert r["ok"] is True and r["orders_armed"] is True
            assert (await c.get("/api/live/status")).json()["orders_armed"] is True


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
        assert d["exits"]["stop_loss"]["threshold"] == -0.15  # stop floor
        assert d["exits"]["portfolio_circuit_breaker"]["threshold"] == -0.30
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


class TestRiskAuditStockPnl:
    """/risk/audit 的 stock_pnl:按股票聚合的历史(已平仓)盈亏。"""

    @staticmethod
    def _trade(db, tid, code, direction, qty, price, tdate, comm=0.0,
               created_at=None):
        db.insert_trade({
            "trade_id": tid, "order_id": "", "code": code,
            "direction": direction, "quantity": qty, "price": price,
            "commission": comm, "strategy_name": "",
            "trade_date": tdate,
            "created_at": created_at or f"{tdate}T09:30:00",
        })

    @pytest.mark.asyncio
    async def test_empty_without_trades(self, client):
        r = await client.get("/api/risk/audit")
        assert r.status_code == 200
        assert r.json()["stock_pnl"] == []

    @pytest.mark.asyncio
    async def test_aggregates_per_code(self, client, db):
        # 000001(已入库,平安银行):两笔已平仓,一赢一亏
        self._trade(db, "t1", "000001", "buy", 100, 10.0, "2024-01-05")
        self._trade(db, "t2", "000001", "sell", 100, 12.0, "2024-01-10")  # +200
        self._trade(db, "t3", "000001", "buy", 100, 10.0, "2024-01-15")
        self._trade(db, "t4", "000001", "sell", 100, 9.0, "2024-01-22")   # -100
        # 600999:stocks 表里没有 → name 回退为代码;单笔盈利
        self._trade(db, "t5", "600999", "buy", 200, 5.0, "2024-01-05")
        self._trade(db, "t6", "600999", "sell", 200, 6.0, "2024-01-12")  # +200
        # 未平仓的买入不计入
        self._trade(db, "t7", "000001", "buy", 100, 11.0, "2024-01-25")

        r = await client.get("/api/risk/audit")
        rows = r.json()["stock_pnl"]
        by_code = {x["code"]: x for x in rows}
        assert set(by_code) == {"000001", "600999"}

        p = by_code["000001"]
        assert p["name"] == "平安银行"
        assert p["trips"] == 2
        assert p["total_pnl"] == pytest.approx(100.0)
        assert p["avg_return"] == pytest.approx(0.05)
        assert p["win_rate"] == pytest.approx(0.5)
        assert p["last_sell_date"] == "2024-01-22"
        assert p["last_return"] == pytest.approx(-0.1)

        q = by_code["600999"]
        assert q["name"] == "600999"
        assert q["trips"] == 1
        assert q["total_pnl"] == pytest.approx(200.0)
        assert q["win_rate"] == pytest.approx(1.0)

        # 按累计盈亏降序排列
        assert [x["code"] for x in rows] == ["600999", "000001"]

    @pytest.mark.asyncio
    async def test_last_return_same_day_multi_sell(self, client, db):
        # 同日分批平仓:「最近一笔」应取当天时间最晚的卖出(-10%),
        # 而不是最早的(+20%)——日期并列时不能靠 max() 的取首行为。
        self._trade(db, "m1", "000001", "buy", 100, 10.0, "2024-01-05")
        self._trade(db, "m2", "000001", "buy", 100, 10.0, "2024-01-08")
        self._trade(db, "m3", "000001", "sell", 100, 12.0, "2024-01-10",
                    created_at="2024-01-10T09:31:00")
        self._trade(db, "m4", "000001", "sell", 100, 9.0, "2024-01-10",
                    created_at="2024-01-10T14:00:00")
        r = await client.get("/api/risk/audit")
        p = {x["code"]: x for x in r.json()["stock_pnl"]}["000001"]
        assert p["last_sell_date"] == "2024-01-10"
        assert p["last_return"] == pytest.approx(-0.1)

    @pytest.mark.asyncio
    async def test_fifo_survives_large_trade_history(self, client, db):
        # FIFO 需要完整历史:最旧的买腿即使排在几千笔成交之前也必须参与配对,
        # 否则卖单会错配到更晚的买入、盈亏符号可翻转(newest-N 窗口截断回归)。
        self._trade(db, "old", "000001", "buy", 100, 10.0, "2023-01-05")
        filler = [
            (f"f{i}", "", "600999", "buy", 100, 5.0, 0.0, "",
             "2023-06-01", f"2023-06-01T09:30:{i % 60:02d}.{i:06d}")
            for i in range(2100)
        ]
        db.conn.executemany(
            "INSERT INTO trades (trade_id, order_id, code, direction, quantity, "
            "price, commission, strategy_name, trade_date, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", filler)
        db.conn.commit()
        self._trade(db, "new", "000001", "sell", 100, 12.0, "2024-01-10")
        r = await client.get("/api/risk/audit")
        p = {x["code"]: x for x in r.json()["stock_pnl"]}.get("000001")
        assert p is not None and p["total_pnl"] == pytest.approx(200.0)


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


class TestRiskControlConfig:
    _FULL = {
        "stop_loss_pct": -0.05, "portfolio_stop_loss_pct": -0.12,
        "take_profit_activate_pct": 0.20, "take_profit_trail_pct": 0.08,
        "strategy_exit_enabled": False, "atr_stop_k": 2.0, "atr_stop_n": 10,
    }

    @pytest.mark.asyncio
    async def test_get_defaults(self, client):
        r = await client.get("/api/config/risk-control")
        assert r.status_code == 200
        assert r.json()["stop_loss_pct"] == -0.15  # absolute stop floor default
        assert r.json()["atr_stop_k"] == 2.0  # ATR-adaptive on by default
        assert r.json()["max_position_pct"] == 0.20  # single-stock cap default
        assert r.json()["max_industry_pct"] == 0.30  # industry cap default

    @pytest.mark.asyncio
    async def test_post_persists_and_audit_reflects(self, client):
        r = await client.post("/api/config/risk-control", json=self._FULL)
        assert r.status_code == 200 and r.json()["stop_loss_pct"] == -0.05
        # GET reflects it
        assert (await client.get("/api/config/risk-control")
                ).json()["atr_stop_k"] == 2.0
        # /risk/audit reflects it (reads DB, not stale broker config)
        audit = (await client.get("/api/risk/audit")).json()
        assert audit["exits"]["stop_loss"]["threshold"] == -0.05
        assert audit["exits"]["atr_stop"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_post_rejects_nonneg_stop(self, client):
        bad = {**self._FULL, "stop_loss_pct": 0.05}
        r = await client.post("/api/config/risk-control", json=bad)
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_edit_takes_effect_live_no_restart(self, client, app):
        broker = app.state.broker
        broker._sync_risk_config()
        assert broker._risk.config.stop_loss_pct == -0.15  # default before edit
        await client.post("/api/config/risk-control", json=self._FULL)
        broker._sync_risk_config()  # what check_exits/enforce do each cycle
        assert broker._risk.config.stop_loss_pct == -0.05
        assert broker._risk.config.atr_stop_k == 2.0

    @pytest.mark.asyncio
    async def test_post_persists_concentration_caps(self, client, app):
        body = {**self._FULL, "max_position_pct": 0.25, "max_industry_pct": 0.40}
        r = await client.post("/api/config/risk-control", json=body)
        assert r.status_code == 200
        assert r.json()["max_position_pct"] == 0.25
        # GET reflects it
        got = (await client.get("/api/config/risk-control")).json()
        assert got["max_position_pct"] == 0.25 and got["max_industry_pct"] == 0.40
        # Live broker picks it up on the next sync (no restart)
        broker = app.state.broker
        broker._sync_risk_config()
        assert broker._risk.config.max_position_pct == 0.25
        assert broker._risk.config.max_industry_pct == 0.40

    @pytest.mark.asyncio
    async def test_post_rejects_out_of_range_caps(self, client):
        for field, bad in [("max_position_pct", 0.6),   # > 50% ceiling
                           ("max_position_pct", 0.0),    # must be > 0
                           ("max_industry_pct", 1.5)]:   # > 100%
            r = await client.post("/api/config/risk-control",
                                  json={**self._FULL, field: bad})
            assert r.status_code == 422, f"{field}={bad} should be rejected"


def test_intraday_guard_sec_env(db, monkeypatch):
    """Guard cadence is env-tunable (QUANTI_INTRADAY_GUARD_SEC), default 5s."""
    from quanti.api.app import create_app
    from quanti.data.provider import DataProvider

    monkeypatch.delenv("QUANTI_INTRADAY_GUARD_SEC", raising=False)
    app = create_app(db=db, provider=DataProvider(db), strategies_dir="strategies",
                     autostart_background_sync=False)
    assert app.state.agent._intraday_guard_sec == 5          # default

    monkeypatch.setenv("QUANTI_INTRADAY_GUARD_SEC", "13")
    app2 = create_app(db=db, provider=DataProvider(db), strategies_dir="strategies",
                      autostart_background_sync=False)
    assert app2.state.agent._intraday_guard_sec == 13        # env override
