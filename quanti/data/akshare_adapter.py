"""AkShare data source adapter."""

from __future__ import annotations

import logging
from datetime import date, datetime

import akshare as ak
import pandas as pd

from quanti.data.database import Database

logger = logging.getLogger(__name__)


class AkShareAdapter:
    """Fetches A-share data from AkShare and saves to database."""

    def __init__(self, db: Database):
        self._db = db

    def sync_stock_list(self) -> int:
        """Fetch and save A-share stock list. Returns count."""
        df = ak.stock_info_a_code_name()
        count = 0
        for _, row in df.iterrows():
            code = row["code"]
            name = row["name"]
            try:
                info = ak.stock_individual_info_em(symbol=code)
                info_dict = dict(zip(info["item"], info["value"]))
                list_date_str = info_dict.get("上市时间", "19700101")
                list_date = datetime.strptime(str(list_date_str), "%Y%m%d").date()
                industry = info_dict.get("行业", "")
                exchange = "SH" if code.startswith("6") else "SZ"
                self._db.upsert_stock(code, name, exchange, list_date, industry)
                count += 1
            except Exception as e:
                logger.warning(f"Failed to fetch info for {code}: {e}")
        return count

    def sync_daily_quotes(
        self,
        code: str,
        start: date | None = None,
        end: date | None = None,
    ) -> int:
        """Fetch and save daily quotes for a stock. Returns count of new rows."""
        if end is None:
            end = date.today()
        if start is None:
            latest = self._db.get_latest_quote_date(code)
            start = latest if latest else date(2020, 1, 1)

        try:
            raw = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust="qfq",  # 前复权
            )
        except Exception as e:
            logger.warning(f"Failed to fetch quotes for {code}: {e}")
            return 0

        if raw.empty:
            return 0

        df = pd.DataFrame(
            {
                "code": code,
                "date": pd.to_datetime(raw["日期"]).dt.date,
                "open": raw["开盘"].astype(float),
                "high": raw["最高"].astype(float),
                "low": raw["最低"].astype(float),
                "close": raw["收盘"].astype(float),
                "volume": raw["成交量"].astype(float),
                "amount": raw["成交额"].astype(float),
                "turnover": raw["换手率"].astype(float) if "换手率" in raw.columns else 0,
            }
        )
        return self._db.save_daily_quotes(df)

    def sync_trade_calendar(self, year: int | None = None) -> int:
        """Fetch and save trade calendar."""
        try:
            cal = ak.tool_trade_date_hist_sina()
            dates = [d.date() if hasattr(d, "date") else d for d in cal["trade_date"]]
            if year:
                dates = [d for d in dates if d.year == year]
            self._db.save_trade_calendar(dates)
            return len(dates)
        except Exception as e:
            logger.warning(f"Failed to fetch trade calendar: {e}")
            return 0
