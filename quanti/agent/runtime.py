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
from quanti.execution.base import Broker
from quanti.factors.cross_sectional import FactorConfig, compute_factor_panel
from quanti.models import Direction, Signal
from quanti.screener.loader import ScreenerLoader
from quanti.strategy.loader import StrategyLoader

logger = logging.getLogger(__name__)


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
        # Universe cache: (today, config_key, filtered_codes). Liquidity /
        # age / name filters only change daily, so we cache the filtered
        # list and reuse for ticks within the same trading day. See
        # _cached_tradable_universe for details.
        self._universe_cache: tuple[date, tuple, list[str]] | None = None
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
        from quanti.agent.universe import UniverseBuilder, UniverseConfig

        cfg = UniverseConfig(
            min_adv20_yuan=float(params.get("universe_min_adv20",
                                             UniverseConfig.min_adv20_yuan)),
            min_active_days_60=int(params.get("universe_min_active_days",
                                              UniverseConfig.min_active_days_60)),
            min_age_days=int(params.get("universe_min_age_days",
                                        UniverseConfig.min_age_days)),
        )
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
            strat.init(dict(params))
            prepared.append((strat, weight))

        per_strategy, weights = collect_signals_per_strategy(
            prepared, candidates, self._provider)

        panel = compute_factor_panel(
            self._provider, self._db, candidates,
            config=FactorConfig(industry_neutralize=industry_neutral))

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
        signals = [c.to_signal() for c in fused]
        return signals, "ensemble", evaluations

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
