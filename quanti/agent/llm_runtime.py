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
from dataclasses import dataclass
from typing import Protocol

from quanti.agent.goal import Goal
from quanti.agent.signal_pipeline import FusedCandidate, select_rotation_sells
from quanti.data.database import Database
from quanti.execution.base import Broker
from quanti.models import Direction, Signal

logger = logging.getLogger(__name__)

# Default to a fast/cheap model. Override via goal.params["llm_model"].
DEFAULT_MODEL = "claude-sonnet-4-6"

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


# Debate personas. Bull and Bear argue over the SAME candidate context; their
# transcript is then handed to the judgment pass (SYSTEM_PROMPT above) which
# acts as the research manager and makes the actual propose_orders call.
BULL_SYSTEM = """你是 A 股交易台的【多头研究员】。系统已经给出一份经过走查选股 + \
因子打分(可能含新闻情绪)筛过的候选买入清单,以及当前组合。请论证看多逻辑:\
这些候选里哪些此刻最值得买入、理由是什么(动量、因子共振、利好新闻、分散化等)。\
要点要具体、点名股票代码。最多 6 条要点,中文。你不做最终决策,只负责进攻性论证。"""

BEAR_SYSTEM = """你是 A 股交易台的【空头研究员】。你看到同一份候选清单、当前组合,\
以及多头的论点。请论证看空/回避逻辑:哪些候选应当回避或减小仓位,多头忽视了哪些风险\
(追高、拥挤、利空、行业集中、因子弱、相对目标的回撤等),并针对多头论点逐条反驳。\
最多 6 条要点,中文。你不做最终决策,只负责唱反调。"""


@dataclass
class LLMConfig:
    model: str = DEFAULT_MODEL
    max_tokens: int = 4096
    max_tool_iterations: int = 5
    max_candidates_in_context: int = 20
    max_decisions_in_context: int = 5
    temperature: float = 0.3  # mild creativity, mostly deterministic
    debate_enabled: bool = False     # run a Bull/Bear debate before judgment
    debate_rounds: int = 1           # Bull→Bear exchanges before the manager decides
    risk_debate_enabled: bool = False  # aggressive/neutral/conservative size review
    reflection_enabled: bool = False   # inject outcome-keyed reflections into context
    max_reflections: int = 8


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


def build_llm_client(params: dict) -> LLMClient:
    """Build an LLM client from goal params.

    Checks ``params["llm_provider"]`` (default: ``"anthropic"``).
    ``"deepseek"`` or ``"openai_compat"`` → :class:`DeepSeekLLMClient`;
    anything else → :class:`AnthropicLLMClient`.

    Raises if the required SDK / API key is missing — callers should wrap in
    try/except and fall back gracefully.
    """
    provider = str(params.get("llm_provider", "anthropic")).lower()
    if provider in ("deepseek", "openai_compat"):
        from quanti.agent.openai_compat import DeepSeekLLMClient  # noqa: PLC0415

        return DeepSeekLLMClient()
    return AnthropicLLMClient()


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


# ------------------------------------------------------- bull/bear debate

def _complete_text(llm: LLMClient, system_text: str, user_text: str,
                   cfg: LLMConfig) -> str:
    """Single text completion with a persona system prompt (no tools)."""
    system = [{
        "type": "text", "text": system_text,
        "cache_control": {"type": "ephemeral"},
    }]
    resp = llm.create_message(
        model=cfg.model, system=system,
        messages=[{"role": "user", "content": user_text}],
        tools=[], max_tokens=cfg.max_tokens, temperature=cfg.temperature,
    )
    parts = [b.get("text", "") for b in resp.get("content", []) or []
             if b.get("type") == "text"]
    return " ".join(p for p in parts if p).strip()


def _debate_user(base_context: str, bull: str, bear: str, role: str) -> str:
    parts = [base_context, ""]
    if bull:
        parts += ["# 已有多头观点(BULL)", bull, ""]
    if bear:
        parts += ["# 已有空头观点(BEAR)", bear, ""]
    parts.append("请给出你的多头论点。" if role == "bull"
                 else "请针对多头论点给出你的空头反驳。")
    return "\n".join(parts)


def _format_transcript(rounds: list[dict]) -> str:
    out: list[str] = []
    for r in rounds:
        out.append(f"## 第 {r['round']} 轮")
        out.append(f"多头: {r.get('bull', '')}")
        out.append(f"空头: {r.get('bear', '')}")
        out.append("")
    return "\n".join(out).strip()


def run_debate(llm: LLMClient, base_context: str,
               cfg: LLMConfig) -> tuple[str, list[dict]]:
    """Run `cfg.debate_rounds` Bull→Bear exchanges over the candidate context.

    Returns (formatted_transcript, rounds). Each round adds two LLM calls
    (still within the per-tick token budget the caller controls via cfg).
    Degrades to ("", rounds_so_far) on any failure so the caller falls back
    to direct judgment without the debate.
    """
    rounds: list[dict] = []
    bull_prev = bear_prev = ""
    try:
        for r in range(max(1, cfg.debate_rounds)):
            bull = _complete_text(
                llm, BULL_SYSTEM,
                _debate_user(base_context, bull_prev, bear_prev, "bull"), cfg)
            bear = _complete_text(
                llm, BEAR_SYSTEM,
                _debate_user(base_context, bull, bear_prev, "bear"), cfg)
            rounds.append({"round": r + 1, "bull": bull, "bear": bear})
            bull_prev, bear_prev = bull, bear
    except Exception as e:
        logger.warning(f"debate failed, falling back to direct judgment: {e}")
        return "", rounds
    return _format_transcript(rounds), rounds


# ------------------------------------------------- risk-debate triad

RISK_AGGRESSIVE_SYSTEM = """你是交易台的【激进风控】。在守住底线风险的前提下,你倾向\
尽量保留仓位、抓住机会。审阅经理提议的买入清单,对每个订单给出 keep_pct∈[0,1](保留\
该订单提议仓位的比例)。仅当标的与现有持仓高度重叠、或组合已逼近回撤容忍线时才下调;\
否则倾向 keep_pct=1.0。调用 submit_risk_review 一次。"""

RISK_NEUTRAL_SYSTEM = """你是交易台的【中性风控】。你在机会与风险间求平衡。审阅经理提议\
的买入清单,综合单票集中度、行业集中度、与目标回撤的距离,对每个订单给出 keep_pct∈\
[0,1]。调用 submit_risk_review 一次。"""

RISK_CONSERVATIVE_SYSTEM = """你是交易台的【保守风控】。你优先保护本金、压低回撤。对追高、\
拥挤、与现有持仓/行业重叠、临近回撤容忍线的订单果断下调甚至否决(keep_pct=0)。对每个\
订单给出 keep_pct∈[0,1]。调用 submit_risk_review 一次。"""

RISK_TOOL: list[dict] = [{
    "name": "submit_risk_review",
    "description": "Return a keep_pct ∈ [0,1] per proposed order "
                   "(fraction of the manager's size to keep; 0 = veto). "
                   "Call exactly once.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reviews": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                        "keep_pct": {"type": "number",
                                     "minimum": 0, "maximum": 1},
                        "reason": {"type": "string", "maxLength": 60},
                    },
                    "required": ["code", "keep_pct"],
                },
            },
        },
        "required": ["reviews"],
    },
}]


def _risk_review_one(llm: LLMClient, system_text: str, orders: list[dict],
                     portfolio: dict, goal, cfg: LLMConfig) -> dict[str, float]:
    """One risk persona reviews all orders → { code → keep_pct }."""
    lines = ["经理提议的买入订单:"]
    for o in orders:
        lines.append(f"- {o.get('code')} size_pct={float(o.get('size_pct', 0)):.3f} "
                     f"理由:{o.get('reason', '')}")
    lines.append("")
    lines.append(f"组合: 总资产 ¥{portfolio.get('total_value', 0):,.0f}, "
                 f"现金 ¥{portfolio.get('cash', 0):,.0f}, "
                 f"累计盈亏 {portfolio.get('pnl_pct', 0):+.1%}")
    positions = portfolio.get("positions", []) or []
    if positions:
        held = ", ".join(f"{p.get('code')}({p.get('pnl_pct', 0):+.0%})"
                         for p in positions)
        lines.append(f"当前持仓: {held}")
    rt = goal.risk_tolerance.value if hasattr(goal.risk_tolerance, "value") \
        else goal.risk_tolerance
    lines.append(f"目标: 年化 {goal.target_annual_return:.0%}, "
                 f"最大回撤容忍 {goal.max_drawdown:.0%}, 风险偏好 {rt}")
    lines.append("")
    lines.append("对每个订单给出 keep_pct∈[0,1](保留经理提议仓位的比例,0=否决),"
                 "调用 submit_risk_review 一次。")

    system = [{"type": "text", "text": system_text,
               "cache_control": {"type": "ephemeral"}}]
    resp = llm.create_message(
        model=cfg.model, system=system,
        messages=[{"role": "user", "content": "\n".join(lines)}],
        tools=RISK_TOOL, max_tokens=cfg.max_tokens, temperature=cfg.temperature,
    )
    out: dict[str, float] = {}
    for block in resp.get("content", []) or []:
        if block.get("type") == "tool_use" and block.get("name") == "submit_risk_review":
            for r in (block.get("input", {}) or {}).get("reviews", []) or []:
                code = str(r.get("code", "")).strip()
                if not code:
                    continue
                try:
                    kp = float(r.get("keep_pct", 1.0))
                except (TypeError, ValueError):
                    kp = 1.0
                out[code] = max(0.0, min(1.0, kp))
    return out


def run_risk_debate(llm: LLMClient, orders: list[dict], portfolio: dict,
                    goal, cfg: LLMConfig) -> dict[str, float]:
    """Aggressive/Neutral/Conservative each review order sizes; aggregate to
    one keep_pct per code by the goal's risk tolerance (low→min, medium→mean,
    high→max). keep_pct ≤ 1 by construction, so this can only shrink/veto the
    manager's sizes, never inflate them. Degrades to {} (no change) on error.
    """
    try:
        agg = _risk_review_one(llm, RISK_AGGRESSIVE_SYSTEM, orders, portfolio, goal, cfg)
        neu = _risk_review_one(llm, RISK_NEUTRAL_SYSTEM, orders, portfolio, goal, cfg)
        con = _risk_review_one(llm, RISK_CONSERVATIVE_SYSTEM, orders, portfolio, goal, cfg)
    except Exception as e:
        logger.warning(f"risk debate failed, leaving sizes unchanged: {e}")
        return {}
    rt = (goal.risk_tolerance.value if hasattr(goal.risk_tolerance, "value")
          else str(goal.risk_tolerance)).lower()
    out: dict[str, float] = {}
    for o in orders:
        code = o.get("code", "")
        vals = [d.get(code, 1.0) for d in (agg, neu, con)]
        if rt == "low":
            keep = min(vals)
        elif rt == "high":
            keep = max(vals)
        else:
            keep = sum(vals) / len(vals)
        out[code] = max(0.0, min(1.0, keep))
    return out


# ------------------------------------------------------- context builder

def build_context_message(
    goal: Goal,
    portfolio: dict,
    candidates: list[FusedCandidate],
    recent_decisions: list[dict],
    max_candidates: int = 20,
    max_decisions: int = 5,
    reflections: list[dict] | None = None,
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
        sent = (f" 情绪={c.sentiment_score:+.2f}"
                if getattr(c, "sentiment_score", 0.0) else "")
        lines.append(
            f"- {c.code} | final={c.final_score:.2f} "
            f"strat={c.strategy_score:.2f} factor={c.factor_score:+.2f}{sent} "
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

    if reflections:
        lines.append("# 历史经验 (按相关度, 绑定已实现盈亏)")
        for it in reflections:
            lines.append(f"- {it.get('text', '')}")
        lines.append("")

    lines.append("请基于以上信息调用 propose_orders。")
    return "\n".join(lines)


# ------------------------------------------------------- orchestrator entry

def _rotation_sells_if_enabled(
    db: Database, broker: Broker,
    candidates: list[FusedCandidate], valid_orders: list[dict],
) -> list[Signal]:
    """SELLs that free a slot for a stronger LLM pick when the book is full.

    Opt-in via RiskConfig.rotation_enabled (read live from the DB). The LLM's
    chosen buys are scored by their candidate final_score; the weakest holding
    is swapped out iff a pick beats it by rotation_margin. Returns [] when
    disabled or nothing qualifies. Never raises — rotation is a convenience,
    not a safety mechanism.
    """
    try:
        from quanti.risk.manager import risk_config_from_dict
        rc = risk_config_from_dict(db.get_risk_config())
        if not rc.rotation_enabled or not valid_orders:
            return []
        score_by_code = {c.code: c.final_score for c in candidates}
        # LLM picks, strongest first by candidate score.
        buy_codes = sorted((o["code"] for o in valid_orders),
                           key=lambda c: score_by_code.get(c, 0.0), reverse=True)
        pf = broker.snapshot_portfolio()
        held_mv = {p["code"]: float(p.get("market_value", 0.0) or 0.0)
                   for p in pf.get("positions", [])}
        return select_rotation_sells(
            buy_codes, score_by_code, held_mv,
            float(pf.get("cash", 0.0) or 0.0),
            float(pf.get("total_value", 0.0) or 0.0),
            margin=rc.rotation_margin, max_position_pct=rc.max_position_pct)
    except Exception as e:  # noqa: BLE001 - rotation must never break a tick
        logger.warning(f"rotation skipped: {e}")
        return []


def run_llm_decision(
    *,
    db: Database,
    broker: Broker,
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

    # Outcome-keyed reflections: relevant past round-trips bound to realized
    # P&L, replacing pure "recent N" with "relevant N". Read-only, no LLM cost.
    reflections: list[dict] = []
    if cfg.reflection_enabled:
        try:
            from quanti.agent.reflection import build_reflections
            reflections = build_reflections(db, candidates,
                                             max_items=cfg.max_reflections)
        except Exception as e:
            logger.warning(f"reflection build failed, skipping: {e}")

    ctx = build_context_message(goal, portfolio, candidates, recent,
                                max_candidates=cfg.max_candidates_in_context,
                                max_decisions=cfg.max_decisions_in_context,
                                reflections=reflections)

    # Optional Bull/Bear debate. The transcript is appended to the context so
    # the judgment loop below acts as the research manager weighing both sides.
    debate_rounds: list[dict] = []
    if cfg.debate_enabled:
        transcript, debate_rounds = run_debate(llm_client, ctx, cfg)
        if transcript:
            ctx = (ctx + "\n\n# 多空辩论\n" + transcript +
                   "\n\n以上为多空研究员的辩论。请作为研究主管,"
                   "权衡双方观点后调用 propose_orders。")

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

    # Optional risk-debate triad. Aggressive/Neutral/Conservative reviewers
    # return a keep_pct ∈ [0, 1] per order; aggregation follows the goal's risk
    # tolerance (low→min, medium→mean, high→max). They can only SHRINK or veto
    # the manager's size — never exceed it — and the mechanical RiskManager
    # still gates every resulting order downstream.
    risk_review: dict[str, float] = {}
    if cfg.risk_debate_enabled and valid_orders:
        risk_review = run_risk_debate(llm_client, valid_orders, portfolio, goal, cfg)
        if risk_review:
            kept: list[dict] = []
            for o in valid_orders:
                keep = risk_review.get(o["code"], 1.0)
                new_size = float(o.get("size_pct", 0)) * keep
                if new_size >= 0.01:
                    kept.append({**o, "size_pct": new_size})
                else:
                    rejection_reasons.append(
                        f"risk triad cut {o['code']} below floor (keep={keep:.2f})")
            valid_orders = kept

    # Convert to Signal objects. The broker's sizer (if configured) will
    # then turn size_pct into a notional. We use signal.strength = size_pct
    # so a FixedSizer with max_pct=0.10 will deploy exactly the LLM's request
    # (strength * max_pct ≈ size_pct when max_pct=0.10).
    # Carry each candidate's dominant strategy onto the buy signal so the
    # position records who to replay at exit (the LLM picks FROM candidates,
    # so the ensemble's strategy attribution still applies).
    dominant_by_code = {c.code: c.dominant_strategy for c in candidates}
    signals = [
        Signal(stock_code=o["code"], direction=Direction.BUY,
               strength=min(1.0, float(o["size_pct"]) / 0.10),
               reason=f"LLM: {o.get('reason', '')}",
               entry_strategy=dominant_by_code.get(o["code"], ""))
        for o in valid_orders
    ]

    # Exits first (stop-loss / strategy / take-profit), then LLM buys.
    sl_count = broker.check_exits()

    # Score-gated rotation (换仓, opt-in): if the book is full, free the weakest
    # holding so a clearly-stronger LLM pick can be funded. The LLM's picks are
    # a subset of `candidates`, so their final_score still applies. Runs after
    # check_exits (post-exit cash/positions) and before the buys, so the freed
    # cash funds them.
    rot_sells = _rotation_sells_if_enabled(db, broker, candidates, valid_orders)
    if rot_sells:
        broker.execute_signals(rot_sells, strategy_name="rotation")
        db.log_decision(
            "rotation",
            f"换仓 释放 {len(rot_sells)} 个弱仓为更强候选腾位: "
            + ", ".join(s.stock_code for s in rot_sells),
            details={"sells": [{"code": s.stock_code, "reason": s.reason}
                               for s in rot_sells]})

    result = broker.execute_signals(signals, strategy_name="llm")
    snapshot = broker.snapshot_portfolio()

    log_payload = {
        # Ground truth, not the requested alias: provider clients may remap
        # (e.g. claude-* → deepseek-v4-pro). Anthropic client has no remap.
        "model": getattr(llm_client, "resolved_model", lambda m: m)(cfg.model),
        "reasoning": reasoning,
        "n_candidates": len(candidates),
        "n_proposed": len(proposed),
        "n_valid": len(valid_orders),
        "rejections": rejection_reasons,
        "filled": result.filled,
        "stop_loss_filled": sl_count,
        "usage": debug.get("usage", {}),
        "iterations": debug.get("iterations", 0),
        "debate_rounds": debate_rounds,
        "risk_review": risk_review,
        "n_reflections": len(reflections),
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
        "debate": debate_rounds,
        "risk_review": risk_review,
        "reflections": reflections,
        "debug": debug,
    }
