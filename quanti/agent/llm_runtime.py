"""LLM-driven agent decision loop.

The legacy `AgentRuntime` is rule-driven: it picks a strategy (or ensemble),
generates signals, dispatches. The LLM runtime adds a *judgment layer* on
top of that pipeline:

  1. Ensemble + factor pipeline produces a list of fused BUY candidates
     (same as `agent_mode="ensemble"`).
  2. We hand that list, plus portfolio state and recent decisions, to
     Claude as context.
  3. Claude can call inspection tools to drill into specific positions or
     review decision history.
  4. Claude ends by calling `propose_orders` with its final picks. Each
     proposed order still passes through the RiskManager — the LLM can
     re-weight or skip candidates, but it CANNOT exceed risk limits.

This keeps the LLM *additive*: backtesting, factor scoring, and risk control
remain deterministic and auditable. The LLM's job is judgment about which
of the system's candidates to actually execute, in what conviction.

API key: read from `ANTHROPIC_API_KEY` env var via the anthropic SDK's own
defaults; no explicit handling here.

Safety invariants:
  * Hard token cap per tick (`max_tokens_total`); blow-through ends the tick.
  * Hard tool-call loop cap (`max_tool_iterations`); the LLM cannot endlessly
    inspect without committing.
  * Every proposed order revalidated against RiskManager via PaperBroker.
  * Anthropic import is lazy — projects without the `[llm]` extra can use
    the rest of the system unchanged.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Protocol

from quanti.agent.goal import Goal
from quanti.agent.signal_pipeline import FusedCandidate
from quanti.data.database import Database
from quanti.execution.paper_broker import PaperBroker
from quanti.models import Direction, Signal

logger = logging.getLogger(__name__)

# Default to a fast/cheap model. Override via goal.params["llm_model"].
DEFAULT_MODEL = "claude-sonnet-4-5"

SYSTEM_PROMPT = """You are a quantitative trading assistant for A-share stocks. \
You are working WITH a rule-based pipeline that has already done the heavy lifting: \
walk-forward strategy selection, cross-sectional factor scoring, and basic risk filtering. \
Your job is to apply judgment to the candidate list before execution.

The candidate list comes ranked by a `final_score` ∈ [0, 1] that blends ensemble \
strategy agreement and cross-sectional factors. You should:

1. Look at the candidates and their attribution (which strategies voted, what the \
factor score is, what industry they're in).
2. Cross-reference the current portfolio — don't double-down on positions you \
already hold heavily; don't pile into one industry.
3. Look at recent decisions if needed (e.g. did we just sell this name? Why?).
4. Call `propose_orders` exactly once with your final picks. Each order gets:
   - code: the stock code
   - direction: "buy" (sells are handled by stop-loss machinery, not you)
   - size_pct: target weight as fraction of portfolio (0.01-0.10). The system \
will further clamp this by single-stock risk limits.
   - reason: ONE SHORT Chinese sentence (≤60 chars) explaining your call.

Constraints you MUST respect:
  * NEVER propose more than 5 orders per tick.
  * NEVER propose size_pct > 0.10 (risk manager caps anyway, but make it easy on it).
  * NEVER propose orders for codes not in the candidate list — those haven't been vetted.
  * If you don't see attractive candidates, call propose_orders with an empty `orders=[]`. \
That's a valid "wait" decision.

Be terse. The dashboard renders your `reason` field directly to humans."""


@dataclass
class LLMConfig:
    model: str = DEFAULT_MODEL
    max_tokens: int = 4096
    max_tool_iterations: int = 5
    max_candidates_in_context: int = 20
    max_decisions_in_context: int = 5
    temperature: float = 0.3  # mild creativity, mostly deterministic


# ---------------------------------------------------------- LLM client

class LLMClient(Protocol):
    """Minimal interface so tests can inject canned responses without
    pulling anthropic into the test deps."""

    def create_message(
        self,
        *,
        model: str,
        system: list[dict] | str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        temperature: float,
    ) -> dict:
        """Return a dict shaped like Anthropic's `.messages.create()` result:
            {
              "stop_reason": "tool_use" | "end_turn" | "max_tokens" | ...,
              "content": [
                {"type": "text", "text": "..."},
                {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
              ],
              "usage": {"input_tokens": int, "output_tokens": int},
            }
        """
        ...


class AnthropicLLMClient:
    """Thin wrapper around the official Anthropic SDK with prompt caching.

    Imports anthropic lazily so the module loads even when the `[llm]`
    extra isn't installed — we only fail when someone actually constructs
    a client.
    """

    def __init__(self, api_key: str | None = None) -> None:
        try:
            import anthropic  # type: ignore
        except ImportError as e:
            raise ImportError(
                "anthropic SDK not installed. Install with: pip install -e '.[llm]'"
            ) from e
        self._client = anthropic.Anthropic(api_key=api_key)

    def create_message(self, *, model, system, messages, tools,
                       max_tokens, temperature) -> dict:
        # Anthropic SDK accepts list[TextBlock] or str for system; we use
        # list[dict] form to enable prompt caching on the system block.
        resp = self._client.messages.create(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        # Convert to dict shape for uniformity with tests.
        return {
            "stop_reason": resp.stop_reason,
            "content": [self._block_to_dict(b) for b in resp.content],
            "usage": {
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
                "cache_read_input_tokens": getattr(
                    resp.usage, "cache_read_input_tokens", 0) or 0,
                "cache_creation_input_tokens": getattr(
                    resp.usage, "cache_creation_input_tokens", 0) or 0,
            },
        }

    @staticmethod
    def _block_to_dict(block) -> dict:
        if hasattr(block, "type") and block.type == "text":
            return {"type": "text", "text": block.text}
        if hasattr(block, "type") and block.type == "tool_use":
            return {"type": "tool_use", "id": block.id,
                    "name": block.name, "input": dict(block.input)}
        # Fallback: best-effort dict conversion
        return getattr(block, "__dict__", {"type": "unknown"})


# ----------------------------------------------------------- tool schemas

TOOLS_SCHEMA = [
    {
        "name": "inspect_position",
        "description": "Get current position details for a single stock "
                       "(quantity, average cost, unrealized PnL).",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Stock code, e.g. 000001"}
            },
            "required": ["code"],
        },
    },
    {
        "name": "inspect_decision_history",
        "description": "Return the most recent N decisions made by the agent — "
                       "useful for understanding why a position was opened or "
                       "closed, or if a stock has been repeatedly losing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 20,
                          "description": "How many recent decisions to fetch"}
            },
            "required": ["limit"],
        },
    },
    {
        "name": "propose_orders",
        "description": "Submit final order proposals for this tick. Call exactly "
                       "once to end the decision loop. Empty list = 'do nothing'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "orders": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string"},
                            "direction": {"type": "string", "enum": ["buy"]},
                            "size_pct": {"type": "number",
                                         "minimum": 0.01, "maximum": 0.10},
                            "reason": {"type": "string", "maxLength": 80},
                        },
                        "required": ["code", "direction", "size_pct", "reason"],
                    },
                },
                "reasoning": {
                    "type": "string",
                    "description": "One paragraph (≤300 chars) explaining your "
                                   "overall thinking for this tick. Shown in UI.",
                },
            },
            "required": ["orders"],
        },
    },
]


# ----------------------------------------------------------- decision loop

class LLMDecisionLoop:
    """Drives a Claude conversation to produce a propose_orders tool call.

    Caller responsibilities:
      * Build `context_user_message` (situation summary as a single user msg).
      * Provide a `tool_dispatcher` callable that handles `inspect_*` tool
        calls by returning their result strings.
      * After loop returns: execute proposed orders through the broker.

    The loop bounds itself to `cfg.max_tool_iterations` to prevent infinite
    tool-call cycles when the LLM gets stuck inspecting things.
    """

    def __init__(self, llm: LLMClient, cfg: LLMConfig | None = None) -> None:
        self._llm = llm
        self._cfg = cfg or LLMConfig()

    def run(
        self,
        context_user_message: str,
        tool_dispatcher,
    ) -> tuple[list[dict], str, dict]:
        """Returns (proposed_orders, reasoning, debug_info).

        On any failure (LLM error, max_iterations hit without propose_orders,
        validation error), returns ([], "", debug_info) — the agent falls
        back to "no LLM-proposed orders this tick".
        """
        debug = {"iterations": 0, "tool_calls": [], "usage": {}}
        # Use list-of-blocks form for `system` to enable prompt-cache control.
        system_blocks: list[dict] = [{
            "type": "text", "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }]
        messages: list[dict] = [{"role": "user", "content": context_user_message}]

        proposed_orders: list[dict] = []
        reasoning = ""

        for i in range(self._cfg.max_tool_iterations):
            debug["iterations"] = i + 1
            try:
                resp = self._llm.create_message(
                    model=self._cfg.model,
                    system=system_blocks,
                    messages=messages,
                    tools=TOOLS_SCHEMA,
                    max_tokens=self._cfg.max_tokens,
                    temperature=self._cfg.temperature,
                )
            except Exception as e:
                logger.warning(f"LLM call failed at iter {i}: {e}")
                debug["error"] = str(e)
                return [], "", debug

            debug["usage"] = resp.get("usage", {})
            stop = resp.get("stop_reason")
            blocks = resp.get("content", [])

            tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
            text_blocks = [b for b in blocks if b.get("type") == "text"]
            if not tool_uses:
                # LLM didn't call any tools — treat any text as reasoning and stop.
                reasoning = " ".join(b.get("text", "") for b in text_blocks).strip()
                break

            # Append the assistant's full content to messages so subsequent
            # tool_result blocks reference the right tool_use ids.
            messages.append({"role": "assistant", "content": blocks})

            tool_results: list[dict] = []
            terminal = False
            for tu in tool_uses:
                name = tu.get("name", "")
                tu_id = tu.get("id", "")
                inp = tu.get("input", {}) or {}
                debug["tool_calls"].append({"name": name, "input": inp})
                if name == "propose_orders":
                    proposed_orders = list(inp.get("orders") or [])
                    reasoning = str(inp.get("reasoning") or "")
                    terminal = True
                    break
                # Dispatch inspection tools
                try:
                    result_str = tool_dispatcher(name, inp)
                except Exception as e:
                    result_str = f"[tool error] {e}"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": result_str,
                })

            if terminal:
                break
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            if stop == "end_turn":
                break

        return proposed_orders, reasoning, debug


# ------------------------------------------------------- context builder

def build_context_message(
    goal: Goal,
    portfolio: dict,
    candidates: list[FusedCandidate],
    recent_decisions: list[dict],
    max_candidates: int = 20,
    max_decisions: int = 5,
) -> str:
    """Compact, deterministic textual context for the LLM.

    Deterministic ordering matters because we want Anthropic's prompt cache
    to hit across ticks when the universe and portfolio are stable. Sorted
    by code where applicable, fixed precision on floats.
    """
    lines: list[str] = []
    lines.append("# Goal")
    lines.append(f"- 目标年化: {goal.target_annual_return:.0%}")
    lines.append(f"- 最大回撤容忍: {goal.max_drawdown:.0%}")
    lines.append(f"- 风险偏好: {goal.risk_tolerance.value if hasattr(goal.risk_tolerance, 'value') else goal.risk_tolerance}")
    lines.append(f"- 调仓频率: {goal.rebalance_freq}")
    lines.append("")

    lines.append("# Portfolio")
    lines.append(f"- 总资产: ¥{portfolio.get('total_value', 0):,.0f}")
    lines.append(f"- 现金: ¥{portfolio.get('cash', 0):,.0f}")
    lines.append(f"- 累计盈亏: {portfolio.get('pnl_pct', 0):+.2%}")
    positions = portfolio.get("positions", []) or []
    if positions:
        lines.append("- 当前持仓:")
        for p in sorted(positions, key=lambda p: p.get("code", "")):
            lines.append(f"  - {p.get('code')} {p.get('name', '')} "
                         f"x{p.get('quantity', 0)} "
                         f"均价{p.get('avg_cost', 0):.2f} "
                         f"现价{p.get('current_price', 0):.2f} "
                         f"盈亏 {p.get('pnl_pct', 0):+.2%}")
    else:
        lines.append("- 当前持仓: 空")
    lines.append("")

    lines.append(f"# 候选股 (top {max_candidates}, 按 final_score 降序)")
    for c in candidates[:max_candidates]:
        lines.append(
            f"- {c.code} | final={c.final_score:.2f} "
            f"strat={c.strategy_score:.2f} factor={c.factor_score:+.2f} "
            f"行业={c.industry or '未知'} "
            f"策略={'+'.join(c.contributing_strategies) or '无'}"
        )
    if not candidates:
        lines.append("- (本轮无候选股)")
    lines.append("")

    lines.append(f"# 最近决策 (最多 {max_decisions} 条)")
    for d in recent_decisions[:max_decisions]:
        lines.append(f"- [{d.get('ts', '')}] {d.get('kind', '')}: {d.get('summary', '')}")
    if not recent_decisions:
        lines.append("- (无近期决策)")
    lines.append("")
    lines.append("请基于以上信息调用 propose_orders。")
    return "\n".join(lines)


# ------------------------------------------------------- orchestrator entry

def run_llm_decision(
    *,
    db: Database,
    broker: PaperBroker,
    goal: Goal,
    candidates: list[FusedCandidate],
    llm_client: LLMClient,
    cfg: LLMConfig | None = None,
) -> dict:
    """Top-level entry. Composes context → LLM loop → execution → log.

    Returns a result dict with keys: ok, signals, filled, rejected,
    reasoning, debug, llm_orders.

    On any LLM failure, falls back to "no LLM orders this tick" — the
    legacy ensemble path's signals (if any) are NOT re-run here; the
    caller decides the fallback strategy.
    """
    cfg = cfg or LLMConfig()
    portfolio = broker.snapshot_portfolio()
    recent = db.list_decisions(limit=cfg.max_decisions_in_context)

    ctx = build_context_message(goal, portfolio, candidates, recent,
                                max_candidates=cfg.max_candidates_in_context,
                                max_decisions=cfg.max_decisions_in_context)

    def dispatcher(name: str, inp: dict) -> str:
        if name == "inspect_position":
            code = str(inp.get("code", ""))
            pos = next((p for p in portfolio.get("positions", [])
                        if p.get("code") == code), None)
            if pos is None:
                return json.dumps({"held": False, "code": code})
            return json.dumps({"held": True, **{k: v for k, v in pos.items()
                                                if isinstance(v, (int, float, str))}})
        if name == "inspect_decision_history":
            limit = int(inp.get("limit", 5))
            decisions = db.list_decisions(limit=max(1, min(limit, 20)))
            return json.dumps([{
                "ts": d.get("ts"), "kind": d.get("kind"),
                "summary": d.get("summary"),
            } for d in decisions])
        return json.dumps({"error": f"unknown tool {name}"})

    loop = LLMDecisionLoop(llm_client, cfg)
    proposed, reasoning, debug = loop.run(ctx, dispatcher)

    # Validate proposals: filter to candidates the pipeline already vetted.
    allowed_codes = {c.code for c in candidates}
    valid_orders: list[dict] = []
    rejection_reasons: list[str] = []
    for o in proposed:
        code = str(o.get("code", ""))
        size_pct = float(o.get("size_pct", 0))
        if code not in allowed_codes:
            rejection_reasons.append(f"LLM proposed {code} not in candidate set")
            continue
        if not (0 < size_pct <= 0.10):
            rejection_reasons.append(f"LLM proposed {code} with invalid size_pct={size_pct}")
            continue
        if str(o.get("direction", "")).lower() != "buy":
            rejection_reasons.append(f"LLM proposed non-buy for {code}")
            continue
        valid_orders.append(o)

    # Cap at 5 orders even if LLM ignored its own instruction.
    valid_orders = valid_orders[:5]

    # Convert to Signal objects. The broker's sizer (if configured) will
    # then turn size_pct into a notional. We use signal.strength = size_pct
    # so a FixedSizer with max_pct=0.10 will deploy exactly the LLM's request
    # (strength * max_pct ≈ size_pct when max_pct=0.10).
    signals = [
        Signal(stock_code=o["code"], direction=Direction.BUY,
               strength=min(1.0, float(o["size_pct"]) / 0.10),
               reason=f"LLM: {o.get('reason', '')}")
        for o in valid_orders
    ]

    # Stop-loss first, then LLM-proposed buys.
    sl_count = broker.check_stop_loss()
    result = broker.execute_signals(signals, strategy_name="llm")
    snapshot = broker.snapshot_portfolio()

    log_payload = {
        "model": cfg.model,
        "reasoning": reasoning,
        "n_candidates": len(candidates),
        "n_proposed": len(proposed),
        "n_valid": len(valid_orders),
        "rejections": rejection_reasons,
        "filled": result.filled,
        "stop_loss_filled": sl_count,
        "usage": debug.get("usage", {}),
        "iterations": debug.get("iterations", 0),
    }
    db.log_decision(
        "llm_cycle",
        f"LLM 决策: {len(valid_orders)} 单提议, {result.filled} 成交 — {reasoning[:120]}",
        details=log_payload)

    return {
        "ok": True,
        "signals": len(signals),
        "filled": result.filled,
        "rejected": result.rejected,
        "stop_loss_filled": sl_count,
        "reasoning": reasoning,
        "llm_orders": valid_orders,
        "snapshot": snapshot,
        "debug": debug,
    }
