"""API route definitions."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta

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
    codes: list[str] = []
    start: str  # YYYY-MM-DD
    end: str
    initial_cash: float = 1_000_000.0
    params: dict = {}
    apply_risk: bool = True  # apply live exit policy (stop-loss/TP/caps)
    survivorship_free: bool = False  # backtest the point-in-time universe instead of `codes`
    max_universe: int = 300          # cap on the survivorship-free universe size


class TradeResponse(BaseModel):
    date: str
    stock_code: str
    direction: str
    quantity: int
    price: float
    commission: float
    strategy: str = ""
    reason: str = ""  # exits: 止损 / 移动止盈 / 策略离场


class BacktestResponse(BaseModel):
    metrics: dict
    trades: list[TradeResponse]
    equity_curve: dict[str, float]
    warning: str = ""


class SyncRequest(BaseModel):
    codes: list[str]
    # Opt-in fail-loud: codes whose post-sync coverage (present/expected trading
    # days) is below this are recorded as ERRORS, not just warnings. None/0 →
    # completeness is reported as warnings only (job still 'done').
    min_coverage: float | None = None
    # Sync granularity (from the UI 同步设置 card). years → history window;
    # with_basic → also pull daily_basic (turnover + valuation); with_financials
    # → run a whole-market financials pass after the quotes.
    years: int = 1
    with_basic: bool = False
    with_financials: bool = False


class SyncResult(BaseModel):
    synced: dict[str, int]  # code -> bar count
    errors: dict[str, str] = {}  # code -> error message


class StockPoolStats(BaseModel):
    total: int
    with_quotes: int  # stocks that have quote data
    exchange_sh: int
    exchange_sz: int
    latest_quote_date: str | None = None  # newest bar date across all codes


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
    """Sync daily quotes for given stock codes from the configured source.
    Honors the UI 同步设置: years (window), with_basic (turnover+valuation),
    with_financials (whole-market financials pass after)."""
    from datetime import date, timedelta
    from quanti.data.source import try_make_quote_adapter

    db = request.app.state.db
    adapter, src_err = try_make_quote_adapter(db)
    if src_err:
        return SyncResult(synced={}, errors={"_source": src_err})
    end_d = date.today()
    start_d = end_d - timedelta(days=365 * max(1, body.years))
    results = {}
    errors = {}
    for code in body.codes:
        try:
            count = adapter.sync_daily_quotes(code, start=start_d, end=end_d,
                                              with_basic=body.with_basic)
            results[code] = count
            if count == 0:
                errors[code] = "未获取到数据，可能是网络问题或股票代码无效"
        except Exception as e:
            results[code] = 0
            errors[code] = str(e)
    if body.with_financials:
        from quanti.data.source import sync_financials_years
        try:
            sync_financials_years(db, body.years)  # follows source, akshare backstop
        except Exception as e:  # noqa: BLE001 - optional
            errors["_financials"] = str(e)
    return SyncResult(synced=results, errors=errors)


@router.post("/sync/quotes/async")
async def sync_quotes_async(body: SyncRequest, request: Request):
    """Start async sync for given stock codes. Returns job_id immediately."""

    db = request.app.state.db
    codes = body.codes
    if not codes:
        all_stocks = db.list_stocks()
        codes = [s.code for s in all_stocks]
    if not codes:
        return {"error": "没有可同步的股票"}

    job_id = f"q_{str(uuid.uuid4())[:8]}"
    db.create_sync_job(job_id, "_quotes_sync", len(codes))
    asyncio.create_task(_run_quotes_sync(
        job_id, codes, db, min_coverage=body.min_coverage,
        years=body.years, with_basic=body.with_basic,
        with_financials=body.with_financials))

    return {"job_id": job_id}


async def _run_quotes_sync(job_id: str, codes: list[str], db,
                           min_coverage: float | None = None,
                           years: int = 1, with_basic: bool = False,
                           with_financials: bool = False) -> None:
    from datetime import date, timedelta
    from quanti.data.source import try_make_quote_adapter
    from quanti.data.integrity import (check_quote_completeness,
                                       expected_trading_days)
    import asyncio
    from functools import partial

    end_d = date.today()
    start_d = end_d - timedelta(days=365 * max(1, years))
    adapter, src_err = try_make_quote_adapter(db)
    if src_err:
        db.update_sync_job(job_id, 0, "error", {"_source": src_err})
        return
    # Window-level calendar lookup once, reused for every code's completeness.
    exp_days, used_cal = expected_trading_days(db, start_d, end_d)
    errors: dict[str, str] = {}
    warnings: dict[str, str] = {}
    loop = asyncio.get_event_loop()

    for i, code in enumerate(codes):
        try:
            fn = partial(adapter.sync_daily_quotes, code, start=start_d,
                         end=end_d, repair_gaps=False, with_basic=with_basic)
            count = await loop.run_in_executor(None, fn)
            if count == 0:
                errors[code] = "未获取到数据"
            else:
                # Validate what actually landed (source-agnostic): real missing
                # trading days vs the calendar, plus OHLC/dup defects.
                rep = check_quote_completeness(
                    db, code, start_d, end_d,
                    expected_days=exp_days, used_calendar=used_cal)
                if min_coverage and rep.coverage < min_coverage:
                    errors[code] = f"完整性不足: {rep.summary()}"
                elif not rep.clean:
                    warnings[code] = rep.summary()
        except Exception as e:
            errors[code] = str(e)
        db.update_sync_job(job_id, i + 1, "running", errors, warnings)

    if with_financials:
        from quanti.data.source import sync_financials_years
        try:
            await loop.run_in_executor(
                None, partial(sync_financials_years, db, years))  # follows source
        except Exception as e:  # noqa: BLE001 - financials optional; don't fail job
            warnings["_financials"] = str(e)

    final_status = "error" if errors else "done"
    db.update_sync_job(job_id, len(codes), final_status, errors, warnings)


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
    warns = job.get("warnings", {})
    warn_count = len(warns)
    eta = _calc_eta_seconds(job)
    if status == "running":
        message = f"已同步 {current}/{total}"
        if eta is not None:
            message += f"，约 {eta // 60} 分钟"
    elif status == "done":
        message = f"同步完成，共 {total} 只"
        if warn_count:
            message += f"（{warn_count} 只有完整性告警）"
    else:
        message = f"同步结束，{err_count} 只失败"
        if warn_count:
            message += f"，{warn_count} 只告警"
    return SyncStatusResponse(
        job_id=job_id, current=current, total=total,
        status=status, errors=job["errors"], message=message,
        eta_seconds=eta, warnings=warns,
    )


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/meta")
async def meta(request: Request):
    """Which account this server serves — drives the UI's 模拟盘/实盘 badge."""
    account = getattr(request.app.state, "account", "paper")
    return {"account": account, "is_live": account == "live"}


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
    from quanti.data.source import try_make_quote_adapter

    db = request.app.state.db
    adapter, src_err = try_make_quote_adapter(db)
    if src_err:
        return {"synced": 0, "error": src_err, "message": src_err}
    # Fail fast + clean: roster sync is a few stock_basic calls, which on a
    # low-tier tushare token is 1/min. Don't 500 / block the request waiting out
    # the limit — return a clear message; the CLI (`quanti sync --stocks`) syncs
    # patiently (waits the per-minute window).
    try:
        count = adapter.sync_stock_list()
    except Exception as e:  # noqa: BLE001
        msg = (f"同步失败: {e}。若为频率超限,请稍后重试,"
               f"或用 CLI `quanti sync --stocks`(自动按分钟错峰)。")
        return {"synced": 0, "error": str(e), "message": msg}
    return {"synced": count, "message": f"成功同步 {count} 只股票到股票池"}


@router.get("/stocks/stats")
async def stock_pool_stats(request: Request):
    """Get stock pool statistics."""
    db = request.app.state.db
    all_stocks = db.list_stocks()
    with_quotes = request.app.state.provider.get_all_codes()
    latest = db.get_global_latest_quote_date()
    return StockPoolStats(
        total=len(all_stocks),
        with_quotes=len(with_quotes),
        exchange_sh=sum(1 for s in all_stocks if s.exchange.upper() in ("SH", "SHANGHAI")),
        exchange_sz=sum(1 for s in all_stocks if s.exchange.upper() in ("SZ", "SHENZHEN")),
        latest_quote_date=latest.isoformat() if latest else None,
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
    warnings: dict = {}  # code -> completeness/quality summary (non-fatal)


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
    from quanti.data.source import try_make_quote_adapter
    from quanti.data.integrity import (check_quote_completeness,
                                       expected_trading_days)
    import asyncio
    from functools import partial

    end_d = date.today()
    start_d = end_d - timedelta(days=365)
    adapter, src_err = try_make_quote_adapter(db)
    if src_err:
        db.update_sync_job(job_id, 0, "error", {"_source": src_err})
        return
    exp_days, used_cal = expected_trading_days(db, start_d, end_d)
    errors: dict[str, str] = {}
    warnings: dict[str, str] = {}
    loop = asyncio.get_event_loop()

    for i, code in enumerate(codes):
        try:
            fn = partial(adapter.sync_daily_quotes, code, start=start_d, end=end_d, repair_gaps=False)
            count = await loop.run_in_executor(None, fn)
            if count == 0:
                errors[code] = "未获取到数据"
            else:
                rep = check_quote_completeness(
                    db, code, start_d, end_d,
                    expected_days=exp_days, used_calendar=used_cal)
                if not rep.clean:
                    warnings[code] = rep.summary()
        except Exception as e:
            errors[code] = str(e)
        db.update_sync_job(job_id, i + 1, "running", errors, warnings)

    final_status = "error" if errors else "done"
    db.update_sync_job(job_id, len(codes), final_status, errors, warnings)


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
    warns = job.get("warnings", {})
    warn_count = len(warns)
    eta = _calc_eta_seconds(job)
    if status == "running":
        message = f"已同步 {current}/{total}"
        if eta is not None:
            message += f"，约 {eta // 60} 分钟"
    elif status == "done":
        message = f"同步完成，共 {total} 只"
        if warn_count:
            message += f"（{warn_count} 只有完整性告警）"
    else:
        message = f"同步结束，{err_count} 只失败"
        if warn_count:
            message += f"，{warn_count} 只告警"
    return SyncStatusResponse(
        job_id=job_id, current=current, total=total,
        status=status, errors=job["errors"], message=message,
        eta_seconds=eta, warnings=warns,
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
    df = provider.get_daily_df(code, start_d, end_d, adjust="none")  # 真实市场价
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
    from quanti.data.source import try_make_quote_adapter
    import asyncio

    # If the source is unavailable (e.g. tushare, no token — no silent akshare
    # fallback), skip auto-sync and run the screener on whatever data exists.
    adapter, src_err = try_make_quote_adapter(db)
    if src_err:
        logger.warning(f"Screener auto-sync skipped (data source): {src_err}")
    # Only sync stocks that have NO data at all (len == 0), skip stocks with partial data
    codes_to_sync = []
    if adapter is not None:
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


class DataSourceBody(BaseModel):
    source: str  # tushare | akshare | xtdata
    token: str | None = None  # only for sources needing a key; None = keep existing


@router.get("/config/data-source")
async def get_data_source(request: Request):
    """Current data source + whether a token is configured. The token itself is
    NEVER returned (secret)."""
    from quanti.data.source import VALID_SOURCES, resolve_source, tushare_token
    db = request.app.state.db
    return {
        "source": resolve_source(db),
        "has_token": bool(tushare_token(db)),
        "available_sources": list(VALID_SOURCES),
    }


@router.post("/config/data-source/test")
async def test_data_source(body: DataSourceBody, request: Request):
    """Probe connectivity for a source (using the passed token, or the stored
    one if omitted). Does NOT persist anything."""
    from quanti.data.source import probe_source
    ok, message = probe_source(body.source, body.token, request.app.state.db)
    return {"ok": ok, "message": message}


@router.post("/config/data-source")
async def set_data_source(body: DataSourceBody, request: Request):
    """Validate connectivity first; only persist (source + token) if it passes."""
    from quanti.data.source import VALID_SOURCES, probe_source
    if body.source not in VALID_SOURCES:
        raise HTTPException(status_code=422,
                            detail=f"invalid source: {body.source!r}; "
                                   f"expected one of {VALID_SOURCES}")
    db = request.app.state.db
    ok, message = probe_source(body.source, body.token, db)
    if not ok:
        return {"ok": False, "message": message}
    db.upsert_app_config(body.source, body.token)
    return {"ok": True, "message": message}


class RiskControlBody(BaseModel):
    stop_loss_pct: float
    portfolio_stop_loss_pct: float
    take_profit_activate_pct: float
    take_profit_trail_pct: float
    strategy_exit_enabled: bool
    atr_stop_k: float
    atr_stop_n: int
    # Concentration trim (削峰) — opt-in, default off. Defaulted so existing
    # clients that omit them still validate.
    drift_trim_enabled: bool = False
    drift_trim_to_pct: float = 0.10
    drift_trim_band: float = 0.25
    # Score-gated rotation (换仓) — opt-in, default off.
    rotation_enabled: bool = False
    rotation_margin: float = 0.15
    # Concentration caps (单票/行业上限). Defaulted so existing clients that
    # omit them still validate; also the LLM's per-order size ceiling.
    max_position_pct: float = 0.20
    max_industry_pct: float = 0.30


def _risk_config_dict(db) -> dict:
    """Current effective risk thresholds (persisted overrides + defaults)."""
    from quanti.risk.manager import risk_config_from_dict
    cfg = risk_config_from_dict(db.get_risk_config())
    return {
        "stop_loss_pct": cfg.stop_loss_pct,
        "portfolio_stop_loss_pct": cfg.portfolio_stop_loss_pct,
        "take_profit_activate_pct": cfg.take_profit_activate_pct,
        "take_profit_trail_pct": cfg.take_profit_trail_pct,
        "strategy_exit_enabled": cfg.strategy_exit_enabled,
        "atr_stop_k": cfg.atr_stop_k,
        "atr_stop_n": cfg.atr_stop_n,
        "drift_trim_enabled": cfg.drift_trim_enabled,
        "drift_trim_to_pct": cfg.drift_trim_to_pct,
        "drift_trim_band": cfg.drift_trim_band,
        "rotation_enabled": cfg.rotation_enabled,
        "rotation_margin": cfg.rotation_margin,
        "max_position_pct": cfg.max_position_pct,
        "max_industry_pct": cfg.max_industry_pct,
    }


@router.get("/config/risk-control")
async def get_risk_control(request: Request):
    """Current runtime risk thresholds (effective values, overrides + defaults)."""
    return _risk_config_dict(request.app.state.db)


@router.post("/config/risk-control")
async def set_risk_control(body: RiskControlBody, request: Request):
    """Validate + persist runtime risk thresholds. Brokers read these live, so
    changes take effect on the next exit/breaker check — no restart (P0-3)."""
    errs = []
    if body.stop_loss_pct >= 0:
        errs.append("单标的止损线必须 < 0(负百分比)")
    if body.portfolio_stop_loss_pct >= 0:
        errs.append("组合熔断线必须 < 0")
    if body.stop_loss_pct < -0.9 or body.portfolio_stop_loss_pct < -0.9:
        errs.append("止损/熔断线过深(< -90%),疑似填错")
    if body.take_profit_activate_pct < 0 or body.take_profit_trail_pct < 0:
        errs.append("止盈参数必须 ≥ 0")
    if body.atr_stop_k < 0:
        errs.append("atr_stop_k 必须 ≥ 0")
    if body.atr_stop_n < 1:
        errs.append("atr_stop_n 必须 ≥ 1")
    if not (0 < body.drift_trim_to_pct <= 1):
        errs.append("削峰目标权重 drift_trim_to_pct 必须在 (0, 1]")
    if body.drift_trim_band < 0:
        errs.append("削峰带 drift_trim_band 必须 ≥ 0")
    if not (0 < body.rotation_margin <= 1):
        errs.append("换仓分数门 rotation_margin 必须在 (0, 1]")
    if not (0 < body.max_position_pct <= 0.5):
        errs.append("单票上限 max_position_pct 必须在 (0, 50%]")
    if not (0 < body.max_industry_pct <= 1):
        errs.append("行业上限 max_industry_pct 必须在 (0, 100%]")
    if errs:
        raise HTTPException(status_code=422, detail="; ".join(errs))
    request.app.state.db.upsert_risk_config(body.model_dump())
    return {"ok": True, **_risk_config_dict(request.app.state.db)}


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
    snap = request.app.state.broker.snapshot_portfolio()
    # Annotate each holding with its current fused candidate score (None when
    # the name is no longer a candidate → UI shows "—"; that's also when
    # rotation treats it as the weakest).
    scores = request.app.state.db.get_candidate_scores()
    for p in snap.get("positions", []):
        p["score"] = scores.get(p.get("code"))
    return snap


@router.get("/agent/live-status")
async def live_status(request: Request):
    """Live status card: intraday-guard daemon state + per-holding stop price
    (买入价/当前价/止损价). Stop price is computed server-side from the risk
    config + ATR ratios: avg_cost·(1+max(floor, -k·ATRratio))."""
    from quanti.execution.exits import compute_atr_ratios
    from quanti.utils.market import in_trading_session

    st = request.app.state
    broker = st.broker
    snap = broker.snapshot_portfolio()
    positions = snap.get("positions", [])
    risk = getattr(broker, "_risk", None)
    atr_n = risk.config.atr_stop_n if risk else 14
    ratios = (compute_atr_ratios(st.provider, [{"code": p["code"]} for p in positions],
                                 atr_n) if positions else {})
    out = []
    for p in positions:
        info = (risk.stop_info(p["avg_cost"], ratios.get(p["code"])) if risk
                else {"stop_pct": 0.0, "stop_price": 0.0, "atr_driven": False})
        out.append({
            "code": p["code"], "name": p.get("name", p["code"]),
            "quantity": p.get("quantity", 0),
            "avg_cost": p["avg_cost"], "current_price": p["current_price"],
            "pnl_pct": p.get("pnl_pct", 0.0),
            # entry_strategy rides along from list_positions via snapshot's
            # **pos spread; surface it for the live-status 进场策略 column.
            "entry_strategy": p.get("entry_strategy", ""), **info,
        })
    connected = broker.is_connected() if hasattr(broker, "is_connected") else None
    return {
        "is_live": st.account == "live",
        "guard": {**st.agent.guard_status(), "connected": connected,
                  "in_session": in_trading_session(None, st.provider)},
        "positions": out,
    }


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


@router.get("/orders/pending")
async def list_pending_orders(request: Request):
    """Pending orders enriched with their fill timeline (queued time,
    expected fill date, whether the bar is available, TTL). Drives the
    Agent page's 待成交订单 detail."""
    orders = request.app.state.broker.pending_orders_detail()
    scores = request.app.state.db.get_candidate_scores()
    for o in orders:
        o["score"] = scores.get(o.get("code"))
    return orders


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


@router.post("/agent/mine-factors/async")
async def mine_factors_async(request: Request):
    """Start an async LLM factor-mining job. Returns job_id immediately."""
    db = request.app.state.db
    n = 10
    job_id = f"mine_{str(uuid.uuid4())[:8]}"
    db.create_sync_job(job_id, "_mine", n)
    asyncio.create_task(_run_mine(job_id, request.app.state, n))
    return {"job_id": job_id}


async def _run_mine(job_id: str, state, n: int) -> None:
    """Background worker: build LLM client, run mine_factors in a thread pool."""
    from quanti.agent import factor_miner
    from quanti.agent.goal import load_goal
    from quanti.agent.llm_runtime import build_llm_client
    from quanti.agent.universe import resolve_tradable_universe
    from quanti.data.provider import DataProvider

    db = state.db
    loop = asyncio.get_event_loop()

    def work() -> None:
        goal = load_goal(db)
        params = goal.params or {}
        provider = DataProvider(db)
        # Same tradable-universe selection the live agent uses, as of today.
        codes = resolve_tradable_universe(
            db, provider, pool=goal.universe_pool,
            params=goal.params, as_of=date.today())
        llm = build_llm_client(params)
        db.update_sync_job(job_id, 0, "running", {})
        results = factor_miner.mine_factors(
            llm, db, provider, codes, date.today(), n_candidates=n
        )
        db.update_sync_job(job_id, len(results), "done", {})

    try:
        await loop.run_in_executor(None, work)
    except Exception as e:  # noqa: BLE001
        db.update_sync_job(job_id, 0, "error", {"error": str(e)})


@router.post("/factors/rescore/async")
async def rescore_factors_async(request: Request):
    """Re-validate every existing generated factor against current data (no
    LLM): recompute IC, refresh `accepted`, keep `enabled`. Returns job_id;
    poll the same /agent/mine-factors/status."""
    db = request.app.state.db
    job_id = f"rescore_{str(uuid.uuid4())[:8]}"
    db.create_sync_job(job_id, "_rescore", len(db.list_generated_factors()))
    asyncio.create_task(_run_rescore(job_id, request.app.state))
    return {"job_id": job_id}


async def _run_rescore(job_id: str, state) -> None:
    """Background worker: re-score the generated-factor library in a thread pool."""
    from quanti.agent import factor_miner
    from quanti.agent.goal import load_goal
    from quanti.agent.universe import resolve_tradable_universe
    from quanti.data.provider import DataProvider

    db = state.db
    loop = asyncio.get_event_loop()

    def work() -> None:
        goal = load_goal(db)
        provider = DataProvider(db)
        codes = resolve_tradable_universe(
            db, provider, pool=goal.universe_pool,
            params=goal.params, as_of=date.today())
        db.update_sync_job(job_id, 0, "running", {})
        results = factor_miner.rescore_generated_factors(
            db, provider, codes, date.today())
        db.update_sync_job(job_id, len(results), "done", {})

    try:
        await loop.run_in_executor(None, work)
    except Exception as e:  # noqa: BLE001
        db.update_sync_job(job_id, 0, "error", {"error": str(e)})


@router.get("/agent/mine-factors/status")
async def mine_factors_status(job_id: str, request: Request):
    """Get async mine-factors job progress and results."""
    db = request.app.state.db
    job = db.get_sync_job(job_id)
    if job is None:
        return {"error": f"Job '{job_id}' not found"}
    return {
        "job_id": job["job_id"],
        "current": job["current"],
        "total": job["total"],
        "status": job["status"],
        "results": db.list_generated_factors(),
    }


@router.get("/factors/generated")
async def generated_factors(request: Request):
    """List all generated (LLM-mined) factors."""
    return request.app.state.db.list_generated_factors()


class _EnabledBody(BaseModel):
    enabled: bool


@router.post("/factors/generated/{name}/enabled")
async def set_generated_enabled(name: str, body: _EnabledBody, request: Request):
    """Enable or disable a generated factor by name."""
    request.app.state.db.set_factor_enabled(name, body.enabled)
    return {"ok": True, "name": name, "enabled": body.enabled}


@router.post("/agent/start")
async def agent_start(request: Request):
    request.app.state.agent.start()
    return {"status": "started"}


@router.post("/agent/stop")
async def agent_stop(request: Request):
    request.app.state.agent.stop()
    return {"status": "stopped"}


@router.post("/agent/restart")
async def agent_restart(request: Request):
    """Stop the agent loop thread and start a fresh one so a changed daily
    schedule takes effect immediately. Persisted goal.enabled is untouched."""
    request.app.state.agent.restart()
    return {"status": "restarted"}


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
        "started_at": s.started_at, "tick_interval_sec": s.tick_interval_sec,
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


def _exit_kind(order: dict) -> str:
    """Classify a risk-driven exit order by its reason/strategy tag."""
    reason = order.get("reason") or ""
    if order.get("strategy_name") == "kill_switch" or "熔断" in reason:
        return "circuit_breaker"
    if reason.startswith("止损"):
        return "stop_loss"
    if reason.startswith("移动止盈"):
        return "trailing_tp"
    if "策略离场" in reason:
        return "strategy_exit"
    return "other"


def build_risk_audit(db, provider, broker, account: str,
                     exits_limit: int = 50) -> dict:
    """Aggregate the risk-control audit view: exit thresholds, three-channel
    parity, the live protection-guard / circuit-breaker state, and recent
    risk-driven exits. Pure (no Request) so it's unit-testable. Read-only."""
    # Read the effective (runtime-overridable) config straight from the DB so
    # the audit reflects edits even before a broker exit-cycle re-syncs (P0-3).
    from quanti.risk.manager import risk_config_from_dict
    rc = risk_config_from_dict(db.get_risk_config())
    pc = broker._protections.config
    tp_on = rc.take_profit_activate_pct > 0
    se_on = rc.strategy_exit_enabled
    atr_on = rc.atr_stop_k > 0

    exits = {
        "stop_loss": {"enabled": True, "threshold": rc.stop_loss_pct},
        "atr_stop": {"enabled": atr_on, "k": rc.atr_stop_k, "n": rc.atr_stop_n},
        "trailing_take_profit": {"enabled": tp_on,
                                 "activate": rc.take_profit_activate_pct,
                                 "trail": rc.take_profit_trail_pct},
        "strategy_exit": {"enabled": se_on},
        "portfolio_circuit_breaker": {"threshold": rc.portfolio_stop_loss_pct},
    }

    # Three-channel parity. Reflects code behaviour + current config switches.
    # Stop-loss is always on; trailing-TP/strategy-exit follow the switches.
    # Backtest deliberately omits strategy-exit (the strategy's own SELL replays
    # via on_bar, so check_exits passes an empty set — engine.py).
    channel_parity = [
        {"channel": "回测", "stop_loss": True, "trailing_tp": tp_on,
         "strategy_exit": False,
         "note": "策略 SELL 经 on_bar 重放,不重复计入 check_exits"},
        {"channel": "模拟盘 Paper", "stop_loss": True, "trailing_tp": tp_on,
         "strategy_exit": se_on, "note": ""},
        {"channel": "实盘 QMT", "stop_loss": True, "trailing_tp": tp_on,
         "strategy_exit": se_on,
         "note": "三档与回测/Paper 对齐;触发为每 tick 收盘价粒度,非盘中"},
    ]

    # Live protection-guard state: locked = new BUYs are blocked right now.
    guard: dict = {
        "enabled": pc.enabled, "locked": False, "reason": "",
        "stoploss_guard": {
            "enabled": pc.stoploss_guard_enabled,
            "lookback_days": pc.sg_lookback_days,
            "trade_limit": pc.sg_trade_limit, "lock_days": pc.sg_lock_days},
        "max_drawdown": {
            "enabled": pc.max_drawdown_enabled,
            "lookback_days": pc.md_lookback_days,
            "max_drawdown_pct": pc.md_max_drawdown_pct,
            "lock_days": pc.md_lock_days},
    }
    if pc.enabled:
        try:
            from quanti.risk.protection_context import build_db_context
            ctx = build_db_context(db, provider, pc)
            allowed, reason = broker._protections.check_entry(ctx)
            guard["locked"] = not allowed
            guard["reason"] = reason
        except Exception:  # noqa: BLE001 - an audit view must never 500
            pass
    since = date.today() - timedelta(days=pc.sg_lookback_days * 2 + 14)
    guard["recent_stop_losses"] = len(db.stop_loss_exit_dates(since))

    # Circuit-breaker proximity: equity vs all-time high-water mark.
    snaps = db.get_portfolio_snapshots(limit=1)
    total = float(snaps[0]["total_value"]) if snaps else None
    peak = db.get_peak_total_value()
    if total is not None and peak and peak > 0:
        peak = max(peak, total)
        dd = (total - peak) / peak
        cb = {"total_value": total, "peak_value": peak, "drawdown": dd,
              "threshold": rc.portfolio_stop_loss_pct,
              "tripped": dd <= rc.portfolio_stop_loss_pct,
              "headroom": dd - rc.portfolio_stop_loss_pct}
    else:
        cb = {"total_value": total, "peak_value": peak or None,
              "drawdown": None, "threshold": rc.portfolio_stop_loss_pct,
              "tripped": False, "headroom": None}

    # Recent risk-driven exits (filled SELLs from risk_exit / kill_switch).
    # No strategy_name filter on the query, so scan the recent window and pick.
    recent = [
        {"ts": o.get("filled_at") or o.get("created_at"), "code": o.get("code"),
         "kind": _exit_kind(o), "reason": o.get("reason") or "",
         "price": o.get("filled_price"), "quantity": o.get("filled_quantity")}
        for o in db.list_orders(limit=500)
        if o.get("direction") == "sell" and o.get("status") == "filled"
        and o.get("strategy_name") in ("risk_exit", "kill_switch")
    ]
    recent.sort(key=lambda x: x["ts"] or "", reverse=True)

    return {"account": account, "is_live": account == "live",
            "exits": exits, "channel_parity": channel_parity, "guard": guard,
            "circuit_breaker": cb, "recent_exits": recent[:exits_limit]}


@router.get("/risk/audit")
async def risk_audit(request: Request, exits_limit: int = 50):
    """Risk-control audit panel data. Read-only."""
    st = request.app.state
    return build_risk_audit(st.db, st.provider, st.broker,
                            getattr(st, "account", "paper"), exits_limit)


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

    from quanti.data.source import make_quote_adapter

    logger = logging.getLogger(__name__)
    db = request.app.state.db
    provider = request.app.state.provider

    try:
        start_d = date.fromisoformat(body.start)
        end_d = date.fromisoformat(body.end)
    except ValueError as e:
        raise HTTPException(status_code=422,
                            detail=f"invalid date (use YYYY-MM-DD): {e}")
    if body.survivorship_free:
        codes = db.point_in_time_universe(start_d, end_d)[:body.max_universe]
        logger.info(f"Survivorship-free universe: {len(codes)} stocks "
                    f"(cap {body.max_universe})")
    else:
        codes = body.codes
        # Auto-sync: if any stock has no data, fetch it automatically
        for code in codes:
            bars = provider.get_daily_bars(code, start_d, end_d)
            if len(bars) == 0:
                logger.info(f"No data for {code}, auto-syncing...")
                try:
                    adapter = make_quote_adapter(db)
                    adapter.sync_daily_quotes(code, start=start_d, end=end_d,
                                              repair_gaps=False)
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

    # Apply the live exit policy by default so the UI backtest reflects how
    # the agent actually trades (stop-loss / trailing take-profit / caps).
    from quanti.risk.manager import RiskConfig, RiskManager
    from quanti.risk.protections import ProtectionManager
    risk = RiskManager(RiskConfig()) if body.apply_risk else None
    protections = ProtectionManager() if body.apply_risk else None
    engine = BacktestEngine(provider=provider, initial_cash=body.initial_cash,
                            risk_manager=risk, protection_manager=protections)
    result = engine.run(
        strategy=strategy,
        codes=codes,
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
                strategy=t.strategy,
                reason=t.reason,
            )
            for t in result.trades
        ],
        equity_curve={
            d.isoformat() if hasattr(d, "isoformat") else str(d): round(v, 2)
            for d, v in result.equity_curve.items()
        },
    )


# --- Hyperopt async API ---


@router.post("/agent/optimize/async")
async def optimize_async(request: Request):
    """Start an on-demand hyperopt run. Returns a job_id immediately.

    Progress is tracked via /agent/optimize/status. Heavy work runs in a
    thread executor so the event loop is not blocked (same pattern as the
    quotes async sync).
    """
    db = request.app.state.db
    loader = StrategyLoader()
    strategies_dir = request.app.state.strategies_dir or "strategies"
    classes = [type(s) for s in loader.load_directory(strategies_dir)]
    n = len(classes)
    job_id = f"opt_{str(uuid.uuid4())[:8]}"
    db.create_sync_job(job_id, "_optimize", n)
    asyncio.create_task(_run_optimize(job_id, classes, request.app.state))
    return {"job_id": job_id}


async def _run_optimize(job_id: str, classes: list, state) -> None:
    """Worker coroutine: runs HyperOptimizer.optimize_all in a thread executor."""
    from datetime import date as _date

    from quanti.agent.hyperopt import HyperOptimizer
    from quanti.agent.universe import resolve_tradable_universe
    from quanti.backtest.engine import BacktestEngine
    from quanti.data.provider import DataProvider
    from quanti.agent.goal import load_goal
    from quanti.risk.manager import RiskConfig, RiskManager

    db = state.db
    loop = asyncio.get_event_loop()

    def work() -> None:
        goal = load_goal(db)
        provider = DataProvider(db)
        # Same tradable-universe selection the live agent uses (ADV-ranked,
        # optional liquidity filter) — not a dictionary-order slice.
        codes = resolve_tradable_universe(
            db, provider, pool=goal.universe_pool,
            params=goal.params, as_of=_date.today())

        db.update_sync_job(job_id, 0, "running", {})

        engine = BacktestEngine(
            provider=provider,
            initial_cash=1_000_000,
            risk_manager=RiskManager(RiskConfig()),
        )

        def progress(done: int, total: int, name: str) -> None:
            db.update_sync_job(job_id, done, "running", {"current_strategy": name})

        results = HyperOptimizer(engine).optimize_all(
            classes, codes, _date.today(), progress=progress
        )
        for r in results:
            db.save_optimization(
                r.strategy_name,
                r.chosen_params,
                r.tuned_oos_sharpe,
                r.default_oos_sharpe,
                r.accepted,
                r.n_combos_tried,
                len(codes),
            )
        db.update_sync_job(job_id, len(results), "done", {})

    try:
        await loop.run_in_executor(None, work)
    except Exception as e:  # noqa: BLE001
        db.update_sync_job(job_id, 0, "error", {"error": str(e)})


@router.get("/agent/optimize/status")
async def optimize_status(job_id: str, request: Request):
    """Get hyperopt job progress and results so far."""
    db = request.app.state.db
    job = db.get_sync_job(job_id)
    if job is None:
        return {"error": f"Job '{job_id}' not found"}
    current_strategy = job["errors"].get("current_strategy", "") if isinstance(job["errors"], dict) else ""
    return {
        "job_id": job["job_id"],
        "current": job["current"],
        "total": job["total"],
        "status": job["status"],
        "current_strategy": current_strategy,
        "results": db.list_optimization_results(),
    }


@router.get("/agent/tuned-params")
async def tuned_params(request: Request):
    """Return all stored optimization results (tuned strategy parameters)."""
    return request.app.state.db.list_optimization_results()
