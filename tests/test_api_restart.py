"""Tests for the POST /agent/restart endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from quanti.api.app import create_app


def test_restart_endpoint_calls_agent_restart(monkeypatch):
    app = create_app(initial_cash=1_000_000, autostart_agent=False)
    calls = {"n": 0}
    # Spy 替换真实 restart，避免起线程/触网。
    monkeypatch.setattr(
        app.state.agent, "restart",
        lambda: calls.__setitem__("n", calls["n"] + 1))
    client = TestClient(app)

    r = client.post("/api/agent/restart")

    assert r.status_code == 200
    assert r.json() == {"status": "restarted"}
    assert calls["n"] == 1
