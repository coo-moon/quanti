"""Tushare data adapter — A-share roster (incl. delisted) + qfq daily bars.

Closes survivorship bias for backtests: Tushare's free/low-tier `stock_basic`
returns delisted names with their delist_date, and `pro_bar` returns the full
price history of a delisted ts_code up to its delisting day. Both land through
the SAME db.upsert_stock / db.save_daily_quotes exits as AkShare/xtdata, so the
rest of the system reads SQLite unchanged.

tushare is an OPTIONAL dependency (guarded import). Without the package or a
TUSHARE_TOKEN, the adapter still imports; its methods raise a clear error.
Token is read from the TUSHARE_TOKEN env var and is never logged.

# VERIFY (real token / real box): pro_bar history depth for delisted ts_codes,
# adj='qfq' availability for delisted names, and current per-endpoint point
# thresholds (changes over time; see https://tushare.pro/document/1?doc_id=290).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime

import pandas as pd

try:
    import tushare as ts
except ImportError:  # pragma: no cover - exercised via monkeypatch
    ts = None

from quanti.data.database import Database

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds; free tier rate-limits per-minute call counts


class TushareAdapter:
    """Fetches A-share data (incl. delisted) from Tushare and saves to the DB."""

    def __init__(self, db: Database, token: str | None = None, *,
                 pro=None, pro_bar=None) -> None:
        self._db = db
        self._token = token
        self._pro = pro            # injected in tests; lazily built otherwise
        self._pro_bar_fn = pro_bar  # injected in tests; ts.pro_bar otherwise

    # --- ts_code <-> code mapping ---

    @staticmethod
    def _code_to_ts_code(code: str) -> str:
        if code.startswith("6"):
            return f"{code}.SH"
        if code.startswith(("4", "8")):
            return f"{code}.BJ"
        return f"{code}.SZ"

    @staticmethod
    def _ts_code_to_code(ts_code: str) -> tuple[str, str]:
        code, _, suffix = ts_code.partition(".")
        suffix = suffix.upper()
        exchange = suffix if suffix in ("SH", "SZ", "BJ") else (
            "SH" if code.startswith("6") else "SZ"
        )
        return code, exchange

    @staticmethod
    def _parse_ts_date(v) -> date | None:
        """Tushare gives 'YYYYMMDD' strings or None/NaN/'' → date | None."""
        if v is None:
            return None
        s = str(v).strip()
        if not s or s.lower() == "nan" or len(s) < 8:
            return None
        try:
            return datetime.strptime(s[:8], "%Y%m%d").date()
        except ValueError:
            return None

    # --- lazy network seams (never invoked when fully injected) ---

    def _ensure_pro(self):
        if self._pro is None:
            if ts is None:
                raise RuntimeError(
                    "tushare not installed; run: pip install 'quanti[data]'")
            token = self._token or os.environ.get("TUSHARE_TOKEN")
            if not token:
                raise RuntimeError("TUSHARE_TOKEN not set")
            ts.set_token(token)  # so module-level ts.pro_bar works too
            self._pro = ts.pro_api(token)
        return self._pro

    def _bar_fn(self):
        if self._pro_bar_fn is not None:
            return self._pro_bar_fn
        if ts is None:
            raise RuntimeError(
                "tushare not installed; run: pip install 'quanti[data]'")
        self._ensure_pro()  # ensures ts present + token registered
        return ts.pro_bar

    @staticmethod
    def _retry(fn, *args, **kwargs):
        last_err: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 - upstream/rate-limit transient
                last_err = e
                logger.warning("tushare call attempt %d/%d failed: %s",
                               attempt, MAX_RETRIES, e)
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
        if last_err is not None:
            raise last_err
        return None

    # --- public API ---

    def sync_stock_list(self) -> int:
        """Fetch L (listed) + D (delisted) + P (paused) rosters and upsert each,
        carrying delist_date for the delisted ones. Returns count saved."""
        pro = self._ensure_pro()
        count = 0
        for status in ("L", "D", "P"):
            df = self._retry(
                pro.stock_basic, list_status=status,
                fields="ts_code,name,list_date,delist_date")
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                code, exchange = self._ts_code_to_code(str(row["ts_code"]))
                list_date = self._parse_ts_date(row.get("list_date"))
                if list_date is None:
                    continue  # list_date is NOT NULL in schema; skip junk rows
                delist_date = self._parse_ts_date(row.get("delist_date"))
                try:
                    self._db.upsert_stock(
                        code, str(row["name"]), exchange, list_date,
                        industry="", delist_date=delist_date)
                    count += 1
                except Exception as e:  # noqa: BLE001 - one bad row shouldn't abort
                    logger.warning("save %s failed: %s", code, e)
        return count

    def sync_daily_quotes(self, code: str, start: date | None = None,
                          end: date | None = None) -> int:
        """Fetch qfq daily bars for `code` (incremental from the last stored bar
        by default) and save them. turnover is set to 0 (free tier lacks it).
        Returns rows saved."""
        if end is None:
            end = date.today()
        if start is None:
            latest = self._db.get_latest_quote_date(code)
            start = latest if latest else date(2010, 1, 1)

        bar = self._bar_fn()
        # Prefer the exchange stored by sync_stock_list (parsed from the real
        # ts_code suffix); the bare-prefix heuristic mis-maps BSE 92x (→.BJ)
        # and SH B-shares 90x (→.SH) to .SZ. Fall back only when not in roster.
        stock = self._db.get_stock(code)
        ts_code = (f"{code}.{stock.exchange}" if stock is not None
                   else self._code_to_ts_code(code))
        raw = self._retry(
            bar, ts_code=ts_code, asset="E", adj="qfq",
            start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
        if raw is None or raw.empty:
            return 0

        df = pd.DataFrame({
            "code": code,
            "date": pd.to_datetime(raw["trade_date"]).dt.date,
            "open": raw["open"].astype(float),
            "high": raw["high"].astype(float),
            "low": raw["low"].astype(float),
            "close": raw["close"].astype(float),
            "volume": raw["vol"].astype(float),
            "amount": raw["amount"].astype(float),
            "turnover": 0.0,
        })
        saved = self._db.save_daily_quotes(df)
        logger.info("%s: %d bars [%s~%s] via tushare", code, saved,
                    df["date"].min(), df["date"].max())
        return saved
