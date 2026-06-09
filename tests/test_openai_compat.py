"""Tests for the OpenAI-compatible (DeepSeek) LLM client.

Uses httpx.MockTransport to verify the Anthropic<->OpenAI translation without
network or an API key, then proves the client drops into the existing
sentiment pipeline unchanged.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from quanti.agent.openai_compat import (
    DeepSeekLLMClient,
    from_openai_response,
    to_openai_messages,
    to_openai_tools,
)
from quanti.agent.sentiment import score_candidates
from quanti.data.database import Database

ONE_TOOL = [{
    "name": "submit_sentiment",
    "description": "score",
    "input_schema": {"type": "object",
                     "properties": {"scores": {"type": "array"}},
                     "required": ["scores"]},
}]


def _oai_tool_resp(name, args, finish="tool_calls"):
    return {
        "choices": [{"index": 0, "finish_reason": finish,
                     "message": {"role": "assistant", "content": None,
                                 "tool_calls": [{"id": "call_1", "type": "function",
                                                 "function": {"name": name,
                                                              "arguments": json.dumps(args)}}]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _oai_text_resp(text, finish="stop"):
    return {
        "choices": [{"index": 0, "finish_reason": finish,
                     "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }


def _client_capturing(response: dict):
    """DeepSeek client whose transport records the outgoing payload."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=response)

    client = DeepSeekLLMClient(api_key="test-key",
                               transport=httpx.MockTransport(handler))
    return client, captured


# ----- pure translation helpers -----------------------------------------

class TestTranslation:
    def test_tools_to_openai_functions(self):
        out = to_openai_tools(ONE_TOOL)
        assert out[0]["type"] == "function"
        assert out[0]["function"]["name"] == "submit_sentiment"
        assert out[0]["function"]["parameters"] == ONE_TOOL[0]["input_schema"]

    def test_empty_tools_is_none(self):
        assert to_openai_tools([]) is None
        assert to_openai_tools(None) is None

    def test_system_blocks_join(self):
        msgs = to_openai_messages(
            [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}],
            [{"role": "user", "content": "hi"}])
        assert msgs[0] == {"role": "system", "content": "A\nB"}
        assert msgs[1] == {"role": "user", "content": "hi"}

    def test_assistant_tooluse_and_tool_result_roundtrip(self):
        msgs = to_openai_messages(None, [
            {"role": "user", "content": "ctx"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "thinking"},
                {"type": "tool_use", "id": "tu1", "name": "inspect_position",
                 "input": {"code": "600519"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu1", "content": "{}"}]},
        ])
        assistant = next(m for m in msgs if m["role"] == "assistant")
        assert assistant["tool_calls"][0]["id"] == "tu1"
        assert assistant["tool_calls"][0]["function"]["name"] == "inspect_position"
        assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"code": "600519"}
        tool_msg = next(m for m in msgs if m["role"] == "tool")
        assert tool_msg["tool_call_id"] == "tu1"
        assert tool_msg["content"] == "{}"

    def test_response_tool_call_to_anthropic_blocks(self):
        out = from_openai_response(_oai_tool_resp("submit_sentiment",
                                                  {"scores": [{"code": "X", "score": 0.5}]}))
        assert out["stop_reason"] == "tool_use"
        block = out["content"][0]
        assert block["type"] == "tool_use"
        assert block["name"] == "submit_sentiment"
        assert block["input"] == {"scores": [{"code": "X", "score": 0.5}]}
        assert out["usage"]["input_tokens"] == 10
        assert out["usage"]["output_tokens"] == 5

    def test_response_text_maps_stop_reason(self):
        out = from_openai_response(_oai_text_resp("hello", finish="stop"))
        assert out["stop_reason"] == "end_turn"
        assert out["content"][0] == {"type": "text", "text": "hello"}


# ----- create_message over MockTransport ---------------------------------

class TestCreateMessage:
    def test_scoring_call_payload_and_response(self):
        client, cap = _client_capturing(
            _oai_tool_resp("submit_sentiment", {"scores": [{"code": "600519", "score": 0.5}]}))
        resp = client.create_message(
            model="claude-sonnet-4-5",  # should remap to deepseek-chat
            system=[{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": "score it"}],
            tools=ONE_TOOL, max_tokens=256, temperature=0.0)

        p = cap["payload"]
        assert p["model"] == "deepseek-chat"          # claude id remapped
        assert p["messages"][0] == {"role": "system", "content": "SYS"}
        assert p["messages"][1] == {"role": "user", "content": "score it"}
        assert p["tools"][0]["function"]["name"] == "submit_sentiment"
        # single tool → forced
        assert p["tool_choice"]["function"]["name"] == "submit_sentiment"
        assert cap["url"].endswith("/v1/chat/completions")
        assert cap["auth"] == "Bearer test-key"
        # response translated
        assert resp["stop_reason"] == "tool_use"
        assert resp["content"][0]["input"] == {"scores": [{"code": "600519", "score": 0.5}]}

    def test_no_tools_means_no_tool_choice(self):
        client, cap = _client_capturing(_oai_text_resp("看多"))
        resp = client.create_message(
            model="deepseek-chat", system="S",
            messages=[{"role": "user", "content": "argue"}],
            tools=[], max_tokens=128, temperature=0.3)
        assert "tools" not in cap["payload"]
        assert "tool_choice" not in cap["payload"]
        assert resp["content"][0]["text"] == "看多"

    def test_model_passthrough_for_non_claude(self):
        client, cap = _client_capturing(_oai_text_resp("ok"))
        client.create_message(model="deepseek-reasoner", system=None,
                              messages=[{"role": "user", "content": "x"}],
                              tools=None, max_tokens=10, temperature=0.0)
        assert cap["payload"]["model"] == "deepseek-reasoner"

    def test_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        with pytest.raises(ValueError):
            DeepSeekLLMClient()


# ----- end-to-end through the sentiment pipeline -------------------------

def test_score_candidates_with_deepseek_client(tmp_path):
    db = Database(str(tmp_path / "ds.db"))
    db.initialize()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_oai_tool_resp("submit_sentiment", {"scores": [
            {"code": "600519", "score": 0.7, "reason": "利好"},
            {"code": "000001", "score": -0.3, "reason": "利空"}]}))

    client = DeepSeekLLMClient(api_key="k", transport=httpx.MockTransport(handler))

    def fake_news(code, **kw):
        return [{"title": f"{code} 新闻"}]

    scores = score_candidates(db, ["600519", "000001"], client,
                              as_of=date(2026, 6, 1), news_fetcher=fake_news)
    assert scores["600519"] == pytest.approx(0.7)
    assert scores["000001"] == pytest.approx(-0.3)
    # cached
    assert db.get_news_sentiment("600519", "2026-06-01")["score"] == pytest.approx(0.7)
