"""dividend_tr_enhance:股票池 PIT 清洗(分红实施/ST/新股/面值退市)+ 市值权重。"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

from quanti.models import BarData, Direction

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "strategies"))
from dividend_tr_enhance import DividendTREnhanceStrategy, is_st_name  # noqa: E402


def _bar(code: str, d: date, px: float = 10.0) -> BarData:
    return BarData(code=code, date=d, open=px, high=px, low=px,
                   close=px, volume=1e6, amount=px * 1e6)


# 2021-06-30 月末快照(code, 总市值万元, 原始收盘价)
_SNAP = [
    ("600001", 600_000.0, 10.0),  # 主板 + 分红 → 入池
    ("600002", 300_000.0, 5.0),   # 主板 + 分红 → 入池
    ("000001", 200_000.0, 12.0),  # 主板(深) + 分红 → 入池
    ("300001", 200_000.0, 30.0),  # 创业板但**中证红利成分** + 分红 → 入池
    ("601001", 500_000.0, 8.0),   # 主板但过去 12 月没分红 → 剔
    ("688001", 900_000.0, 40.0),  # 科创板 + 分红,但非主板非成分 → 剔
    ("600003", 400_000.0, 9.0),   # 主板 + 分红但 ST → 剔(PIT)
    ("600004", 700_000.0, 1.2),   # 主板 + 分红但 <1.5 元(面值退市高危)→ 剔
    ("600005", 40_000.0, 6.0),    # 主板 + 分红但市值 <5 亿 → 剔
    ("600006", 800_000.0, 20.0),  # 主板 + 分红但上市 <1 年 → 剔
    ("600007", 500_000.0, 7.0),   # 主板 + 分红但已除息 400 天前(过期)→ 剔
]

_LIST_DATES = {
    "600001": "2010-01-01", "600002": "2010-01-01", "000001": "2010-01-01",
    "300001": "2010-01-01", "601001": "2010-01-01", "688001": "2010-01-01",
    "600003": "2010-01-01", "600004": "2010-01-01", "600005": "2010-01-01",
    "600006": "2021-01-04", "600007": "2010-01-01",
}


def _div(con, code, ex_date, ann_date, *, div_proc="实施", cash_div_tax=0.2):
    con.execute("insert into dividends values (?,?,?,?,?,?,?,?,?,?)",
                (code, ann_date, "2020-12-31", div_proc, 0.0, cash_div_tax,
                 ex_date, ex_date, ann_date, "tushare"))


@pytest.fixture
def mkdb(tmp_path):
    """最小市场库:月末快照 + 分红实施 + 指数权重 + 曾用名(两张月末快照)。"""
    p = tmp_path / "market.db"
    con = sqlite3.connect(p)
    con.executescript("""
        create table stocks (code text primary key, name text, exchange text,
            list_date text, industry text, delist_date text);
        create table daily_quotes (code text, date text, open real, high real,
            low real, close real, volume real, amount real, turnover real,
            adj_factor real, source text, primary key (code, date));
        create table daily_basic (code text, date text, pe real, total_mv real,
            primary key (code, date));
        create table dividends (code text, ann_date text, end_date text,
            div_proc text, stk_div real, cash_div_tax real, ex_date text,
            pay_date text, imp_ann_date text, source text,
            primary key (code, ann_date, end_date, div_proc));
        create table index_weights (index_code text, trade_date text, code text,
            weight real, primary key (index_code, trade_date, code));
        create table name_history (code text, name text, start_date text,
            end_date text, ann_date text, change_reason text,
            primary key (code, start_date));
    """)
    for code, ld in _LIST_DATES.items():
        con.execute("insert into stocks values (?,?,?,?,?,?)",
                    (code, code, "SH", ld, "", None))
    for code, mv, close in _SNAP:
        con.execute("insert into daily_basic values (?,?,?,?)",
                    (code, "2021-06-30", None, mv))
        con.execute(
            "insert into daily_quotes (code, date, open, high, low, close,"
            " volume, amount, turnover, adj_factor, source)"
            " values (?,?,?,?,?,?,?,?,?,?,?)",
            (code, "2021-06-30", close, close, close, close, 1e6, 1e7, 1.0,
             1.0, "tushare"))
    # 分红实施(除息 2021-05-20 在 12 个月窗口内);600007 是 400 天前的旧记录
    for code in ("600001", "600002", "000001", "300001", "688001", "600003",
                 "600004", "600005", "600006"):
        _div(con, code, "2021-05-20", "2021-04-20")
    _div(con, "600007", "2020-05-20", "2020-04-20")
    # 噪音行:预案阶段、现金为 0 —— 不构成"实施过分红"
    _div(con, "601001", "2021-05-20", "2021-04-20", div_proc="预案",
         cash_div_tax=0.0)
    # 中证红利成分(2021-06-30 快照):300001 是非主板成分 → 并集入口
    con.execute("insert into index_weights values (?,?,?,?)",
                ("000922.CSI", "2021-06-30", "300001", 3.0))
    con.execute("insert into index_weights values (?,?,?,?)",
                ("000922.CSI", "2021-06-30", "600001", 2.0))
    # 2021-07-31 快照:688001 才被纳入指数(8 月的调仓才轮到它)
    con.execute("insert into index_weights values (?,?,?,?)",
                ("000922.CSI", "2021-07-31", "688001", 2.5))
    # 曾用名:600003 自 2021-05-01 起叫 ST 风险(截至 7 月仍 ST)
    con.execute("insert into name_history values (?,?,?,?,?,?)",
                ("600003", "ST风险", "2021-05-01", None, "2021-04-28", "ST"))
    # 2021-08-31 月末快照(9 月的调仓要用它);除息记录不变
    for code, mv, close in _SNAP:
        con.execute("insert into daily_basic values (?,?,?,?)",
                    (code, "2021-08-31", None, mv))
        con.execute(
            "insert into daily_quotes (code, date, open, high, low, close,"
            " volume, amount, turnover, adj_factor, source)"
            " values (?,?,?,?,?,?,?,?,?,?,?)",
            (code, "2021-08-31", close, close, close, close, 1e6, 1e7, 1.0,
             1.0, "tushare"))
    con.commit()
    con.close()
    return str(p)


def _make(mkdb, **cfg):
    s = DividendTREnhanceStrategy()
    s.init({"market_db_path": mkdb, **cfg})
    return s


class TestPool:
    def test_cap_weighted_pool_passes_all_hard_filters(self, mkdb):
        s = _make(mkdb)
        sigs = s.on_bar(_bar("600001", date(2021, 7, 1)))
        by_code = {x.stock_code: x for x in sigs}
        assert set(by_code) == {"600001", "600002", "000001", "300001"}
        assert all(x.direction == Direction.BUY for x in sigs)
        ws = {c: x.strength for c, x in by_code.items()}
        assert sum(ws.values()) == pytest.approx(1.0)
        # 总市值加权:60/130, 30/130, 20/130, 20/130
        assert ws["600001"] == pytest.approx(6 / 13)
        assert ws["600002"] == pytest.approx(3 / 13)
        assert ws["000001"] == pytest.approx(2 / 13)
        assert ws["300001"] == pytest.approx(2 / 13)

    def test_st_filter_is_pit_by_name_history(self, mkdb):
        """600003 被剔是因为 2021-05-01 起叫 ST 风险 —— 变更前的时点不该被
        排除(用今天的名字回判历史是前视)。"""
        s = _make(mkdb)
        assert "600003" not in s._st_codes(date(2021, 4, 30))  # 改名生效前
        assert "600003" in s._st_codes(date(2021, 5, 1))
        assert "600003" in s._st_codes(date(2021, 7, 1))

    def test_dividend_record_must_be_within_12_months(self, mkdb):
        s = _make(mkdb)
        sigs = s.on_bar(_bar("600001", date(2021, 7, 1)))
        codes = {x.stock_code for x in sigs}
        assert "600007" not in codes  # 除息在 400 天前
        assert "601001" not in codes  # 只有预案,无实施

    def test_index_member_joins_pool_and_future_snapshot_not_used(self, mkdb):
        """指数成分是并集入口,但只能用**已发布**的快照:7 月看不到 7-31 才
        发布的成分,9 月的调仓才轮到 688001。"""
        s = _make(mkdb)
        july = {x.stock_code for x in s.on_bar(_bar("600001", date(2021, 7, 1)))}
        assert "688001" not in july
        assert s.on_bar(_bar("600001", date(2021, 7, 2))) == []
        august = s.on_bar(_bar("600001", date(2021, 8, 2)))   # 用 7 月快照
        assert "688001" not in {x.stock_code for x in august}
        sept = s.on_bar(_bar("600001", date(2021, 9, 1)))     # 用 8 月快照
        buys = [x for x in sept if x.direction == Direction.BUY]
        assert "688001" in {x.stock_code for x in buys}

    def test_thin_new_ipo_is_dropped_by_list_age(self, mkdb):
        s = _make(mkdb, min_list_days=365)
        codes = {x.stock_code for x in s.on_bar(_bar("600001", date(2021, 7, 1)))}
        assert "600006" not in codes

    def test_index_only_mode_is_the_diagnostic_control(self, mkdb):
        """默认是任务书的并集口径;index_only 只作"篮子不同 vs 机制本身"的
        诊断对照(不出货)。"""
        s = _make(mkdb, pool_mode="index_only")
        codes = {x.stock_code
                 for x in s.on_bar(_bar("600001", date(2021, 7, 1)))}
        assert codes == {"600001", "300001"}   # 只有 000922 成分
        with pytest.raises(ValueError):
            _make(mkdb, pool_mode="bogus")

    def test_min_weight_truncates_and_renormalizes(self, mkdb):
        s = _make(mkdb, min_weight=0.2)
        sigs = s.on_bar(_bar("600001", date(2021, 7, 1)))
        ws = {x.stock_code: x.strength for x in sigs}
        assert set(ws) == {"600001", "600002"}       # 20/130 低于阈值被截断
        assert sum(ws.values()) == pytest.approx(1.0)
        assert ws["600001"] == pytest.approx(2 / 3)

    def test_pool_stats_report_weighted_trailing_dividend_yield(self, mkdb):
        """诊断口径:池内市值加权滚动股息率(每股 0.2 元 / 收盘价,按权重)。"""
        s = _make(mkdb)
        s.on_bar(_bar("600001", date(2021, 7, 1)))
        stats = s.pool_stats["2021-06"]
        assert stats["n"] == 4
        expect = (6 / 13) * 0.2 / 10 + (3 / 13) * 0.2 / 5 \
            + (2 / 13) * 0.2 / 12 + (2 / 13) * 0.2 / 30
        assert stats["div_yield"] == pytest.approx(expect, abs=1e-6)


class TestRebalance:
    def test_no_signals_mid_month(self, mkdb):
        s = _make(mkdb)
        s.on_bar(_bar("600001", date(2021, 7, 1)))
        assert s.on_bar(_bar("600001", date(2021, 7, 15))) == []

    def test_month_roll_only_trades_membership(self, mkdb):
        """跨月只做成分进出,不调已持有权重(总市值加权自漂移)。"""
        s = _make(mkdb)
        s.on_bar(_bar("600001", date(2021, 7, 1)))
        sigs = s.on_bar(_bar("600001", date(2021, 8, 2)))
        assert not [x for x in sigs if x.direction == Direction.SELL]

    def test_not_selectable_and_preferred_sizer(self, mkdb):
        s = _make(mkdb)
        assert s.selectable is False
        assert s.param_space == {}
        assert s.preferred_sizer.target_weight(
            code="600001", signal_strength=0.037, recent_bars=[],
            portfolio_total_value=1e6) == pytest.approx(0.037)


def test_is_st_name():
    assert is_st_name("ST富煌") and is_st_name("*ST卓然")
    assert is_st_name("S*ST龙昌") and is_st_name("SST前锋")
    assert not is_st_name("长江电力") and not is_st_name("")
