"""Tests for CLI cmd_mine_factors + shared build_llm_client helper."""
from __future__ import annotations

import types
from datetime import date

import quanti.cli as cli


def test_cmd_mine_factors_persists(tmp_path, monkeypatch):
    from quanti.data.database import Database

    dbp = str(tmp_path / "paper.db")
    real = Database(dbp)
    real.initialize()
    real.ensure_portfolio(1_000_000)
    def _make_db():
        d = Database(dbp)
        d.initialize()
        return d

    monkeypatch.setattr(cli, "_open_db", _make_db)

    from quanti.agent import factor_miner

    def fake_mine(llm, db, provider, codes, end, **kw):
        db.save_generated_factor("cli_f", "-Mean(close,5)", 0.05, 0.04, True)
        return [factor_miner.MineResult("cli_f", "-Mean(close,5)", 0.05, 0.04, True, "ok")]

    monkeypatch.setattr(factor_miner, "mine_factors", fake_mine)
    # Stub the shared LLM client builder so no network/key is needed.
    from quanti.agent import llm_runtime
    monkeypatch.setattr(llm_runtime, "build_llm_client", lambda params: object())

    args = types.SimpleNamespace(
        universe=None,
        n=5,
        end=date.today().isoformat(),
        cash=1_000_000,
    )
    cli.cmd_mine_factors(args)
    check_db = Database(dbp)
    check_db.initialize()
    assert any(r["name"] == "cli_f" for r in check_db.list_generated_factors())
