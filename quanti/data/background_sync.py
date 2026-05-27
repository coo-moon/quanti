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
  * Scan priority: held positions > codes with no bars > codes with stale bars.
  * Per-code failure backoff: after one error, skip that code for
    `failure_backoff_sec` (default 30 min) so a persistently-broken endpoint
    doesn't burn through the entire universe.
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
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


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
    """A code is "stale" if its latest bar date is older than this many days.
    1 = bars from before yesterday trigger a refresh."""

    failure_backoff_sec: int = 30 * 60
    """After a failed sync, skip this code for this long. Prevents one bad
    code (delisted, removed from feed) from being retried every loop."""

    lookback_days: int = 365
    """How much history to request when filling in a missing code. The
    underlying adapter is incremental for codes that already have data,
    so this is only the cold-start cost."""

    max_queue_size: int = 6000
    """Safety valve: cap queue size so a buggy scan can't OOM."""


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
    ) -> None:
        self._db = db
        self._cfg = config or BackgroundSyncConfig()
        self._adapter_factory = adapter_factory or self._default_adapter_factory

        self._thread: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self._pause_flag = threading.Event()
        self._lock = threading.Lock()

        # Mutable status fields. Read concurrently via status(); we accept
        # mildly torn reads since the dashboard refreshes anyway.
        self._state: str = "stopped"
        self._current_code: str | None = None
        self._queue: deque[str] = deque()
        self._failed_backoff: dict[str, float] = {}  # code → unix ts to retry
        self._synced_session = 0
        self._failed_session = 0
        self._started_at: str | None = None
        self._last_loop_at: str | None = None
        self._last_full_scan_at: str | None = None
        self._last_error: str | None = None

    # ----------------- adapter factory -----------------

    def _default_adapter_factory(self):
        # Lazy import: keeps this module loadable without akshare installed
        # (useful for tests).
        from quanti.data.akshare_adapter import AkShareAdapter
        return AkShareAdapter(self._db)

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
                last_full_scan_at=self._last_full_scan_at,
                last_error=self._last_error,
                config={
                    "batch_size": cfg.batch_size,
                    "per_code_sleep_sec": cfg.per_code_sleep_sec,
                    "stale_after_days": cfg.stale_after_days,
                    "idle_interval_sec": cfg.idle_interval_sec,
                    "failure_backoff_sec": cfg.failure_backoff_sec,
                },
            )

    # ----------------- main loop -----------------

    def _loop(self) -> None:
        """Outer scheduler. Re-scans the DB whenever the queue drains."""
        while not self._stop_flag.is_set():
            self._last_loop_at = datetime.now().isoformat()

            if self._pause_flag.is_set():
                # Spin slowly while paused.
                self._sleep_responsive(5)
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

            # Active path: drain a batch.
            self._process_batch()
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
        held positions first (because their PnL freezes if stale), then
        codes with no data at all, then codes with stale data.

        Result is bounded by `max_queue_size`. Backed-off codes are skipped.
        """
        try:
            today = date.today()
            stale_cutoff = today - timedelta(days=self._cfg.stale_after_days)
            now_ts = time.time()

            # Held first
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

            missing: list[str] = []
            stale: list[str] = []
            for code in all_codes:
                # Failure backoff
                retry_at = self._failed_backoff.get(code, 0)
                if retry_at > now_ts:
                    continue
                latest = latest_map.get(code)
                if latest is None:
                    missing.append(code)
                elif latest < stale_cutoff:
                    stale.append(code)

            # Deduped, ordered: held → missing → stale.
            ordered: list[str] = []
            seen: set[str] = set()
            for c in held_codes + missing + stale:
                if c and c not in seen:
                    ordered.append(c)
                    seen.add(c)

            ordered = ordered[: self._cfg.max_queue_size]
            with self._lock:
                self._queue = deque(ordered)
                self._last_full_scan_at = datetime.now().isoformat()
            logger.info(
                f"bg-sync scan: {len(all_codes)} known, "
                f"queued {len(ordered)} (held={len(held_codes)}, "
                f"missing={len(missing)}, stale={len(stale)})"
            )
        except Exception as e:
            logger.exception(f"bg-sync scan failed: {e}")
            self._last_error = f"scan: {e}"

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

    def _process_batch(self) -> None:
        cfg = self._cfg
        adapter = None
        end_d = date.today()
        start_d = end_d - timedelta(days=cfg.lookback_days)

        for _ in range(cfg.batch_size):
            if self._stop_flag.is_set() or self._pause_flag.is_set():
                break
            with self._lock:
                if not self._queue:
                    break
                code = self._queue.popleft()
                self._current_code = code

            try:
                if adapter is None:
                    adapter = self._adapter_factory()
                # repair_gaps=False because the background path values throughput
                # over completeness; user-triggered sync still does gap repair.
                count = adapter.sync_daily_quotes(
                    code, start=start_d, end=end_d, repair_gaps=False)
                with self._lock:
                    if count == 0:
                        # No data is a soft failure (delisted? halted?) —
                        # treat as failure for backoff purposes so we don't
                        # spam-retry the same dead code.
                        self._failed_session += 1
                        self._failed_backoff[code] = (
                            time.time() + cfg.failure_backoff_sec)
                    else:
                        self._synced_session += 1
            except Exception as e:
                logger.warning(f"bg-sync failed for {code}: {e}")
                with self._lock:
                    self._failed_session += 1
                    self._last_error = f"{code}: {e}"
                    self._failed_backoff[code] = (
                        time.time() + cfg.failure_backoff_sec)

            # Per-code throttle.
            self._sleep_responsive(cfg.per_code_sleep_sec)

        with self._lock:
            self._current_code = None
