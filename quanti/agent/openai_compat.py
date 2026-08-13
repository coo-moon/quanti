"""OpenAI-compatible LLM client (DeepSeek, etc.) for the agent runtime.

The rest of the system speaks the Anthropic message shape (system as text
blocks, `content` as a list of text/tool_use blocks, `stop_reason`, `usage`).
DeepSeek — and most non-Anthropic providers — speak the OpenAI Chat
Completions shape instead. This module bridges the two so a DeepSeek key can
drive the *same* sentiment / debate / risk-triad / judgment code with no
changes upstream.

Why httpx and not the openai SDK: quanti already depends on httpx, so this
adds zero new dependencies. The translation is small and explicit:

  request  : Anthropic-style (system, messages, tools)  → OpenAI chat payload
  response : OpenAI choice (content + tool_calls)        → Anthropic blocks

Implements the `LLMClient` Protocol structurally (duck-typed via
`create_message`), so it drops into `run_llm_decision`, `run_debate`,
`run_risk_debate`, and `score_candidates` interchangeably with
`AnthropicLLMClient`.
"""

from __future__ import annotations

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"  # full function-calling support


def _thinking_on_by_default(model: str) -> bool:
    """DeepSeek's explicit v4 ids (deepseek-v4-pro / deepseek-v4-flash) run
    in "thinking" mode unless told otherwise; the legacy `deepseek-chat`
    alias (currently served by v4-flash) does not. Verified live 2026-06-11:
    thinking mode rejects a *forced* tool_choice with HTTP 400, while both
    tool_choice="auto" and thinking={"type": "disabled"} work fine."""
    return model.startswith("deepseek-v4")

_FINISH_TO_STOP = {
    "tool_calls": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "end_turn",
}


# ----------------------------------------------------- translation helpers

def _system_to_text(system) -> str | None:
    """Anthropic `system` (str or list of text blocks) → one system string."""
    if not system:
        return None
    if isinstance(system, str):
        return system
    parts = [b.get("text", "") for b in system
             if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in parts if p) or None


def _block_content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b)
                         for b in content)
    return str(content)


def to_openai_messages(system, messages: list[dict]) -> list[dict]:
    """Flatten Anthropic-style messages to OpenAI chat messages.

    * str content passes through.
    * assistant block-lists → text + `tool_calls`.
    * user block-lists of tool_result → one OpenAI `tool` message each.
    """
    out: list[dict] = []
    sys_text = _system_to_text(system)
    if sys_text:
        out.append({"role": "system", "content": sys_text})

    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        blocks = content or []
        if role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict] = []
            for b in blocks:
                if b.get("type") == "text":
                    text_parts.append(b.get("text", ""))
                elif b.get("type") == "tool_use":
                    tool_calls.append({
                        "id": b.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": b.get("name", ""),
                            "arguments": json.dumps(b.get("input", {}) or {},
                                                    ensure_ascii=False),
                        },
                    })
            msg: dict = {"role": "assistant",
                         "content": " ".join(p for p in text_parts if p) or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
        else:
            text_parts = []
            for b in blocks:
                if b.get("type") == "tool_result":
                    out.append({
                        "role": "tool",
                        "tool_call_id": b.get("tool_use_id", ""),
                        "content": _block_content_to_text(b.get("content", "")),
                    })
                elif b.get("type") == "text":
                    text_parts.append(b.get("text", ""))
            if text_parts:
                out.append({"role": "user",
                            "content": " ".join(p for p in text_parts if p)})
    return out


def to_openai_tools(tools) -> list[dict] | None:
    if not tools:
        return None
    return [{
        "type": "function",
        "function": {
            "name": t.get("name", ""),
            "description": t.get("description", ""),
            "parameters": t.get("input_schema") or {"type": "object",
                                                     "properties": {}},
        },
    } for t in tools]


def from_openai_response(data: dict) -> dict:
    """OpenAI chat completion → Anthropic-shaped response dict."""
    choices = data.get("choices") or []
    choice = choices[0] if choices else {}
    msg = choice.get("message") or {}
    finish = choice.get("finish_reason")

    content_blocks: list[dict] = []
    text = msg.get("content")
    if text:
        content_blocks.append({"type": "text", "text": text})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {}) or {}
        raw = fn.get("arguments", "{}")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except json.JSONDecodeError:
            # finish_reason="length" means max_tokens cut the JSON mid-string —
            # the usual cause, and the one worth naming in the log.
            logger.warning("could not parse tool arguments (finish_reason=%s, "
                           "%d chars): %r", finish, len(raw) if isinstance(raw, str)
                           else 0, raw)
            parsed = {}
        content_blocks.append({
            "type": "tool_use", "id": tc.get("id", ""),
            "name": fn.get("name", ""), "input": parsed,
        })

    usage = data.get("usage", {}) or {}
    return {
        "stop_reason": _FINISH_TO_STOP.get(finish, finish or "end_turn"),
        "content": content_blocks,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0) or 0,
            "output_tokens": usage.get("completion_tokens", 0) or 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }


# ----------------------------------------------------- client

class OpenAICompatLLMClient:
    """Minimal OpenAI Chat Completions client implementing `LLMClient`.

    `transport` lets tests inject an `httpx.MockTransport` so the translation
    can be verified without network or an API key.
    """

    def __init__(self, *, api_key: str, base_url: str, default_model: str,
                 timeout: float = 60.0, transport=None) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"}
        self._default_model = default_model
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def _resolve_model(self, model: str | None) -> str:
        # Callers may pass an Anthropic model id (the runtime's default); fall
        # back to this provider's model rather than sending a bad name.
        if not model or str(model).startswith("claude"):
            return self._default_model
        return str(model)

    def resolved_model(self, model: str | None) -> str:
        """The model id actually sent for a requested (possibly Anthropic)
        name — lets callers log ground truth instead of the alias."""
        return self._resolve_model(model)

    # 对暂时性失败(限流/服务端错误/超时)的指数退避重试。止损点位与盘中
    # 守护的决策链现在都过这个 client——之前全链路零重试,一次 429 就等于
    # 本轮空手(红队可用性面 F1)。4xx(除 429)不重试:请求本身有病。
    _RETRIES = 2
    _BACKOFF_SEC = (1.0, 4.0)

    def create_message(self, *, model, system, messages, tools,
                        max_tokens, temperature) -> dict:
        payload: dict = {
            "model": self._resolve_model(model),
            "messages": to_openai_messages(system, messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        oai_tools = to_openai_tools(tools)
        if oai_tools:
            payload["tools"] = oai_tools
            # Exactly one tool → force it, so structured-output flows
            # (sentiment, risk review) reliably return the call. With several
            # tools (the judgment loop) leave it to the model.
            if len(oai_tools) == 1:
                payload["tool_choice"] = {
                    "type": "function",
                    "function": {"name": oai_tools[0]["function"]["name"]},
                }
                # v4 thinking mode 400s on a forced tool_choice, and pure
                # structured-output calls gain nothing from CoT — turn it
                # off for this request only. Free-text and multi-tool calls
                # keep thinking (that's what the v4 thinking tier is for).
                if _thinking_on_by_default(payload["model"]):
                    payload["thinking"] = {"type": "disabled"}
        import time as _time
        last_exc: Exception | None = None
        for attempt in range(self._RETRIES + 1):
            try:
                resp = self._client.post(self._url, headers=self._headers,
                                         json=payload)
                resp.raise_for_status()
                return from_openai_response(resp.json())
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status != 429 and status < 500:
                    raise  # client error — retrying the same payload is futile
                last_exc = e
            except httpx.HTTPError as e:  # timeouts, transport errors
                last_exc = e
            if attempt < self._RETRIES:
                delay = self._BACKOFF_SEC[min(attempt, len(self._BACKOFF_SEC) - 1)]
                logger.warning("LLM call failed (%s), retry %d/%d in %.0fs",
                               last_exc, attempt + 1, self._RETRIES, delay)
                _time.sleep(delay)
        raise last_exc

    def close(self) -> None:
        self._client.close()


class DeepSeekLLMClient(OpenAICompatLLMClient):
    """DeepSeek preset. Reads DEEPSEEK_API_KEY unless `api_key` is passed."""

    def __init__(self, api_key: str | None = None, *, base_url: str | None = None,
                 default_model: str = DEEPSEEK_DEFAULT_MODEL,
                 timeout: float = 60.0, transport=None) -> None:
        key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise ValueError(
                "DEEPSEEK_API_KEY not set (export it or pass api_key=...)")
        super().__init__(api_key=key, base_url=base_url or DEEPSEEK_BASE_URL,
                         default_model=default_model, timeout=timeout,
                         transport=transport)
