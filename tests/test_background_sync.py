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


class ByDateStubAdapter:
    """tushare-like adapter exposing the by-date sweep. The daemon must route to
    this (whole-market-per-day) instead of per code on such sources."""

    def __init__(self, db, fail_after: int | None = None, rows: int = 4000) -> None:
        self._db = db
        self.by_date_calls: list[date] = []
        self._fail_after = fail_after  # raise on the Nth call (1-based)
        self._rows = rows

    def sync_daily_quotes_by_date(self, d: date, seed_state=None) -> int:
        self.by_date_calls.append(d)
        if self._fail_after and len(self.by_date_calls) >= self._fail_after:
            raise RuntimeError("抱歉,您访问接口(adj_factor)频率超限(1次/分钟)")
        return self._rows


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

    def test_adapter_factory_failure_skips_batch_cleanly(self, db_with_stocks):
        """Source unbuildable (e.g. tushare, no token — no silent akshare
        fallback): the batch aborts once with last_error set, queued codes are
        left untouched, and NO per-code failure is counted for every stock."""
        from quanti.data.source import DataSourceUnavailable

        def _boom():
            raise DataSourceUnavailable("未配置 token")

        syncer = BackgroundQuoteSyncer(
            db=db_with_stocks, adapter_factory=_boom,
            config=BackgroundSyncConfig(per_code_sleep_sec=0.0, batch_size=3))
        syncer._queue.extend(["BBB", "CCC"])
        syncer._process_batch()
        s = syncer.status()
        assert s.failed_session == 0                  # not a per-code failure
        assert s.last_error and "数据源" in s.last_error
        assert "BBB" in list(syncer._queue)           # nothing dropped
        assert "CCC" in list(syncer._queue)

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


class TestByDateFastPath:
    """tushare's per-endpoint rate limits (adj_factor as low as 1/min) make the
    per-code path catastrophic. On a by-date-capable source the daemon must top
    up the WHOLE market by trading DAY instead — a few calls/day, queue bypassed."""

    def _db_at(self, tmp_path, latest):
        db = Database(str(tmp_path / "bd.db"))
        db.initialize()
        if latest is not None:
            db.save_daily_quotes(pd.DataFrame([{
                "code": "AAA", "date": latest, "open": 1.0, "high": 1.0,
                "low": 1.0, "close": 1.0, "volume": 1.0, "amount": 1.0,
                "turnover": 0.0}]))
        return db

    def test_routes_by_date_for_missing_days(self, tmp_path):
        clock = datetime(2024, 6, 12, 16, 0)        # Wed, post-close → exp 06-12
        db = self._db_at(tmp_path, date(2024, 6, 7))  # Fri → 3 missing trade days
        adapter = ByDateStubAdapter(db)
        syncer = BackgroundQuoteSyncer(
            db=db, adapter_factory=lambda: adapter, now_fn=lambda: clock,
            config=BackgroundSyncConfig(by_date_sleep_sec=0.0, batch_idle_sec=0.0))
        syncer._process_by_date(adapter)
        assert adapter.by_date_calls == [
            date(2024, 6, 10), date(2024, 6, 11), date(2024, 6, 12)]
        assert syncer.status().synced_session == 3
        db.close()

    def test_empty_db_defers_to_backfill(self, tmp_path):
        db = self._db_at(tmp_path, None)            # empty library
        adapter = ByDateStubAdapter(db)
        syncer = BackgroundQuoteSyncer(
            db=db, adapter_factory=lambda: adapter,
            config=BackgroundSyncConfig(idle_interval_sec=0))
        syncer._process_by_date(adapter)
        assert adapter.by_date_calls == []          # no hammering on empty DB
        assert "backfill" in (syncer.status().last_error or "")
        db.close()

    def test_rate_limit_stops_batch_no_churn(self, tmp_path):
        clock = datetime(2024, 6, 12, 16, 0)
        db = self._db_at(tmp_path, date(2024, 6, 7))   # 3 missing days
        adapter = ByDateStubAdapter(db, fail_after=1)  # first day rate-limited
        syncer = BackgroundQuoteSyncer(
            db=db, adapter_factory=lambda: adapter, now_fn=lambda: clock,
            config=BackgroundSyncConfig(by_date_sleep_sec=0.0, idle_interval_sec=0))
        syncer._process_by_date(adapter)
        assert len(adapter.by_date_calls) == 1      # stops, doesn't churn 2 & 3
        assert syncer.status().failed_session == 1
        assert "失败" in (syncer.status().last_error or "")
        db.close()

    def test_per_code_fresh_bars_do_not_mask_market_staleness(self, tmp_path):
        """Regression (2026-07-02): manual /api/sync/quotes and the agent's
        _ensure_recent_data wrote TODAY's bar for 1-2 codes, pushing the
        global MAX(date) to `expected` — the by-date top-up then idled
        forever while 5000+ codes sat on yesterday's bar. Freshness must
        follow the market-wide latest, not a stray per-code top-up."""
        clock = datetime(2024, 6, 12, 16, 0)      # Wed post-close → exp 06-12
        db = Database(str(tmp_path / "mask.db"))
        db.initialize()
        codes = [f"{i:06d}" for i in range(20)]
        db.save_daily_quotes(pd.DataFrame({       # whole market at Tue 06-11
            "code": codes, "date": date(2024, 6, 11),
            "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
            "volume": 1.0, "amount": 1.0, "turnover": 0.0}))
        db.save_daily_quotes(pd.DataFrame({       # 2 codes topped up per-code
            "code": codes[:2], "date": date(2024, 6, 12),
            "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
            "volume": 1.0, "amount": 1.0, "turnover": 0.0}))
        adapter = ByDateStubAdapter(db)
        syncer = BackgroundQuoteSyncer(
            db=db, adapter_factory=lambda: adapter, now_fn=lambda: clock,
            config=BackgroundSyncConfig(by_date_sleep_sec=0.0,
                                        batch_idle_sec=0.0,
                                        idle_interval_sec=0))
        syncer._process_by_date(adapter)
        # Must top up 06-12 for the whole market (and only 06-12 — the
        # market-latest 06-11 is already complete).
        assert adapter.by_date_calls == [date(2024, 6, 12)]
        db.close()

    def test_far_behind_caps_to_max_topup(self, tmp_path):
        clock = datetime(2024, 6, 28, 16, 0)         # Fri
        db = self._db_at(tmp_path, date(2024, 1, 2))  # ~6 months behind
        adapter = ByDateStubAdapter(db)
        syncer = BackgroundQuoteSyncer(
            db=db, adapter_factory=lambda: adapter, now_fn=lambda: clock,
            config=BackgroundSyncConfig(max_topup_days=5, by_date_sleep_sec=0.0,
                                        batch_idle_sec=0.0))
        syncer._process_by_date(adapter)
        assert len(adapter.by_date_calls) == 5       # capped, bulk deferred
        assert max(adapter.by_date_calls) == date(2024, 6, 28)  # newest tail
        db.close()


def test_financials_fn_runs_once_per_day(db_with_stocks):
    """The daemon refreshes financials at most once per calendar day, and is a
    no-op when no financials_fn is injected (tests/CI never hit the network)."""
    calls = {"n": 0}

    def _fin():
        calls["n"] += 1
        return 7

    s = BackgroundQuoteSyncer(
        db=db_with_stocks, adapter_factory=lambda: None,
        financials_fn=_fin, now_fn=lambda: datetime(2026, 6, 24, 16, 0))
    s._maybe_sync_financials()   # first call today → runs
    s._maybe_sync_financials()   # same day → skipped
    assert calls["n"] == 1
    # next calendar day → runs again
    s._now = lambda: datetime(2026, 6, 25, 16, 0)
    s._maybe_sync_financials()
    assert calls["n"] == 2

    # default (no financials_fn) is a silent no-op — never raises, never calls
    off = BackgroundQuoteSyncer(
        db=db_with_stocks, adapter_factory=lambda: None,
        now_fn=lambda: datetime(2026, 6, 24, 16, 0))
    off._maybe_sync_financials()  # would raise if it tried anything


class TestHeavyWarmupGate:
    """The heavy daily hooks (doctor / strategy gate / factor mining) must
    defer for heavy_warmup_sec after boot so they never pile onto the cold
    first-tick selector sweep (2026-08-14 rounds 8-9 lock-convoy diagnosis)."""

    def _syncer(self, db, clock, warmup):
        calls = {"doctor": 0, "gate": 0, "mine": 0}

        def doctor_fn():
            calls["doctor"] += 1
            return {"ok": True}

        def gate_fn():
            calls["gate"] += 1
            return {}

        def mine_fn():
            calls["mine"] += 1
            return 0

        syncer = BackgroundQuoteSyncer(
            db=db, now_fn=lambda: clock["now"],
            doctor_fn=doctor_fn, strategy_gate_fn=gate_fn, mining_fn=mine_fn,
            heavy_warmup_sec=warmup)
        return syncer, calls

    def test_heavy_hooks_defer_until_warmup(self, tmp_path):
        from datetime import datetime, timedelta

        db = Database(str(tmp_path / "w.db"))
        db.initialize()
        clock = {"now": datetime(2026, 6, 24, 18, 0)}  # after DOCTOR_RUN_AT
        syncer, calls = self._syncer(db, clock, warmup=1800.0)
        syncer._maybe_run_doctor()
        syncer._maybe_run_strategy_gate()
        syncer._maybe_mine_factors()
        assert calls == {"doctor": 0, "gate": 0, "mine": 0}  # all deferred

        clock["now"] += timedelta(seconds=1801)
        syncer._maybe_run_doctor()
        syncer._maybe_run_strategy_gate()
        syncer._maybe_mine_factors()
        assert calls == {"doctor": 1, "gate": 1, "mine": 1}
        # Day latches: a second call the same day does not re-run.
        syncer._maybe_run_doctor()
        assert calls["doctor"] == 1
        db.close()

    def test_warmup_zero_runs_immediately(self, tmp_path):
        from datetime import datetime

        db = Database(str(tmp_path / "w2.db"))
        db.initialize()
        clock = {"now": datetime(2026, 6, 24, 18, 0)}
        syncer, calls = self._syncer(db, clock, warmup=0.0)
        syncer._maybe_run_doctor()
        assert calls["doctor"] == 1
        db.close()



    def test_syncer_loop_yields_during_warmup(self, tmp_path):
        """The whole syncer (financials + queue + regime) must yield to the
        agent cold tick during warm-up — the sweep alone takes ~90s in a
        quiet process but 19+ min under boot coexistence (2026-08-14)."""
        from datetime import datetime, timedelta

        db = Database(str(tmp_path / "w3.db"))
        db.initialize()
        clock = {"now": datetime(2026, 6, 24, 18, 0)}
        calls = {"fin": 0, "sync": 0}

        class _Adapter:
            def sync_daily_quotes_by_date(self, d, seed_state=None):
                calls["sync"] += 1
                return 0

            def sync_daily_quotes(self, code, start=None, end=None,
                                  repair_gaps=False):
                calls["sync"] += 1
                return 0

        def fin():
            calls["fin"] += 1
            return 0

        cfg = BackgroundSyncConfig()
        syncer = BackgroundQuoteSyncer(
            db=db, adapter_factory=lambda: _Adapter(),
            config=cfg, now_fn=lambda: clock["now"],
            financials_fn=fin, heavy_warmup_sec=30.0)
        syncer.start()
        import time
        time.sleep(2.0)  # several loop iterations inside the warm-up window
        assert calls == {"fin": 0, "sync": 0}
        assert syncer.status().state == "warming"
        # Advance past warm-up: the next loop pass must fire the daily hooks.
        clock["now"] += timedelta(seconds=31)
        for _ in range(30):
            if calls["fin"] >= 1:
                break
            time.sleep(0.5)
        assert calls["fin"] >= 1
        syncer.stop()
        syncer.shutdown(timeout=3)
        db.close()

