"""Tests for PaperBroker's pending order lifecycle.

The change moves the broker from "fill synchronously at latest close" to
"queue → fill at next trading bar's open". These tests verify:

  1. Off-hours signal → no trade row, but a 'pending' order appears.
  2. Once a new bar is in the DB, try_fill_pending_orders fills at OPEN.
  3. Risk re-checked at fill time (cash drift between queue and fill is
     caught and produces a 'rejected' status).
  4. Duplicate signal for same (code, direction) is deduped, not queued
     twice.
  5. Pending TTL: an order with no fillable bar for N trading days is
     auto-cancelled with status='cancelled' and a decision log entry.
  6. T+1: a same-day BUY → SELL queued sequence doesn't violate T+1
     because the SELL waits for its own next bar.
  7. SELL without position is rejected immediately at queue time.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.paper_broker import PaperBroker
from quanti.models import Direction, Signal
from quanti.risk.manager import RiskConfig


@pytest.fixture
def seeded_pending(tmp_path):
    """Seed two stocks. AAA has 5 daily bars including 'today-1'; BBB
    has just 2 bars (much older). Lets us simulate both 'fresh new bar'
    and 'no new bar yet' scenarios."""
    db = Database(str(tmp_path / "p.db"))
    db.initialize()
    today = pd.Timestamp.today().normalize()
    db.upsert_stock("AAA", "alpha", "SZ", date(1991, 4, 3), "test")
    db.upsert_stock("BBB", "beta", "SZ", date(1991, 4, 3), "test")

    # AAA bars: today-5 .. today-1 (so 'today-1' is the "latest known close")
    # When we later insert today's bar, that becomes the next bar.
    def _bars(code, start_offset, n, base_price):
        np.random.seed(hash(code) % 1000)
        dates = [today - pd.Timedelta(days=start_offset + i) for i in range(n)]
        dates = sorted(dates)  # ascending
        prices = [base_price + i * 0.05 for i in range(n)]
        return pd.DataFrame({
            "code": code,
            "date": [d.date() for d in dates],
            "open": [p - 0.02 for p in prices],
            "high": [p + 0.05 for p in prices],
            "low": [p - 0.05 for p in prices],
            "close": prices,
            "volume": np.full(n, 5_000_000.0),
            "amount": [p * 5_000_000 for p in prices],
            "turnover": np.full(n, 1.0),
        })

    db.save_daily_quotes(_bars("AAA", 1, 5, 10.0))
    db.save_daily_quotes(_bars("BBB", 30, 2, 20.0))
    provider = DataProvider(db)
    yield db, provider
    db.close()


# --- queue-time behavior --------------------------------------------------

class TestQueueing:
    def test_buy_signal_queues_no_trade(self, seeded_pending):
        db, provider = seeded_pending
        broker = PaperBroker(db, provider, initial_cash=200_000,
                             fill_mode="pending")
        sig = Signal(stock_code="AAA", direction=Direction.BUY,
                     strength=0.5, reason="test queue")
        # Use `execute_signals` to also assert BrokerResult counters.
        result = broker.execute_signals([sig], "test")
        assert result.accepted == 1
        assert result.pending == 1
        assert result.filled == 0
        assert result.rejected == 0
        # No trades yet
        assert db.list_trades() == []
        # But one pending order
        pending = db.list_orders(status="pending")
        assert len(pending) == 1
        assert pending[0]["code"] == "AAA"
        assert pending[0]["direction"] == "buy"
        assert pending[0]["status"] == "pending"
        # Decision log captured the queue event
        logs = db.list_decisions(kind="order_queued")
        assert len(logs) == 1

    def test_duplicate_signal_deduped(self, seeded_pending):
        """Two BUYs for the same code → only the first queues."""
        db, provider = seeded_pending
        broker = PaperBroker(db, provider, initial_cash=200_000,
                             fill_mode="pending")
        sig = Signal(stock_code="AAA", direction=Direction.BUY,
                     strength=0.5, reason="first")
        sig2 = Signal(stock_code="AAA", direction=Direction.BUY,
                      strength=0.6, reason="second")
        broker.execute_signal(sig, "test")
        broker.execute_signal(sig2, "test")
        pending = db.list_orders(status="pending")
        assert len(pending) == 1, \
            f"expected dedup to keep 1 pending, got {len(pending)}"

    def test_sell_without_position_rejected_at_queue(self, seeded_pending):
        db, provider = seeded_pending
        broker = PaperBroker(db, provider, initial_cash=200_000,
                             fill_mode="pending")
        sig = Signal(stock_code="AAA", direction=Direction.SELL,
                     strength=1.0, reason="phantom sell")
        landed = broker.execute_signal(sig, "test")
        assert landed is False
        pending = db.list_orders(status="pending")
        assert len(pending) == 0
        rejected = [o for o in db.list_orders() if o["status"] == "rejected"]
        assert len(rejected) == 1
        assert "no position" in rejected[0]["reason"].lower()


# --- fill behavior --------------------------------------------------------

def _append_new_bar(db, code, days_from_today_back=0,
                   open_price=10.5, close_price=10.6):
    """Append a single new bar dated today - days_from_today_back."""
    today = pd.Timestamp.today().normalize()
    d = (today - pd.Timedelta(days=days_from_today_back)).date()
    df = pd.DataFrame([{
        "code": code, "date": d,
        "open": open_price, "high": close_price + 0.1,
        "low": open_price - 0.1, "close": close_price,
        "volume": 5_000_000.0,
        "amount": close_price * 5_000_000,
        "turnover": 1.0,
    }])
    db.save_daily_quotes(df)


def _backdate_pending(db, days: int = 1) -> datetime:
    """Rewrite every pending order's created_at to NOON `days` ago.

    Noon matters: order_decision_date re-attributes pre-09:25 timestamps to
    the previous trading day, so a backdate carrying the suite's wall-clock
    time would shift by an extra day when tests run in the early morning.
    Pinning to 12:00 keeps decision date == calendar date at any run time.
    """
    ts = (datetime.now() - timedelta(days=days)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    db.conn.execute("UPDATE orders SET created_at=? WHERE status='pending'",
                    (ts.isoformat(),))
    db.conn.commit()
    return ts


class TestEntryStrategyPlumbing:
    def test_entry_strategy_survives_queue_to_position(self, seeded_pending):
        """A buy signal's entry_strategy must travel signal → order row →
        (next-bar fill) → position row, so an exit can later replay the
        owning strategy on the holding."""
        db, provider = seeded_pending
        broker = PaperBroker(db, provider, initial_cash=200_000,
                             fill_mode="pending")
        sig = Signal(stock_code="AAA", direction=Direction.BUY,
                     strength=0.5, reason="q", entry_strategy="turtle_breakout")
        broker.execute_signal(sig, "ensemble")
        # Stored on the pending order row.
        assert db.list_orders(status="pending")[0]["entry_strategy"] == "turtle_breakout"
        # Backdate + new bar so it fills.
        _backdate_pending(db)
        _append_new_bar(db, "AAA", days_from_today_back=0,
                        open_price=11.0, close_price=11.1)
        broker.try_fill_pending_orders()
        pos = next(p for p in db.list_positions() if p["code"] == "AAA")
        assert pos["entry_strategy"] == "turtle_breakout"

    def test_addon_buy_preserves_original_owner(self, seeded_pending):
        """Averaging into an existing position must NOT overwrite the original
        entry_strategy (COALESCE on empty)."""
        db, provider = seeded_pending
        broker = PaperBroker(db, provider, initial_cash=500_000,
                             fill_mode="immediate")
        broker.execute_signal(
            Signal(stock_code="AAA", direction=Direction.BUY, strength=0.3,
                   reason="open", entry_strategy="macd_cross"), "ensemble")
        # Second buy with a DIFFERENT (or empty) owner must not clobber it.
        broker.execute_signal(
            Signal(stock_code="AAA", direction=Direction.BUY, strength=0.3,
                   reason="addon", entry_strategy=""), "ensemble")
        pos = next(p for p in db.list_positions() if p["code"] == "AAA")
        assert pos["entry_strategy"] == "macd_cross"


class TestFilling:
    def test_pending_fill_uses_next_bar_open(self, seeded_pending):
        """Queue at time T (last AAA bar is 'today-1'). Backdate the
        order's created_at to 'yesterday' so today's new bar counts as
        'next'. Append a new bar for 'today'. Run try_fill_pending —
        should fill at today's OPEN + slippage."""
        db, provider = seeded_pending
        broker = PaperBroker(db, provider, initial_cash=200_000,
                             fill_mode="pending", slippage=0.001)
        sig = Signal(stock_code="AAA", direction=Direction.BUY,
                     strength=0.5, reason="queue")
        broker.execute_signal(sig, "test")
        # Sanity: queued, not filled
        assert len(db.list_orders(status="pending")) == 1
        # Backdate created_at to yesterday so today's new bar is "after"
        _backdate_pending(db)

        # Append today's bar with a clean OPEN = 11.00 we can recognize.
        _append_new_bar(db, "AAA", days_from_today_back=0,
                       open_price=11.00, close_price=11.15)
        result = broker.try_fill_pending_orders()
        assert result.scanned == 1
        assert result.filled == 1
        assert result.still_pending == 0
        # Trade exists, price ≈ 11.00 * (1 + 0.001) = 11.011
        trades = db.list_trades()
        assert len(trades) == 1
        assert trades[0]["direction"] == "buy"
        assert 11.005 < trades[0]["price"] < 11.020, \
            f"expected fill near open 11.00 + 10bps, got {trades[0]['price']}"
        # Order moved to filled
        filled = [o for o in db.list_orders() if o["status"] == "filled"]
        assert len(filled) == 1
        # Decision log shows order_filled_pending entry
        assert len(db.list_decisions(kind="order_filled_pending")) == 1

    def test_pending_fill_sizes_on_persisted_strength(self, seeded_pending):
        """F3: a weak (strength=0.5) buy, queued then filled, must size on the
        persisted 0.5 — not the pre-fix hardcoded 1.0 that over-invested weak
        signals to full size on the pending path."""
        db, provider = seeded_pending
        # High concentration caps so STRENGTH is the binding sizing constraint —
        # otherwise the 20% single-stock cap clips every strength to the same qty
        # and the test can't tell 0.5 from 1.0.
        broker = PaperBroker(db, provider, initial_cash=200_000,
                             fill_mode="pending",
                             risk_config=RiskConfig(max_position_pct=0.95,
                                                    max_industry_pct=0.95))
        broker.execute_signal(
            Signal(stock_code="AAA", direction=Direction.BUY,
                   strength=0.5, reason="weak"), "test")
        # Persisted on the pending order row (not clobbered to 1.0).
        assert db.list_orders(status="pending")[0]["strength"] == 0.5
        _backdate_pending(db)
        _append_new_bar(db, "AAA", days_from_today_back=0,
                        open_price=11.0, close_price=11.1)
        broker.try_fill_pending_orders()
        pos = next(p for p in db.list_positions() if p["code"] == "AAA")
        notional = pos["quantity"] * 11.011
        # strength 0.5 → ~0.475×cash ≈ 95k. The pre-fix bug (size on 1.0) would
        # be ~0.95×cash ≈ 190k. Bound brackets 0.5 and excludes 1.0.
        assert 60_000 < notional < 120_000, notional

    def test_no_new_bar_keeps_pending(self, seeded_pending):
        """try_fill_pending with no new bar → pending stays pending."""
        db, provider = seeded_pending
        broker = PaperBroker(db, provider, initial_cash=200_000,
                             fill_mode="pending")
        sig = Signal(stock_code="AAA", direction=Direction.BUY,
                     strength=0.5, reason="queue")
        broker.execute_signal(sig, "test")
        # Pin created_at to noon: an early-morning (<09:25) suite run would
        # otherwise re-attribute the order to the previous session, where the
        # fixture's synthetic weekend bars could satisfy next_trading_bar.
        _backdate_pending(db, days=0)
        result = broker.try_fill_pending_orders()
        assert result.scanned == 1
        assert result.filled == 0
        assert result.still_pending == 1
        # Order is still pending
        assert len(db.list_orders(status="pending")) == 1


# --- risk re-check --------------------------------------------------------

class TestRiskAtFill:
    def test_cash_drained_between_queue_and_fill(self, seeded_pending):
        """Queue a BUY, then drain cash before fill. Risk check at fill
        time should reject the order — but we have to be careful about
        which limit triggers. With max_position_pct=10% on a $200k
        portfolio, a $11k buy is right at the edge. Easier: zero out
        cash directly then fill, expect rejected status."""
        db, provider = seeded_pending
        broker = PaperBroker(db, provider, initial_cash=200_000,
                             fill_mode="pending")
        sig = Signal(stock_code="AAA", direction=Direction.BUY,
                     strength=0.9, reason="queue")
        broker.execute_signal(sig, "test")
        # Backdate so today's bar is "after" the order
        _backdate_pending(db)
        # Zero out cash
        db.update_cash(0.0)
        _append_new_bar(db, "AAA", days_from_today_back=0,
                       open_price=11.00, close_price=11.15)
        result = broker.try_fill_pending_orders()
        # Either rejected (no cash → can't afford even 100 shares) — count it.
        assert result.filled == 0
        assert result.rejected == 1
        # The pending order is no longer pending
        assert len(db.list_orders(status="pending")) == 0


# --- TTL expiry -----------------------------------------------------------

class TestTTL:
    def test_expired_pending_cancelled(self, seeded_pending):
        """A pending order with no fillable bar past TTL is auto-cancelled."""
        db, provider = seeded_pending
        # TTL=0 trading days → immediate expiry on any tick after creation.
        broker = PaperBroker(db, provider, initial_cash=200_000,
                             fill_mode="pending",
                             pending_ttl_trading_days=0)
        sig = Signal(stock_code="BBB", direction=Direction.BUY,
                     strength=0.3, reason="queue")
        broker.execute_signal(sig, "test")
        # Backdate created_at so the TTL math triggers
        _backdate_pending(db, days=10)
        result = broker.try_fill_pending_orders()
        # BBB has no new bar past created date → expired
        assert result.expired == 1
        cancelled = [o for o in db.list_orders() if o["status"] == "cancelled"]
        assert len(cancelled) == 1
        # Decision log
        assert len(db.list_decisions(kind="order_expired_pending")) == 1


# --- decision-date attribution (midnight-spillover fix) --------------------

class TestOrderDecisionDate:
    """`order_decision_date` maps an order's wall-clock created_at to the
    trading day whose data the decision was based on.

    The agent's 23:30 daily cycle spills past midnight (~2h pipeline), so an
    order stamped 01:32 on day D was decided on D-1's close. Dating it D made
    `next_trading_bar` (strictly-greater) skip D's bar → an extra trading day
    of lag vs the design (next-session OPEN fill). Anything created before the
    09:25 opening-auction match belongs to the previous session; D's open is
    still unknown then, so filling on D's bar is not look-ahead.

    Fixed 2026 calendar: 07-01 Wed, 07-02 Thu, 07-04 Sat, 07-06 Mon.
    """

    def test_same_night_keeps_wall_clock_date(self):
        from quanti.utils.market import order_decision_date
        assert order_decision_date(datetime(2026, 7, 1, 23, 45)) == date(2026, 7, 1)

    def test_past_midnight_attributes_to_previous_trading_day(self):
        from quanti.utils.market import order_decision_date
        assert order_decision_date(datetime(2026, 7, 2, 1, 32)) == date(2026, 7, 1)

    def test_intraday_keeps_wall_clock_date(self):
        from quanti.utils.market import order_decision_date
        assert order_decision_date(datetime(2026, 7, 2, 10, 0)) == date(2026, 7, 2)

    def test_cutoff_boundary_at_auction_match(self):
        from quanti.utils.market import order_decision_date
        # 09:25 is when the opening auction matches — from that moment
        # today's open is knowable, so the order keeps its wall-clock date.
        assert order_decision_date(datetime(2026, 7, 2, 9, 25)) == date(2026, 7, 2)
        assert order_decision_date(datetime(2026, 7, 2, 9, 24, 59)) == date(2026, 7, 1)

    def test_saturday_early_morning_attributes_to_friday(self):
        from quanti.utils.market import order_decision_date
        assert order_decision_date(datetime(2026, 7, 4, 1, 32)) == date(2026, 7, 3)

    def test_monday_early_morning_skips_weekend(self):
        from quanti.utils.market import order_decision_date
        assert order_decision_date(datetime(2026, 7, 6, 1, 32)) == date(2026, 7, 3)


@pytest.fixture
def fixed_calendar_pending(tmp_path):
    """Fixed-calendar variant of `seeded_pending` for the midnight-spillover
    tests: AAA has bars 2026-06-29 (Mon) .. 2026-07-01 (Wed), all dates
    hard-coded so assertions don't depend on when the suite runs."""
    db = Database(str(tmp_path / "fx.db"))
    db.initialize()
    db.upsert_stock("AAA", "alpha", "SZ", date(1991, 4, 3), "test")
    days = [date(2026, 6, 29), date(2026, 6, 30), date(2026, 7, 1)]
    prices = [10.0, 10.05, 10.10]
    df = pd.DataFrame({
        "code": "AAA",
        "date": days,
        "open": [p - 0.02 for p in prices],
        "high": [p + 0.05 for p in prices],
        "low": [p - 0.05 for p in prices],
        "close": prices,
        "volume": np.full(3, 5_000_000.0),
        "amount": [p * 5_000_000 for p in prices],
        "turnover": np.full(3, 1.0),
    })
    db.save_daily_quotes(df)
    provider = DataProvider(db)
    yield db, provider
    db.close()


def _set_created_at(db, iso_ts: str) -> None:
    db.conn.execute("UPDATE orders SET created_at=? WHERE status='pending'",
                    (iso_ts,))
    db.conn.commit()


def _append_bar_on(db, code, d, open_price, close_price):
    df = pd.DataFrame([{
        "code": code, "date": d,
        "open": open_price, "high": close_price + 0.1,
        "low": open_price - 0.1, "close": close_price,
        "volume": 5_000_000.0,
        "amount": close_price * 5_000_000,
        "turnover": 1.0,
    }])
    db.save_daily_quotes(df)


class TestMidnightSpilloverFill:
    """An order stamped past midnight (23:30 cycle + ~2h pipeline) must fill
    at the SAME day's open — the design's next-session fill for a decision
    made on the previous close — not slip an extra trading day."""

    def test_past_midnight_order_fills_on_same_days_bar(self, fixed_calendar_pending):
        """Order stamped 07-02 01:32 (decided on 07-01 close) + the 07-02 bar
        arrives → fills at the 07-02 OPEN. Wall-clock dating would demand a
        bar > 07-02 and slip the fill to 07-03."""
        db, provider = fixed_calendar_pending
        broker = PaperBroker(db, provider, initial_cash=200_000,
                             fill_mode="pending", slippage=0.001)
        broker.execute_signal(
            Signal(stock_code="AAA", direction=Direction.BUY,
                   strength=0.5, reason="llm cycle"), "llm")
        _set_created_at(db, "2026-07-02T01:32:00")
        _append_bar_on(db, "AAA", date(2026, 7, 2),
                       open_price=10.20, close_price=10.30)

        result = broker.try_fill_pending_orders()
        assert result.filled == 1, \
            f"expected same-day fill, got {result}"
        trades = db.list_trades()
        assert len(trades) == 1
        assert trades[0]["trade_date"] == "2026-07-02"
        # Filled at the 07-02 open + slippage, not any later bar.
        assert trades[0]["price"] == pytest.approx(10.20 * 1.001, rel=1e-6)

    def test_same_night_order_still_fills_next_session(self, fixed_calendar_pending):
        """Control: a pipeline that finishes BEFORE midnight (23:45) keeps the
        existing behavior — decision day 07-01, fill on the 07-02 bar."""
        db, provider = fixed_calendar_pending
        broker = PaperBroker(db, provider, initial_cash=200_000,
                             fill_mode="pending", slippage=0.001)
        broker.execute_signal(
            Signal(stock_code="AAA", direction=Direction.BUY,
                   strength=0.5, reason="llm cycle"), "llm")
        _set_created_at(db, "2026-07-01T23:45:00")
        _append_bar_on(db, "AAA", date(2026, 7, 2),
                       open_price=10.20, close_price=10.30)

        result = broker.try_fill_pending_orders()
        assert result.filled == 1
        assert db.list_trades()[0]["trade_date"] == "2026-07-02"

    def test_past_midnight_order_never_fills_on_known_bar(self, fixed_calendar_pending):
        """No look-ahead: at 07-02 01:32 the newest bar on disk is 07-01 —
        already known when the order was created — so it must NOT fill until
        a genuinely newer bar arrives."""
        db, provider = fixed_calendar_pending
        broker = PaperBroker(db, provider, initial_cash=200_000,
                             fill_mode="pending")
        broker.execute_signal(
            Signal(stock_code="AAA", direction=Direction.BUY,
                   strength=0.5, reason="llm cycle"), "llm")
        _set_created_at(db, "2026-07-02T01:32:00")
        # No 07-02 bar appended.
        result = broker.try_fill_pending_orders()
        assert result.filled == 0
        assert db.list_trades() == []

    def test_detail_expected_fill_date_uses_decision_day(self, fixed_calendar_pending):
        """UI: the 07-02 01:32 order should advertise 07-02 as its expected
        fill date (next session after the 07-01 decision day), not 07-03."""
        db, provider = fixed_calendar_pending
        broker = PaperBroker(db, provider, initial_cash=200_000,
                             fill_mode="pending")
        broker.execute_signal(
            Signal(stock_code="AAA", direction=Direction.BUY,
                   strength=0.5, reason="llm cycle"), "llm")
        _set_created_at(db, "2026-07-02T01:32:00")

        d = broker.pending_orders_detail()[0]
        assert d["bar_available"] is False        # 07-02 bar not on disk yet
        assert d["expected_fill_date"] == "2026-07-02"

        _append_bar_on(db, "AAA", date(2026, 7, 2),
                       open_price=10.20, close_price=10.30)
        d = broker.pending_orders_detail()[0]
        assert d["bar_available"] is True
        assert d["expected_fill_date"] == "2026-07-02"


# --- limit-up/down tradability gate (audit C3) ---------------------------

class TestLimitTradabilityGate:
    """Paper/live must not 'fill' into a 一字板 the way the old code did — a
    BUY can't fill a limit-up lock, a SELL can't fill a limit-down lock. The
    backtest already gated this; these pin the broker to the same rule."""

    def _backdate(self, db):
        _backdate_pending(db)

    def test_pending_buy_blocked_by_limit_up(self, seeded_pending):
        db, provider = seeded_pending
        broker = PaperBroker(db, provider, initial_cash=200_000,
                             fill_mode="pending", slippage=0.001)
        broker.execute_signal(Signal("AAA", Direction.BUY, 0.5, "q"), "test")
        self._backdate(db)
        # Today's bar opens limit-up (一字板) vs prev close ~10.20 (>+10%).
        _append_new_bar(db, "AAA", 0, open_price=11.40, close_price=11.40)
        result = broker.try_fill_pending_orders()
        assert result.filled == 0
        assert result.still_pending == 1          # stays queued, not filled
        assert db.list_trades() == []
        assert len(db.list_orders(status="pending")) == 1

    def test_pending_sell_blocked_by_limit_down(self, seeded_pending):
        db, provider = seeded_pending
        broker = PaperBroker(db, provider, initial_cash=200_000,
                             fill_mode="pending")
        # Hold AAA from long ago (T+1 satisfied), then queue an exit SELL.
        db.upsert_position("AAA", 1000, 10.0, 10.2, date(2020, 1, 1))
        broker.execute_signal(Signal("AAA", Direction.SELL, 1.0, "exit"), "test")
        self._backdate(db)
        # Today opens limit-down vs prev ~10.20 (<-10%) → SELL can't fill.
        _append_new_bar(db, "AAA", 0, open_price=9.00, close_price=9.00)
        result = broker.try_fill_pending_orders()
        assert result.filled == 0
        assert result.still_pending == 1
        assert any(p["code"] == "AAA" for p in db.list_positions())  # untouched

    def test_pending_buy_fills_when_open_within_band(self, seeded_pending):
        """Control: a normal open (+7%) within the band fills as usual — the
        gate doesn't block legitimate moves."""
        db, provider = seeded_pending
        broker = PaperBroker(db, provider, initial_cash=200_000,
                             fill_mode="pending", slippage=0.0)
        broker.execute_signal(Signal("AAA", Direction.BUY, 0.5, "q"), "test")
        self._backdate(db)
        _append_new_bar(db, "AAA", 0, open_price=10.90, close_price=10.95)
        result = broker.try_fill_pending_orders()
        assert result.filled == 1
        assert result.still_pending == 0

    def test_extreme_gap_up_abandons_pending_buy(self, tmp_path):
        """A pending BUY whose next open gaps up >= the guard threshold is
        ABANDONED (cancelled), not filled — chasing a blow-off open. Uses a
        20cm (创业板 300xxx) code so the +15% open is tradable (< 20% limit),
        i.e. the limit-up gate does NOT already block it — this is the zone the
        guard newly protects."""
        db = Database(str(tmp_path / "gap.db"))
        db.initialize()
        db.upsert_stock("300001", "cyb", "SZ", date(2010, 1, 1), "test")
        today = pd.Timestamp.today().normalize()
        prev = (today - pd.Timedelta(days=1)).date()
        db.save_daily_quotes(pd.DataFrame([{
            "code": "300001", "date": prev, "open": 9.9, "high": 10.1,
            "low": 9.8, "close": 10.0, "volume": 5e6, "amount": 5e7,
            "turnover": 1.0}]))
        provider = DataProvider(db)
        # Default RiskConfig → extreme_gap_up_block_pct = 0.10 (guard ON).
        broker = PaperBroker(db, provider, initial_cash=200_000,
                             fill_mode="pending", slippage=0.0)
        broker.execute_signal(Signal("300001", Direction.BUY, 0.5, "q"), "test")
        _backdate_pending(db)
        # Today opens +15% (11.5) vs prev close 10.0 — tradable (< 20% limit),
        # but >= 10% guard → abandoned.
        _append_new_bar(db, "300001", 0, open_price=11.50, close_price=11.60)
        result = broker.try_fill_pending_orders()
        assert result.filled == 0
        assert result.expired == 1          # abandoned counts under expired
        assert db.list_trades() == []
        o = db.list_orders()[0]
        assert o["status"] == "cancelled"
        assert "gap-up" in o["reason"].lower()
        assert len(db.list_decisions(kind="order_gap_abandoned")) == 1
        db.close()

    def test_gap_guard_off_lets_big_gap_fill(self, tmp_path):
        """Control: with the guard disabled (0), the same +15% open fills —
        proving the abandonment above is the guard, not some other gate."""
        db = Database(str(tmp_path / "gap0.db"))
        db.initialize()
        db.upsert_stock("300001", "cyb", "SZ", date(2010, 1, 1), "test")
        today = pd.Timestamp.today().normalize()
        prev = (today - pd.Timedelta(days=1)).date()
        db.save_daily_quotes(pd.DataFrame([{
            "code": "300001", "date": prev, "open": 9.9, "high": 10.1,
            "low": 9.8, "close": 10.0, "volume": 5e6, "amount": 5e7,
            "turnover": 1.0}]))
        provider = DataProvider(db)
        broker = PaperBroker(db, provider, initial_cash=200_000,
                             fill_mode="pending", slippage=0.0,
                             risk_config=RiskConfig(extreme_gap_up_block_pct=0.0))
        broker.execute_signal(Signal("300001", Direction.BUY, 0.5, "q"), "test")
        _backdate_pending(db)
        _append_new_bar(db, "300001", 0, open_price=11.50, close_price=11.60)
        result = broker.try_fill_pending_orders()
        assert result.filled == 1
        assert result.expired == 0
        db.close()

    def test_immediate_buy_blocked_by_limit_up_close(self, seeded_pending):
        db, provider = seeded_pending
        broker = PaperBroker(db, provider, initial_cash=200_000,
                             fill_mode="immediate")
        # Latest bar sealed limit-up at close vs prev ~10.20.
        _append_new_bar(db, "AAA", 0, open_price=11.40, close_price=11.40)
        assert broker.execute_signal(Signal("AAA", Direction.BUY, 0.5, "m"),
                                     "test") is False
        assert db.list_trades() == []
        assert any(d["kind"] == "order_skipped_limit"
                   for d in db.list_decisions(limit=10))


# --- immediate-mode regression -------------------------------------------

class TestImmediateMode:
    def test_immediate_mode_still_fills_synchronously(self, seeded_pending):
        """Belt-and-suspenders: fill_mode='immediate' must keep the old
        behavior so the existing test_paper_broker.py suite stays green."""
        db, provider = seeded_pending
        broker = PaperBroker(db, provider, initial_cash=200_000,
                             fill_mode="immediate")
        sig = Signal(stock_code="AAA", direction=Direction.BUY,
                     strength=0.5, reason="immediate")
        assert broker.execute_signal(sig, "test") is True
        # Trade row created synchronously
        assert len(db.list_trades()) == 1
        # No pending orders
        assert len(db.list_orders(status="pending")) == 0


# --- pending-order detail (UI) -------------------------------------------

class TestPendingDetail:
    def test_detail_waiting_on_data(self, seeded_pending):
        """A freshly queued order whose next bar isn't on disk yet reports
        bar_available=False and still estimates an expected fill date."""
        db, provider = seeded_pending
        broker = PaperBroker(db, provider, initial_cash=200_000,
                             fill_mode="pending", pending_ttl_trading_days=3)
        broker.execute_signal(
            Signal(stock_code="AAA", direction=Direction.BUY,
                   strength=0.5, reason="排队测试"), "test")
        # Noon pin — see test_no_new_bar_keeps_pending.
        _backdate_pending(db, days=0)
        detail = broker.pending_orders_detail()
        assert len(detail) == 1
        d = detail[0]
        assert d["code"] == "AAA"
        assert d["name"] == "alpha"          # resolved from stocks table
        assert d["industry"] == "test"       # industry surfaced for the UI
        assert d["direction"] == "buy"
        assert d["reason"] == "排队测试"
        assert d["bar_available"] is False   # today's bar not in DB
        assert d["expected_fill_date"]       # still estimated, not None
        assert d["fill_price_basis"] == "open"
        assert d["ttl_trading_days"] == 3
        assert d["created_at"]               # queued timestamp surfaced

    def test_detail_bar_available_after_new_bar(self, seeded_pending):
        """Once the next trading bar lands, detail flips to bar_available
        and points expected_fill_date at that bar's date."""
        db, provider = seeded_pending
        broker = PaperBroker(db, provider, initial_cash=200_000,
                             fill_mode="pending")
        broker.execute_signal(
            Signal(stock_code="AAA", direction=Direction.BUY,
                   strength=0.5, reason="q"), "test")
        # Backdate to yesterday, then append today's bar → it's now "next".
        created = _backdate_pending(db).date()
        _append_new_bar(db, "AAA", days_from_today_back=0,
                        open_price=11.0, close_price=11.1)
        today = pd.Timestamp.today().normalize().date()
        d = broker.pending_orders_detail()[0]
        assert d["bar_available"] is True
        assert d["expected_fill_date"] == today.isoformat()
        # trading_days_pending counts *trading* days only, so it's legitimately
        # 0 when "today" is a weekend/holiday. Assert it matches the calendar
        # function (robust on any run date) rather than hard-coding >= 1.
        from quanti.utils.market import count_trading_days_between
        assert d["trading_days_pending"] == count_trading_days_between(created, today)

    def test_detail_exposes_entry_strategy_not_decision_label(self, seeded_pending):
        """The 进场策略 column must show the OWNING strategy (signal.entry_strategy
        — the one an exit replays), NOT the execute-time strategy_name. They
        differ in LLM/ensemble mode: strategy_name='llm' but entry_strategy is the
        dominant strategy. Regression guard for the column once showing 'llm' for
        every LLM buy."""
        db, provider = seeded_pending
        broker = PaperBroker(db, provider, initial_cash=200_000,
                             fill_mode="pending")
        broker.execute_signal(
            Signal(stock_code="AAA", direction=Direction.BUY, strength=0.5,
                   reason="LLM: 测试", entry_strategy="supertrend"), "llm")
        d = broker.pending_orders_detail()[0]
        assert d["entry_strategy"] == "supertrend"  # owning strategy, not "llm"


def test_snapshot_position_has_price_date(seeded_pending):
    """snapshot_portfolio marks each position to the latest bar and reports
    the bar's date as price_date (not the DB row's updated_at)."""
    db, provider = seeded_pending
    broker = PaperBroker(db, provider, initial_cash=200_000,
                         fill_mode="immediate")
    broker.execute_signal(
        Signal(stock_code="AAA", direction=Direction.BUY,
               strength=0.5, reason="buy"), "test")
    snap = broker.snapshot_portfolio()
    pos = next(p for p in snap["positions"] if p["code"] == "AAA")
    # AAA's latest bar is today-1 (per fixture); price_date reflects it.
    expected = (pd.Timestamp.today().normalize() - pd.Timedelta(days=1)).date()
    assert pos["price_date"] == expected.isoformat()


# --- exit engine: trailing take-profit + strategy replay -----------------

def _exit_db(tmp_path, bars: list[tuple], factor: float = 1.0):
    """Fresh DB with one stock AAA and an explicit bar series.
    bars = list of (date, open, high, low, close); prices are RAW and stored
    with adj_factor=`factor` (hfq = raw × factor)."""
    db = Database(str(tmp_path / "exit.db"))
    db.initialize()
    db.upsert_stock("AAA", "alpha", "SZ", date(1991, 4, 3), "test")
    df = pd.DataFrame([{
        "code": "AAA", "date": d, "open": o, "high": h, "low": lo,
        "close": c, "volume": 5e6, "amount": c * 5e6, "turnover": 1.0,
        "adj_factor": factor,
    } for (d, o, h, lo, c) in bars])
    db.save_daily_quotes(df)
    return db


class TestExitEngine:
    def test_trailing_take_profit_fires(self, tmp_path):
        """Position +18% from cost, peaked at 13.0, now 11.6 (-10.8% off
        peak) → trailing take-profit sells."""
        today = pd.Timestamp.today().normalize()
        bars = [(( today - pd.Timedelta(days=n)).date(),
                 10.0, h, 9.5, c)
                for n, (h, c) in zip(
                    range(4, -1, -1),
                    [(10.5, 10.2), (12.0, 11.8), (13.0, 12.9),
                     (12.5, 12.0), (11.7, 11.6)])]
        db = _exit_db(tmp_path, bars)
        provider = DataProvider(db)
        # Position opened at 10.0 well before the window.
        db.upsert_position("AAA", 1000, 10.0, 11.6,
                           (today - pd.Timedelta(days=20)).date())
        broker = PaperBroker(db, provider, fill_mode="immediate",
                             risk_config=RiskConfig(
                                 take_profit_activate_pct=0.15,
                                 take_profit_trail_pct=0.10,
                                 strategy_exit_enabled=False))
        landed = broker.check_exits()
        assert landed == 1
        trades = db.list_trades()
        assert len(trades) == 1 and trades[0]["direction"] == "sell"

    def test_peak_is_max_high_since_buy(self, tmp_path):
        today = pd.Timestamp.today().normalize()
        bars = [((today - pd.Timedelta(days=n)).date(), 10, 10 + n, 9, 10)
                for n in range(5, 0, -1)]  # highs 15,14,13,12,11
        db = _exit_db(tmp_path, bars)
        broker = PaperBroker(db, DataProvider(db), fill_mode="immediate")
        buy = (today - pd.Timedelta(days=3)).date()  # highs from this date: 13,12,11
        peaks = broker._compute_peaks(
            [{"code": "AAA", "buy_date": buy}])
        assert peaks["AAA"] == pytest.approx(13.0)

    def test_trailing_tp_fires_for_dividend_stock(self, tmp_path):
        """adj_factor≠1 axis consistency: current_price is hfq, so the peak
        must be hfq too (raw high × adj_factor). With the old raw MAX(high)
        peak this dividend stock (factor 1.3) showed a fake +11.8% 'drawdown'
        and the trailing TP never fired; on the hfq axis it is -14% → sell."""
        today = pd.Timestamp.today().normalize()
        raw = [(8.0, 8.2, 7.8, 8.0), (9.5, 10.0, 9.3, 9.8),   # peak high 10.0
               (9.5, 9.6, 9.2, 9.3), (9.1, 9.2, 8.9, 9.0),
               (8.7, 8.8, 8.5, 8.6)]                          # close 8.6
        bars = [((today - pd.Timedelta(days=n)).date(), o, h, lo, c)
                for n, (o, h, lo, c) in zip(range(4, -1, -1), raw)]
        db = _exit_db(tmp_path, bars, factor=1.3)
        buy = (today - pd.Timedelta(days=20)).date()
        # hfq peak = 10.0 × 1.3; the raw 10.0 peak was the bug.
        assert db.get_high_water("AAA", buy) == pytest.approx(13.0)
        # avg_cost 9.1 hfq; current hfq close = 8.6 × 1.3 = 11.18 → +22.9%
        # (armed), retrace off 13.0 = -14% → fires.
        db.upsert_position("AAA", 1000, 9.1, 11.18, buy)
        broker = PaperBroker(db, DataProvider(db), fill_mode="immediate",
                             risk_config=RiskConfig(
                                 take_profit_activate_pct=0.15,
                                 take_profit_trail_pct=0.10,
                                 strategy_exit_enabled=False))
        assert broker.check_exits() == 1
        trades = db.list_trades()
        assert len(trades) == 1 and trades[0]["direction"] == "sell"

    def test_strategy_replay_detects_turtle_exit(self, tmp_path):
        """A holding owned by turtle_breakout that breaks below the N/2-day
        low on the last bar is flagged for exit by the replay."""
        today = pd.Timestamp.today().normalize()
        # 24 climbing bars then a final crash below the 10-day low.
        bars = []
        for n in range(24, 0, -1):
            px = 10.0 + (24 - n) * 0.3
            bars.append(((today - pd.Timedelta(days=n)).date(),
                         px, px + 0.2, px - 0.2, px))
        bars.append((today.date(), 16.0, 16.0, 7.0, 7.0))  # crash
        db = _exit_db(tmp_path, bars)
        broker = PaperBroker(db, DataProvider(db), fill_mode="immediate",
                             strategies_dir="strategies")
        codes = broker._compute_strategy_exits(
            [{"code": "AAA", "buy_date": (today - pd.Timedelta(days=24)).date(),
              "entry_strategy": "turtle_breakout"}])
        # If the strategies dir loaded, the crash must trigger turtle's exit.
        if broker._load_strategies():
            assert "AAA" in codes

    def test_strategy_exit_uses_resolved_params(self, tmp_path, monkeypatch):
        """Exit replay inits the owning strategy via resolve_params (tuned over
        goal) — same as the entry path — not bare class defaults."""
        import quanti.agent.params as params_mod
        seen: list[str] = []
        real = params_mod.resolve_params
        monkeypatch.setattr(
            params_mod, "resolve_params",
            lambda db, name, goal: (seen.append(name), real(db, name, goal))[1])

        today = pd.Timestamp.today().normalize()
        bars = [((today - pd.Timedelta(days=n)).date(), 10, 10.2, 9.8, 10)
                for n in range(24, 0, -1)]
        bars.append((today.date(), 10, 10, 9, 9.5))
        db = _exit_db(tmp_path, bars)
        broker = PaperBroker(db, DataProvider(db), fill_mode="immediate",
                             strategies_dir="strategies")
        if not broker._load_strategies():
            pytest.skip("strategies dir not loaded")
        broker._compute_strategy_exits(
            [{"code": "AAA", "buy_date": (today - pd.Timedelta(days=24)).date(),
              "entry_strategy": "turtle_breakout"}])
        assert "turtle_breakout" in seen  # resolved params, not class defaults
