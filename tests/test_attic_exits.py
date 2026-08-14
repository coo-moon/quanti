"""Tests for the attic exit-replay fallback: a holding whose entry-strategy
was retired into strategies/attic keeps its strategy-based exit (replay loads
the attic), while the selector never sees attic strategies."""

from __future__ import annotations

from datetime import date

import pytest

from quanti.data.database import Database
from quanti.execution import exits
from quanti.health import exit_coverage


MAIN_STRAT = """from quanti.models import BarData, Direction, Signal
from quanti.strategy.base import BaseStrategy


class MainActive(BaseStrategy):
    name = "main_active"

    def init(self, config):
        pass

    def on_bar(self, bar):
        return []
"""

ATTIC_STRAT = """from quanti.models import BarData, Direction, Signal
from quanti.strategy.base import BaseStrategy


class RetiredSell(BaseStrategy):
    name = "retired_sell"

    def init(self, config):
        pass

    def on_bar(self, bar):
        return [Signal(stock_code=bar.code, direction=Direction.SELL,
                       strength=1.0, reason="attic exit")]
"""


@pytest.fixture
def dirs(tmp_path):
    """A strategies dir with one active strategy + one attic strategy."""
    root = tmp_path / "strategies"
    attic = root / "attic"
    attic.mkdir(parents=True)
    (root / "main_active.py").write_text(MAIN_STRAT, encoding="utf-8")
    (attic / "retired_sell.py").write_text(ATTIC_STRAT, encoding="utf-8")
    return str(root)


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "e.db"))
    d.initialize()
    yield d
    d.close()


def test_load_strategies_includes_attic(dirs):
    cache = exits.load_strategies(dirs)
    assert "main_active" in cache
    assert "retired_sell" in cache  # attic fallback


class _Bars:
    def get_daily_bars(self, code, start, end):
        from quanti.models import BarData
        return [BarData(code=code, date=date.today(), open=10.0, high=10.5,
                        low=9.5, close=10.0, volume=1e6, amount=1e7,
                        turnover=1.0)]


def test_exit_replay_uses_attic_strategy(db, dirs):
    """The position opened with the retired strategy still gets its strategy
    exit — replay falls back to the attic."""
    strategies = exits.load_strategies(dirs)
    positions = [{"code": "000001", "entry_strategy": "retired_sell"}]
    sells = exits.compute_strategy_exits(_Bars(), strategies, positions, db)
    assert sells.get("000001") == "retired_sell"


def test_warning_only_when_missing_everywhere(db, dirs, caplog):
    import logging
    strategies = exits.load_strategies(dirs)
    positions = [{"code": "000001", "entry_strategy": "gone_forever"}]
    key = ("gone_forever", "000001")
    exits._degraded_warned.pop(key, None)
    with caplog.at_level(logging.WARNING):
        sells = exits.compute_strategy_exits(None, strategies, positions, db)
    exits._degraded_warned.pop(key, None)
    assert sells == {}
    warnings = [r for r in caplog.records if "gone_forever" in r.getMessage()]
    assert len(warnings) == 1


def test_exit_coverage_attic_aware(db, dirs):
    db.upsert_position("000001", 100, 10.0, 10.0, date.today(),
                       entry_strategy="retired_sell")
    report = exit_coverage(db, dirs)
    assert report["ok"] is True
    assert report["degraded"] == []


def test_exit_coverage_degrades_only_when_missing_both(db, dirs):
    db.upsert_position("000001", 100, 10.0, 10.0, date.today(),
                       entry_strategy="gone_forever")
    report = exit_coverage(db, dirs)
    assert report["ok"] is False
    assert len(report["degraded"]) == 1
    assert "strategies/attic" in report["detail"]

