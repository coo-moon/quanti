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

    def trade_cal(self, exchange, is_open):
        return pd.DataFrame([{"cal_date": "20240102"}, {"cal_date": "20240103"}])

    # --- by-date (whole-market) endpoints for backfill ---
    def daily(self, trade_date):
        return pd.DataFrame([
            {"ts_code": "000001.SZ", "open": 10.0, "high": 10.5, "low": 9.8,
             "close": 10.2, "vol": 1000.0, "amount": 1020.0},
            {"ts_code": "600001.SH", "open": 3.0, "high": 3.1, "low": 2.9,
             "close": 3.0, "vol": 500.0, "amount": 150.0},  # delisted-style code
        ])

    def adj_factor(self, trade_date):
        return pd.DataFrame([
            {"ts_code": "000001.SZ", "adj_factor": 1.5},
            {"ts_code": "600001.SH", "adj_factor": 2.0},
        ])

    def daily_basic(self, trade_date, fields):
        return pd.DataFrame([
            {"ts_code": "000001.SZ", "turnover_rate": 1.2},
            {"ts_code": "600001.SH", "turnover_rate": 0.8},
        ])


def test_sync_by_date_whole_market(db):
    """One call set covers the whole market for a day: units normalized, native
    adj_factor stored, turnover from daily_basic, delisted code lands too (P3)."""
    adapter = TushareAdapter(db, pro=FakePro())
    n = adapter.sync_daily_quotes_by_date(date(2024, 1, 2))
    assert n == 2
    out = db.get_daily_quotes("000001", date(2024, 1, 1), date(2024, 1, 3))
    row = out.iloc[0]
    assert row["close"] == 10.2
    assert row["volume"] == 1000.0 * 100        # 手 → 股
    assert row["amount"] == 1020.0 * 1000        # 千元 → 元
    assert row["adj_factor"] == 1.5              # tushare native factor
    assert row["turnover"] == 1.2                # from daily_basic
    assert db.get_quote_source("000001") == "tushare"
    # the delisted-style 600001 also landed
    assert len(db.get_daily_quotes("600001", date(2024, 1, 1), date(2024, 1, 3))) == 1


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


def test_units_normalized_and_source_tagged(db):
    """vol(手)→股 ×100, amount(千元)→元 ×1000, and source='tushare' (P2)."""
    db.upsert_stock("600001", "x", "SH", date(1998, 1, 22), "")
    adapter = TushareAdapter(db, pro=FakePro(), pro_bar=_fake_pro_bar)
    adapter.sync_daily_quotes("600001", start=date(2010, 1, 1), end=date(2010, 1, 31))
    out = db.get_daily_quotes("600001", date(2010, 1, 1), date(2010, 1, 31))
    row = out[out["date"] == date(2010, 1, 19)].iloc[0]
    assert row["volume"] == 1200.0 * 100        # 手 → 股
    assert row["amount"] == 3_600_000.0 * 1000   # 千元 → 元
    assert db.get_quote_source("600001") == "tushare"


def test_sync_trade_calendar(db):
    adapter = TushareAdapter(db, pro=FakePro())
    n = adapter.sync_trade_calendar()
    assert n == 2
    assert db.is_trade_date(date(2024, 1, 2)) is True


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


def test_adj_factor_from_raw_and_hfq(db):
    """RAW close is stored; adj_factor = hfq_close / raw_close, from a second
    adj='hfq' pull aligned by trade_date."""
    db.upsert_stock("600001", "x", "SH", date(1998, 1, 22), "")

    def bar(ts_code, asset, adj, start_date, end_date):
        # raw vs hfq diverge — hfq is anchored higher (post-split inflation).
        c = ([6.2, 6.0] if adj == "hfq" else [3.1, 3.0])
        return pd.DataFrame([
            {"ts_code": ts_code, "trade_date": "20100120", "open": c[0],
             "high": c[0] + 0.1, "low": c[0] - 0.1, "close": c[0],
             "vol": 1000.0, "amount": 3.1e6},
            {"ts_code": ts_code, "trade_date": "20100119", "open": c[1],
             "high": c[1] + 0.1, "low": c[1] - 0.1, "close": c[1],
             "vol": 1200.0, "amount": 3.6e6},
        ])

    adapter = TushareAdapter(db, pro=FakePro(), pro_bar=bar)
    adapter.sync_daily_quotes("600001", start=date(2010, 1, 1),
                              end=date(2010, 1, 31))
    out = db.get_daily_quotes("600001", date(2010, 1, 1), date(2010, 1, 31))
    by_date = {str(d): (c, f) for d, c, f in
               zip(out["date"], out["close"], out["adj_factor"])}
    assert by_date["2010-01-19"][0] == pytest.approx(3.0)           # RAW stored
    assert by_date["2010-01-19"][1] == pytest.approx(6.0 / 3.0)     # hfq/raw
    assert by_date["2010-01-20"][1] == pytest.approx(6.2 / 3.1)


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
