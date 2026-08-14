"""Subprocess entry point for the selector sweep.

The walk-forward sweep is ~95s in an isolated process but 13+ minutes inside
the server process — GIL/DB-lock contention with UI status polls, guard
threads and the syncer (measured 2026-08-14: an isolated subprocess ran the
same sweep in 95s WHILE the in-process one was grinding). This worker runs
the exact same evaluation in a fresh process and prints JSON to stdout;
StrategySelector.evaluate_subprocess parses it back into StrategyEvaluation
objects. On any failure the parent falls back to the in-process path, so a
broken worker can never take the tick down.

Input (stdin, JSON): {account_db, market_db, strategies_dir, codes, end,
initial_cash, params, candidate_names}. Output (stdout, JSON): a list of
StrategyEvaluation dicts (including the pooled oos_returns).
"""

from __future__ import annotations

import json
import sys
from datetime import date


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": f"bad input: {e}"}))
        return 2
    try:
        from quanti.agent.goal import Goal
        from quanti.agent.selector import StrategySelector
        from quanti.data.database import Database
        from quanti.data.provider import DataProvider

        db = Database(payload["account_db"],
                      market_db_path=payload.get("market_db"))
        db.initialize()
        provider = DataProvider(db)
        sel = StrategySelector(db, provider,
                               strategies_dir=payload["strategies_dir"],
                               initial_cash=float(payload.get("initial_cash",
                                                          1_000_000.0)))
        by_name = {s.name: s for s in sel.load_candidates()}
        wanted = payload.get("candidate_names") or list(by_name)
        candidates = [by_name[n] for n in wanted if n in by_name]
        goal = Goal()
        goal.params = dict(payload.get("params") or {})
        end = date.fromisoformat(payload["end"])
        ranking = sel.evaluate(goal, payload["codes"], candidates, as_of=end)
        out = [{
            "strategy_name": r.strategy_name,
            "annual_return": r.annual_return,
            "max_drawdown": r.max_drawdown,
            "sharpe": r.sharpe,
            "total_trades": r.total_trades,
            "score": r.score,
            "oos_annual_return": r.oos_annual_return,
            "oos_max_drawdown": r.oos_max_drawdown,
            "oos_sharpe": r.oos_sharpe,
            "oos_consistency": r.oos_consistency,
            "n_folds": r.n_folds,
            "n_populated_folds": r.n_populated_folds,
            "oos_trades": r.oos_trades,
            "n_obs": r.n_obs,
            "oos_returns": r.oos_returns,
        } for r in ranking]
        print(json.dumps(out, ensure_ascii=False))
        return 0
    except Exception as e:  # noqa: BLE001 — parent logs and falls back
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())

