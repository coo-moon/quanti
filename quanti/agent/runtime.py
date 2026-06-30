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
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from quanti.agent.goal import Goal, load_goal, save_goal
from quanti.agent.params import resolve_params
from quanti.agent.selector import StrategySelector
from quanti.agent.signal_pipeline import (
    collect_signals_per_strategy,
    filter_affordable,
    filter_by_threshold,
    fuse_buy_signals,
    industry_cap,
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
        self._thread: threading.Thread | None = None
        self._guard_thread: threading.Thread | None = None
        # Serializes broker mutations between the full tick and the intraday
        # guard thread (RLock: the in-cycle cache-miss recursion re-enters).
        self._broker_lock = threading.RLock()
        self._stop_flag = threading.Event()
        self._status = AgentStatus()
        self._lock = threading.Lock()

    # ------------------------------------------------------------- public
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
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
        for th in (self._thread, self._guard_thread):
            if th is not None:
                try:
                    th.join(timeout=2)
                except Exception:
                    pass

    def guard_status(self) -> dict:
        """Intraday-guard daemon state for the live status card."""
        return {
            "enabled": self._intraday_guard_sec > 0,
            "interval_sec": self._intraday_guard_sec,
            "running": bool(self._guard_thread and self._guard_thread.is_alive()),
        }

    def _guard_loop(self) -> None:
        """Fast intraday guard (live only): every `intraday_guard_sec`, reconcile
        venue fills + run exits + the portfolio circuit breaker — so async fills
        and stop-losses are caught in ~1 min instead of waiting for the 4h full
        tick. Idles outside trading sessions / when the venue isn't connected, so
        marks always ride xtdata realtime quotes via the bridge, never stale data."""
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
        # Only act when the live venue is actually connected — exits/marks must
        # ride xtdata realtime quotes via the bridge, never stale/mock data.
        is_conn = getattr(self._broker, "is_connected", None)
        if callable(is_conn) and not is_conn():
            return
        with self._broker_lock:  # never overlap the full tick's broker mutations
            try:
                self._broker.try_fill_pending_orders()
            except Exception as e:  # noqa: BLE001
                logger.warning("guard fill reconcile failed: %s", e)
            try:
                if self._broker.enforce_portfolio_stop():
                    self._db.log_decision("cycle_halt", "盘中组合熔断:已清仓并暂停 agent")
                    self.stop()
                    return
            except Exception as e:  # noqa: BLE001
                logger.warning("guard portfolio-stop failed: %s", e)
            try:
                self._broker.check_exits()
            except Exception as e:  # noqa: BLE001
                logger.warning("guard check_exits failed: %s", e)

    def restart(self) -> None:
        """Reschedule cleanly: stop the running loop thread (joining it) then
        start a fresh one, WITHOUT flipping persisted goal.enabled. Used when
        the daily schedule changes so a new daily_run_time takes effect
        immediately. Safe to call when not running (acts like start())."""
        self.shutdown()
        self.start()

    def status(self) -> AgentStatus:
        with self._lock:
            snap = self._broker.snapshot_portfolio()
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

        missing: list[str] = []
        for c in ordered:
            bars = self._provider.get_daily_bars(c, start, end)
            if not bars or bars[-1].date < stale_after:
                missing.append(c)
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
        self, goal: Goal, candidates: list[str],
    ) -> tuple[list, list[dict], dict[str, float]]:
        """Run top-K Selector + factor pipeline → FusedCandidate list.

        Shared between the rule-ensemble and LLM paths. Returns
        (fused_candidates, evaluations, strategy_weights). Empty list
        means "no actionable candidates this cycle".

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

        selector = StrategySelector(self._db, self._provider,
                                    self._strategies_dir)
        pairs, ranking = selector.pick_topk(goal, candidates, k=k)
        evaluations = [e.as_dict() for e in ranking]
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

        if industry_neutral:
            fused = industry_cap(fused, n_per_industry=n_per_industry)
        fused = filter_by_threshold(fused, threshold=threshold)

        # Affordability: drop names whose one A股-lot (100 股) costs more than the
        # single-stock cap room — they'd size to 0 lots and be rejected anyway,
        # so feeding them to the LLM/ensemble is noise (the LLM, blind to the
        # cash/lot constraint, kept re-picking unaffordable high-priced names).
        # Read state directly — snapshot_portfolio() would persist a snapshot row.
        risk_cfg = getattr(getattr(self._broker, "_risk", None), "config", None)
        max_pos_pct = float(getattr(risk_cfg, "max_position_pct", 0.0) or 0.0)
        state = self._db.get_portfolio_state()
        if fused and max_pos_pct > 0 and state:
            held_value = {
                p["code"]: p["quantity"] * (p["current_price"] or p["avg_cost"])
                for p in self._db.list_positions()}
            total_value = state["cash"] + sum(held_value.values())
            if total_value > 0:
                end = date.today()

                def _last_close(code: str) -> float:
                    try:
                        bars = self._provider.get_daily_bars(
                            code, end - timedelta(days=10), end)
                        return bars[-1].close if bars else 0.0
                    except Exception:  # noqa: BLE001 - a bad code can't break the cycle
                        return 0.0

                fused, dropped = filter_affordable(
                    fused, total_value, max_pos_pct, held_value, _last_close)
                if dropped:
                    self._db.log_decision(
                        "affordability_filter",
                        f"过滤 {len(dropped)} 只 1 手买不起的票(单票限额 "
                        f"{max_pos_pct:.0%}×{total_value:,.0f}≈"
                        f"{total_value * max_pos_pct:,.0f} 元): "
                        f"{', '.join(dropped[:10])}"
                        f"{'…' if len(dropped) > 10 else ''}",
                        details={"dropped": dropped,
                                 "max_position_pct": max_pos_pct,
                                 "total_value": round(total_value, 2)})

        # Breadth cap: keep only the top-N by final_score. Lets the user run a
        # deliberately diversified book (e.g. 20 equal-weight names) instead of
        # the emergent ~4-5 the 20% per-name cap allows by default. 0 = off.
        # Note: this caps NEW buys only; held names that fall out of top-N are
        # not rotated out (exits stay stop/strategy-driven) — by design.
        max_holdings = int(params.get("max_holdings", 0))
        if max_holdings > 0:
            fused = fused[:max_holdings]

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
                       ) -> tuple[list[Signal], str, list[dict]]:
        """Rule-driven ensemble: candidates → signals (no LLM)."""
        fused, evaluations, _weights = self._compute_fused_candidates(
            goal, candidates)
        params = goal.params or {}
        equal_weight = bool(params.get("equal_weight", False)) and bool(fused)
        if not equal_weight:
            self._set_cycle_sizer(None)
            return [c.to_signal() for c in fused], "ensemble", evaluations

        # Passive equal-weight book: each held name targets the SAME weight via
        # a FixedSizer, so capital isn't front-loaded into the top ranks.
        weight_per_name = 1.0 / len(fused)
        if not self._set_cycle_sizer(weight_per_name):
            # Broker has no equal-weight sizer (e.g. live QmtBroker sizes via
            # cash%/risk-cap). Fall back to conviction-weighted sizing rather
            # than forcing strength=1.0 onto a sizer that ignores it.
            return [c.to_signal() for c in fused], "ensemble", evaluations

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
        return signals, "ensemble", evaluations

    def _set_cycle_sizer(self, weight_per_name: float | None) -> bool:
        """Install (or clear) an equal-weight FixedSizer on the broker for this
        cycle. Returns True iff an equal-weight sizer is now active.

        On install we snapshot the broker's prior sizer and restore it on
        reset, so a sizer injected at broker construction survives an
        equal-weight toggle. No-op returning False on brokers without a sizer
        slot (e.g. QmtBroker, which sizes via cash%/risk-cap and ignores
        Sizer)."""
        setter = getattr(self._broker, "set_sizer", None)
        if setter is None:
            return False
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
                sl = self._broker.check_exits()
                result = self._broker.execute_signals(signals, "ensemble_fallback")
                snap = self._broker.snapshot_portfolio()
                return {"ok": True, "signals": len(signals),
                        "filled": result.filled, "rejected": result.rejected,
                        "stop_loss_filled": sl, "snapshot": snap,
                        "evaluations": evaluations,
                        "strategy": "ensemble_fallback"}

        return run_llm_decision(
            db=self._db, broker=self._broker, goal=goal,
            candidates=fused, llm_client=client, cfg=cfg,
        ) | {"evaluations": evaluations, "strategy": "llm"}

    # ----------------------------------------------------- the actual cycle
    def _run_one_cycle(self) -> dict:
        # Serialize with the intraday guard thread — both mutate broker state
        # (fills / exits / portfolio stop), so they must never run concurrently.
        with self._broker_lock:
            return self._run_cycle_body()

    def _run_cycle_body(self) -> dict:
        ts = datetime.now().isoformat()
        goal = load_goal(self._db)

        # FIRST: try to fill any pending orders from prior ticks. This must
        # happen before new signal generation so today's fills update cash
        # / positions before any new sizing decisions are made. Safe to call
        # in immediate-fill mode too — returns 0-scanned and exits.
        try:
            pending_result = self._broker.try_fill_pending_orders()
        except AttributeError:
            # Broker without pending support (legacy / test stub).
            pending_result = None

        # Portfolio drawdown circuit breaker — if equity has fallen past the
        # portfolio stop from its high-water mark, flatten everything and halt
        # the agent. Last line of defense before deeper losses; runs before any
        # new signal generation.
        try:
            if self._broker.enforce_portfolio_stop():
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

        # Observe-only regime detection (v1): logs a `regime` decision so we can
        # eyeball whether it classifies trend/range/high-vol correctly. Does NOT
        # change candidate generation or sizing. Gated; never raises.
        if (goal.params or {}).get("regime_detect"):
            try:
                from quanti.agent.regime import detect_regime, last_regime_label
                rs = detect_regime(self._provider, date.today(),
                                   universe=universe,
                                   prev_label=last_regime_label(self._db))
                self._db.log_decision("regime", rs.summary(), details=rs.as_dict())
            except Exception as e:
                logger.warning(f"regime detect skipped: {e}")

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
            # liquidity == a large-cap book. Regime-split validation
            # (2026-06-26, scripts/breadth_regime.py) shows breadth is a
            # regime-dependent BETA CHOICE, not a free optimization — the
            # ADV100/300/1000 ranking flips by regime:
            #   micro-cap crash (2024-01): ADV100 -14% vs ADV1000 -31% (large
            #     caps protect hard);  large-cap recovery (24-07~): ADV100 +43%
            #     vs ADV1000 +36%;  small-cap regime (21~23): ADV1000 -19% vs
            #     ADV100 -27%;  full 5y: ~tied (ADV1000 -2.5% / -53% DD vs
            #     ADV100 -3.2% / -58% DD).
            # Keep the large-cap default: it's the tail-protective posture and
            # this system's edge is drawdown control, not alpha. But log it
            # once/day so the operator sees the beta they're running and can
            # widen via no_screener_take if they want small-cap exposure.
            today = date.today()
            if self._candidate_source_logged != today:
                self._candidate_source_logged = today
                self._db.log_decision(
                    "candidate_source",
                    f"无 screener:取 ADV 前 {take} 只 = 大盘 beta(防御:微盘踩踏回撤更小)。"
                    f"放宽 no_screener_take 纳中小盘在小盘行情更强、微盘崩盘尾部更大——"
                    f"是 regime 押注、非免费优化。",
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
        if agent_mode == "llm" and not goal.strategy_name:
            llm_result = self._llm_path(goal, candidates)
            # Maintain the cycle-end status snapshot so the UI stays fresh.
            with self._lock:
                self._status.last_tick_at = ts
                self._status.last_tick_summary = (
                    f"LLM 决策: {llm_result.get('filled', 0)} 成交, "
                    f"{llm_result.get('rejected', 0)} 拒绝")
                self._status.last_strategy = llm_result.get("strategy", "llm")
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

        if ensemble_enabled:
            signals, strategy_name, evaluations = self._ensemble_path(
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
        held = {p["code"] for p in self._db.list_positions()}
        signals = [s for s in signals
                   if not (s.direction == Direction.SELL and s.stock_code not in held)]

        # Risk exits first (stop-loss / strategy / take-profit) so we free
        # cash before new buys.
        sl_count = self._broker.check_exits()

        # Execute
        result = self._broker.execute_signals(signals, strategy_name=strategy_name)
        snap = self._broker.snapshot_portfolio()

        # In pending mode, `result.filled` is 0 and `result.pending` is the
        # count of queued signals. Surface both so users see the lifecycle.
        # `pending_result` (from the start of this tick) shows what filled
        # from PRIOR ticks' queue at today's open.
        landed_label = f"成交 {result.filled}" if result.filled else f"挂单 {result.pending}"
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
