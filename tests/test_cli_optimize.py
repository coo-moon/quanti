# tests/test_cli_optimize.py
from __future__ import annotations

import types
from datetime import date

import quanti.cli as cli


def test_cmd_optimize_persists_results(tmp_path, monkeypatch):
    db_path = tmp_path / "paper.db"
    monkeypatch.setenv("QUANTI_DB", str(db_path))  # if cli honors it; else patch _open_db

    from quanti.data.database import Database
    real_db = Database(str(db_path))
    real_db.initialize()
    real_db.ensure_portfolio(1_000_000)
    def _open_db_stub():
        db = Database(str(db_path))
        db.initialize()
        return db
    monkeypatch.setattr(cli, "_open_db", _open_db_stub)

    # Stub the optimizer so the test is fast and deterministic.
    from quanti.agent import hyperopt
    def fake_all(self, classes, codes, end, progress=None):
        return [hyperopt.OptimizeResult("ma_cross", True, {"short_period": 8},
                                        {}, 1.5, 0.4, 12, 12, "accepted")]
    monkeypatch.setattr(hyperopt.HyperOptimizer, "optimize_all", fake_all)

    args = types.SimpleNamespace(universe=None, end=date.today().isoformat(),
                                 cash=1_000_000)
    cli.cmd_optimize(args)

    verify_db = Database(str(db_path))
    verify_db.initialize()
    out = verify_db.get_active_params("ma_cross")
    assert out == {"short_period": 8}
