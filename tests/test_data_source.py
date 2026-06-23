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
    assert db.get_app_config() == {"data_source": "", "data_source_token": ""}
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
