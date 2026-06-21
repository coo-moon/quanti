"""Tests for async mine-factors job + generated-factor endpoints."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from quanti.api.app import create_app


def test_generated_endpoints_and_toggle(monkeypatch):
    app = create_app(initial_cash=1_000_000, autostart_agent=False)
    db = app.state.db
    db.save_generated_factor("f1", "-Mean(close,5)", 0.05, 0.04, accepted=True)
    client = TestClient(app)
    r = client.get("/api/factors/generated")
    assert r.status_code == 200 and any(x["name"] == "f1" for x in r.json())
    t = client.post("/api/factors/generated/f1/enabled", json={"enabled": False})
    assert t.status_code == 200 and t.json()["enabled"] is False
    assert all(not x["enabled"] for x in client.get("/api/factors/generated").json()
               if x["name"] == "f1")


def test_mine_async_lifecycle(monkeypatch):
    from quanti.agent import factor_miner

    def fake_mine(llm, db, provider, codes, end, **kw):
        db.save_generated_factor("af", "-Std(close,20)", 0.05, 0.04, True)
        return [factor_miner.MineResult("af", "-Std(close,20)", 0.05, 0.04, True, "ok")]

    monkeypatch.setattr(factor_miner, "mine_factors", fake_mine)

    app = create_app(initial_cash=1_000_000, autostart_agent=False)
    client = TestClient(app)
    jid = client.post("/api/agent/mine-factors/async").json()["job_id"]
    for _ in range(60):
        s = client.get("/api/agent/mine-factors/status", params={"job_id": jid}).json()
        if s.get("status") in ("done", "error"):
            break
        time.sleep(0.05)
    assert s["status"] == "done"
    assert any(x["name"] == "af" for x in s["results"])
