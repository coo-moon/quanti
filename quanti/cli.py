"""Command-line interface for Quanti."""

from __future__ import annotations

import argparse
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _open_db():
    """Open the account DB (trading state) with the shared market DB attached.
    Account = QUANTI_ACCOUNT env (default 'paper') so the CLI shares market
    data with the server and keeps each account's trades in its own file."""
    from quanti.data.database import Database
    account = os.environ.get("QUANTI_ACCOUNT", "paper")
    db = Database(f"data/{account}.db", market_db_path="data/market.db")
    db.initialize()
    return db


def cmd_sync(args):
    """Sync market data."""
    from quanti.data.akshare_adapter import AkShareAdapter

    db = _open_db()
    adapter = AkShareAdapter(db)

    if args.calendar:
        logger.info("Syncing trade calendar...")
        count = adapter.sync_trade_calendar()
        logger.info(f"Synced {count} trade dates")

    if args.stocks:
        logger.info("Syncing stock list...")
        count = adapter.sync_stock_list()
        logger.info(f"Synced {count} stocks")

    if args.quotes:
        codes = args.codes.split(",") if args.codes else db.list_stocks()
        codes = [c.code if hasattr(c, "code") else c for c in codes]
        logger.info(f"Syncing daily quotes for {len(codes)} stocks...")
        for i, code in enumerate(codes):
            count = adapter.sync_daily_quotes(code)
            if (i + 1) % 50 == 0:
                logger.info(f"  Progress: {i + 1}/{len(codes)}")
        logger.info("Quote sync complete")

    db.close()


def cmd_backtest(args):
    """Run a backtest."""
    from datetime import date

    from quanti.backtest.engine import BacktestEngine
    from quanti.data.provider import DataProvider
    from quanti.strategy.loader import StrategyLoader

    db = _open_db()
    provider = DataProvider(db)

    loader = StrategyLoader()
    strategies = loader.load_directory("strategies")
    strategy = None
    for s in strategies:
        if s.name == args.strategy:
            strategy = s
            break

    if strategy is None:
        logger.error(f"Strategy '{args.strategy}' not found")
        sys.exit(1)

    strategy.init({})
    codes = args.codes.split(",")

    from quanti.risk.manager import RiskConfig, RiskManager
    from quanti.risk.protections import ProtectionManager
    engine = BacktestEngine(provider=provider, initial_cash=args.cash,
                            risk_manager=RiskManager(RiskConfig()),
                            protection_manager=ProtectionManager())
    result = engine.run(
        strategy=strategy,
        codes=codes,
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
    )

    logger.info("=== Backtest Results ===")
    for key, value in result.metrics.items():
        if isinstance(value, float):
            logger.info(f"  {key}: {value:.4f}")
        else:
            logger.info(f"  {key}: {value}")
    logger.info(f"  Total trades: {len(result.trades)}")
    db.close()


def cmd_serve(args):
    """Start the web server only."""
    import uvicorn

    from quanti.api.app import create_app

    app = create_app(initial_cash=args.cash, autostart_agent=False)
    uvicorn.run(app, host=args.host, port=args.port)


def cmd_up(args):
    """One-command boot: ensure DB+stocks, set goal if needed, run server + agent."""
    import uvicorn

    from quanti.agent.goal import RiskTolerance, load_goal, save_goal
    from quanti.api.app import create_app
    from quanti.data.akshare_adapter import AkShareAdapter

    db = _open_db()

    # First-run: populate stock list so the universe isn't empty. Run in a
    # background thread so the API is reachable immediately while the list
    # downloads (>5k stocks → can take a couple minutes the first time).
    if not db.list_stocks():
        import threading

        def _bootstrap_stocks() -> None:
            try:
                local_db = _open_db()  # own connection for the thread
                adapter = AkShareAdapter(local_db)
                count = adapter.sync_stock_list()
                logger.info(f"后台股票列表同步完成：{count} 只")
                local_db.close()
            except Exception as e:
                logger.warning(f"股票列表同步失败 (可稍后从 Web 重试): {e}")

        threading.Thread(target=_bootstrap_stocks, daemon=True,
                         name="bootstrap-stocks").start()
        logger.info("空数据库：后台拉取 A 股股票列表...")

    # Bootstrap goal if missing.
    goal = load_goal(db)
    if args.target is not None:
        goal.target_annual_return = float(args.target)
    if args.max_drawdown is not None:
        goal.max_drawdown = float(args.max_drawdown)
    if args.risk is not None:
        goal.risk_tolerance = RiskTolerance(args.risk)
    if args.pool is not None:
        goal.universe_pool = args.pool
    if args.screener is not None:
        goal.screener_name = args.screener
    if args.strategy is not None:
        goal.strategy_name = args.strategy
    goal.enabled = not args.no_agent
    save_goal(db, goal)
    db.close()

    logger.info(f"目标：年化 {goal.target_annual_return:+.1%}, 最大回撤 {goal.max_drawdown:.1%}, "
                f"风险 {goal.risk_tolerance.value if hasattr(goal.risk_tolerance, 'value') else goal.risk_tolerance}, "
                f"agent={'on' if goal.enabled else 'off'}")
    logger.info(f"启动服务 → http://{args.host}:{args.port}")

    app = create_app(initial_cash=args.cash,
                     autostart_agent=goal.enabled)
    uvicorn.run(app, host=args.host, port=args.port)


def cmd_agent(args):
    """Inspect or trigger the agent without a server."""
    from quanti.agent.goal import RiskTolerance, load_goal, save_goal
    from quanti.agent.runtime import AgentRuntime
    from quanti.data.provider import DataProvider
    from quanti.execution.paper_broker import PaperBroker

    db = _open_db()
    provider = DataProvider(db)
    broker = PaperBroker(db, provider, initial_cash=args.cash)
    agent = AgentRuntime(db, provider, broker)

    if args.action == "tick":
        result = agent.tick()
        print(__import__("json").dumps(result, indent=2,
                                       ensure_ascii=False, default=str))
    elif args.action == "status":
        s = agent.status()
        print(__import__("json").dumps({
            "enabled": s.enabled, "running": s.running,
            "last_tick_at": s.last_tick_at,
            "last_tick_summary": s.last_tick_summary,
            "last_strategy": s.last_strategy,
            "total_value": s.total_value, "pnl_pct": s.pnl_pct,
        }, indent=2, ensure_ascii=False))
    elif args.action == "goal":
        print(__import__("json").dumps(load_goal(db).to_db(),
                                       indent=2, ensure_ascii=False))
    elif args.action == "set-goal":
        goal = load_goal(db)
        if args.target is not None:
            goal.target_annual_return = float(args.target)
        if args.max_drawdown is not None:
            goal.max_drawdown = float(args.max_drawdown)
        if args.risk is not None:
            goal.risk_tolerance = RiskTolerance(args.risk)
        save_goal(db, goal)
        print("更新完成:", goal.to_db())
    elif args.action == "decisions":
        for d in db.list_decisions(limit=args.limit):
            print(f"[{d['ts']}] {d['kind']:14s} {d.get('code','') or '-':>8s} | {d['summary']}")
    elif args.action == "prune":
        removed = db.prune_decisions(args.older_than_days)
        print(f"Removed {removed} decision rows older than {args.older_than_days} days")
    db.close()


def cmd_mcp(_args):
    """Launch the MCP stdio server."""
    from quanti.mcp_server import main as run_mcp
    run_mcp()


def main():
    parser = argparse.ArgumentParser(description="Quanti - A-share AI quant trading")
    subparsers = parser.add_subparsers(dest="command")

    # sync
    sync_parser = subparsers.add_parser("sync", help="Sync market data")
    sync_parser.add_argument("--stocks", action="store_true")
    sync_parser.add_argument("--quotes", action="store_true")
    sync_parser.add_argument("--calendar", action="store_true")
    sync_parser.add_argument("--codes", type=str)

    # backtest
    bt_parser = subparsers.add_parser("backtest", help="Run backtest")
    bt_parser.add_argument("--strategy", required=True)
    bt_parser.add_argument("--codes", required=True)
    bt_parser.add_argument("--start", required=True)
    bt_parser.add_argument("--end", required=True)
    bt_parser.add_argument("--cash", type=float, default=1_000_000)

    # serve
    serve_parser = subparsers.add_parser("serve", help="Start web server (no agent)")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument("--cash", type=float, default=1_000_000)

    # up
    up_parser = subparsers.add_parser("up", help="一键启动：数据 + 目标 + Web + Agent")
    up_parser.add_argument("--host", default="127.0.0.1")
    up_parser.add_argument("--port", type=int, default=8000)
    up_parser.add_argument("--cash", type=float, default=1_000_000,
                           help="初始资金（仅首次创建组合时生效）")
    up_parser.add_argument("--target", type=float, default=None,
                           help="目标年化收益（如 0.20 表示 20%%）")
    up_parser.add_argument("--max-drawdown", dest="max_drawdown", type=float,
                           default=None, help="可接受的最大回撤，负数（如 -0.20）")
    up_parser.add_argument("--risk", choices=["low", "medium", "high"], default=None)
    up_parser.add_argument("--pool", type=str, default=None)
    up_parser.add_argument("--screener", type=str, default=None)
    up_parser.add_argument("--strategy", type=str, default=None,
                           help="指定策略名，留空则由 Agent 自动挑选")
    up_parser.add_argument("--no-agent", action="store_true",
                           help="只起 Web，不自动启动 Agent")

    # agent
    ag_parser = subparsers.add_parser("agent", help="无 server 模式下操作 Agent")
    ag_parser.add_argument("action",
                           choices=["tick", "status", "goal", "set-goal",
                                    "decisions", "prune"])
    ag_parser.add_argument("--cash", type=float, default=1_000_000)
    ag_parser.add_argument("--target", type=float, default=None)
    ag_parser.add_argument("--max-drawdown", dest="max_drawdown", type=float, default=None)
    ag_parser.add_argument("--risk", choices=["low", "medium", "high"], default=None)
    ag_parser.add_argument("--limit", type=int, default=20)
    ag_parser.add_argument("--older-than-days", dest="older_than_days",
                           type=int, default=90,
                           help="prune: 保留多少天内的决策日志")

    # mcp
    subparsers.add_parser("mcp", help="启动 MCP server（stdio）供 OpenClaw 接入")

    args = parser.parse_args()
    cmd = args.command
    if cmd == "sync":
        cmd_sync(args)
    elif cmd == "backtest":
        cmd_backtest(args)
    elif cmd == "serve":
        cmd_serve(args)
    elif cmd == "up":
        cmd_up(args)
    elif cmd == "agent":
        cmd_agent(args)
    elif cmd == "mcp":
        cmd_mcp(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
