"""AkShare data source adapter."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import akshare as ak
import pandas as pd

from quanti.data.database import Database

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds
# A-share max consecutive non-trading days (Spring Festival ~11, National Day ~9)
MAX_HOLIDAY_GAP = 15
# Canonical DB units: volume = 股, amount = 元. East Money 成交量 is 手 (lots);
# Sina volume is already 股 — normalizing EM fixes a latent EM-vs-Sina mismatch
# (same code, 100× different volume depending on which source filled the bar).
EM_VOL_TO_SHARES = 100


def _code_to_sina_symbol(code: str) -> str:
    """Convert stock code to Sina symbol format (e.g. '600519' -> 'sh600519')."""
    if code.startswith("6"):
        return f"sh{code}"
    return f"sz{code}"


@dataclass
class SyncReport:
    """Data sync integrity report."""

    code: str
    rows: int = 0
    start: date | None = None
    end: date | None = None
    gaps: list[tuple[date, date, int]] = field(default_factory=list)  # (from, to, days)
    source: str = ""
    repaired: int = 0  # rows added during gap repair

    @property
    def ok(self) -> bool:
        return self.rows > 0 and len(self.gaps) == 0

    def summary(self) -> str:
        parts = [f"{self.code}: {self.rows} bars [{self.start}~{self.end}] via {self.source}"]
        if self.repaired:
            parts.append(f"  repaired {self.repaired} rows")
        for g_from, g_to, days in self.gaps:
            parts.append(f"  gap: {g_from} -> {g_to} ({days}d)")
        return "\n".join(parts)


def _detect_gaps(df: pd.DataFrame, max_gap: int = MAX_HOLIDAY_GAP) -> list[tuple[date, date, int]]:
    """Find abnormal gaps in a date-sorted DataFrame (must have 'date' column)."""
    if df.empty or len(df) < 2:
        return []
    dates = pd.to_datetime(df["date"]).sort_values()
    diffs = dates.diff().dt.days
    gaps = []
    for i in range(1, len(diffs)):
        d = diffs.iloc[i]
        if pd.notna(d) and int(d) > max_gap:
            gaps.append((dates.iloc[i - 1].date(), dates.iloc[i].date(), int(d)))
    return gaps


def _estimate_expected_rows(start: date, end: date) -> int:
    """Rough estimate of trading days (~242/year for A-share)."""
    calendar_days = (end - start).days
    return max(1, int(calendar_days * 242 / 365))


def _attach_adj_factor(df: pd.DataFrame, hfq: pd.DataFrame | None,
                       date_col: str, close_col: str) -> pd.DataFrame:
    """Attach `adj_factor = hfq_close / raw_close` (aligned by date) to a RAW
    price frame `df` (must have 'date' + 'close'). `hfq` is the source's
    back-adjusted frame; date_col/close_col name its date+close columns.

    The hfq series is anchored to the listing day (window-independent), so the
    factor for a given date is stable across incremental syncs — the property
    qfq lacked (audit A2). Missing dates or non-positive raw close → factor 1.0.
    # VERIFY against the installed akshare build: hfq is listing-anchored (qfq
    # is rebased to the latest day and MUST NOT be used to derive the factor)."""
    df = df.copy()
    if hfq is None or hfq.empty:
        df["adj_factor"] = 1.0
        return df
    h = pd.DataFrame({
        "date": pd.to_datetime(hfq[date_col]).dt.date,
        "_hfq": hfq[close_col].astype(float),
    }).drop_duplicates(subset=["date"])
    df = df.merge(h, on="date", how="left")
    raw_close = df["close"].astype(float)
    factor = pd.Series(1.0, index=df.index)
    ok = df["_hfq"].notna() & (raw_close > 0)
    factor[ok] = df["_hfq"][ok] / raw_close[ok]
    df["adj_factor"] = factor.where(factor > 0, 1.0)
    return df.drop(columns=["_hfq"])


class AkShareAdapter:
    """Fetches A-share data from AkShare and saves to database."""

    def __init__(self, db: Database):
        self._db = db

    def ensure_stock_info(self, code: str) -> None:
        """Register stock with real name/industry if not already in DB."""
        stock = self._db.get_stock(code)
        if stock is not None and stock.name != stock.code:
            return  # Already has real info

        exchange = "SH" if code.startswith("6") else "SZ"
        name = code
        industry = ""
        list_date = date(2000, 1, 1)

        try:
            info = ak.stock_individual_info_em(symbol=code)
            info_dict = dict(zip(info["item"], info["value"]))
            name = info_dict.get("股票简称", code)
            industry = info_dict.get("行业", "")
            list_date_str = info_dict.get("上市时间", "")
            if list_date_str:
                list_date = datetime.strptime(str(list_date_str), "%Y%m%d").date()
        except Exception as e:
            # Fallback: try the name list
            try:
                all_stocks = ak.stock_info_a_code_name()
                match = all_stocks[all_stocks["code"] == code]
                if not match.empty:
                    name = match.iloc[0]["name"]
            except Exception:
                pass
            if name == code:
                logger.warning(f"Could not fetch info for {code}: {e}")

        self._db.upsert_stock(code, name, exchange, list_date, industry)

    def sync_stock_list(self, patient: bool = False) -> int:
        """Fetch and save A-share stock list (code + name only). Returns count.
        `patient` accepted for adapter-signature parity (tushare uses it for
        per-minute rate limits); akshare ignores it.

        Retries on network failure — AkShare upstream occasionally resets
        connections and a one-shot failure shouldn't bring down the bootstrap.
        """
        df = None
        last_err: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                df = ak.stock_info_a_code_name()
                if df is not None and not df.empty:
                    break
            except Exception as e:
                last_err = e
                logger.warning(f"sync_stock_list attempt {attempt}/{MAX_RETRIES}: {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
        if df is None or df.empty:
            if last_err is not None:
                raise last_err
            return 0

        count = 0
        for _, row in df.iterrows():
            code = str(row["code"])
            name = str(row["name"])
            exchange = "SH" if code.startswith("6") else "SZ"
            # Use a default list_date; real date can be fetched later per-stock if needed
            list_date = date(2000, 1, 1)
            industry = ""
            try:
                self._db.upsert_stock(code, name, exchange, list_date, industry)
                count += 1
            except Exception as e:
                logger.warning(f"Failed to save {code}: {e}")
        return count

    # --- Data sources ---

    def _em_hist(self, code: str, start: date, end: date,
                 adjust: str) -> pd.DataFrame | None:
        """East Money raw akshare frame for the given adjust mode, with retries."""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                raw = ak.stock_zh_a_hist(
                    symbol=code, period="daily",
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"), adjust=adjust)
                if raw is not None and not raw.empty:
                    return raw
                return None
            except Exception as e:
                logger.warning(f"East Money({adjust or 'raw'}) attempt "
                               f"{attempt}/{MAX_RETRIES} for {code}: {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
        return None

    def _fetch_eastmoney(self, code: str, start: date, end: date) -> pd.DataFrame | None:
        """Fetch RAW (不复权) OHLCV + adj_factor (=hfq/raw) from East Money."""
        raw = self._em_hist(code, start, end, adjust="")  # RAW prices
        if raw is None or raw.empty:
            return None
        df = pd.DataFrame(
            {
                "code": code,
                "date": pd.to_datetime(raw["日期"]).dt.date,
                "open": raw["开盘"].astype(float),
                "high": raw["最高"].astype(float),
                "low": raw["最低"].astype(float),
                "close": raw["收盘"].astype(float),
                "volume": raw["成交量"].astype(float) * EM_VOL_TO_SHARES,  # 手→股
                "amount": raw["成交额"].astype(float),  # already 元
                "turnover": raw["换手率"].astype(float) if "换手率" in raw.columns else 0,
                "source": "akshare",
            }
        )
        hfq = self._em_hist(code, start, end, adjust="hfq")  # for the factor
        return _attach_adj_factor(df, hfq, "日期", "收盘")

    def _sina_daily_chunked(self, symbol: str, start: date, end: date,
                            adjust: str) -> pd.DataFrame | None:
        """Sina raw akshare frame for the given adjust mode, chunked by quarter
        to avoid truncation; concatenated + de-duped by date."""
        chunks: list[pd.DataFrame] = []
        chunk_start = start
        while chunk_start < end:
            chunk_end = min(
                date(chunk_start.year + (chunk_start.month + 2) // 12,
                     (chunk_start.month + 2) % 12 + 1, 1),
                end,
            )
            try:
                raw = ak.stock_zh_a_daily(
                    symbol=symbol,
                    start_date=chunk_start.strftime("%Y%m%d"),
                    end_date=chunk_end.strftime("%Y%m%d"),
                    adjust=adjust)
                if raw is not None and not raw.empty:
                    chunks.append(raw)
            except Exception as e:
                logger.warning(f"Sina({adjust or 'raw'}) chunk "
                               f"{chunk_start}~{chunk_end} for {symbol}: {e}")
            chunk_start = chunk_end
        if not chunks:
            return None
        return pd.concat(chunks, ignore_index=True).drop_duplicates(subset=["date"])

    def _fetch_sina(self, code: str, start: date, end: date) -> pd.DataFrame | None:
        """Fetch RAW (不复权) OHLCV + adj_factor (=hfq/raw) from Sina (fallback)."""
        symbol = _code_to_sina_symbol(code)
        raw = self._sina_daily_chunked(symbol, start, end, adjust="")  # RAW
        if raw is None or raw.empty:
            return None
        df = pd.DataFrame(
            {
                "code": code,
                "date": pd.to_datetime(raw["date"]).dt.date,
                "open": raw["open"].astype(float),
                "high": raw["high"].astype(float),
                "low": raw["low"].astype(float),
                "close": raw["close"].astype(float),
                "volume": raw["volume"].astype(float),  # Sina already 股
                "amount": raw["amount"].astype(float) if "amount" in raw.columns else 0,
                "turnover": raw["turnover"].astype(float) if "turnover" in raw.columns else 0,
                "source": "akshare",
            },
        )
        hfq = self._sina_daily_chunked(symbol, start, end, adjust="hfq")  # for factor
        return _attach_adj_factor(df, hfq, "date", "close")

    def _fetch_with_fallback(self, code: str, start: date, end: date) -> tuple[pd.DataFrame | None, str]:
        """Try East Money, fallback to Sina. Returns (df, source_name)."""
        df = self._fetch_sina(code, start, end)
        if df is not None and not df.empty:
            return df, "sina"
        logger.info(f"Sina unavailable for {code}, trying East Money...")
        df = self._fetch_eastmoney(code, start, end)
        if df is not None and not df.empty:
            return df, "eastmoney"
        return None, ""

    # --- Integrity validation ---

    def _validate_and_report(self, df: pd.DataFrame, code: str, start: date, end: date, source: str) -> SyncReport:
        """Validate data integrity and build report."""
        report = SyncReport(code=code, source=source)

        if df is None or df.empty:
            return report

        report.rows = len(df)
        report.start = df["date"].min()
        report.end = df["date"].max()

        # 1. Gap detection
        report.gaps = _detect_gaps(df)

        # 2. Row count sanity check
        expected = _estimate_expected_rows(start, end)
        ratio = report.rows / expected if expected > 0 else 1.0
        if ratio < 0.7:
            logger.warning(
                f"Data for {code} looks sparse: {report.rows} rows vs ~{expected} expected "
                f"({ratio:.0%} coverage)"
            )

        # 3. OHLC sanity: high >= low, close within [low, high]
        bad_ohlc = df[(df["high"] < df["low"]) | (df["close"] > df["high"]) | (df["close"] < df["low"])]
        if len(bad_ohlc) > 0:
            logger.warning(f"{code}: {len(bad_ohlc)} bars with invalid OHLC (high < low or close out of range)")

        # 4. Zero/negative price check
        bad_price = df[(df["close"] <= 0) | (df["open"] <= 0)]
        if len(bad_price) > 0:
            logger.warning(f"{code}: {len(bad_price)} bars with zero/negative price")

        # 5. Duplicate date check
        dup_dates = df[df.duplicated(subset=["date"], keep=False)]
        if len(dup_dates) > 0:
            logger.warning(f"{code}: {len(dup_dates)} duplicate date entries")

        return report

    def _repair_gaps(self, report: SyncReport, code: str, primary_source: str) -> int:
        """Attempt to fill gaps using the other data source. Returns rows added."""
        if not report.gaps:
            return 0

        total_added = 0
        for gap_from, gap_to, days in report.gaps:
            repair_start = gap_from + timedelta(days=1)
            repair_end = gap_to - timedelta(days=1)
            if repair_start >= repair_end:
                continue

            logger.info(f"Repairing gap {gap_from}->{gap_to} ({days}d) for {code}")

            # Try the OTHER source to fill the gap
            patch = None
            if primary_source == "eastmoney":
                patch = self._fetch_sina(code, repair_start, repair_end)
            else:
                patch = self._fetch_eastmoney(code, repair_start, repair_end)

            if patch is not None and not patch.empty:
                added = self._db.save_daily_quotes(patch)
                total_added += added
                logger.info(f"  filled {added} rows for gap {gap_from}->{gap_to}")

        return total_added

    # --- Public API ---

    def sync_daily_quotes(
        self,
        code: str,
        start: date | None = None,
        end: date | None = None,
        repair_gaps: bool = True,
    ) -> int:
        """Fetch, validate, and save daily quotes. Returns count of rows saved."""
        if end is None:
            end = date.today()
        if start is None:
            latest = self._db.get_latest_quote_date(code)
            start = latest if latest else date(2020, 1, 1)

        # Ensure stock has real name/info
        self.ensure_stock_info(code)

        # Fetch data
        df, source = self._fetch_with_fallback(code, start, end)
        if df is None or df.empty:
            return 0

        # Validate
        report = self._validate_and_report(df, code, start, end, source)
        saved = self._db.save_daily_quotes(df)

        # Repair gaps with cross-source fill (skip for bulk pool sync to avoid slowdowns)
        if repair_gaps and report.gaps:
            repaired = self._repair_gaps(report, code, source)
            report.repaired = repaired
            saved += repaired

            # Re-validate after repair
            full_df = self._db.get_daily_quotes(code, start, end)
            remaining_gaps = _detect_gaps(full_df)
            if remaining_gaps:
                report.gaps = remaining_gaps
                logger.warning(f"{code}: {len(remaining_gaps)} gaps remain after repair")
            else:
                report.gaps = []
                logger.info(f"{code}: all gaps repaired successfully")

        logger.info(report.summary())
        return saved

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

    # akshare 业绩报表 (东财) column → financials field. FREE alternative to
    # tushare's permission-gated fina_indicator, and richer: it also carries the
    # net_profit / revenue ABSOLUTES, and — crucially — the announcement date,
    # so financials stay point-in-time (merge_asof on ann_date, no look-ahead).
    _YJBB_MAP = {
        "净资产收益率": "roe",
        "净利润-净利润": "net_profit",
        "营业总收入-营业总收入": "revenue",
        "净利润-同比增长": "netprofit_yoy",
        "营业总收入-同比增长": "revenue_yoy",
    }

    @staticmethod
    def _statutory_ann_date(period: date) -> date:
        """PIT announcement date = the A-share regulatory disclosure DEADLINE for
        the report period. akshare 业绩报表's 最新公告日期 is a last-updated
        timestamp (often today / a later annual-report date), NOT this period's
        announce date — using it would mis-align PIT. The statutory deadline is
        look-ahead-safe: the report is guaranteed public by then (most firms
        report earlier, so this is conservative, never leaky).
          Q1 03-31 → 04-30 同年; 中报 06-30 → 08-31 同年;
          Q3 09-30 → 10-31 同年; 年报 12-31 → 次年 04-30。"""
        md = (period.month, period.day)
        if md == (3, 31):
            return date(period.year, 4, 30)
        if md == (6, 30):
            return date(period.year, 8, 31)
        if md == (9, 30):
            return date(period.year, 10, 31)
        if md == (12, 31):
            return date(period.year + 1, 4, 30)
        return period

    def sync_financials_by_period(self, period: date) -> int:
        """Pull the WHOLE market's report for ONE report period (e.g. 2024-03-31)
        via akshare 业绩报表 — free, no token, whole-market in one call. ann_date
        is the period's STATUTORY disclosure deadline (see _statutory_ann_date),
        NOT akshare's unreliable 最新公告日期, so financials stay point-in-time
        (merge_asof on ann_date, no look-ahead). Returns rows saved; degrades to
        0 (logged) on endpoint/columns drift. `financials` is its own table, so
        this is orthogonal to the daily_quotes source (one-source guard N/A)."""
        ds = period.strftime("%Y%m%d")
        try:
            df = ak.stock_yjbb_em(date=ds)
        except Exception as e:  # noqa: BLE001 - upstream/columns drift → skip
            logger.info("stock_yjbb_em unavailable for %s: %s", ds, e)
            return 0
        if df is None or df.empty or "股票代码" not in df.columns:
            return 0
        ann = self._statutory_ann_date(period)
        rows = []
        for _, r in df.iterrows():
            code = str(r.get("股票代码", "") or "").strip()
            if not code:
                continue
            rec = {"code": code, "end_date": period.isoformat(),
                   "ann_date": ann, "report_type": ""}
            for src, dst in self._YJBB_MAP.items():
                v = r.get(src)
                rec[dst] = None if pd.isna(v) else float(v)
            rows.append(rec)
        if not rows:
            return 0
        n = self._db.save_financials(pd.DataFrame(rows))
        logger.info("financials %s (ann≤%s): %d rows via akshare 业绩报表",
                    period.isoformat(), ann.isoformat(), n)
        return n
