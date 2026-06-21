"""Tests for TushareAdapter using injected fakes — never touches the network."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from quanti.data.database import Database
from quanti.data.tushare_adapter import TushareAdapter


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    yield d
    d.close()


class FakePro:
    """Stand-in for tushare's pro_api object (provides stock_basic)."""

    def stock_basic(self, list_status, fields):
        if list_status == "L":
            return pd.DataFrame([
                {"ts_code": "000001.SZ", "name": "平安银行",
                 "list_date": "19910403", "delist_date": None},
            ])
        if list_status == "D":
            return pd.DataFrame([
                {"ts_code": "600001.SH", "name": "邯郸钢铁",
                 "list_date": "19980122", "delist_date": "20100824"},
            ])
        return pd.DataFrame(columns=["ts_code", "name", "list_date", "delist_date"])


def _fake_pro_bar(ts_code, asset, adj, start_date, end_date):
    # tushare returns newest-first; columns trade_date/open/high/low/close/vol/amount
    return pd.DataFrame([
        {"ts_code": ts_code, "trade_date": "20100120", "open": 3.0, "high": 3.2,
         "low": 2.9, "close": 3.1, "vol": 1000.0, "amount": 3_100_000.0},
        {"ts_code": ts_code, "trade_date": "20100119", "open": 3.1, "high": 3.3,
         "low": 3.0, "close": 3.0, "vol": 1200.0, "amount": 3_600_000.0},
    ])


def test_code_ts_code_mapping():
    assert TushareAdapter._code_to_ts_code("600519") == "600519.SH"
    assert TushareAdapter._code_to_ts_code("000001") == "000001.SZ"
    assert TushareAdapter._code_to_ts_code("830799") == "830799.BJ"
    assert TushareAdapter._ts_code_to_code("600519.SH") == ("600519", "SH")
    assert TushareAdapter._ts_code_to_code("000001.SZ") == ("000001", "SZ")
    assert TushareAdapter._ts_code_to_code("830799.BJ") == ("830799", "BJ")


def test_sync_stock_list_includes_delisted(db):
    adapter = TushareAdapter(db, pro=FakePro())
    n = adapter.sync_stock_list()
    assert n == 2
    listed = db.get_stock("000001")
    delisted = db.get_stock("600001")
    assert listed is not None and listed.delist_date is None
    assert delisted is not None and delisted.delist_date == date(2010, 8, 24)
    assert delisted.exchange == "SH"


def test_sync_daily_quotes_lands_with_zero_turnover(db):
    db.upsert_stock("600001", "邯郸钢铁", "SH", date(1998, 1, 22), "",
                    delist_date=date(2010, 8, 24))
    adapter = TushareAdapter(db, pro=FakePro(), pro_bar=_fake_pro_bar)
    saved = adapter.sync_daily_quotes("600001", start=date(2010, 1, 1),
                                      end=date(2010, 1, 31))
    assert saved == 2
    out = db.get_daily_quotes("600001", date(2010, 1, 1), date(2010, 1, 31))
    assert len(out) == 2
    assert (out["close"] > 0).all()
    assert (out["turnover"] == 0).all()  # free tier has no turnover


def test_methods_raise_clearly_without_token(db, monkeypatch):
    # No pro injected, no TUSHARE_TOKEN → clear error, no token leak.
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    import quanti.data.tushare_adapter as mod
    monkeypatch.setattr(mod, "ts", None)  # simulate tushare not installed
    adapter = TushareAdapter(db)
    with pytest.raises(RuntimeError):
        adapter.sync_stock_list()


def test_bar_fn_raises_clearly_when_tushare_absent(db, monkeypatch):
    # pro injected but pro_bar NOT injected, and tushare package absent →
    # _bar_fn must raise the clear RuntimeError, not an AttributeError.
    import quanti.data.tushare_adapter as mod
    monkeypatch.setattr(mod, "ts", None)
    adapter = TushareAdapter(db, pro=FakePro())  # no pro_bar
    with pytest.raises(RuntimeError):
        adapter.sync_daily_quotes("600001", start=date(2010, 1, 1),
                                  end=date(2010, 1, 31))
