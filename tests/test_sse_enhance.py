"""Tests for the SSE total-return replication strategy (sse_enhance)."""

import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

from quanti.models import BarData, Direction

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "strategies"))
from sse_index_enhance import SSEIndexEnhanceStrategy  # noqa: E402


def _bar(code: str, d: date, px: float = 10.0) -> BarData:
    return BarData(code=code, date=d, open=px, high=px, low=px,
                   close=px, volume=1e6, amount=px * 1e6)


@pytest.fixture
def mkdb(tmp_path):
    """Minimal market.db: 3 old stocks + 1 recent IPO, two month-end snapshots."""
    p = tmp_path / "market.db"
    con = sqlite3.connect(p)
    con.execute("create table stocks (code text primary key, name text,"
                " exchange text, list_date text, industry text,"
                " delist_date text)")
    con.execute("create table daily_basic (code text, date text, total_mv real,"
                " primary key (code, date))")
    stocks = [("600001", "2010-01-01"), ("600002", "2010-01-01"),
              ("601001", "2015-06-30"), ("688001", "2021-05-01")]  # 688001=新股
    for c, ld in stocks:
        con.execute("insert into stocks values (?,?,?,?,?,?)",
                    (c, c, "SH", ld, "", None))
    # 2021-06 月末快照: mv 比例 600001:600002:601001 = 6:3:1, 新股 688001 忽大
    for c, mv in [("600001", 600.0), ("600002", 300.0), ("601001", 100.0),
                  ("688001", 5000.0)]:
        con.execute("insert into daily_basic values (?,?,?)",
                    (c, "2021-06-30", mv))
    # 2021-07 月末快照: 601001 消失(退市), 其余不变
    for c, mv in [("600001", 600.0), ("600002", 300.0), ("688001", 5000.0)]:
        con.execute("insert into daily_basic values (?,?,?)",
                    (c, "2021-07-30", mv))
    con.commit()
    con.close()
    return str(p)


def _make(mkdb, **cfg):
    s = SSEIndexEnhanceStrategy()
    s.init({"market_db_path": mkdb, **cfg})
    return s


class TestSSEEnhance:
    def test_initial_weights_are_cap_weighted_and_exclude_ipo(self, mkdb):
        s = _make(mkdb)
        sigs = s.on_bar(_bar("600001", date(2021, 7, 1)))
        by_code = {x.stock_code: x for x in sigs}
        # 新股 688001 (上市 2021-05-01, 不满 365 天) 被剔除
        assert "688001" not in by_code
        assert set(by_code) == {"600001", "600002", "601001"}
        assert all(x.direction == Direction.BUY for x in sigs)
        ws = {c: x.strength for c, x in by_code.items()}
        assert sum(ws.values()) == pytest.approx(1.0)
        assert ws["600001"] == pytest.approx(0.6)
        assert ws["600002"] == pytest.approx(0.3)
        assert ws["601001"] == pytest.approx(0.1)

    def test_no_signals_mid_month(self, mkdb):
        s = _make(mkdb)
        s.on_bar(_bar("600001", date(2021, 7, 1)))
        assert s.on_bar(_bar("600001", date(2021, 7, 2))) == []
        assert s.on_bar(_bar("600002", date(2021, 7, 15))) == []

    def test_month_roll_sells_dropped_member(self, mkdb):
        s = _make(mkdb)
        s.on_bar(_bar("600001", date(2021, 7, 1)))
        sigs = s.on_bar(_bar("600001", date(2021, 8, 2)))  # 跨月首 bar
        sells = [x for x in sigs if x.direction == Direction.SELL]
        assert [x.stock_code for x in sells] == ["601001"]
        # 已持有成员不重发 BUY(市值加权自漂移)
        assert not [x for x in sigs if x.direction == Direction.BUY]

    def test_min_weight_truncates_and_renormalizes(self, mkdb):
        s = _make(mkdb, min_weight=0.2)
        sigs = s.on_bar(_bar("600001", date(2021, 7, 1)))
        ws = {x.stock_code: x.strength for x in sigs}
        assert "601001" not in ws  # 0.1 < 0.2 截断
        assert sum(ws.values()) == pytest.approx(1.0)
        assert ws["600001"] == pytest.approx(6 / 9)

    def test_not_selectable_and_has_preferred_sizer(self, mkdb):
        s = _make(mkdb)
        assert s.selectable is False
        assert s.preferred_sizer.target_weight(
            code="600001", signal_strength=0.037, recent_bars=[],
            portfolio_total_value=1e6) == pytest.approx(0.037)


def test_selector_skips_unselectable(tmp_path):
    """selectable=False 的策略不进 selector 自动池。"""
    from quanti.agent.selector import StrategySelector
    strat_dir = tmp_path / "strats"
    strat_dir.mkdir()
    (strat_dir / "two.py").write_text(
        "from quanti.strategy.base import BaseStrategy\n"
        "class A(BaseStrategy):\n"
        "    name = 'sel_a'\n"
        "    def init(self, config): pass\n"
        "    def on_bar(self, bar): return []\n"
        "class B(BaseStrategy):\n"
        "    name = 'sel_b'\n"
        "    selectable = False\n"
        "    def init(self, config): pass\n"
        "    def on_bar(self, bar): return []\n")
    sel = StrategySelector.__new__(StrategySelector)
    sel._strategies_dir = str(strat_dir)
    names = {s.name for s in sel.load_candidates()}
    assert names == {"sel_a"}
