"""API route definitions."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from pydantic import BaseModel

from quanti.backtest.engine import BacktestEngine
from quanti.models import Direction
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


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/stocks")
async def list_stocks(request: Request):
    stocks = request.app.state.db.list_stocks()
    return [
        {
            "code": s.code,
            "name": s.name,
            "exchange": s.exchange,
            "list_date": s.list_date.isoformat(),
            "industry": s.industry,
        }
        for s in stocks
    ]


@router.get("/stocks/{code}/quotes")
async def get_quotes(code: str, start: str, end: str, request: Request):
    provider = request.app.state.provider
    df = provider.get_daily_df(code, date.fromisoformat(start), date.fromisoformat(end))
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

    return [{"name": s.name, "description": s.description} for s in all_screeners]


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
        codes = provider.get_all_codes()

    if not codes:
        return ScreenResponse(
            screener=screener.name,
            description=screener.description,
            results=[],
            total_scanned=0,
        )

    end_d = date.fromisoformat(body.end) if body.end else date.today()
    start_d = end_d - timedelta(days=int(body.lookback_days * 1.5))  # extra margin

    # Auto-sync: fetch data for stocks with insufficient bars
    from quanti.data.akshare_adapter import AkShareAdapter

    adapter = AkShareAdapter(db)
    for code in codes:
        bars = provider.get_daily_bars(code, start_d, end_d)
        if len(bars) < 10:
            logger.info(f"Screener auto-sync: {code} has {len(bars)} bars, syncing...")
            try:
                adapter.sync_daily_quotes(code, start=start_d, end=end_d)
            except Exception as e:
                logger.warning(f"Auto-sync failed for {code}: {e}")

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


@router.post("/backtest/run")
async def run_backtest(body: BacktestRequest, request: Request):
    import logging

    from quanti.data.akshare_adapter import AkShareAdapter

    logger = logging.getLogger(__name__)
    db = request.app.state.db
    provider = request.app.state.provider

    # Auto-sync: if any stock has no data, fetch it automatically
    start_d = date.fromisoformat(body.start)
    end_d = date.fromisoformat(body.end)
    for code in body.codes:
        bars = provider.get_daily_bars(code, start_d, end_d)
        if len(bars) == 0:
            logger.info(f"No data for {code}, auto-syncing...")
            try:
                adapter = AkShareAdapter(db)
                adapter.sync_daily_quotes(code, start=start_d, end=end_d)
            except Exception as e:
                logger.warning(f"Auto-sync failed for {code}: {e}")

    # Find strategy by name
    strategy = None
    strategies_dir = request.app.state.strategies_dir
    if strategies_dir:
        loader = StrategyLoader()
        strategies = loader.load_directory(strategies_dir)
        for s in strategies:
            if s.name == body.strategy_name:
                strategy = s
                break

    if strategy is None:
        # Fallback: import built-in strategies
        from strategies.bollinger_band import BollingerBandStrategy
        from strategies.ma_cross import MACrossStrategy
        from strategies.ma_volume import MAVolumeStrategy
        from strategies.macd_cross import MACDCrossStrategy
        from strategies.rsi_ob_os import RSIOverboughtOversoldStrategy
        from strategies.turtle_breakout import TurtleBreakoutStrategy

        builtin = {
            "ma_cross": MACrossStrategy,
            "macd_cross": MACDCrossStrategy,
            "rsi_ob_os": RSIOverboughtOversoldStrategy,
            "bollinger_band": BollingerBandStrategy,
            "ma_volume": MAVolumeStrategy,
            "turtle_breakout": TurtleBreakoutStrategy,
        }
        cls = builtin.get(body.strategy_name)
        if cls:
            strategy = cls()

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
