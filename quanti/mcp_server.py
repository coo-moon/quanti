"""Quanti MCP server — exposes the agent over stdio JSON-RPC.

Implements a minimal Model Context Protocol server (spec 2024-11-05) over
stdio so any MCP-aware client (Claude Desktop, OpenClaw, Cursor, etc.) can
drive the trading agent.

Run with:    quanti mcp
Or:          python -m quanti.mcp_server

Tools surfaced:
  list_strategies / list_screeners / list_pools
  set_goal / get_goal
  agent_start / agent_stop / agent_status / agent_tick
  get_portfolio / list_positions / list_orders / list_trades
  list_decisions
  place_order            (manual override)
  run_backtest           (try a strategy without affecting the live book)
  run_screener           (top-N stock candidates)
  sync_stocks / sync_quotes
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import date, timedelta
from typing import Any

from quanti.agent.goal import Goal, RiskTolerance, load_goal, save_goal
from quanti.agent.runtime import AgentRuntime
from quanti.backtest.engine import BacktestEngine
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.paper_broker import PaperBroker
from quanti.models import Direction, Signal
from quanti.screener.loader import ScreenerLoader
from quanti.strategy.loader import StrategyLoader

logger = logging.getLogger("quanti.mcp")

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "quanti"
SERVER_VERSION = "0.2.0"


# --------------------------------------------------------------- runtime
class QuantiContext:
    """Bundle of long-lived objects the MCP tools work against."""

    def __init__(self, db_path: str = "data/quanti.db",
                 strategies_dir: str = "strategies",
                 screeners_dir: str = "screeners",
                 initial_cash: float = 1_000_000.0) -> None:
        self.db = Database(db_path)
        self.db.initialize()
        self.provider = DataProvider(self.db)
        self.strategies_dir = strategies_dir
        self.screeners_dir = screeners_dir
        self.broker = PaperBroker(self.db, self.provider, initial_cash=initial_cash,
                                  strategies_dir=strategies_dir)
        self.agent = AgentRuntime(
            self.db, self.provider, self.broker,
            strategies_dir=strategies_dir, screeners_dir=screeners_dir,
        )


# --------------------------------------------------------------- tools
def _tool_specs() -> list[dict]:
    return [
        {
            "name": "list_strategies",
            "description": "列出所有可用的策略名称",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "list_screeners",
            "description": "列出所有可用的选股器",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "list_pools",
            "description": "列出所有股票池",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_goal",
            "description": "获取当前 Agent 的目标设定",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "set_goal",
            "description": "设定/更新 Agent 目标（年化收益、最大回撤、风险偏好、策略/选股/"
                           "股票池）。LLM 决策与多智能体增强开关都放在 params 里，见其说明。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target_annual_return": {"type": "number"},
                    "max_drawdown": {"type": "number"},
                    "risk_tolerance": {"type": "string",
                                       "enum": ["low", "medium", "high"]},
                    "universe_pool": {"type": "string"},
                    "screener_name": {"type": "string"},
                    "strategy_name": {"type": "string"},
                    "params": {
                        "type": "object",
                        "description": (
                            "策略调参与 LLM 增强开关；未列出的键原样透传。常用：\n"
                            "  agent_mode: ''|'llm'（'llm' 启用 LLM 决策路）\n"
                            "  ensemble_enabled: bool（Top-K 策略融合）\n"
                            "  llm_provider: 'anthropic'|'deepseek'；llm_model: str\n"
                            "    （DeepSeek 默认 'deepseek-v4-pro'，需 DEEPSEEK_API_KEY）\n"
                            "  sentiment_enabled + sentiment_blend(0~1)：①新闻情绪 overlay\n"
                            "  llm_debate + llm_debate_rounds：②多空辩论\n"
                            "  llm_risk_debate：③风控三角（激进/中性/保守，只能缩仓/否决）\n"
                            "  llm_reflection + llm_max_reflections：④历史经验（按相关度+已实现盈亏）"),
                        "properties": {
                            "agent_mode": {"type": "string", "enum": ["", "llm"]},
                            "ensemble_enabled": {"type": "boolean"},
                            "llm_provider": {"type": "string",
                                             "enum": ["anthropic", "deepseek"]},
                            "llm_model": {"type": "string"},
                            "sentiment_enabled": {"type": "boolean"},
                            "sentiment_blend": {"type": "number",
                                                "minimum": 0, "maximum": 1},
                            "sentiment_max_codes": {"type": "integer"},
                            "llm_debate": {"type": "boolean"},
                            "llm_debate_rounds": {"type": "integer"},
                            "llm_risk_debate": {"type": "boolean"},
                            "llm_reflection": {"type": "boolean"},
                            "llm_max_reflections": {"type": "integer"},
                        },
                        "additionalProperties": True,
                    },
                    "rebalance_freq": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
            },
        },
        {
            "name": "agent_start",
            "description": "启动 Agent 自治循环",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "agent_stop",
            "description": "停止 Agent 自治循环",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "agent_status",
            "description": "查询 Agent 当前状态与最近一次决策",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "agent_tick",
            "description": "强制 Agent 跑一次完整周期（同步→选股→评估策略→生成信号→风控→模拟下单）",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_portfolio",
            "description": "返回当前组合（现金、持仓、市值、盈亏）",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "list_orders",
            "description": "列出最近的订单",
            "inputSchema": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 100}},
            },
        },
        {
            "name": "list_trades",
            "description": "列出最近的成交记录",
            "inputSchema": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 100}},
            },
        },
        {
            "name": "list_decisions",
            "description": "查看 Agent 决策日志（cycle、trade、risk_reject、agent_start 等）",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 50},
                    "kind": {"type": "string"},
                },
            },
        },
        {
            "name": "prune_decisions",
            "description": "清理 N 天前的决策日志，控制 DB 体积",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "older_than_days": {"type": "integer", "default": 90},
                },
            },
        },
        {
            "name": "place_order",
            "description": "手动下单（OpenClaw / 操作员覆盖）。direction 必填 buy|sell。",
            "inputSchema": {
                "type": "object",
                "required": ["code", "direction"],
                "properties": {
                    "code": {"type": "string"},
                    "direction": {"type": "string", "enum": ["buy", "sell"]},
                    "strength": {"type": "number", "default": 1.0,
                                 "description": "0~1，作为目标仓位占比"},
                    "reason": {"type": "string"},
                },
            },
        },
        {
            "name": "run_backtest",
            "description": "试跑一次回测，不影响实盘账户",
            "inputSchema": {
                "type": "object",
                "required": ["strategy_name", "codes"],
                "properties": {
                    "strategy_name": {"type": "string"},
                    "codes": {"type": "array", "items": {"type": "string"}},
                    "start": {"type": "string", "description": "YYYY-MM-DD"},
                    "end": {"type": "string"},
                    "initial_cash": {"type": "number", "default": 1000000},
                    "params": {"type": "object"},
                },
            },
        },
        {
            "name": "run_screener",
            "description": "跑选股器返回 top-N",
            "inputSchema": {
                "type": "object",
                "required": ["screener_name"],
                "properties": {
                    "screener_name": {"type": "string"},
                    "codes": {"type": "array", "items": {"type": "string"}},
                    "top_n": {"type": "integer", "default": 20},
                    "lookback_days": {"type": "integer", "default": 120},
                },
            },
        },
        {
            "name": "sync_stocks",
            "description": "同步 A 股股票列表（名称/行业）",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "sync_quotes",
            "description": "同步指定股票的最近 1 年 K 线",
            "inputSchema": {
                "type": "object",
                "required": ["codes"],
                "properties": {
                    "codes": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    ]


def _handle_tool_call(ctx: QuantiContext, name: str, args: dict[str, Any]) -> Any:
    if name == "list_strategies":
        loader = StrategyLoader()
        return [
            {
                "name": s.name,
                "name_zh": getattr(s, "name_zh", "") or s.name,
                "description": getattr(s, "description", "") or "",
            }
            for s in loader.load_directory(ctx.strategies_dir)
        ]
    if name == "list_screeners":
        loader = ScreenerLoader()
        return [
            {
                "name": s.name,
                "name_zh": getattr(s, "name_zh", "") or s.name,
                "description": s.description,
            }
            for s in loader.load_directory(ctx.screeners_dir)
        ]
    if name == "list_pools":
        return ctx.db.list_pools()
    if name == "get_goal":
        return load_goal(ctx.db).to_db()
    if name == "set_goal":
        current = load_goal(ctx.db)
        merged = current.to_db()
        merged.update({k: v for k, v in args.items() if v is not None})
        # Coerce + validate the numeric / enum fields before we write back —
        # the DB has no schema-level type guard so junk would silently rot
        # until the next agent tick failed at runtime.
        try:
            target = float(merged["target_annual_return"])
            dd = float(merged["max_drawdown"])
        except (TypeError, ValueError) as e:
            return {"error": f"target_annual_return/max_drawdown must be numbers: {e}"}
        try:
            risk = RiskTolerance(merged["risk_tolerance"])
        except ValueError as e:
            return {"error": f"invalid risk_tolerance: {e}"}
        params = merged.get("params", {})
        if not isinstance(params, dict):
            return {"error": f"params must be an object, got {type(params).__name__}"}
        save_goal(ctx.db, Goal(
            target_annual_return=target,
            max_drawdown=dd,
            risk_tolerance=risk,
            universe_pool=str(merged.get("universe_pool", "") or ""),
            screener_name=str(merged.get("screener_name", "") or ""),
            strategy_name=str(merged.get("strategy_name", "") or ""),
            params=params,
            rebalance_freq=str(merged.get("rebalance_freq", "daily") or "daily"),
            enabled=bool(merged.get("enabled", False)),
        ))
        return {"ok": True, "goal": load_goal(ctx.db).to_db()}
    if name == "agent_start":
        ctx.agent.start()
        return {"status": "started"}
    if name == "agent_stop":
        ctx.agent.stop()
        return {"status": "stopped"}
    if name == "agent_status":
        s = ctx.agent.status()
        return {
            "enabled": s.enabled, "running": s.running,
            "last_tick_at": s.last_tick_at,
            "last_tick_summary": s.last_tick_summary,
            "last_strategy": s.last_strategy,
            "last_evaluations": s.last_evaluations,
            "total_value": s.total_value, "pnl_pct": s.pnl_pct,
        }
    if name == "agent_tick":
        return ctx.agent.tick()
    if name == "get_portfolio":
        return ctx.broker.snapshot_portfolio()
    if name == "list_orders":
        return ctx.db.list_orders(limit=int(args.get("limit", 100)))
    if name == "list_trades":
        return ctx.db.list_trades(limit=int(args.get("limit", 100)))
    if name == "list_decisions":
        return ctx.db.list_decisions(limit=int(args.get("limit", 50)),
                                     kind=args.get("kind"))
    if name == "prune_decisions":
        days = int(args.get("older_than_days", 90))
        removed = ctx.db.prune_decisions(days)
        return {"removed": removed, "older_than_days": days}
    if name == "place_order":
        signal = Signal(
            stock_code=args["code"],
            direction=Direction(args["direction"]),
            strength=float(args.get("strength", 1.0)),
            reason=args.get("reason", "manual via MCP"),
        )
        filled = ctx.broker.execute_signal(signal, strategy_name="manual_mcp")
        return {"filled": filled, "snapshot": ctx.broker.snapshot_portfolio()}
    if name == "run_backtest":
        loader = StrategyLoader()
        strategies = loader.load_directory(ctx.strategies_dir)
        strat = next((s for s in strategies if s.name == args["strategy_name"]), None)
        if strat is None:
            return {"error": f"strategy {args['strategy_name']} not found"}
        strat.init(args.get("params") or {})
        end = date.fromisoformat(args["end"]) if args.get("end") else date.today()
        start = (date.fromisoformat(args["start"])
                 if args.get("start") else end - timedelta(days=365))
        engine = BacktestEngine(provider=ctx.provider,
                                initial_cash=float(args.get("initial_cash", 1_000_000)))
        bt = engine.run(strat, args["codes"], start, end)
        return {
            "metrics": bt.metrics,
            "trades": len(bt.trades),
            "skipped_signals": bt.skipped_signals,
            "warning": bt.skip_reason,
        }
    if name == "run_screener":
        loader = ScreenerLoader()
        screeners = loader.load_directory(ctx.screeners_dir)
        scr = next((s for s in screeners if s.name == args["screener_name"]), None)
        if scr is None:
            return {"error": f"screener {args['screener_name']} not found"}
        scr.init({})
        end = date.today()
        start = end - timedelta(days=int(args.get("lookback_days", 120)))
        codes = args.get("codes") or [s.code for s in ctx.db.list_stocks()]
        results: list[dict] = []
        for c in codes:
            bars = ctx.provider.get_daily_bars(c, start, end)
            if len(bars) < 20:
                continue
            score = scr.screen(c, bars)
            if score > 0:
                stock = ctx.db.get_stock(c)
                results.append({
                    "code": c,
                    "name": stock.name if stock else c,
                    "score": round(score, 4),
                    "close": bars[-1].close,
                })
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[: int(args.get("top_n", 20))]
    if name == "sync_stocks":
        from quanti.data.akshare_adapter import AkShareAdapter
        adapter = AkShareAdapter(ctx.db)
        return {"synced": adapter.sync_stock_list()}
    if name == "sync_quotes":
        from quanti.data.akshare_adapter import AkShareAdapter
        adapter = AkShareAdapter(ctx.db)
        out: dict[str, int] = {}
        for c in args["codes"]:
            try:
                out[c] = adapter.sync_daily_quotes(c, repair_gaps=False)
            except Exception:
                out[c] = 0
        return out
    return {"error": f"unknown tool: {name}"}


# --------------------------------------------------------------- protocol
def _ok(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, msg: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": msg}}


def _format_tool_result(payload: Any) -> dict:
    text = json.dumps(payload, ensure_ascii=False, default=str, indent=2)
    return {"content": [{"type": "text", "text": text}]}


def serve_stdio(db_path: str = "data/quanti.db") -> None:
    """Main stdio loop. One JSON message per line in/out."""
    ctx = QuantiContext(db_path=db_path)
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        stream=sys.stderr)
    logger.info(f"Quanti MCP server starting (db={db_path})")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        method = req.get("method")
        req_id = req.get("id")
        params = req.get("params") or {}
        try:
            if method == "initialize":
                resp = _ok(req_id, {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                })
            elif method == "initialized" or method == "notifications/initialized":
                continue  # notification, no response
            elif method == "tools/list":
                resp = _ok(req_id, {"tools": _tool_specs()})
            elif method == "tools/call":
                name = params.get("name")
                args = params.get("arguments") or {}
                try:
                    result = _handle_tool_call(ctx, name, args)
                    resp = _ok(req_id, _format_tool_result(result))
                except Exception as e:
                    tb = traceback.format_exc()
                    resp = _ok(req_id, {
                        "content": [{"type": "text",
                                     "text": f"Error: {e}\n{tb}"}],
                        "isError": True,
                    })
            elif method == "ping":
                resp = _ok(req_id, {})
            elif method == "shutdown":
                resp = _ok(req_id, {})
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
                break
            else:
                if req_id is None:
                    continue
                resp = _err(req_id, -32601, f"method not found: {method}")
        except Exception as e:
            resp = _err(req_id, -32000, str(e))
        if req_id is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False, default=str) + "\n")
            sys.stdout.flush()


def main() -> None:
    db_path = "data/quanti.db"
    serve_stdio(db_path=db_path)


if __name__ == "__main__":
    main()
