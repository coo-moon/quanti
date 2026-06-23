"""Tushare data adapter — A-share roster (incl. delisted) + RAW daily bars.

Closes survivorship bias for backtests: Tushare's free/low-tier `stock_basic`
returns delisted names with their delist_date, and `daily` returns the full RAW
price history of a ts_code up to its delisting day. Both land through the SAME
db.upsert_stock / db.save_daily_quotes exits as AkShare/xtdata, so the rest of
the system reads SQLite unchanged.

Back-adjustment (hfq) is reconstructed from `daily`'s pre_close (see
`reconstruct_adj_factor`) so we NEVER call the `adj_factor`/`pro_bar` endpoints,
which are rate-limited far harder than `daily` (doc_id=27). This is the key to
backfilling years of history without tripping the per-minute limit.

tushare is an OPTIONAL dependency (guarded import). Without the package or a
TUSHARE_TOKEN, the adapter still imports; its methods raise a clear error.
Token is read from the TUSHARE_TOKEN env var and is never logged.

# VERIFIED 2026-06-23 (real token, 600519 daily 2022-2024, 725 bars incl. 6
# ex-div days): reconstructed hfq returns match tushare's own pct_chg to <5e-7;
# the factor steps up monotonically across each dividend. Observed per-endpoint
# limits on a LOW-points token: `daily` 50/min, `adj_factor` 1/HOUR — exactly
# why we reconstruct from pre_close (higher tiers raise `daily` toward 500/min).
"""

from __future__ import annotations

import logging
import math
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
RATE_LIMIT_WAIT = 61  # seconds to wait out a per-minute rate limit (patient mode)
# Canonical DB units: volume = 股 (shares), amount = 元 (yuan). Tushare returns
# vol in 手 (lots) and amount in 千元 (thousand-yuan), so convert at the edge.
# VERIFIED 2026-06-23 (real token, 600519): (amount*1000)/(vol*100) lands inside
# [low, high] on 100% of bars → vol=手, amount=千元 confirmed.
TS_VOL_TO_SHARES = 100
TS_AMOUNT_TO_YUAN = 1000


def reconstruct_adj_factor(raw_close, pre_close, *, seed_close=None,
                           seed_factor=1.0):
    """Back-adjustment (hfq) factor reconstructed from `daily`'s `pre_close` —
    so we NEVER call the `adj_factor` endpoint (the rate-limited one, as low as
    1 call/min on low point tiers; `daily` itself is 500/min).

    On an ex-rights day tushare's `pre_close[t]` is the previous close adjusted
    for the action, so it differs from the raw close[t-1]; that ratio captures
    the corporate action. With the factor anchored at the first bar:

        f[t] = f[t-1] * close[t-1] / pre_close[t]

    making `raw_close[t] * f[t]` a continuous back-adjusted (hfq) series whose
    bar-to-bar return equals tushare's own close/pre_close — identical returns
    to native hfq (only the absolute anchor differs, which is irrelevant for
    backtests). `seed_close`/`seed_factor` continue a previously stored bar so
    INCREMENTAL syncs splice seamlessly (omit → fresh anchor f0 = 1.0). A
    missing/non-positive pre_close contributes no adjustment (ratio 1.0).

    Args are date-ASCENDING parallel sequences. Returns a list of factors."""
    factors: list[float] = []
    prev_close = seed_close            # None → first bar anchors at seed_factor
    prev_factor = float(seed_factor)
    for c, pc in zip(raw_close, pre_close):
        c = float(c)
        if prev_close is None:
            f = prev_factor            # first-ever bar → anchor (no prior close)
        else:
            try:
                pcv = float(pc)
            except (TypeError, ValueError):
                pcv = 0.0
            ratio = (prev_close / pcv if pcv > 0 and prev_close > 0
                     and math.isfinite(pcv) else 1.0)
            if not math.isfinite(ratio) or ratio <= 0:
                ratio = 1.0
            f = prev_factor * ratio
        factors.append(f)
        prev_close, prev_factor = c, f
    return factors


class TushareAdapter:
    """Fetches A-share data (incl. delisted) from Tushare and saves to the DB."""

    def __init__(self, db: Database, token: str | None = None, *,
                 pro=None) -> None:
        self._db = db
        self._token = token
        self._pro = pro            # injected in tests; lazily built otherwise

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

    # --- lazy network seam (never invoked when fully injected) ---

    def _ensure_pro(self):
        if self._pro is None:
            if ts is None:
                raise RuntimeError(
                    "tushare not installed; run: pip install 'quanti[data]'")
            token = self._token or os.environ.get("TUSHARE_TOKEN")
            if not token:
                raise RuntimeError("TUSHARE_TOKEN not set")
            self._pro = ts.pro_api(token)
        return self._pro

    @staticmethod
    def _retry(fn, *args, **kwargs):
        # _patient (popped, not forwarded): on a PER-MINUTE rate-limit, wait out
        # the ~60s window and retry instead of the short backoff. Used by patient
        # callers (CLI/backfill) where a slow token (e.g. stock_basic 1/min) is
        # worth waiting for; API stays non-patient so it fails fast + clean.
        patient = kwargs.pop("_patient", False)
        last_err: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 - upstream/rate-limit transient
                last_err = e
                logger.warning("tushare call attempt %d/%d failed: %s",
                               attempt, MAX_RETRIES, e)
                if attempt < MAX_RETRIES:
                    msg = str(e)
                    if patient and "频率超限" in msg and "分钟" in msg:
                        time.sleep(RATE_LIMIT_WAIT)   # let the 1-min window reset
                    else:
                        time.sleep(RETRY_DELAY * attempt)
        if last_err is not None:
            raise last_err
        return None

    # --- public API ---

    def sync_stock_list(self, patient: bool = False) -> int:
        """Fetch L (listed) + D (delisted) + P (paused) rosters and upsert each,
        carrying delist_date for the delisted ones. Returns count saved.
        `patient=True` waits out per-minute rate limits (stock_basic can be
        1/min on low tiers → ~2 min for all three) — set it for CLI, not the API."""
        pro = self._ensure_pro()
        count = 0
        for status in ("L", "D", "P"):
            df = self._retry(
                pro.stock_basic, list_status=status,
                fields="ts_code,name,list_date,delist_date", _patient=patient)
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

    def sync_trade_calendar(self, year: int | None = None) -> int:
        """Fetch SSE open trading days from tushare and save. Mirrors
        AkShareAdapter.sync_trade_calendar so the default source can own the
        calendar too."""
        pro = self._ensure_pro()
        df = self._retry(pro.trade_cal, exchange="SSE", is_open="1")
        if df is None or df.empty:
            return 0
        dates = []
        for v in df["cal_date"]:
            d = self._parse_ts_date(v)
            if d is not None and (year is None or d.year == year):
                dates.append(d)
        self._db.save_trade_calendar(dates)
        return len(dates)

    def sync_daily_quotes(self, code: str, start: date | None = None,
                          end: date | None = None,
                          repair_gaps: bool = True) -> int:
        """Fetch RAW daily bars for `code` (incremental from the last stored bar
        by default) and save them with a reconstructed adj_factor. ONE `daily`
        call (500/min) — no `adj_factor`/`pro_bar` call (rate-limited as low as
        1/min): the factor is rebuilt from `daily`'s pre_close (see
        `reconstruct_adj_factor`). turnover stays 0 (the per-code path has no
        daily_basic). Returns rows saved.

        `repair_gaps` is accepted for adapter-signature parity (sync sites pass
        it) but ignored — tushare is a single source, no cross-source repair."""
        if end is None:
            end = date.today()
        if start is None:
            latest = self._db.get_latest_quote_date(code)
            start = latest if latest else date(2010, 1, 1)

        pro = self._ensure_pro()
        ts_code = self._code_to_ts_code(code)
        sd, ed = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
        raw = self._retry(pro.daily, ts_code=ts_code, start_date=sd, end_date=ed)
        if raw is None or raw.empty:
            return 0

        raw = raw.sort_values("trade_date")  # tushare returns newest-first
        df = pd.DataFrame({
            "code": code,
            "date": pd.to_datetime(raw["trade_date"]).dt.date,
            "open": raw["open"].astype(float),
            "high": raw["high"].astype(float),
            "low": raw["low"].astype(float),
            "close": raw["close"].astype(float),
            # Normalize to canonical units (股 / 元) — see TS_* constants.
            "volume": raw["vol"].astype(float) * TS_VOL_TO_SHARES,
            "amount": raw["amount"].astype(float) * TS_AMOUNT_TO_YUAN,
            "turnover": 0.0,  # the per-code path has no daily_basic
            "source": "tushare",
        }).reset_index(drop=True)
        # Reconstruct adj_factor from pre_close, seeded by the stored bar just
        # before this window so an incremental append splices seamlessly.
        seed = self._db.get_latest_quote_before(code, df["date"].iloc[0])
        seed_close, seed_factor = seed if seed else (None, 1.0)
        df["adj_factor"] = reconstruct_adj_factor(
            df["close"].tolist(), raw["pre_close"].astype(float).tolist(),
            seed_close=seed_close, seed_factor=seed_factor)
        saved = self._db.save_daily_quotes(df)
        logger.info("%s: %d bars [%s~%s] via tushare", code, saved,
                    df["date"].min(), df["date"].max())
        return saved

    def sync_daily_quotes_by_date(self, trade_date: date,
                                  seed_state: dict | None = None,
                                  patient: bool = False) -> int:
        """Pull the WHOLE market for ONE trading day — the efficient bulk path.
        Returns rows saved. Delisted names appear in pro.daily for dates they
        traded.

        adj_factor is reconstructed from `daily`'s pre_close (NO `adj_factor`
        endpoint call — that's the rate-limited one, 1/min on low tiers; `daily`
        is 500/min). `seed_state` is a caller-owned {code: (raw_close, factor)}
        map carried across days so the cumulative factor splices day-to-day
        WITHOUT re-querying the DB or re-fetching history; on a miss it seeds
        from the DB's last stored bar (fresh anchor 1.0 if none). turnover +
        valuation come from daily_basic when that endpoint is available
        (gracefully skipped otherwise). `patient` waits out per-minute rate
        limits (set by the bulk backfill so a slow `daily` cap doesn't drop days)."""
        pro = self._ensure_pro()
        td = trade_date.strftime("%Y%m%d")
        raw = self._retry(pro.daily, trade_date=td, _patient=patient)
        if raw is None or raw.empty:
            return 0
        # daily_basic (point-tier gated) feeds BOTH the daily_basic table (P4
        # valuation factors) and the quotes' turnover — one call, not two.
        # Degrade gracefully (turnover 0, no valuation) if it's unavailable.
        turn_by_code: dict[str, float] = {}
        basic = None
        try:
            basic = self._retry(
                pro.daily_basic, trade_date=td, _patient=patient,
                fields=("ts_code,turnover_rate,pe,pe_ttm,pb,ps,ps_ttm,"
                        "dv_ratio,total_mv,circ_mv"))
        except Exception as e:  # noqa: BLE001
            logger.debug("daily_basic unavailable for %s: %s", td, e)
        if basic is not None and not basic.empty:
            turn_by_code = {str(r["ts_code"]): float(r["turnover_rate"] or 0)
                            for _, r in basic.iterrows()}
            self._save_daily_basic_frame(basic, trade_date)

        rows = []
        for _, r in raw.iterrows():
            ts_code = str(r["ts_code"])
            code, _ex = self._ts_code_to_code(ts_code)
            close = float(r["close"])
            # adj_factor = prev_factor × prev_close / pre_close (continuous hfq;
            # provider._apply_adjust does raw×factor). Seed from carried state or
            # the DB's last stored bar; fresh stocks anchor at 1.0.
            seed = (seed_state.get(code) if seed_state is not None else None)
            if seed is None:
                seed = self._db.get_latest_quote_before(code, trade_date)
            (factor,) = reconstruct_adj_factor(
                [close], [r.get("pre_close")],
                seed_close=seed[0] if seed else None,
                seed_factor=seed[1] if seed else 1.0)
            if seed_state is not None:
                seed_state[code] = (close, factor)
            rows.append({
                "code": code, "date": trade_date,
                "open": float(r["open"]), "high": float(r["high"]),
                "low": float(r["low"]), "close": close,
                "volume": float(r["vol"]) * TS_VOL_TO_SHARES,
                "amount": float(r["amount"]) * TS_AMOUNT_TO_YUAN,
                "turnover": turn_by_code.get(ts_code, 0.0),
                "adj_factor": factor,
                "source": "tushare",
            })
        return self._db.save_daily_quotes(pd.DataFrame(rows))

    def _save_daily_basic_frame(self, basic, trade_date: date) -> int:
        """Map a tushare daily_basic frame → daily_basic table (P4 valuation)."""
        rows = []
        for _, r in basic.iterrows():
            code, _ex = self._ts_code_to_code(str(r["ts_code"]))
            rows.append({
                "code": code, "date": trade_date,
                "pe": r.get("pe"), "pe_ttm": r.get("pe_ttm"),
                "pb": r.get("pb"), "ps": r.get("ps"), "ps_ttm": r.get("ps_ttm"),
                "total_mv": r.get("total_mv"), "circ_mv": r.get("circ_mv"),
                "dv_ratio": r.get("dv_ratio"),
                "turnover_rate": r.get("turnover_rate"),
            })
        return self._db.save_daily_basic(pd.DataFrame(rows))

    def sync_financials(self, code: str, patient: bool = False) -> int:
        """Per-code financial indicators (ROE + YoY growth) keyed by report
        period, carrying the REAL ann_date for point-in-time alignment (more
        precise than akshare's statutory-deadline proxy). Needs the 2000-point
        tier; degrades to 0 rows (logged) if the endpoint is unavailable.
        `patient` waits out per-minute rate limits (set by the CLI loop)."""
        pro = self._ensure_pro()
        ts_code = self._code_to_ts_code(code)
        try:
            df = self._retry(
                pro.fina_indicator, ts_code=ts_code, _patient=patient,
                fields="ts_code,ann_date,end_date,roe,netprofit_yoy,or_yoy")
        except Exception as e:  # noqa: BLE001 - point-tier / availability
            logger.info("fina_indicator unavailable for %s: %s", code, e)
            return 0
        if df is None or df.empty:
            return 0
        rows = []
        for _, r in df.iterrows():
            ann = self._parse_ts_date(r.get("ann_date"))
            end = self._parse_ts_date(r.get("end_date"))
            if ann is None or end is None:
                continue  # ann_date is the PIT key — skip rows without it
            rows.append({
                "code": code, "end_date": end.isoformat(),
                "ann_date": ann.isoformat(), "report_type": "",
                "roe": r.get("roe"), "net_profit": None, "revenue": None,
                "netprofit_yoy": r.get("netprofit_yoy"),
                "revenue_yoy": r.get("or_yoy"),
            })
        if not rows:
            return 0
        return self._db.save_financials(pd.DataFrame(rows))
