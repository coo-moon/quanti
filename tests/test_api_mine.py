"""Tests for async mine-factors job + generated-factor endpoints."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from quanti.api.app import create_app
from quanti.data.database import Database


def _hermetic_app(tmp_path):
    """App on a throwaway DB — never the real data/paper.db|market.db (else
    tests pollute the account library AND the mine worker's real-universe
    resolution makes the async test slow + flaky)."""
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    return create_app(db=db, initial_cash=1_000_000, autostart_agent=False,
                      autostart_background_sync=False)


def test_generated_endpoints_and_toggle(tmp_path):
    app = _hermetic_app(tmp_path)
    app.state.db.save_generated_factor("f1", "-Mean(close,5)", 0.05, 0.04,
                                       accepted=True)
    client = TestClient(app)
    r = client.get("/api/factors/generated")
    assert r.status_code == 200 and any(x["name"] == "f1" for x in r.json())
    t = client.post("/api/factors/generated/f1/enabled", json={"enabled": False})
    assert t.status_code == 200 and t.json()["enabled"] is False
    assert all(not x["enabled"] for x in client.get("/api/factors/generated").json()
               if x["name"] == "f1")


def test_mine_async_lifecycle(tmp_path, monkeypatch):
    from quanti.agent import factor_miner, llm_runtime, universe

    def fake_mine(llm, db, provider, codes, end, **kw):
        db.save_generated_factor("af", "-Std(close,20)", 0.05, 0.04, True)
        return [factor_miner.MineResult("af", "-Std(close,20)", 0.05, 0.04, True, "ok")]

    # Mock the worker's heavy/non-deterministic setup so it runs instantly and
    # never touches a real LLM client or the full-market universe → no flake.
    monkeypatch.setattr(factor_miner, "mine_factors", fake_mine)
    monkeypatch.setattr(llm_runtime, "build_llm_client", lambda params: object())
    monkeypatch.setattr(universe, "resolve_tradable_universe",
                        lambda *a, **k: ["000001"])

    app = _hermetic_app(tmp_path)
    client = TestClient(app)
    jid = client.post("/api/agent/mine-factors/async").json()["job_id"]
    s = {}
    for _ in range(200):   # generous; work() is now instant + hermetic
        s = client.get("/api/agent/mine-factors/status", params={"job_id": jid}).json()
        if s.get("status") in ("done", "error"):
            break
        time.sleep(0.02)
    assert s["status"] == "done", s
    assert any(x["name"] == "af" for x in s["results"])
