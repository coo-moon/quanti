"""Tests for XtdataAdapter against the in-process mock bridge gateway.

Confirms xtdata-sourced bars land through the same db.save_daily_quotes exit,
so the daily_quotes table is identical to the AkShare path.
"""

from __future__ import annotations

from datetime import date

import pytest

from bridge.qmt_bridge import QmtGateway, route
from quanti.data.database import Database
from quanti.data.xtdata_adapter import XtdataAdapter


class InProcBridge:
    def __init__(self) -> None:
        self.gw = QmtGateway()

    def get(self, path: str, params: dict | None = None) -> dict:
        return route(self.gw, "GET", path, params or {}, None)[1]

    def post(self, path: str, json: dict | None = None) -> dict:
        return route(self.gw, "POST", path, json or {}, json)[1]


class RecordingBridge(InProcBridge):
    """Captures /data/kline query params so tests can assert the requested
    date window (e.g. that incremental sync resumes from the latest bar)."""

    def __init__(self) -> None:
        super().__init__()
        self.kline_params: list[dict] = []

    def get(self, path: str, params: dict | None = None) -> dict:
        if path == "/data/kline":
            self.kline_params.append(dict(params or {}))
        return super().get(path, params)


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    yield d
    d.close()


def test_sync_stock_list(db):
    adapter = XtdataAdapter(db, client=InProcBridge())
    n = adapter.sync_stock_list()
    assert n >= 3
    s = db.get_stock("000001")
    assert s is not None and s.name


def test_sync_daily_quotes_lands_in_daily_quotes(db):
    db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
    adapter = XtdataAdapter(db, client=InProcBridge())
    saved = adapter.sync_daily_quotes("000001", start=date(2024, 1, 1),
                                      end=date(2024, 2, 1))
    assert saved > 0
    df = db.get_daily_quotes("000001", date(2024, 1, 1), date(2024, 2, 1))
    assert not df.empty
    assert (df["close"] > 0).all()
    assert (df["high"] >= df["low"]).all()


def test_sync_daily_quotes_resumes_from_latest(db):
    db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
    bridge = RecordingBridge()
    adapter = XtdataAdapter(db, client=bridge)
    adapter.sync_daily_quotes("000001", start=date(2024, 1, 1),
                              end=date(2024, 1, 31))
    latest = db.get_latest_quote_date("000001")
    assert latest is not None

    # Second call with start=None must resume from the latest stored bar —
    # not silently refetch all history from the 2020 default.
    adapter.sync_daily_quotes("000001", end=date(2024, 2, 15))
    assert bridge.kline_params[-1]["start"] == latest.strftime("%Y%m%d")
    assert bridge.kline_params[-1]["start"] != "20200101"
