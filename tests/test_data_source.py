"""Tests for the data-source factory, resolution, probe, and config endpoints."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from quanti.data import source as src
from quanti.data.akshare_adapter import AkShareAdapter
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.data.xtdata_adapter import XtdataAdapter


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    yield d
    d.close()


# --- app_config persistence ------------------------------------------------

def test_app_config_roundtrip_and_token_preserved(db):
    assert db.get_app_config() == {"data_source": "", "data_source_token": "",
                                   "alert_webhook_url": ""}
    db.upsert_app_config("tushare", "tok123")
    cfg = db.get_app_config()
    assert cfg["data_source"] == "tushare" and cfg["data_source_token"] == "tok123"
    # token=None must NOT wipe the stored token (UI can change source only).
    db.upsert_app_config("akshare", None)
    cfg2 = db.get_app_config()
    assert cfg2["data_source"] == "akshare"
    assert cfg2["data_source_token"] == "tok123"


# --- resolve_source precedence --------------------------------------------

def test_resolve_source_precedence(db, monkeypatch):
    monkeypatch.delenv("QUANTI_DATA_SOURCE", raising=False)
    assert src.resolve_source(db) == "tushare"                 # default
    monkeypatch.setenv("QUANTI_DATA_SOURCE", "akshare")
    assert src.resolve_source(db) == "akshare"                 # env > default
    db.upsert_app_config("xtdata", None)
    assert src.resolve_source(db) == "xtdata"                  # DB > env
    assert src.resolve_source(db, explicit="akshare") == "akshare"  # explicit wins


# --- make_quote_adapter + graceful fallback --------------------------------

def test_make_adapter_tushare_when_available(db, monkeypatch):
    from quanti.data.tushare_adapter import TushareAdapter
    monkeypatch.setattr(src, "tushare_available", lambda _db=None: True)
    monkeypatch.setattr(src, "tushare_token", lambda _db=None: "tok")
    assert isinstance(src.make_quote_adapter(db, "tushare"), TushareAdapter)


def test_make_adapter_no_silent_fallback_by_default(db, monkeypatch):
    """Default must NOT silently downgrade tushare→akshare: it raises so the
    DB never gets polluted with a different-convention source unawares."""
    monkeypatch.setattr(src, "tushare_available", lambda _db=None: False)
    with pytest.raises(src.DataSourceUnavailable):
        src.make_quote_adapter(db, "tushare")                 # no allow_fallback
    with pytest.raises(src.DataSourceUnavailable):
        src.make_quote_adapter(db, "tushare", allow_fallback=False)


def test_make_adapter_fallback_only_when_opted_in(db, monkeypatch, caplog):
    """akshare degradation is opt-in via allow_fallback=True (best-effort sites)."""
    monkeypatch.setattr(src, "tushare_available", lambda _db=None: False)
    adapter = src.make_quote_adapter(db, "tushare", allow_fallback=True)
    assert isinstance(adapter, AkShareAdapter)
    assert any("akshare" in r.message for r in caplog.records)


def test_try_make_quote_adapter_returns_message_not_raise(db, monkeypatch):
    """The non-raising variant used by API/daemon returns (None, message)."""
    monkeypatch.setattr(src, "tushare_available", lambda _db=None: False)
    adapter, err = src.try_make_quote_adapter(db, "tushare")
    assert adapter is None
    assert err and "token" in err.lower()
    # Available → (adapter, None).
    monkeypatch.setattr(src, "tushare_available", lambda _db=None: True)
    monkeypatch.setattr(src, "tushare_token", lambda _db=None: "tok")
    adapter2, err2 = src.try_make_quote_adapter(db, "tushare")
    assert adapter2 is not None and err2 is None


def test_make_adapter_explicit_sources(db):
    assert isinstance(src.make_quote_adapter(db, "akshare"), AkShareAdapter)
    assert isinstance(src.make_quote_adapter(db, "xtdata"), XtdataAdapter)


# --- make_financials_adapter (daemon follows source, akshare backstop) ------

def test_financials_adapter_follows_source_with_akshare_backstop(db, monkeypatch):
    from quanti.data.tushare_adapter import TushareAdapter

    # tushare available → tushare (real ann_date via VIP); has the by-period method
    monkeypatch.setattr(src, "tushare_available", lambda _db=None: True)
    monkeypatch.setattr(src, "tushare_token", lambda _db=None: "tok")
    a = src.make_financials_adapter(db, "tushare")
    assert isinstance(a, TushareAdapter)
    assert hasattr(a, "sync_financials_by_period")

    # xtdata has no financials endpoint → silently backstops to free akshare
    assert isinstance(src.make_financials_adapter(db, "xtdata"), AkShareAdapter)

    # tushare selected but unavailable (no token) → akshare backstop, not a raise
    monkeypatch.setattr(src, "tushare_available", lambda _db=None: False)
    assert isinstance(src.make_financials_adapter(db, "tushare"), AkShareAdapter)


def test_financials_backstop_falls_through_on_zero(db, monkeypatch):
    """The follow-source helpers must heal a 0-row result (e.g. tushare selected
    but no VIP tier) by falling through to free akshare — but NOT when the source
    already returned rows, and NOT double-call when the source IS akshare."""
    import quanti.data.akshare_adapter as ak_mod
    from datetime import date

    calls = []

    def _rec(tag, n):
        calls.append(tag)
        return n

    class FakeAk:
        def __init__(self, _db):
            pass

        @staticmethod
        def report_periods(_years):
            return [date(2024, 3, 31)]

        def sync_financials_by_period(self, _p):
            return _rec("ak", 5)

        def sync_financials(self, _y):
            return _rec("ak", 5)

    class FakeTu:   # tushare-like: returns 0 (no VIP)
        def sync_financials_by_period(self, _p):
            return _rec("tu", 0)

        def sync_financials(self, _y):
            return _rec("tu", 0)

    monkeypatch.setattr(ak_mod, "AkShareAdapter", FakeAk)

    # tushare returns 0 → backstop to akshare (both multi-period and latest)
    monkeypatch.setattr(src, "make_financials_adapter", lambda _db, s=None: FakeTu())
    calls.clear()
    assert src.sync_financials_years(db, 3) == 5 and calls == ["tu", "ak"]
    calls.clear()
    assert src.refresh_latest_financials(db) == 5 and calls == ["tu", "ak"]

    # source returns rows → no backstop
    class FakeTuOK(FakeTu):
        def sync_financials(self, _y):
            return _rec("tu", 9)
    monkeypatch.setattr(src, "make_financials_adapter", lambda _db, s=None: FakeTuOK())
    calls.clear()
    assert src.sync_financials_years(db, 3) == 9 and calls == ["tu"]

    # source already IS akshare → never double-call even on 0
    class FakeAkZero(FakeAk):
        def sync_financials(self, _y):
            return _rec("ak", 0)
    monkeypatch.setattr(src, "make_financials_adapter", lambda _db, s=None: FakeAkZero(db))
    calls.clear()
    assert src.sync_financials_years(db, 3) == 0 and calls == ["ak"]


# --- probe_source ----------------------------------------------------------

def test_probe_tushare_success_and_failure(db, monkeypatch):
    import quanti.data.tushare_adapter as ta

    class _Pro:
        def trade_cal(self, **kw):
            import pandas as pd
            return pd.DataFrame({"cal_date": ["20240102"]})

    class _TS:
        @staticmethod
        def pro_api(token):
            return _Pro()

    monkeypatch.setattr(ta, "ts", _TS)
    ok, msg = src.probe_source("tushare", token="tok")
    assert ok is True and "成功" in msg
    # No token → fail without network.
    ok2, msg2 = src.probe_source("tushare", token="")
    assert ok2 is False and "token" in msg2.lower()


def test_probe_unknown_source(db):
    ok, msg = src.probe_source("nope")
    assert ok is False


# --- config endpoints ------------------------------------------------------

@pytest.fixture
def app(db):
    from quanti.api.app import create_app
    return create_app(db=db, provider=DataProvider(db), strategies_dir="strategies")


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_get_data_source_never_leaks_token(db, app, monkeypatch):
    monkeypatch.delenv("QUANTI_DATA_SOURCE", raising=False)
    db.upsert_app_config("tushare", "secret-token")
    async with _client(app) as c:
        r = await c.get("/api/config/data-source")
    body = r.json()
    assert r.status_code == 200
    assert body["source"] == "tushare" and body["has_token"] is True
    assert "secret-token" not in r.text and "token" not in body  # masked


@pytest.mark.asyncio
async def test_sync_stocks_rate_limit_returns_clean_error_not_500(db, app, monkeypatch):
    """A rate-limited stock_basic must yield a clean error payload, not a 500."""
    class Boom:
        def sync_stock_list(self):
            raise Exception("抱歉，您访问接口(stock_basic)频率超限(1次/分钟)")

    monkeypatch.setattr(src, "try_make_quote_adapter",
                        lambda d, s=None: (Boom(), None))
    async with _client(app) as c:
        r = await c.post("/api/sync/stocks")
    assert r.status_code == 200                 # clean, NOT 500
    body = r.json()
    assert body["synced"] == 0
    assert "频率超限" in body["error"]


@pytest.mark.asyncio
async def test_set_data_source_persists_only_when_probe_ok(db, app, monkeypatch):
    monkeypatch.setattr(src, "probe_source",
                        lambda s, t=None, d=None: (False, "bad token"))
    async with _client(app) as c:
        r = await c.post("/api/config/data-source",
                         json={"source": "tushare", "token": "x"})
    assert r.json() == {"ok": False, "message": "bad token"}
    assert db.get_app_config()["data_source"] == ""   # NOT persisted on failure

    monkeypatch.setattr(src, "probe_source",
                        lambda s, t=None, d=None: (True, "ok"))
    async with _client(app) as c:
        r2 = await c.post("/api/config/data-source",
                          json={"source": "tushare", "token": "good"})
    assert r2.json()["ok"] is True
    cfg = db.get_app_config()
    assert cfg["data_source"] == "tushare" and cfg["data_source_token"] == "good"
