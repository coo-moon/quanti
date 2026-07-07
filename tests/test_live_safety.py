"""Live-money safety gates (H3).

Two independent guards so real money is never traded by accident:
  1. `make_broker` (the single paper↔live switch point) refuses to build a live
     broker unless QUANTI_LIVE_ACK=I_KNOW_REAL_MONEY is set — QUANTI_ACCOUNT=live
     alone (a stray env / reused script) is not enough.
  2. In live, the app never auto-starts the agent on boot — the operator must
     explicitly agent_start each session (no boot=trade, no auto-resume).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from quanti.api.app import create_app
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.factory import LIVE_ACK_ENV, LIVE_ACK_TOKEN, make_broker


@pytest.fixture
def dbp(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    yield db, DataProvider(db)
    db.close()


# --- Gate 1: the real-money acknowledgment ----------------------------------

def test_live_broker_refused_without_ack(dbp, monkeypatch):
    db, provider = dbp
    monkeypatch.delenv(LIVE_ACK_ENV, raising=False)
    with pytest.raises(RuntimeError, match="二次确认"):
        make_broker(db, provider, account="live")


def test_live_broker_refused_with_wrong_ack(dbp, monkeypatch):
    db, provider = dbp
    monkeypatch.setenv(LIVE_ACK_ENV, "sure")
    with pytest.raises(RuntimeError):
        make_broker(db, provider, account="live")


def test_live_broker_built_with_ack(dbp, monkeypatch):
    db, provider = dbp
    monkeypatch.setenv(LIVE_ACK_ENV, LIVE_ACK_TOKEN)
    broker = make_broker(db, provider, account="live")
    assert type(broker).__name__ == "QmtBroker"
    assert broker._require_live is True


def test_paper_broker_needs_no_ack(dbp, monkeypatch):
    db, provider = dbp
    monkeypatch.delenv(LIVE_ACK_ENV, raising=False)
    assert type(make_broker(db, provider, account="paper")).__name__ == "PaperBroker"


# --- Gate 2: no auto-start of the agent in live -----------------------------

def _spy_start(app, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(app.state.agent, "start",
                        lambda: calls.__setitem__("n", calls["n"] + 1))
    return calls


def test_live_does_not_autostart_agent(dbp, monkeypatch):
    db, provider = dbp
    monkeypatch.setenv("QUANTI_ACCOUNT", "live")
    monkeypatch.setenv(LIVE_ACK_ENV, LIVE_ACK_TOKEN)
    app = create_app(db=db, provider=provider, autostart_agent=True,
                     autostart_background_sync=False)
    calls = _spy_start(app, monkeypatch)
    with TestClient(app):          # runs lifespan
        pass
    assert calls["n"] == 0, "live must not auto-start the agent even with autostart"


def test_paper_autostarts_agent(dbp, monkeypatch):
    db, provider = dbp
    monkeypatch.setenv("QUANTI_ACCOUNT", "paper")
    app = create_app(db=db, provider=provider, autostart_agent=True,
                     autostart_background_sync=False)
    calls = _spy_start(app, monkeypatch)
    with TestClient(app):
        pass
    assert calls["n"] == 1, "paper keeps the boot-time autostart"
