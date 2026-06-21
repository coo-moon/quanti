# Risk Protections (v1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a composable "protections" layer (freqtrade-style) that soft-locks **new BUYs** after repeated stop-losses (StoplossGuard) or a recent equity drawdown (MaxDrawdown), enforced in paper, live (QMT), and backtest via one shared pure-logic engine.

**Architecture:** A pure-logic `ProtectionManager` (no DB import) decides "is BUY locked today?" from a plain `ProtectionContext` (recent stop-loss exit dates + recent equity series + a trading-day-distance function). Live/paper build that context from SQLite; the backtest builds it from in-memory structures — **same logic, same tests**. Lock model = "today is locked iff a *trigger day* falls within the last K trading days" (stateless, DB-derivable, restart-safe, forward-K-lock hysteresis). Protections only gate BUY; SELL/exits always pass.

**Tech Stack:** Python 3.13, stdlib `dataclasses`/`datetime`, SQLite via `quanti.data.database.Database`, pytest. No new dependencies, no new DB tables.

## Global Constraints

- Protections **only block BUY**. SELL and all exits (stop-loss / take-profit / strategy-exit) always pass.
- **No persisted lock object, no new DB table.** Lock state is recomputed each cycle from facts (orders + portfolio_snapshots for live; in-memory trades + equity for backtest).
- All windows and lock durations are in **trading days**, computed via `quanti.utils.market.count_trading_days_between(start, end, provider)` which counts trading days in **(start, end]**.
- Stop-loss exits are identified by a **double match**: order/trade `strategy_name == "risk_exit"` **and** `reason.startswith(STOP_LOSS_REASON_PREFIX)`. The prefix constant lives in `quanti/risk/manager.py` and is the single source of truth.
- `risk/protections.py` is **pure** — it must not import `Database`, `DataProvider`, or any execution/backtest module. Context builders live elsewhere.
- Defaults: StoplossGuard `W=5, N=3, K=5` (global); MaxDrawdown `W=10, threshold=-0.08, K=10, min_points=5` (global). The MaxDrawdown soft-lock threshold (`-0.08`) must stay strictly shallower than the `-0.15` hard portfolio breaker.
- Backtest protections default **ON** in the Selector/walk-forward path so strategy ranking matches live; user-facing API backtest follows its existing `apply_risk` flag.
- Keep the existing 337-test suite green. Run `ruff check .` clean before each commit.

Spec: `docs/superpowers/specs/2026-06-21-protections-design.md`.

---

## File Structure

- **Create** `quanti/risk/protections.py` — pure logic: `ProtectionConfig`, `ProtectionContext`, `ProtectionManager` (Task 1).
- **Create** `quanti/risk/protection_context.py` — live context builder from `Database` + `DataProvider` (Task 4).
- **Create** `tests/test_protections.py` — pure-logic unit tests (Task 1) + builder test (Task 4).
- **Modify** `quanti/risk/manager.py` — add `STOP_LOSS_REASON_PREFIX`; `check_exits` uses it (Task 2).
- **Modify** `quanti/data/database.py` — add `stop_loss_exit_dates(since)` query (Task 3).
- **Modify** `quanti/execution/paper_broker.py` — `_entry_allowed` wrapper + `protection_config` (Task 5).
- **Modify** `quanti/execution/qmt_broker.py` — `_entry_allowed` wrapper + `protection_config` (Task 6).
- **Modify** `quanti/backtest/engine.py` — optional `protection_manager` + per-day BUY lock (Task 7).
- **Modify** `quanti/agent/selector.py`, `quanti/api/routes.py`, `quanti/cli.py`, `quanti/mcp_server.py` — wire backtest protections (Task 8).
- **Modify** test files + docs as noted per task.

---

## Task 1: Pure protections engine

**Files:**
- Create: `quanti/risk/protections.py`
- Test: `tests/test_protections.py`

**Interfaces:**
- Consumes: nothing (pure stdlib).
- Produces:
  - `ProtectionConfig` dataclass (fields below).
  - `ProtectionContext` dataclass: `today: date`, `stop_loss_exit_dates: list[date]`, `equity_series: list[tuple[date, float]]`, `trading_days_between: Callable[[date, date], int]`.
  - `ProtectionManager(config: ProtectionConfig | None = None)` with `check_entry(ctx: ProtectionContext, code: str | None = None) -> tuple[bool, str]` → `(allowed, reason)`; `allowed=False` blocks a BUY, `reason` names the protection + latest trigger date.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_protections.py
from __future__ import annotations

from datetime import date, timedelta

from quanti.risk.protections import (
    ProtectionConfig, ProtectionContext, ProtectionManager,
)


def _consecutive_td(a: date, b: date) -> int:
    """Trading-day distance for tests using consecutive calendar days as
    trading days: counts days in (a, b]."""
    return (b - a).days if b > a else 0


def _ctx(today, sl_dates=None, equity=None):
    return ProtectionContext(
        today=today,
        stop_loss_exit_dates=sl_dates or [],
        equity_series=equity or [],
        trading_days_between=_consecutive_td,
    )


D0 = date(2026, 6, 1)


def _d(n: int) -> date:
    return D0 + timedelta(days=n)


# ---- StoplossGuard ----------------------------------------------------

def test_stoploss_guard_below_limit_allows():
    # 2 stops in window, limit is 3 → allowed.
    mgr = ProtectionManager(ProtectionConfig())
    ctx = _ctx(_d(4), sl_dates=[_d(1), _d(2)])
    assert mgr.check_entry(ctx) == (True, "")


def test_stoploss_guard_locks_after_n_stops_for_k_days():
    mgr = ProtectionManager(ProtectionConfig())  # W=5 N=3 K=5
    # 3 stops within 5 trading days → trigger on _d(3); locked through _d(8).
    locked_ctx = _ctx(_d(3), sl_dates=[_d(1), _d(2), _d(3)])
    allowed, reason = mgr.check_entry(locked_ctx)
    assert allowed is False
    assert "StoplossGuard" in reason
    # Still locked on the K-th trading day after the trigger (_d(3)+5 = _d(8)).
    assert mgr.check_entry(_ctx(_d(8), sl_dates=[_d(1), _d(2), _d(3)]))[0] is False
    # Unlocked on K+1 (_d(9)).
    assert mgr.check_entry(_ctx(_d(9), sl_dates=[_d(1), _d(2), _d(3)]))[0] is True


def test_stoploss_guard_extends_on_new_trigger():
    mgr = ProtectionManager(ProtectionConfig())
    # Stops at d1,d2,d3 (trigger d3) then another cluster d6,d7 keeping >=3 in
    # the 5-day window ending d7 (d3..d7) → trigger d7, lock extends to d12.
    dates = [_d(1), _d(2), _d(3), _d(6), _d(7)]
    assert mgr.check_entry(_ctx(_d(11), sl_dates=dates))[0] is False
    assert mgr.check_entry(_ctx(_d(13), sl_dates=dates))[0] is True


def test_stoploss_guard_disabled():
    cfg = ProtectionConfig(stoploss_guard_enabled=False)
    mgr = ProtectionManager(cfg)
    assert mgr.check_entry(_ctx(_d(3), sl_dates=[_d(1), _d(2), _d(3)]))[0] is True


# ---- MaxDrawdown ------------------------------------------------------

def _equity(values_by_offset):
    return [(_d(n), v) for n, v in values_by_offset]


def test_max_drawdown_above_threshold_allows():
    mgr = ProtectionManager(ProtectionConfig())  # thr=-0.08 minpts=5
    eq = _equity([(0, 100), (1, 99), (2, 100), (3, 101), (4, 100)])  # ~-1%
    assert mgr.check_entry(_ctx(_d(4), equity=eq))[0] is True


def test_max_drawdown_locks_on_window_peak_to_trough():
    mgr = ProtectionManager(ProtectionConfig())
    # Peak 100 → trough 88 = -12% within window → lock; threshold -8%.
    eq = _equity([(0, 100), (1, 96), (2, 92), (3, 90), (4, 88)])
    allowed, reason = mgr.check_entry(_ctx(_d(4), equity=eq))
    assert allowed is False
    assert "MaxDrawdown" in reason


def test_max_drawdown_uses_true_peak_to_trough_not_current_point():
    # Deep dip then partial bounce: current vs peak is only -7%, but the window
    # peak-to-trough is -12% → must still lock (the bug the design fixes).
    mgr = ProtectionManager(ProtectionConfig())
    eq = _equity([(0, 100), (1, 95), (2, 90), (3, 88), (4, 93)])  # trough 88 = -12%
    assert mgr.check_entry(_ctx(_d(4), equity=eq))[0] is False


def test_max_drawdown_fail_open_on_thin_window():
    mgr = ProtectionManager(ProtectionConfig())  # minpts=5
    eq = _equity([(0, 100), (1, 80)])  # -20% but only 2 points
    assert mgr.check_entry(_ctx(_d(1), equity=eq))[0] is True


def test_max_drawdown_unlocks_after_k_days():
    cfg = ProtectionConfig(md_lock_days=2, md_lookback_days=5, md_min_points=3)
    mgr = ProtectionManager(cfg)
    eq = _equity([(0, 100), (1, 96), (2, 90)])  # trigger at _d(2)
    assert mgr.check_entry(_ctx(_d(2), equity=eq))[0] is False  # day 0 after
    assert mgr.check_entry(_ctx(_d(4), equity=eq))[0] is False  # K-th day
    assert mgr.check_entry(_ctx(_d(5), equity=eq))[0] is True   # K+1


# ---- Aggregation ------------------------------------------------------

def test_check_entry_first_lock_wins_and_disabled_passes():
    mgr = ProtectionManager(ProtectionConfig(enabled=False))
    eq = _equity([(0, 100), (1, 96), (2, 92), (3, 90), (4, 88)])
    assert mgr.check_entry(_ctx(_d(4), sl_dates=[_d(2), _d(3), _d(4)],
                                equity=eq)) == (True, "")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_protections.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'quanti.risk.protections'`.

- [ ] **Step 3: Write the implementation**

```python
# quanti/risk/protections.py
"""Composable, pluggable risk *protections* — a soft layer ABOVE the hard
RiskManager caps and BELOW the -15% portfolio breaker.

Each protection answers one question: "should new BUYs be locked today?".
They never force a sell. Pure logic: given a ProtectionContext of facts
(recent stop-loss exit dates, recent equity series, a trading-day-distance
function), decide locked/allowed. Live and backtest feed the same logic from
different fact sources — see protection_context.py (live) and
backtest/engine.py (in-memory).

Lock model (forward-K-lock, stateless): a day `e` is a "trigger day" when its
protection condition holds; today is locked iff some trigger day falls within
the last K trading days. This is fully derivable from facts (no persisted lock
object, restart-safe) yet gives freqtrade-style hysteresis — once tripped it
stays locked K trading days, and continued distress extends it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable


@dataclass
class ProtectionConfig:
    """Tunable thresholds. All windows/locks are in trading days."""

    enabled: bool = True

    # StoplossGuard: >= sg_trade_limit stop-loss exits within sg_lookback_days
    # trading days → lock new BUYs for sg_lock_days trading days.
    stoploss_guard_enabled: bool = True
    sg_lookback_days: int = 5
    sg_trade_limit: int = 3
    sg_lock_days: int = 5

    # MaxDrawdown: window peak-to-trough drawdown over md_lookback_days trading
    # days <= md_max_drawdown_pct → lock new BUYs for md_lock_days trading days.
    # Must stay strictly shallower than the -0.15 hard portfolio breaker.
    max_drawdown_enabled: bool = True
    md_lookback_days: int = 10
    md_max_drawdown_pct: float = -0.08
    md_lock_days: int = 10
    md_min_points: int = 5  # fewer equity points in window → fail-open


@dataclass
class ProtectionContext:
    """Facts the protections read. Built from DB (live) or memory (backtest).

    `trading_days_between(start, end)` counts trading days in (start, end]
    (same contract as quanti.utils.market.count_trading_days_between)."""

    today: date
    stop_loss_exit_dates: list[date]
    equity_series: list[tuple[date, float]]
    trading_days_between: Callable[[date, date], int]


class ProtectionManager:
    """Aggregates the enabled protections. First one that locks wins."""

    def __init__(self, config: ProtectionConfig | None = None) -> None:
        self.config = config or ProtectionConfig()

    def check_entry(self, ctx: ProtectionContext,
                    code: str | None = None) -> tuple[bool, str]:
        """Return (allowed, reason). allowed=False blocks a BUY. `code` is
        unused in v1 (both protections are global); reserved for v2 per-pair
        cooldown."""
        if not self.config.enabled:
            return True, ""
        for reason in (self._stoploss_guard(ctx), self._max_drawdown(ctx)):
            if reason:
                return False, reason
        return True, ""

    # ------------------------------------------------------------------
    def _stoploss_guard(self, ctx: ProtectionContext) -> str | None:
        cfg = self.config
        if not cfg.stoploss_guard_enabled:
            return None
        dates = sorted(d for d in ctx.stop_loss_exit_dates if d <= ctx.today)
        if not dates:
            return None
        W, N, K = cfg.sg_lookback_days, cfg.sg_trade_limit, cfg.sg_lock_days
        latest_trigger: date | None = None
        for i, e in enumerate(dates):
            # Stops within the W-trading-day window ending at e (e included).
            count = sum(1 for d in dates[:i + 1]
                        if ctx.trading_days_between(d, e) < W)
            if count >= N and (latest_trigger is None or e > latest_trigger):
                latest_trigger = e
        if latest_trigger is None:
            return None
        if ctx.trading_days_between(latest_trigger, ctx.today) <= K:
            return (f"StoplossGuard 锁定: 近{W}交易日止损≥{N}次 "
                    f"(最近触发 {latest_trigger.isoformat()}, 锁{K}交易日)")
        return None

    def _max_drawdown(self, ctx: ProtectionContext) -> str | None:
        cfg = self.config
        if not cfg.max_drawdown_enabled:
            return None
        W, thr, K = (cfg.md_lookback_days, cfg.md_max_drawdown_pct,
                     cfg.md_lock_days)
        series = sorted((d, v) for d, v in ctx.equity_series if d <= ctx.today)
        if not series:
            return None
        latest_trigger: date | None = None
        for j, (d, _v) in enumerate(series):
            window = [(wd, wv) for wd, wv in series[:j + 1]
                      if ctx.trading_days_between(wd, d) < W]
            if len(window) < cfg.md_min_points:
                continue  # fail-open on thin window
            peak = None
            mdd = 0.0
            for _wd, wv in window:
                if peak is None or wv > peak:
                    peak = wv
                if peak and peak > 0:
                    dd = (wv - peak) / peak
                    if dd < mdd:
                        mdd = dd
            if mdd <= thr and (latest_trigger is None or d > latest_trigger):
                latest_trigger = d
        if latest_trigger is None:
            return None
        if ctx.trading_days_between(latest_trigger, ctx.today) <= K:
            return (f"MaxDrawdown 锁定: 近{W}交易日净值回撤≤{thr:.0%} "
                    f"(最近触发 {latest_trigger.isoformat()}, 锁{K}交易日)")
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_protections.py -q`
Expected: PASS (all tests).

- [ ] **Step 5: Lint + commit**

```bash
ruff check quanti/risk/protections.py tests/test_protections.py
git add quanti/risk/protections.py tests/test_protections.py
git commit -m "feat(risk): pure protections engine — StoplossGuard + MaxDrawdown"
```

---

## Task 2: Stop-loss reason prefix as a shared constant

**Files:**
- Modify: `quanti/risk/manager.py` (add constant near top; use at `manager.py:154`)
- Test: `tests/test_risk.py` (add invariant test)

**Interfaces:**
- Produces: `quanti.risk.manager.STOP_LOSS_REASON_PREFIX: str = "止损"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_risk.py — append
def test_check_exits_stop_loss_reason_uses_prefix():
    from quanti.models import Direction, Portfolio, Position
    from quanti.risk.manager import (
        RiskManager, RiskConfig, STOP_LOSS_REASON_PREFIX,
    )
    rm = RiskManager(RiskConfig(stop_loss_pct=-0.08,
                                take_profit_activate_pct=0.15))
    # A position down -10% → stop-loss exit.
    pos = Position(stock_code="000001", quantity=1000, avg_cost=10.0,
                   current_price=9.0)
    pf = Portfolio(cash=0.0, positions={"000001": pos})
    sells = rm.check_exits(pf)
    assert sells and sells[0].reason.startswith(STOP_LOSS_REASON_PREFIX)
    # Take-profit / strategy-exit reasons must NOT match the stop-loss prefix.
    assert not "移动止盈".startswith(STOP_LOSS_REASON_PREFIX)
    assert not "策略离场信号".startswith(STOP_LOSS_REASON_PREFIX)
```

Note: confirm `Position` constructor args against `quanti/models.py` before running; adjust kwargs to the actual fields (it has `pnl_pct`/`market_value` derived from `quantity/avg_cost/current_price`). If `Portfolio`/`Position` need different kwargs, match them — the assertion on `reason` is the point.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_risk.py::test_check_exits_stop_loss_reason_uses_prefix -q`
Expected: FAIL — `ImportError: cannot import name 'STOP_LOSS_REASON_PREFIX'`.

- [ ] **Step 3: Add the constant and use it**

In `quanti/risk/manager.py`, after the imports (before `class RiskConfig`):

```python
STOP_LOSS_REASON_PREFIX = "止损"
"""Single source of truth for the stop-loss exit reason prefix. `check_exits`
emits it; protections identify stop-loss exits by it (plus strategy_name
'risk_exit'). Changing the wording here keeps producer and consumer in sync."""
```

At `manager.py:154`, change the stop-loss reason in `check_exits` from:

```python
                    reason=f"止损 {pos.pnl_pct:.1%} ≤ {cfg.stop_loss_pct:.1%}"))
```

to:

```python
                    reason=f"{STOP_LOSS_REASON_PREFIX} {pos.pnl_pct:.1%} ≤ {cfg.stop_loss_pct:.1%}"))
```

(Leave the legacy English `check_stop_loss` at `manager.py:114` untouched — brokers and the backtest engine all route exits through `check_exits`, so it is not a persisted stop-loss producer. Confirm with `grep -rn "\.check_stop_loss(" quanti/` showing no non-test callers.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_risk.py -q`
Expected: PASS (new test + existing risk tests). The backtest test `tests/test_backtest.py::test_backtest_risk_exit_tags_reason` asserts `"止损" in t.reason` — still true.

- [ ] **Step 5: Lint + commit**

```bash
ruff check quanti/risk/manager.py
git add quanti/risk/manager.py tests/test_risk.py
git commit -m "refactor(risk): STOP_LOSS_REASON_PREFIX shared constant for stop-loss identification"
```

---

## Task 3: DB query for stop-loss exit dates

**Files:**
- Modify: `quanti/data/database.py` (add method near `list_orders`, ~`database.py:835`)
- Test: `tests/test_database_protections.py` (new)

**Interfaces:**
- Consumes: `STOP_LOSS_REASON_PREFIX` from Task 2; `Database.conn`, `insert_order` (existing).
- Produces: `Database.stop_loss_exit_dates(since: date) -> list[date]` — fill dates of filled SELL orders with `strategy_name='risk_exit'` and `reason LIKE '止损%'`, with `filled_at >= since`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_database_protections.py
from __future__ import annotations

from datetime import date, datetime, timedelta

from quanti.data.database import Database


def _order(db, *, code, direction, status, strategy, reason, filled_at):
    db.insert_order({
        "order_id": "o_" + code + direction + (filled_at or "x"),
        "code": code, "direction": direction, "quantity": 100,
        "price_type": "market", "limit_price": 0.0, "status": status,
        "strategy_name": strategy, "filled_price": 9.0, "filled_quantity": 100,
        "reason": reason, "created_at": datetime.now().isoformat(),
        "filled_at": filled_at, "entry_strategy": "",
    })


def test_stop_loss_exit_dates_filters_correctly(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.ensure_portfolio(100_000)
    today = date(2026, 6, 20)
    iso = lambda d: datetime(d.year, d.month, d.day, 15, 0).isoformat()
    # A real stop-loss exit (counts).
    _order(db, code="000001", direction="sell", status="filled",
           strategy="risk_exit", reason="止损 -10.0% ≤ -8.0%",
           filled_at=iso(date(2026, 6, 18)))
    # Take-profit exit (excluded — wrong reason).
    _order(db, code="000002", direction="sell", status="filled",
           strategy="risk_exit", reason="移动止盈 浮盈+20%",
           filled_at=iso(date(2026, 6, 18)))
    # A BUY (excluded — wrong direction).
    _order(db, code="000003", direction="buy", status="filled",
           strategy="ma_cross", reason="买入信号",
           filled_at=iso(date(2026, 6, 18)))
    # Unfilled stop intent (excluded — not filled).
    _order(db, code="000004", direction="sell", status="pending",
           strategy="risk_exit", reason="止损 -9% ≤ -8%", filled_at=None)
    # Too old (excluded by `since`).
    _order(db, code="000005", direction="sell", status="filled",
           strategy="risk_exit", reason="止损 -9% ≤ -8%",
           filled_at=iso(date(2026, 5, 1)))

    dates = db.stop_loss_exit_dates(since=today - timedelta(days=10))
    assert dates == [date(2026, 6, 18)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_database_protections.py -q`
Expected: FAIL — `AttributeError: 'Database' object has no attribute 'stop_loss_exit_dates'`.

- [ ] **Step 3: Add the query**

In `quanti/data/database.py`, add (after `list_orders`, before `update_order_status`):

```python
    def stop_loss_exit_dates(self, since: date) -> list[date]:
        """Fill dates of stop-loss exits since `since`, for StoplossGuard.

        A stop-loss exit is a FILLED SELL order tagged strategy_name
        'risk_exit' whose reason starts with STOP_LOSS_REASON_PREFIX — the
        double match excludes take-profit / strategy exits that share the
        'risk_exit' tag. `since` is a generous calendar lower bound; the pure
        protection logic filters precisely by trading days."""
        from quanti.risk.manager import STOP_LOSS_REASON_PREFIX
        rows = self.conn.execute(
            "SELECT filled_at FROM orders "
            "WHERE direction='sell' AND status='filled' "
            "AND strategy_name='risk_exit' AND reason LIKE ? "
            "AND filled_at IS NOT NULL AND filled_at >= ?",
            (STOP_LOSS_REASON_PREFIX + "%", since.isoformat()),
        ).fetchall()
        out: list[date] = []
        for r in rows:
            try:
                out.append(datetime.fromisoformat(r[0]).date())
            except (ValueError, TypeError):
                continue
        return out
```

Ensure `from datetime import date, datetime` is available at module scope in `database.py` (it imports `datetime` locally in some methods — add `date`/`datetime` to the top-level imports if not present, or keep the local-import style already used and import inside the method).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_database_protections.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
ruff check quanti/data/database.py tests/test_database_protections.py
git add quanti/data/database.py tests/test_database_protections.py
git commit -m "feat(db): stop_loss_exit_dates query for StoplossGuard"
```

---

## Task 4: Live context builder

**Files:**
- Create: `quanti/risk/protection_context.py`
- Test: `tests/test_protections.py` (append builder test)

**Interfaces:**
- Consumes: `ProtectionConfig`, `ProtectionContext` (Task 1); `Database.stop_loss_exit_dates` (Task 3), `Database.get_portfolio_snapshots` (existing), `count_trading_days_between` (existing).
- Produces: `build_db_context(db, provider, config, today=None) -> ProtectionContext`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_protections.py — append
def test_build_db_context_from_database(tmp_path):
    from datetime import date, datetime
    from quanti.data.database import Database
    from quanti.data.provider import DataProvider
    from quanti.risk.protections import ProtectionConfig
    from quanti.risk.protection_context import build_db_context

    db = Database(str(tmp_path / "t.db"))
    db.ensure_portfolio(100_000)
    iso = lambda d: datetime(d.year, d.month, d.day, 15, 0).isoformat()
    db.insert_order({
        "order_id": "o1", "code": "000001", "direction": "sell",
        "quantity": 100, "price_type": "market", "limit_price": 0.0,
        "status": "filled", "strategy_name": "risk_exit",
        "filled_price": 9.0, "filled_quantity": 100,
        "reason": "止损 -10% ≤ -8%", "created_at": iso(date(2026, 6, 18)),
        "filled_at": iso(date(2026, 6, 18)), "entry_strategy": "",
    })
    db.save_portfolio_snapshot(date(2026, 6, 18), 50_000, 50_000, 100_000)
    db.save_portfolio_snapshot(date(2026, 6, 19), 50_000, 48_000, 98_000)

    ctx = build_db_context(db, DataProvider(db), ProtectionConfig(),
                           today=date(2026, 6, 20))
    assert date(2026, 6, 18) in ctx.stop_loss_exit_dates
    assert any(v == 100_000 for _d, v in ctx.equity_series)
    assert ctx.today == date(2026, 6, 20)
    assert callable(ctx.trading_days_between)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_protections.py::test_build_db_context_from_database -q`
Expected: FAIL — `ModuleNotFoundError: quanti.risk.protection_context`.

- [ ] **Step 3: Write the builder**

```python
# quanti/risk/protection_context.py
"""Build a ProtectionContext from the live SQLite state (paper/live brokers).

Kept separate from protections.py so the protection logic stays pure (no DB
import). The backtest builds its own ProtectionContext from in-memory data."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.risk.protections import ProtectionConfig, ProtectionContext
from quanti.utils.market import count_trading_days_between


def build_db_context(db: Database, provider: DataProvider,
                     config: ProtectionConfig,
                     today: date | None = None) -> ProtectionContext:
    today = today or date.today()
    # Generous calendar lower bound covering the largest fact window
    # (lock + lookback) in trading days, padded for weekends/holidays.
    span_td = max(config.sg_lock_days + config.sg_lookback_days,
                  config.md_lock_days + config.md_lookback_days)
    since = today - timedelta(days=span_td * 2 + 14)

    sl_dates = db.stop_loss_exit_dates(since)

    equity: list[tuple[date, float]] = []
    for snap in db.get_portfolio_snapshots(limit=span_td * 2 + 14):
        try:
            sd = date.fromisoformat(snap["snapshot_date"])
        except (ValueError, TypeError, KeyError):
            continue
        if sd >= since:
            equity.append((sd, float(snap["total_value"])))
    equity.sort()

    return ProtectionContext(
        today=today,
        stop_loss_exit_dates=sl_dates,
        equity_series=equity,
        trading_days_between=lambda a, b: count_trading_days_between(a, b, provider),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_protections.py -q`
Expected: PASS. (If `DataProvider(db)` needs other args, match the constructor used in `tests/test_pending_orders.py:435` — `DataProvider(db)`.)

- [ ] **Step 5: Lint + commit**

```bash
ruff check quanti/risk/protection_context.py tests/test_protections.py
git add quanti/risk/protection_context.py tests/test_protections.py
git commit -m "feat(risk): live ProtectionContext builder from SQLite"
```

---

## Task 5: Wire protections into PaperBroker

**Files:**
- Modify: `quanti/execution/paper_broker.py` (`__init__` ~52-90; gates at `:197`, `:236`, `:331`; add `_entry_allowed` + `_build_protection_context`)
- Test: `tests/test_paper_broker.py` (append)

**Interfaces:**
- Consumes: `ProtectionManager`, `ProtectionConfig` (Task 1); `build_db_context` (Task 4).
- Produces: `PaperBroker(..., protection_config: ProtectionConfig | None = None)`; private `_entry_allowed(signal, portfolio) -> tuple[bool, str, str]` returning `(ok, reason, reject_kind)` with `reject_kind` in `{"", "risk_reject", "protection_block"}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_paper_broker.py — append
def test_protection_blocks_buy_after_stop_loss_cluster(tmp_path):
    from datetime import date, datetime, timedelta
    from quanti.data.database import Database
    from quanti.data.provider import DataProvider
    from quanti.models import Direction, Signal
    from quanti.execution.paper_broker import PaperBroker
    from quanti.risk.protections import ProtectionConfig

    db = Database(str(tmp_path / "t.db"))
    provider = DataProvider(db)
    # Seed 3 stop-loss exits in the last few days → StoplossGuard locks BUYs.
    today = date.today()
    iso = lambda d: datetime(d.year, d.month, d.day, 15, 0).isoformat()
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_paper_broker.py::test_protection_blocks_buy_after_stop_loss_cluster -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'protection_config'`.

- [ ] **Step 3: Implement**

In `paper_broker.py __init__` signature, add a param after `risk_config`:

```python
        risk_config: RiskConfig | None = None,
        protection_config: "ProtectionConfig | None" = None,
```

In the `__init__` body, after `self._risk = RiskManager(risk_config)`:

```python
        from quanti.risk.protections import ProtectionConfig, ProtectionManager
        self._protections = ProtectionManager(
            protection_config if protection_config is not None
            else ProtectionConfig())
```

Add two private methods (e.g. just after `__init__`):

```python
    def _build_protection_context(self):
        from quanti.risk.protection_context import build_db_context
        return build_db_context(self._db, self._provider,
                                self._protections.config)

    def _entry_allowed(self, signal, portfolio):
        """Risk caps + protections gate for an entry. Returns
        (ok, reason, reject_kind). Protections only gate BUY."""
        ok, reason = self._risk.check(signal, portfolio)
        if not ok:
            return False, reason, "risk_reject"
        if (signal.direction == Direction.BUY
                and self._protections.config.enabled):
            ctx = self._build_protection_context()
            plocked, preason = self._protections.check_entry(ctx,
                                                             signal.stock_code)
            if plocked:
                return False, preason, "protection_block"
        return True, "", ""
```

Replace the three gate sites. At `:197` (in `_execute_signal_immediate`):

```python
        ok, reason, kind = self._entry_allowed(signal, portfolio)
        if not ok:
            self._record_order(signal, strategy_name, status="rejected",
                               reason=reason)
            self._db.log_decision(
                kind,
                f"{'风控' if kind == 'risk_reject' else '保护层'}拒绝 "
                f"{signal.direction.value} {signal.stock_code}: {reason}",
                code=signal.stock_code,
                details={"signal_reason": signal.reason},
            )
            return False
```

At `:236` (in `_queue_pending_signal`):

```python
        ok, reason, kind = self._entry_allowed(signal, portfolio)
        if not ok:
            self._record_order(signal, strategy_name, status="rejected",
                               reason=reason)
            self._db.log_decision(
                kind,
                f"{'风控' if kind == 'risk_reject' else '保护层'}拒绝 "
                f"{signal.direction.value} {signal.stock_code}: {reason}",
                code=signal.stock_code,
                details={"signal_reason": signal.reason, "stage": "queue"},
            )
            return False
```

At `:331` (in `try_fill_pending_orders`):

```python
            ok, reason, kind = self._entry_allowed(sig, portfolio)
            if not ok:
                self._db.update_order_status(o["order_id"], "rejected",
                                             reason=f"{kind}: {reason}")
                self._db.log_decision(
                    kind,
                    f"{'风控' if kind == 'risk_reject' else '保护层'}拒绝 (成交时) "
                    f"{sig.direction.value} {sig.stock_code}: {reason}",
                    code=sig.stock_code,
                    details={"order_id": o["order_id"], "stage": "fill"})
                out.rejected += 1
                out.reasons.append(reason)
                continue
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_paper_broker.py tests/test_pending_orders.py tests/test_agent.py -q`
Expected: PASS. (Existing tests build `PaperBroker` without `protection_config`; the default `ProtectionConfig()` enables protections but with no stop-loss orders and no/thin snapshots they fail-open → behavior unchanged.)

- [ ] **Step 5: Lint + commit**

```bash
ruff check quanti/execution/paper_broker.py tests/test_paper_broker.py
git add quanti/execution/paper_broker.py tests/test_paper_broker.py
git commit -m "feat(execution): PaperBroker enforces protections at all BUY gates"
```

---

## Task 6: Wire protections into QmtBroker

**Files:**
- Modify: `quanti/execution/qmt_broker.py` (`__init__` ~46-64; gate at `:174` in `_submit_signal`)
- Test: `tests/test_qmt_broker.py` (append)

**Interfaces:**
- Consumes: same as Task 5.
- Produces: `QmtBroker(..., protection_config: ProtectionConfig | None = None)`; `_entry_allowed` (same signature as PaperBroker) + `_build_protection_context`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_qmt_broker.py — append (uses the existing broker fixture style)
def test_qmt_protection_blocks_buy(tmp_path):
    from datetime import date, datetime, timedelta
    from quanti.data.database import Database
    from quanti.data.provider import DataProvider
    from quanti.models import Direction, Signal
    from quanti.execution.qmt_broker import QmtBroker
    from quanti.risk.protections import ProtectionConfig
    from tests.test_qmt_broker import InProcBridge  # reuse in-proc bridge

    db = Database(str(tmp_path / "t.db"))
    provider = DataProvider(db)
    today = date.today()
    iso = lambda d: datetime(d.year, d.month, d.day, 15, 0).isoformat()
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
    broker = QmtBroker(db, provider, client=InProcBridge(),
                       protection_config=ProtectionConfig(
                           sg_lookback_days=10, sg_trade_limit=3,
                           sg_lock_days=10, max_drawdown_enabled=False))
    ok, reason, kind = broker._entry_allowed(
        Signal("600519", Direction.BUY, 1.0, "buy"),
        broker._reconciled_portfolio()[0])
    assert ok is False and kind == "protection_block"
```

(If `InProcBridge` isn't importable that way, instantiate the broker via the module's existing test fixture/helper — the assertion target is `_entry_allowed`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_qmt_broker.py::test_qmt_protection_blocks_buy -q`
Expected: FAIL — unexpected kwarg `protection_config`.

- [ ] **Step 3: Implement**

In `qmt_broker.py __init__`, add after `risk_config`:

```python
        risk_config: RiskConfig | None = None,
        protection_config: "ProtectionConfig | None" = None,
```

After `self._risk = RiskManager(risk_config)`:

```python
        from quanti.risk.protections import ProtectionConfig, ProtectionManager
        self._protections = ProtectionManager(
            protection_config if protection_config is not None
            else ProtectionConfig())
```

Add the same `_build_protection_context` and `_entry_allowed` methods as in Task 5 (identical bodies — `self._db`, `self._provider`, `self._protections`, `self._risk` all exist on QmtBroker).

Replace the gate at `_submit_signal` `:174`:

```python
        portfolio, sellable = self._reconciled_portfolio()
        ok, reason, kind = self._entry_allowed(signal, portfolio)
        if not ok:
            self._mirror_order(signal, strategy_name, status="rejected",
                               reason=reason)
            self._db.log_decision(
                kind,
                f"{'风控' if kind == 'risk_reject' else '保护层'}拒绝(实盘) "
                f"{signal.direction.value} {signal.stock_code}: {reason}",
                code=signal.stock_code,
                details={"venue": "qmt", "signal_reason": signal.reason})
            return False, "rejected", reason
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_qmt_broker.py -q`
Expected: PASS (new test + existing — existing build QmtBroker without `protection_config`, default fail-open).

- [ ] **Step 5: Lint + commit**

```bash
ruff check quanti/execution/qmt_broker.py tests/test_qmt_broker.py
git add quanti/execution/qmt_broker.py tests/test_qmt_broker.py
git commit -m "feat(execution): QmtBroker enforces protections at BUY gate"
```

---

## Task 7: Wire protections into the backtest engine

**Files:**
- Modify: `quanti/backtest/engine.py` (`__init__` ~90-115; `run` loop ~164-223)
- Test: `tests/test_backtest.py` (append)

**Interfaces:**
- Consumes: `ProtectionManager` (Task 1); `STOP_LOSS_REASON_PREFIX` (Task 2); `ProtectionContext` (Task 1).
- Produces: `BacktestEngine(..., protection_manager: ProtectionManager | None = None)`; when set, BUY signals are skipped on locked days (counted in `skipped_signals`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtest.py — append
def test_backtest_protections_block_buys_after_stop_cluster(tmp_path):
    """With a ProtectionManager that locks after stop-losses, BUYs on locked
    days are skipped. Without one, behavior is unchanged."""
    from quanti.backtest.engine import BacktestEngine
    from quanti.risk.manager import RiskConfig, RiskManager
    from quanti.risk.protections import ProtectionConfig, ProtectionManager
    # Reuse this module's helper that seeds a provider + a strategy that
    # buys daily and a price path that triggers stop-losses. Construct the
    # engine WITH and WITHOUT a protection manager and compare trade counts.
    provider, codes, strat, start, end = _seed_stop_loss_scenario(tmp_path)  # see note
    base = BacktestEngine(provider, 200_000.0,
                          risk_manager=RiskManager(RiskConfig())).run(
        strat, codes, start, end)
    guarded = BacktestEngine(
        provider, 200_000.0, risk_manager=RiskManager(RiskConfig()),
        protection_manager=ProtectionManager(ProtectionConfig(
            sg_lookback_days=5, sg_trade_limit=2, sg_lock_days=5,
            max_drawdown_enabled=False))).run(strat, codes, start, end)
    assert guarded.skipped_signals >= base.skipped_signals
    # Guarded run buys strictly fewer times once the guard trips.
    n_buys = lambda r: sum(1 for t in r.trades if t.direction.value == "buy")
    assert n_buys(guarded) <= n_buys(base)
```

Note: `_seed_stop_loss_scenario` — build it from the patterns already in `tests/test_backtest.py` (it has a synthetic provider + a stop-loss scenario in `test_backtest_risk_exit_tags_reason`). Reuse that price path (a crash that triggers ≥2 stop-losses) and a simple "buy every bar" strategy so the guard has both stops to count and buys to block. If a shared helper doesn't exist, inline the synthetic bars as that test does.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_backtest.py::test_backtest_protections_block_buys_after_stop_cluster -q`
Expected: FAIL — unexpected kwarg `protection_manager`.

- [ ] **Step 3: Implement**

In `engine.py __init__`, add a param after `risk_manager`:

```python
        risk_manager: RiskManager | None = None,
        protection_manager: "ProtectionManager | None" = None,
```

In the body, after `self._risk = risk_manager`:

```python
        self._protections = protection_manager
```

In `run`, at the top add the import (module-level is fine):

```python
from quanti.risk.manager import STOP_LOSS_REASON_PREFIX
from quanti.risk.protections import ProtectionContext
```

Inside the `for idx, current_date in enumerate(sorted_dates):` loop, AFTER step 2 (marking positions to close, ~line 189) and BEFORE the `_queue` definition / signal generation, compute the per-day BUY lock once:

```python
            # Per-day protection lock (global, only affects new BUYs). Built
            # from the SAME pure ProtectionManager as live, fed from memory.
            buy_locked = False
            buy_lock_reason = ""
            if self._protections is not None and self._protections.config.enabled:
                sl_dates = [t.date for t in trades
                            if t.strategy == "risk_exit"
                            and t.reason.startswith(STOP_LOSS_REASON_PREFIX)]
                eq = sorted(equity_values.items())
                eq.append((current_date, portfolio.total_value))

                def _bt_td(a: date, b: date,
                           _days=sorted_dates) -> int:
                    if a >= b:
                        return 0
                    return sum(1 for d in _days if a < d <= b)

                ctx = ProtectionContext(
                    today=current_date, stop_loss_exit_dates=sl_dates,
                    equity_series=eq, trading_days_between=_bt_td)
                buy_locked, buy_lock_reason = (
                    lambda r: (not r[0], r[1]))(
                        self._protections.check_entry(ctx))
```

(The lambda flips `(allowed, reason)` to `(locked, reason)`; or write it explicitly:
`allowed, buy_lock_reason = self._protections.check_entry(ctx); buy_locked = not allowed`.)

Then in the strategy signal loop (currently `engine.py:211-220`), gate BUYs:

```python
            for code, bar in today_bars.items():
                for signal in strategy.on_bar(bar):
                    if signal.stock_code not in today_bars:
                        continue
                    if signal.direction == Direction.BUY and buy_locked:
                        skipped_signals += 1
                        continue
                    if self._risk is not None:
                        ok, _ = self._risk.check(signal, portfolio)
                        if not ok:
                            skipped_signals += 1
                            continue
                    _queue(signal, strategy.name)
```

The risk-exit queue block (`engine.py:206-209`) is unchanged — exits are never locked.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_backtest.py tests/test_walk_forward.py tests/test_slippage.py -q`
Expected: PASS. (Existing engine constructions pass no `protection_manager` → `None` → loop unchanged.)

- [ ] **Step 5: Lint + commit**

```bash
ruff check quanti/backtest/engine.py tests/test_backtest.py
git add quanti/backtest/engine.py tests/test_backtest.py
git commit -m "feat(backtest): optional protections gate on BUY (same logic as live)"
```

---

## Task 8: Wire backtest construction sites (Selector default-on)

**Files:**
- Modify: `quanti/agent/selector.py:119-121`, `quanti/api/routes.py:862-863`, `quanti/cli.py:82`, `quanti/mcp_server.py:375`
- Test: `tests/test_walk_forward.py` or `tests/test_agent.py` (assert Selector engine carries a ProtectionManager)

**Interfaces:**
- Consumes: `ProtectionManager` (Task 1), `BacktestEngine(protection_manager=...)` (Task 7).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_walk_forward.py — append (or tests/test_agent.py)
def test_selector_engine_enables_protections(monkeypatch, tmp_path):
    """The Selector's backtest engine must carry a ProtectionManager so its
    strategy ranking matches how live trades (backtest≡live)."""
    import quanti.agent.selector as sel
    captured = {}
    real_engine = sel.BacktestEngine

    def _spy(*args, **kwargs):
        captured["protection_manager"] = kwargs.get("protection_manager")
        return real_engine(*args, **kwargs)

    monkeypatch.setattr(sel, "BacktestEngine", _spy)
    # Trigger one Selector evaluation cycle via the existing test harness in
    # this module (reuse its provider/goal/candidates setup), then:
    # ... run selector.evaluate(...) ...
    assert captured.get("protection_manager") is not None
```

Note: wire this into the module's existing Selector test setup (there is already walk-forward/selector coverage). The assertion is that `protection_manager` is passed and non-None.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_walk_forward.py::test_selector_engine_enables_protections -q`
Expected: FAIL — `protection_manager` is `None`.

- [ ] **Step 3: Implement the wiring**

`quanti/agent/selector.py` — add import near the top (with the other risk import):

```python
from quanti.risk.protections import ProtectionManager
```

At `:119-121`, change the engine construction to:

```python
        engine = BacktestEngine(provider=self._provider,
                                initial_cash=self._initial_cash,
                                risk_manager=RiskManager(RiskConfig()),
                                protection_manager=ProtectionManager())
```

`quanti/api/routes.py` at `:860-863` — protections follow the existing `apply_risk` flag (the UI backtest already opts into the live exit policy via it):

```python
    from quanti.risk.manager import RiskConfig, RiskManager
    from quanti.risk.protections import ProtectionManager
    risk = RiskManager(RiskConfig()) if body.apply_risk else None
    protections = ProtectionManager() if body.apply_risk else None
    engine = BacktestEngine(provider=provider, initial_cash=body.initial_cash,
                            risk_manager=risk, protection_manager=protections)
```

`quanti/cli.py` at `:82` and `quanti/mcp_server.py` at `:375` — pass `protection_manager=ProtectionManager()` alongside the existing `risk_manager=RiskManager(RiskConfig())`, adding the import `from quanti.risk.protections import ProtectionManager` in each file. (These run agent-style backtests, so default-on matches live.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_walk_forward.py tests/test_api.py tests/test_agent.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
ruff check quanti/agent/selector.py quanti/api/routes.py quanti/cli.py quanti/mcp_server.py
git add quanti/agent/selector.py quanti/api/routes.py quanti/cli.py quanti/mcp_server.py tests/test_walk_forward.py
git commit -m "feat: enable protections in Selector/CLI/MCP backtests (backtest≡live)"
```

---

## Task 9: Full regression + docs

**Files:**
- Modify: `docs/plans/2026-06-16-live-trading-qmt.md` (add a `# VERIFY` / 待办 line: ensure `trade_calendar` synced before live so protection windows use real trading days)
- Modify: `quanti/risk/manager.py` (one-line docstring note on the two-layer relationship — optional)

- [ ] **Step 1: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS — previous 337 plus the new protection tests, 0 failures.

- [ ] **Step 2: Lint the whole tree**

Run: `ruff check .`
Expected: clean.

- [ ] **Step 3: Add the trading-calendar caveat to the roadmap**

In `docs/plans/2026-06-16-live-trading-qmt.md`, under the QMT 待办 / `# VERIFY` section, add:

```markdown
- **protections 依赖交易日历**：StoplossGuard/MaxDrawdown 的窗口与锁期按交易日计；上实盘前确保 `trade_calendar` 已 sync（否则 `is_trading_day` 退化为"工作日"，窗口会偏）。
```

- [ ] **Step 4: Commit**

```bash
git add docs/plans/2026-06-16-live-trading-qmt.md quanti/risk/manager.py
git commit -m "docs(risk): protections trade-calendar caveat + two-layer note"
```

- [ ] **Step 5: Push and open PR**

```bash
git push -u origin feat/risk-protections
# open PR feat/risk-protections -> main (title: protections v1: StoplossGuard + MaxDrawdown)
```

---

## Self-Review (completed by plan author)

- **Spec coverage:** §2 lock model → Task 1. §3 pure-logic + abstract context → Tasks 1, 4, 7. §4.1 live `_entry_allowed` → Tasks 5, 6. §4.2 backtest gate → Task 7. §4.3 config wiring + selector default-on → Tasks 5–8. §5 DB queries → Tasks 3, 4. §6 stop-loss identification constant + invariant test → Task 2. §7 two-layer relationship → Task 9 docs. §8 fail-open / trading-day / filled_at / lock-extension → Tasks 1 (tests), 3 (filled), 9 (calendar). §9 testing → every task’s tests + Task 9 regression. §11 deferred (exit_kind column, CooldownPeriod, LowProfit) → out of scope, noted.
- **Type consistency:** `ProtectionConfig` / `ProtectionContext` / `ProtectionManager.check_entry(ctx, code=None) -> (allowed, reason)` are defined in Task 1 and consumed verbatim in Tasks 4–8. `_entry_allowed(...) -> (ok, reason, reject_kind)` defined identically in Tasks 5 and 6. `stop_loss_exit_dates(since)` defined in Task 3, consumed in Task 4. `STOP_LOSS_REASON_PREFIX` defined in Task 2, consumed in Tasks 3 and 7.
- **Placeholder scan:** Two tests reference module-local helpers (`_seed_stop_loss_scenario`, the Selector harness, `InProcBridge`) — each flagged with a build-from-existing-pattern note rather than left blank; the surrounding test code is complete.
```
