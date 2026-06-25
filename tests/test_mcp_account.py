"""MCP server picks BOTH db and broker by QUANTI_ACCOUNT — no live-broker /
paper-db mismatch (the gap app.py/cli.py already closed via make_broker)."""

from __future__ import annotations

from quanti.execution.paper_broker import PaperBroker
from quanti.execution.qmt_broker import QmtBroker
from quanti.mcp_server import QuantiContext


def test_mcp_paper_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)            # keep data/*.db out of the repo
    (tmp_path / "data").mkdir()
    monkeypatch.delenv("QUANTI_ACCOUNT", raising=False)
    ctx = QuantiContext()
    try:
        assert isinstance(ctx.broker, PaperBroker)
        assert (tmp_path / "data" / "paper.db").exists()
    finally:
        ctx.db.close()


def test_mcp_live_uses_qmt_and_live_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    monkeypatch.setenv("QUANTI_ACCOUNT", "live")
    ctx = QuantiContext()
    try:
        assert isinstance(ctx.broker, QmtBroker)          # live broker, not paper
        assert ctx.broker._require_live is True           # real-money guard on
        assert (tmp_path / "data" / "live.db").exists()   # db + broker same account
    finally:
        ctx.db.close()
