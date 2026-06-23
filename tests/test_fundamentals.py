"""Tests for fundamentals storage + point-in-time merge + DSL exposure (P4)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.factors.cross_sectional import _merge_fundamentals
from quanti.factors.library import as_factor_fn
from quanti.factors.parser import FactorParseError, parse_expr


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    yield d
    d.close()


def test_daily_basic_roundtrip(db):
    db.save_daily_basic(pd.DataFrame([
        {"code": "000001", "date": date(2024, 1, 2), "pe": 12.0, "pb": 1.5,
         "total_mv": 1e9, "dv_ratio": 2.0, "turnover_rate": 1.1},
    ]))
    out = db.get_daily_basic("000001", date(2024, 1, 1), date(2024, 1, 3))
    assert out.iloc[0]["pe"] == 12.0 and out.iloc[0]["pb"] == 1.5
    assert db.has_fundamentals() is True


def test_financials_asof_excludes_future_announcements(db):
    db.save_financials(pd.DataFrame([
        {"code": "000001", "end_date": "20231231", "ann_date": "20240415",
         "report_type": "", "roe": 15.0, "netprofit_yoy": 20.0, "revenue_yoy": 8.0},
        {"code": "000001", "end_date": "20240331", "ann_date": "20240428",
         "report_type": "", "roe": 4.0, "netprofit_yoy": 5.0, "revenue_yoy": 3.0},
    ]))
    # As of 2024-04-20: only the first report (ann 04-15) is visible.
    seen = db.get_financials_asof("000001", date(2024, 4, 20))
    assert list(seen["ann_date"]) == [date(2024, 4, 15)]
    assert seen.iloc[0]["roe"] == 15.0


def test_merge_fundamentals_is_point_in_time(db):
    """roe is NaN before its ann_date and equals the report value on/after —
    no look-ahead. pe (daily_basic) is available same-day."""
    dates = [date(2024, 4, x) for x in (12, 15, 16, 17)]
    db.save_daily_basic(pd.DataFrame([
        {"code": "000001", "date": d, "pe": 10.0 + i}
        for i, d in enumerate(dates)]))
    db.save_financials(pd.DataFrame([
        {"code": "000001", "end_date": "20231231", "ann_date": "20240415",
         "report_type": "", "roe": 15.0, "netprofit_yoy": 20.0, "revenue_yoy": 8.0},
    ]))
    bars = pd.DataFrame({"code": "000001", "date": dates,
                         "close": [10, 11, 12, 13]})
    merged = _merge_fundamentals(bars, DataProvider(db), "000001",
                                 dates[0], dates[-1])
    by_date = merged.set_index("date")
    # pe present every day (same-day PIT).
    assert by_date.loc[date(2024, 4, 12), "pe"] == 10.0
    # roe announced 04-15 → NaN on 04-12, value on/after 04-15.
    assert pd.isna(by_date.loc[date(2024, 4, 12), "roe"])
    assert by_date.loc[date(2024, 4, 15), "roe"] == 15.0
    assert by_date.loc[date(2024, 4, 17), "roe"] == 15.0
    # A factor referencing roe reads the latest-known value at the as-of bar.
    assert as_factor_fn(parse_expr("roe"))(merged) == 15.0


def test_parser_whitelists_fundamentals_and_rejects_unknown():
    parse_expr("pe / Mean(pe, 20)")          # valuation factor parses
    parse_expr("-roe")                        # quality factor parses
    with pytest.raises(FactorParseError):
        parse_expr("market_cap")              # not whitelisted


def test_dsl_whitelists_stay_in_sync():
    """The LLM vocabulary (factor_miner) must match the parser whitelist, or
    mined factors fail to parse."""
    from quanti.factors.expr import FUNDAMENTAL_FIELDS
    from quanti.factors.parser import _FIELDS
    for f in FUNDAMENTAL_FIELDS:
        assert f in _FIELDS                   # parser accepts every advertised field
    from quanti.agent.factor_miner import _SYSTEM
    for f in FUNDAMENTAL_FIELDS:
        assert f in _SYSTEM                    # LLM is told about each one
