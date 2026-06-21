"""Unified data provider interface."""

from __future__ import annotations

from datetime import date

import pandas as pd

from quanti.data.database import Database
from quanti.models import BarData


class DataProvider:
    """Reads market data from database. Single source of truth for strategies."""

    def __init__(self, db: Database):
        self._db = db

    def get_daily_bars(self, code: str, start: date, end: date) -> list[BarData]:
        """Get daily bar data as list of BarData objects."""
        df = self._db.get_daily_quotes(code, start, end)
        return [
            BarData(
                code=row["code"],
                date=row["date"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                amount=row["amount"],
                turnover=row.get("turnover", 0),
            )
            for _, row in df.iterrows()
        ]

    def get_daily_df(self, code: str, start: date, end: date) -> pd.DataFrame:
        """Get daily data as DataFrame (for factor computation)."""
        return self._db.get_daily_quotes(code, start, end)

    def get_trade_dates(self, start: date, end: date) -> list[date]:
        return self._db.get_trade_dates(start, end)

    def is_trade_date(self, d: date) -> bool:
        return self._db.is_trade_date(d)

    def get_all_codes(self) -> list[str]:
        """Get all stock codes."""
        return [s.code for s in self._db.list_stocks()]

    def get_adv20_map(self, start: date, end: date,
                      window: int = 20) -> dict[str, float]:
        """20-day ADV per code over [start, end], batched in one query.

        See Database.get_adv20_map — used by `sort_by_adv20` to rank a whole
        universe without one round-trip per code.
        """
        return self._db.get_adv20_map(start, end, window)
