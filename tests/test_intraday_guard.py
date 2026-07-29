"""Live broker wiring (make_broker) + intraday guard (AgentRuntime)."""

from __future__ import annotations

import threading
from datetime import date, datetime

import pytest

from quanti.agent.goal import load_goal, save_goal
from quanti.agent.runtime import AgentRuntime, CycleHalted
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.factory import make_broker
from quanti.execution.paper_broker import PaperBroker
from quanti.execution.qmt_broker import QmtBroker
from quanti.utils.market import in_trading_session, session_closed_for_day


@pytest.fixture
def dbp(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    yield db, DataProvider(db)
    db.close()


def test_make_broker_picks_account(dbp, monkeypatch):
    db, provider = dbp
    assert isinstance(make_broker(db, provider, account="paper"), PaperBroker)
    # Live requires the explicit real-money acknowledgment (H3).
    monkeypatch.setenv("QUANTI_LIVE_ACK", "I_KNOW_REAL_MONEY")
    live = make_broker(db, provider, account="live")
    assert isinstance(live, QmtBroker) and live._require_live is True  # real-money guard on


def test_in_trading_session_window():
    assert in_trading_session(datetime(2026, 6, 24, 10, 0)) is True    # Wed, morning
    assert in_trading_session(datetime(2026, 6, 24, 12, 0)) is False   # Wed, lunch
    assert in_trading_session(datetime(2026, 6, 28, 10, 0)) is False   # Sunday


def test_session_closed_for_day():
    assert session_closed_for_day(datetime(2026, 6, 24, 10, 0)) is False   # intraday
    assert session_closed_for_day(datetime(2026, 6, 24, 12, 0)) is False   # lunch: not final
    assert session_closed_for_day(datetime(2026, 6, 24, 15, 0)) is True    # at close
    assert session_closed_for_day(datetime(2026, 6, 24, 16, 30)) is True   # post-close
    assert session_closed_for_day(datetime(2026, 6, 28, 10, 0)) is True    # Sunday


class _FakeBroker:
    def __init__(self, *, stop=False, connected=True):
        self.calls: list[str] = []
        self._stop, self._connected = stop, connected

    def is_connected(self):
        return self._connected

    def try_fill_pending_orders(self):
        self.calls.append("fill")

    def enforce_portfolio_stop(self):
        self.calls.append("stop")
        return self._stop

    def check_exits(self):
        self.calls.append("exits")

    def snapshot_portfolio(self):  # status() marks to this
        return {"total_value": 0.0, "pnl_pct": 0.0}


def _agent(dbp, broker):
    db, provider = dbp
    return AgentRuntime(db, provider, broker, intraday_guard_sec=60)


def test_guard_runs_three_steps_in_session(dbp, monkeypatch):
    monkeypatch.setattr("quanti.utils.market.in_trading_session", lambda *a, **k: True)
    br = _FakeBroker()
    _agent(dbp, br)._intraday_guard()
    assert br.calls == ["fill", "stop", "exits"]


def test_guard_idles_outside_session(dbp, monkeypatch):
    monkeypatch.setattr("quanti.utils.market.in_trading_session", lambda *a, **k: False)
    br = _FakeBroker()
    _agent(dbp, br)._intraday_guard()
    assert br.calls == []


def test_guard_idles_when_venue_disconnected(dbp, monkeypatch):
    monkeypatch.setattr("quanti.utils.market.in_trading_session", lambda *a, **k: True)
    br = _FakeBroker(connected=False)
    _agent(dbp, br)._intraday_guard()
    assert br.calls == []  # never mark/exit against stale/mock data


def test_stop_info_atr_tightens_floor():
    """止损价 = avg_cost·(1+max(floor, -k·ATRratio)). ATR tightens; floor backstops."""
    from quanti.risk.manager import RiskConfig, RiskManager
    rm = RiskManager(RiskConfig(stop_loss_pct=-0.15, atr_stop_k=2.0))
    i = rm.stop_info(10.0, 0.03)                       # -2*0.03=-0.06 (tighter than -0.15)
    assert i["atr_driven"] and abs(i["stop_pct"] + 0.06) < 1e-9
    assert abs(i["stop_price"] - 9.4) < 1e-6
    floor = rm.stop_info(10.0, 0.20)                   # -0.40 < -0.15 → floor wins
    assert not floor["atr_driven"] and abs(floor["stop_price"] - 8.5) < 1e-6
    none = rm.stop_info(10.0, None)                    # no ATR → floor
    assert not none["atr_driven"] and abs(none["stop_price"] - 8.5) < 1e-6


def test_live_status_endpoint(tmp_path):
    from fastapi.testclient import TestClient

    from quanti.api.app import create_app
    db = Database(str(tmp_path / "live.db"))
    db.initialize()
    app = create_app(db=db, autostart_agent=False, autostart_background_sync=False)
    with TestClient(app) as c:
        r = c.get("/api/agent/live-status").json()
    assert set(r) >= {"is_live", "guard", "positions"}
    assert r["guard"]["enabled"] is True    # paper runs the guard too (Tencent marks)
    assert r["positions"] == []             # empty portfolio
    db.close()


def test_guard_halts_on_portfolio_stop(dbp, monkeypatch):
    monkeypatch.setattr("quanti.utils.market.in_trading_session", lambda *a, **k: True)
    br = _FakeBroker(stop=True)
    agent = _agent(dbp, br)
    agent._intraday_guard()
    assert br.calls == ["fill", "stop"]            # halted before check_exits
    assert agent.status().running is False
    # A tick computing right now must not buy back into the flattened book.
    assert agent._breaker_flag.is_set()


# ----- lock narrowing: the guard must not starve behind a slow tick ---------

def _slow_tick_agent(dbp, broker):
    """Runtime whose tick blocks in a pure-compute stage (`_ensure_recent_data`,
    which sits after the fills/breaker section and before everything else) until
    released. Goal pins a missing strategy so the tick returns right after."""
    db, provider = dbp
    db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
    goal = load_goal(db)
    goal.strategy_name = "__missing__"   # short-circuits after the compute stage
    save_goal(db, goal)
    agent = AgentRuntime(db, provider, broker, intraday_guard_sec=60)
    in_compute, release = threading.Event(), threading.Event()

    def _blocking_refresh(codes, lookback_days=200):
        in_compute.set()
        assert release.wait(10), "test never released the tick"

    agent._ensure_recent_data = _blocking_refresh
    return agent, in_compute, release


def test_guard_runs_while_tick_computes(dbp, monkeypatch):
    """The regression: the tick used to hold `_broker_lock` end-to-end, so a
    slow cycle blocked the guard's stop-loss (prod: 9m09s on 2026-07-24)."""
    monkeypatch.setattr("quanti.utils.market.in_trading_session", lambda *a, **k: True)
    br = _FakeBroker()
    agent, in_compute, release = _slow_tick_agent(dbp, br)

    t = threading.Thread(target=agent._safe_cycle, daemon=True)
    t.start()
    assert in_compute.wait(10), "tick never reached the compute stage"
    br.calls.clear()                     # drop the tick's own fill/stop calls
    agent._intraday_guard()              # would block forever before the fix
    assert br.calls == ["fill", "stop", "exits"]

    release.set()
    t.join(10)
    assert not t.is_alive()


def test_tick_mutations_still_exclude_the_guard(dbp, monkeypatch):
    """Narrowed ≠ removed: while the guard holds the broker lock, a tick can
    compute but cannot enter its mutating sections."""
    monkeypatch.setattr("quanti.utils.market.in_trading_session", lambda *a, **k: True)
    agent, _, _ = _slow_tick_agent(dbp, _FakeBroker())
    entered = threading.Event()

    with agent._broker_lock:
        t = threading.Thread(
            target=lambda: (agent._broker_exec().__enter__(), entered.set()),
            daemon=True)
        t.start()
        assert not entered.wait(0.5)     # blocked on the guard's lock
    assert entered.wait(5)               # released once the guard is done


def test_breaker_mid_tick_aborts_before_orders(dbp):
    """Guard flattens + halts while the tick is in the LLM/compute stage → the
    tick's execution gate refuses instead of re-entering the market."""
    agent, _, _ = _slow_tick_agent(dbp, _FakeBroker())
    with agent._broker_exec():
        pass                                     # clean book: gate opens
    agent._breaker_flag.set()                    # guard tripped the breaker
    with pytest.raises(CycleHalted), agent._broker_exec():
        pass
    # _run_one_cycle turns it into a halted result, not an agent_error.
    agent._run_cycle_body = lambda: (_ for _ in ()).throw(CycleHalted("x"))
    assert agent._run_one_cycle() == {"ok": True, "halted": True, "reason": "x"}
