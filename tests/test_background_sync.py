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
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from quanti.data.background_sync import (
    BackgroundQuoteSyncer,
    BackgroundSyncConfig,
    expected_latest_bar,
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

    def test_pending_order_code_leads_queue(self, db_with_stocks):
        """A code with a pending order jumps ahead of even held positions —
        its fill is blocked until its next bar lands, so it's most urgent."""
        # Queue a pending BUY for CCC (not held, stale).
        db_with_stocks.insert_order({
            "order_id": "o_test1", "code": "CCC", "direction": "buy",
            "quantity": 0, "price_type": "market", "status": "pending",
            "strategy_name": "test", "reason": "t",
        })
        adapter = StubAdapter(db_with_stocks)
        syncer = BackgroundQuoteSyncer(
            db=db_with_stocks, adapter_factory=lambda: adapter,
            config=BackgroundSyncConfig(batch_size=10, idle_interval_sec=3600))
        syncer._scan_and_enqueue()
        queued = list(syncer._queue)
        assert queued[0] == "CCC", \
            f"pending-order code should lead, got {queued}"
        # AAA (held) still queued, just after pending.
        assert "AAA" in queued

    def test_prioritize_pending_jumps_live_queue(self, db_with_stocks):
        """A pending order placed mid-cycle (queue already full of stale codes)
        jumps to the FRONT on the next batch — without waiting for a rescan."""
        syncer = BackgroundQuoteSyncer(
            db=db_with_stocks,
            adapter_factory=lambda: StubAdapter(db_with_stocks),
            config=BackgroundSyncConfig())
        # Simulate a live queue mid-drain (stale codes already loaded).
        syncer._queue.extend(["AAA", "BBB", "CCC"])
        # A pending order arrives now.
        db_with_stocks.insert_order({
            "order_id": "o_live", "code": "CCC", "direction": "buy",
            "quantity": 0, "price_type": "market", "status": "pending",
            "strategy_name": "test", "reason": "t",
        })
        syncer._prioritize_pending()
        q = list(syncer._queue)
        assert q[0] == "CCC", f"pending code must jump to front, got {q}"
        # No duplication, the rest preserved.
        assert q.count("CCC") == 1 and set(q) == {"AAA", "BBB", "CCC"}

    def test_prioritize_pending_skips_backed_off(self, db_with_stocks):
        """A pending code in backoff is NOT re-injected (bounds re-sync)."""
        syncer = BackgroundQuoteSyncer(
            db=db_with_stocks,
            adapter_factory=lambda: StubAdapter(db_with_stocks),
            config=BackgroundSyncConfig())
        syncer._queue.extend(["AAA", "BBB"])
        db_with_stocks.insert_order({
            "order_id": "o_bo", "code": "CCC", "direction": "buy",
            "quantity": 0, "price_type": "market", "status": "pending",
            "strategy_name": "test", "reason": "t",
        })
        syncer._backoff_until["CCC"] = time.time() + 3600
        syncer._prioritize_pending()
        assert "CCC" not in list(syncer._queue)

    def test_pending_code_respects_backoff(self, db_with_stocks):
        """Even a pending-order code is skipped while in backoff (e.g. its
        feed had nothing new) — no per-loop spam."""
        db_with_stocks.insert_order({
            "order_id": "o_test2", "code": "BBB", "direction": "buy",
            "quantity": 0, "price_type": "market", "status": "pending",
            "strategy_name": "test", "reason": "t",
        })
        syncer = BackgroundQuoteSyncer(
            db=db_with_stocks,
            adapter_factory=lambda: StubAdapter(db_with_stocks),
            config=BackgroundSyncConfig(failure_backoff_sec=3600))
        syncer._backoff_until["BBB"] = time.time() + 3600
        syncer._scan_and_enqueue()
        assert "BBB" not in list(syncer._queue)

    def test_failure_backoff_skips_code(self, db_with_stocks):
        """A code that throws on sync should be skipped for failure_backoff_sec.
        Verify by injecting a backoff entry directly and confirming the next
        scan does not re-queue it."""
        adapter = StubAdapter(db_with_stocks)
        syncer = BackgroundQuoteSyncer(
            db=db_with_stocks, adapter_factory=lambda: adapter,
            config=BackgroundSyncConfig(failure_backoff_sec=3600))
        syncer._backoff_until["BBB"] = time.time() + 3600
        syncer._scan_and_enqueue()
        queued = list(syncer._queue)
        assert "BBB" not in queued, \
            f"BBB in backoff should be skipped, got {queued}"
        # CCC and AAA still go through
        assert "CCC" in queued

    def test_held_code_respects_backoff(self, db_with_stocks):
        """Held codes jump the queue but must still honor backoff — otherwise
        a held halted stock (nothing new to fetch) is re-fetched every loop."""
        syncer = BackgroundQuoteSyncer(
            db=db_with_stocks,
            adapter_factory=lambda: StubAdapter(db_with_stocks),
            config=BackgroundSyncConfig(failure_backoff_sec=3600))
        syncer._backoff_until["AAA"] = time.time() + 3600  # AAA is held
        syncer._scan_and_enqueue()
        assert "AAA" not in list(syncer._queue)


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
        assert "BBB" in syncer._backoff_until

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
        assert "BBB" in syncer._backoff_until
        assert s.last_error and "api down" in s.last_error

    def test_no_new_bars_backs_off(self, db_with_stocks):
        """A fetch that succeeds but yields nothing newer (halted stock
        re-serving its old last bar) must enter backoff instead of being
        re-queued every loop — this was an infinite ~20s re-sync cycle for
        suspended ST stocks in production."""
        adapter = StubAdapter(db_with_stocks)  # returns 1, writes nothing
        syncer = BackgroundQuoteSyncer(
            db=db_with_stocks, adapter_factory=lambda: adapter,
            config=BackgroundSyncConfig(per_code_sleep_sec=0.0, batch_size=1,
                                        failure_backoff_sec=1800))
        syncer._queue.append("CCC")  # stale, has data
        syncer._process_batch()
        s = syncer.status()
        assert s.synced_session == 1            # the fetch itself succeeded
        assert s.failed_session == 0            # ...and is NOT a failure
        assert "CCC" in syncer._backoff_until   # ...but won't retry at once
        assert "CCC" not in syncer._fail_counts  # no failure streak started
        # The next scan must skip it.
        syncer._scan_and_enqueue()
        assert "CCC" not in list(syncer._queue)

    def test_new_bars_clear_backoff_and_streak(self, db_with_stocks):
        """A sync that lands genuinely new bars resets both the failure
        streak and any standing backoff."""
        class WritingStub(StubAdapter):
            def sync_daily_quotes(self, code, start=None, end=None,
                                  repair_gaps=True):
                today = pd.Timestamp.today().normalize()
                self._db.save_daily_quotes(pd.DataFrame({
                    "code": code, "date": [today.date()],
                    "open": 10, "high": 10, "low": 10, "close": 10,
                    "volume": 1e6, "amount": 1e7, "turnover": 1.0,
                }))
                return super().sync_daily_quotes(code, start, end, repair_gaps)

        adapter = WritingStub(db_with_stocks)
        syncer = BackgroundQuoteSyncer(
            db=db_with_stocks, adapter_factory=lambda: adapter,
            config=BackgroundSyncConfig(per_code_sleep_sec=0.0, batch_size=1))
        syncer._fail_counts["CCC"] = 3
        syncer._backoff_until["CCC"] = time.time() - 1  # expired entry
        syncer._queue.append("CCC")
        syncer._process_batch()
        assert syncer.status().synced_session == 1
        assert "CCC" not in syncer._fail_counts
        assert "CCC" not in syncer._backoff_until

    def test_hard_failure_backoff_is_exponential(self, db_with_stocks):
        """Consecutive hard failures double the backoff window up to the
        cap: base 100s → 200s → 400s(cap) → 400s."""
        adapter = StubAdapter(db_with_stocks,
                              results={"BBB": RuntimeError("x")})
        syncer = BackgroundQuoteSyncer(
            db=db_with_stocks, adapter_factory=lambda: adapter,
            config=BackgroundSyncConfig(per_code_sleep_sec=0.0, batch_size=1,
                                        failure_backoff_sec=100,
                                        max_backoff_sec=400))
        for want in (100, 200, 400, 400):
            syncer._queue.append("BBB")
            syncer._process_batch()
            delay = syncer._backoff_until["BBB"] - time.time()
            assert want - 5 < delay <= want, f"want ~{want}s got {delay:.0f}s"
        assert syncer._fail_counts["BBB"] == 4

    def test_incremental_start_for_existing_data(self, db_with_stocks):
        """Codes with existing bars fetch incrementally (start=None → the
        adapter resumes from their latest bar); only no-data codes pay the
        bounded cold-start (today - lookback_days). Previously every code was
        force-fed start=today-365, re-pulling a full year each loop."""
        adapter = StubAdapter(db_with_stocks)
        syncer = BackgroundQuoteSyncer(
            db=db_with_stocks, adapter_factory=lambda: adapter,
            config=BackgroundSyncConfig(per_code_sleep_sec=0.0, batch_size=3,
                                        batch_idle_sec=0.0, lookback_days=365))
        # CCC: stale-but-has-data, AAA: fresh-has-data, BBB: no data.
        syncer._queue.extend(["CCC", "AAA", "BBB"])
        syncer._process_batch()
        starts = {c: start for c, start, _ in adapter.calls}
        assert starts["CCC"] is None   # has data → incremental
        assert starts["AAA"] is None   # has data → incremental
        assert starts["BBB"] == date.today() - timedelta(days=365)  # cold-start


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


class TestExpectedLatestBar:
    """2026-06-11 is a Thursday; 06-13/14 the weekend; 06-15 Monday."""

    def test_intraday_expects_previous_day(self):
        assert expected_latest_bar(
            datetime(2026, 6, 11, 10, 0)) == date(2026, 6, 10)

    def test_after_close_expects_same_day(self):
        assert expected_latest_bar(
            datetime(2026, 6, 11, 15, 30)) == date(2026, 6, 11)
        assert expected_latest_bar(
            datetime(2026, 6, 11, 19, 52)) == date(2026, 6, 11)

    def test_weekend_rolls_back_to_friday(self):
        assert expected_latest_bar(
            datetime(2026, 6, 13, 12, 0)) == date(2026, 6, 12)
        assert expected_latest_bar(
            datetime(2026, 6, 14, 20, 0)) == date(2026, 6, 12)

    def test_monday_morning_expects_friday(self):
        assert expected_latest_bar(
            datetime(2026, 6, 15, 9, 0)) == date(2026, 6, 12)

    def test_monday_after_close_expects_monday(self):
        assert expected_latest_bar(
            datetime(2026, 6, 15, 16, 0)) == date(2026, 6, 15)


class TestMarketHoursStaleness:
    """Scan freshness must follow the trading clock, not the calendar."""

    def _db_one_stock(self, tmp_path, latest: date) -> Database:
        db = Database(str(tmp_path / "clock.db"))
        db.initialize()
        db.upsert_stock("DDD", "d", "SZ", date(2000, 1, 1), "t")
        db.save_daily_quotes(pd.DataFrame({
            "code": "DDD",
            "date": [latest - timedelta(days=i) for i in range(3)],
            "open": 10, "high": 10, "low": 10, "close": 10,
            "volume": 1e6, "amount": 1e7, "turnover": 1.0,
        }))
        return db

    def _syncer(self, db, now: datetime) -> BackgroundQuoteSyncer:
        return BackgroundQuoteSyncer(
            db=db, adapter_factory=lambda: StubAdapter(db),
            config=BackgroundSyncConfig(), now_fn=lambda: now)

    def test_yesterdays_bar_goes_stale_only_after_close(self, tmp_path):
        """The production complaint: at 19:52 on Thu 06-11 the whole
        universe sat on 06-10 bars and the syncer reported idle (the old
        rule said yesterday's bar is always fresh). After the post-close
        grace, yesterday's bar must count as stale; intraday it must not
        (no pointless churn before today's bar can exist)."""
        db = self._db_one_stock(tmp_path, date(2026, 6, 10))
        s = self._syncer(db, datetime(2026, 6, 11, 19, 52))
        s._scan_and_enqueue()
        assert "DDD" in list(s._queue)

        s2 = self._syncer(db, datetime(2026, 6, 11, 10, 0))
        s2._scan_and_enqueue()
        assert "DDD" not in list(s2._queue)
        db.close()

    def test_friday_bar_fresh_until_monday_close(self, tmp_path):
        db = self._db_one_stock(tmp_path, date(2026, 6, 12))  # Friday
        for now in (datetime(2026, 6, 13, 12, 0),   # Saturday
                    datetime(2026, 6, 15, 9, 0)):   # Monday pre-open
            s = self._syncer(db, now)
            s._scan_and_enqueue()
            assert list(s._queue) == [], f"unexpected queue at {now}"

        s = self._syncer(db, datetime(2026, 6, 15, 16, 0))  # Mon post-close
        s._scan_and_enqueue()
        assert "DDD" in list(s._queue)
        db.close()
