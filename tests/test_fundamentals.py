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


def test_akshare_financials_by_period_is_pit(db, monkeypatch):
    """Free akshare 业绩报表 → financials with net_profit/revenue absolutes, keyed
    by the STATUTORY disclosure deadline (Q1 → 04-30) so it's point-in-time —
    akshare's own 最新公告日期 (a last-updated stamp) is deliberately ignored."""
    import quanti.data.akshare_adapter as aks
    from quanti.data.akshare_adapter import AkShareAdapter

    def fake_yjbb(**kw):
        return pd.DataFrame([
            {"股票代码": "000001", "净资产收益率": 15.0,
             "净利润-净利润": 1.2e9, "营业总收入-营业总收入": 5.0e9,
             "净利润-同比增长": 20.0, "营业总收入-同比增长": 8.0,
             # unreliable: a 2025 last-updated stamp — must NOT be used as ann.
             "最新公告日期": date(2025, 4, 30)},
            {"股票代码": "", "净资产收益率": 5.0, "净利润-净利润": 1.0e8,
             "营业总收入-营业总收入": 2.0e8, "净利润-同比增长": float("nan"),
             "营业总收入-同比增长": 1.0, "最新公告日期": None},  # no code → skipped
        ])

    monkeypatch.setattr(aks.ak, "stock_yjbb_em", fake_yjbb)
    n = AkShareAdapter(db).sync_financials_by_period(date(2024, 3, 31))
    assert n == 1                                        # blank-code row dropped
    # Q1 2024 → statutory deadline 2024-04-30 (NOT akshare's 2025 stamp).
    asof = db.get_financials_asof("000001", date(2024, 5, 1))
    row = asof.iloc[0]
    assert list(asof["ann_date"]) == [date(2024, 4, 30)]
    assert row["roe"] == 15.0 and row["netprofit_yoy"] == 20.0
    assert row["revenue_yoy"] == 8.0
    assert row["net_profit"] == 1.2e9 and row["revenue"] == 5.0e9
    # Before the 04-30 deadline → not visible (point-in-time).
    assert db.get_financials_asof("000001", date(2024, 4, 15)).empty


def test_statutory_ann_date_deadlines():
    from quanti.data.akshare_adapter import AkShareAdapter as A
    assert A._statutory_ann_date(date(2024, 3, 31)) == date(2024, 4, 30)
    assert A._statutory_ann_date(date(2024, 6, 30)) == date(2024, 8, 31)
    assert A._statutory_ann_date(date(2024, 9, 30)) == date(2024, 10, 31)
    assert A._statutory_ann_date(date(2024, 12, 31)) == date(2025, 4, 30)


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


def test_factor_ic_scores_fundamental_factor_only_when_merged(db):
    """A fundamental factor (pe) gets a REAL IC only when fundamentals are merged
    into the eval frame — proving factor mining now leverages daily_basic/
    financials instead of silently dropping value/quality candidates."""
    from quanti.factors.evaluation import factor_ic
    dates = list(pd.bdate_range("2024-01-01", periods=60).date)
    codes = [f"00000{i}" for i in range(1, 7)]            # 6 codes ≥ min_names
    for i, code in enumerate(codes):
        closes = [10.0 + i + 0.1 * t for t in range(len(dates))]  # varies by code
        db.save_daily_quotes(pd.DataFrame({
            "code": code, "date": dates, "open": closes, "high": closes,
            "low": closes, "close": closes, "volume": 1e6, "amount": 1e7,
            "turnover": 1.0}))
        db.save_daily_basic(pd.DataFrame(
            [{"code": code, "date": d, "pe": 10.0 + i} for d in dates]))  # distinct pe
    prov = DataProvider(db)
    start, end = dates[20], dates[40]
    ic_with = factor_ic(parse_expr("pe"), prov, codes, start, end,
                        fwd_days=5, with_fundamentals=True)
    ic_without = factor_ic(parse_expr("pe"), prov, codes, start, end,
                          fwd_days=5, with_fundamentals=False)
    assert not pd.isna(ic_with)        # merged → pe is a real column → scorable
    assert pd.isna(ic_without)         # not merged → pe all-NaN → unscorable
