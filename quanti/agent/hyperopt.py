"""Walk-forward parameter optimization (hyperopt) for strategies.

A1 design: grid-search each strategy's `param_space` on a TRAIN window, then
validate the winning combo AND the default config out-of-sample (reusing
run_walk_forward); accept the tuned combo only if it beats the default OOS by
a margin and passes the fold/trade guards. On-demand (CLI + async API), never
per agent cycle. See docs/superpowers/specs/2026-06-21-walk-forward-hyperopt-design.md.
"""

from __future__ import annotations

import itertools
import logging
import random
from dataclasses import dataclass
from datetime import date, timedelta

from quanti.agent.walk_forward import make_folds, run_walk_forward
from quanti.backtest.metrics import compute_metrics
from quanti.utils.parallel import thread_map

logger = logging.getLogger(__name__)


def build_grid(param_space: dict[str, list], max_combos: int,
               seed: int) -> tuple[list[dict], int]:
    """Cartesian product of `param_space` → list of full param dicts.

    Returns (combos, total_before_cap). If the product exceeds `max_combos`,
    randomly sample `max_combos` of them with a fixed seed (reproducible) and
    log the dropped count — never silently truncate."""
    if not param_space:
        return [], 0
    keys = list(param_space.keys())
    value_lists = [param_space[k] for k in keys]
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*value_lists)]
    total = len(combos)
    if total > max_combos:
        combos = random.Random(seed).sample(combos, max_combos)
        logger.info("hyperopt grid capped: %d → %d combos (sampled, seed=%d)",
                    total, max_combos, seed)
    return combos, total


@dataclass
class OptimizeResult:
    strategy_name: str
    accepted: bool
    chosen_params: dict
    default_params: dict
    tuned_oos_sharpe: float
    default_oos_sharpe: float
    n_combos_tried: int
    n_combos_total: int
    reason: str


class HyperOptimizer:
    def __init__(self, engine, *, train_days: int = 1095, n_folds: int = 6,
                 warmup_days: int = 120, test_days: int = 126,
                 max_combos: int = 64, accept_margin: float = 0.1,
                 min_folds: int = 2, min_trades_oos: int = 5,
                 seed: int = 42) -> None:
        # Depth defaults deepened (was train=365d, 3×21d OOS ≈ 63 OOS days) so
        # tuning validates over more history: 6×126d ≈ 2y OOS across regimes,
        # preceded by a 3y train window. Unlike the Selector, hyperopt CANNOT
        # eat the *full* history — it grid-searches params on a TRAIN window
        # that must sit strictly before the OOS folds, so the folds stay bounded
        # to leave room for training.
        self._engine = engine
        self.train_days = train_days
        self.n_folds = n_folds
        self.warmup_days = warmup_days
        self.test_days = test_days
        self.max_combos = max_combos
        self.accept_margin = accept_margin
        self.min_folds = min_folds
        self.min_trades_oos = min_trades_oos
        self.seed = seed

    def _train_window(self, end: date) -> tuple[date, date]:
        """Train window strictly before the OOS folds (incl. their warmup)."""
        folds = make_folds(end, n_folds=self.n_folds,
                           warmup_days=self.warmup_days, test_days=self.test_days)
        earliest_warmup = min(f.warmup_start for f in folds)
        train_end = earliest_warmup - timedelta(days=1)
        train_start = train_end - timedelta(days=self.train_days)
        return train_start, train_end

    def _factory(self, strategy_cls, cfg: dict):
        def make():
            inst = strategy_cls()
            inst.init(dict(cfg))
            return inst
        return make

    def optimize(self, strategy_cls, codes: list[str],
                 end: date) -> OptimizeResult:
        name = getattr(strategy_cls, "name", strategy_cls.__name__)
        space = dict(getattr(strategy_cls, "param_space", {}) or {})
        if not space:
            return OptimizeResult(name, False, {}, {}, 0.0, 0.0, 0, 0,
                                  "no param_space — not tuned")
        combos, total = build_grid(space, self.max_combos, self.seed)
        train_start, train_end = self._train_window(end)

        # 1) Search on TRAIN: best combo by train Sharpe. One thread per combo;
        # each clones the engine (own RiskManager + caches) so concurrent run()s
        # don't race. ponytail: thread_map, not a hand-rolled pool.
        def _train_sharpe(combo: dict) -> float:
            try:
                bt = self._engine.clone().run(
                    strategy=self._factory(strategy_cls, combo)(),
                    codes=codes, start=train_start, end=train_end)
            except Exception as e:  # noqa: BLE001 - one combo failing != fatal
                logger.warning("hyperopt train run failed %s %s: %s", name, combo, e)
                return float("-inf")
            curve = getattr(bt, "equity_curve", None)
            m = compute_metrics(curve) if curve is not None and len(curve) > 1 else {}
            return float(m.get("sharpe_ratio", 0.0) or 0.0)

        sharpes = thread_map(_train_sharpe, combos)
        best_combo, best_train = None, float("-inf")
        for combo, sharpe in zip(combos, sharpes):
            if sharpe > best_train:
                best_train, best_combo = sharpe, combo
        if best_combo is None:
            return OptimizeResult(name, False, {}, {}, 0.0, 0.0, len(combos),
                                  total, "no valid train result")

        # 2) Validate the best combo AND the default ({}) on OOS folds.
        def _wf(cfg: dict):
            return run_walk_forward(
                self._engine, self._factory(strategy_cls, cfg), codes, end,
                n_folds=self.n_folds, warmup_days=self.warmup_days,
                test_days=self.test_days)
        tuned = _wf(best_combo)
        default = _wf({})

        # 3) Accept gate.
        folds_ok = len(tuned.folds) >= self.min_folds
        trades_ok = tuned.total_trades_oos >= self.min_trades_oos
        beats = tuned.oos_sharpe > default.oos_sharpe + self.accept_margin
        positive = tuned.oos_sharpe > 0
        accepted = folds_ok and trades_ok and beats and positive
        if accepted:
            verdict = "accepted"
        elif not folds_ok:
            verdict = f"rejected: folds {len(tuned.folds)} < {self.min_folds}"
        elif not trades_ok:
            verdict = f"rejected: trades {tuned.total_trades_oos} < {self.min_trades_oos}"
        else:
            verdict = "rejected: did not beat default → keep default"
        reason = (f"tuned_oos={tuned.oos_sharpe:.2f} vs default={default.oos_sharpe:.2f}"
                  f" (margin {self.accept_margin}); {verdict}")
        return OptimizeResult(name, accepted, dict(best_combo), {},
                              float(tuned.oos_sharpe), float(default.oos_sharpe),
                              len(combos), total, reason)

    def optimize_all(self, strategy_classes, codes: list[str], end: date,
                     progress=None) -> list[OptimizeResult]:
        results: list[OptimizeResult] = []
        total = len(strategy_classes)
        for i, cls in enumerate(strategy_classes):
            if progress:
                progress(i, total, getattr(cls, "name", cls.__name__))
            results.append(self.optimize(cls, codes, end))
        if progress:
            progress(total, total, "")
        return results
