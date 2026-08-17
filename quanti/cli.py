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


def _cmd_clear(db, args) -> None:
    """Delete already-synced market data. DRY-RUN by default (prints matched row
    counts); pass --yes to actually delete. Scope with --clear {quotes|
    daily_basic|financials|all}, optional --codes (limit to stocks) and --source
    (limit quotes to one vendor)."""
    target = args.clear
    codes = ([c.strip() for c in args.codes.split(",") if c.strip()]
             if getattr(args, "codes", None) else None)
    src = getattr(args, "source", None)
    yes = getattr(args, "yes", False)

    plan = []  # (table label, delete callable taking dry_run)
    if target in ("quotes", "all"):
        plan.append(("daily_quotes",
                     lambda d: db.delete_quotes(codes, src, dry_run=d)))
    if target in ("daily_basic", "all"):
        plan.append(("daily_basic", lambda d: db.delete_daily_basic(codes, dry_run=d)))
    if target in ("financials", "all"):
        plan.append(("financials", lambda d: db.delete_financials(codes, dry_run=d)))

    scope = []
    if codes:
        scope.append(f"codes={len(codes)}")
    if src:
        scope.append(f"source={src}(仅 quotes)")
    scope_s = ", ".join(scope) if scope else "全部"
    mode = "执行删除" if yes else "预演(dry-run)"
    logger.info(f"--clear {target} [{scope_s}] — {mode}")

    for label, fn in plan:
        n = fn(True)  # always count first
        if yes:
            fn(False)
            logger.info(f"  ✓ {label}: 已删除 {n} 行")
        else:
            logger.info(f"  [dry-run] {label}: 将删除 {n} 行")

    # A wholesale quotes wipe must also reset the by-date backfill checkpoint,
    # else a later --backfill skips those dates and won't re-pull.
    if yes and target in ("quotes", "all") and not codes and not src:
        cleared = db.clear_backfill_progress()
        logger.info(f"  ✓ backfill_progress: 清除 {cleared} 条断点(可重新全量回填)")

    if not yes:
        logger.info("以上为预演;确认无误后加 --yes 实际执行")


def _cmd_strategy_gate(db, args) -> None:
    """策略健康闸门:每个策略跑 2 年长窗回测(默认风险),熔断/深亏策略标记为
    剔除并写入 strategy_gate,走查选股器将不再选用它们。退出码 1 = 存在剔除。"""
    import json as _json

    from quanti.agent.strategy_gate import compute_gate, format_gate
    from quanti.data.provider import DataProvider
    report = compute_gate(db, DataProvider(db), strategies_dir="strategies",
                          lookback_days=args.lookback_days,
                          sharpe_threshold=args.threshold,
                          max_codes=args.max_codes)
    if getattr(args, "json", False):
        print(_json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_gate(report))
    excluded = [n for n, g in report.items()
                if g.get("verdict") in ("breaker", "deep_loss")]
    if excluded:
        logger.error("被闸门剔除: %s(走查选股器将不再选用)", ", ".join(excluded))
        sys.exit(1)


def _cmd_doctor(db, args) -> None:
    """One-shot system health check: exit coverage + data freshness + DB
    integrity. Human-readable by default (--json for machines); exits 1 when
    any check fails so it can gate cron/launchd health checks."""
    import json as _json

    from quanti.health import format_doctor, run_doctor
    codes = ([c.strip() for c in args.codes.split(",") if c.strip()]
             if getattr(args, "codes", None) else None)
    strategies_dir = getattr(args, "strategies_dir", None) or "strategies"
    report = run_doctor(db, strategies_dir=strategies_dir, codes=codes)
    if getattr(args, "json", False):
        print(_json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_doctor(report))
    if not report["ok"]:
        logger.error("体检发现问题(详见上方),建议按 docs/deployment.md 排查")
        sys.exit(1)


def _cmd_factor_watch(db, args) -> None:
    """因子 IC 漂移体检:基线 vs 近期 OOS IC,标记衰减/退役/无快照因子。
    退出码 1 = 存在需关注的因子(可接告警)。"""
    import json as _json

    from quanti.agent.factor_watch import format_watch, watch_factor_drift
    report = watch_factor_drift(db)
    if getattr(args, "json", False):
        print(_json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_watch(report))
    if not report.get("ok", True):
        logger.error("存在需关注的因子(衰减/退役/无快照),详见上方;每日自动重评会持续追踪")
        sys.exit(1)


def _report_periods(years: int) -> list:
    """Quarterly report-period ends (MM-DD ∈ 03-31/06-30/09-30/12-31) within the
    last `years`, up to today — the keys akshare 业绩报表 is fetched by."""
    from datetime import date as _date
    today = _date.today()
    out = []
    for y in range(today.year - years, today.year + 1):
        for mo, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            d = _date(y, mo, day)
            if d <= today:
                out.append(d)
    return out


def cmd_sync(args):
    """Sync market data."""
    from datetime import date

    from quanti.data.source import make_quote_adapter, make_stock_list_adapter

    db = _open_db()
    source = getattr(args, "source", None)  # None → resolve (DB > env > tushare)
    # --refetch: ignore the incremental start and re-pull full history so old
    # qfq rows are overwritten (INSERT OR REPLACE) with raw price + adj_factor.
    refetch_start = date(2010, 1, 1) if getattr(args, "refetch", False) else None

    if getattr(args, "clear", None):
        _cmd_clear(db, args)
        return

    if args.calendar:
        logger.info("Syncing trade calendar...")
        cal_adapter = make_quote_adapter(db, source)
        if not hasattr(cal_adapter, "sync_trade_calendar"):
            # xtdata has no calendar API — fall back to akshare for the calendar.
            from quanti.data.akshare_adapter import AkShareAdapter
            cal_adapter = AkShareAdapter(db)
        count = cal_adapter.sync_trade_calendar()
        logger.info(f"Synced {count} trade dates")

    if args.stocks:
        logger.info("Syncing stock list...")
        # patient=True: CLI can wait out per-minute rate limits. tushare
        # stock_basic can be 1/HOUR on low-points tokens (L/D/P → ~3h, impractical)
        # → on rate-limit, point the user at the free akshare roster instead.
        try:
            count = make_stock_list_adapter(db, source).sync_stock_list(patient=True)
            logger.info(f"Synced {count} stocks")
        except Exception as e:  # noqa: BLE001
            logger.error("名册同步失败: %s", e)
            if "频率超限" in str(e):
                logger.error("tushare 该接口积分受限;改用免费 akshare 含退市股名册:"
                             "  quanti sync --stocks --source akshare")

    if getattr(args, "backfill", False):
        from quanti.data.backfill import run_backfill
        years = getattr(args, "years", 5)
        cpm = getattr(args, "calls_per_min", 400) or 400
        logger.info(f"Backfilling {years}y of history by trading date "
                    f"(source={source or 'default'}, calls/min={cpm})...")

        def _prog(i, total, d, res):
            if i % 50 == 0 or i == total:
                logger.info(f"  {i}/{total} @ {d} — rows={res.rows} "
                            f"skipped={res.dates_skipped} errors={len(res.errors)}")

        res = run_backfill(db, years=years, source=source or "tushare",
                           calls_per_min=cpm, on_progress=_prog)
        logger.info(f"Backfill complete: {res.dates_done} dates, {res.rows} rows, "
                    f"{res.dates_skipped} skipped, {len(res.errors)} errors")

    if getattr(args, "financials", False):
        # Default: tushare fina_indicator (per-code, REAL ann_date — precise PIT;
        # needs 2000-pt tier). `--source akshare` uses the free 业绩报表 (whole-
        # market per report period, also carries net_profit/revenue absolutes,
        # ann_date = statutory deadline).
        if (source or "").lower() == "akshare":
            from quanti.data.akshare_adapter import AkShareAdapter
            years = getattr(args, "years", 5)
            periods = _report_periods(years)
            ak_adapter = AkShareAdapter(db)
            logger.info(f"Syncing financials over {len(periods)} report periods "
                        f"(akshare 业绩报表, PIT by 公告日)...")
            total = 0
            for p in periods:
                total += ak_adapter.sync_financials_by_period(p)
            logger.info(f"Financials sync complete: {total} rows (akshare)")
        else:
            import time as _time
            adapter = make_quote_adapter(db, source or "tushare")
            if not hasattr(adapter, "sync_financials_for_code"):
                logger.error("source 无 financials 支持;用 --source akshare(免费)")
            else:
                codes = [s.code for s in db.list_stocks()]
                logger.info(f"Syncing financials for {len(codes)} stocks "
                            f"(tushare fina_indicator, PIT by 真实 ann_date)...")
                total = 0
                for i, code in enumerate(codes):
                    try:
                        total += adapter.sync_financials_for_code(code, patient=True)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"  {code}: {e}")
                    if (i + 1) % 100 == 0:
                        logger.info(f"  {i + 1}/{len(codes)} (rows={total})")
                    _time.sleep(0.2)  # gentle pacing under the per-min cap
                logger.info(f"Financials sync complete: {total} rows (tushare)")

    if args.quotes:
        adapter = make_quote_adapter(db, source)
        codes = args.codes.split(",") if args.codes else db.list_stocks()
        codes = [c.code if hasattr(c, "code") else c for c in codes]
        logger.info(f"Syncing daily quotes for {len(codes)} stocks"
                    f"{' (full refetch)' if refetch_start else ''}...")
        for i, code in enumerate(codes):
            count = adapter.sync_daily_quotes(code, start=refetch_start)
            if (i + 1) % 50 == 0:
                logger.info(f"  Progress: {i + 1}/{len(codes)}")
        logger.info("Quote sync complete")

    if getattr(args, "tushare_stocks", False):
        from quanti.data.tushare_adapter import TushareAdapter
        logger.info("Syncing Tushare stock list (incl. delisted)...")
        n = TushareAdapter(db).sync_stock_list()
        logger.info(f"Synced {n} stocks from Tushare")

    if getattr(args, "tushare_quotes", False):
        from quanti.data.tushare_adapter import TushareAdapter
        ta = TushareAdapter(db)
        stocks = db.list_stocks()
        if getattr(args, "delisted_only", False):
            stocks = [s for s in stocks if s.delist_date is not None]
        codes = [s.code for s in stocks]
        logger.info(f"Syncing Tushare quotes for {len(codes)} stocks...")
        for i, code in enumerate(codes):
            try:
                ta.sync_daily_quotes(code, start=refetch_start)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"  {code}: {e}")
            if (i + 1) % 50 == 0:
                logger.info(f"  Progress: {i + 1}/{len(codes)}")
        logger.info("Tushare quote sync complete")

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
    if getattr(args, "survivorship_free", False):
        start_d = date.fromisoformat(args.start)
        end_d = date.fromisoformat(args.end)
        all_codes = db.point_in_time_universe(start_d, end_d)
        max_u = getattr(args, "max_universe", 300)
        codes = all_codes[:max_u]
        logger.info(
            f"Survivorship-free universe: {len(all_codes)} stocks in window, "
            f"using {len(codes)} (cap {max_u})")
        if len(all_codes) > len(codes):
            logger.info(f"  dropped {len(all_codes) - len(codes)} over cap")
    else:
        if not args.codes:
            logger.error("--codes is required unless --survivorship-free is set")
            sys.exit(1)
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


def cmd_optimize(args):
    """Walk-forward hyperopt over all candidate strategies; persist tuned params."""
    from datetime import date

    from quanti.agent.goal import load_goal
    from quanti.agent.hyperopt import HyperOptimizer
    from quanti.agent.universe import resolve_tradable_universe
    from quanti.backtest.engine import BacktestEngine
    from quanti.data.provider import DataProvider
    from quanti.risk.manager import RiskConfig, RiskManager
    from quanti.strategy.loader import StrategyLoader

    db = _open_db()
    provider = DataProvider(db)
    goal = load_goal(db)
    end = date.fromisoformat(args.end) if args.end else date.today()
    pool = args.universe or goal.universe_pool
    # Same tradable-universe selection the live agent uses (ADV-ranked, with
    # optional liquidity filter) as of `end` — not a dictionary-order slice.
    codes = resolve_tradable_universe(db, provider, pool=pool,
                                      params=goal.params, as_of=end)

    classes = [type(s) for s in StrategyLoader().load_directory("strategies")]
    engine = BacktestEngine(provider=provider, initial_cash=args.cash,
                            risk_manager=RiskManager(RiskConfig()))
    results = HyperOptimizer(engine).optimize_all(classes, codes, end)

    for r in results:
        db.save_optimization(r.strategy_name, r.chosen_params, r.tuned_oos_sharpe,
                             r.default_oos_sharpe, r.accepted, r.n_combos_tried,
                             len(codes))
        flag = "✓ 采纳" if r.accepted else "· 默认"
        logger.info("%s %-16s tuned=%.2f default=%.2f combos=%d/%d — %s",
                    flag, r.strategy_name, r.tuned_oos_sharpe,
                    r.default_oos_sharpe, r.n_combos_tried, r.n_combos_total,
                    r.reason)
    db.close()


def _preflight_port(host: str, port: int) -> None:
    """uvicorn 是先跑 lifespan(启动后台同步、按 goal 自动拉起 agent、写
    agent_start 决策)之后才 bind 端口——端口被占时这些副作用已经发生。
    launchd KeepAlive 每 ~10s 拉一次,曾在 3 天里往 paper.db 刷了 4000+ 条
    agent_start(2026-08-17 实发)。在构建 app 之前先探一次端口,占用就
    fail-fast,让 crash-loop 至少是零副作用的。非原子(探完到真 bind 有
    窗口),目的只是挡长期循环污染,不是抢锁。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
    except OSError as e:
        raise SystemExit(
            f"端口 {host}:{port} 已被占用({e})——已有另一个 quanti serve 在跑?"
            f"用 `lsof -nP -iTCP:{port} -sTCP:LISTEN` 查看占用进程;"
            "若是 launchd 服务与手动实例冲突,二选一。") from e
    finally:
        s.close()


def cmd_serve(args):
    """Start the web server only."""
    import uvicorn

    from quanti.api.app import create_app

    _preflight_port(args.host, args.port)
    app = create_app(initial_cash=args.cash, autostart_agent=False)
    uvicorn.run(app, host=args.host, port=args.port)


def cmd_up(args):
    """One-command boot: ensure DB+stocks, set goal if needed, run server + agent."""
    import uvicorn

    from quanti.agent.goal import RiskTolerance, load_goal, save_goal
    from quanti.api.app import create_app
    from quanti.data.source import make_stock_list_adapter

    db = _open_db()

    # First-run: populate stock list so the universe isn't empty. Run in a
    # background thread so the API is reachable immediately while the list
    # downloads (>5k stocks → can take a couple minutes the first time).
    if not db.list_stocks():
        import threading

        def _bootstrap_stocks() -> None:
            try:
                local_db = _open_db()  # own connection for the thread
                adapter = make_stock_list_adapter(local_db)
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

    _preflight_port(args.host, args.port)
    app = create_app(initial_cash=args.cash,
                     autostart_agent=goal.enabled)
    uvicorn.run(app, host=args.host, port=args.port)


def cmd_agent(args):
    """Inspect or trigger the agent without a server."""
    from quanti.agent.goal import RiskTolerance, load_goal, save_goal
    from quanti.agent.runtime import AgentRuntime
    from quanti.data.provider import DataProvider
    from quanti.execution.factory import make_broker

    db = _open_db()
    provider = DataProvider(db)
    # account from env QUANTI_ACCOUNT: live → QmtBroker(require_live), else paper.
    broker = make_broker(db, provider, initial_cash=args.cash, fill_mode="immediate")
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


def cmd_mine_factors(args):
    """LLM factor mining: propose → IC gate → persist to generated_factors."""
    from datetime import date

    from quanti.agent.factor_miner import mine_factors
    from quanti.agent.goal import load_goal
    from quanti.agent.llm_runtime import build_llm_client
    from quanti.agent.universe import resolve_tradable_universe
    from quanti.data.provider import DataProvider

    db = _open_db()
    provider = DataProvider(db)
    goal = load_goal(db)
    params = goal.params or {}
    end = date.fromisoformat(args.end) if args.end else date.today()
    pool = args.universe or goal.universe_pool
    # Same tradable-universe selection the live agent uses, as of `end`.
    codes = resolve_tradable_universe(db, provider, pool=pool,
                                      params=goal.params, as_of=end)
    try:
        llm = build_llm_client(params)
    except Exception as e:  # noqa: BLE001
        logger.error("LLM unavailable for mining: %s", e)
        db.close()
        return
    results = mine_factors(llm, db, provider, codes, end, n_candidates=args.n)
    for r in results:
        flag = "adopted" if r.accepted else "dropped"
        logger.info(
            "%s %-16s train_ic=%.3f oos_ic=%.3f — %s",
            flag, r.name, r.train_ic, r.oos_ic, r.reason,
        )
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
    sync_parser.add_argument("--source", choices=["tushare", "akshare", "xtdata"],
                             default=None,
                             help="历史源(默认按配置: DB app_config > env "
                                  "QUANTI_DATA_SOURCE > tushare)。tushare 无 token "
                                  "时直接报错,不再静默回退;要用 akshare 请显式指定")
    sync_parser.add_argument("--tushare-stocks", action="store_true",
                             dest="tushare_stocks",
                             help="Sync full roster incl. delisted via Tushare")
    sync_parser.add_argument("--tushare-quotes", action="store_true",
                             dest="tushare_quotes",
                             help="Sync daily history via Tushare")
    sync_parser.add_argument("--backfill", action="store_true",
                             help="逐交易日全市场批量回填(含退市股,高效/可断点续),"
                                  "配合 --years;需 Tushare token")
    sync_parser.add_argument("--years", type=int, default=5,
                             help="--backfill 回填年数(默认 5)")
    sync_parser.add_argument("--calls-per-min", dest="calls_per_min", type=int,
                             default=400,
                             help="--backfill 每分钟调用上限(按 token 的 daily 限额设;"
                                  "低档约 50,设 ~80;高档可 400+)")
    sync_parser.add_argument("--financials", action="store_true",
                             help="拉财务指标(ROE/净利营收及同比,按公告日 PIT)"
                                  "—— akshare 业绩报表,免费、无需 token,按 --years "
                                  "覆盖报告期")
    sync_parser.add_argument("--refetch", action="store_true",
                             help="全量重拉历史(覆盖旧数据)。从 qfq 切到"
                                  "「原始价+adj_factor」后须跑一次,否则旧 qfq "
                                  "行与新原始行混用,口径不一致")
    sync_parser.add_argument("--delisted-only", action="store_true",
                             dest="delisted_only",
                             help="With --tushare-quotes: only delisted stocks")
    sync_parser.add_argument("--clear",
                             choices=["quotes", "daily_basic", "financials", "all"],
                             default=None,
                             help="删除已同步数据(默认预演,加 --yes 实际执行);"
                                  "可配 --codes 限定股票、--source 限定行情源")
    sync_parser.add_argument("--yes", action="store_true",
                             help="确认执行删除(配合 --clear;不加则只预演)")

    # backtest
    bt_parser = subparsers.add_parser("backtest", help="Run backtest")
    bt_parser.add_argument("--strategy", required=True)
    bt_parser.add_argument("--codes", required=False, default=None)
    bt_parser.add_argument("--start", required=True)
    bt_parser.add_argument("--end", required=True)
    bt_parser.add_argument("--cash", type=float, default=1_000_000)
    bt_parser.add_argument("--survivorship-free", action="store_true",
                           dest="survivorship_free",
                           help="Backtest over the point-in-time universe "
                                "(incl. delisted) instead of --codes")
    bt_parser.add_argument("--max-universe", type=int, default=300,
                           dest="max_universe",
                           help="Cap on survivorship-free universe size")

    # optimize
    opt_parser = subparsers.add_parser("optimize", help="走查式参数寻优(hyperopt)")
    opt_parser.add_argument("--universe", type=str, default=None,
                            help="股票池名；留空用 goal.universe_pool 或全市场")
    opt_parser.add_argument("--end", type=str, default=None,
                            help="优化截止日 YYYY-MM-DD；默认今天")
    opt_parser.add_argument("--cash", type=float, default=1_000_000)

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

    # mine-factors
    mine_parser = subparsers.add_parser("mine-factors", help="LLM 因子挖掘")
    mine_parser.add_argument("--universe", type=str, default=None)
    mine_parser.add_argument("--n", type=int, default=10)
    mine_parser.add_argument("--end", type=str, default=None)
    mine_parser.add_argument("--cash", type=float, default=1_000_000)

    # factor-watch
    fw_parser = subparsers.add_parser("factor-watch",
                                      help="因子 IC 漂移体检：衰减/退役/无快照")
    fw_parser.add_argument("--json", action="store_true",
                           help="输出机器可读 JSON")

    # strategy-gate
    sg_parser = subparsers.add_parser("strategy-gate",
                                      help="策略健康闸门：长窗回测剔除熔断/深亏策略")
    sg_parser.add_argument("--lookback-days", dest="lookback_days", type=int,
                           default=730)
    sg_parser.add_argument("--threshold", type=float, default=-0.5,
                           help="年化夏普剔除阈值")
    sg_parser.add_argument("--max-codes", dest="max_codes", type=int,
                           default=100)
    sg_parser.add_argument("--json", action="store_true")

    # doctor
    doc_parser = subparsers.add_parser("doctor", help="系统体检：退出覆盖/数据新鲜度/DB 完整性")
    doc_parser.add_argument("--codes", type=str, default=None,
                            help="只体检这些代码的数据新鲜度(逗号分隔,默认全部)")
    doc_parser.add_argument("--strategies-dir", dest="strategies_dir",
                            type=str, default=None)
    doc_parser.add_argument("--json", action="store_true",
                            help="输出机器可读 JSON")

    # mcp
    subparsers.add_parser("mcp", help="启动 MCP server（stdio）供 OpenClaw 接入")

    args = parser.parse_args()
    cmd = args.command
    from quanti.data.source import DataSourceUnavailable
    try:
        if cmd == "sync":
            cmd_sync(args)
        elif cmd == "backtest":
            cmd_backtest(args)
        elif cmd == "optimize":
            cmd_optimize(args)
        elif cmd == "serve":
            cmd_serve(args)
        elif cmd == "up":
            cmd_up(args)
        elif cmd == "agent":
            cmd_agent(args)
        elif cmd == "mcp":
            cmd_mcp(args)
        elif cmd == "mine-factors":
            cmd_mine_factors(args)
        elif cmd == "doctor":
            _cmd_doctor(_open_db(), args)
        elif cmd == "factor-watch":
            _cmd_factor_watch(_open_db(), args)
        elif cmd == "strategy-gate":
            _cmd_strategy_gate(_open_db(), args)
        else:
            parser.print_help()
    except DataSourceUnavailable as e:
        # Clean, actionable message instead of a traceback when the configured
        # source can't be built (e.g. tushare with no token). No silent fallback.
        logger.error(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
