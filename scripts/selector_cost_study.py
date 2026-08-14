#!/usr/bin/env python3
"""Selector sweep cost study — isolate workers (GIL) and fold-count effects.

Each invocation runs the selector sweep ONCE in a fresh process (cold cache,
like a real tick) on the liquid top-60 universe with the 4 non-gated
strategies, and prints JSON {workers, max_folds, seconds, scores, n_obs}.

Usage: python scripts/selector_cost_study.py --workers 4 --max-folds 16
"""
import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-folds", dest="max_folds", type=int, default=8)
    ap.add_argument("--codes", type=int, default=60)
    args = ap.parse_args()

    from quanti.agent.goal import Goal
    from quanti.agent.selector import StrategySelector
    from quanti.agent.strategy_gate import excluded_names
    from quanti.data.database import Database
    from quanti.data.provider import DataProvider

    db = Database("data/paper.db", market_db_path="data/market.db")
    db.initialize()
    provider = DataProvider(db)
    today = date.today()

    from quanti.agent.universe import resolve_tradable_universe
    codes = resolve_tradable_universe(db, provider, pool=None,
                                      params={"liquidity_filter": True},
                                      as_of=today)[:args.codes]

    goal = Goal()
    goal.params = {"wf_max_folds": args.max_folds,
                   "selector_max_universe": args.codes}
    sel = StrategySelector(db, provider)
    banned = excluded_names(db)
    candidates = [s for s in sel.load_candidates() if s.name not in banned]

    # Force the worker count regardless of os.cpu_count().
    import quanti.agent.selector as sel_mod
    import quanti.utils.parallel as par
    orig = sel_mod.thread_map

    def forced(fn, items, workers=None):
        return orig(fn, items, workers=args.workers)
    sel_mod.thread_map = forced
    par.thread_map = forced

    t0 = time.monotonic()
    ranking = sel.evaluate(goal, codes, candidates)
    secs = time.monotonic() - t0
    out = {
        "workers": args.workers,
        "max_folds": args.max_folds,
        "codes": len(codes),
        "strategies": [r.strategy_name for r in ranking],
        "seconds": round(secs, 1),
        "scores": {r.strategy_name: round(r.score, 4) for r in ranking},
        "n_obs": [r.n_obs for r in ranking],
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()

