"""Pick the best strategy for a given universe + Goal.

Two evaluation modes:

  * **walk-forward** (default since 2026-05-25): the strategy is backtested
    on N disjoint out-of-sample windows. The score is dominated by OOS
    metrics + a consistency bonus across folds. This is what you want when
    you're choosing between strategies for real money.

  * **in-sample** (legacy, opt-in via `goal.params["wf_enabled"]=False`):
    backtest the strategy on the most recent `training_days`. Faster but
    overfits — kept for backwards compatibility and for the case where the
    universe doesn't have enough history for walk-forward.

The "best" score blends:
  - return distance to the user's target_annual_return,
  - drawdown vs the user's max_drawdown ceiling,
  - Sharpe,
  - OOS consistency (only in walk-forward mode),
  - weighted by RiskTolerance.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

from quanti.agent.goal import Goal, RiskTolerance
from quanti.agent.params import resolve_params
from quanti.agent.walk_forward import _MIN_POPULATED_FOLDS, run_walk_forward
from quanti.backtest.engine import BacktestEngine
from quanti.backtest.overfit import (
    deflated_sharpe_from_stats,
    deflated_sharpe_ratio,
    sharpe_per_obs,
)
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.risk.manager import RiskConfig, RiskManager
from quanti.risk.protections import ProtectionManager
from quanti.strategy.base import BaseStrategy
from quanti.utils.parallel import thread_map
from quanti.strategy.loader import StrategyLoader

logger = logging.getLogger(__name__)

# Minimum OOS trades for a walk-forward result to be trusted. Below this, the
# OOS Sharpe is sampling noise — it neither earns the Sharpe/consistency score
# components nor gets softmax capital weight (overrideable via goal.params
# "wf_min_oos_trades"). Real money shouldn't ride a 2-trade Sharpe.
_MIN_OOS_TRADES = 10

# Periods/year used to annualize Sharpe (metrics.annualized_sharpe uses 252).
# DSR math is per-period, so we divide the stored *annualized* oos_sharpe back
# by √252 before feeding it to the deflated-Sharpe estimator. Getting this
# wrong mis-calibrates DSR badly (a √252≈15.9× error in the Sharpe).
_TRADING_DAYS = 252.0

# Below this the pooled-OOS DSR gate is off: the softmax winner keeps its
# multiple-testing haircut only when we tried enough distinct strategies for
# expected_max_sharpe to mean anything (needs ≥2 trials to have any dispersion).
_MIN_DSR_TRIALS = 2


@dataclass
class StrategyEvaluation:
    strategy_name: str
    annual_return: float            # IS / single-window return
    max_drawdown: float
    sharpe: float
    total_trades: int
    score: float
    # Walk-forward fields. Populated only when walk_forward ran successfully.
    # `oos_annual_return` of 0.0 with `n_folds=0` means "no WF data available".
    oos_annual_return: float = 0.0
    oos_max_drawdown: float = 0.0
    oos_sharpe: float = 0.0
    oos_consistency: float = 0.0
    n_folds: int = 0
    # Folds that actually produced OOS returns. The independent-sample
    # guardrail (_aggregate zeroes oos_sharpe below _MIN_POPULATED_FOLDS) hinges
    # on this, so the DSR path reuses it instead of re-deriving trust from the
    # raw pooled series (see _per_obs_sharpe / _winner_dsr).
    n_populated_folds: int = 0
    oos_trades: int = 0   # total OOS trades across folds (confidence guard)
    # Observation count backing the Sharpe used for DSR: pooled OOS bars (WF)
    # or IS trading days (no-WF). Real T for the deflated-Sharpe estimator.
    n_obs: int = 0
    # Pooled OOS daily returns across folds. Kept off as_dict() (audit bloat);
    # feeds the most-accurate DSR path (real skew/kurt) in pick_topk.
    oos_returns: list[float] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "strategy_name": self.strategy_name,
            "annual_return": self.annual_return,
            "max_drawdown": self.max_drawdown,
            "sharpe": self.sharpe,
            "total_trades": self.total_trades,
            "score": self.score,
            "oos_annual_return": self.oos_annual_return,
            "oos_max_drawdown": self.oos_max_drawdown,
            "oos_sharpe": self.oos_sharpe,
            "oos_consistency": self.oos_consistency,
            "n_folds": self.n_folds,
            "n_populated_folds": self.n_populated_folds,
            "oos_trades": self.oos_trades,
            "n_obs": self.n_obs,
        }


class StrategySelector:
    """Backtest each candidate strategy on a code universe and pick the best."""

    def __init__(
        self,
        db: Database,
        provider: DataProvider,
        strategies_dir: str | Path = "strategies",
        training_days: int = 365,
        initial_cash: float = 1_000_000.0,
    ) -> None:
        self._db = db
        self._provider = provider
        self._strategies_dir = str(strategies_dir)
        self._training_days = training_days
        self._initial_cash = initial_cash

    # --------------------------------------------------------------- API
    def load_candidates(self) -> list[BaseStrategy]:
        loader = StrategyLoader()
        return loader.load_directory(self._strategies_dir)

    def evaluate(self, goal: Goal, codes: list[str],
                 candidates: Iterable[BaseStrategy] | None = None,
                 as_of: date | None = None,
                 ) -> list[StrategyEvaluation]:
        # `as_of` runs the whole evaluation (IS window + walk-forward folds) as
        # of a historical date instead of today — used by scripts/dsr_calibration.py
        # to replay past selector decisions on the exact production path. Default
        # None → today, so live behavior is unchanged.
        candidates = list(candidates) if candidates is not None else self.load_candidates()
        if not candidates:
            return []
        if not codes:
            return []

        params = goal.params or {}
        wf_enabled = bool(params.get("wf_enabled", True))
        n_folds = int(params.get("wf_n_folds", 3))
        warmup_days = int(params.get("wf_warmup_days", 120))
        # Half-year OOS blocks by default (was 21d): ≥60 trading days so each
        # fold's annual_return actually annualizes (metrics._MIN_DAYS_TO_ANNUALIZE)
        # and each block holds enough obs to matter.
        test_days = int(params.get("wf_test_days", 126))
        # "Eat the full history": tile OOS blocks across ALL available data
        # instead of just the last few. The Selector's walk-forward is pure OOS
        # (strategies have no fittable params, only a warmup tail), so there is
        # no train window to protect — spanning the whole sample is safe and
        # lets the OOS Sharpe see multiple regimes/cycles, not one recent quarter.
        wf_full_history = bool(params.get("wf_full_history", True))
        wf_max_folds = int(params.get("wf_max_folds", 40))

        end = as_of or date.today()
        history_start = None
        if wf_full_history:
            earliest = self._db.get_global_earliest_quote_date()
            # Require enough span for at least _MIN_POPULATED_FOLDS blocks — the
            # same floor _aggregate needs to TRUST the pooled Sharpe. Gating on
            # only warmup+test would admit a span that yields a single fold,
            # which the guardrail then zeroes: full-history work, no OOS signal,
            # and worse than the fixed n_folds path. None (empty/short DB) falls
            # back to that fixed path.
            min_span_days = warmup_days + _MIN_POPULATED_FOLDS * test_days
            if earliest is not None and (end - earliest).days > min_span_days:
                history_start = earliest
        # In-sample window still computed for tie-break and as a fallback.
        is_start = end - timedelta(days=self._training_days)
        # Score strategies under the SAME exit policy they'll trade live
        # (stop-loss + trailing take-profit + position caps). Without this the
        # Selector ranked raw strategy alpha while the live agent trades with
        # exits — a backtest/live mismatch that can mis-rank strategies.
        engine = BacktestEngine(provider=self._provider,
                                initial_cash=self._initial_cash,
                                risk_manager=RiskManager(RiskConfig()),
                                protection_manager=ProtectionManager())

        # Cap universe so each Selector cycle stays bounded. Default raised
        # from 50 → 100 in 2026-05 (P4): 50 was statistically too thin for
        # walk-forward to discriminate strategies — many fold splits ended
        # up with zero trades. 100 doubles backtest cost but gives the
        # ranking real signal. Override via goal.params["selector_max_universe"].
        max_universe = int(params.get("selector_max_universe", 100))
        capped = codes[:max(20, max_universe)]

        # One thread per strategy. Each clones the engine (its own RiskManager +
        # per-run caches) so concurrent run()s don't race; the shared provider /
        # DB is thread-safe (RLock'd connection). ponytail: thread_map, not a
        # hand-rolled pool.
        def _eval(strat: BaseStrategy) -> StrategyEvaluation:
            try:
                eng = engine.clone()
                # In-sample baseline (always computed, cheap).
                strat.init(goal.params or {})
                is_bt = eng.run(strategy=strat, codes=capped,
                                start=is_start, end=end)
                m = is_bt.metrics or {}
                ev = StrategyEvaluation(
                    strategy_name=strat.name,
                    annual_return=float(m.get("annual_return", 0) or 0),
                    max_drawdown=float(m.get("max_drawdown", 0) or 0),
                    sharpe=float(m.get("sharpe_ratio", 0) or 0),
                    total_trades=len(is_bt.trades),
                    score=0.0,
                    # IS obs count — the T for the no-WF DSR fallback.
                    n_obs=int(m.get("trading_days", 0) or 0),
                )
                if wf_enabled:
                    # Fresh instance per fold via factory. `copy.copy` is
                    # enough because BaseStrategy state is cleared in init().
                    cls = type(strat)
                    cfg = resolve_params(self._db, strat.name, goal)
                    def factory(_cls=cls, _cfg=cfg) -> BaseStrategy:
                        inst = _cls()
                        inst.init(dict(_cfg))
                        return inst
                    wf = run_walk_forward(
                        eng, factory, capped, end,
                        n_folds=n_folds, warmup_days=warmup_days,
                        test_days=test_days,
                        history_start=history_start, max_folds=wf_max_folds,
                    )
                    ev.oos_annual_return = wf.oos_annual_return
                    ev.oos_max_drawdown = wf.oos_max_drawdown
                    ev.oos_sharpe = wf.oos_sharpe
                    ev.oos_consistency = wf.oos_consistency
                    ev.n_folds = len(wf.folds)
                    ev.n_populated_folds = wf.n_populated_folds
                    ev.oos_trades = wf.total_trades_oos
                    # Pooled OOS returns (concat across folds) drive the most
                    # accurate DSR path; real T = their count.
                    pooled: list[float] = []
                    for fr in wf.folds:
                        pooled.extend(fr.oos_returns)
                    ev.oos_returns = pooled
                    ev.n_obs = len(pooled)
                ev.score = self._score(ev, goal)
                return ev
            except Exception as e:
                logger.warning(f"Selector backtest failed for {strat.name}: {e}")
                return StrategyEvaluation(
                    strategy_name=strat.name, annual_return=0,
                    max_drawdown=0, sharpe=0, total_trades=0, score=-999,
                )

        results = thread_map(_eval, candidates)
        results.sort(key=lambda r: r.score, reverse=True)
        return results

    def _rank(self, goal: Goal, codes: list[str],
               candidates: list[BaseStrategy]) -> list[StrategyEvaluation]:
        """Rank candidates — in a SUBPROCESS by default (goal.params
        selector_subprocess=True): the walk-forward sweep is ~95s isolated but
        13+ minutes inside the server process (GIL/DB-lock contention with UI
        polls, guard threads, the syncer — measured 2026-08-14). Any worker
        failure falls back to the in-process path, so the tick never dies on
        a broken worker."""
        params = goal.params or {}
        if bool(params.get("selector_subprocess", True)) and len(candidates) > 1:
            try:
                ranking = self.evaluate_subprocess(goal, codes, candidates)
                if ranking:
                    logger.info("selector sweep ran in subprocess (%d strategies)",
                                len(ranking))
                    return ranking
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "selector subprocess sweep failed (%s) — in-process fallback", e)
        return self.evaluate(goal, codes, candidates)

    def evaluate_subprocess(self, goal: Goal, codes: list[str],
                            candidates: list[BaseStrategy],
                            timeout_sec: int = 1800
                            ) -> list[StrategyEvaluation]:
        """Run the sweep in a fresh process (quanti.agent.selector_worker),
        JSON over stdio. Raises on any failure — callers fall back."""
        import json
        import subprocess
        import sys

        payload = {
            "account_db": str(self._db._db_path),
            "market_db": (str(self._db.market_db_path)
                          if getattr(self._db, "_market_db_path", None)
                          else None),
            "strategies_dir": str(Path(self._strategies_dir).resolve()),
            "codes": codes,
            "end": date.today().isoformat(),
            "initial_cash": self._initial_cash,
            "params": goal.params or {},
            "candidate_names": [c.name for c in candidates],
        }
        proc = subprocess.run(
            [sys.executable, "-m", "quanti.agent.selector_worker"],
            input=json.dumps(payload), capture_output=True, text=True,
            timeout=timeout_sec,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"worker exit {proc.returncode}: "
                f"{(proc.stderr or proc.stdout or '').strip()[-300:]}")
        rows = json.loads(proc.stdout or "[]")
        if isinstance(rows, dict) and rows.get("error"):
            raise RuntimeError(f"worker: {rows['error']}")
        return [StrategyEvaluation(**row) for row in rows]

    def pick_best(self, goal: Goal, codes: list[str],
                  ) -> tuple[BaseStrategy | None, list[StrategyEvaluation]]:
        candidates = self._gated_candidates()
        if not candidates:
            return None, []
        ranking = self._rank(goal, codes, candidates)
        if not ranking:
            return None, []
        winner_name = ranking[0].strategy_name
        for s in candidates:
            if s.name == winner_name:
                return s, ranking
        return candidates[0], ranking

    def _gated_candidates(self) -> list[BaseStrategy]:
        """Load candidates minus strategies excluded by the daily health gate
        (quanti/agent/strategy_gate.py: breaker/deep_loss verdicts). The gate
        is computed on a 2-year window under default risk — walk-forward must
        never re-admit a strategy that rides -30% drawdowns (2026-08-14
        erratum). No gate rows yet → nothing excluded (gate only excludes on
        evidence)."""
        from quanti.agent.strategy_gate import excluded_names
        try:
            banned = excluded_names(self._db)
        except Exception:  # noqa: BLE001 - a gate read failure must not break selection
            banned = set()
        out = [s for s in self.load_candidates() if s.name not in banned]
        if banned:
            logger.info("strategy gate excludes: %s", sorted(banned))
        return out

    def pick_topk(self, goal: Goal, codes: list[str], k: int = 3,
                  ) -> tuple[list[tuple[BaseStrategy, float]], list[StrategyEvaluation]]:
        """Return top-K strategies with softmax-normalized weights.

        Weights derive from `oos_sharpe` (or IS sharpe if WF disabled),
        floored at 0 to prevent losing strategies from getting weight. A
        single positive-Sharpe strategy gets weight 1.0; multiple compete via
        softmax with temperature 0.5 so the best gets ~60-70% weight rather
        than 100%. Strategies excluded by the daily health gate (breaker /
        deep_loss verdicts) are dropped before ranking.
        """
        candidates = self._gated_candidates()
        if not candidates:
            return [], []
        ranking = self._rank(goal, codes, candidates)
        if not ranking:
            return [], []

        params = goal.params or {}
        wf_enabled = bool(params.get("wf_enabled", True))

        min_trades = int(params.get("wf_min_oos_trades", _MIN_OOS_TRADES))
        top = ranking[:k]
        # Capital weight rides on Sharpe — but a low-OOS-trade Sharpe is noise,
        # so zero its weight (it can still be the rank winner on return/dd, just
        # not get concentrated capital). All-zero → equal-weight fallback below.
        sharpes = []
        for ev in top:
            if wf_enabled and ev.n_folds > 0:
                s = ev.oos_sharpe if ev.oos_trades >= min_trades else 0.0
            else:
                s = ev.sharpe
            sharpes.append(max(0.0, s))
        total = sum(sharpes)
        if total <= 0:
            # All non-positive: fall back to score rank.
            weights = [1.0 / len(top)] * len(top)
        else:
            # Soft-temperature weighting. temp=1.0 (was 0.5) flattens the
            # allocation so a noisy Sharpe edge — estimated from short OOS
            # windows — doesn't concentrate most of the capital into one pick.
            temp = 1.0
            exps = [math.exp(s / temp) for s in sharpes]
            ze = sum(exps)
            weights = [e / ze for e in exps]

        # --- DSR multiple-testing deflation (diagnostic always; gate opt-in) ---
        # The softmax above hands concentrated capital to the rank winner on a
        # Sharpe estimated from short OOS windows — the exact "selection is
        # noise" pathology the 2026-07-01 audit flagged. Deflate the winner's
        # Sharpe by how many strategies we tried (N candidates = the
        # multiple-testing family): a best-of-N fluke earns a low deflated-Sharpe
        # probability. Gate is opt-in (dsr_gate, default off) until the logged
        # DSR confirms it's calibrated and not mis-firing; on trip we revert the
        # whole top-K to equal weight (zeroes the winner's concentration edge,
        # a strict overlay on the existing min-OOS-trades confidence guard).
        dsr = self._winner_dsr(top[0], ranking, wf_enabled)
        if dsr is not None:
            logger.info(
                "selector DSR: winner=%s dsr=%.3f sr_obs=%.4f sr0=%.4f "
                "n_trials=%d n_obs=%d weights=%s", top[0].strategy_name,
                dsr["dsr"], dsr["sr_observed"], dsr["sr0_benchmark"],
                dsr["n_trials"], dsr["n_obs"], [round(w, 3) for w in weights])
            if bool(params.get("dsr_gate", False)):
                # 0.85 default from scripts/dsr_calibration.py (54 月 OOS 重放):
                # 0.70~0.95 是平台,0.85 是前瞻收益点最优;低于它退等权净减损。
                dsr_min = float(params.get("dsr_min", 0.85))
                if dsr["dsr"] < dsr_min:
                    logger.info("selector DSR gate: winner %s DSR %.3f < %.2f "
                                "→ revert to equal weight",
                                top[0].strategy_name, dsr["dsr"], dsr_min)
                    weights = [1.0 / len(top)] * len(top)

        pairs: list[tuple[BaseStrategy, float]] = []
        by_name = {s.name: s for s in candidates}
        for ev, w in zip(top, weights):
            strat = by_name.get(ev.strategy_name)
            if strat is not None:
                pairs.append((strat, w))
        return pairs, ranking

    # -------------------------------------------------------------- DSR gate
    @staticmethod
    def _trust_pooled(ev: StrategyEvaluation, wf_enabled: bool) -> bool:
        """Whether ev's pooled OOS series clears the independent-sample
        guardrail (≥ _MIN_POPULATED_FOLDS populated folds) — the SAME floor
        _aggregate uses before it trusts oos_sharpe. A series pooled from a
        single populated fold is one draw/regime, not an independent-cycle
        signal; below the floor _aggregate has already zeroed oos_sharpe, so the
        DSR path must not resurrect a nonzero Sharpe from the raw series. No WF
        (no folds) → nothing to pool."""
        if not (wf_enabled and ev.n_folds > 0):
            return False
        return ev.n_populated_folds >= _MIN_POPULATED_FOLDS

    @staticmethod
    def _per_obs_sharpe(ev: StrategyEvaluation, wf_enabled: bool) -> float:
        """Per-period (NON-annualized) Sharpe for DSR math.

        Prefer the pooled OOS return series (exact) — but only when it clears
        the independent-sample guardrail (_trust_pooled). Otherwise de-annualize
        the stored Sharpe by ÷√252: the stored oos_sharpe/sharpe are annualized
        (metrics.annualized_sharpe ×√252) but DSR is a per-period quantity, and
        below the guardrail oos_sharpe is already 0 → a consistent per-obs 0.
        """
        if len(ev.oos_returns) >= 3 and StrategySelector._trust_pooled(ev, wf_enabled):
            return sharpe_per_obs(ev.oos_returns)
        ann = ev.oos_sharpe if (wf_enabled and ev.n_folds > 0) else ev.sharpe
        return ann / math.sqrt(_TRADING_DAYS)

    @staticmethod
    def _winner_dsr(winner: StrategyEvaluation,
                    ranking: list[StrategyEvaluation],
                    wf_enabled: bool) -> dict | None:
        """Deflated Sharpe of the rank winner vs the full N-candidate trial set.

        The N candidates ARE the multiple-testing family (selection breadth);
        their per-obs Sharpe dispersion sets how high a best-of-N fluke can
        reach. Returns the DSR info dict (with `n_obs`), or None when there
        aren't enough trials or observations to estimate it. Uses the pooled
        OOS returns when present AND trusted by the independent-sample guardrail
        (real skew/kurt, real T — most accurate); else falls back to summary
        stats fed the guardrail-consistent (possibly zeroed) Sharpe, so a
        single-populated-fold winner never reports a Sharpe the score zeroed.
        """
        trials = [StrategySelector._per_obs_sharpe(ev, wf_enabled)
                  for ev in ranking]
        if len(trials) < _MIN_DSR_TRIALS:
            return None
        if (len(winner.oos_returns) >= 3
                and StrategySelector._trust_pooled(winner, wf_enabled)):
            info = deflated_sharpe_ratio(winner.oos_returns, trials)
        elif winner.n_obs >= 3:
            info = deflated_sharpe_from_stats(
                StrategySelector._per_obs_sharpe(winner, wf_enabled),
                winner.n_obs, trials)
        else:
            return None
        info["n_obs"] = winner.n_obs
        return info

    # ------------------------------------------------------------ scoring
    @staticmethod
    def _score(ev: StrategyEvaluation, goal: Goal) -> float:
        """Composite score: higher is better.

        Uses OOS metrics when walk-forward data is available, falling back to
        IS otherwise. The component shape and weights are unchanged from the
        original — only the *inputs* change. Plus a consistency bonus that
        rewards low fold-to-fold variance.
        """
        tol = goal.risk_tolerance
        if isinstance(tol, str):
            tol = RiskTolerance(tol)

        # Trust the OOS Sharpe/consistency only with enough OOS trades —
        # otherwise it's sampling noise that mustn't drive selection.
        min_trades = int((goal.params or {}).get(
            "wf_min_oos_trades", _MIN_OOS_TRADES))
        confident = ev.n_folds > 0 and ev.oos_trades >= min_trades

        # Pick which numbers feed the score. WF available → use OOS.
        if ev.n_folds > 0:
            ann_return = ev.oos_annual_return
            max_dd = ev.oos_max_drawdown
            # Don't reward a Sharpe estimated from too few OOS trades.
            sharpe = ev.oos_sharpe if confident else 0.0
        else:
            ann_return = ev.annual_return
            max_dd = ev.max_drawdown
            sharpe = ev.sharpe

        # Normalize return relative to the target. A strategy that exactly
        # hits target earns 1.0; one that returns half earns 0.5; one that
        # doubles is capped at 1.5 so lottery-style outliers can't bury
        # solid-but-balanced picks.
        target = max(goal.target_annual_return, 0.01)
        return_score = max(min(ann_return / target, 1.5), -1.0)

        # Normalize drawdown relative to the user-stated ceiling. Positive
        # when comfortably within tolerance, 0 right at the limit, negative
        # when breaching.
        dd_ceiling = abs(goal.max_drawdown) if goal.max_drawdown != 0 else 0.20
        dd_score = (max_dd - goal.max_drawdown) / dd_ceiling
        # Clamp so a 10× breach doesn't dominate — at -2 a strategy is already
        # losing badly regardless.
        dd_score = max(min(dd_score, 1.5), -2.0)

        if tol is RiskTolerance.LOW:
            w_ret, w_dd, w_sharpe = 0.3, 1.8, 0.6
        elif tol is RiskTolerance.HIGH:
            w_ret, w_dd, w_sharpe = 1.2, 0.6, 0.4
        else:
            w_ret, w_dd, w_sharpe = 0.8, 1.0, 0.5

        # Consistency bonus only with a trustworthy (enough-trades) WF result.
        # A strategy that does 10% every fold is preferred over one that does
        # 30% / -10% / 30% / -10% even if their mean matches.
        w_consistency = 0.4 if confident else 0.0

        # `total_trades` is the IS count; a strategy that didn't trade at all
        # in IS but had OOS trades is rare-but-possible (e.g. WF found a
        # window where indicators warmed up enough). Use the more permissive
        # signal: any activity in either window counts.
        any_activity = ev.total_trades > 0 or (ev.n_folds > 0 and ev.oos_annual_return != 0)
        activity = 1.0 if any_activity else -1.0

        return (w_ret * return_score
                + w_dd * dd_score
                + w_sharpe * sharpe
                + w_consistency * ev.oos_consistency
                + activity)
