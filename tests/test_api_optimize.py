"""Tests for async hyperopt optimize API endpoints."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from quanti.api.app import create_app


def test_tuned_params_endpoint_lists_rows(tmp_path, monkeypatch):
    app = create_app(initial_cash=1_000_000, autostart_agent=False)
    client = TestClient(app)
    r = client.get("/api/agent/tuned-params")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_optimize_async_returns_job_and_status(monkeypatch):
    # Stub HyperOptimizer.optimize_all to be instant + deterministic.
    from quanti.agent import hyperopt

    def fake_all(self, classes, codes, end, progress=None):
        if progress:
            progress(1, 1, "ma_cross")
        return [
            hyperopt.OptimizeResult(
                "ma_cross", True, {"short_period": 8},
                {}, 1.5, 0.4, 12, 12, "accepted",
            )
        ]

    monkeypatch.setattr(hyperopt.HyperOptimizer, "optimize_all", fake_all)

    app = create_app(initial_cash=1_000_000, autostart_agent=False)
    client = TestClient(app)
    r = client.post("/api/agent/optimize/async")
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    # Poll until done (the executor task is quick with the stub).
    s = {}
    for _ in range(50):
        s = client.get(
            "/api/agent/optimize/status", params={"job_id": job_id}
        ).json()
        if s.get("status") in ("done", "completed", "error"):
            break
        time.sleep(0.05)

    assert s["status"] in ("done", "completed")
    assert any(x["strategy_name"] == "ma_cross" for x in s["results"])
