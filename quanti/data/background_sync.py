"""Background daemon that keeps `daily_quotes` fresh independent of agent ticks.

The legacy path was: AgentRuntime, on every 4h tick, syncs ~20 codes that look
stale. That's fine for steady-state maintenance, but it makes cold-start
catastrophic: 5000 codes × 0.2 codes/tick = ~40 days to fill the DB. Users
hit this and (correctly) complain that the universe-filter cuts down to
nothing because most stocks have no bars at all.

This module runs continuously, throttled to a single AkShare-friendly pace,
so that even from a totally empty DB the system reaches full coverage in
20-40 minutes — and then quietly maintains freshness forever.

Design:
  * Daemon thread, started by FastAPI lifespan. Process exit kills it.
  * Idempotent start/stop; calling start() twice is a no-op.
  * Three states per loop iteration:
      ACTIVE  — queue has codes to sync; process `batch_size` then short sleep.
      IDLE    — queue drained; sleep `idle_interval_sec` (~30 min), rescan.
      PAUSED  — explicitly disabled via `pause()`; no work, just heartbeat.
  * Scan priority: pending-order codes > held positions > no bars > stale bars.
    Pending-order codes lead because a queued order can't fill until its next
    trading bar is on disk — syncing them first is what unblocks fills.
  * Per-code backoff (held codes included):
      - hard failure (exception / 0 rows): exponential, `failure_backoff_sec`
        doubling per consecutive failure up to `max_backoff_sec` — a feed that
        simply lacks the code (e.g. 北交所 on Sina) settles at a few probes/day.
      - synced but latest bar didn't advance (suspended/halted stock, or
        today's bar not published yet): flat `failure_backoff_sec`. Without
        this, a halted stock stays "stale" forever and re-syncs every loop.
  * Lockless reads of status (single-writer thread). Counters are session-
    scoped — they reset when the syncer starts, not at midnight.

This is intentionally NOT a replacement for the user-triggered `/sync/quotes`
endpoints — those still exist for explicit bulk syncs with progress reporting.
This module is the silent background process that means a user "shouldn't
have to think about data sync ever again."
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

#: A-share daily bars publish shortly after the 15:00 close; feeds are
#: reliably serving them by this wall-clock boundary.
MARKET_CLOSE_GRACE = dtime(15, 30)


def expected_latest_bar(now: datetime) -> date:
    """The most recent calendar date whose daily bar should exist upstream.

    Before the post-close boundary we expect the previous trading day's bar
    (today's doesn't exist yet); after it, today's. Weekends roll back to
    Friday. Weekday fallback instead of trade_calendar because the calendar
    table is typically unsynced — same convention as the paper broker. CN
    holidays therefore look like one stale day: the whole universe gets one
    no-new-data probe and lands in flat backoff, which is cheap and correct.
    """
    d = now.date()
    if now.time() < MARKET_CLOSE_GRACE:
        d -= timedelta(days=1)
    while d.weekday() >= 5:  # Sat/Sun
        d -= timedelta(days=1)
    return d


@dataclass
class BackgroundSyncConfig:
    batch_size: int = 5
    """Codes to sync before sleeping. Keeps the loop responsive to stop()."""

    per_code_sleep_sec: float = 0.5
    """Wall time between successive code syncs. AkShare against East Money /
    Sina handles ~2 req/sec comfortably; this gives headroom."""

    batch_idle_sec: float = 2.0
    """Sleep after each batch (mostly so logs aren't deafening)."""

    idle_interval_sec: int = 30 * 60
    """When queue is drained, wait this long before rescanning. 30 min
    means up to 30 min delay before catching a fresh listing — fine for
    daily-bar trading."""

    stale_after_days: int = 1
    """A code is "stale" once its latest bar is more than (stale_after_days-1)
    days behind the expected latest bar — today after the post-close grace
    (15:30), the previous trading day before it. 1 = refresh as soon as the
    newest expected bar is missing, i.e. one whole-universe incremental sweep
    shortly after each close, quiet the rest of the day."""

    failure_backoff_sec: int = 30 * 60
    """Base backoff after a failed (or no-new-data) sync. Hard failures double
    this per consecutive failure, capped at `max_backoff_sec`. Prevents one
    bad code (delisted, removed from feed) from being retried every loop."""

    max_backoff_sec: int = 4 * 3600
    """Ceiling for the exponential failure backoff (default 4h). A code the
    feed will never serve still gets re-probed a few times a day, so it heals
    on its own if the feed starts covering it."""

    lookback_days: int = 365
    """How much history to request when filling in a missing code. The
    underlying adapter is incremental for codes that already have data,
    so this is only the cold-start cost."""

    max_queue_size: int = 6000
    """Safety valve: cap queue size so a buggy scan can't OOM."""

    max_topup_days: int = 10
    """By-date (tushare) fast-path: if the library is more than this many trading
    days behind, the daemon tops up only the newest `max_topup_days` and defers
    the bulk to `quanti sync --backfill` (checkpointed/throttled). Stops the live
    loop from churning hundreds of rate-limited calls on a cold/stale DB."""

    by_date_sleep_sec: float = 1.0
    """Pause between successive per-trading-day sweeps on the by-date path."""


@dataclass
class BackgroundSyncStatus:
    """Point-in-time snapshot. Returned by `BackgroundQuoteSyncer.status()`."""

    enabled: bool = False
    running: bool = False
    state: str = "stopped"  # "stopped" | "active" | "idle" | "paused"
    started_at: Optional[str] = None
    last_loop_at: Optional[str] = None
    current_code: Optional[str] = None
    queue_remaining: int = 0
    synced_session: int = 0
    failed_session: int = 0
    backoff_codes: int = 0
    last_full_scan_at: Optional[str] = None
    last_error: Optional[str] = None
    config: dict = field(default_factory=dict)


class BackgroundQuoteSyncer:
    """Long-running daemon to keep daily_quotes fresh."""

    def __init__(
        self,
        db,  # Database (untyped to avoid circular import)
        adapter_factory=None,  # callable returning an AkShareAdapter; for tests
        config: BackgroundSyncConfig | None = None,
        now_fn=None,  # () -> datetime; injectable clock for tests
        financials_fn=None,  # () -> int; runs once/day if set (None = off, tests)
        mining_fn=None,      # () -> int; runs once/day if set (None = off, tests)
    ) -> None:
        self._db = db
        self._cfg = config or BackgroundSyncConfig()
        self._adapter_factory = adapter_factory or self._default_adapter_factory
        self._now = now_fn or datetime.now
        self._financials_fn = financials_fn
        self._last_fin_day = None  # date of the last financials sync (once/day)
        self._mining_fn = mining_fn
        self._last_mine_day = None  # date of the last factor mining (once/day)

        self._thread: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self._pause_flag = threading.Event()
        self._lock = threading.Lock()

        # Mutable status fields. Read concurrently via status(); we accept
        # mildly torn reads since the dashboard refreshes anyway.
        self._state: str = "stopped"
        self._current_code: str | None = None
        self._queue: deque[str] = deque()
        self._backoff_until: dict[str, float] = {}  # code → unix ts to retry
        self._fail_counts: dict[str, int] = {}  # code → consecutive hard fails
        self._synced_session = 0
        self._failed_session = 0
        self._started_at: str | None = None
        self._last_loop_at: str | None = None
        self._last_full_scan_at: str | None = None
        self._last_error: str | None = None

    # ----------------- adapter factory -----------------

    def _default_adapter_factory(self):
        # Resolve the configured source (DB app_config > env > default tushare).
        # Built per batch so a UI source/token change takes effect without a
        # restart. Raises DataSourceUnavailable when the source can't be built
        # (e.g. tushare with no token) — we do NOT silently fall back to akshare;
        # _process_batch catches it and skips the batch (next loop retries).
        from quanti.data.source import make_quote_adapter
        return make_quote_adapter(self._db)

    # ----------------- public API -----------------

    def start(self) -> None:
        """Start the daemon. Safe to call multiple times."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_flag.clear()
            self._pause_flag.clear()
            self._synced_session = 0
            self._failed_session = 0
            self._started_at = datetime.now().isoformat()
            self._state = "active"
            self._thread = threading.Thread(
                target=self._loop, name="quanti-bg-sync", daemon=True)
            self._thread.start()
            logger.info("BackgroundQuoteSyncer started")

    def stop(self) -> None:
        """Signal the daemon to stop. Returns immediately; thread exits at
        the next loop boundary (within a few seconds)."""
        self._stop_flag.set()
        with self._lock:
            self._state = "stopped"
        logger.info("BackgroundQuoteSyncer stop requested")

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop + join. Used by FastAPI lifespan on process exit."""
        self.stop()
        t = self._thread
        if t is not None:
            try:
                t.join(timeout=timeout)
            except Exception:
                pass

    def pause(self) -> None:
        """Stop processing the queue without killing the thread. Resume with resume()."""
        self._pause_flag.set()
        with self._lock:
            self._state = "paused"

    def resume(self) -> None:
        self._pause_flag.clear()
        with self._lock:
            if self._thread and self._thread.is_alive():
                self._state = "active" if self._queue else "idle"

    def status(self) -> BackgroundSyncStatus:
        with self._lock:
            cfg = self._cfg
            return BackgroundSyncStatus(
                enabled=self._thread is not None,
                running=self._thread.is_alive() if self._thread else False,
                state=self._state,
                started_at=self._started_at,
                last_loop_at=self._last_loop_at,
                current_code=self._current_code,
                queue_remaining=len(self._queue),
                synced_session=self._synced_session,
                failed_session=self._failed_session,
                backoff_codes=sum(
                    1 for ts in self._backoff_until.values() if ts > time.time()),
                last_full_scan_at=self._last_full_scan_at,
                last_error=self._last_error,
                config={
                    "batch_size": cfg.batch_size,
                    "per_code_sleep_sec": cfg.per_code_sleep_sec,
                    "stale_after_days": cfg.stale_after_days,
                    "idle_interval_sec": cfg.idle_interval_sec,
                    "failure_backoff_sec": cfg.failure_backoff_sec,
                    "max_backoff_sec": cfg.max_backoff_sec,
                },
            )

    # ----------------- financials (once/day) -----------------

    def _maybe_sync_financials(self) -> None:
        """Run the injected financials sync at most once per calendar day. Off
        (no-op) when no financials_fn was provided — e.g. in tests, so the loop
        never makes a network call."""
        if self._financials_fn is None:
            return
        today = self._now().date()
        if self._last_fin_day == today:
            return
        self._last_fin_day = today  # set first → a failure won't retry till tomorrow
        try:
            n = self._financials_fn()
            logger.info("bg-sync financials: %s rows", n)
        except Exception as e:  # noqa: BLE001 - optional; never kills the loop
            logger.warning("bg-sync financials failed: %s", e)

    def _maybe_mine_factors(self) -> None:
        """Run the injected factor mining at most once per calendar day. Off
        (no-op) when no mining_fn was provided — e.g. tests / no LLM key."""
        if self._mining_fn is None:
            return
        today = self._now().date()
        if self._last_mine_day == today:
            return
        self._last_mine_day = today  # set first → a failure won't retry till tomorrow
        try:
            n = self._mining_fn()
            logger.info("bg-sync factor mining: %s factors", n)
        except Exception as e:  # noqa: BLE001 - optional; never kills the loop
            logger.warning("bg-sync factor mining failed: %s", e)

    # ----------------- main loop -----------------

    def _loop(self) -> None:
        """Outer scheduler. Re-scans the DB whenever the queue drains."""
        while not self._stop_flag.is_set():
            self._last_loop_at = datetime.now().isoformat()

            if self._pause_flag.is_set():
                # Spin slowly while paused.
                self._sleep_responsive(5)
                continue

            self._maybe_sync_financials()
            self._maybe_mine_factors()

            # Build the source adapter per loop so a UI source/token change
            # applies without a restart. Misconfig (e.g. tushare, no token — no
            # silent akshare fallback) → record once, idle, retry next loop.
            try:
                adapter = self._adapter_factory()
            except Exception as e:
                with self._lock:
                    self._last_error = f"数据源不可用: {e}"
                    self._state = "idle"
                logger.warning("bg-sync skipped (data source): %s", e)
                self._sleep_responsive(self._cfg.idle_interval_sec)
                continue

            # tushare (and any by-date-capable source): top up the WHOLE market
            # by trading DAY (~3 calls/day), NOT per code (2 calls × thousands)
            # — the per-code path instantly blows tushare's per-endpoint rate
            # limits (adj_factor can be 1/min). This bypasses the per-code queue.
            if hasattr(adapter, "sync_daily_quotes_by_date"):
                self._process_by_date(adapter)
                continue

            if not self._queue:
                # Idle path: rebuild queue from DB scan.
                self._scan_and_enqueue()
                if not self._queue:
                    # Truly nothing to do; sleep long.
                    with self._lock:
                        self._state = "idle"
                    self._sleep_responsive(self._cfg.idle_interval_sec)
                    continue
                with self._lock:
                    self._state = "active"

            # Active path: pending-order codes jump to the front of the queue
            # every batch, so an order queued mid-cycle is synced next instead
            # of waiting for the queue to drain and the next full rescan.
            self._prioritize_pending()
            self._process_batch(adapter)
            self._sleep_responsive(self._cfg.batch_idle_sec)

        with self._lock:
            self._state = "stopped"
            self._current_code = None

    def _sleep_responsive(self, seconds: float) -> None:
        """Sleep that wakes early on stop. Granularity 0.5s."""
        end = time.time() + seconds
        while time.time() < end and not self._stop_flag.is_set():
            time.sleep(min(0.5, max(0, end - time.time())))

    # ----------------- queue building -----------------

    def _scan_and_enqueue(self) -> None:
        """Walk the stocks table, decide which codes need sync. Priority:
        pending-order codes first (a queued order can't fill until its next
        bar lands, so these are the most time-sensitive), then held positions
        (their PnL / stops freeze if stale), then no-data, then stale.

        Result is bounded by `max_queue_size`. Backed-off codes are skipped.
        """
        try:
            # Market-hours-aware freshness: compare against the bar that
            # should exist NOW (today's after the post-close grace, the
            # previous trading day's before it) — not a naive calendar
            # offset. The old `today - stale_after_days` rule meant "yes-
            # terday's bar is always fresh enough", so the day's bars were
            # only pulled the NEXT day, and the agent's evening tick traded
            # on stale closes.
            expected = expected_latest_bar(self._now())
            stale_cutoff = expected - timedelta(
                days=self._cfg.stale_after_days - 1)
            now_ts = time.time()

            # Pending-order codes first — a queued buy/sell is blocked until
            # the next trading bar for that code is on disk, so syncing these
            # ahead of everything else is what actually unblocks fills.
            pending_codes: list[str] = []
            try:
                pend = self._db.list_orders(limit=1000, status="pending") or []
                pending_codes = [o.get("code") for o in pend if o.get("code")]
            except Exception as e:
                logger.warning(f"bg-sync: could not list pending orders: {e}")

            # Held next
            held_codes: list[str] = []
            try:
                positions = self._db.list_positions() or []
                held_codes = [p.get("code") for p in positions if p.get("code")]
            except Exception as e:
                logger.warning(f"bg-sync: could not list positions: {e}")

            # All known stocks
            try:
                stocks = self._db.list_stocks() or []
                all_codes = [s.code for s in stocks]
            except Exception as e:
                logger.warning(f"bg-sync: could not list stocks: {e}")
                all_codes = []

            # Compute "latest quote date" per code in one DB pass if possible.
            latest_map = self._fetch_latest_quote_dates(all_codes)

            def backed_off(code: str) -> bool:
                return self._backoff_until.get(code, 0) > now_ts

            # Backoff applies to pending/held too — without it, a code whose
            # feed has nothing new (halted, or today's bar not out yet) would
            # be re-fetched every single loop instead of every backoff window.
            # They still jump the queue whenever they ARE eligible.
            pending_codes = [c for c in pending_codes if not backed_off(c)]
            held_codes = [c for c in held_codes if not backed_off(c)]

            missing: list[str] = []
            stale: list[str] = []
            for code in all_codes:
                if backed_off(code):
                    continue
                latest = latest_map.get(code)
                if latest is None:
                    missing.append(code)
                elif latest < stale_cutoff:
                    stale.append(code)

            # Deduped, ordered: pending → held → missing → stale.
            ordered: list[str] = []
            seen: set[str] = set()
            for c in pending_codes + held_codes + missing + stale:
                if c and c not in seen:
                    ordered.append(c)
                    seen.add(c)

            ordered = ordered[: self._cfg.max_queue_size]
            with self._lock:
                self._queue = deque(ordered)
                self._last_full_scan_at = datetime.now().isoformat()
            logger.info(
                f"bg-sync scan: {len(all_codes)} known, "
                f"queued {len(ordered)} (pending={len(pending_codes)}, "
                f"held={len(held_codes)}, missing={len(missing)}, "
                f"stale={len(stale)}, expected_bar={expected})"
            )
        except Exception as e:
            logger.exception(f"bg-sync scan failed: {e}")
            self._last_error = f"scan: {e}"

    def _prioritize_pending(self) -> None:
        """Move pending-order codes to the FRONT of the live queue so a
        freshly-queued order is synced next, not after the (possibly huge)
        stale backlog drains. Backed-off codes are skipped — consistent with
        the scan — which also bounds re-syncing: once a pending code syncs
        with no new bar it flat-backs-off and drops out until it's due again.
        Never raises into the loop."""
        try:
            pend = self._db.list_orders(limit=1000, status="pending") or []
        except Exception as e:
            logger.debug("bg-sync: prioritize_pending list_orders failed: %s", e)
            return
        now_ts = time.time()
        codes: list[str] = []
        seen: set[str] = set()
        for o in pend:
            c = o.get("code")
            if c and c not in seen and self._backoff_until.get(c, 0) <= now_ts:
                codes.append(c)
                seen.add(c)
        if not codes:
            return
        with self._lock:
            front = set(codes)
            rest = [c for c in self._queue if c not in front]
            self._queue = deque(codes + rest)

    def _fetch_latest_quote_dates(self, codes: list[str]) -> dict[str, date]:
        """Return {code: max(date)} for codes that have any bars.

        Uses one aggregate query if the Database object exposes it; otherwise
        falls back to per-code lookups (slow but correct).
        """
        if not codes:
            return {}
        # Best path: one query for all codes.
        try:
            rows = self._db.conn.execute(
                "SELECT code, MAX(date) FROM daily_quotes GROUP BY code"
            ).fetchall()
            out: dict[str, date] = {}
            wanted = set(codes)
            for code, max_d in rows:
                if code not in wanted:
                    continue
                if isinstance(max_d, str):
                    try:
                        out[code] = date.fromisoformat(max_d)
                    except ValueError:
                        continue
                elif isinstance(max_d, date):
                    out[code] = max_d
            return out
        except Exception:
            # Fallback per-code.
            out = {}
            for c in codes:
                try:
                    d = self._db.get_latest_quote_date(c)
                    if d is not None:
                        out[c] = d
                except Exception:
                    pass
            return out

    # ----------------- batch processing -----------------

    def _process_batch(self, adapter=None) -> None:
        cfg = self._cfg
        end_d = date.today()
        cold_start = end_d - timedelta(days=cfg.lookback_days)
        if adapter is None:
            # Direct callers (tests) don't prebuild the adapter. Build it here,
            # gracefully: a misconfigured source records the error and skips the
            # batch rather than failing every queued code.
            try:
                adapter = self._adapter_factory()
            except Exception as e:
                with self._lock:
                    self._last_error = f"数据源不可用: {e}"
                logger.warning("bg-sync skipped (data source): %s", e)
                return

        for _ in range(cfg.batch_size):
            if self._stop_flag.is_set() or self._pause_flag.is_set():
                break
            with self._lock:
                if not self._queue:
                    break
                code = self._queue.popleft()
                self._current_code = code

            # Incremental fetch: codes that already have bars pass start=None so
            # the adapter fetches only from their latest bar forward (its built-in
            # incremental path). Only no-data codes pay the bounded cold-start
            # cost. Passing an explicit start=today-365 (as before) defeated the
            # adapter's incrementalism and re-pulled a full year over the network
            # for every code, every loop.
            try:
                latest_before = self._db.get_latest_quote_date(code)
            except Exception:
                latest_before = None
            start_d = None if latest_before is not None else cold_start

            try:
                # repair_gaps=False because the background path values throughput
                # over completeness; user-triggered sync still does gap repair.
                count = adapter.sync_daily_quotes(
                    code, start=start_d, end=end_d, repair_gaps=False)
                try:
                    latest_after = self._db.get_latest_quote_date(code)
                except Exception:
                    latest_after = None
                with self._lock:
                    if count == 0:
                        # No data at all (delisted, feed doesn't carry the
                        # code) — hard failure, exponential backoff.
                        self._failed_session += 1
                        self._apply_backoff(code, hard=True)
                    else:
                        self._synced_session += 1
                        self._fail_counts.pop(code, None)  # streak broken
                        if latest_after == latest_before:
                            # Fetch worked but produced nothing NEW (halted
                            # stock re-serving its old last bar, or today's
                            # bar not published yet). Flat backoff so the
                            # scan doesn't re-queue it every loop.
                            self._apply_backoff(code, hard=False)
                        else:
                            self._backoff_until.pop(code, None)
            except Exception as e:
                logger.warning(f"bg-sync failed for {code}: {e}")
                with self._lock:
                    self._failed_session += 1
                    self._last_error = f"{code}: {e}"
                    self._apply_backoff(code, hard=True)

            # Per-code throttle.
            self._sleep_responsive(cfg.per_code_sleep_sec)

        with self._lock:
            self._current_code = None

    def _process_by_date(self, adapter) -> None:
        """tushare fast-path: top up the WHOLE market by trading DAY (~3 calls/
        day) instead of per code (2 calls × thousands of codes), which instantly
        blows tushare's per-endpoint rate limits (adj_factor can be 1/min). The
        per-code queue is bypassed entirely on this source. Self-manages the
        sleep so the outer loop just `continue`s."""
        cfg = self._cfg
        expected = expected_latest_bar(self._now())
        latest = self._db.get_global_latest_quote_date()

        if latest is None:
            # Empty library — a multi-year bulk load belongs in the explicit,
            # checkpointed `quanti sync --backfill`, not the live loop.
            with self._lock:
                self._state = "idle"
                self._last_error = ("行情库为空:请先运行 `quanti sync --backfill "
                                    "--years 5`(逐日回填,可断点续/限速)")
            logger.warning("bg-sync(by-date): empty DB — run "
                           "`quanti sync --backfill` first")
            self._sleep_responsive(cfg.idle_interval_sec)
            return

        if latest >= expected:
            with self._lock:
                self._state = "idle"
            self._sleep_responsive(cfg.idle_interval_sec)
            return

        # Missing trading days (latest, expected]. Use the synced calendar; fall
        # back to weekdays when it isn't populated.
        start = latest + timedelta(days=1)
        days = self._db.get_trade_dates(start, expected)
        if not days:
            days = [start + timedelta(days=i)
                    for i in range((expected - start).days + 1)
                    if (start + timedelta(days=i)).weekday() < 5]
        if not days:
            with self._lock:
                self._state = "idle"
            self._sleep_responsive(cfg.idle_interval_sec)
            return

        deferred = 0
        if len(days) > cfg.max_topup_days:
            deferred = len(days) - cfg.max_topup_days
            days = days[-cfg.max_topup_days:]  # newest tail — most relevant
            logger.warning("bg-sync(by-date): %d trading days behind — defer "
                           "bulk to `quanti sync --backfill`; topping up newest %d",
                           deferred + cfg.max_topup_days, cfg.max_topup_days)

        with self._lock:
            self._state = "active"
        progressed = failed = False
        seed_state: dict = {}  # carries adj-factor state across this batch's days
        for d in days:
            if self._stop_flag.is_set() or self._pause_flag.is_set():
                break
            with self._lock:
                self._current_code = f"@{d.isoformat()}"
            try:
                rows = adapter.sync_daily_quotes_by_date(d, seed_state=seed_state)
                with self._lock:
                    self._current_code = None
                    if rows > 0:
                        self._synced_session += 1
                        progressed = True
                        self._last_full_scan_at = datetime.now().isoformat()
                    self._last_error = (
                        f"已补 {d};仍约 {deferred} 天待 `quanti sync --backfill`"
                        if deferred else None)
            except Exception as e:
                # Rate-limit / upstream. Stop the batch — the remaining days
                # would hit the same per-minute cap. The next loop (after the
                # idle wait, which exceeds the 1-min window) retries the next
                # day, so we catch up steadily instead of churning + spamming.
                with self._lock:
                    self._current_code = None
                    self._failed_session += 1
                    self._last_error = f"逐日同步 {d} 失败: {e}"
                logger.warning("bg-sync(by-date) %s failed: %s", d, e)
                failed = True
                break
            self._sleep_responsive(cfg.by_date_sleep_sec)

        # On failure, wait out the rate-limit window before retrying. If we made
        # progress with days still pending, loop again soon to keep catching up.
        if failed:
            self._sleep_responsive(cfg.idle_interval_sec)
        elif progressed:
            self._sleep_responsive(cfg.batch_idle_sec)
        else:
            self._sleep_responsive(cfg.idle_interval_sec)

    def _apply_backoff(self, code: str, *, hard: bool) -> None:
        """Schedule the next retry for `code`. Caller holds `_lock`.

        Hard outcomes (exception / zero rows) escalate exponentially per
        consecutive failure, capped at `max_backoff_sec`; soft no-new-data
        outcomes use the flat base window and leave the streak untouched.
        """
        cfg = self._cfg
        if hard:
            n = self._fail_counts.get(code, 0) + 1
            self._fail_counts[code] = n
            delay = min(cfg.failure_backoff_sec * (2 ** (n - 1)),
                        cfg.max_backoff_sec)
        else:
            delay = cfg.failure_backoff_sec
        self._backoff_until[code] = time.time() + delay
