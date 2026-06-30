"""Market hours and trading-day utilities for A-share simulation.

A-share trading sessions (Beijing time, UTC+8):
  * Morning:   09:30 - 11:30
  * Afternoon: 13:00 - 15:00
  * Closed:    Saturday, Sunday, statutory holidays

We can't yet rely on `trade_calendar` (often empty in fresh installs) for
holiday awareness, so this module's weekend/time-window heuristic is the
floor. When the user runs `quanti sync --calendar` the holiday-aware path
kicks in automatically.

These helpers are intentionally small + pure so they can be tested without
a DB or any external state. Callers that need DB-backed precision should
prefer `provider.is_trade_date()` when calendar data is available.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from quanti.data.provider import DataProvider
from quanti.models import BarData, Direction


# Beijing is UTC+8 with no DST. Hard-coded because A-shares only trade there.
BEIJING_TZ = timezone(timedelta(hours=8))

# A-share trading session windows (Beijing time).
MORNING_OPEN = time(9, 30)
MORNING_CLOSE = time(11, 30)
AFTERNOON_OPEN = time(13, 0)
AFTERNOON_CLOSE = time(15, 0)


def _to_beijing(dt: datetime | None) -> datetime:
    """Normalize an input datetime to Beijing wall-clock time.

    Naive datetimes are interpreted as already-Beijing (matches how the
    rest of the codebase uses `datetime.now()` — the server is assumed to
    run in CN timezone). Aware datetimes are converted properly.
    """
    if dt is None:
        return datetime.now(BEIJING_TZ).replace(tzinfo=None)
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(BEIJING_TZ).replace(tzinfo=None)


def is_market_open(now: datetime | None = None) -> bool:
    """True iff `now` (Beijing time) is inside a trading session.

    Heuristic only — does NOT consult `trade_calendar` for holidays. Use
    `is_market_open_strict(now, provider)` when calendar is populated.
    """
    dt = _to_beijing(now)
    if dt.weekday() >= 5:  # Sat / Sun
        return False
    t = dt.time()
    if MORNING_OPEN <= t <= MORNING_CLOSE:
        return True
    if AFTERNOON_OPEN <= t <= AFTERNOON_CLOSE:
        return True
    return False


def is_trading_day(d: date, provider: Optional[DataProvider] = None) -> bool:
    """True if `d` is a (likely) trading day.

    With a provider whose `trade_calendar` is populated, this is exact.
    Without it, falls back to "weekday + not in hardcoded holiday set".

    The hardcoded set is intentionally minimal — just the spring festival
    + national day for the current year, refreshed by the user via
    `quanti sync --calendar` (which dumps the authoritative list into DB).
    """
    if provider is not None:
        try:
            if provider.is_trade_date(d):
                return True
            # If calendar has ANY data, treat absent days as non-trading.
            # Otherwise (empty calendar) fall through to heuristic.
            sample = provider.get_trade_dates(
                d - timedelta(days=30), d + timedelta(days=30))
            if sample:
                return False
        except Exception:
            pass
    return d.weekday() < 5


def in_trading_session(now: datetime | None = None,
                       provider: Optional[DataProvider] = None) -> bool:
    """Calendar-aware trading-session check: `now` (Beijing) is inside a
    morning/afternoon session AND it's a real trading day. Gates the live
    intraday guard so it idles outside market hours and on holidays."""
    dt = _to_beijing(now)
    return is_market_open(dt) and is_trading_day(dt.date(), provider)


def next_trading_day(after: date,
                     provider: Optional[DataProvider] = None) -> date:
    """First trading day strictly after `after`."""
    d = after + timedelta(days=1)
    # Bounded search — max 14 calendar days covers the longest A-share
    # holiday (national day + spring festival often hit ~10 consecutive).
    for _ in range(14):
        if is_trading_day(d, provider):
            return d
        d += timedelta(days=1)
    # Pathological fallback so we never return None.
    return d


def next_trading_bar(provider: DataProvider, code: str,
                     after_date: date) -> Optional[BarData]:
    """Earliest available bar for `code` with date > `after_date`.

    Returns None when no such bar exists yet — meaning the order should
    stay pending until the data feed catches up.

    Bounded lookback to keep this fast: we ask for bars from `after_date`
    forward up to 14 days. If we needed more we'd ask for an unbounded
    range, but in practice if no bar arrived in 14 days the stock is
    probably suspended and the pending order will be expired by then.
    """
    end = after_date + timedelta(days=14)
    bars = provider.get_daily_bars(code, after_date + timedelta(days=1), end)
    if not bars:
        return None
    # Bars are sorted ascending by date; take the first.
    return bars[0]


def count_trading_days_between(start: date, end: date,
                               provider: Optional[DataProvider] = None) -> int:
    """Number of trading days in (start, end], for pending-TTL math.

    Bounded; if start >= end, returns 0. Used to decide "this pending has
    been queued for N trading days, time to cancel".
    """
    if start >= end:
        return 0
    n = 0
    d = start + timedelta(days=1)
    while d <= end:
        if is_trading_day(d, provider):
            n += 1
        d += timedelta(days=1)
    return n


# --- Daily price-limit (涨跌停) tradability gate -----------------------------
# Shared by the backtest engine AND the live brokers so "what can actually
# fill" is defined in exactly one place — the divergence the 2026-06-22 audit
# flagged as C3 (paper/live filled into 一字板 that the backtest correctly
# skipped). QMT live is gated by the venue itself; this covers paper + backtest.

def board_limit_pct(code: str) -> float:
    """A-share daily price-limit by board (best-effort by code prefix).

    STAR (688/689) + ChiNext (300/301): 20%; Beijing Exchange (8../4../920):
    30%; main board: 10%. ST (±5%) can't be detected from the code alone, so it
    falls back to the board limit (conservative — won't over-block)."""
    if code.startswith(("688", "689", "300", "301")):
        return 0.20
    if code.startswith(("8", "4", "920", "92")):
        return 0.30
    return 0.10


def _within_limit(direction: Direction, code: str, fill_price: float,
                  prev_close: float | None) -> bool:
    """Whether a fill at `fill_price` is realistic given the daily price limit:
    a BUY can't fill at/above limit-up, a SELL can't fill at/below limit-down.
    Without a prior close to reference, allow it (conservative)."""
    if prev_close is None or prev_close <= 0:
        return True
    lim = board_limit_pct(code)
    eps = 0.005
    if direction == Direction.BUY:
        return fill_price < prev_close * (1 + lim) - eps
    return fill_price > prev_close * (1 - lim) + eps


def tradable_at_open(direction: Direction, bar: BarData,
                     prev_close: float | None) -> bool:
    """Tradability for an OPEN-fill (backtest + pending brokers): blocks buying
    into a limit-up open and selling into a limit-down open (incl. 一字板)."""
    return _within_limit(direction, bar.code, bar.open, prev_close)


def tradable_at_close(direction: Direction, bar: BarData,
                      prev_close: float | None) -> bool:
    """Tradability for a CLOSE-fill (immediate broker mode): blocks buying when
    the close is sealed at limit-up and selling when sealed at limit-down."""
    return _within_limit(direction, bar.code, bar.close, prev_close)


def prev_bar_close(provider: DataProvider, code: str,
                   before_date: date) -> float | None:
    """Close of the most recent bar strictly before `before_date` — the prior
    close the daily price-limit is computed from. None when unavailable."""
    bars = provider.get_daily_bars(
        code, before_date - timedelta(days=20), before_date - timedelta(days=1))
    return bars[-1].close if bars else None


# --- Single-bar capacity cap (成交量上限) --------------------------------
# Max share of a bar's turnover one order may take. A single order can't
# realistically be the whole day's volume — backtest/paper that fill a huge
# order instantly overstate fills (audit B1). zipline's default is 2.5%; 25%
# here is generous (we already trade a liquidity-filtered universe), bounding
# only the egregious "buy 100% of a thin day's turnover" case.
DEFAULT_PARTICIPATION = 0.25


def max_fill_shares(bar_amount: float, price: float,
                    participation: float = DEFAULT_PARTICIPATION) -> int | None:
    """Largest lot-rounded (×100) share quantity fillable in ONE bar without
    taking more than `participation` of that bar's turnover (成交额, 元).

    Uses 成交额 (元) not volume (shares), so it is free of the akshare-手 /
    xtdata-股 volume-unit mismatch (A3). Returns None when turnover/price are
    unavailable (caller then applies no cap)."""
    if not bar_amount or bar_amount <= 0 or price <= 0:
        return None
    return int(participation * bar_amount / (price * 100)) * 100


def lot_round_strength(qty: int, strength: float, lot: int = 100) -> int:
    """Scale a sellable quantity by a signal's `strength`, rounded DOWN to whole
    lots. `strength >= 1.0` (the default for full exits — stop-loss / take-profit
    / flatten / strategy-sell) returns `qty` UNCHANGED, so existing full-exit
    behavior is untouched. A partial strength (e.g. 0.4 from a concentration
    trim) returns `floor(qty * strength / lot) * lot` — a sub-lot trim → 0 (no-op)."""
    if strength >= 1.0:
        return qty
    if strength <= 0.0:
        return 0
    # round() before floor-to-lot: int() alone truncates float error
    # (10000*0.57 = 5699.9999… → 5699 → 5600 instead of 5700).
    return int(round(qty * strength)) // lot * lot
