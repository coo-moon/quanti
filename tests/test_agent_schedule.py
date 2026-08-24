"""Tests for the agent's optional daily-run schedule.

When goal.params['daily_run_time'] = 'HH:MM' is set, the agent loop waits until
that clock time each day instead of ticking on the fixed interval.
"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime

from quanti.agent.goal import load_goal, save_goal
from quanti.agent.runtime import (
    AgentRuntime,
    _parse_hhmm,
    _seconds_until_daily,
)
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.paper_broker import PaperBroker


def test_parse_hhmm_valid():
    assert _parse_hhmm("17:30") == (17, 30)
    assert _parse_hhmm("09:05") == (9, 5)
    assert _parse_hhmm(" 0:0 ") == (0, 0)
    assert _parse_hhmm("23:59") == (23, 59)


def test_parse_hhmm_invalid():
    for bad in ("", "abc", "25:00", "12:60", "1730", "12", "12:30:00", None, 1730):
        assert _parse_hhmm(bad) is None


def test_seconds_until_daily_later_today():
    now = datetime(2026, 6, 22, 9, 0, 0)
    assert _seconds_until_daily(now, 17, 30) == (17 * 3600 + 30 * 60) - (9 * 3600)


def test_seconds_until_daily_rolls_to_tomorrow():
    now = datetime(2026, 6, 22, 18, 0, 0)
    assert _seconds_until_daily(now, 17, 30) == 24 * 3600 - 30 * 60


def test_seconds_until_daily_exactly_now_rolls():
    now = datetime(2026, 6, 22, 17, 30, 0)
    assert _seconds_until_daily(now, 17, 30) == 24 * 3600  # target <= now → +1d


def _runtime(tmp_path):
    db = Database(str(tmp_path / "sched.db"))
    db.initialize()
    db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
    provider = DataProvider(db)
    broker = PaperBroker(db, provider, initial_cash=100_000)
    return db, AgentRuntime(db, provider, broker, tick_interval_sec=14400)


def test_interval_mode_when_no_daily_time(tmp_path):
    db, rt = _runtime(tmp_path)
    assert rt._daily_run_time() is None
    assert rt._next_wait_seconds() == 14400  # falls back to the tick interval


def test_daily_mode_when_goal_configured(tmp_path):
    db, rt = _runtime(tmp_path)
    goal = load_goal(db)
    goal.params = {**(goal.params or {}), "daily_run_time": "17:30"}
    save_goal(db, goal)

    assert rt._daily_run_time() == (17, 30)
    wait = rt._next_wait_seconds()
    assert 0 < wait <= 24 * 3600  # scheduled, not the 4h interval


def test_bad_daily_time_falls_back_to_interval(tmp_path):
    db, rt = _runtime(tmp_path)
    goal = load_goal(db)
    goal.params = {**(goal.params or {}), "daily_run_time": "nonsense"}
    save_goal(db, goal)
    assert rt._daily_run_time() is None
    assert rt._next_wait_seconds() == 14400


def test_daily_runs_only_on_trading_days(tmp_path, monkeypatch):
    import quanti.utils.market as mkt
    db, rt = _runtime(tmp_path)
    goal = load_goal(db)
    goal.params = {**(goal.params or {}), "daily_run_time": "17:30"}
    save_goal(db, goal)

    monkeypatch.setattr(mkt, "is_trading_day", lambda d, p=None: True)
    assert rt._daily_runs_today() is True
    monkeypatch.setattr(mkt, "is_trading_day", lambda d, p=None: False)
    assert rt._daily_runs_today() is False  # non-trading day → skipped

    # Opt out of the gate → runs every day regardless of the calendar.
    goal.params = {**goal.params, "daily_trading_days_only": False}
    save_goal(db, goal)
    assert rt._daily_runs_today() is True


def test_restart_spawns_new_thread_and_keeps_enabled(tmp_path):
    db, rt = _runtime(tmp_path)
    # Daily mode → start() 不会立即跑一轮 cycle（避免触网/耗时）。
    goal = load_goal(db)
    goal.params = {**(goal.params or {}), "daily_run_time": "23:59"}
    save_goal(db, goal)
    rt.start()
    try:
        assert rt.status().running is True
        first_thread = rt._thread
        rt.restart()
        assert rt.status().running is True
        assert rt._thread is not first_thread        # 换了新线程
        assert load_goal(db).enabled is True         # enabled 被保留
    finally:
        rt.stop()


def test_restart_when_not_running_behaves_like_start(tmp_path):
    db, rt = _runtime(tmp_path)
    goal = load_goal(db)
    goal.params = {**(goal.params or {}), "daily_run_time": "23:59"}
    save_goal(db, goal)
    rt.restart()  # 从未 start 过 → 等价于 start()
    try:
        assert rt.status().running is True
    finally:
        rt.stop()


class TestWaitUntilDueSleepDrift:
    """睡眠漂移免疫等待(2026-08-24 实发:跨周末合盖,周一 12:30 tick 丢失)。"""

    def _agent(self, tmp_path):
        from quanti.data.database import Database
        from quanti.data.provider import DataProvider
        from quanti.execution.paper_broker import PaperBroker
        db = Database(str(tmp_path / "w.db"))
        db.initialize()
        provider = DataProvider(db)
        return AgentRuntime(db, provider, PaperBroker(db, provider),
                            strategies_dir="strategies",
                            screeners_dir="screeners"), db

    def test_due_or_overslept_returns_immediately(self, tmp_path):
        agent, db = self._agent(tmp_path)
        agent._next_wait_seconds = lambda: 0.0  # 已到点/睡过头
        t0 = time.monotonic()
        assert agent._wait_until_due() is False  # False = 该跑了
        assert time.monotonic() - t0 < 0.5
        db.close()

    def test_slices_and_fires_on_wall_clock(self, tmp_path):
        agent, db = self._agent(tmp_path)
        agent._WAIT_SLICE_SEC = 0.05
        agent._next_wait_seconds = lambda: 0.2
        t0 = time.monotonic()
        assert agent._wait_until_due() is False
        assert 0.1 < time.monotonic() - t0 < 2.0  # 多片后按墙钟到点
        db.close()

    def test_stop_flag_interrupts_wait(self, tmp_path):
        agent, db = self._agent(tmp_path)
        agent._WAIT_SLICE_SEC = 0.05
        agent._next_wait_seconds = lambda: 30.0
        threading.Timer(0.1, agent._stop_flag.set).start()
        t0 = time.monotonic()
        assert agent._wait_until_due() is True  # True = 退出循环
        assert time.monotonic() - t0 < 5.0
        db.close()
