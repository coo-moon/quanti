"""Tests for the PaperBroker (live-mirror execution layer)."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.paper_broker import PaperBroker
from quanti.models import Direction, Signal
from quanti.risk.manager import RiskConfig


@pytest.fixture
def setup(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    db.initialize()
    db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=15)
    np.random.seed(7)
    prices = 10 + np.cumsum(np.random.randn(len(dates)) * 0.05)
    df = pd.DataFrame({
        "code": "000001",
        "date": [d.date() for d in dates],
        "open": prices - 0.05,
        "high": prices + 0.1,
        "low": prices - 0.1,
        "close": prices,
        "volume": np.full(len(dates), 1_000_000.0),
        "amount": prices * 1_000_000,
        "turnover": np.full(len(dates), 1.0),
    })
    db.save_daily_quotes(df)
    provider = DataProvider(db)
    broker = PaperBroker(db, provider, initial_cash=200_000)
    yield db, provider, broker
    db.close()


def test_buy_persists_position_and_trade(setup):
    db, _, broker = setup
    sig = Signal(stock_code="000001", direction=Direction.BUY, strength=0.5,
                 reason="test")
    assert broker.execute_signal(sig, "test_strategy") is True
    positions = db.list_positions()
    assert len(positions) == 1
    assert positions[0]["code"] == "000001"
    assert positions[0]["quantity"] >= 100
    assert positions[0]["quantity"] % 100 == 0
    state = db.get_portfolio_state()
    assert state is not None
    assert state["cash"] < state["initial_cash"]
    trades = db.list_trades()
    assert len(trades) == 1
    assert trades[0]["direction"] == "buy"
    orders = db.list_orders()
    assert orders[0]["status"] == "filled"


def test_t_plus_one_blocks_same_day_sell(setup):
    db, _, broker = setup
    buy = Signal(stock_code="000001", direction=Direction.BUY, strength=0.4,
                 reason="t1 buy")
    broker.execute_signal(buy, "test")
    sell = Signal(stock_code="000001", direction=Direction.SELL, strength=1.0,
                  reason="same day sell")
    assert broker.execute_signal(sell, "test") is False
    # Position still here
    assert len(db.list_positions()) == 1


def test_risk_rejects_oversize_position(setup):
    db, provider, _ = setup
    # 0.1% per-stock cap → room below a 100-share lot → rejected by sizing.
    tight = RiskConfig(max_position_pct=0.001)
    broker = PaperBroker(db, provider, initial_cash=200_000, risk_config=tight)
    sig = Signal(stock_code="000001", direction=Direction.BUY,
                 strength=0.9, reason="oversized")
    assert broker.execute_signal(sig, "test") is False
    rejected_orders = [o for o in db.list_orders() if o["status"] == "rejected"]
    assert len(rejected_orders) == 1
    decisions = db.list_decisions(kind="risk_reject")
    assert len(decisions) == 1


def test_snapshot_records_history(setup):
    db, _, broker = setup
    broker.execute_signal(
        Signal(stock_code="000001", direction=Direction.BUY,
               strength=0.4, reason="snap"),
        "test",
    )
    snap = broker.snapshot_portfolio()
    assert snap["total_value"] > 0
    assert any(p["code"] == "000001" for p in snap["positions"])
    snaps = db.get_portfolio_snapshots()
    assert len(snaps) >= 1


def test_paper_broker_satisfies_broker_protocol(setup):
    """PaperBroker must structurally implement the Broker interface the
    runtime depends on — guards against drift when QmtBroker is added."""
    from quanti.execution.base import Broker
    _, _, broker = setup
    assert isinstance(broker, Broker)
    for m in ("execute_signal", "execute_signals", "try_fill_pending_orders",
              "check_exits", "snapshot_portfolio", "pending_orders_detail"):
        assert callable(getattr(broker, m)), f"missing {m}"


def test_industry_cap_limits_same_industry_buy(tmp_path):
    """A buy in a sector already near the 30% industry cap is limited by the
    *industry* room, not the (looser) 10% single-stock cap — proving the
    industry concentration limit is actually enforced at sizing."""
    from quanti.risk.manager import RiskConfig

    db = Database(str(tmp_path / "ind.db"))
    db.initialize()
    db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
    db.upsert_stock("600000", "浦发银行", "SH", date(1999, 11, 10), "银行")
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=10)
    for code in ("000001", "600000"):
        px = np.full(len(dates), 10.0)
        db.save_daily_quotes(pd.DataFrame({
            "code": code, "date": [d.date() for d in dates],
            "open": px, "high": px, "low": px, "close": px,
            "volume": np.full(len(dates), 1e6), "amount": np.full(len(dates), 1e7),
            "turnover": np.ones(len(dates))}))
    provider = DataProvider(db)
    broker = PaperBroker(db, provider, initial_cash=100_000, fill_mode="immediate",
                         risk_config=RiskConfig(max_position_pct=0.10,
                                                max_industry_pct=0.30))
    # Seed 28% already in 银行 (2800 sh @ 10 = 28k; cash 72k → total 100k).
    db.update_cash(72_000.0)
    db.upsert_position("000001", 2800, 10.0, 10.0, date(2020, 1, 1))

    # Buy another 银行 name: industry room = 30% - 28% = 2% (~2k), far under the
    # 10% single-stock cap (~10k) — so the fill is industry-limited.
    broker.execute_signal(Signal("600000", Direction.BUY, 1.0, "x"), "s")
    pos = {p["code"]: p for p in db.list_positions()}
    assert "600000" in pos
    assert pos["600000"]["quantity"] * 10.0 <= 2_000 * 1.05  # industry-capped


def _seed_drawdown_env(tmp_path, name, mark_price):
    db = Database(str(tmp_path / name))
    db.initialize()
    db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=8)
    px = np.full(len(dates), mark_price)
    db.save_daily_quotes(pd.DataFrame({
        "code": "000001", "date": [d.date() for d in dates],
        "open": px, "high": px, "low": px, "close": px,
        "volume": np.full(len(dates), 1e6),
        "amount": np.full(len(dates), mark_price * 1e6),
        "turnover": np.ones(len(dates))}))
    return db


def test_portfolio_stop_flattens_on_drawdown(tmp_path):
    """>15% drawdown from the equity high-water mark trips the circuit breaker:
    positions are flattened and it returns True (runtime then halts)."""
    from quanti.risk.manager import RiskConfig
    db = _seed_drawdown_env(tmp_path, "ps.db", 7.5)  # position marks to 7.5
    provider = DataProvider(db)
    broker = PaperBroker(db, provider, initial_cash=100_000, fill_mode="immediate",
                         risk_config=RiskConfig(portfolio_stop_loss_pct=-0.15))
    db.save_portfolio_snapshot(date.today(), 100_000.0, 0.0, 100_000.0)  # peak
    db.update_cash(20_000.0)
    db.upsert_position("000001", 8000, 10.0, 7.5, date(2020, 1, 1))  # mv 60k
    # total = 20k + 60k = 80k vs peak 100k → -20% → fire.
    assert broker.enforce_portfolio_stop() is True
    assert db.list_positions() == []


def test_portfolio_stop_fires_on_same_day_peak_then_drop(tmp_path):
    """Same-day top-then-drop must still trip the breaker. The peak is recorded
    via the REAL snapshot_portfolio() API (today's row), then equity drops and
    enforce runs. Regression for the INSERT-OR-REPLACE overwrite that deflated
    the high-water mark when peak and trough fell on the same calendar day
    (audit G3/L2): enforce must read the peak BEFORE overwriting today's row."""
    from quanti.risk.manager import RiskConfig
    db = _seed_drawdown_env(tmp_path, "ps_same_day.db", 10.0)  # marks to 10.0
    provider = DataProvider(db)
    broker = PaperBroker(db, provider, initial_cash=100_000, fill_mode="immediate",
                         risk_config=RiskConfig(portfolio_stop_loss_pct=-0.15))
    # Establish today's high-water mark via the normal API: 40k cash + 60k mv.
    db.update_cash(40_000.0)
    db.upsert_position("000001", 6000, 10.0, 10.0, date(2020, 1, 1))  # mv 60k
    peak_snap = broker.snapshot_portfolio()
    assert peak_snap["total_value"] == pytest.approx(100_000.0)

    # Same calendar day: book a loss to cash → equity 82k = -18% from peak.
    db.update_cash(22_000.0)
    assert broker.enforce_portfolio_stop() is True
    assert db.list_positions() == []  # flattened
    assert any(d["kind"] == "portfolio_stop"
               for d in db.list_decisions(limit=10))


def test_portfolio_stop_holds_within_tolerance(tmp_path):
    from quanti.risk.manager import RiskConfig
    db = _seed_drawdown_env(tmp_path, "ps2.db", 9.8)
    provider = DataProvider(db)
    broker = PaperBroker(db, provider, initial_cash=100_000, fill_mode="immediate",
                         risk_config=RiskConfig(portfolio_stop_loss_pct=-0.15))
    db.save_portfolio_snapshot(date.today(), 100_000.0, 0.0, 100_000.0)
    db.update_cash(5_000.0)
    db.upsert_position("000001", 9000, 10.0, 9.8, date(2020, 1, 1))  # mv 88.2k
    # total = 5k + 88.2k = 93.2k vs peak 100k → -6.8% → hold.
    assert broker.enforce_portfolio_stop() is False
    assert len(db.list_positions()) == 1


def test_protection_blocks_buy_after_stop_loss_cluster(tmp_path):
    from datetime import date, datetime, timedelta
    from quanti.data.database import Database
    from quanti.data.provider import DataProvider
    from quanti.models import Direction, Signal
    from quanti.execution.paper_broker import PaperBroker
    from quanti.risk.protections import ProtectionConfig

    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    provider = DataProvider(db)
    # Seed 3 stop-loss exits in the last few days → StoplossGuard locks BUYs.
    today = date.today()

    def iso(d):
        return datetime(d.year, d.month, d.day, 15, 0).isoformat()

    for i, c in enumerate(["000001", "000002", "000003"]):
        d = today - timedelta(days=i + 1)
        db.insert_order({
            "order_id": f"o{i}", "code": c, "direction": "sell",
            "quantity": 100, "price_type": "market", "limit_price": 0.0,
            "status": "filled", "strategy_name": "risk_exit",
            "filled_price": 9.0, "filled_quantity": 100,
            "reason": "止损 -10% ≤ -8%", "created_at": iso(d),
            "filled_at": iso(d), "entry_strategy": "",
        })
    broker = PaperBroker(db, provider, initial_cash=200_000,
                         fill_mode="immediate",
                         protection_config=ProtectionConfig(
                             sg_lookback_days=10, sg_trade_limit=3,
                             sg_lock_days=10, max_drawdown_enabled=False))
    ok, reason, kind = broker._entry_allowed(
        Signal("600519", Direction.BUY, 1.0, "test buy"),
        broker._build_runtime_portfolio())
    assert ok is False and kind == "protection_block"
    assert "StoplossGuard" in reason
    # A SELL is never protection-blocked.
    ok2, _, _ = broker._entry_allowed(
        Signal("600519", Direction.SELL, 1.0, "test sell"),
        broker._build_runtime_portfolio())
    assert ok2 is True


def test_execute_signals_reports_reject_reasons(tmp_path):
    """A gated BUY surfaces its reason in BrokerResult.reasons (parity with
    QmtBroker), so the caller can report WHY it was turned away — not just a
    bare rejected count. Regression: PaperBroker used to leave reasons empty."""
    from datetime import datetime, timedelta
    from quanti.risk.protections import ProtectionConfig

    db = Database(str(tmp_path / "rr.db"))
    db.initialize()
    provider = DataProvider(db)
    today = date.today()

    def iso(d):
        return datetime(d.year, d.month, d.day, 15, 0).isoformat()

    # 3 stop-loss exits in the window → StoplossGuard locks all new BUYs.
    for i, c in enumerate(["000001", "000002", "000003"]):
        d = today - timedelta(days=i + 1)
        db.insert_order({
            "order_id": f"o{i}", "code": c, "direction": "sell",
            "quantity": 100, "price_type": "market", "limit_price": 0.0,
            "status": "filled", "strategy_name": "risk_exit",
            "filled_price": 9.0, "filled_quantity": 100,
            "reason": "止损", "created_at": iso(d), "filled_at": iso(d),
            "entry_strategy": "",
        })
    broker = PaperBroker(db, provider, initial_cash=200_000,
                         fill_mode="pending",
                         protection_config=ProtectionConfig(
                             sg_lookback_days=10, sg_trade_limit=3,
                             sg_lock_days=10, max_drawdown_enabled=False))
    result = broker.execute_signals(
        [Signal("600519", Direction.BUY, 1.0, "buy")], "llm")
    assert result.rejected == 1
    assert result.filled == 0 and result.pending == 0
    assert result.reasons and any("StoplossGuard" in r for r in result.reasons)


def test_volume_cap_limits_immediate_buy(tmp_path):
    """B1: PaperBroker's fill is capped at 25% of the bar's turnover too, so
    backtest and paper agree on capacity (not just the backtest)."""
    db = Database(str(tmp_path / "vc.db"))
    db.initialize()
    db.upsert_stock("000001", "x", "SZ", date(1991, 4, 3), "银行")
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=10)
    df = pd.DataFrame({
        "code": "000001", "date": [d.date() for d in dates],
        "open": 10.0, "high": 10.01, "low": 9.99, "close": 10.0,
        "volume": 1e4, "amount": 100_000.0, "turnover": 1.0})  # thin: cap=2500
    db.save_daily_quotes(df)
    provider = DataProvider(db)
    broker = PaperBroker(db, provider, initial_cash=1_000_000,
                         fill_mode="immediate", slippage=0.0)
    assert broker.execute_signal(
        Signal("000001", Direction.BUY, 1.0, "buy"), "t") is True
    # Same 2500 cap as the backtest engine test → backtest≡paper on capacity.
    assert db.list_positions()[0]["quantity"] == 2500  # turnover-capped


# --- T+1 frozen-lot tracking (audit F1) ----------------------------------

def _sell(broker, code="000001"):
    return broker.execute_signal(Signal(code, Direction.SELL, 1.0, "exit"), "t")


def test_t1_blocks_selling_todays_fresh_buy(setup):
    """A position opened today is fully frozen → a same-day SELL is rejected
    and the position is untouched (the basic T+1 guard)."""
    db, _, broker = setup
    assert broker.execute_signal(
        Signal("000001", Direction.BUY, 0.5, "open"), "t") is True
    pos = db.list_positions()[0]
    assert pos["frozen_qty"] == pos["quantity"]        # whole lot frozen today
    assert _sell(broker) is False                      # T+1 blocks it
    assert db.list_positions()[0]["quantity"] == pos["quantity"]  # untouched


def test_addon_today_freezes_only_new_lot_not_old(setup):
    """THE F1 bug: a settled lot + a same-day add-on. The SELL must liquidate
    ONLY the settled shares; today's add-on stays frozen (was: whole position
    sellable because the add-on kept the old buy_date)."""
    db, _, broker = setup
    # Settled holding from long ago (frozen_qty defaults to 0 → fully sellable).
    db.upsert_position("000001", 1000, 10.0, 10.0, date(2020, 1, 1))
    # Add to it TODAY via the broker.
    assert broker.execute_signal(
        Signal("000001", Direction.BUY, 0.5, "addon"), "t") is True
    total = db.list_positions()[0]["quantity"]
    addon = total - 1000
    assert addon > 0 and db.list_positions()[0]["frozen_qty"] == addon

    assert _sell(broker) is True
    sells = [t for t in db.list_trades() if t["direction"] == "sell"]
    assert sells[0]["quantity"] == 1000               # only the settled lot
    remaining = db.list_positions()
    assert remaining and remaining[0]["quantity"] == addon   # add-on still held
    assert remaining[0]["frozen_qty"] == addon               # …and still frozen


def test_frozen_lot_settles_next_session(setup):
    """A frozen lot dated before today has settled → the whole holding sells."""
    db, _, broker = setup
    db.upsert_position("000001", 1500, 10.0, 10.0, date(2020, 1, 1),
                       frozen_qty=500, frozen_date=date(2020, 1, 2))  # past → settled
    assert _sell(broker) is True
    sells = [t for t in db.list_trades() if t["direction"] == "sell"]
    assert sells[0]["quantity"] == 1500
    assert db.list_positions() == []


# ---------------------------------------------------------- intraday marks

def _mk_broker(db, provider, quote_fn):
    # Realtime marks are pending-mode only (immediate prices fills off the
    # latest daily bar — marks there would decouple decision from fill).
    return PaperBroker(db, provider, initial_cash=200_000,
                       fill_mode="pending", realtime_quote_fn=quote_fn)


def _in_session(monkeypatch, value: bool):
    monkeypatch.setattr("quanti.utils.market.in_trading_session",
                        lambda *a, **k: value)


def test_intraday_marks_override_daily_close_in_session(setup, monkeypatch):
    db, provider, _ = setup
    db.upsert_position("000001", 1000, 10.0, 10.0, date(2020, 1, 1))
    broker = _mk_broker(db, provider, lambda codes: {"000001": 12.34})
    _in_session(monkeypatch, True)
    snap = broker.snapshot_portfolio()
    pos = snap["positions"][0]
    assert pos["current_price"] == pytest.approx(12.34)  # adj_factor 1.0
    assert pos["industry"] == "银行"   # industry surfaced for the UI
    assert pos["price_date"] == date.today().isoformat()
    assert snap["market_value"] == pytest.approx(12.34 * 1000)


def test_intraday_marks_convert_raw_quote_to_hfq_axis(setup, monkeypatch):
    """Tencent quotes are RAW; the account books are hfq — the mark must be
    raw × latest adj_factor or dividend-heavy names show phantom losses."""
    db, provider, _ = setup
    db.conn.execute("UPDATE daily_quotes SET adj_factor=1.2 WHERE code='000001'")
    db.upsert_position("000001", 1000, 12.0, 12.0, date(2020, 1, 1))
    broker = _mk_broker(db, provider, lambda codes: {"000001": 10.0})
    _in_session(monkeypatch, True)
    snap = broker.snapshot_portfolio()
    assert snap["positions"][0]["current_price"] == pytest.approx(10.0 * 1.2)


def test_intraday_marks_reject_out_of_band_quote(setup, monkeypatch):
    """One glitched print (way outside the ±35% daily-band envelope) must not
    become a mark — it could fire a stop or the circuit breaker."""
    db, provider, _ = setup
    db.upsert_position("000001", 1000, 10.0, 10.0, date(2020, 1, 1))
    broker = _mk_broker(db, provider, lambda codes: {"000001": 3.0})  # ~-70%
    _in_session(monkeypatch, True)
    snap = broker.snapshot_portfolio()
    # Falls back to the daily-close mark, not the bad tick.
    assert snap["positions"][0]["current_price"] != pytest.approx(3.0)
    assert snap["positions"][0]["current_price"] > 5.0


def test_intraday_marks_ignored_off_session(setup, monkeypatch):
    db, provider, _ = setup
    db.upsert_position("000001", 1000, 10.0, 10.0, date(2020, 1, 1))
    broker = _mk_broker(db, provider, lambda codes: {"000001": 11.0})
    _in_session(monkeypatch, False)
    snap = broker.snapshot_portfolio()
    # Off-session the quote fn must not even matter: daily-close mark.
    assert snap["positions"][0]["current_price"] != pytest.approx(11.0)


def test_intraday_marks_ignored_in_immediate_mode(setup, monkeypatch):
    """Immediate mode fills at the latest daily bar — realtime marks would
    let exits decide at today's price but fill at yesterday's close, so the
    overlay must stay off (CLI `agent tick` / MCP / backtests use this mode)."""
    db, provider, _ = setup
    db.upsert_position("000001", 1000, 10.0, 10.0, date(2020, 1, 1))
    broker = PaperBroker(db, provider, initial_cash=200_000,
                         realtime_quote_fn=lambda codes: {"000001": 11.0})
    _in_session(monkeypatch, True)
    snap = broker.snapshot_portfolio()
    assert snap["positions"][0]["current_price"] != pytest.approx(11.0)


def test_intraday_marks_fetch_failure_falls_back(setup, monkeypatch):
    db, provider, _ = setup
    db.upsert_position("000001", 1000, 10.0, 10.0, date(2020, 1, 1))

    def boom(codes):
        raise OSError("qt.gtimg.cn unreachable")

    broker = _mk_broker(db, provider, boom)
    _in_session(monkeypatch, True)
    snap = broker.snapshot_portfolio()  # must not raise
    assert snap["positions"][0]["current_price"] > 0


def test_intraday_snapshot_not_persisted(setup, monkeypatch):
    """Realtime-marked snapshots are transient: persisting them would let an
    intraday spike inflate the circuit breaker's high-water mark forever."""
    db, provider, _ = setup
    db.upsert_position("000001", 1000, 10.0, 10.0, date(2020, 1, 1))
    broker = _mk_broker(db, provider, lambda codes: {"000001": 11.0})
    _in_session(monkeypatch, True)
    broker.snapshot_portfolio()
    assert db.get_portfolio_snapshots() == []      # in-session: not persisted
    _in_session(monkeypatch, False)
    broker.snapshot_portfolio()
    assert len(db.get_portfolio_snapshots()) == 1  # close-marked: persisted


def test_intraday_crash_triggers_stop_via_check_exits(setup, monkeypatch):
    """The point of the paper intraday guard: a stop hit at TODAY's realtime
    price exits TODAY at that price (live-mirror market sell), instead of
    waiting for today's bar to land and filling at tomorrow's open."""
    from datetime import timedelta as _td
    db, provider, _ = setup
    db.upsert_position("000001", 1000, 10.0, 10.0, date(2020, 1, 1))
    # -8% vs yesterday's close: through the ~-4% ATR stop (k=2, ATR≈2% on the
    # fixture walk) but above the -10% main-board limit-down, so the market
    # sell is actually fillable — a realistic single-day stop hit.
    raw_close, _f = db.get_latest_quote_before("000001", date.today() + _td(days=1))
    crash = raw_close * 0.92
    broker = _mk_broker(db, provider, lambda codes: {"000001": crash})
    _in_session(monkeypatch, True)
    assert broker.check_exits() >= 1
    sells = [t for t in db.list_trades() if t["direction"] == "sell"]
    assert sells and sells[0]["price"] == pytest.approx(crash * (1 - 0.001))
    assert db.list_positions() == []
    assert db.list_orders(limit=10, status="pending") == []


# ------------------------------------ llm_full 盘中实时买入 (live-mirror BUY)

def test_llm_managed_buy_fills_now_in_session(setup, monkeypatch):
    """llm_full 盘中 tick 的买单:盘中有实时报价且未涨停锁板 → 立即按
    实时价(+滑点)成交,不排队等次日开盘。"""
    from datetime import timedelta as _td
    db, provider, _ = setup
    raw_close, _f = db.get_latest_quote_before("000001", date.today() + _td(days=1))
    quote = raw_close * 1.02  # +2%:盘中正常波动,未触 +10% 涨停
    broker = _mk_broker(db, provider, lambda codes: {"000001": quote})
    broker.llm_managed = True
    _in_session(monkeypatch, True)
    status, reason = broker._submit_one(
        Signal(stock_code="000001", direction=Direction.BUY, strength=0.2,
               reason="LLM 盘中买入"), "llm_full")
    assert status == "filled", reason
    pos = {p["code"]: p for p in db.list_positions()}
    assert pos["000001"]["quantity"] >= 100
    assert pos["000001"]["avg_cost"] == pytest.approx(quote * 1.001, rel=1e-4)


def test_llm_managed_buy_slippage_capped_at_half_percent(setup, monkeypatch):
    """构造时 slippage=1% 被硬钳到 0.5%:即时成交价 = 实时价 × 1.005,
    纸面账户永远不会记出超过半个点的滑点(契约由 MAX_FILL_SLIPPAGE 单点强制)。"""
    from datetime import timedelta as _td
    db, provider, _ = setup
    raw_close, _f = db.get_latest_quote_before("000001", date.today() + _td(days=1))
    quote = raw_close * 1.02
    broker = PaperBroker(db, provider, initial_cash=200_000,
                         fill_mode="pending", slippage=0.01,
                         realtime_quote_fn=lambda codes: {"000001": quote})
    assert broker._slippage == 0.005
    broker.llm_managed = True
    _in_session(monkeypatch, True)
    status, reason = broker._submit_one(
        Signal(stock_code="000001", direction=Direction.BUY, strength=0.2,
               reason="test"), "llm_full")
    assert status == "filled", reason
    assert db.list_positions()[0]["avg_cost"] == pytest.approx(
        quote * 1.005, rel=1e-4)


def test_llm_managed_buy_falls_back_to_queue_without_quote(setup, monkeypatch):
    """无实时报价(盘外/源故障/停牌)→ 照旧排队次日开盘成交——腾讯源
    无 SLA,即时路径的回退必须保留,不能把订单丢掉。"""
    db, provider, _ = setup
    broker = _mk_broker(db, provider, lambda codes: {})  # 无报价
    broker.llm_managed = True
    _in_session(monkeypatch, True)
    status, _reason = broker._submit_one(
        Signal(stock_code="000001", direction=Direction.BUY, strength=0.2,
               reason="test"), "llm_full")
    assert status == "pending"
    assert db.list_positions() == []


def test_llm_managed_buy_at_limit_up_queues(setup, monkeypatch):
    """涨停锁板(实时价已顶到 +10%)买不进——保持挂单而不是假装成交,
    锁板期间按 TTL 每个交易日重试。"""
    from datetime import timedelta as _td
    db, provider, _ = setup
    raw_close, _f = db.get_latest_quote_before("000001", date.today() + _td(days=1))
    quote = raw_close * 1.10  # 顶板
    broker = _mk_broker(db, provider, lambda codes: {"000001": quote})
    broker.llm_managed = True
    _in_session(monkeypatch, True)
    status, _reason = broker._submit_one(
        Signal(stock_code="000001", direction=Direction.BUY, strength=0.2,
               reason="test"), "llm_full")
    assert status == "pending"
    assert db.list_positions() == []


def test_no_quote_fn_keeps_legacy_marks(setup):
    """Default construction (tests/backtests) never touches realtime marks."""
    db, _, broker = setup
    db.upsert_position("000001", 1000, 10.0, 10.0, date(2020, 1, 1))
    snap = broker.snapshot_portfolio()
    assert snap["positions"][0]["current_price"] > 0


# ------------------------------------------------- live-mirror market sells

def test_in_session_sell_fills_now_at_realtime_price(setup, monkeypatch):
    """Pending mode + in-session quote: a SELL fills immediately at the
    realtime mark, exactly like a live market sell via xtdata."""
    db, provider, _ = setup
    db.upsert_position("000001", 1000, 10.0, 10.0, date(2020, 1, 1))
    broker = _mk_broker(db, provider, lambda codes: {"000001": 10.8})
    _in_session(monkeypatch, True)
    assert broker.execute_signal_ex(
        Signal("000001", Direction.SELL, 1.0, "exit"), "t") == "filled"
    assert db.list_positions() == []
    sells = [t for t in db.list_trades() if t["direction"] == "sell"]
    assert sells and sells[0]["price"] == pytest.approx(10.8 * (1 - 0.001))


def test_off_session_sell_queues_as_before(setup, monkeypatch):
    db, provider, _ = setup
    db.upsert_position("000001", 1000, 10.0, 10.0, date(2020, 1, 1))
    broker = _mk_broker(db, provider, lambda codes: {"000001": 10.8})
    _in_session(monkeypatch, False)
    assert broker.execute_signal_ex(
        Signal("000001", Direction.SELL, 1.0, "exit"), "t") == "pending"
    assert db.list_positions() != []
    assert db.list_orders(limit=10, status="pending")


def test_in_session_buy_still_queues(setup, monkeypatch):
    """Live-mirror is sells-only: BUYs keep the next-open queue semantics."""
    db, provider, _ = setup
    broker = _mk_broker(db, provider, lambda codes: {"000001": 10.8})
    _in_session(monkeypatch, True)
    assert broker.execute_signal_ex(
        Signal("000001", Direction.BUY, 0.5, "entry"), "t") == "pending"
    assert db.list_positions() == []


def test_limit_down_locked_sell_falls_back_to_queue(setup, monkeypatch):
    """Into a limit-down lock a market sell can't fill — the order rests in
    the queue (like at a live venue) instead of being rejected."""
    from datetime import timedelta as _td
    db, provider, _ = setup
    db.upsert_position("000001", 1000, 10.0, 10.0, date(2020, 1, 1))
    raw_close, _f = db.get_latest_quote_before("000001", date.today() + _td(days=1))
    locked = raw_close * 0.90  # main board -10% = limit-down
    broker = _mk_broker(db, provider, lambda codes: {"000001": locked})
    _in_session(monkeypatch, True)
    assert broker.execute_signal_ex(
        Signal("000001", Direction.SELL, 1.0, "exit"), "t") == "pending"
    assert db.list_positions() != []


def test_t1_frozen_sell_falls_back_to_queue(setup, monkeypatch):
    """A holding entirely bought today can't sell now (T+1) — queue it so it
    still fills at tomorrow's open instead of being rejected outright."""
    db, provider, _ = setup
    db.upsert_position("000001", 1000, 10.0, 10.0, date.today(),
                       frozen_qty=1000, frozen_date=date.today())
    broker = _mk_broker(db, provider, lambda codes: {"000001": 10.8})
    _in_session(monkeypatch, True)
    assert broker.execute_signal_ex(
        Signal("000001", Direction.SELL, 1.0, "exit"), "t") == "pending"
    assert db.list_positions() != []


def test_market_sell_supersedes_queued_sell(setup, monkeypatch):
    """An overnight queued SELL is cancelled when the in-session market sell
    fills — no double-fill tomorrow."""
    db, provider, _ = setup
    db.upsert_position("000001", 1000, 10.0, 10.0, date(2020, 1, 1))
    broker = _mk_broker(db, provider, lambda codes: {"000001": 10.5})
    _in_session(monkeypatch, False)
    assert broker.execute_signal_ex(
        Signal("000001", Direction.SELL, 1.0, "queued exit"), "t") == "pending"
    _in_session(monkeypatch, True)
    assert broker.execute_signal_ex(
        Signal("000001", Direction.SELL, 1.0, "exit now"), "t") == "filled"
    assert db.list_orders(limit=10, status="pending") == []
    assert db.list_positions() == []


def test_partial_t1_full_exit_fills_settled_and_queues_frozen_rest(setup, monkeypatch):
    """Settled 1000 + 400 frozen today, full exit in-session: the settled
    shares fill now at the mark; the frozen remainder is re-queued so the
    exit intent still lands at the next open (never silently stranded)."""
    db, provider, _ = setup
    db.upsert_position("000001", 1400, 10.0, 10.0, date(2020, 1, 1),
                       frozen_qty=400, frozen_date=date.today())
    broker = _mk_broker(db, provider, lambda codes: {"000001": 10.8})
    _in_session(monkeypatch, True)
    assert broker.execute_signal_ex(
        Signal("000001", Direction.SELL, 1.0, "exit"), "t") == "filled"
    sells = [t for t in db.list_trades() if t["direction"] == "sell"]
    assert sells and sells[0]["quantity"] == 1000
    assert db.list_positions()[0]["quantity"] == 400
    pending = db.list_orders(limit=10, status="pending")
    assert [o for o in pending if o["direction"] == "sell"]


def test_trim_partial_sell_leaves_resting_full_exit_alone(setup, monkeypatch):
    """A 削峰 trim is a partial-intent sell: it must NOT cancel a resting
    full-exit order (which still owns the remainder's exit tomorrow)."""
    from quanti.risk.manager import DRIFT_TRIM_STRATEGY
    db, provider, _ = setup
    db.upsert_position("000001", 1000, 10.0, 10.0, date(2020, 1, 1))
    broker = _mk_broker(db, provider, lambda codes: {"000001": 10.5})
    _in_session(monkeypatch, False)
    assert broker.execute_signal_ex(
        Signal("000001", Direction.SELL, 1.0, "full exit"), "t") == "pending"
    _in_session(monkeypatch, True)
    assert broker.execute_signal_ex(
        Signal("000001", Direction.SELL, 0.5, "trim"),
        DRIFT_TRIM_STRATEGY) == "filled"
    # Trim sold half; the resting full-exit order survives for the rest.
    assert db.list_positions()[0]["quantity"] == 500
    assert [o for o in db.list_orders(limit=10, status="pending")
            if o["direction"] == "sell"]


def test_sublot_trim_reject_preserves_resting_order(setup, monkeypatch):
    """A trim that lot-rounds to zero rejects WITHOUT destroying the resting
    full-exit order (the old code cancelled first, then rejected)."""
    from quanti.risk.manager import DRIFT_TRIM_STRATEGY
    db, provider, _ = setup
    db.upsert_position("000001", 100, 10.0, 10.0, date(2020, 1, 1))
    broker = _mk_broker(db, provider, lambda codes: {"000001": 10.5})
    _in_session(monkeypatch, False)
    assert broker.execute_signal_ex(
        Signal("000001", Direction.SELL, 1.0, "full exit"), "t") == "pending"
    _in_session(monkeypatch, True)
    assert broker.execute_signal_ex(
        Signal("000001", Direction.SELL, 0.3, "trim"),
        DRIFT_TRIM_STRATEGY) == "rejected"
    assert [o for o in db.list_orders(limit=10, status="pending")
            if o["direction"] == "sell"]
    assert db.list_positions()[0]["quantity"] == 100


def test_market_sell_respects_b1_participation_cap(setup, monkeypatch):
    """In-session fills honor the B1 single-bar 25% participation cap
    (yesterday's turnover as proxy); the un-filled remainder re-queues."""
    db, provider, _ = setup
    db.upsert_position("000001", 300_000, 10.0, 10.0, date(2020, 1, 1))
    broker = _mk_broker(db, provider, lambda codes: {"000001": 10.8})
    _in_session(monkeypatch, True)
    assert broker.execute_signal_ex(
        Signal("000001", Direction.SELL, 1.0, "exit"), "t") == "filled"
    sells = [t for t in db.list_trades() if t["direction"] == "sell"]
    qty = sells[0]["quantity"]
    remaining = db.list_positions()[0]["quantity"]
    assert 0 < qty < 300_000 and qty + remaining == 300_000
    assert [o for o in db.list_orders(limit=10, status="pending")
            if o["direction"] == "sell"]


def test_st_stock_uses_5pct_limit_down_band(setup, monkeypatch):
    """ST names lock at -5%: a -6% quote must fall back to the queue even
    though the main-board band (-10%) would have let it fill."""
    from datetime import timedelta as _td
    db, provider, _ = setup
    db.upsert_stock("000001", "ST平安", "SZ", date(1991, 4, 3), "银行")
    db.upsert_position("000001", 1000, 10.0, 10.0, date(2020, 1, 1))
    raw_close, _f = db.get_latest_quote_before("000001", date.today() + _td(days=1))
    broker = _mk_broker(db, provider,
                        lambda codes: {"000001": raw_close * 0.94})
    _in_session(monkeypatch, True)
    assert broker.execute_signal_ex(
        Signal("000001", Direction.SELL, 1.0, "exit"), "t") == "pending"
    assert db.list_positions()[0]["quantity"] == 1000


# ------------------------------------- llm_managed 挂单转盘中实时成交


def test_llm_pending_buy_converts_when_session_resumes(setup, monkeypatch):
    """午休下的单(排队时无实时报价)在下午开盘守护扫 pending 时按实时价
    补成交:老行结转 cancelled、新 filled 行落地、建仓成功。"""
    from datetime import timedelta as _td
    db, provider, _ = setup
    raw_close, _f = db.get_latest_quote_before("000001", date.today() + _td(days=1))
    quote = {"px": 0.0}  # 0 = 午休无报价
    broker = _mk_broker(db, provider,
                        lambda codes: ({"000001": quote["px"]} if quote["px"] else {}))
    broker.llm_managed = True
    _in_session(monkeypatch, True)
    status, _r = broker._submit_one(
        Signal(stock_code="000001", direction=Direction.BUY, strength=0.2,
               reason="LLM 午休买入"), "llm_full")
    assert status == "pending"  # 无报价 → 排队(问题现场)

    quote["px"] = raw_close * 1.02  # 下午开盘,报价恢复
    result = broker.try_fill_pending_orders()
    assert result.filled == 1
    pos = {p["code"]: p for p in db.list_positions()}
    assert pos["000001"]["quantity"] >= 100
    assert pos["000001"]["avg_cost"] == pytest.approx(quote["px"] * 1.001, rel=1e-4)
    by_status = {}
    for o in db.list_orders(limit=10):
        by_status.setdefault(o["status"], []).append(o)
    assert "转盘中实时成交" in by_status["cancelled"][0]["reason"]
    assert by_status["filled"][0]["entry_strategy"] == \
        by_status["cancelled"][0]["entry_strategy"]


def test_non_llm_pending_buy_stays_queued(setup, monkeypatch):
    """非 llm_managed:同样场景零行为变化,老老实实等次日开盘。"""
    from datetime import timedelta as _td
    db, provider, _ = setup
    raw_close, _f = db.get_latest_quote_before("000001", date.today() + _td(days=1))
    broker = _mk_broker(db, provider, lambda codes: {"000001": raw_close * 1.02})
    _in_session(monkeypatch, True)
    status, _r = broker._queue_pending_signal(
        Signal(stock_code="000001", direction=Direction.BUY, strength=0.2,
               reason="规则买入"), "ma_cross")
    assert status is True
    result = broker.try_fill_pending_orders()
    assert result.filled == 0
    assert db.list_positions() == []


def test_llm_pending_convert_reject_cancels(setup, monkeypatch):
    """转成交尝试走到 _buy_now 却被拒(极端高开熔断)→ 挂单直接取消,
    不留到次日开盘再拒一遍(用户拍板 2026-08-20:llm_full 每日重新决策,
    被拒的旧意图是僵尸挂单)。"""
    from datetime import timedelta as _td
    db, provider, _ = setup
    raw_close, _f = db.get_latest_quote_before("000001", date.today() + _td(days=1))
    quote = {"px": 0.0}
    broker = _mk_broker(db, provider,
                        lambda codes: ({"000001": quote["px"]} if quote["px"] else {}))
    broker.llm_managed = True
    _in_session(monkeypatch, True)
    status, _r = broker._submit_one(
        Signal(stock_code="000001", direction=Direction.BUY, strength=0.2,
               reason="test"), "llm_full")
    assert status == "pending"

    quote["px"] = raw_close * 1.08  # +8%,压低熔断阈确保触发
    broker._risk.config.extreme_gap_up_block_pct = 0.05
    result = broker.try_fill_pending_orders()
    assert result.rejected == 1
    by_status = {}
    for o in db.list_orders(limit=20):
        by_status.setdefault(o["status"], []).append(o)
    assert "pending" not in by_status            # 不留僵尸挂单
    cancelled = [o for o in by_status["cancelled"]
                 if "转单被拒后取消" in o["reason"]]
    assert cancelled                              # 取消行带原因
    assert by_status.get("rejected")              # _buy_now 的拒单行留审计
    assert db.list_positions() == []

    # 第二轮守护:队列已空,不再产生新行
    n_before = sum(len(v) for v in by_status.values())
    broker.try_fill_pending_orders()
    assert len(db.list_orders(limit=20)) == n_before


def test_llm_pending_no_quote_keeps_waiting(setup, monkeypatch):
    """无报价/锁板不是拒:挂单保留,不取消——等报价恢复或次日开盘。"""
    db, provider, _ = setup
    broker = _mk_broker(db, provider, lambda codes: {})
    broker.llm_managed = True
    _in_session(monkeypatch, True)
    broker._submit_one(
        Signal(stock_code="000001", direction=Direction.BUY, strength=0.2,
               reason="test"), "llm_full")
    broker.try_fill_pending_orders()
    assert [o for o in db.list_orders(limit=10) if o["status"] == "pending"]
