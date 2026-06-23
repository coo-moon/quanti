"""Daily-quote completeness + quality validation — source-agnostic.

Validates what's PERSISTED in daily_quotes against the trade calendar, so it
covers every vendor (tushare / akshare / xtdata) uniformly. This is the gap the
async sync had: the akshare adapter's fetch-time checks were log-only (never
surfaced) and never ran for tushare at all, so the only completeness signal a
job exposed was "0 rows = failed". Here we count the REAL missing trading days
(to the single day, via the calendar — not the akshare adapter's >15-day-gap
heuristic) plus OHLC / non-positive-price / duplicate-date defects, and the
caller writes them onto the sync job for the UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass
class CompletenessReport:
    code: str
    expected: int = 0           # trading days expected in the window
    present: int = 0            # of those, how many have a stored bar
    missing_days: list[date] = field(default_factory=list)
    bad_ohlc: int = 0           # high<low, or close outside [low, high]
    nonpos_price: int = 0       # open/close <= 0
    dup_dates: int = 0          # duplicate (code,date) rows
    used_calendar: bool = True  # False → weekday fallback (calendar unsynced)

    @property
    def coverage(self) -> float:
        return self.present / self.expected if self.expected else 1.0

    @property
    def clean(self) -> bool:
        return not (self.missing_days or self.bad_ohlc
                    or self.nonpos_price or self.dup_dates)

    def summary(self) -> str:
        parts = [f"覆盖 {self.coverage:.0%} ({self.present}/{self.expected})"]
        if self.missing_days:
            head = ", ".join(d.isoformat() for d in self.missing_days[:3])
            more = "…" if len(self.missing_days) > 3 else ""
            parts.append(f"缺 {len(self.missing_days)} 个交易日 [{head}{more}]")
        if self.bad_ohlc:
            parts.append(f"{self.bad_ohlc} 坏OHLC")
        if self.nonpos_price:
            parts.append(f"{self.nonpos_price} 非正价")
        if self.dup_dates:
            parts.append(f"{self.dup_dates} 重复日")
        if not self.used_calendar:
            parts.append("(交易日历未同步,按工作日估算)")
        return "; ".join(parts)


def expected_trading_days(db, start: date, end: date) -> tuple[list[date], bool]:
    """Trading days in [start, end] from `trade_calendar`; weekday fallback
    (coarser — counts CN holidays as missing) when the calendar isn't synced.
    Returns (days, used_calendar)."""
    cal = db.get_trade_dates(start, end)
    if cal:
        return sorted(cal), True
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out, False


def check_quote_completeness(db, code: str, start: date, end: date, *,
                             expected_days: list[date] | None = None,
                             used_calendar: bool | None = None
                             ) -> CompletenessReport:
    """Compare persisted bars for `code` in [start, end] against the trade
    calendar. Reads RAW stored bars (`db.get_daily_quotes`), so the result is
    identical for every data source. Pass `expected_days`/`used_calendar` to
    reuse a window-level calendar lookup across many codes (avoids one calendar
    query per code in a bulk sync)."""
    if expected_days is None:
        expected_days, used_calendar = expected_trading_days(db, start, end)
    rep = CompletenessReport(code=code, expected=len(expected_days),
                             used_calendar=bool(used_calendar))
    df = db.get_daily_quotes(code, start, end)
    if df is None or len(df) == 0:
        rep.missing_days = list(expected_days)
        return rep
    have = {d if isinstance(d, date) else date.fromisoformat(str(d))
            for d in df["date"]}
    rep.present = sum(1 for d in expected_days if d in have)
    rep.missing_days = [d for d in expected_days if d not in have]
    o, h, lo, c = df["open"], df["high"], df["low"], df["close"]
    rep.bad_ohlc = int(((h < lo) | (c > h) | (c < lo)).sum())
    rep.nonpos_price = int(((o <= 0) | (c <= 0)).sum())
    rep.dup_dates = int(df.duplicated(subset=["date"]).sum())
    return rep
