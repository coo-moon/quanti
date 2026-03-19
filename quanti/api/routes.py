"""API route definitions."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from pydantic import BaseModel

from quanti.backtest.engine import BacktestEngine
from quanti.models import Direction
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
        # Auto-register stock if not in db
        stock = db.get_stock(code)
        if stock is None:
            exchange = "SH" if code.startswith("6") else "SZ"
            db.upsert_stock(code, code, exchange, date(2000, 1, 1))
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
            stock = db.get_stock(code)
            if stock is None:
                exchange = "SH" if code.startswith("6") else "SZ"
                db.upsert_stock(code, code, exchange, date(2000, 1, 1))
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
        # Fallback: try to import from built-in strategies
        from strategies.ma_cross import MACrossStrategy

        if body.strategy_name == "ma_cross":
            strategy = MACrossStrategy()

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
