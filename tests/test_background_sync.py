"""Tests for the BackgroundQuoteSyncer daemon.

Network is never hit — we inject a stub adapter that records calls and
returns scripted outcomes. What we want to verify:

  * start/stop is idempotent and survives multiple calls
  * pause/resume gates work without killing the thread
  * scan prioritizes held positions, then no-data, then stale-data
  * per-code failure backoff prevents endless retry of a broken code
  * sync_daily_quotes returning 0 is treated as a failure (for backoff)
  * status() snapshots reflect the latest counters
"""

from __future__ import annotations

import time
from datetime import date

import numpy as np
import pandas as pd
import pytest

from quanti.data.background_sync import (
    BackgroundQuoteSyncer,
    BackgroundSyncConfig,
)
from quanti.data.database import Database


class StubAdapter:
    """Replaces AkShareAdapter in tests. Records every call; returns
    a scripted result keyed by code (default: 1 row saved)."""

    def __init__(self, db, results: dict[str, object] | None = None) -> None:
        self._db = db
        self._results = dict(results or {})
        self.calls: list[tuple[str, date, date]] = []

    def sync_daily_quotes(self, code, start=None, end=None, repair_gaps=True):
        self.calls.append((code, start, end))
        result = self._results.get(code, 1)
        if isinstance(result, Exception):
            raise result
        return int(result)


@pytest.fixture
def db_with_stocks(tmp_path):
    """A DB with 3 stocks and varied quote freshness:
      AAA: held, has fresh data → should NOT be queued
      BBB: not held, no data → should be queued (missing)
      CCC: not held, very stale → should be queued (stale)
    """
    db = Database(str(tmp_path / "bg.db"))
    db.initialize()
    today = pd.Timestamp.today().normalize()

    db.upsert_stock("AAA", "held-fresh", "SZ", date(2000, 1, 1), "test")
    db.upsert_stock("BBB", "no-data", "SZ", date(2000, 1, 1), "test")
    db.upsert_stock("CCC", "stale", "SZ", date(2000, 1, 1), "test")

    # AAA has bars up to today
    df_a = pd.DataFrame({
        "code": "AAA",
        "date": [(today - pd.Timedelta(days=i)).date() for i in range(5)],
        "open": 10, "high": 10, "low": 10, "close": 10,
        "volume": 1e6, "amount": 1e7, "turnover": 1.0,
    })
    db.save_daily_quotes(df_a)

    # CCC has bars only 30 days ago (stale)
    df_c = pd.DataFrame({
        "code": "CCC",
        "date": [(today - pd.Timedelta(days=30 + i)).date() for i in range(5)],
        "open": 10, "high": 10, "low": 10, "close": 10,
        "volume": 1e6, "amount": 1e7, "turnover": 1.0,
    })
    db.save_daily_quotes(df_c)

    # AAA is held (so it should still be prioritized even though fresh — but
    # the freshness check filters it out of the queue regardless).
    db.upsert_position("AAA", 100, 10.0, 10.0, date.today())

    yield db
    db.close()


class TestLifecycle:
    def test_start_stop_idempotent(self, db_with_stocks):
        syncer = BackgroundQuoteSyncer(
            db=db_with_stocks,
            adapter_factory=lambda: StubAdapter(db_with_stocks),
            config=BackgroundSyncConfig(per_code_sleep_sec=0.01,
                                        batch_idle_sec=0.05,
                                        idle_interval_sec=3600))
        syncer.start()
        syncer.start()  # should be no-op, not double-start
        assert syncer.status().running
        syncer.stop()
        time.sleep(0.5)
        syncer.shutdown(timeout=1.0)
        # Repeated stop/shutdown safe
        syncer.shutdown(timeout=1.0)

    def test_pause_resume(self, db_with_stocks):
        adapter = StubAdapter(db_with_stocks)
        syncer = BackgroundQuoteSyncer(
            db=db_with_stocks, adapter_factory=lambda: adapter,
            config=BackgroundSyncConfig(per_code_sleep_sec=0.01,
                                        batch_idle_sec=0.02,
                                        idle_interval_sec=3600))
        syncer.start()
        time.sleep(0.3)
        syncer.pause()
        time.sleep(0.2)
        assert syncer.status().state == "paused"
        # Calls should plateau while paused.
        before = len(adapter.calls)
        time.sleep(0.4)
        after_paused = len(adapter.calls)
        # Allow up to 2 calls slipping through (race between pause set and
        # the per-batch loop checking it).
        assert after_paused - before <= 2
        syncer.resume()
        syncer.shutdown(timeout=2.0)


class TestQueueBuilding:
    def test_scan_queues_held_missing_stale(self, db_with_stocks):
        """Held-fresh stocks are still queued as #1 priority but the
        no-data one (BBB) and stale one (CCC) both appear in the queue.
        The exact behavior:
          - AAA is held and fresh → priority spot regardless
          - BBB has no bars → "missing" bucket
          - CCC is stale → "stale" bucket
        """
        adapter = StubAdapter(db_with_stocks)
        syncer = BackgroundQuoteSyncer(
            db=db_with_stocks, adapter_factory=lambda: adapter,
            config=BackgroundSyncConfig(per_code_sleep_sec=0.01,
                                        batch_size=10,
                                        batch_idle_sec=0.02,
                                        idle_interval_sec=3600))
        # Direct call to _scan_and_enqueue avoids racing with the loop.
        syncer._scan_and_enqueue()
        queued = list(syncer._queue)
        # BBB (missing) and CCC (stale) must both be queued; AAA is held +
        # fresh so it gets priority position 0 even though it's not strictly
        # needed (this is intentional: we err on the side of refreshing
        # what the user is exposed to).
        assert "BBB" in queued
        assert "CCC" in queued
        assert queued[0] == "AAA", \
            f"held position should come first, got {queued}"

    def test_failure_backoff_skips_code(self, db_with_stocks):
        """A code that throws on sync should be skipped for failure_backoff_sec.
        Verify by injecting a backoff entry directly and confirming the next
        scan does not re-queue it."""
        adapter = StubAdapter(db_with_stocks)
        syncer = BackgroundQuoteSyncer(
            db=db_with_stocks, adapter_factory=lambda: adapter,
            config=BackgroundSyncConfig(failure_backoff_sec=3600))
        syncer._failed_backoff["BBB"] = time.time() + 3600
        syncer._scan_and_enqueue()
        queued = list(syncer._queue)
        assert "BBB" not in queued, \
            f"BBB in backoff should be skipped, got {queued}"
        # CCC and AAA still go through
        assert "CCC" in queued


class TestBatchProcessing:
    def test_batch_calls_adapter(self, db_with_stocks):
        adapter = StubAdapter(db_with_stocks)
        syncer = BackgroundQuoteSyncer(
            db=db_with_stocks, adapter_factory=lambda: adapter,
            config=BackgroundSyncConfig(per_code_sleep_sec=0.01,
                                        batch_size=2,
                                        batch_idle_sec=0.02))
        syncer._queue.extend(["BBB", "CCC"])
        syncer._process_batch()
        # Both should have been synced once
        codes_called = [c for c, _, _ in adapter.calls]
        assert codes_called == ["BBB", "CCC"]
        s = syncer.status()
        assert s.synced_session == 2
        assert s.failed_session == 0

    def test_zero_rows_counts_as_failure(self, db_with_stocks):
        """A code that returns 0 (delisted, no data on feed) should be
        backed off, not retried every cycle."""
        adapter = StubAdapter(db_with_stocks, results={"BBB": 0})
        syncer = BackgroundQuoteSyncer(
            db=db_with_stocks, adapter_factory=lambda: adapter,
            config=BackgroundSyncConfig(per_code_sleep_sec=0.01,
                                        batch_size=2,
                                        batch_idle_sec=0.02,
                                        failure_backoff_sec=3600))
        syncer._queue.append("BBB")
        syncer._process_batch()
        s = syncer.status()
        assert s.synced_session == 0
        assert s.failed_session == 1
        # And the code is in backoff.
        assert "BBB" in syncer._failed_backoff

    def test_exception_counts_as_failure(self, db_with_stocks):
        adapter = StubAdapter(db_with_stocks,
                              results={"BBB": RuntimeError("api down")})
        syncer = BackgroundQuoteSyncer(
            db=db_with_stocks, adapter_factory=lambda: adapter,
            config=BackgroundSyncConfig(per_code_sleep_sec=0.01,
                                        batch_size=1,
                                        failure_backoff_sec=3600))
        syncer._queue.append("BBB")
        syncer._process_batch()
        s = syncer.status()
        assert s.failed_session == 1
        assert "BBB" in syncer._failed_backoff
        assert s.last_error and "api down" in s.last_error


class TestStatusSnapshot:
    def test_status_reflects_state_and_counters(self, db_with_stocks):
        adapter = StubAdapter(db_with_stocks)
        syncer = BackgroundQuoteSyncer(
            db=db_with_stocks, adapter_factory=lambda: adapter,
            config=BackgroundSyncConfig(per_code_sleep_sec=0.01,
                                        batch_size=10,
                                        idle_interval_sec=3600))
        # Before start
        s = syncer.status()
        assert not s.running
        assert s.state == "stopped"

        syncer.start()
        time.sleep(0.5)
        s = syncer.status()
        assert s.running
        # State will be active or idle depending on whether the scan
        # completed before we read; both are valid.
        assert s.state in ("active", "idle")
        syncer.shutdown(timeout=2.0)
