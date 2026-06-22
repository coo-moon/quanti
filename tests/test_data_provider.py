"""Tests for data provider interface and AkShare adapter."""

from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest

from quanti.data.provider import DataProvider
from quanti.data.akshare_adapter import AkShareAdapter
from quanti.data.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    database.initialize()
    yield database
    database.close()


class TestDataProvider:
    def test_get_daily_bars(self, db):
        """DataProvider reads from database."""
        df = pd.DataFrame(
            {
                "code": ["000001", "000001"],
                "date": [date(2024, 1, 2), date(2024, 1, 3)],
                "open": [10.0, 10.2],
                "high": [10.5, 10.6],
                "low": [9.8, 10.0],
                "close": [10.2, 10.5],
                "volume": [1_000_000, 1_200_000],
                "amount": [10_200_000, 12_600_000],
                "turnover": [1.5, 1.8],
            }
        )
        db.save_daily_quotes(df)
        provider = DataProvider(db)
        bars = provider.get_daily_bars("000001", date(2024, 1, 1), date(2024, 1, 5))
        assert len(bars) == 2
        assert bars[0].close == 10.2

    def test_get_daily_df(self, db):
        """DataProvider returns DataFrame."""
        df = pd.DataFrame(
            {
                "code": ["000001"],
                "date": [date(2024, 1, 2)],
                "open": [10.0],
                "high": [10.5],
                "low": [9.8],
                "close": [10.2],
                "volume": [1_000_000],
                "amount": [10_200_000],
                "turnover": [1.5],
            }
        )
        db.save_daily_quotes(df)
        provider = DataProvider(db)
        result = provider.get_daily_df("000001", date(2024, 1, 1), date(2024, 1, 5))
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 1


class TestPriceAdjustment:
    """Qlib-style raw + adj_factor: daily_quotes stores RAW prices; the provider
    back-adjusts (hfq) on read. adjusted = raw × factor, volume = raw / factor."""

    def _save(self, db, closes, factors, opens=None, vols=None):
        n = len(closes)
        opens = opens or closes
        vols = vols or [1_000_000.0] * n
        dates = [date(2024, 1, 2 + i) for i in range(n)]
        db.save_daily_quotes(pd.DataFrame({
            "code": ["000001"] * n, "date": dates,
            "open": opens, "high": [c + 0.1 for c in closes],
            "low": [c - 0.1 for c in closes], "close": closes,
            "volume": vols, "amount": [c * v for c, v in zip(closes, vols)],
            "turnover": [1.0] * n, "adj_factor": factors,
        }))
        return dates

    def test_none_returns_raw(self, db):
        self._save(db, [10.0, 11.0], [1.10, 1.10])
        p = DataProvider(db)
        bars = p.get_daily_bars("000001", date(2024, 1, 1), date(2024, 1, 9),
                                adjust="none")
        assert [round(b.close, 4) for b in bars] == [10.0, 11.0]
        assert [b.volume for b in bars] == [1_000_000.0, 1_000_000.0]

    def test_hfq_applies_factor(self, db):
        self._save(db, [10.0, 20.0], [1.10, 1.05], vols=[1_000_000.0, 2_000_000.0])
        p = DataProvider(db)
        bars = p.get_daily_bars("000001", date(2024, 1, 1), date(2024, 1, 9))  # default hfq
        assert round(bars[0].close, 4) == round(10.0 * 1.10, 4)
        assert round(bars[1].close, 4) == round(20.0 * 1.05, 4)
        # volume / factor — keeps price×volume == amount (the invariant anchor).
        assert round(bars[0].volume, 2) == round(1_000_000.0 / 1.10, 2)
        # amount is unchanged by adjustment (raw 元).
        raw = DataProvider(db).get_daily_bars(
            "000001", date(2024, 1, 1), date(2024, 1, 9), adjust="none")
        assert [b.amount for b in bars] == [b.amount for b in raw]

    def test_hfq_is_default(self, db):
        self._save(db, [10.0], [1.25])
        p = DataProvider(db)
        default = p.get_daily_bars("000001", date(2024, 1, 1), date(2024, 1, 9))
        hfq = p.get_daily_bars("000001", date(2024, 1, 1), date(2024, 1, 9),
                               adjust="hfq")
        assert default[0].close == hfq[0].close == pytest.approx(12.5)

    def test_price_times_volume_invariant(self, db):
        # adjusted close × adjusted volume == raw close × raw volume (== amount).
        self._save(db, [10.0, 12.0], [1.30, 1.15], vols=[3e6, 4e6])
        p = DataProvider(db)
        raw = p.get_daily_bars("000001", date(2024, 1, 1), date(2024, 1, 9), adjust="none")
        adj = p.get_daily_bars("000001", date(2024, 1, 1), date(2024, 1, 9), adjust="hfq")
        for r, a in zip(raw, adj):
            assert a.close * a.volume == pytest.approx(r.close * r.volume)

    def test_ex_dividend_continuity(self, db):
        # RAW close gaps down on the ex-div day (10.0 → 9.0) — a fake -10% that
        # is not a real loss. The factor makes the hfq series continuous.
        # factor chosen so hfq is flat at 11.0 across the boundary.
        self._save(db, [10.0, 9.0, 9.0], [1.10, 1.2222, 1.2222])
        p = DataProvider(db)
        raw = [b.close for b in p.get_daily_bars(
            "000001", date(2024, 1, 1), date(2024, 1, 9), adjust="none")]
        hfq = [b.close for b in p.get_daily_bars(
            "000001", date(2024, 1, 1), date(2024, 1, 9))]
        # raw shows the artificial -10% gap…
        assert raw[1] / raw[0] - 1 == pytest.approx(-0.10)
        # …hfq is continuous across the ex-div boundary (≈ no day-over-day jump).
        assert abs(hfq[1] / hfq[0] - 1) < 0.01

    def test_factor_window_independent(self, db):
        # The anchored factor for a date must not change when later bars arrive
        # (the A2 fix). Save 2 bars, read; append a 3rd, re-read — earlier
        # adjusted prices are identical.
        self._save(db, [10.0, 11.0], [1.10, 1.05])
        p = DataProvider(db)
        before = [b.close for b in p.get_daily_bars(
            "000001", date(2024, 1, 1), date(2024, 1, 9))]
        # Append a later bar (new dividend → its own factor); old rows untouched.
        db.save_daily_quotes(pd.DataFrame({
            "code": ["000001"], "date": [date(2024, 1, 4)],
            "open": [12.0], "high": [12.1], "low": [11.9], "close": [12.0],
            "volume": [1e6], "amount": [12e6], "turnover": [1.0],
            "adj_factor": [1.21]}))
        after = [b.close for b in p.get_daily_bars(
            "000001", date(2024, 1, 1), date(2024, 1, 9))]
        assert after[:2] == before  # earlier adjusted prices unchanged

    def test_backward_compat_no_factor_column(self, db):
        # A DataFrame without adj_factor → stored factor defaults to 1.0 →
        # adjusted == raw (existing callers/data unaffected).
        db.save_daily_quotes(pd.DataFrame({
            "code": ["000001"], "date": [date(2024, 1, 2)],
            "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2],
            "volume": [1e6], "amount": [10.2e6], "turnover": [1.5]}))
        p = DataProvider(db)
        bars = p.get_daily_bars("000001", date(2024, 1, 1), date(2024, 1, 9))
        assert bars[0].close == 10.2  # hfq == raw when factor defaulted to 1.0


class TestAkShareAdapter:
    @patch("quanti.data.akshare_adapter.ak")
    def test_fetch_stock_list(self, mock_ak, db):
        mock_ak.stock_info_a_code_name.return_value = pd.DataFrame(
            {"code": ["000001", "600519"], "name": ["平安银行", "贵州茅台"]}
        )
        mock_ak.stock_individual_info_em.side_effect = [
            pd.DataFrame(
                {"item": ["上市时间", "行业"], "value": ["19910403", "银行"]}
            ),
            pd.DataFrame(
                {"item": ["上市时间", "行业"], "value": ["20010827", "白酒"]}
            ),
        ]
        adapter = AkShareAdapter(db)
        adapter.sync_stock_list()
        stocks = db.list_stocks()
        assert len(stocks) == 2

    @patch("quanti.data.akshare_adapter.ak")
    def test_adj_factor_from_raw_and_hfq(self, mock_ak, db):
        """East Money: store RAW OHLCV + adj_factor = hfq_close/raw_close, from
        two stock_zh_a_hist pulls (adjust='' and adjust='hfq')."""
        def hist(symbol, period, start_date, end_date, adjust):
            # raw drops 10→9 (ex-div-style); hfq is flat 11 (continuous).
            c = ([11.0, 11.0] if adjust == "hfq" else [10.0, 9.0])
            return pd.DataFrame({
                "日期": ["2024-01-02", "2024-01-03"],
                "开盘": c, "最高": [x + 0.1 for x in c],
                "最低": [x - 0.1 for x in c], "收盘": c,
                "成交量": [1e4, 1e4], "成交额": [1e7, 1e7], "换手率": [1.0, 1.0]})
        mock_ak.stock_zh_a_hist.side_effect = hist
        mock_ak.stock_zh_a_daily.side_effect = RuntimeError("sina down")  # force EM
        db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3))
        AkShareAdapter(db).sync_daily_quotes(
            "000001", start=date(2024, 1, 1), end=date(2024, 1, 5),
            repair_gaps=False)
        res = db.get_daily_quotes("000001", date(2024, 1, 1), date(2024, 1, 9))
        by_date = {str(d): (c, f) for d, c, f in
                   zip(res["date"], res["close"], res["adj_factor"])}
        assert by_date["2024-01-02"][0] == 10.0                     # RAW stored
        assert by_date["2024-01-02"][1] == pytest.approx(11.0 / 10.0)
        assert by_date["2024-01-03"][1] == pytest.approx(11.0 / 9.0)

    @patch("quanti.data.akshare_adapter.ak")
    def test_fetch_daily_quotes(self, mock_ak, db):
        mock_ak.stock_zh_a_hist.return_value = pd.DataFrame(
            {
                "日期": ["2024-01-02"],
                "开盘": [10.0],
                "最高": [10.5],
                "最低": [9.8],
                "收盘": [10.2],
                "成交量": [10000],
                "成交额": [10200000],
                "换手率": [1.5],
            }
        )
        db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3))
        adapter = AkShareAdapter(db)
        count = adapter.sync_daily_quotes("000001")
        assert count == 1
