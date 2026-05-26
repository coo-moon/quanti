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
from quanti.agent.selector import StrategySelector
from quanti.agent.signal_pipeline import (
    collect_signals_per_strategy,
    filter_by_threshold,
    fuse_buy_signals,
    industry_cap,
)
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.paper_broker import PaperBroker
from quanti.factors.cross_sectional import FactorConfig, compute_factor_panel
from quanti.models import BarData, Direction, Signal
from quanti.screener.loader import ScreenerLoader
from quanti.strategy.loader import StrategyLoader

logger = logging.getLogger(__name__)


@dataclass
class AgentStatus:
    enabled: bool = False
    running: bool = False
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
        broker: PaperBroker,
        strategies_dir: str | Path = "strategies",
        screeners_dir: str | Path = "screeners",
        tick_interval_sec: int = 60 * 60 * 4,  # 4h between cycles by default
        decision_retention_days: int = 90,
        selector_reselect_interval_sec: int = 60 * 60 * 24,  # 24h
    ) -> None:
        self._db = db
        self._provider = provider
        self._broker = broker
        self._strategies_dir = str(strategies_dir)
        self._screeners_dir = str(screeners_dir)
        self._tick_interval = tick_interval_sec
        self._decision_retention_days = decision_retention_days
        self._reselect_interval = selector_reselect_interval_sec
        # Selector cache: (timestamp, strategy_name, evaluations). The
        # Selector runs at most once per `_reselect_interval`. In between we
        # reuse the cached pick — saves running a 6-strategy backtest sweep
        # on every 4h tick (~9× faster ticks once cache is warm).
        self._selector_cache: tuple[float, str, list[dict]] | None = None
        self._thread: threading.Thread | None = None
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
        with self._lock:
            self._status.enabled = True
            self._status.running = True
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
        try:
            self._thread.join(timeout=2)
        except Exception:
            pass

    def status(self) -> AgentStatus:
        with self._lock:
            snap = self._broker.snapshot_portfolio()
            self._status.total_value = snap["total_value"]
            self._status.pnl_pct = snap["pnl_pct"]
            return AgentStatus(
                enabled=self._status.enabled,
                running=self._thread.is_alive() if self._thread else False,
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
        # Run immediately so the user gets feedback without waiting.
        self._safe_cycle()
        while not self._stop_flag.is_set():
            if self._stop_flag.wait(self._tick_interval):
                break
            self._safe_cycle()

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
        if goal.universe_pool:
            codes = self._db.get_pool_codes(goal.universe_pool)
            if codes:
                return codes
        # Fall back to anything we already have quotes for.
        codes = self._provider.get_all_codes()
        if codes:
            return codes
        return []

    def _ensure_recent_data(self, codes: list[str], lookback_days: int = 200) -> None:
        """Trigger an AkShare sync for any code missing recent bars. Bounded.

        Two priorities:
          1. Currently held positions go first — they drive the mark-to-market
             pnl on the dashboard, so stale prices visibly "freeze" the UI.
          2. Other candidates fill the rest of the per-tick budget.

        Freshness: a bar from before yesterday is considered stale (was 7
        days, which left positions frozen for nearly a trading week).
        """
        from quanti.data.akshare_adapter import AkShareAdapter
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

        adapter = AkShareAdapter(self._db)
        for c in missing:
            try:
                adapter.sync_daily_quotes(c, start=start, end=end,
                                          repair_gaps=False)
            except Exception as e:
                logger.warning(f"Agent data refresh failed for {c}: {e}")

    def _run_screener(self, goal: Goal, codes: list[str]) -> list[str]:
        if not goal.screener_name:
            return codes
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
        return [c for c, _ in scored[:20]]  # top-20 candidates

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

        selector = StrategySelector(self._db, self._provider,
                                    self._strategies_dir)
        pairs, ranking = selector.pick_topk(goal, candidates, k=k)
        evaluations = [e.as_dict() for e in ranking]
        if not pairs:
            return [], evaluations, {}

        prepared: list[tuple] = []
        for strat, weight in pairs:
            strat.init(dict(params))
            prepared.append((strat, weight))

        per_strategy, weights = collect_signals_per_strategy(
            prepared, candidates, self._provider)

        panel = compute_factor_panel(
            self._provider, self._db, candidates,
            config=FactorConfig(industry_neutralize=industry_neutral))

        fused = fuse_buy_signals(per_strategy, weights,
                                 factor_panel=panel,
                                 factor_blend=factor_blend)

        if industry_neutral:
            fused = industry_cap(fused, n_per_industry=n_per_industry)
        fused = filter_by_threshold(fused, threshold=threshold)

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

    def _ensemble_path(self, goal: Goal, candidates: list[str],
                       ) -> tuple[list[Signal], str, list[dict]]:
        """Rule-driven ensemble: candidates → signals (no LLM)."""
        fused, evaluations, _weights = self._compute_fused_candidates(
            goal, candidates)
        signals = [c.to_signal() for c in fused]
        return signals, "ensemble", evaluations

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
            AnthropicLLMClient,
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
            model=str(params.get("llm_model", "claude-sonnet-4-5")),
            max_tokens=int(params.get("llm_max_tokens", 4096)),
            max_tool_iterations=int(params.get("llm_max_iterations", 5)),
            max_candidates_in_context=int(params.get("llm_max_candidates", 20)),
            temperature=float(params.get("llm_temperature", 0.3)),
        )

        # Allow tests / advanced users to inject a custom client via the
        # runtime attribute. Default to the Anthropic SDK; if its import
        # fails (no [llm] extra installed), fall back to ensemble path.
        client = getattr(self, "_llm_client", None)
        if client is None:
            try:
                client = AnthropicLLMClient()
            except ImportError as e:
                logger.warning(f"LLM unavailable, falling back to ensemble: {e}")
                self._db.log_decision(
                    "llm_unavailable",
                    "LLM 未安装,降级到 ensemble 路径",
                    details={"error": str(e)})
                signals = [c.to_signal() for c in fused]
                sl = self._broker.check_stop_loss()
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
        ts = datetime.now().isoformat()
        goal = load_goal(self._db)
        universe = self._resolve_universe(goal)
        if not universe:
            summary = "宇宙为空：请先 sync stocks 或选择一个非空 pool"
            self._db.log_decision("cycle_skip", summary,
                                  details={"goal": goal.to_db()})
            with self._lock:
                self._status.last_tick_at = ts
                self._status.last_tick_summary = summary
            return {"ok": False, "reason": summary}

        self._ensure_recent_data(universe)
        candidates = self._run_screener(goal, universe)
        if not candidates:
            candidates = universe[:30]

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
            strategy.init(goal.params or {})
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
                strategy.init(goal.params or {})
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
                strategy.init(goal.params or {})
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

        # Risk stop-loss first (so we free cash before new buys)
        sl_count = self._broker.check_stop_loss()

        # Execute
        result = self._broker.execute_signals(signals, strategy_name=strategy_name)
        snap = self._broker.snapshot_portfolio()

        summary = (f"策略 {strategy_name}: 信号 {len(signals)}, 成交 {result.filled}, "
                   f"拒绝 {result.rejected}, 止损 {sl_count}, 净值 ¥{snap['total_value']:,.0f} "
                   f"({snap['pnl_pct']:+.2%})")
        self._db.log_decision(
            "cycle", summary,
            details={
                "strategy": strategy_name, "signals": len(signals),
                "filled": result.filled, "rejected": result.rejected,
                "stop_loss_filled": sl_count,
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
