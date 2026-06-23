"""Unified data provider interface."""

from __future__ import annotations

from datetime import date

import pandas as pd

from quanti.data.database import Database
from quanti.models import BarData


class DataProvider:
    """Reads market data from database. Single source of truth for strategies.

    `daily_quotes` stores RAW (不复权) prices + a per-(code,date) back-adjustment
    factor `adj_factor`. This is the single boundary where adjustment is applied:
    with ``adjust="hfq"`` (the default) prices are back-adjusted to a continuous
    series (correct returns, no ex-dividend gaps); with ``adjust="none"`` the raw
    market prices are returned (for live order pricing and UI display). The DB
    layer never adjusts — so gap-repair / re-reads can't double-adjust.
    """

    def __init__(self, db: Database):
        self._db = db

    @staticmethod
    def _apply_adjust(df: pd.DataFrame, adjust: str) -> pd.DataFrame:
        """Back-adjust (hfq) a raw-price frame by adj_factor, or return raw.

        adjusted price = raw × adj_factor; volume = raw / adj_factor (Qlib
        convention, keeps price×volume ≈ amount); amount/turnover unchanged.
        Returns a copy — never mutates the input — so no double-adjustment.
        """
        if adjust == "none" or df.empty or "adj_factor" not in df.columns:
            return df
        df = df.copy()
        f = df["adj_factor"].astype(float)
        for col in ("open", "high", "low", "close"):
            df[col] = df[col] * f
        df["volume"] = df["volume"] / f
        return df

    def get_daily_bars(self, code: str, start: date, end: date,
                       adjust: str = "hfq") -> list[BarData]:
        """Get daily bars. ``adjust="hfq"`` (default) returns back-adjusted
        prices; ``adjust="none"`` returns raw market prices."""
        df = self._apply_adjust(self._db.get_daily_quotes(code, start, end), adjust)
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

    def get_daily_df(self, code: str, start: date, end: date,
                    adjust: str = "hfq") -> pd.DataFrame:
        """Get daily data as DataFrame (for factor computation). Adjusted (hfq)
        by default; pass ``adjust="none"`` for raw prices."""
        return self._apply_adjust(self._db.get_daily_quotes(code, start, end), adjust)

    def get_daily_basic_df(self, code: str, start: date, end: date) -> pd.DataFrame:
        """Per-(code,date) valuation (PE/PB/PS/mv/dv/turnover). Point-in-time by
        construction — used by the factor panel."""
        return self._db.get_daily_basic(code, start, end)

    def get_financials_asof(self, code: str, as_of: date) -> pd.DataFrame:
        """Financial reports ANNOUNCED on/before `as_of` (ann_date ≤ as_of) —
        the point-in-time-safe view for fundamental factors."""
        return self._db.get_financials_asof(code, as_of)

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
