"""tushare 实时兜底源 + 双源组合器:符号映射/新鲜度过滤/退避/兜底切换。"""

from __future__ import annotations

import pytest

from quanti.data import realtime, tencent_quotes, tushare_quotes
from quanti.data.database import Database


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    monkeypatch.setattr(tushare_quotes, "_cache", None)
    monkeypatch.setattr(tushare_quotes, "_last_fail", None)


def test_ts_symbol_mapping():
    assert tushare_quotes._ts_symbol("600519") == "600519.SH"
    assert tushare_quotes._ts_symbol("930001") == "930001.SH"  # 6/9 开头 → SH
    assert tushare_quotes._ts_symbol("000001") == "000001.SZ"
    assert tushare_quotes._ts_symbol("300750") == "300750.SZ"
    assert tushare_quotes._ts_symbol("830001") == "830001.BJ"
    assert tushare_quotes._ts_symbol("920001") == "920001.BJ"


def test_rows_to_prices_filters_stale_and_bad():
    rows = [
        {"TS_CODE": "000001.SZ", "PRICE": 11.39, "DATE": "20260820"},
        {"TS_CODE": "000002.SZ", "PRICE": 0.0, "DATE": "20260820"},    # 无价
        {"TS_CODE": "000703.SZ", "PRICE": 20.0, "DATE": "20260818"},   # 旧戳(停牌)
        {"TS_CODE": "601328.SH", "PRICE": "7.12", "DATE": "20260820"},
        {"TS_CODE": "", "PRICE": 9.9, "DATE": "20260820"},             # 缺码
    ]
    out = tushare_quotes._rows_to_prices(rows, "20260820")
    assert out == {"000001": 11.39, "601328": 7.12}


def test_fetch_backs_off_after_failure(monkeypatch):
    calls = {"n": 0}

    class BoomTS:
        @staticmethod
        def set_token(t):
            pass

        @staticmethod
        def realtime_quote(**kw):
            calls["n"] += 1
            raise RuntimeError("sina down")

    import sys
    monkeypatch.setitem(sys.modules, "tushare", BoomTS)
    assert tushare_quotes.fetch_last_prices(["000001"], "tok") == {}
    assert calls["n"] == 1
    # 退避窗口内不再打源
    assert tushare_quotes.fetch_last_prices(["000001"], "tok") == {}
    assert calls["n"] == 1


def test_fetch_requires_token():
    assert tushare_quotes.fetch_last_prices(["000001"], "") == {}


# ------------------------------------------------------------- 组合器

@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    d.upsert_app_config("tushare", "tok-xyz")
    yield d
    d.close()


def test_combiner_prefers_tushare(db, monkeypatch):
    """用户拍板(2026-08-20):tushare 主源。有货时不打腾讯。"""
    monkeypatch.setattr(tushare_quotes, "fetch_last_prices",
                        lambda codes, token: {"000001": 11.2}
                        if token == "tok-xyz" else {})
    called = {"tencent": False}
    monkeypatch.setattr(tencent_quotes, "fetch_last_prices",
                        lambda codes: called.update(tencent=True) or {})
    fetch = realtime.make_realtime_fetcher(db)
    assert fetch(["000001"]) == {"000001": 11.2}
    assert called["tencent"] is False


def test_combiner_falls_back_to_tencent_when_tushare_empty(db, monkeypatch):
    monkeypatch.setattr(tushare_quotes, "fetch_last_prices",
                        lambda codes, token: {})
    monkeypatch.setattr(tencent_quotes, "fetch_last_prices",
                        lambda codes: {"000001": 11.0})
    fetch = realtime.make_realtime_fetcher(db)
    assert fetch(["000001"]) == {"000001": 11.0}


def test_combiner_no_token_goes_straight_to_tencent(tmp_path, monkeypatch):
    d = Database(str(tmp_path / "nt.db"))
    d.initialize()
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    called = {"tushare": False}
    monkeypatch.setattr(tushare_quotes, "fetch_last_prices",
                        lambda codes, token: called.update(tushare=True) or {})
    monkeypatch.setattr(tencent_quotes, "fetch_last_prices",
                        lambda codes: {"000001": 11.0})
    fetch = realtime.make_realtime_fetcher(d)
    assert fetch(["000001"]) == {"000001": 11.0}
    assert called["tushare"] is False  # 无 token 不打 tushare
    d.close()


def test_combiner_both_empty_returns_empty(db, monkeypatch):
    monkeypatch.setattr(tushare_quotes, "fetch_last_prices",
                        lambda codes, token: {})
    monkeypatch.setattr(tencent_quotes, "fetch_last_prices", lambda codes: {})
    fetch = realtime.make_realtime_fetcher(db)
    assert fetch(["000001"]) == {}


def test_combiner_tencent_raise_swallowed(db, monkeypatch):
    monkeypatch.setattr(tushare_quotes, "fetch_last_prices",
                        lambda codes, token: {})

    def boom(codes):
        raise RuntimeError("qt.gtimg.cn down")

    monkeypatch.setattr(tencent_quotes, "fetch_last_prices", boom)
    fetch = realtime.make_realtime_fetcher(db)
    assert fetch(["000001"]) == {}
