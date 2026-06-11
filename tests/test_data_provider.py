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
