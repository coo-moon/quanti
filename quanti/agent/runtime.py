"""AgentRuntime: the long-running self-maintaining loop.

A single tick is:
  1. Pull the latest goal from DB.
  2. Resolve the universe (pool name → list of codes, or all codes).
  3. Make sure today's bars are fresh; auto-sync any code missing data.
  4. Run the optional Screener to narrow the candidate list.
  5. Either pick the user-pinned strategy or let StrategySelector choose
     the one closest to the goal.
  6. Walk the candidates through the strategy, collect Signals.
  7. Pass signals through PaperBroker (which enforces RiskManager rules and
     writes orders/trades/positions).
  8. Run a stop-loss sweep and snapshot the portfolio.
  9. Append a decision-log entry summarizing the cycle.

The loop is driven by a daemon thread so the FastAPI process can host it
without blocking. Each tick is wrapped in a wide try/except — one bad day
should never kill the agent.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from quanti.agent.goal import Goal, load_goal, save_goal
from quanti.agent.params import resolve_params
from quanti.agent.selector import StrategySelector
from quanti.agent.signal_pipeline import (
    FusedCandidate,
    collect_signals_per_strategy,
    filter_by_threshold,
    fuse_buy_signals,
    industry_cap,
    select_rotation_sells,
    trending_holdings,
)
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.base import Broker
from quanti.factors.cross_sectional import FactorConfig, compute_factor_panel
from quanti.models import Direction, Signal
from quanti.screener.loader import ScreenerLoader
from quanti.strategy.loader import StrategyLoader

logger = logging.getLogger(__name__)


def _parse_hhmm(value) -> tuple[int, int] | None:
    """Parse a 'HH:MM' 24-hour string → (hour, minute), or None if absent or
    malformed. Used for the optional daily-run schedule."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return (h, m) if 0 <= h <= 23 and 0 <= m <= 59 else None


def _seconds_until_daily(now: datetime, hour: int, minute: int) -> float:
    """Seconds from `now` until the next occurrence of hour:minute — today if
    it's still ahead, otherwise tomorrow."""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


class CycleHalted(Exception):
    """The portfolio circuit breaker tripped (intraday guard) while this tick
    was still computing. Raised by `AgentRuntime._broker_exec` so the tick
    aborts instead of buying back into a book that was just flattened."""


@dataclass
class AgentStatus:
    enabled: bool = False
    running: bool = False
    started_at: Optional[str] = None  # when the current loop (re)started
    tick_interval_sec: int = 0        # cadence, so the UI can show next-tick
    last_tick_at: Optional[str] = None
    last_tick_summary: str = ""
    last_strategy: str = ""
    last_evaluations: list[dict] = field(default_factory=list)
    total_value: float = 0.0
    pnl_pct: float = 0.0
    degraded_exits: list[dict] = field(default_factory=list)
    """Holdings whose entry_strategy is no longer loadable — their strategy
    exit silently degraded to stop-loss/TP only. Computed lazily (see
    `_degraded_exits_cached`) so the status endpoint stays cheap."""


class AgentRuntime:
    """Singleton agent loop. One instance per process (lives on app.state)."""

    def __init__(
        self,
        db: Database,
        provider: DataProvider,
        broker: Broker,
        strategies_dir: str | Path = "strategies",
        screeners_dir: str | Path = "screeners",
        tick_interval_sec: int = 60 * 60 * 4,  # 4h between cycles by default
        decision_retention_days: int = 90,
        selector_reselect_interval_sec: int = 60 * 60 * 24,  # 24h
        intraday_guard_sec: int = 0,  # >0: fast live fills/exits guard cadence
    ) -> None:
        self._db = db
        self._provider = provider
        self._broker = broker
        self._strategies_dir = str(strategies_dir)
        self._screeners_dir = str(screeners_dir)
        self._tick_interval = tick_interval_sec
        self._decision_retention_days = decision_retention_days
        self._reselect_interval = selector_reselect_interval_sec
        self._intraday_guard_sec = intraday_guard_sec
        # Selector cache: (timestamp, strategy_name, evaluations). The
        # Selector runs at most once per `_reselect_interval`. In between we
        # reuse the cached pick — saves running a 6-strategy backtest sweep
        # on every 4h tick (~9× faster ticks once cache is warm).
        self._selector_cache: tuple[float, str, list[dict]] | None = None
        # Ensemble/LLM top-K selection cache: (timestamp, [(name, weight)],
        # evaluations). Same purpose and cadence as _selector_cache but for the
        # fused-candidate path — pick_topk runs a per-strategy walk-forward
        # sweep that, spanning the full history, is far too heavy to repeat on
        # every tick. The per-tick signal fusion downstream still runs fresh.
        self._topk_cache: tuple[float, list[tuple[str, float]], list[dict]] | None = None
        # Universe cache: (today, config_key, filtered_codes). Liquidity /
        # age / name filters only change daily, so we cache the filtered
        # list and reuse for ticks within the same trading day. See
        # _cached_tradable_universe for details.
        self._universe_cache: tuple[date, tuple, list[str]] | None = None
        # True while we (the runtime) have installed an equal-weight FixedSizer
        # on the broker, so _set_cycle_sizer only resets a sizer it owns.
        self._equal_weight_active = False
        self._prev_sizer = None  # broker's sizer before equal-weight install
        self._candidate_source_logged: date | None = None  # daily dedup for the
        # no-screener "candidate_source" beta-exposure decision log
        self._attribution_logged: date | None = None  # daily dedup for the
        # observe-only "pool_vs_passive" active-vs-passive guardrail log
        # Degraded-exit cache: (monotonic ts, list) — recomputed at most once
        # per 60s inside status(), so the UI sees fresh coverage without a
        # strategy-directory scan on every poll.
        self._degraded_cache: tuple[float, list[dict]] = (0.0, [])
        self._thread: threading.Thread | None = None
        self._guard_thread: threading.Thread | None = None
        # 盘中 LLM 守护线程(llm_full 模式,默认每 300s 一轮)。独立于 5 秒
        # 机械 guard:LLM 单轮是分钟级网络 I/O,塞进机械线程会让止损停摆。
        self._llm_guard_thread: threading.Thread | None = None
        self._llm_full_live_warned: date | None = None  # 每日一次的降级告警去重
        # Serializes broker MUTATIONS between the tick and the intraday guard
        # thread. Held only for the short mutating segments of a tick (see
        # `_broker_exec`), never across the compute stages — those can take
        # minutes and the guard would starve. RLock so a nested critical
        # section (e.g. `_set_cycle_sizer` inside one) doesn't self-deadlock.
        self._broker_lock = threading.RLock()
        # Serializes tick-vs-tick (loop thread vs the manual /api tick button).
        # This is what `_broker_lock` used to do by being held end-to-end.
        # RLock: the strategy-cache-miss path recurses into _run_one_cycle.
        self._cycle_lock = threading.RLock()
        # Set by whichever thread fires the portfolio circuit breaker; checked
        # under `_broker_lock` before every tick mutation (see `_broker_exec`).
        self._breaker_flag = threading.Event()
        self._stop_flag = threading.Event()
        self._status = AgentStatus()
        self._lock = threading.Lock()
        # Cross-process live-trading singleton (see _acquire_live_singleton).
        self._singleton_owner: str | None = None
        self._singleton_thread: threading.Thread | None = None

    # ------------------------------------------------------------- public
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        # Live only: take the cross-process singleton BEFORE starting the loop —
        # refuse to run a second live agent against the same account (raises).
        self._acquire_live_singleton()
        goal = load_goal(self._db)
        goal.enabled = True
        save_goal(self._db, goal)
        self._stop_flag.clear()
        self._thread = threading.Thread(
            target=self._loop, name="quanti-agent", daemon=True)
        self._thread.start()
        if self._intraday_guard_sec > 0 and (
                self._guard_thread is None or not self._guard_thread.is_alive()):
            self._guard_thread = threading.Thread(
                target=self._guard_loop, name="quanti-intraday-guard", daemon=True)
            self._guard_thread.start()
        if (self._llm_guard_thread is None
                or not self._llm_guard_thread.is_alive()):
            # 无条件启动:线程每轮自判 agent_mode,非 llm_full 时纯空转等待。
            self._llm_guard_thread = threading.Thread(
                target=self._llm_guard_loop, name="quanti-llm-guard",
                daemon=True)
            self._llm_guard_thread.start()
        if self._singleton_owner and (
                self._singleton_thread is None
                or not self._singleton_thread.is_alive()):
            self._singleton_thread = threading.Thread(
                target=self._singleton_heartbeat, name="quanti-live-lock",
                daemon=True)
            self._singleton_thread.start()
        with self._lock:
            self._status.enabled = True
            self._status.running = True
            self._status.started_at = datetime.now().isoformat()
        self._db.log_decision("agent_start", "Agent started")

    def stop(self) -> None:
        """User-initiated stop: halt the loop AND persist disabled state so the
        agent does not auto-resume across server restarts."""
        was_running = self._thread is not None and self._thread.is_alive()
        self._stop_flag.set()
        goal = load_goal(self._db)
        if goal.enabled:
            goal.enabled = False
            save_goal(self._db, goal)
        with self._lock:
            self._status.enabled = False
            self._status.running = False
            self._status.started_at = None
        self._release_live_singleton()
        if was_running:
            self._db.log_decision("agent_stop", "Agent stopped")

    def shutdown(self) -> None:
        """Process-shutdown: halt the loop but DO NOT touch goal.enabled — so
        the agent will auto-resume on the next server start. No decision log.
        """
        if self._thread is None or not self._thread.is_alive():
            return
        self._stop_flag.set()
        with self._lock:
            self._status.running = False
        for th in (self._thread, self._guard_thread, self._llm_guard_thread,
                   self._singleton_thread):
            if th is not None:
                try:
                    th.join(timeout=2)
                except Exception:
                    pass
        self._release_live_singleton()

    # ------------------------------------------ live singleton (cross-process)
    def _acquire_live_singleton(self) -> None:
        """Live only: claim the cross-process singleton so a second process
        (server + CLI agent + MCP …) can't drive the same live account and
        double/conflict orders. No-op for paper. Raises if another live process
        currently holds it."""
        if not getattr(self._broker, "_require_live", False):
            return
        import os
        import socket
        import uuid
        owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        if not self._db.claim_live_singleton(owner):
            raise RuntimeError(
                "拒绝启动实盘 Agent:已有另一个实盘进程在运行(live_singleton 被占用)。"
                "同一账户被多个进程驱动会重复/冲突下单——请先停掉那个进程再启动。")
        self._singleton_owner = owner
        self._db.log_decision(
            "live_singleton_acquired", "实盘单例锁已获取(跨进程互斥)",
            details={"owner": owner})

    def _singleton_heartbeat(self) -> None:
        """Refresh the singleton heartbeat so our lock doesn't look stale. If we
        LOSE it (another process reclaimed a stale lock), halt — two live agents
        must never run at once."""
        while not self._stop_flag.wait(30):
            owner = self._singleton_owner
            if not owner:
                return
            try:
                if not self._db.refresh_live_singleton(owner):
                    logger.critical(
                        "live singleton lost by %s — halting agent to avoid "
                        "split-brain live trading", owner)
                    self._db.log_decision(
                        "live_singleton_lost",
                        "实盘单例锁丢失(疑似被另一进程接管),已自停避免双实盘",
                        details={"owner": owner})
                    self._stop_flag.set()
                    return
            except Exception as e:  # noqa: BLE001
                logger.warning("live singleton heartbeat error: %s", e)

    def _release_live_singleton(self) -> None:
        owner = self._singleton_owner
        if not owner:
            return
        self._singleton_owner = None  # stop the heartbeat before deleting the row
        try:
            self._db.release_live_singleton(owner)
        except Exception as e:  # noqa: BLE001
            logger.warning("live singleton release error: %s", e)

    def guard_status(self) -> dict:
        """Intraday-guard daemon state for the live status card."""
        try:
            params = load_goal(self._db).params or {}
        except Exception:  # noqa: BLE001
            params = {}
        llm_full = str(params.get("agent_mode", "")).lower() == "llm_full"
        return {
            "enabled": self._intraday_guard_sec > 0,
            "interval_sec": self._intraday_guard_sec,
            "running": bool(self._guard_thread and self._guard_thread.is_alive()),
            "llm_guard": {
                "mode": llm_full,
                "interval_sec": int(float(
                    params.get("llm_guard_interval_sec", 300) or 300)),
                "running": bool(self._llm_guard_thread
                                and self._llm_guard_thread.is_alive()),
            },
        }

    def _guard_loop(self) -> None:
        """Fast intraday guard: every `intraday_guard_sec`, reconcile venue
        fills + run exits + the portfolio circuit breaker — so async fills
        and stop-losses are caught in ~1 min instead of waiting for the 4h full
        tick. Idles outside trading sessions / when the venue isn't connected.
        Marks ride realtime quotes: xtdata via the bridge on live, Tencent
        quotes on paper (PaperBroker._intraday_marks), never stale data."""
        while not self._stop_flag.is_set():
            if self._stop_flag.wait(self._intraday_guard_sec):
                return
            try:
                self._intraday_guard()
            except Exception as e:  # noqa: BLE001 - a guard hiccup can't kill the loop
                logger.warning("intraday guard cycle failed: %s", e)

    def _intraday_guard(self) -> None:
        from quanti.utils.market import in_trading_session
        if not in_trading_session(datetime.now(), self._provider):
            return
        # Only act when the venue is connected (paper: always) — live exits/
        # marks must ride xtdata quotes via the bridge, never stale/mock data.
        is_conn = getattr(self._broker, "is_connected", None)
        if callable(is_conn) and not is_conn():
            return
        # llm_full 模式同步(锁外读 goal):机械 guard 的 check_exits 走 LLM
        # 点位分支、check_llm_adds 生效,都取决于 broker.llm_managed。
        try:
            self._sync_llm_mode(load_goal(self._db))
        except Exception as e:  # noqa: BLE001 - 同步失败保持上次模式
            logger.warning("guard llm-mode sync failed: %s", e)
        with self._broker_lock:  # never overlap the full tick's broker mutations
            try:
                self._broker.try_fill_pending_orders()
            except Exception as e:  # noqa: BLE001
                logger.warning("guard fill reconcile failed: %s", e)
            try:
                if self._broker.enforce_portfolio_stop():
                    # Flag first: a tick may be mid-compute right now and must
                    # not place its buys into the book we just flattened.
                    self._breaker_flag.set()
                    self._db.log_decision("cycle_halt", "盘中组合熔断:已清仓并暂停 agent")
                    self.stop()
                    return
            except Exception as e:  # noqa: BLE001
                logger.warning("guard portfolio-stop failed: %s", e)
            try:
                self._broker.check_exits()
            except Exception as e:  # noqa: BLE001
                logger.warning("guard check_exits failed: %s", e)
            # LLM 加仓点位触发(仅 llm_full;paper broker 才有此方法)。
            try:
                adds = getattr(self._broker, "check_llm_adds", None)
                if callable(adds):
                    adds()
            except Exception as e:  # noqa: BLE001
                logger.warning("guard check_llm_adds failed: %s", e)

    # ------------------------------------------ 盘中 LLM 守护 (llm_full)
    def _llm_guard_loop(self) -> None:
        """盘中 LLM 守护:每 llm_guard_interval_sec(默认 300s,goal.params
        可调)把持仓明细+实时价+现行点位交给 LLM,输出卖出/加仓/改点位并即刻
        执行。单线程顺序执行天然单飞(上一轮没跑完不会发下一轮)。任何一轮
        失败只跳过本轮——5 秒机械 guard 的点位执行从不依赖这里。"""
        while not self._stop_flag.is_set():
            if self._stop_flag.wait(self._llm_guard_interval()):
                return
            try:
                self._llm_guard_cycle()
            except Exception as e:  # noqa: BLE001 - 一轮失败不许杀线程
                logger.warning("llm guard cycle failed: %s", e)
                try:
                    self._db.log_decision(
                        "llm_guard_skip", f"盘中 LLM 守护异常跳过: {e}")
                except Exception:  # noqa: BLE001
                    pass

    def _llm_guard_interval(self) -> float:
        try:
            params = load_goal(self._db).params or {}
            return max(60.0, float(params.get("llm_guard_interval_sec", 300)))
        except Exception:  # noqa: BLE001
            return 300.0

    def _llm_guard_cycle(self) -> None:
        from quanti.utils.market import in_trading_session
        goal = load_goal(self._db)
        if not self._sync_llm_mode(goal):
            return  # 非 llm_full:线程空转
        if not in_trading_session(datetime.now(), self._provider):
            return
        is_conn = getattr(self._broker, "is_connected", None)
        if callable(is_conn) and not is_conn():
            return
        params = goal.params or {}
        client = getattr(self, "_llm_client", None)
        if client is None:
            try:
                client = self._build_llm_client(params)
            except (ImportError, ValueError) as e:
                self._db.log_decision(
                    "llm_guard_skip",
                    f"盘中 LLM 守护跳过(LLM 不可用): {e}",
                    details={"error": str(e)})
                return
        from quanti.agent.llm_full import run_llm_guard_decision
        cfg = self._llm_full_cfg(params)
        cfg.max_tokens = int(params.get("llm_guard_max_tokens", 4096))
        cfg.max_tool_iterations = 4
        run_llm_guard_decision(
            db=self._db, broker=self._broker, provider=self._provider,
            goal=goal, llm_client=client, cfg=cfg,
            broker_gate=self._broker_exec)

    def restart(self) -> None:
        """Reschedule cleanly: stop the running loop thread (joining it) then
        start a fresh one, WITHOUT flipping persisted goal.enabled. Used when
        the daily schedule changes so a new daily_run_time takes effect
        immediately. Safe to call when not running (acts like start())."""
        self.shutdown()
        self.start()

    def _degraded_exits_cached(self) -> list[dict]:
        """Exit-coverage scan with a 60s TTL — status() is polled by the UI
        every few seconds and must not rescan the strategy directory on every
        poll. A miss runs the doctor's exit_coverage check (local reads only)."""
        now = time.monotonic()
        ts, cached = self._degraded_cache
        if now - ts < 60.0:
            return cached
        try:
            from quanti.health import exit_coverage
            degraded = exit_coverage(self._db, self._strategies_dir)["degraded"]
        except Exception:  # noqa: BLE001 - status() must never raise
            degraded = cached
        self._degraded_cache = (now, degraded)
        return degraded

    def status(self) -> AgentStatus:
        # Snapshot BEFORE taking the status lock: in-session it may do real
        # network I/O (realtime marks) — holding the lock through that would
        # block start/stop and every other status() caller on a slow quote.
        snap = self._broker.snapshot_portfolio()
        with self._lock:
            self._status.total_value = snap["total_value"]
            self._status.pnl_pct = snap["pnl_pct"]
            return AgentStatus(
                enabled=self._status.enabled,
                running=self._thread.is_alive() if self._thread else False,
                started_at=self._status.started_at,
                tick_interval_sec=self._tick_interval,
                last_tick_at=self._status.last_tick_at,
                last_tick_summary=self._status.last_tick_summary,
                last_strategy=self._status.last_strategy,
                last_evaluations=list(self._status.last_evaluations),
                total_value=self._status.total_value,
                pnl_pct=self._status.pnl_pct,
                degraded_exits=self._degraded_exits_cached(),
            )

    def tick(self) -> dict:
        """Force one synchronous cycle (used by manual button / MCP)."""
        return self._run_one_cycle()

    # ------------------------------------------------------------ internal
    def _loop(self) -> None:
        # Interval mode: run immediately so the user gets feedback. Daily-
        # schedule mode (goal.params["daily_run_time"]="HH:MM", e.g. "17:30"):
        # wait for the scheduled time instead of firing on start.
        if self._daily_run_time() is None:
            self._safe_cycle()
        while not self._stop_flag.is_set():
            if self._stop_flag.wait(self._next_wait_seconds()):
                break
            if self._stop_flag.is_set():
                break
            # Daily-schedule mode skips non-trading days (weekends / holidays)
            # unless goal.params["daily_trading_days_only"] is False. Interval
            # mode runs every tick as before.
            if self._daily_run_time() is not None and not self._daily_runs_today():
                logger.info("daily schedule: %s is not a trading day, skipping",
                            date.today().isoformat())
                continue
            self._safe_cycle()

    def _daily_runs_today(self) -> bool:
        """In daily-schedule mode, whether today should run. Gated to trading
        days unless goal.params['daily_trading_days_only'] is False. Holiday
        accuracy needs a synced trade_calendar (`quanti sync --calendar`);
        without it, falls back to weekday. Never blocks on a calendar error."""
        try:
            params = load_goal(self._db).params or {}
        except Exception:  # noqa: BLE001
            params = {}
        if not params.get("daily_trading_days_only", True):
            return True
        try:
            from quanti.utils.market import is_trading_day
            return is_trading_day(date.today(), self._provider)
        except Exception:  # noqa: BLE001 - never block trading on a calendar error
            return True

    def _next_wait_seconds(self) -> float:
        """Seconds to wait before the next cycle: until a fixed daily clock time
        when goal.params['daily_run_time']='HH:MM' is set, else the tick
        interval. Re-read each iteration so the schedule can change live."""
        t = self._daily_run_time()
        if t is None:
            return self._tick_interval
        return _seconds_until_daily(datetime.now(), t[0], t[1])

    def _daily_run_time(self) -> tuple[int, int] | None:
        try:
            raw = (load_goal(self._db).params or {}).get("daily_run_time")
        except Exception:  # noqa: BLE001 - schedule lookup must never crash loop
            return None
        return _parse_hhmm(raw)

    def _safe_cycle(self) -> None:
        """Run one cycle and swallow ALL errors — including ones thrown by the
        error-logging path itself — so a transient DB / disk issue cannot
        kill the agent thread. The next tick will get another chance."""
        try:
            self._run_one_cycle()
        except Exception as e:
            logger.exception(f"Agent tick failed: {e}")
            try:
                self._db.log_decision(
                    "agent_error", f"Tick error: {e}",
                    details={"trace": str(e)})
            except Exception as log_err:
                # DB itself may be the problem (read-only, full, locked).
                # Logging here failing is fine — we already log to stderr above.
                logger.warning(f"Could not persist agent_error decision: {log_err}")

    # ---------------------------------------------------- universe helpers
    def _resolve_universe(self, goal: Goal) -> list[str]:
        """Build the candidate stock universe for this tick.

        Three sources, in order:
          1. `goal.universe_pool` — a user-curated pool. Trusted; used as-is.
          2. All codes with quotes in DB (`provider.get_all_codes()`).
          3. Optional liquidity filter (P4, 2026-05): when
             `goal.params["liquidity_filter"]=True`, run UniverseBuilder
             to drop ST / new IPOs / illiquid names. Result cached daily.
        """
        if goal.universe_pool:
            codes = self._db.get_pool_codes(goal.universe_pool)
            if codes:
                return codes

        codes = self._provider.get_all_codes()
        if not codes:
            return []

        params = goal.params or {}
        if not bool(params.get("liquidity_filter", False)):
            return codes

        return self._cached_tradable_universe(codes, params)

    def _cached_tradable_universe(self, codes: list[str], params: dict) -> list[str]:
        """Run UniverseBuilder, cache the result for the current trading day.

        Liquidity / age / name filters only change daily — running them on
        every 4h tick is wasteful. Key on `(today, config-hash)`. If the
        builder filtered everything out (config too strict), fall back to
        the unfiltered list so the agent doesn't deadlock.
        """
        from quanti.agent.universe import UniverseBuilder, universe_config_from_params

        cfg = universe_config_from_params(params)
        today = date.today()
        cfg_key = (cfg.min_adv20_yuan, cfg.min_active_days_60, cfg.min_age_days,
                   len(codes))
        if (self._universe_cache is not None
                and self._universe_cache[0] == today
                and self._universe_cache[1] == cfg_key):
            return self._universe_cache[2]

        builder = UniverseBuilder(self._db, self._provider, cfg)
        filtered, result = builder.build(candidates=codes, return_details=True)

        if not filtered:
            logger.warning(
                f"Liquidity filter dropped all {len(codes)} codes — "
                f"falling back to unfiltered. result={result.as_dict()}")
            return codes

        self._db.log_decision(
            "universe_filter",
            f"流动性宇宙清洗: {result.initial} → {result.final and len(result.final)} 只",
            details={"result": result.as_dict(), "config": {
                "min_adv20_yuan": cfg.min_adv20_yuan,
                "min_active_days_60": cfg.min_active_days_60,
                "min_age_days": cfg.min_age_days,
            }})
        self._universe_cache = (today, cfg_key, filtered)
        return filtered

    def _ensure_recent_data(self, codes: list[str], lookback_days: int = 200) -> None:
        """Trigger an AkShare sync for any code missing recent bars. Bounded.

        Two priorities:
          1. Currently held positions go first — they drive the mark-to-market
             pnl on the dashboard, so stale prices visibly "freeze" the UI.
          2. Other candidates fill the rest of the per-tick budget.

        Freshness: a bar from before yesterday is considered stale (was 7
        days, which left positions frozen for nearly a trading week).
        """
        from quanti.data.source import make_quote_adapter
        end = date.today()
        start = end - timedelta(days=lookback_days)
        stale_after = end - timedelta(days=1)  # bars older than yesterday

        held = [p["code"] for p in self._db.list_positions()]
        held_set = set(held)
        # Held codes first, others after, dedupe while preserving order
        ordered: list[str] = []
        for c in held + [c for c in codes if c not in held_set]:
            if c not in ordered:
                ordered.append(c)

        # ONE bulk freshness query for the whole universe instead of one
        # get_daily_bars call per code. The per-code loop was 5.8k serialized
        # DB round trips on the full market — with the bg-sync's factor
        # rescore and the daily hooks contending for the same SQLite lock, it
        # turned cold-start ticks into hour-long lock convoys (2026-08-14
        # faulthandler diagnosis). latest_quote_dates does a single GROUP BY.
        try:
            latest = self._db.latest_quote_dates(ordered)
        except AttributeError:  # legacy/older DB wrapper in exotic setups
            latest = {}
        missing: list[str] = [
            c for c in ordered
            if latest.get(c) is None or latest[c] < stale_after
        ]
        if not missing:
            return
        # Cap per tick. Held positions are guaranteed to be in the first
        # `len(held)` slots; the rest of the budget covers candidates.
        budget = max(20, len(held_set))
        missing = missing[:budget]

        try:
            adapter = make_quote_adapter(self._db)
        except Exception as e:
            # Source unavailable (e.g. tushare, no token — no silent akshare
            # fallback). Skip the refresh this tick rather than crash the loop.
            logger.warning(f"Agent data refresh skipped (data source): {e}")
            return
        for c in missing:
            try:
                adapter.sync_daily_quotes(c, start=start, end=end,
                                          repair_gaps=False)
                # The provider's series cache may hold the pre-sync series —
                # drop it so the same tick's downstream reads see the new bar.
                self._provider.invalidate_series_cache(c)
            except Exception as e:
                logger.warning(f"Agent data refresh failed for {c}: {e}")

    def _run_screener(self, goal: Goal, codes: list[str]) -> list[str]:
        # No screener configured → return [] so the caller falls back to
        # the ADV-ranked top-N selection. The old `return codes` shortcut
        # accidentally pushed the full universe (4000+ codes after the P4
        # liquidity filter) through to the ensemble path, producing
        # thousands of signals per tick that the broker couldn't handle.
        if not goal.screener_name:
            return []
        loader = ScreenerLoader()
        screeners = loader.load_directory(self._screeners_dir)
        screener = next((s for s in screeners if s.name == goal.screener_name), None)
        if screener is None:
            return codes
        screener.init({})
        end = date.today()
        start = end - timedelta(days=180)
        scored: list[tuple[str, float]] = []
        for c in codes:
            try:
                bars = self._provider.get_daily_bars(c, start, end)
                if len(bars) < 20:
                    continue
                s = screener.screen(c, bars)
                if s > 0:
                    scored.append((c, s))
            except Exception:
                pass
        scored.sort(key=lambda x: x[1], reverse=True)
        # Default raised from 20 → 50 in 2026-05 (P4). The 20-cap was so
        # tight that mid-quality stocks couldn't reach the factor / strategy
        # layers downstream. Override via goal.params["screener_top_n"].
        params = goal.params or {}
        top_n = int(params.get("screener_top_n", 50))
        return [c for c, _ in scored[:top_n]]

    # --------------------------------------------------- signal generators
    def _single_strategy_signals(self, strategy, candidates: list[str]) -> list[Signal]:
        """Run a single strategy over the candidate universe and return its
        recent (last 3 trading days) signals, deduplicated per (code, direction).

        Extracted from `_run_one_cycle` so both the pin and select-one paths
        share it without duplication. Older history bars still get fed in so
        the strategy's internal state (MA buffers, MACD lines, etc.) is fully
        warmed up before today's bar produces a signal.
        """
        end = date.today()
        start = end - timedelta(days=365)
        recent_cutoff = end - timedelta(days=3)
        latest: dict[tuple[str, str], tuple[date, Signal]] = {}
        for code in candidates:
            bars = self._provider.get_daily_bars(code, start, end)
            for bar in bars:
                if bar.date < recent_cutoff:
                    strategy.on_bar(bar)
                    continue
                produced = strategy.on_bar(bar)
                for sig in produced:
                    key = (sig.stock_code, sig.direction.value)
                    prior = latest.get(key)
                    if prior is None or bar.date >= prior[0]:
                        latest[key] = (bar.date, sig)
        return [v[1] for v in latest.values()]

    def _compute_fused_candidates(
        self, goal: Goal, candidates: list[str], include_all: bool = False,
    ) -> tuple[list, list[dict], dict[str, float]]:
        """Run top-K Selector + factor pipeline → FusedCandidate list.

        Shared between the rule-ensemble and LLM paths. Returns
        (fused_candidates, evaluations, strategy_weights). Empty list
        means "no actionable candidates this cycle".

        `include_all=True`(llm_full 模式):策略没投票的候选也按因子分补进
        列表——LLM 全权模式要看到整个候选池,策略票只是打分信号之一,不再是
        入场闸门。

        Logs a `strategy_ensemble` decision as a side-effect — this is the
        only place we have full attribution and weights, and the audit
        trail wants it regardless of which downstream path consumes them.
        """
        params = goal.params or {}
        k = int(params.get("top_k_strategies", 3))
        threshold = float(params.get("signal_threshold", 0.30))
        factor_blend = float(params.get("factor_blend", 0.5))
        industry_neutral = bool(params.get("industry_neutral", False))
        n_per_industry = int(params.get("n_per_industry", 2))
        sentiment_enabled = bool(params.get("sentiment_enabled", False))
        sentiment_blend = float(params.get("sentiment_blend", 0.0))
        sentiment_max_codes = int(params.get("sentiment_max_codes", 30))

        # Reuse the cached top-K selection within _reselect_interval (mirrors
        # the single-strategy _selector_cache). Strategy selection + weights are
        # a slow-moving, expensive walk-forward result; only recompute it once
        # per interval. Fresh strategy instances are loaded on a cache hit so no
        # per-tick state carries across (same discipline as the single path).
        now_ts = time.time()
        cached = self._topk_cache
        if cached is not None and now_ts - cached[0] < self._reselect_interval:
            name_weights, evaluations = cached[1], cached[2]
            by_name = {s.name: s
                       for s in StrategyLoader().load_directory(self._strategies_dir)}
            pairs = [(by_name[n], w) for n, w in name_weights if n in by_name]
        else:
            selector = StrategySelector(self._db, self._provider,
                                        self._strategies_dir)
            pairs, ranking = selector.pick_topk(goal, candidates, k=k)
            evaluations = [e.as_dict() for e in ranking]
            if pairs:  # only cache a real selection; empty → retry next tick
                self._topk_cache = (now_ts, [(s.name, w) for s, w in pairs],
                                    evaluations)
        if not pairs:
            return [], evaluations, {}

        prepared: list[tuple] = []
        for strat, weight in pairs:
            strat.init(resolve_params(self._db, strat.name, goal))
            prepared.append((strat, weight))

        per_strategy, weights = collect_signals_per_strategy(
            prepared, candidates, self._provider)

        # Cross-section is the `candidates` set (ADV-top-N), NOT the full
        # market — and this is deliberately left as-is. Validation (2026-06-26,
        # scripts/factor_breadth_validate.py, 5y+OOS) recomputed the panel over
        # a wide ADV-1000 pool and re-ranked the same candidates: 84% of picks
        # were identical and the return gap was inside the noise floor (Sharpe
        # SE ≈ ±0.7 at n=24 months). Since production also blends the panel
        # only 50% with strategy votes (factor_blend), widening the z-score
        # reference set is a ~0-impact change that adds per-tick cost for no
        # measurable benefit. (Separately, the whole factor layer loses to
        # plain pool equal-weight OOS — there's no within-pool alpha to sharpen
        # here regardless.) Don't "fix" this without re-running that script.
        panel = compute_factor_panel(
            self._provider, self._db, candidates,
            config=FactorConfig(industry_neutralize=industry_neutral),
            include_generated=bool(params.get("use_generated_factors", False)))

        sentiment_scores = None
        if sentiment_enabled and sentiment_blend > 0:
            sentiment_scores = self._compute_sentiment(
                goal, panel, sentiment_max_codes)

        fused = fuse_buy_signals(per_strategy, weights,
                                 factor_panel=panel,
                                 factor_blend=factor_blend,
                                 sentiment_scores=sentiment_scores,
                                 sentiment_blend=(sentiment_blend
                                                  if sentiment_scores else 0.0))

        if include_all:
            from quanti.agent.signal_pipeline import (_factor_score,
                                                      _industry_for, _sigmoid)
            have = {c.code for c in fused}
            extras = []
            for code in candidates:
                if code in have:
                    continue
                fs = _factor_score(panel, code)
                extras.append(FusedCandidate(
                    code=code, strategy_score=0.0, factor_score=fs,
                    sentiment_score=0.0,
                    final_score=factor_blend * _sigmoid(fs, k=1.0),
                    contributing_strategies=[], dominant_strategy="",
                    industry=_industry_for(panel, code)))
            fused = sorted(fused + extras,
                           key=lambda c: c.final_score, reverse=True)

        if industry_neutral:
            fused = industry_cap(fused, n_per_industry=n_per_industry)
        fused = filter_by_threshold(fused, threshold=threshold)

        # Breadth cap: keep only the top-N by final_score. Lets the user run a
        # deliberately diversified book (e.g. 20 equal-weight names) instead of
        # the emergent ~9-10 the 10% per-name cap allows by default. 0 = off.
        # Note: this caps NEW buys only; the breadth cap itself never sells a
        # held name that fell out of top-N. Freeing a full book for a stronger
        # name is the (opt-in) score-gated rotation's job — see _maybe_rotate.
        max_holdings = int(params.get("max_holdings", 0))
        if max_holdings > 0:
            fused = fused[:max_holdings]

        # Attach each candidate's latest close so the LLM path can reason about
        # lot value (×100 = one A-share board lot) and affordability. fused is
        # already filtered to the top-N, so this is a handful of cheap reads.
        price_start = date.today() - timedelta(days=30)
        for c in fused:
            bars = self._provider.get_daily_bars(c.code, price_start, date.today())
            if bars:
                c.current_price = float(bars[-1].close)

        contributing = sorted({s for c in fused for s in c.contributing_strategies})
        self._db.log_decision(
            "strategy_ensemble",
            f"ensemble 选定 {len(pairs)} 个策略: {', '.join(s.name for s, _ in pairs)}",
            details={
                "evaluations": evaluations,
                "weights": {name: round(w, 3) for name, w in weights.items()},
                "fused_candidates": len(fused),
                "contributing": contributing,
                "industry_neutral": industry_neutral,
            })
        # Persist this cycle's fused scores so the UI can show a holding's /
        # order's current candidate score. Replace-all (skip on empty so a
        # transient no-candidate cycle doesn't wipe the last known scores).
        if fused:
            self._db.replace_candidate_scores(
                {c.code: c.final_score for c in fused}, date.today().isoformat())
        return fused, evaluations, weights

    def _compute_sentiment(self, goal: Goal, panel, max_codes: int):
        """News-sentiment overlay for the strongest candidates.

        Returns { code → score ∈ [-1, 1] } or None to disable the overlay
        (no candidates, no LLM available, or any failure). Scores only the
        top-`max_codes` by factor composite to bound LLM cost, and logs a
        `sentiment_overlay` decision for the audit trail. Never raises.
        """
        from quanti.agent.llm_runtime import DEFAULT_MODEL
        from quanti.agent.sentiment import SentimentConfig, score_candidates
        from quanti.factors.cross_sectional import rank_by_composite

        if panel is None or panel.empty:
            return None
        top = [c for c, _ in rank_by_composite(panel, top_n=max_codes)]
        if not top:
            return None

        client = getattr(self, "_llm_client", None)
        if client is None:
            try:
                client = self._build_llm_client(goal.params or {})
            except (ImportError, ValueError):
                return None  # overlay is a no-op without an LLM

        params = goal.params or {}
        cfg = SentimentConfig(
            model=str(params.get("sentiment_model",
                                 params.get("llm_model", DEFAULT_MODEL))),
            max_codes=max_codes,
        )
        try:
            scores = score_candidates(self._db, top, client, cfg=cfg)
        except Exception as e:
            logger.warning(f"sentiment overlay failed, skipping: {e}")
            return None

        self._db.log_decision(
            "sentiment_overlay",
            f"新闻情绪: 评分 {len(scores)} 只候选",
            details={"scores": {k: round(float(v), 3)
                                for k, v in scores.items()}})
        return scores or None

    def _ensemble_path(self, goal: Goal, candidates: list[str],
                       ) -> tuple[list[Signal], str, list[dict], list]:
        """Rule-driven ensemble: candidates → signals (no LLM).

        Returns the FusedCandidate list too (4th element) so the cycle can run
        score-gated rotation against it — single-strategy paths return [] there.
        """
        fused, evaluations, _weights = self._compute_fused_candidates(
            goal, candidates)
        params = goal.params or {}
        equal_weight = bool(params.get("equal_weight", False)) and bool(fused)
        if not equal_weight:
            self._set_cycle_sizer(None)
            return [c.to_signal() for c in fused], "ensemble", evaluations, fused

        # Passive equal-weight book: each held name targets the SAME weight via
        # a FixedSizer, so capital isn't front-loaded into the top ranks.
        weight_per_name = 1.0 / len(fused)
        if not self._set_cycle_sizer(weight_per_name):
            # Broker has no equal-weight sizer (e.g. live QmtBroker sizes via
            # cash%/risk-cap). Fall back to conviction-weighted sizing rather
            # than forcing strength=1.0 onto a sizer that ignores it.
            return [c.to_signal() for c in fused], "ensemble", evaluations, fused

        # Per-name target is min(1/N, max_position_pct): the RiskManager
        # single-stock cap still binds. For N < ~10 that cap wins and the book
        # under-invests to N×cap — log it loudly rather than silently leaving
        # cash idle (data-integrity: never relax a risk cap behind the user).
        cap = getattr(getattr(self._broker, "_risk", None), "config", None)
        cap_pct = float(getattr(cap, "max_position_pct", 0.0) or 0.0)
        if cap_pct and weight_per_name > cap_pct:
            self._db.log_decision(
                "equal_weight_capped",
                f"等权 {len(fused)} 只:目标 {weight_per_name:.1%}/只 超过单票风控上限 "
                f"{cap_pct:.1%},按上限建仓,组合仅约 {len(fused) * cap_pct:.0%} 仓位、"
                f"其余留现金。要全仓等权请用 N≥{int(round(1 / cap_pct))} 只。")
        signals = [c.to_signal(strength=1.0) for c in fused]
        return signals, "ensemble", evaluations, fused

    def _maybe_rotate(self, fused: list, intended_buys: list[str]) -> None:
        """Score-gated rotation (换仓): when the book is full, sell the weakest
        holding to free room for a clearly-stronger fresh candidate. Opt-in via
        RiskConfig.rotation_enabled (read live from the DB); no-op otherwise.

        Shared by the ensemble path here and the LLM path
        (`llm_runtime.run_llm_decision`). The emitted SELLs go through the same
        broker path as exits, so cash frees before the buys fill.
        """
        from quanti.risk.manager import risk_config_from_dict
        rc = risk_config_from_dict(self._db.get_risk_config())
        if not rc.rotation_enabled:
            return
        snap = self._broker.snapshot_portfolio()
        held_mv = {p["code"]: float(p.get("market_value", 0.0) or 0.0)
                   for p in snap.get("positions", [])}
        score_by_code = {c.code: c.final_score for c in fused}
        # Buy/hold spread: retain a still-trending holding even when it's no
        # longer a fresh candidate (it would otherwise score 0 = weakest and be
        # sold first). See trending_holdings.
        protected = trending_holdings(self._provider, list(held_mv))
        sells = select_rotation_sells(
            intended_buys, score_by_code, held_mv,
            float(snap.get("cash", 0.0) or 0.0),
            float(snap.get("total_value", 0.0) or 0.0),
            margin=rc.rotation_margin, max_position_pct=rc.max_position_pct,
            exclude=protected)
        if not sells:
            return
        self._broker.execute_signals(sells, strategy_name="rotation")
        self._db.log_decision(
            "rotation",
            f"换仓 释放 {len(sells)} 个弱仓为更强候选腾位: "
            + ", ".join(s.stock_code for s in sells),
            details={"sells": [{"code": s.stock_code, "reason": s.reason}
                               for s in sells],
                     "margin": rc.rotation_margin})

    def _set_cycle_sizer(self, weight_per_name: float | None) -> bool:
        """Install (or clear) an equal-weight FixedSizer on the broker for this
        cycle. Returns True iff an equal-weight sizer is now active.

        On install we snapshot the broker's prior sizer and restore it on
        reset, so a sizer injected at broker construction survives an
        equal-weight toggle. No-op returning False on brokers without a sizer
        slot (e.g. QmtBroker, which sizes via cash%/risk-cap and ignores
        Sizer).

        Takes `_broker_lock` itself (it's a broker mutation, and it is called
        from the lock-free compute stages) — the guard only ever sells, which
        the sizer doesn't size, but the invariant "no concurrent broker
        mutation" is cheaper to keep than to reason about."""
        setter = getattr(self._broker, "set_sizer", None)
        if setter is None:
            return False
        with self._broker_lock:
            if weight_per_name is not None:
                from quanti.risk.sizer import FixedSizer
                if not self._equal_weight_active:
                    self._prev_sizer = getattr(self._broker, "_sizer", None)
                setter(FixedSizer(max_pct=min(1.0, weight_per_name)))
                self._equal_weight_active = True
                return True
            if self._equal_weight_active:
                setter(self._prev_sizer)
                self._equal_weight_active = False
            return False

    def _build_llm_client(self, params: dict):
        """Construct an LLM client per goal.params['llm_provider'].

        'deepseek' → OpenAI-compatible DeepSeek client (reads DEEPSEEK_API_KEY).
        anything else → Anthropic SDK client (reads ANTHROPIC_API_KEY).
        Raises ImportError/ValueError when the chosen provider isn't usable;
        callers treat that as "LLM unavailable" and degrade gracefully.
        """
        provider = str(params.get("llm_provider", "anthropic")).lower()
        if provider in ("deepseek", "openai_compat"):
            from quanti.agent.openai_compat import DeepSeekLLMClient
            return DeepSeekLLMClient()
        from quanti.agent.llm_runtime import AnthropicLLMClient
        return AnthropicLLMClient()

    def _sync_llm_mode(self, goal: Goal) -> bool:
        """把 goal 的 agent_mode 同步到 broker.llm_managed(单一真源=goal)。

        返回本进程是否处于 llm_full 模式。实盘 QmtBroker 一律拒绝 llm_full
        (代码层 paper-only,晋级唯一通道=paper 先跑出可信记录),降级为普通
        "llm" 模式并落告警日志。"""
        params = goal.params or {}
        want = (str(params.get("agent_mode", "")).lower() == "llm_full"
                and not goal.strategy_name)
        if want and getattr(self._broker, "_require_live", False):
            if self._llm_full_live_warned != date.today():
                self._llm_full_live_warned = date.today()
                self._db.log_decision(
                    "llm_full_refused",
                    "llm_full(LLM 全权模式)仅限模拟盘:实盘链路保持机械风控,"
                    "本进程降级为普通 llm 模式。")
            want = False
        if hasattr(self._broker, "llm_managed"):
            self._broker.llm_managed = want
        return want

    def _llm_full_path(self, goal: Goal, candidates: list[str]) -> dict:
        """LLM 全权决策路径 (agent_mode="llm_full")。

        与 _llm_path 的区别:候选管线不设阈值/行业cap/持仓数上限(alpha 过滤
        全部旁路,策略跑分只作为打分信号),全候选直达 LLM;LLM 可买可卖,
        并为每个持仓落止损/加仓点位(llm_position_plans),由 5 秒机械 guard
        比价执行。LLM 不可用时本 tick 不产生新决策——存量点位继续机械保护,
        不回退到任何规则决策者。"""
        import copy

        from quanti.agent.llm_full import run_llm_full_decision

        params = goal.params or {}
        g2 = copy.copy(goal)
        g2.params = dict(params)
        g2.params.update({
            "signal_threshold": 0.0,   # 不设分数闸——LLM 看全部候选
            "industry_neutral": False,  # 行业集中度交给 LLM 判断
            "max_holdings": 0,          # 广度上限交给 LLM 判断
        })
        fused, evaluations, _weights = self._compute_fused_candidates(
            g2, candidates, include_all=True)
        if not fused:
            self._db.log_decision(
                "cycle_skip", "llm_full 模式: 无候选股",
                details={"evaluations": evaluations})
            return {"ok": False, "reason": "llm_full 模式: 无候选股",
                    "evaluations": evaluations}

        cfg = self._llm_full_cfg(params)
        client = getattr(self, "_llm_client", None)
        if client is None:
            try:
                client = self._build_llm_client(params)
            except (ImportError, ValueError) as e:
                # llm_full 没有规则决策者可回退:本 tick 空过,存量点位继续
                # 由机械 guard 执行(fail-loud,不静默降级成别的策略)。
                logger.warning("llm_full: LLM unavailable, tick skipped: %s", e)
                self._db.log_decision(
                    "llm_unavailable",
                    "llm_full 模式: LLM 不可用,本轮不产生新决策"
                    "(存量止损点位由机械守护继续执行)",
                    details={"error": str(e)})
                return {"ok": False, "reason": "LLM 不可用",
                        "evaluations": evaluations}

        return run_llm_full_decision(
            db=self._db, broker=self._broker, provider=self._provider,
            goal=goal, candidates=fused, llm_client=client, cfg=cfg,
            broker_gate=self._broker_exec,
        ) | {"evaluations": evaluations, "strategy": "llm_full"}

    @staticmethod
    def _llm_full_cfg(params: dict):
        """llm_full 的 LLMConfig:上下文放宽到 100 候选、输出 8192 tokens
        (截断硬拒,PR#149 教训)、单轮最多 10 单。均可经 goal.params 覆盖。"""
        from quanti.agent.llm_runtime import DEFAULT_MODEL, LLMConfig
        return LLMConfig(
            model=str(params.get("llm_model", DEFAULT_MODEL)),
            max_tokens=int(params.get("llm_max_tokens", 8192)),
            max_tool_iterations=int(params.get("llm_max_iterations", 8)),
            max_candidates_in_context=int(params.get("llm_max_candidates", 100)),
            temperature=float(params.get("llm_temperature", 0.3)),
            max_orders=int(params.get("llm_max_orders", 10)),
        )

    def _llm_path(self, goal: Goal, candidates: list[str]) -> dict:
        """LLM-driven path. Uses ensemble for candidate generation, then
        hands the candidates to Claude for the final pick/sizing decision.

        Returns the cycle result dict directly (this short-circuits the
        rest of `_run_one_cycle`'s execution + logging — LLM path has its
        own log entry kind `llm_cycle`).

        If LLM is unavailable (missing API key, anthropic not installed,
        upstream failure), gracefully falls back to the rule-ensemble path
        for this tick. The next tick will retry.
        """
        from quanti.agent.llm_runtime import (
            DEFAULT_MODEL,
            LLMConfig,
            run_llm_decision,
        )

        fused, evaluations, _weights = self._compute_fused_candidates(
            goal, candidates)
        if not fused:
            self._db.log_decision(
                "cycle_skip", "LLM 模式: 无候选股",
                details={"evaluations": evaluations})
            return {"ok": False, "reason": "LLM 模式: 无候选股",
                    "evaluations": evaluations}

        params = goal.params or {}
        cfg = LLMConfig(
            model=str(params.get("llm_model", DEFAULT_MODEL)),
            max_tokens=int(params.get("llm_max_tokens", 4096)),
            max_tool_iterations=int(params.get("llm_max_iterations", 5)),
            max_candidates_in_context=int(params.get("llm_max_candidates", 20)),
            temperature=float(params.get("llm_temperature", 0.3)),
            debate_enabled=bool(params.get("llm_debate", False)),
            debate_rounds=int(params.get("llm_debate_rounds", 1)),
            risk_debate_enabled=bool(params.get("llm_risk_debate", False)),
            reflection_enabled=bool(params.get("llm_reflection", False)),
            max_reflections=int(params.get("llm_max_reflections", 8)),
        )

        # Allow tests / advanced users to inject a custom client via the
        # runtime attribute. Default to the Anthropic SDK; if its import
        # fails (no [llm] extra installed), fall back to ensemble path.
        client = getattr(self, "_llm_client", None)
        if client is None:
            try:
                client = self._build_llm_client(params)
            except (ImportError, ValueError) as e:
                logger.warning(f"LLM unavailable, falling back to ensemble: {e}")
                self._db.log_decision(
                    "llm_unavailable",
                    "LLM 未安装,降级到 ensemble 路径",
                    details={"error": str(e)})
                signals = [c.to_signal() for c in fused]
                with self._broker_exec():
                    sl = self._broker.check_exits()
                    result = self._broker.execute_signals(signals,
                                                          "ensemble_fallback")
                    snap = self._broker.snapshot_portfolio()
                return {"ok": True, "signals": len(signals),
                        "filled": result.filled, "rejected": result.rejected,
                        "stop_loss_filled": sl, "snapshot": snap,
                        "evaluations": evaluations,
                        "strategy": "ensemble_fallback"}

        # The LLM leg (debate + judgment + risk triad) is minutes of network
        # I/O and must NOT hold `_broker_lock`; only its execution tail does,
        # via this gate. See `_broker_exec`.
        return run_llm_decision(
            db=self._db, broker=self._broker, goal=goal,
            candidates=fused, llm_client=client, cfg=cfg,
            broker_gate=self._broker_exec,
        ) | {"evaluations": evaluations, "strategy": "llm"}

    # ----------------------------------------------------- the actual cycle
    @contextmanager
    def _broker_exec(self):
        """Critical section around a tick's broker mutations.

        The whole cycle body used to run under `_broker_lock`, which starved
        the 5s intraday guard for as long as a tick took: production logs show
        2026-07-24 10:04:58 → 10:14:07, i.e. 9m09s of an open book with no
        stop-loss, no fill reconcile and no circuit breaker. Now only the
        mutating segments take the lock (fills+breaker, sizer install, exits→
        rotation→execute); the compute stages in between — data refresh,
        screener, factor panel, walk-forward selection, LLM debate/decision —
        run lock-free, so the guard gets in every 5s as designed.

        The guard's intent is preserved: every broker mutation on either side
        still happens under this lock, so tick and guard never mutate
        concurrently. What they may now do is *interleave* — hence the
        breaker re-check: if the guard flattened and halted the agent while we
        were computing, buying back in would undo the circuit breaker.
        """
        with self._broker_lock:
            if self._breaker_flag.is_set():
                raise CycleHalted("组合熔断已触发,本轮放弃下单")
            yield

    def _run_one_cycle(self) -> dict:
        # Tick-vs-tick exclusion only (manual /api tick vs the loop thread).
        # Serialization against the intraday guard is per-mutation now — see
        # `_broker_exec`.
        with self._cycle_lock:
            try:
                return self._run_cycle_body()
            except CycleHalted as e:
                logger.warning("cycle aborted mid-flight: %s", e)
                return {"ok": True, "halted": True, "reason": str(e)}

    def _run_cycle_body(self) -> dict:
        ts = datetime.now().isoformat()
        self._breaker_flag.clear()  # fresh cycle; the breaker re-runs below
        goal = load_goal(self._db)
        # llm_full 模式同步要先于本 tick 的一切 broker 调用(check_exits 的
        # LLM 点位分支、护栏旁路、实时 BUY 都读 broker.llm_managed)。
        is_llm_full = self._sync_llm_mode(goal)

        # FIRST: try to fill any pending orders from prior ticks. This must
        # happen before new signal generation so today's fills update cash
        # / positions before any new sizing decisions are made. Safe to call
        # in immediate-fill mode too — returns 0-scanned and exits.
        with self._broker_exec():
            try:
                pending_result = self._broker.try_fill_pending_orders()
            except AttributeError:
                # Broker without pending support (legacy / test stub).
                pending_result = None

            # Portfolio drawdown circuit breaker — if equity has fallen past the
            # portfolio stop from its high-water mark, flatten everything and
            # halt the agent. Last line of defense before deeper losses; runs
            # before any new signal generation. Same critical section as the
            # fills above so the breaker's read-peak-then-snapshot sequence
            # can't be interleaved by the guard's own snapshot (audit G3/L2).
            try:
                if self._broker.enforce_portfolio_stop():
                    self._breaker_flag.set()
                    summary = "组合回撤熔断：已清仓并暂停 agent"
                    self._db.log_decision("cycle_halt", summary)
                    self.stop()  # disables goal + sets stop flag (no self-join)
                    with self._lock:
                        self._status.last_tick_at = ts
                        self._status.last_tick_summary = summary
                    return {"ok": True, "halted": True, "reason": summary}
            except AttributeError:
                pass  # broker without circuit-breaker support (test stub)

        universe = self._resolve_universe(goal)
        if not universe:
            summary = "宇宙为空:请先 sync stocks 或选择一个非空 pool"
            self._db.log_decision("cycle_skip", summary,
                                  details={"goal": goal.to_db()})
            with self._lock:
                self._status.last_tick_at = ts
                self._status.last_tick_summary = summary
            return {"ok": False, "reason": summary}

        self._ensure_recent_data(universe)

        # 数据陈旧闸:_ensure_recent_data 是「尽力补」(每 tick 预算 ~20 只),
        # 不是保证。数据源挂掉/回填中断时大面积股票停在旧价上,继续往下就是
        # 拿旧价选股下单。复用 doctor 的 data_freshness 判定,但阈值按「系统
        # 性失效」放宽:≥2 只且 ≥20% 缺失/陈旧才拦——单只停牌股或新建池里
        # 一只没同步过的票不该锁死整个 agent。只拦新信号:持仓离场/止损/熔断
        # 在 tick 开头与盘中守护里已照常跑(H2 教训:数据问题绝不能顺带禁用
        # 止损)。kind 在告警白名单,配了 webhook 会推人。
        try:
            from quanti.health import data_freshness
            fresh = data_freshness(self._db, codes=universe)
            n_bad = int(fresh.get("missing", 0)) + int(fresh.get("stale", 0))
            total = int(fresh.get("total", 0))
            if total > 0 and n_bad >= 2 and n_bad / total >= 0.20:
                summary = (f"数据陈旧闸:宇宙 {total} 只中 {n_bad} 只缺失/陈旧,"
                           f"本 tick 不生成新信号;持仓守护不受影响。"
                           f"{fresh.get('detail', '')}")
                self._db.log_decision("stale_data_skip", summary, details=fresh)
                with self._lock:
                    self._status.last_tick_at = ts
                    self._status.last_tick_summary = summary
                return {"ok": False, "reason": summary}
        except Exception as e:  # noqa: BLE001 - 闸自身失败不拦截,但要可见
            logger.warning("data freshness gate failed (skipped): %s", e)

        # Step 1 of every tick: read the market-regime snapshot (full-market
        # breadth / rotation / turnover, produced once a day at 17:30 by the
        # background syncer) and log it. Observe-only here — it changes nothing
        # about candidate generation or sizing; it only reaches the judge LLM's
        # context unless goal.params["regime_in_prompt"] is explicitly false
        # (both flags default ON — see quanti/regime/prompt.py `enabled`).
        #
        # READ, never compute. `load_latest` is one indexed row (~1ms);
        # `report.generate()` would be 10s of full-market pandas plus an LLM
        # call up to 300s. That no longer blocks the intraday guard (the tick
        # only takes `_broker_lock` for its mutating segments — see
        # `_broker_exec`), but it would still stall the tick's own exits and
        # buys by that long. Never call generate() from inside a tick.
        # 整段(含开关判定)都在 try 里:这是观测,任何异常都不许打掉一个 tick。
        try:
            from quanti.regime.prompt import DETECT_PARAM as _DETECT
            from quanti.regime.prompt import (enabled as _regime_enabled,
                                              fill_mode_ok, latest_usable)
            if _regime_enabled(goal, _DETECT):
                snap, reason = latest_usable(self._db, provider=self._provider)
                if snap is None:
                    self._db.log_decision(
                        "regime", f"无市场 regime 快照({reason});"
                                  f"后台每日 17:30 生成,或手动 POST /api/regime/run")
                else:
                    m = snap.get("metrics") or {}
                    fresh = not reason
                    # 「已注入」必须和 regime_block 的实际条件一致,否则日志
                    # 会骗人。三个条件缺一不可:开关、next-open 成交模式、
                    # 本 tick 确实走 LLM 决策路径(ensemble/单策略路径根本
                    # 没有裁判 LLM 可注入)。
                    p_ = goal.params or {}
                    into_prompt = bool(
                        fresh and _regime_enabled(goal)
                        and fill_mode_ok(self._broker)
                        and str(p_.get("agent_mode", "")).lower() == "llm"
                        and not goal.strategy_name)
                    # 算不出来的指标写「—」,不写 0:「MA50上方 0%」是一个极端
                    # 看空的读数,而真相是「那天没数据」(2026-08-06 就是),
                    # 两件事在事后排查时必须分得开。
                    def _n(v, digits=0, unit="%", sign=""):
                        return ("—" if v is None
                                else f"{float(v):{sign}.{digits}f}{unit}")
                    self._db.log_decision(
                        "regime",
                        f"{snap['date']} {snap.get('rule_label', '')}"
                        f"(投票分 {int(snap.get('rule_score') or 0):+d})"
                        f" · MA50上方 {_n(m.get('above50'))}"
                        f" · 涨跌比 {_n(m.get('ad_ratio'), 2, '')}"
                        f" · 成交额5v20 {_n(m.get('amt_chg'), 1, '%', '+')}"
                        + (f" · {reason}" if reason else "")
                        + (" · 已注入 LLM 上下文" if into_prompt else " · 仅观测"),
                        details={"date": snap["date"],
                                 "rule_label": snap.get("rule_label"),
                                 "rule_score": snap.get("rule_score"),
                                 "llm_regime": snap.get("llm_regime"),
                                 "metrics": m, "stale_reason": reason,
                                 "into_prompt": into_prompt})
        except Exception as e:  # noqa: BLE001 - observability never breaks a tick
            logger.warning(f"regime snapshot read skipped: {e}")

        candidates = self._run_screener(goal, universe)
        if not candidates:
            # No screener (or screener returned nothing): take the top N by
            # 20-day ADV instead of `universe[:30]`. The old slice was
            # *dictionary order*, which biased every no-screener run toward
            # the codes that happened to sort first (000001, 000002, ...).
            # Sort-by-ADV at least gives "the N most liquid" — a defensible
            # default. Override via goal.params["no_screener_take"].
            params_local = goal.params or {}
            take = int(params_local.get("no_screener_take", 100))
            from quanti.agent.universe import sort_by_adv20
            candidates = sort_by_adv20(self._provider, universe)[:take]
            # Observability for the silent beta this fallback fixes: top-N by
            # liquidity == a large-cap book. Pool-width sweep + adversarial
            # validation (2026-07-02, scripts/baseline_returns.py, memory
            # adv-pool-width-findings) settled the "should we widen?" question:
            #   * Equal-weight paper gradient is monotone-wider-is-better
            #     (full market +11.5%/yr Sharpe 0.59 vs ADV100 -2.2%/yr), but
            #     it is EQUAL-WEIGHT SMALL-CAP SIZE BETA, not liquidity
            #     quality: cap-weighting the same pools collapses the gradient
            #     (ADV100 and ADV1000 both ≈ +4%/yr) and full-market
            #     equal-weight capacity is ~¥2.7亿 (untradable microcap tail;
            #     ADV100 ≈ ¥134亿, ADV1000 ≈ ¥264亿).
            #   * Ranking flips by regime — wide pools win only in the
            #     21~23 small-cap bear; micro-cap crash (2024-01: full market
            #     -13%/mo, intramonth -29.7% vs ADV100 -2.7%) and 2026H1
            #     (full market +2.1% vs ADV100 +38.1%) flip to narrow-best.
            # Keep the large-cap default: it's the tradable, tail-protective
            # posture, and the paper alpha it "gives up" cannot absorb real
            # capital. But log it once/day so the operator sees the beta
            # they're running and can widen via no_screener_take — knowing
            # that's a regime bet, not a free optimization.
            today = date.today()
            if self._candidate_source_logged != today:
                self._candidate_source_logged = today
                self._db.log_decision(
                    "candidate_source",
                    f"无 screener:取 ADV 前 {take} 只 = 大盘 beta(防御:微盘踩踏回撤更小)。"
                    f"放宽 no_screener_take 纳中小盘在小盘行情更强、微盘崩盘尾部更大——"
                    f"是 regime 押注、非免费优化;宽池等权的纸面超额是不可交易的"
                    f"小盘 size beta(2026-07 验证,scripts/baseline_returns.py)。",
                    details={"no_screener_take": take, "universe": len(universe)})

        # Observe-only active-vs-passive guardrail: once/day, compare the
        # account's realized return vs equal-weighting the candidate pool over
        # the same window — surfaces quanti's own finding (active loses to
        # passive equal-weight) as a live signal. Never changes a trade.
        today_av = date.today()
        if self._attribution_logged != today_av:
            try:
                from quanti.agent.attribution import active_vs_passive
                av = active_vs_passive(self._db, self._provider, candidates)
                if av is not None:
                    self._attribution_logged = today_av
                    verdict = "跑输被动" if av["lagging"] else "跑赢/持平被动"
                    self._db.log_decision(
                        "pool_vs_passive",
                        f"主动 vs 池内等权({av['days']}日):账户 {av['active']:+.2%} "
                        f"vs 等权 {av['passive']:+.2%}(差 {av['gap']:+.2%}, "
                        f"{av['n_names']}只)→ {verdict}",
                        details=av)
            except Exception as e:  # noqa: BLE001 - observability never breaks a tick
                logger.warning("active_vs_passive attribution skipped: %s", e)

        # Clear any equal-weight sizer left over from a prior ensemble cycle;
        # the ensemble path re-installs it if equal_weight is still on. Without
        # this, switching to single/LLM mode would keep sizing equal-weight.
        self._set_cycle_sizer(None)

        # Decide path among three options:
        #   * "llm":      ensemble candidates → Claude judgment (P3).
        #   * ensemble:   ensemble candidates → direct execute (P2).
        #   * single:     pin or auto-select one strategy (legacy).
        # A pinned strategy ALWAYS wins — it's the user's explicit override.
        params = goal.params or {}
        agent_mode = str(params.get("agent_mode", "")).lower()
        if agent_mode in ("llm", "llm_full") and not goal.strategy_name:
            # llm_full 被拒(实盘)时 is_llm_full=False → 降级走普通 llm 终审。
            llm_result = (self._llm_full_path(goal, candidates) if is_llm_full
                          else self._llm_path(goal, candidates))
            # Maintain the cycle-end status snapshot so the UI stays fresh.
            with self._lock:
                self._status.last_tick_at = ts
                self._status.last_tick_summary = (
                    f"LLM {'全权' if is_llm_full else ''}决策: "
                    f"{llm_result.get('filled', 0)} 成交, "
                    f"{llm_result.get('rejected', 0)} 拒绝")
                self._status.last_strategy = llm_result.get(
                    "strategy", "llm_full" if is_llm_full else "llm")
                self._status.last_evaluations = llm_result.get("evaluations", [])
                snap = llm_result.get("snapshot") or self._broker.snapshot_portfolio()
                self._status.total_value = snap.get("total_value", 0)
                self._status.pnl_pct = snap.get("pnl_pct", 0)
            return llm_result

        ensemble_enabled = (bool(params.get("ensemble_enabled", False))
                            and not goal.strategy_name)

        evaluations: list[dict] = []
        signals: list[Signal] = []
        strategy_name: str = ""
        fused: list = []  # FusedCandidate list; non-empty only in ensemble mode

        if ensemble_enabled:
            signals, strategy_name, evaluations, fused = self._ensemble_path(
                goal, candidates)
            if signals is None:
                summary = "ensemble 模式未能产出信号"
                self._db.log_decision("cycle_skip", summary,
                                      details={"evaluations": evaluations})
                return {"ok": False, "reason": summary}
        elif goal.strategy_name:
            loader = StrategyLoader()
            strategies = loader.load_directory(self._strategies_dir)
            strategy = next((s for s in strategies
                             if s.name == goal.strategy_name), None)
            if strategy is None:
                summary = f"指定策略 {goal.strategy_name} 未找到"
                self._db.log_decision("cycle_skip", summary)
                return {"ok": False, "reason": summary}
            strategy.init(resolve_params(self._db, strategy.name, goal))
            strategy_name = strategy.name
            self._db.log_decision(
                "strategy_pin",
                f"使用钉选策略：{strategy.name}",
                details={"strategy": strategy.name, "params": goal.params or {}})
            signals = self._single_strategy_signals(strategy, candidates)
        else:
            # Re-pick at most once per _reselect_interval. In between, the
            # cached winner is re-loaded as a fresh instance so its internal
            # state starts clean.
            now_ts = time.time()
            cached = self._selector_cache
            needs_pick = (cached is None
                          or now_ts - cached[0] >= self._reselect_interval)
            if needs_pick:
                selector = StrategySelector(
                    self._db, self._provider, self._strategies_dir)
                strategy, ranking = selector.pick_best(goal, candidates)
                evaluations = [e.as_dict() for e in ranking]
                if strategy is None:
                    summary = "未能选出策略"
                    self._db.log_decision("cycle_skip", summary,
                                          details={"evaluations": evaluations})
                    return {"ok": False, "reason": summary}
                self._selector_cache = (now_ts, strategy.name, evaluations)
                strategy.init(resolve_params(self._db, strategy.name, goal))
                self._db.log_decision(
                    "strategy_pick",
                    f"选定策略：{strategy.name}",
                    details={"evaluations": evaluations, "goal": goal.to_db()})
            else:
                # Use the cached winner. Load a fresh instance so per-tick
                # state (e.g. price buffers in MA strategies) isn't carried
                # across.
                cached_name = cached[1]
                evaluations = cached[2]
                loader = StrategyLoader()
                strategies = loader.load_directory(self._strategies_dir)
                strategy = next((s for s in strategies if s.name == cached_name),
                                None)
                if strategy is None:
                    # Strategy file got removed — invalidate cache and recurse.
                    self._selector_cache = None
                    return self._run_one_cycle()
                strategy.init(resolve_params(self._db, strategy.name, goal))
            strategy_name = strategy.name
            signals = self._single_strategy_signals(strategy, candidates)

        # Filter SELL signals for codes we don't actually hold — strategies
        # maintain their own state during historical replay and may emit
        # sells echoing "positions" that only existed in the backtest. Real
        # sells should come from RiskManager.check_stop_loss() or live
        # position-tracking signals.
        # One critical section for everything that touches the book: the guard
        # must not slip an exit between our position read and our orders.
        with self._broker_exec():
            held = {p["code"] for p in self._db.list_positions()}
            signals = [s for s in signals
                       if not (s.direction == Direction.SELL
                               and s.stock_code not in held)]

            # Risk exits first (stop-loss / strategy / take-profit) so we free
            # cash before new buys.
            sl_count = self._broker.check_exits()

            # Score-gated rotation (换仓, opt-in): if the book is full, free the
            # weakest holding so a clearly-stronger fresh candidate can be
            # funded. Runs AFTER check_exits so it sees the post-exit cash/
            # positions and won't target a name already exiting. Only the scored
            # ensemble path populates `fused`.
            if fused:
                self._maybe_rotate(fused, [c.code for c in fused])

            # Execute
            result = self._broker.execute_signals(signals,
                                                  strategy_name=strategy_name)
            snap = self._broker.snapshot_portfolio()

        # Pending mode can mix outcomes in one batch: in-session SELLs fill
        # immediately (live-mirror) while BUYs queue — surface both counts.
        # `pending_result` (from the start of this tick) shows what filled
        # from PRIOR ticks' queue at today's open.
        landed_bits = ([f"成交 {result.filled}"] if result.filled else []) \
            + ([f"挂单 {result.pending}"] if result.pending else [])
        landed_label = "/".join(landed_bits) or "成交 0"
        pre_filled = pending_result.filled if pending_result else 0
        pre_pending = pending_result.still_pending if pending_result else 0
        pre_expired = pending_result.expired if pending_result else 0

        summary_parts = [f"策略 {strategy_name}", f"信号 {len(signals)}", landed_label,
                         f"拒绝 {result.rejected}", f"离场 {sl_count}"]
        if pending_result and pending_result.scanned > 0:
            summary_parts.append(
                f"昨日挂单成交 {pre_filled}/待 {pre_pending}/过期 {pre_expired}")
        summary_parts.append(
            f"净值 ¥{snap['total_value']:,.0f} ({snap['pnl_pct']:+.2%})")
        summary = ", ".join(summary_parts)

        self._db.log_decision(
            "cycle", summary,
            details={
                "strategy": strategy_name, "signals": len(signals),
                "filled": result.filled, "pending": result.pending,
                "rejected": result.rejected,
                "stop_loss_filled": sl_count,
                "pending_pre_filled": pre_filled,
                "pending_pre_still": pre_pending,
                "pending_pre_expired": pre_expired,
                "total_value": snap["total_value"],
                "pnl_pct": snap["pnl_pct"],
                "evaluations": evaluations,
            })

        # Tail-of-cycle housekeeping: keep the decision log bounded so the DB
        # doesn't grow without limit. At ~6 cycles/day this caps audit history
        # to ~540 cycles (~3 months) by default.
        try:
            removed = self._db.prune_decisions(self._decision_retention_days)
            if removed:
                logger.info(f"Pruned {removed} decision-log rows older than "
                            f"{self._decision_retention_days}d")
        except Exception as e:
            logger.warning(f"prune_decisions failed: {e}")

        with self._lock:
            self._status.last_tick_at = ts
            self._status.last_tick_summary = summary
            self._status.last_strategy = strategy_name
            self._status.last_evaluations = evaluations
            self._status.total_value = snap["total_value"]
            self._status.pnl_pct = snap["pnl_pct"]
        return {
            "ok": True, "summary": summary,
            "strategy": strategy_name, "signals": len(signals),
            "filled": result.filled, "rejected": result.rejected,
            "stop_loss_filled": sl_count,
            "snapshot": snap, "evaluations": evaluations,
        }
