"""LLM key 落库 + hydrate_llm_env:env 优先、DB 兜底、失败不炸。"""

from __future__ import annotations

import pytest

from quanti.agent.goal import Goal, save_goal
from quanti.agent.llm_runtime import hydrate_llm_env
from quanti.data.database import Database


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    yield d
    d.close()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_key_roundtrip_and_other_config_preserved(db):
    assert db.get_llm_api_key() == ""
    db.set_llm_api_key("sk-test-1")
    assert db.get_llm_api_key() == "sk-test-1"
    db.set_alert_webhook("https://x.example/hook")  # 别的列互不干扰
    db.upsert_app_config("tushare", "tok")
    assert db.get_llm_api_key() == "sk-test-1"
    db.set_llm_api_key("")
    assert db.get_llm_api_key() == ""


def test_hydrate_sets_provider_env_from_db(db, monkeypatch):
    import os
    save_goal(db, Goal(params={"llm_provider": "deepseek"}))
    db.set_llm_api_key("sk-from-db")
    hydrate_llm_env(db)
    assert os.environ["DEEPSEEK_API_KEY"] == "sk-from-db"
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_hydrate_anthropic_provider(db, monkeypatch):
    import os
    save_goal(db, Goal(params={"llm_provider": "anthropic"}))
    db.set_llm_api_key("sk-ant")
    hydrate_llm_env(db)
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant"


def test_hydrate_never_overrides_shell_env(db, monkeypatch):
    import os
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-shell")
    save_goal(db, Goal(params={"llm_provider": "deepseek"}))
    db.set_llm_api_key("sk-from-db")
    hydrate_llm_env(db)
    assert os.environ["DEEPSEEK_API_KEY"] == "sk-from-shell"


def test_hydrate_noop_without_key(db):
    import os
    hydrate_llm_env(db)
    assert "DEEPSEEK_API_KEY" not in os.environ
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_hydrate_swallows_db_errors(db, monkeypatch):
    monkeypatch.setattr(db, "get_llm_api_key",
                        lambda: (_ for _ in ()).throw(RuntimeError("x")))
    hydrate_llm_env(db)  # 不许抛
