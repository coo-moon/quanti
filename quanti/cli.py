"""Command-line interface for Quanti."""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def cmd_sync(args):
    """Sync market data."""
    from quanti.data.akshare_adapter import AkShareAdapter
    from quanti.data.database import Database

    db = Database()
    db.initialize()
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
    from quanti.data.database import Database
    from quanti.data.provider import DataProvider
    from quanti.strategy.loader import StrategyLoader

    db = Database()
    db.initialize()
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

    engine = BacktestEngine(provider=provider, initial_cash=args.cash)
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
    """Start the web server."""
    import uvicorn

    from quanti.api.app import create_app

    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port)


def main():
    parser = argparse.ArgumentParser(description="Quanti - A-share quantitative trading")
    subparsers = parser.add_subparsers(dest="command")

    # sync command
    sync_parser = subparsers.add_parser("sync", help="Sync market data")
    sync_parser.add_argument("--stocks", action="store_true", help="Sync stock list")
    sync_parser.add_argument("--quotes", action="store_true", help="Sync daily quotes")
    sync_parser.add_argument("--calendar", action="store_true", help="Sync trade calendar")
    sync_parser.add_argument("--codes", type=str, help="Comma-separated stock codes")

    # backtest command
    bt_parser = subparsers.add_parser("backtest", help="Run backtest")
    bt_parser.add_argument("--strategy", required=True, help="Strategy name")
    bt_parser.add_argument("--codes", required=True, help="Comma-separated stock codes")
    bt_parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    bt_parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    bt_parser.add_argument("--cash", type=float, default=1_000_000, help="Initial cash")

    # serve command
    serve_parser = subparsers.add_parser("serve", help="Start web server")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)

    args = parser.parse_args()
    if args.command == "sync":
        cmd_sync(args)
    elif args.command == "backtest":
        cmd_backtest(args)
    elif args.command == "serve":
        cmd_serve(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
