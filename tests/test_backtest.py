"""Tests for backtesting engine."""

from datetime import date

import pandas as pd
import numpy as np
import pytest

from quanti.backtest.commission import AShareCommission
from quanti.backtest.metrics import compute_metrics
from quanti.backtest.engine import BacktestEngine
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.models import Direction, Signal
from quanti.strategy.base import BaseStrategy


class TestAShareCommission:
    def test_buy_commission(self):
        comm = AShareCommission()
        cost = comm.calculate(price=10.0, quantity=1000, direction=Direction.BUY)
        # 佣金 = 10 * 1000 * 0.00025 = 2.5, min 5 => 5.0
        # 过户费 = 10 * 1000 * 0.00001 = 0.1
        assert cost == pytest.approx(5.1)

    def test_sell_commission_current_rate(self):
        comm = AShareCommission()
        # No date → current (post-2023-08-28) 万5 stamp.
        cost = comm.calculate(price=10.0, quantity=1000, direction=Direction.SELL)
        # 佣金 max(5, 2.5)=5.0 + 印花税 10*1000*0.0005=5.0 + 过户费 0.1
        assert cost == pytest.approx(10.1)

    def test_sell_stamp_duty_is_date_aware(self):
        from datetime import date
        comm = AShareCommission()
        # Before the 2023-08-28 halving: 千1 stamp → total 15.1.
        pre = comm.calculate(10.0, 1000, Direction.SELL, trade_date=date(2023, 8, 27))
        assert pre == pytest.approx(15.1)
        # On/after: 万5 stamp → total 10.1.
        post = comm.calculate(10.0, 1000, Direction.SELL, trade_date=date(2023, 8, 28))
        assert post == pytest.approx(10.1)
        # Buys never carry stamp duty, regardless of date.
        assert comm.calculate(10.0, 1000, Direction.BUY,
                              trade_date=date(2023, 8, 27)) == pytest.approx(5.1)

    def test_min_commission_floor_on_tiny_trade(self):
        comm = AShareCommission()
        # 佣金 raw = 1*100*0.00025 = 0.025 → floored to 5.0; +过户费 0.001
        assert comm.calculate(1.0, 100, Direction.BUY) == pytest.approx(5.001)


class TestMetrics:
    def test_compute_metrics(self):
        # Simulate a simple equity curve
        np.random.seed(42)
        returns = np.random.randn(252) * 0.01
        equity = 100_000 * (1 + pd.Series(returns)).cumprod()
        metrics = compute_metrics(equity, risk_free_rate=0.03)
        assert "total_return" in metrics
        assert "annual_return" in metrics
        assert "max_drawdown" in metrics
        assert "sharpe_ratio" in metrics
        assert metrics["max_drawdown"] <= 0  # Drawdown is negative

    def test_short_window_not_annualized(self):
        # A ~14-bar window must NOT be extrapolated to an absurd annual figure
        # (the "933%" walk-forward bug). It reports the cumulative return.
        eq = pd.Series([100.0 * (1.05) ** (i / 13) for i in range(14)])  # +5%
        m = compute_metrics(eq)
        assert m["annual_return"] == pytest.approx(m["total_return"])
        assert m["annual_return"] < 0.20

    def test_long_window_is_annualized(self):
        # A full year (+20% total) annualizes to ~+20%.
        eq = pd.Series([100.0 * (1.20) ** (i / 251) for i in range(252)])
        m = compute_metrics(eq)
        assert m["annual_return"] == pytest.approx(0.20, rel=0.1)


class AlwaysBuyStrategy(BaseStrategy):
    """Buys on first bar, for testing."""

    name = "always_buy"

    def init(self, config):
        self._bought = set()

    def on_bar(self, bar):
        if bar.code not in self._bought:
            self._bought.add(bar.code)
            return [
                Signal(
                    stock_code=bar.code,
                    direction=Direction.BUY,
                    strength=1.0,
                    reason="test buy",
                )
            ]
        return []


class TestBacktestEngine:
    @pytest.fixture
    def setup(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        db.initialize()

        # Insert 30 days of synthetic data
        dates = pd.bdate_range("2024-01-02", periods=30)
        np.random.seed(42)
        prices = 10 + np.cumsum(np.random.randn(30) * 0.1)
        df = pd.DataFrame(
            {
                "code": "000001",
                "date": [d.date() for d in dates],
                "open": prices - 0.1,
                "high": prices + 0.3,
                "low": prices - 0.3,
                "close": prices,
                "volume": np.random.randint(500000, 2000000, 30).astype(float),
                "amount": prices * 1_000_000,
                "turnover": np.random.rand(30) * 3,
            }
        )
        db.save_daily_quotes(df)
        db.save_trade_calendar([d.date() for d in dates])
        provider = DataProvider(db)
        yield db, provider
        db.close()

    def test_backtest_runs(self, setup):
        db, provider = setup
        strategy = AlwaysBuyStrategy()
        strategy.init({})
        engine = BacktestEngine(
            provider=provider,
            initial_cash=100_000.0,
        )
        result = engine.run(
            strategy=strategy,
            codes=["000001"],
            start=date(2024, 1, 1),
            end=date(2024, 2, 28),
        )
        assert result.equity_curve is not None
        assert len(result.equity_curve) > 0
        assert len(result.trades) >= 1  # At least the buy order
        assert result.metrics is not None


def test_backtest_risk_exit_tags_reason(tmp_path):
    """With a RiskManager, a -8% drawdown triggers a stop-loss exit, recorded
    as a `risk_exit` sell carrying the reason — so a backtest shows WHY each
    exit fired and matches the live exit policy. Without a RiskManager (the
    old default) no such exit appears."""
    from quanti.risk.manager import RiskConfig, RiskManager

    db = Database(str(tmp_path / "rx.db"))
    db.initialize()
    # 10 calm bars, a -20% crash on bar 10, then a trailing bar so the
    # stop-loss (detected at the crash close) can fill at the NEXT bar's open
    # — the engine fills at next-open, never the same close.
    dates = pd.bdate_range("2024-01-02", periods=11)
    closes = [10.0, 10.1, 10.05, 10.1, 10.0, 10.1, 10.05, 10.0, 10.1, 8.0, 8.0]
    df = pd.DataFrame({
        "code": "000001", "date": [d.date() for d in dates],
        "open": closes, "high": [c + 0.1 for c in closes],
        "low": [c - 0.2 for c in closes], "close": closes,
        "volume": 1e6, "amount": [c * 1e6 for c in closes], "turnover": 1.0,
    })
    db.save_daily_quotes(df)
    provider = DataProvider(db)

    # With risk: stop-loss must fire on the crash and be tagged risk_exit.
    s1 = AlwaysBuyStrategy()
    s1.init({})
    res = BacktestEngine(
        provider, 100_000.0,
        risk_manager=RiskManager(RiskConfig(stop_loss_pct=-0.08))
    ).run(s1, ["000001"], date(2024, 1, 1), date(2024, 2, 1))
    exits = [t for t in res.trades if t.strategy == "risk_exit"]
    assert exits, "expected a stop-loss risk_exit on the crash"
    assert any("止损" in t.reason for t in exits)

    # Without risk: no risk_exit at all (old UI/Selector behavior).
    s2 = AlwaysBuyStrategy()
    s2.init({})
    res2 = BacktestEngine(provider, 100_000.0).run(
        s2, ["000001"], date(2024, 1, 1), date(2024, 2, 1))
    assert not [t for t in res2.trades if t.strategy == "risk_exit"]
    db.close()


class BuyOnceStrategy(BaseStrategy):
    """Buys a single named code once, at a configurable strength."""

    name = "buy_once"

    def init(self, config):
        self._code = config.get("code", "000001")
        self._strength = config.get("strength", 1.0)
        self._done = False

    def on_bar(self, bar):
        if bar.code == self._code and not self._done:
            self._done = True
            return [Signal(stock_code=bar.code, direction=Direction.BUY,
                           strength=self._strength, reason="buy once")]
        return []


def _flat_provider(tmp_path, name, closes, code="000001"):
    """A single-stock provider with the given close path (open==close)."""
    db = Database(str(tmp_path / name))
    db.initialize()
    dates = pd.bdate_range("2024-01-02", periods=len(closes))
    df = pd.DataFrame({
        "code": code, "date": [d.date() for d in dates],
        "open": closes, "high": [c + 0.01 for c in closes],
        "low": [c - 0.01 for c in closes], "close": closes,
        "volume": 5e6, "amount": [c * 5e6 for c in closes], "turnover": 1.0,
    })
    db.save_daily_quotes(df)
    db.save_trade_calendar([d.date() for d in dates])
    return db, DataProvider(db), [code]


def test_backtest_buy_respects_signal_strength(tmp_path):
    """C2: a buy is sized by signal.strength (cash*0.95*clamp(strength)) just
    like the live PaperBroker no-sizer path — half-strength deploys ~half the
    capital. Previously the engine ignored strength and spent ~95% of cash."""
    from quanti.risk.manager import RiskConfig, RiskManager

    # 12 flat bars at 10.0 so price doesn't move; per-stock cap lifted to 100%
    # so cash (not the 10% cap) is the binding constraint we're measuring.
    closes = [10.0] * 12

    def risk():
        return RiskManager(RiskConfig(max_position_pct=1.0))

    db, provider, codes = _flat_provider(tmp_path, "s_full.db", closes)
    s_full = BuyOnceStrategy()
    s_full.init({"strength": 1.0})
    full = BacktestEngine(provider, 1_000_000.0, risk_manager=risk(),
                          slippage=0.0).run(s_full, codes,
                                            date(2024, 1, 1), date(2024, 2, 1))
    db.close()

    db2, provider2, codes2 = _flat_provider(tmp_path, "s_half.db", closes)
    s_half = BuyOnceStrategy()
    s_half.init({"strength": 0.5})
    half = BacktestEngine(provider2, 1_000_000.0, risk_manager=risk(),
                          slippage=0.0).run(s_half, codes2,
                                            date(2024, 1, 1), date(2024, 2, 1))
    db2.close()

    full_qty = next(t.quantity for t in full.trades if t.direction == Direction.BUY)
    half_qty = next(t.quantity for t in half.trades if t.direction == Direction.BUY)
    # strength 0.5 → ~half the shares of strength 1.0 (within one 100-lot).
    assert half_qty == pytest.approx(full_qty * 0.5, abs=100)
    # And full strength buys far more than the old hard 10000-share cap allowed.
    assert full_qty > 10_000


def test_backtest_no_10000_share_cap(tmp_path):
    """C2: the arbitrary 10000-share/trade cap is gone — a cheap stock with
    ample cash fills the full risk-capped size in one go."""
    from quanti.risk.manager import RiskConfig, RiskManager

    closes = [2.0] * 8  # cheap stock: 10% of 1M / 2元 = 50000 shares
    db, provider, codes = _flat_provider(tmp_path, "cheap.db", closes)
    s = BuyOnceStrategy()
    s.init({"strength": 1.0})
    res = BacktestEngine(provider, 1_000_000.0,
                         risk_manager=RiskManager(RiskConfig()),
                         slippage=0.0).run(s, codes,
                                           date(2024, 1, 1), date(2024, 2, 1))
    db.close()
    qty = next(t.quantity for t in res.trades if t.direction == Direction.BUY)
    # The old hard 10000-share/trade cap is gone…
    assert qty > 10_000
    # …and the fill is bounded instead by the 10% single-stock notional cap
    # (100k 元 of a ~2元 stock, net of commission ≈ 48700 shares).
    assert qty * 2.0 <= 100_000
    assert qty * 2.0 > 95_000


def test_backtest_volume_cap_limits_single_bar_fill(tmp_path):
    """B1: a buy can't take more than 25% of the bar's turnover in one bar —
    so a thin-turnover name fills far below the 10% single-stock cap."""
    from quanti.risk.manager import RiskConfig, RiskManager

    db = Database(str(tmp_path / "vc.db"))
    db.initialize()
    dates = pd.bdate_range("2024-01-02", periods=8)
    # price 10, tiny turnover 100k 元/bar → 25% = 25k → cap = 2500 shares,
    # well below the 10% single-stock cap (100k/10 = 10000 shares).
    df = pd.DataFrame({
        "code": "000001", "date": [d.date() for d in dates],
        "open": 10.0, "high": 10.01, "low": 9.99, "close": 10.0,
        "volume": 1e4, "amount": 100_000.0, "turnover": 1.0})
    db.save_daily_quotes(df)
    db.save_trade_calendar([d.date() for d in dates])
    provider = DataProvider(db)
    s = BuyOnceStrategy()
    s.init({"strength": 1.0})
    res = BacktestEngine(provider, 1_000_000.0,
                         risk_manager=RiskManager(RiskConfig()),
                         slippage=0.0).run(s, ["000001"],
                                           date(2024, 1, 1), date(2024, 2, 1))
    qty = next(t.quantity for t in res.trades if t.direction == Direction.BUY)
    assert qty == 2500  # capped by turnover, not the 10% single-stock cap


def test_backtest_portfolio_circuit_breaker(tmp_path):
    """C1: equity drawdown past -15% from the high-water mark flattens the book
    at next open and halts — no positions held afterwards, mirroring live."""
    from quanti.risk.manager import RiskConfig, RiskManager

    # Buy at 10, drift up to 11 (new HWM), then crash to 8 (-27% from peak).
    closes = [10.0, 10.5, 11.0, 11.0, 9.5, 8.0, 8.0, 8.0, 8.0]
    db, provider, codes = _flat_provider(tmp_path, "cb.db", closes)
    s = BuyOnceStrategy()
    s.init({"strength": 1.0})
    res = BacktestEngine(
        provider, 1_000_000.0,
        risk_manager=RiskManager(RiskConfig(
            portfolio_stop_loss_pct=-0.15, stop_loss_pct=-0.50,
            max_position_pct=1.0)),
        slippage=0.0,
    ).run(s, codes, date(2024, 1, 1), date(2024, 2, 1))
    db.close()
    # The breaker fired: a flatten SELL tagged portfolio_stop was recorded…
    cb_sells = [t for t in res.trades
                if t.strategy == "portfolio_stop" and t.direction == Direction.SELL]
    assert cb_sells, "expected a 组合回撤熔断 flatten sell"
    assert any("熔断" in t.reason for t in cb_sells)


def test_backtest_can_invest_beyond_80pct(tmp_path):
    """Removed 80% total cap: across many names the book can deploy well past
    80% of equity (bounded only by the per-stock 10% cap)."""
    from quanti.risk.manager import RiskConfig, RiskManager

    db = Database(str(tmp_path / "many.db"))
    db.initialize()
    dates = pd.bdate_range("2024-01-02", periods=6)
    codes = [f"00000{i}" for i in range(1, 10)]  # 9 names → up to ~90% at 10% each
    frames = []
    for c in codes:
        frames.append(pd.DataFrame({
            "code": c, "date": [d.date() for d in dates],
            "open": 10.0, "high": 10.01, "low": 9.99, "close": 10.0,
            "volume": 5e6, "amount": 5e7, "turnover": 1.0}))
    db.save_daily_quotes(pd.concat(frames, ignore_index=True))
    db.save_trade_calendar([d.date() for d in dates])
    provider = DataProvider(db)

    class BuyAll(BaseStrategy):
        name = "buy_all"
        def init(self, config): self._done = set()
        def on_bar(self, bar):
            if bar.code in self._done:
                return []
            self._done.add(bar.code)
            return [Signal(bar.code, Direction.BUY, 1.0, "b")]

    s = BuyAll()
    s.init({})
    res = BacktestEngine(provider, 1_000_000.0,
                         risk_manager=RiskManager(RiskConfig()),
                         slippage=0.0).run(s, codes,
                                           date(2024, 1, 1), date(2024, 2, 1))
    invested = sum(t.price * t.quantity for t in res.trades
                   if t.direction == Direction.BUY)
    db.close()
    # Old 80% cap would have stopped near 800k; now it deploys past it.
    assert invested > 850_000


class BuyEveryBarStrategy(BaseStrategy):
    """Buys on EVERY bar (no memory), so the protection gate has signals to block."""

    name = "buy_every_bar"

    def init(self, config):
        pass

    def on_bar(self, bar):
        return [Signal(stock_code=bar.code, direction=Direction.BUY,
                       strength=1.0, reason="always buy")]


def _seed_stop_loss_scenario(tmp_path):
    """Build a synthetic provider + strategy + date range that reliably triggers
    >=2 stop-loss exits on a single stock.  Price path: calm open, two -20% crash
    days each followed by a recovery bar so the next-open fill can execute."""
    db = Database(str(tmp_path / "sl.db"))
    db.initialize()
    # 20 bars: calm start, crash on bar 8 (~-20%), small recovery, crash again on
    # bar 13 (~-20%), then trailing bars.
    dates = pd.bdate_range("2024-01-02", periods=20)
    closes = [
        10.0, 10.1, 10.0, 10.1, 10.0, 10.1, 10.0, 10.1,  # 8 calm bars
        8.0, 8.1,                                            # crash bar 9, recovery
        8.1, 8.2, 8.1, 8.2,                                 # calm (already re-bought)
        6.5, 6.6,                                            # second crash bar 15
        6.6, 6.7, 6.7, 6.7,                                 # trailing bars
    ]
    df = pd.DataFrame({
        "code": "000001", "date": [d.date() for d in dates],
        "open": closes, "high": [c + 0.1 for c in closes],
        "low": [c - 0.1 for c in closes], "close": closes,
        "volume": 2e6, "amount": [c * 2e6 for c in closes], "turnover": 1.0,
    })
    db.save_daily_quotes(df)
    provider = DataProvider(db)
    strat = BuyEveryBarStrategy()
    strat.init({})
    start = date(2024, 1, 1)
    end = date(2024, 2, 15)
    return provider, ["000001"], strat, start, end, db


def test_backtest_protections_block_buys_after_stop_cluster(tmp_path):
    """With a ProtectionManager that locks after stop-losses, BUYs on locked
    days are skipped. Without one, behavior is unchanged."""
    from quanti.risk.manager import RiskConfig, RiskManager
    from quanti.risk.protections import ProtectionConfig, ProtectionManager

    provider, codes, _strat, start, end, db = _seed_stop_loss_scenario(tmp_path)

    def _make_strat():
        s = BuyEveryBarStrategy()
        s.init({})
        return s

    risk_cfg = RiskConfig(stop_loss_pct=-0.08)

    base = BacktestEngine(
        provider, 200_000.0, risk_manager=RiskManager(risk_cfg),
    ).run(_make_strat(), codes, start, end)

    guarded = BacktestEngine(
        provider, 200_000.0, risk_manager=RiskManager(risk_cfg),
        protection_manager=ProtectionManager(ProtectionConfig(
            sg_lookback_days=10, sg_trade_limit=2, sg_lock_days=10,
            max_drawdown_enabled=False)),
    ).run(_make_strat(), codes, start, end)

    # Guarded run STRICTLY skips more signals than base (guard fired and locked).
    assert guarded.skipped_signals > base.skipped_signals

    # Once the guard trips, strictly fewer BUYs should fill.
    def n_buys(r):
        return sum(1 for t in r.trades if t.direction == Direction.BUY)

    assert n_buys(guarded) < n_buys(base)

    # Smoke: no protection_manager (backward-compatible) still works.
    smoke = BacktestEngine(provider, 200_000.0).run(
        _make_strat(), codes, start, end)
    assert smoke.equity_curve is not None

    db.close()


def test_backtest_atr_stop_fires_when_fixed_would_not(tmp_path):
    """P1-1 end-to-end: a calm name (tiny ATR) that drops -6% is NOT cut by the
    fixed -8% stop, but IS cut by the ATR-adaptive stop (k=2, ratio≈1.7% → stop
    ≈-3.3%). Proves the engine precomputes ATR ratios and injects them into
    check_exits. k=0 on the same data holds."""
    from quanti.risk.manager import RiskConfig, RiskManager

    db = Database(str(tmp_path / "atr.db"))
    db.initialize()
    # 9 flat low-vol bars, a -6% drop on bar 10, a trailing bar for next-open fill.
    closes = [10.0] * 9 + [9.4, 9.4]
    dates = pd.bdate_range("2024-01-02", periods=len(closes))
    df = pd.DataFrame({
        "code": "000001", "date": [d.date() for d in dates],
        "open": closes, "high": [c + 0.02 for c in closes],
        "low": [c - 0.02 for c in closes], "close": closes,
        "volume": 1e6, "amount": [c * 1e6 for c in closes], "turnover": 1.0,
    })
    db.save_daily_quotes(df)
    provider = DataProvider(db)
    codes, start, end = ["000001"], date(2024, 1, 1), date(2024, 2, 28)

    def run(k):
        s = AlwaysBuyStrategy()
        s.init({})
        rm = RiskManager(RiskConfig(stop_loss_pct=-0.08, atr_stop_k=k,
                                    atr_stop_n=5, take_profit_activate_pct=0.0))
        return BacktestEngine(provider, 100_000.0, risk_manager=rm).run(
            s, codes, start, end)

    atr_exits = [t for t in run(2.0).trades
                 if t.strategy == "risk_exit" and "ATR" in (t.reason or "")]
    assert atr_exits, "ATR-adaptive stop should have fired on the -6% drop"
    # Fixed-only (k=0): -6% never breaches -8% → no risk_exit.
    assert not [t for t in run(0.0).trades if t.strategy == "risk_exit"]
