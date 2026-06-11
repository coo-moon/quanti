"""API route definitions."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from quanti.agent.goal import Goal, RiskTolerance, load_goal, save_goal
from quanti.backtest.engine import BacktestEngine
from quanti.models import Direction, Signal
from quanti.screener.loader import ScreenerLoader
from quanti.strategy.loader import StrategyLoader

router = APIRouter()


# --- Request/Response models ---


class BacktestRequest(BaseModel):
    strategy_name: str
    codes: list[str]
    start: str  # YYYY-MM-DD
    end: str
    initial_cash: float = 1_000_000.0
    params: dict = {}


class TradeResponse(BaseModel):
    date: str
    stock_code: str
    direction: str
    quantity: int
    price: float
    commission: float


class BacktestResponse(BaseModel):
    metrics: dict
    trades: list[TradeResponse]
    equity_curve: dict[str, float]
    warning: str = ""


class SyncRequest(BaseModel):
    codes: list[str]


class SyncResult(BaseModel):
    synced: dict[str, int]  # code -> bar count
    errors: dict[str, str] = {}  # code -> error message


class StockPoolStats(BaseModel):
    total: int
    with_quotes: int  # stocks that have quote data
    exchange_sh: int
    exchange_sz: int


class ScreenRequest(BaseModel):
    screener_name: str
    codes: list[str] = []  # empty = all stocks in DB
    end: str = ""  # YYYY-MM-DD, default today
    lookback_days: int = 120
    top_n: int = 20
    params: dict = {}


class ScreenResultItem(BaseModel):
    code: str
    name: str
    score: float
    close: float
    change_pct: float  # latest day change %


class ScreenResponse(BaseModel):
    screener: str
    description: str
    results: list[ScreenResultItem]
    total_scanned: int


# --- Endpoints ---


@router.post("/sync/quotes")
async def sync_quotes(body: SyncRequest, request: Request):
    """Sync daily quotes for given stock codes from AkShare."""
    from quanti.data.akshare_adapter import AkShareAdapter

    db = request.app.state.db
    adapter = AkShareAdapter(db)
    results = {}
    errors = {}
    for code in body.codes:
        try:
            count = adapter.sync_daily_quotes(code)
            results[code] = count
            if count == 0:
                errors[code] = "未获取到数据，可能是网络问题或股票代码无效"
        except Exception as e:
            results[code] = 0
            errors[code] = str(e)
    return SyncResult(synced=results, errors=errors)


@router.post("/sync/quotes/async")
async def sync_quotes_async(body: SyncRequest, request: Request):
    """Start async sync for given stock codes. Returns job_id immediately."""
    from datetime import date, timedelta
    from quanti.data.akshare_adapter import AkShareAdapter

    db = request.app.state.db
    codes = body.codes
    if not codes:
        all_stocks = db.list_stocks()
        codes = [s.code for s in all_stocks]
    if not codes:
        return {"error": "没有可同步的股票"}

    job_id = f"q_{str(uuid.uuid4())[:8]}"
    db.create_sync_job(job_id, "_quotes_sync", len(codes))
    asyncio.create_task(_run_quotes_sync(job_id, codes, db))

    return {"job_id": job_id}


async def _run_quotes_sync(job_id: str, codes: list[str], db) -> None:
    from datetime import date, timedelta
    from quanti.data.akshare_adapter import AkShareAdapter
    import asyncio
    from functools import partial

    end_d = date.today()
    start_d = end_d - timedelta(days=365)
    adapter = AkShareAdapter(db)
    errors: dict[str, str] = {}
    loop = asyncio.get_event_loop()

    for i, code in enumerate(codes):
        try:
            fn = partial(adapter.sync_daily_quotes, code, start=start_d, end=end_d, repair_gaps=False)
            count = await loop.run_in_executor(None, fn)
            if count == 0:
                errors[code] = "未获取到数据"
        except Exception as e:
            errors[code] = str(e)
        db.update_sync_job(job_id, i + 1, "running", errors)

    final_status = "error" if errors else "done"
    db.update_sync_job(job_id, len(codes), final_status, errors)


def _calc_eta_seconds(job: dict) -> int | None:
    """Calculate estimated remaining seconds based on progress rate."""
    if job["status"] != "running" or job["current"] == 0:
        return None
    from datetime import datetime
    created = datetime.fromisoformat(job["created_at"])
    elapsed = (datetime.now() - created).total_seconds()
    if elapsed <= 0:
        return None
    rate = job["current"] / elapsed
    if rate <= 0:
        return None
    remaining = job["total"] - job["current"]
    return int(remaining / rate)


@router.get("/sync/quotes/status")
async def get_quotes_sync_status(job_id: str, request: Request):
    """Get async quotes sync progress."""
    db = request.app.state.db
    job = db.get_sync_job(job_id)
    if job is None:
        return {"error": f"Job '{job_id}' not found"}
    current = job["current"]
    total = job["total"]
    status = job["status"]
    err_count = len(job["errors"])
    eta = _calc_eta_seconds(job)
    if status == "running":
        message = f"已同步 {current}/{total}"
        if eta is not None:
            message += f"，约 {eta // 60} 分钟"
    elif status == "done":
        message = f"同步完成，共 {total} 只"
    else:
        message = f"同步结束，{err_count} 只失败"
    return SyncStatusResponse(
        job_id=job_id, current=current, total=total,
        status=status, errors=job["errors"], message=message,
        eta_seconds=eta,
    )


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/sync/background/status")
async def background_sync_status(request: Request):
    """Read the live status of the BackgroundQuoteSyncer daemon.

    Returns a JSON snapshot of where the syncer is (active / idle / paused),
    how much of the universe it's already pulled this session, what code
    it's working on right now, and the most recent error if any. The
    frontend Dashboard polls this every ~10s.
    """
    bg = getattr(request.app.state, "bg_sync", None)
    if bg is None:
        return {"enabled": False, "running": False, "state": "disabled"}
    s = bg.status()
    return {
        "enabled": s.enabled,
        "running": s.running,
        "state": s.state,
        "started_at": s.started_at,
        "last_loop_at": s.last_loop_at,
        "current_code": s.current_code,
        "queue_remaining": s.queue_remaining,
        "synced_session": s.synced_session,
        "failed_session": s.failed_session,
        "backoff_codes": s.backoff_codes,
        "last_full_scan_at": s.last_full_scan_at,
        "last_error": s.last_error,
        "config": s.config,
    }


@router.post("/sync/background/pause")
async def background_sync_pause(request: Request):
    """Pause the background syncer (e.g. to free up bandwidth during
    a one-off bulk sync). Survives until /sync/background/resume."""
    bg = getattr(request.app.state, "bg_sync", None)
    if bg is None:
        return {"ok": False, "reason": "background syncer not initialized"}
    bg.pause()
    return {"ok": True, "state": bg.status().state}


@router.post("/sync/background/resume")
async def background_sync_resume(request: Request):
    bg = getattr(request.app.state, "bg_sync", None)
    if bg is None:
        return {"ok": False, "reason": "background syncer not initialized"}
    bg.resume()
    return {"ok": True, "state": bg.status().state}


@router.post("/sync/stocks")
async def sync_stock_list(request: Request):
    """Sync the full A-share stock list (name, industry, exchange, list_date)."""
    from quanti.data.akshare_adapter import AkShareAdapter

    db = request.app.state.db
    adapter = AkShareAdapter(db)
    count = adapter.sync_stock_list()
    return {"synced": count, "message": f"成功同步 {count} 只股票到股票池"}


@router.get("/stocks/stats")
async def stock_pool_stats(request: Request):
    """Get stock pool statistics."""
    db = request.app.state.db
    all_stocks = db.list_stocks()
    with_quotes = request.app.state.provider.get_all_codes()
    return StockPoolStats(
        total=len(all_stocks),
        with_quotes=len(with_quotes),
        exchange_sh=sum(1 for s in all_stocks if s.exchange.upper() in ("SH", "SHANGHAI")),
        exchange_sz=sum(1 for s in all_stocks if s.exchange.upper() in ("SZ", "SHENZHEN")),
    )


# --- Stock Pool Management ---

class PoolCreateRequest(BaseModel):
    name: str
    description: str = ""


class PoolAddStocksRequest(BaseModel):
    codes: list[str]


class SyncStatusResponse(BaseModel):
    job_id: str
    current: int
    total: int
    status: str  # running, done, error
    errors: dict
    message: str
    eta_seconds: int | None = None


@router.get("/pools")
async def list_pools(request: Request):
    """List all stock pools."""
    pools = request.app.state.db.list_pools()
    return pools


@router.post("/pools")
async def create_pool(body: PoolCreateRequest, request: Request):
    """Create a new stock pool."""
    db = request.app.state.db
    if db.pool_exists(body.name):
        return {"error": f"股票池 '{body.name}' 已存在"}
    db.create_pool(body.name, body.description)
    return {"name": body.name, "message": f"股票池 '{body.name}' 创建成功"}


@router.delete("/pools/{name}")
async def delete_pool(name: str, request: Request):
    """Delete a stock pool."""
    db = request.app.state.db
    if not db.pool_exists(name):
        return {"error": f"股票池 '{name}' 不存在"}
    db.delete_pool(name)
    return {"name": name, "message": f"股票池 '{name}' 已删除"}


@router.get("/pools/{name}/stocks")
async def get_pool_stocks(name: str, request: Request):
    """Get all stocks in a pool."""
    db = request.app.state.db
    if not db.pool_exists(name):
        return {"error": f"股票池 '{name}' 不存在"}
    stocks = db.get_pool_stocks(name)
    return [
        {
            "code": s.code,
            "name": s.name,
            "exchange": s.exchange,
            "list_date": s.list_date.isoformat(),
            "industry": s.industry,
            "latest_date": (d.isoformat() if (d := db.get_latest_quote_date(s.code)) else None),
        }
        for s in stocks
    ]


@router.post("/pools/{name}/stocks")
async def add_pool_stocks(name: str, body: PoolAddStocksRequest, request: Request):
    """Add stocks to a pool."""
    db = request.app.state.db
    if not db.pool_exists(name):
        return {"error": f"股票池 '{name}' 不存在"}
    count = db.add_stocks_to_pool(name, body.codes)
    return {"added": count, "message": f"成功添加 {count} 只股票到 '{name}'"}


@router.delete("/pools/{name}/stocks")
async def remove_pool_stocks(name: str, body: PoolAddStocksRequest, request: Request):
    """Remove stocks from a pool."""
    db = request.app.state.db
    if not db.pool_exists(name):
        return {"error": f"股票池 '{name}' 不存在"}
    count = db.remove_stocks_from_pool(name, body.codes)
    return {"removed": count, "message": f"已从 '{name}' 移除 {count} 只股票"}


@router.post("/pools/{name}/sync")
async def sync_pool_stocks(name: str, request: Request):
    """Start async sync for pool stocks. Returns job_id immediately."""
    from datetime import timedelta
    from quanti.data.akshare_adapter import AkShareAdapter

    db = request.app.state.db
    if not db.pool_exists(name):
        return {"error": f"股票池 '{name}' 不存在"}

    codes = db.get_pool_codes(name)
    if not codes:
        return {"error": "股票池为空"}

    job_id = str(uuid.uuid4())[:8]
    db.create_sync_job(job_id, name, len(codes))

    # Start async task
    asyncio.create_task(_run_pool_sync(job_id, name, codes, db))

    return {"job_id": job_id}


async def _run_pool_sync(job_id: str, pool_name: str, codes: list[str], db) -> None:
    """Background task to sync pool stocks and update progress."""
    from datetime import date, timedelta
    from quanti.data.akshare_adapter import AkShareAdapter
    import asyncio
    from functools import partial

    end_d = date.today()
    start_d = end_d - timedelta(days=365)
    adapter = AkShareAdapter(db)
    errors: dict[str, str] = {}
    loop = asyncio.get_event_loop()

    for i, code in enumerate(codes):
        try:
            fn = partial(adapter.sync_daily_quotes, code, start=start_d, end=end_d, repair_gaps=False)
            count = await loop.run_in_executor(None, fn)
            if count == 0:
                errors[code] = "未获取到数据"
        except Exception as e:
            errors[code] = str(e)
        db.update_sync_job(job_id, i + 1, "running", errors)

    final_status = "error" if errors else "done"
    db.update_sync_job(job_id, len(codes), final_status, errors)


@router.get("/pools/{name}/sync/status")
async def get_sync_status(name: str, job_id: str, request: Request):
    """Get sync job progress."""
    db = request.app.state.db
    job = db.get_sync_job(job_id)
    if job is None:
        return {"error": f"Job '{job_id}' not found"}
    if job["pool_name"] != name:
        return {"error": "Job does not belong to this pool"}
    current = job["current"]
    total = job["total"]
    status = job["status"]
    err_count = len(job["errors"])
    eta = _calc_eta_seconds(job)
    if status == "running":
        message = f"已同步 {current}/{total}"
        if eta is not None:
            message += f"，约 {eta // 60} 分钟"
    elif status == "done":
        message = f"同步完成，共 {total} 只"
    else:
        message = f"同步结束，{err_count} 只失败"
    return SyncStatusResponse(
        job_id=job_id, current=current, total=total,
        status=status, errors=job["errors"], message=message,
        eta_seconds=eta,
    )


@router.get("/stocks")
async def list_stocks(request: Request):
    db = request.app.state.db
    stocks = db.list_stocks()
    return [
        {
            "code": s.code,
            "name": s.name,
            "exchange": s.exchange,
            "list_date": s.list_date.isoformat(),
            "industry": s.industry,
            "latest_date": (d.isoformat() if (d := db.get_latest_quote_date(s.code)) else None),
        }
        for s in stocks
    ]


@router.get("/stocks/{code}/quotes")
async def get_quotes(code: str, start: str, end: str, request: Request):
    provider = request.app.state.provider
    try:
        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
    except ValueError as e:
        raise HTTPException(status_code=422,
                            detail=f"invalid date (use YYYY-MM-DD): {e}")
    df = provider.get_daily_df(code, start_d, end_d)
    records = []
    for _, row in df.iterrows():
        d = row["date"]
        records.append(
            {
                "date": d.isoformat() if hasattr(d, "isoformat") else str(d),
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
                "amount": row["amount"],
            }
        )
    return records


@router.get("/screeners")
async def list_screeners(request: Request):
    """List all available screener plugins."""
    screeners_dir = request.app.state.screeners_dir
    all_screeners = []

    if screeners_dir:
        loader = ScreenerLoader()
        all_screeners = loader.load_directory(screeners_dir)

    return [
        {
            "name": s.name,
            "name_zh": getattr(s, "name_zh", "") or s.name,
            "description": s.description,
        }
        for s in all_screeners
    ]


@router.post("/screen/run")
async def run_screen(body: ScreenRequest, request: Request):
    """Run a screener against stocks."""
    import logging
    from datetime import timedelta

    logger = logging.getLogger(__name__)
    db = request.app.state.db
    provider = request.app.state.provider

    # Find screener
    screener = None
    screeners_dir = request.app.state.screeners_dir
    if screeners_dir:
        loader = ScreenerLoader()
        for s in loader.load_directory(screeners_dir):
            if s.name == body.screener_name:
                screener = s
                break

    if screener is None:
        return {"error": f"Screener '{body.screener_name}' not found"}

    screener.init(body.params)

    # Determine stock pool
    codes = body.codes
    if not codes:
        # Use all stocks in DB (populated via sync_stock_list)
        all_stocks = db.list_stocks()
        codes = [s.code for s in all_stocks]

    if not codes:
        return ScreenResponse(
            screener=screener.name,
            description=screener.description,
            results=[],
            total_scanned=0,
        )

    end_d = date.fromisoformat(body.end) if body.end else date.today()
    start_d = end_d - timedelta(days=int(body.lookback_days * 1.5))  # extra margin

    # Auto-sync: fetch data for stocks with NO bars only (skip if already has data)
    from quanti.data.akshare_adapter import AkShareAdapter
    import asyncio

    adapter = AkShareAdapter(db)
    # Only sync stocks that have NO data at all (len == 0), skip stocks with partial data
    codes_to_sync = []
    for code in codes:
        bars = provider.get_daily_bars(code, start_d, end_d)
        if len(bars) == 0:
            codes_to_sync.append(code)

    if codes_to_sync:
        # Limit to first 50 to avoid overwhelming the network; rest will be synced on next run
        codes_to_sync = codes_to_sync[:50]
        logger.info(f"Screener auto-sync: syncing {len(codes_to_sync)} stocks (first 50 of total)")
        # Sync with limited concurrency to avoid rate limiting
        semaphore = asyncio.Semaphore(3)

        async def sync_one(code: str) -> None:
            async with semaphore:
                try:
                    loop = asyncio.get_event_loop()
                    count = await loop.run_in_executor(
                        None, lambda: adapter.sync_daily_quotes(code, start=start_d, end=end_d, repair_gaps=False)
                    )
                    logger.info(f"Screener auto-sync: {code} synced {count} bars")
                except Exception as e:
                    logger.warning(f"Auto-sync failed for {code}: {e}")

        await asyncio.gather(*[sync_one(c) for c in codes_to_sync])

    # Score each stock
    scored: list[ScreenResultItem] = []
    for code in codes:
        try:
            bars = provider.get_daily_bars(code, start_d, end_d)
            if len(bars) < 10:
                continue
            score = screener.screen(code, bars)
            if score > 0:
                latest = bars[-1]
                prev_close = bars[-2].close if len(bars) >= 2 else latest.close
                change_pct = round((latest.close - prev_close) / prev_close * 100, 2)
                stock = db.get_stock(code)
                name = stock.name if stock else code
                scored.append(
                    ScreenResultItem(
                        code=code,
                        name=name,
                        score=round(score, 3),
                        close=round(latest.close, 2),
                        change_pct=change_pct,
                    )
                )
        except Exception as e:
            logger.warning(f"Screen failed for {code}: {e}")

    # Sort by score descending, take top N
    scored.sort(key=lambda x: x.score, reverse=True)
    top = scored[: body.top_n]

    return ScreenResponse(
        screener=screener.name,
        description=screener.description,
        results=top,
        total_scanned=len(codes),
    )


# --- Agent / Goal / Portfolio ---


class GoalBody(BaseModel):
    target_annual_return: float = 0.20
    max_drawdown: float = -0.20
    risk_tolerance: str = "medium"  # low / medium / high
    universe_pool: str = ""
    screener_name: str = ""
    strategy_name: str = ""
    params: dict = {}
    rebalance_freq: str = "daily"
    enabled: bool = False


class ManualOrderBody(BaseModel):
    code: str
    direction: str  # buy / sell
    strength: float = 1.0
    reason: str = "manual"


@router.get("/goal")
async def get_goal_endpoint(request: Request):
    goal = load_goal(request.app.state.db)
    return goal.to_db()


@router.post("/goal")
async def set_goal_endpoint(body: GoalBody, request: Request):
    try:
        risk = RiskTolerance(body.risk_tolerance)
    except ValueError:
        raise HTTPException(status_code=422,
                            detail=f"invalid risk_tolerance: {body.risk_tolerance!r}; "
                                   "expected 'low' | 'medium' | 'high'")
    goal = Goal(
        target_annual_return=body.target_annual_return,
        max_drawdown=body.max_drawdown,
        risk_tolerance=risk,
        universe_pool=body.universe_pool,
        screener_name=body.screener_name,
        strategy_name=body.strategy_name,
        params=body.params,
        rebalance_freq=body.rebalance_freq,
        enabled=body.enabled,
    )
    save_goal(request.app.state.db, goal)
    return {"ok": True, "goal": goal.to_db()}


@router.get("/portfolio")
async def get_portfolio(request: Request):
    return request.app.state.broker.snapshot_portfolio()


@router.post("/portfolio/reset")
async def reset_portfolio(request: Request, initial_cash: float = 1_000_000.0):
    request.app.state.db.reset_portfolio(initial_cash)
    request.app.state.db.log_decision(
        "portfolio_reset", f"组合重置，初始资金 {initial_cash:,.0f}")
    return request.app.state.broker.snapshot_portfolio()


@router.get("/portfolio/snapshots")
async def list_portfolio_snapshots(request: Request, limit: int = 365):
    return request.app.state.db.get_portfolio_snapshots(limit=limit)


@router.get("/orders")
async def list_orders(request: Request, limit: int = 200):
    return request.app.state.db.list_orders(limit=limit)


@router.get("/trades")
async def list_trades(request: Request, limit: int = 200):
    return request.app.state.db.list_trades(limit=limit)


@router.post("/orders/manual")
async def manual_order(body: ManualOrderBody, request: Request):
    broker = request.app.state.broker
    try:
        direction = Direction(body.direction)
    except ValueError:
        raise HTTPException(status_code=422,
                            detail=f"invalid direction: {body.direction!r}; "
                                   "expected 'buy' or 'sell'")
    if not body.code or not body.code.strip():
        raise HTTPException(status_code=422, detail="code is required")
    signal = Signal(
        stock_code=body.code.strip(),
        direction=direction,
        strength=max(min(body.strength, 1.0), 0.05),
        reason=body.reason,
    )
    filled = broker.execute_signal(signal, strategy_name="manual")
    return {"filled": filled, "snapshot": broker.snapshot_portfolio()}


@router.post("/agent/start")
async def agent_start(request: Request):
    request.app.state.agent.start()
    return {"status": "started"}


@router.post("/agent/stop")
async def agent_stop(request: Request):
    request.app.state.agent.stop()
    return {"status": "stopped"}


@router.post("/agent/tick")
async def agent_tick(request: Request):
    return request.app.state.agent.tick()


@router.get("/agent/status")
async def agent_status(request: Request):
    s = request.app.state.agent.status()
    # Pending-order count is cheap and we surface it here so the UI can
    # show it next to the Agent state without an extra round trip.
    try:
        pending_count = len(
            request.app.state.db.list_orders(status="pending", limit=1000))
    except Exception:
        pending_count = 0
    return {
        "enabled": s.enabled, "running": s.running,
        "last_tick_at": s.last_tick_at, "last_tick_summary": s.last_tick_summary,
        "last_strategy": s.last_strategy,
        "last_evaluations": s.last_evaluations,
        "total_value": s.total_value, "pnl_pct": s.pnl_pct,
        "pending_orders": pending_count,
    }


@router.get("/agent/decisions")
async def agent_decisions(request: Request, limit: int = 100, kind: str | None = None):
    return request.app.state.db.list_decisions(limit=limit, kind=kind)


@router.post("/agent/decisions/prune")
async def agent_prune_decisions(request: Request, older_than_days: int = 90):
    """Manually trim the decision log to free space / focus the UI."""
    if older_than_days < 1:
        raise HTTPException(status_code=422,
                            detail="older_than_days must be >= 1")
    removed = request.app.state.db.prune_decisions(older_than_days)
    return {"removed": removed, "older_than_days": older_than_days}


@router.get("/strategies")
async def list_strategies(request: Request):
    """List available strategy plugins."""
    strategies_dir = request.app.state.strategies_dir
    if not strategies_dir:
        return []
    loader = StrategyLoader()
    return [
        {
            "name": s.name,
            "name_zh": getattr(s, "name_zh", "") or s.name,
            "description": getattr(s, "description", "") or "",
        }
        for s in loader.load_directory(strategies_dir)
    ]


@router.post("/backtest/run")
async def run_backtest(body: BacktestRequest, request: Request):
    import logging

    from quanti.data.akshare_adapter import AkShareAdapter

    logger = logging.getLogger(__name__)
    db = request.app.state.db
    provider = request.app.state.provider

    # Auto-sync: if any stock has no data, fetch it automatically
    try:
        start_d = date.fromisoformat(body.start)
        end_d = date.fromisoformat(body.end)
    except ValueError as e:
        raise HTTPException(status_code=422,
                            detail=f"invalid date (use YYYY-MM-DD): {e}")
    for code in body.codes:
        bars = provider.get_daily_bars(code, start_d, end_d)
        if len(bars) == 0:
            logger.info(f"No data for {code}, auto-syncing...")
            try:
                adapter = AkShareAdapter(db)
                adapter.sync_daily_quotes(code, start=start_d, end=end_d, repair_gaps=False)
            except Exception as e:
                logger.warning(f"Auto-sync failed for {code}: {e}")

    # Find strategy by name via the dynamic loader. The user can plug a
    # strategy at any path via app.state.strategies_dir, so we no longer
    # hard-import any built-in modules here.
    strategy = None
    strategies_dir = request.app.state.strategies_dir
    if strategies_dir:
        loader = StrategyLoader()
        for s in loader.load_directory(strategies_dir):
            if s.name == body.strategy_name:
                strategy = s
                break

    if strategy is None:
        return {"error": f"Strategy '{body.strategy_name}' not found"}

    strategy.init(body.params)

    engine = BacktestEngine(provider=provider, initial_cash=body.initial_cash)
    result = engine.run(
        strategy=strategy,
        codes=body.codes,
        start=date.fromisoformat(body.start),
        end=date.fromisoformat(body.end),
    )

    return BacktestResponse(
        metrics=result.metrics,
        warning=result.skip_reason,
        trades=[
            TradeResponse(
                date=t.date.isoformat(),
                stock_code=t.stock_code,
                direction=t.direction.value,
                quantity=t.quantity,
                price=round(t.price, 4),
                commission=round(t.commission, 4),
            )
            for t in result.trades
        ],
        equity_curve={
            d.isoformat() if hasattr(d, "isoformat") else str(d): round(v, 2)
            for d, v in result.equity_curve.items()
        },
    )
