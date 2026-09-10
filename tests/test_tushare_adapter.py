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

    def __init__(self):
        self.dividend_calls: list[dict] = []

    def stock_basic(self, list_status, fields):
        if list_status == "L":
            return pd.DataFrame([
                {"ts_code": "000001.SZ", "name": "平安银行", "industry": "银行",
                 "list_date": "19910403", "delist_date": None},
            ])
        if list_status == "D":
            return pd.DataFrame([   # delisted rows often have a blank industry
                {"ts_code": "600001.SH", "name": "邯郸钢铁", "industry": "",
                 "list_date": "19980122", "delist_date": "20100824"},
            ])
        return pd.DataFrame(
            columns=["ts_code", "name", "industry", "list_date", "delist_date"])

    def trade_cal(self, exchange, is_open):
        return pd.DataFrame([{"cal_date": "20240102"}, {"cal_date": "20240103"}])

    # `daily` (doc_id=27) serves BOTH the by-date sweep (trade_date=) and the
    # per-code range (ts_code=, start/end). It carries pre_close — the field we
    # reconstruct adj_factor from, so adj_factor/pro_bar are never called.
    def daily(self, ts_code=None, trade_date=None, start_date=None,
              end_date=None):
        if trade_date is not None:                      # whole-market one day
            return pd.DataFrame([
                {"ts_code": "000001.SZ", "trade_date": trade_date, "open": 10.0,
                 "high": 10.5, "low": 9.8, "close": 10.2, "pre_close": 10.2,
                 "vol": 1000.0, "amount": 1020.0},
                {"ts_code": "600001.SH", "trade_date": trade_date, "open": 3.0,
                 "high": 3.1, "low": 2.9, "close": 3.0, "pre_close": 3.0,
                 "vol": 500.0, "amount": 150.0},  # delisted-style code
            ])
        return pd.DataFrame([                           # per-code, newest-first
            {"ts_code": ts_code, "trade_date": "20100120", "open": 3.0,
             "high": 3.2, "low": 2.9, "close": 3.1, "pre_close": 3.1,
             "vol": 1000.0, "amount": 3_100_000.0},
            {"ts_code": ts_code, "trade_date": "20100119", "open": 3.1,
             "high": 3.3, "low": 3.0, "close": 3.0, "pre_close": 3.0,
             "vol": 1200.0, "amount": 3_600_000.0},
        ])

    def daily_basic(self, trade_date, fields):
        return pd.DataFrame([
            {"ts_code": "000001.SZ", "turnover_rate": 1.2},
            {"ts_code": "600001.SH", "turnover_rate": 0.8},
        ])

    # `dividend` (doc_id=103):按 ann_date 或 ex_date 拉**一天的全市场**。
    # 一行 = 一家公司的一次分红公告阶段,实施行带 ex_date + 税前 cash_div_tax。
    def dividend(self, ann_date=None, ex_date=None):
        self.dividend_calls.append({"ann_date": ann_date, "ex_date": ex_date})
        if (ann_date or ex_date) == "20240620":
            return pd.DataFrame([{
                "ts_code": "600519.SH", "end_date": "20231231",
                "ann_date": "20240521", "div_proc": "实施", "stk_div": 0.0,
                "stk_bo_rate": None, "stk_co_rate": None, "cash_div": 30.876,
                "cash_div_tax": 30.876, "record_date": "20240619",
                "ex_date": "20240620", "pay_date": "20240620",
                "div_listdate": None, "imp_ann_date": "20240614",
            }])
        return pd.DataFrame(columns=[
            "ts_code", "end_date", "ann_date", "div_proc", "stk_div",
            "stk_bo_rate", "stk_co_rate", "cash_div", "cash_div_tax",
            "record_date", "ex_date", "pay_date", "div_listdate",
            "imp_ann_date"])

    def index_weight(self, index_code, start_date, end_date):
        return pd.DataFrame([
            {"index_code": index_code, "con_code": "601919.SH",
             "trade_date": "20240628", "weight": 2.58},
            {"index_code": index_code, "con_code": "000937.SZ",
             "trade_date": "20240628", "weight": 1.84},
        ])

    def namechange(self, start_date, end_date):
        return pd.DataFrame([
            {"ts_code": "600001.SH", "name": "ST邯郸", "start_date": "20240501",
             "end_date": None, "ann_date": "20240428", "change_reason": "ST"},
            {"ts_code": "000002.SZ", "name": "撤销示例", "start_date": "20240701",
             "end_date": "20240801", "ann_date": "20240628",
             "change_reason": "撤销ST"},
        ])


def test_sync_by_date_whole_market(db):
    """One call set covers the whole market for a day: units normalized, factor
    reconstructed (first day → 1.0 anchor), turnover from daily_basic, delisted
    code lands too (P3). No adj_factor endpoint is touched."""
    adapter = TushareAdapter(db, pro=FakePro())
    n = adapter.sync_daily_quotes_by_date(date(2024, 1, 2))
    assert n == 2
    out = db.get_daily_quotes("000001", date(2024, 1, 1), date(2024, 1, 3))
    row = out.iloc[0]
    assert row["close"] == 10.2
    assert row["volume"] == 1000.0 * 100        # 手 → 股
    assert row["amount"] == 1020.0 * 1000        # 千元 → 元
    assert row["adj_factor"] == 1.0              # first bar anchors at 1.0
    assert row["turnover"] == 1.2                # from daily_basic
    assert db.get_quote_source("000001") == "tushare"
    # the delisted-style 600001 also landed
    assert len(db.get_daily_quotes("600001", date(2024, 1, 1), date(2024, 1, 3))) == 1


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
    adapter = TushareAdapter(db, pro=FakePro())
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
    assert listed.industry == "银行"   # stock_basic industry is carried through
    assert delisted is not None and delisted.delist_date == date(2010, 8, 24)
    assert delisted.exchange == "SH"


def test_sync_daily_quotes_lands_with_zero_turnover(db):
    db.upsert_stock("600001", "邯郸钢铁", "SH", date(1998, 1, 22), "",
                    delist_date=date(2010, 8, 24))
    adapter = TushareAdapter(db, pro=FakePro())
    saved = adapter.sync_daily_quotes("600001", start=date(2010, 1, 1),
                                      end=date(2010, 1, 31))
    assert saved == 2
    out = db.get_daily_quotes("600001", date(2010, 1, 1), date(2010, 1, 31))
    assert len(out) == 2
    assert (out["close"] > 0).all()
    assert (out["turnover"] == 0).all()  # the per-code path has no daily_basic


def test_sync_dividends_by_ann_date_lands_ex_date(db):
    """按公告日批量拉全市场分红:ex_date 一并落库(入池按 ex_date,公告日是
    PIT 可见性键),ts_code 映射回 6 位 code,来源可追溯。"""
    adapter = TushareAdapter(db, pro=FakePro())
    n = adapter.sync_dividends_by_date(date(2024, 6, 20))
    assert n == 1
    df = db.get_dividend_events(date(2024, 6, 1), date(2024, 6, 30))
    assert list(df["code"]) == ["600519"]
    assert df["ex_date"].iloc[0] == "2024-06-20"
    assert df["cash_div_tax"].iloc[0] == pytest.approx(30.876)


def test_dividend_events_dedupe_same_payout(db):
    """同一笔分红在 tushare 里有多行(预案/股东大会/实施、ann_date 不同),
    按 (code, end_date, ex_date) 去重 —— 否则分红再投会重复计数。"""
    rows = [
        {"code": "600519", "ann_date": "2024-04-17", "end_date": "2023-12-31",
         "div_proc": "预案", "cash_div_tax": 30.876},
        {"code": "600519", "ann_date": "2024-05-21", "end_date": "2023-12-31",
         "div_proc": "实施", "cash_div_tax": 30.876, "ex_date": "2024-06-20"},
        {"code": "600519", "ann_date": "2024-06-14", "end_date": "2023-12-31",
         "div_proc": "实施", "cash_div_tax": 30.876, "ex_date": "2024-06-20"},
    ]
    db.save_dividends(pd.DataFrame(rows))
    df = db.get_dividend_events(date(2024, 1, 1), date(2024, 12, 31))
    assert len(df) == 1
    assert df["ann_date"].iloc[0] == "2024-05-21"  # 最早的实施公告


def test_sync_dividends_range_skips_weekends(db):
    """ex_date 扫描跳过周末(除息必为交易日),按 calls_per_min 限速但不真等。"""
    pro = FakePro()
    adapter = TushareAdapter(db, pro=pro)
    adapter.sync_dividends(date(2024, 6, 20), date(2024, 6, 23),
                           by="ex_date", calls_per_min=0)
    assert [c["ex_date"] for c in pro.dividend_calls] == ["20240620", "20240621"]
    adapter.sync_dividends(date(2024, 6, 19), date(2024, 6, 19),
                           by="ann_date", calls_per_min=0)
    assert pro.dividend_calls[-1] == {"ann_date": "20240619", "ex_date": None}


def test_sync_index_weights_and_name_history(db):
    """指数成分快照(PIT)与曾用名史(判 ST 用)各按年分片落库。"""
    adapter = TushareAdapter(db, pro=FakePro())
    n = adapter.sync_index_weights("000922.CSI", date(2024, 1, 1),
                                   date(2024, 12, 31))
    assert n == 2
    members = db.get_index_members("000922.CSI", date(2024, 6, 30))
    assert members == {"601919": 2.58, "000937": 1.84}
    # 快照发布前(5 月)查不到 6 月末的成分 —— PIT,不引入未来成分
    assert db.get_index_members("000922.CSI", date(2024, 5, 31)) == {}

    n = adapter.sync_name_history(date(2024, 1, 1), date(2024, 12, 31))
    assert n == 2
    names = db.get_names_asof(date(2024, 6, 1))
    assert names == {"600001": "ST邯郸"}      # 000002 的改名 7-01 才生效
    assert db.get_names_asof(date(2024, 7, 15))["000002"] == "撤销示例"
    assert "000002" not in db.get_names_asof(date(2024, 8, 2))  # 改名再次失效


def test_reconstruct_adj_factor_from_preclose():
    """f[t] = f[t-1]·close[t-1]/pre_close[t]; raw·f is a continuous hfq series —
    NO adj_factor endpoint needed (that's the 1/min-limited one)."""
    from quanti.data.tushare_adapter import reconstruct_adj_factor
    # day1 anchor; day2 ex-div (pre_close 9 < prev close 10); day3 normal.
    f = reconstruct_adj_factor([10.0, 9.5, 9.7], [10.0, 9.0, 9.5])
    assert f[0] == pytest.approx(1.0)
    assert f[1] == pytest.approx(10.0 / 9.0)         # steps up on the ex-div day
    assert f[2] == pytest.approx(10.0 / 9.0)         # constant on a normal day
    # The reconstructed hfq return equals tushare's own close/pre_close.
    hfq = [r * fi for r, fi in zip([10.0, 9.5, 9.7], f)]
    assert hfq[1] / hfq[0] == pytest.approx(9.5 / 9.0)


def test_reconstruct_adj_factor_seed_splices_incrementally():
    """An appended batch seeded by the stored bar joins WITHOUT a jump."""
    from quanti.data.tushare_adapter import reconstruct_adj_factor
    # Normal seam: pre_close == stored close → factor unchanged.
    (f,) = reconstruct_adj_factor([9.7], [9.5], seed_close=9.5, seed_factor=10 / 9)
    assert f == pytest.approx(10 / 9)
    # Ex-div on the seam: factor steps from the stored value.
    (f2,) = reconstruct_adj_factor([9.2], [9.0], seed_close=9.5, seed_factor=10 / 9)
    assert f2 == pytest.approx((10 / 9) * (9.5 / 9.0))


def test_by_date_reconstructs_factor_across_dividend(db):
    """By-date sweep: carried seed_state steps the factor on the ex-div day with
    only `daily` calls — proves the adj_factor endpoint is unnecessary."""
    class DivPro(FakePro):
        _bars = {"20240102": (10.0, 10.0), "20240103": (9.5, 9.0)}  # close, pre

        def daily(self, ts_code=None, trade_date=None, **kw):
            close, pre = self._bars[trade_date]
            return pd.DataFrame([{
                "ts_code": "000001.SZ", "trade_date": trade_date, "open": close,
                "high": close, "low": close, "close": close, "pre_close": pre,
                "vol": 100.0, "amount": 1000.0}])

        def daily_basic(self, trade_date, fields):
            return pd.DataFrame(columns=["ts_code", "turnover_rate"])

    adapter = TushareAdapter(db, pro=DivPro())
    seed: dict = {}
    adapter.sync_daily_quotes_by_date(date(2024, 1, 2), seed_state=seed)
    adapter.sync_daily_quotes_by_date(date(2024, 1, 3), seed_state=seed)
    out = db.get_daily_quotes("000001", date(2024, 1, 1), date(2024, 1, 4))
    by_date = {str(d): f for d, f in zip(out["date"], out["adj_factor"])}
    assert by_date["2024-01-02"] == pytest.approx(1.0)
    assert by_date["2024-01-03"] == pytest.approx(10.0 / 9.0)


def test_methods_raise_clearly_without_token(db, monkeypatch):
    # No pro injected, no TUSHARE_TOKEN → clear error, no token leak.
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    import quanti.data.tushare_adapter as mod
    monkeypatch.setattr(mod, "ts", None)  # simulate tushare not installed
    adapter = TushareAdapter(db)
    with pytest.raises(RuntimeError):
        adapter.sync_stock_list()


def test_retry_patient_waits_out_per_minute_limit(monkeypatch):
    """Patient mode sleeps ~one minute on a per-minute rate limit, then retries
    (so a 1/min endpoint like stock_basic eventually succeeds)."""
    import quanti.data.tushare_adapter as ta
    slept = []
    monkeypatch.setattr(ta.time, "sleep", lambda s: slept.append(s))
    n = {"i": 0}

    def flaky(**kw):
        n["i"] += 1
        if n["i"] == 1:
            raise Exception("抱歉，您访问接口(stock_basic)频率超限(1次/分钟)")
        return "ok"

    assert ta.TushareAdapter._retry(flaky, _patient=True) == "ok"
    assert ta.RATE_LIMIT_WAIT in slept            # waited the per-minute window


def test_retry_nonpatient_uses_short_backoff(monkeypatch):
    """Non-patient (API) fails fast: short backoff only, never the 60s wait."""
    import quanti.data.tushare_adapter as ta
    slept = []
    monkeypatch.setattr(ta.time, "sleep", lambda s: slept.append(s))

    def always(**kw):
        raise Exception("抱歉，频率超限(1次/分钟)")

    with pytest.raises(Exception, match="频率超限"):
        ta.TushareAdapter._retry(always)          # non-patient
    assert slept and all(s < ta.RATE_LIMIT_WAIT for s in slept)


def test_sync_stock_list_patient_survives_rate_limit(db, monkeypatch):
    """With patient=True, a stock_basic that's rate-limited on the first hit of
    D/P still completes (the retry waits out the window — sleep mocked here)."""
    import quanti.data.tushare_adapter as ta
    monkeypatch.setattr(ta.time, "sleep", lambda s: None)

    class RLPro(FakePro):
        def __init__(self):
            self.hits = {}

        def stock_basic(self, list_status, fields):
            self.hits[list_status] = self.hits.get(list_status, 0) + 1
            if self.hits[list_status] == 1 and list_status in ("D", "P"):
                raise Exception("抱歉，您访问接口(stock_basic)频率超限(1次/分钟)")
            return FakePro.stock_basic(self, list_status, fields)

    n = TushareAdapter(db, pro=RLPro()).sync_stock_list(patient=True)
    assert n == 2                                  # L(000001) + D(600001)


def test_per_code_with_basic_fills_turnover_and_daily_basic(db):
    """sync_daily_quotes(with_basic=True) pulls per-code daily_basic → fills the
    quotes' turnover and saves valuation rows (granularity parity with by-date)."""
    class BasicPro(FakePro):
        def daily_basic(self, ts_code=None, trade_date=None, start_date=None,
                        end_date=None, fields=None):
            return pd.DataFrame([
                {"ts_code": ts_code, "trade_date": "20100119", "turnover_rate": 1.5,
                 "pe": 12.0, "pe_ttm": 11.0, "pb": 1.2, "ps": 2.0, "ps_ttm": 1.9,
                 "dv_ratio": 3.0, "total_mv": 1e6, "circ_mv": 9e5},
                {"ts_code": ts_code, "trade_date": "20100120", "turnover_rate": 0.8,
                 "pe": 13.0, "pe_ttm": 12.0, "pb": 1.3, "ps": 2.1, "ps_ttm": 2.0,
                 "dv_ratio": 3.1, "total_mv": 1.1e6, "circ_mv": 9.5e5},
            ])

    db.upsert_stock("600001", "x", "SH", date(1998, 1, 22), "")
    adapter = TushareAdapter(db, pro=BasicPro())
    adapter.sync_daily_quotes("600001", start=date(2010, 1, 1),
                              end=date(2010, 1, 31), with_basic=True)
    out = db.get_daily_quotes("600001", date(2010, 1, 1), date(2010, 1, 31))
    assert out[out["date"] == date(2010, 1, 19)].iloc[0]["turnover"] == 1.5
    basic = db.get_daily_basic("600001", date(2010, 1, 1), date(2010, 1, 31))
    assert len(basic) == 2 and set(basic["pe"]) == {12.0, 13.0}


def test_sync_financials_by_period_vip(db):
    """Whole-market financials for one period via fina_indicator_vip: maps real
    ann_date/end_date, splits ts_code → code, lands in `financials` (PIT-keyed).
    A row missing ann_date is dropped (ann_date is the PIT key)."""
    class FinPro(FakePro):
        def fina_indicator_vip(self, period=None, fields=None):
            return pd.DataFrame([
                {"ts_code": "000001.SZ", "ann_date": "20240428",
                 "end_date": "20240331", "roe": 3.1, "netprofit_yoy": 12.0,
                 "or_yoy": 5.0},
                {"ts_code": "600519.SH", "ann_date": "20240427",
                 "end_date": "20240331", "roe": 30.0, "netprofit_yoy": 8.0,
                 "or_yoy": 18.0},
                {"ts_code": "600001.SH", "ann_date": None,  # no ann_date → dropped
                 "end_date": "20240331", "roe": 1.0, "netprofit_yoy": 0.0,
                 "or_yoy": 0.0},
            ])

    adapter = TushareAdapter(db, pro=FinPro())
    n = adapter.sync_financials_by_period(date(2024, 3, 31))
    assert n == 2                                   # the no-ann_date row dropped
    # PIT: visible only on/after the real ann_date (2024-04-28 for 000001).
    assert db.get_financials_asof("000001", date(2024, 4, 27)).empty
    after = db.get_financials_asof("000001", date(2024, 4, 28))
    assert len(after) == 1 and after.iloc[0]["roe"] == 3.1


def test_financials_by_period_keeps_latest_restatement(db):
    """Restated reports return multiple rows per (ts_code, end_date) with
    update_flag (1=corrected, 0=original). We must persist the CORRECTED value
    regardless of the order tushare returns them in (stale-last here)."""
    class RestatePro(FakePro):
        def fina_indicator_vip(self, period=None, fields=None):
            return pd.DataFrame([
                # corrected first, stale (original) LAST — order must not matter
                {"ts_code": "000001.SZ", "ann_date": "20240501",
                 "end_date": "20240331", "roe": 9.9, "netprofit_yoy": 20.0,
                 "or_yoy": 7.0, "update_flag": "1"},
                {"ts_code": "000001.SZ", "ann_date": "20240428",
                 "end_date": "20240331", "roe": 3.1, "netprofit_yoy": 12.0,
                 "or_yoy": 5.0, "update_flag": "0"},
            ])

    adapter = TushareAdapter(db, pro=RestatePro())
    n = adapter.sync_financials_by_period(date(2024, 3, 31))
    assert n == 1                                    # one period, deduped
    rows = db.get_financials_asof("000001", date(2024, 5, 2))
    assert len(rows) == 1 and rows.iloc[0]["roe"] == 9.9   # corrected, not 3.1
