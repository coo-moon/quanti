"""Tests for the subprocess selector sweep (worker round-trip + fallback)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quanti.agent.goal import Goal
from quanti.agent.selector import StrategySelector
from quanti.data.database import Database
from quanti.data.provider import DataProvider


def _seed_market(db: Database) -> list[str]:
    """6 codes x 180 business days of synthetic bars, written into the db."""
    rng = np.random.default_rng(11)
    days = [d.date() for d in pd.bdate_range("2024-06-01", periods=180)]
    rows = []
    codes = []
    for i in range(6):
        code = f"{i:06d}"
        codes.append(code)
        drift = 0.3 if i < 3 else -0.3
        closes = list(10 + np.cumsum(np.full(180, drift) + rng.normal(0, 0.1, 180)))
        for d, c in zip(days, closes):
            rows.append({"code": code, "date": d, "open": c, "high": c + 0.1,
                         "low": c - 0.1, "close": c, "volume": 1e6,
                         "amount": c * 1e6, "turnover": 1.0})
    db.save_daily_quotes(pd.DataFrame(rows))
    db.save_trade_calendar(days)
    return codes


@pytest.fixture
def dbs(tmp_path):
    account = Database(str(tmp_path / "acct.db"))
    account.initialize()
    market = Database(str(tmp_path / "acct.db"),
                      market_db_path=str(tmp_path / "market.db"))
    market.initialize()
    codes = _seed_market(market)
    yield account, market, codes
    market.close()
    account.close()


def test_worker_roundtrip_matches_inprocess(dbs, tmp_path):
    account, market, codes = dbs
    root = Path(__file__).resolve().parent.parent
    payload = {
        "account_db": str(market._db_path),
        "market_db": str(market._market_db_path),
        "strategies_dir": str(root / "strategies"),
        "codes": codes,
        "end": date.today().isoformat(),
        "initial_cash": 1_000_000.0,
        "params": {"wf_max_folds": 8},
        "candidate_names": None,
    }
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "quanti.agent.selector_worker"],
        input=json.dumps(payload), capture_output=True, text=True,
        timeout=300, env=env)
    assert proc.returncode == 0, proc.stderr[-500:]
    rows = json.loads(proc.stdout)
    assert len(rows) == 6
    assert all(set(r) >= {"strategy_name", "score", "oos_returns"}
               for r in rows)
    # In-process evaluation on the SAME inputs must agree (rank + scores).
    provider = DataProvider(market)
    sel = StrategySelector(market, provider,
                           strategies_dir=str(root / "strategies"))
    goal = Goal()
    goal.params = {"wf_max_folds": 8}
    cands = sel.load_candidates()
    local = sel.evaluate(goal, codes, cands)
    assert [r["strategy_name"] for r in rows] == [e.strategy_name
                                                  for e in local]
    for row, ev in zip(rows, local):
        assert row["score"] == pytest.approx(ev.score, abs=1e-6)
        assert row["oos_returns"] == pytest.approx(ev.oos_returns, abs=1e-9)


def test_worker_bad_input_reports_error(dbs, tmp_path):
    root = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "quanti.agent.selector_worker"],
        input=json.dumps({"account_db": "/nonexistent/x.db", "codes": []}),
        capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode != 0
    assert "error" in proc.stdout


def test_rank_falls_back_on_worker_failure(monkeypatch):
    """A broken subprocess must never kill the tick — in-process fallback."""
    from quanti.agent.selector import StrategyEvaluation

    class _Sel:
        def __init__(self):
            self.calls = {"sub": 0, "inproc": 0}

        def evaluate_subprocess(self, goal, codes, candidates,
                                timeout_sec=1800):
            self.calls["sub"] += 1
            raise RuntimeError("boom")

        def evaluate(self, goal, codes, candidates):
            self.calls["inproc"] += 1
            return [StrategyEvaluation(strategy_name="ma_cross",
                                       annual_return=0.1, max_drawdown=-0.1,
                                       sharpe=0.5, total_trades=5, score=1.0)]

    sel = _Sel()
    ranking = StrategySelector._rank(
        sel, Goal(), ["000001"],  # goal + codes ignored by the stubs
        candidates=[object()])  # len(candidates)==1 → straight in-process
    assert sel.calls == {"sub": 0, "inproc": 1}
    assert ranking[0].strategy_name == "ma_cross"

