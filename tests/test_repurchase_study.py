"""回购事件研究单测:全注入 fake sqlite,零触网、零真库。

覆盖任务书点名的口径陷阱:
  * D0 = **晚于** ann_date 的第一个交易日(周末公告用例)
  * CAR 从 D0 **开盘**起算(手算对账:公告日的隔夜跳空绝不进 CAR)
  * 基准 = 全市场等权日收益(与 fcfy 同源);停牌跨段时基准同区间几何对齐
  * (code, ann_date) 去重取 amount 最大;同票多次预案 = 独立事件
  * amount = -1.0 缺失哨兵当 NaN;完成口径取该票**首个**完成行
  * PIT:ann_date 晚于评估线的行不可见;切片市值取「≤公告日最近一行」不读未来
  * 万元/元换算(total_mv 万元、amount 元)
  * 剔金融 / ST(name_history PIT) / 次新(list_date 起算,面板起点老票不误杀)
  * 右截断如实报 n(满窗样本单独统计);门槛四条件缺一不可

行情轴取自 daily_quotes 的 DISTINCT date(与 fcfy 同源),故 fake 库里的
trade_calendar 建了但**不**被使用 —— 这正是研究层的口径,写在这防止误读。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import fcfy_study as fs  # noqa: E402
import repurchase_event_study as rp  # noqa: E402

DDL = """
CREATE TABLE trade_calendar (date TEXT PRIMARY KEY);
CREATE TABLE daily_quotes (code TEXT, date TEXT, open REAL, close REAL,
    amount REAL, turnover REAL, adj_factor REAL);
CREATE TABLE daily_basic (code TEXT, date TEXT, total_mv REAL,
    pe_ttm REAL, pb REAL, dv_ratio REAL);
CREATE TABLE stocks (code TEXT, name TEXT, exchange TEXT, list_date TEXT,
    industry TEXT, delist_date TEXT);
CREATE TABLE name_history (code TEXT, name TEXT, start_date TEXT,
    end_date TEXT, ann_date TEXT, change_reason TEXT);
CREATE TABLE repurchase_events (
    code TEXT NOT NULL, ann_date TEXT NOT NULL, end_date TEXT,
    proc TEXT NOT NULL, exp_date TEXT, vol REAL, amount REAL,
    high_limit REAL, low_limit REAL,
    PRIMARY KEY (code, ann_date, proc, amount));
"""


def bdays(start, n):
    return [d.date().isoformat() for d in pd.bdate_range(start, periods=n)]


def make_db(tmp_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(tmp_path / "fake.db")
    con.executescript(DDL)
    return con


def add_q(con, code, d, close, open_=None, adj=1.0, mv=None, amount=1e8):
    con.execute("INSERT OR REPLACE INTO daily_quotes VALUES (?,?,?,?,?,?,?)",
                (code, d, close if open_ is None else open_, close, amount,
                 1.0, adj))
    if mv is not None:
        con.execute("INSERT OR REPLACE INTO daily_basic VALUES (?,?,?,?,?,?)",
                    (code, d, mv, 20.0, 2.0, 1.0))


def add_stock(con, code, exchange="SZ", list_date="2018-01-02",
              industry="机械基件"):
    con.execute("INSERT OR REPLACE INTO stocks VALUES (?,?,?,?,?,?)",
                (code, "示例股份", exchange, list_date, industry, ""))


def add_rp(con, code, ann, proc="预案", amount=1e8):
    con.execute("INSERT OR REPLACE INTO repurchase_events "
                "(code, ann_date, proc, amount) VALUES (?,?,?,?)",
                (code, ann, proc, amount))


def load(con, dates):
    return fs.load_panel(con, dates[0], dates[-1], need_open=True)


def collect(con, panel, proc="预案", end_iso=None):
    return rp.collect_events(con, panel, rp.build_market(panel), proc,
                             end_iso or panel.dates[-1],
                             meta=fs.load_static_meta(con),
                             st_events=fs.st_events(con))


# ------------------------------------------------------- D0 与 CAR 起算口径
def test_d0_is_strictly_after_ann_date(tmp_path):
    """周末公告 ⇒ D0 是晚于 ann_date 的第一个交易日,不是公告日当天。"""
    dates = bdays("2021-09-13", 60)
    con = make_db(tmp_path)
    for d in dates:
        add_q(con, "600001", d, 10.0, mv=1e6)
    add_stock(con, "600001", "SH")
    add_rp(con, "600001", "2021-11-13")                 # 周六,不在行情轴上
    con.commit()
    et = collect(con, load(con, dates))
    assert len(et.frame) == 1
    assert et.frame.loc[0, "d0"] == "2021-11-15"        # 周一
    assert et.frame.loc[0, "d0i"] == dates.index("2021-11-15")


def test_car_starts_at_d0_open_not_ann_close(tmp_path):
    """手算对账:公告日隔夜跳空**不进** CAR;D0 首日用 open→close。

    面板只有两只票 ⇒ 全市场等权基准 = 两只票收益的算术均值,可闭式手算。
    A(事件票)公告日收盘 10 → D0 开盘 11、收盘 12(+20% 是公告日反应,须排除);
    D0+1/D0+2 持平;D0+3/D0+4 各 +1%;此后持平。B(对照票)恒为 20。
    """
    dates = bdays("2021-09-13", 100)
    i_ann, d0 = 20, 21
    con = make_db(tmp_path)
    for k, d in enumerate(dates):
        add_q(con, "600002", d, 20.0)                             # 恒定对照票
        if k < d0:
            add_q(con, "600001", d, 10.0)
        elif k == d0:
            add_q(con, "600001", d, 12.0, open_=11.0)
        elif k in (d0 + 1, d0 + 2):
            add_q(con, "600001", d, 12.0)
        elif k in (d0 + 3, d0 + 4):
            add_q(con, "600001", d, 12.0 * 1.01 ** (k - d0 - 2))
        else:
            add_q(con, "600001", d, 12.0 * 1.01 ** 2)
    add_stock(con, "600001", "SH")
    add_stock(con, "600002", "SH")
    add_rp(con, "600001", dates[i_ann])
    con.commit()

    panel = load(con, dates)
    et = collect(con, panel)
    assert list(et.frame["d0i"]) == [d0]
    day1 = 12.0 / 11.0 - 1.0                           # D0 盘中涨幅(隔夜不算)
    expect = day1 / 2.0 + 2 * (0.0 / 2.0) + 2 * (0.01 / 2.0)
    for w in (5, 20, 60):
        car, nobs = et.window_car(w)
        assert float(car[0]) == pytest.approx(expect, abs=1e-6), w
        assert int(nobs[0]) == w
    # 前视自查:若误从公告日收盘起算,CAR 会多出 ~+20% 的隔夜跳空
    assert float(et.window_car(5)[0][0]) < 0.10


def test_market_benchmark_is_equal_weight(tmp_path):
    """等权基准 = 当日全部有效票日收益的算术均值(不是市值加权、不是中位数)。"""
    dates = bdays("2021-09-13", 10)
    con = make_db(tmp_path)
    for k, d in enumerate(dates):
        add_q(con, "600001", d, 10.0 * 1.02 ** k)                 # 每天 +2%
        add_q(con, "600002", d, 50.0 * 1.02 ** k)                 # 每天 +2%
        add_q(con, "600003", d, 5.0 if k == 0 else 4.9)           # 次日 -2%
    con.commit()
    mkt = rp.build_market(load(con, dates))
    assert mkt.ret[1] == pytest.approx((0.02 + 0.02 - 0.02) / 3.0, abs=1e-6)
    assert mkt.ret[2] == pytest.approx((0.02 + 0.02 + 0.0) / 3.0, abs=1e-6)
    assert mkt.intraday[1] == pytest.approx(0.0, abs=1e-12)       # open==close


def test_suspended_days_align_benchmark_span():
    """停牌跨多日 ⇒ 个股一次性结算复牌跳空,基准取**同一段**的几何收益。"""
    dates = bdays("2021-09-13", 40)
    T = len(dates)
    close = np.full((T, 2), np.nan)
    close[:6, 0] = 10.0
    close[12:, 0] = 12.0                                  # 第 6..11 行停牌
    close[:, 1] = 10.0
    panel = fs.Panel(dates=dates, codes=["600001", "600002"],
                     close=close, open_=close.copy())
    ret = np.zeros(T)
    ret[1:] = 0.01
    mkt = rp.MarketContext(ret=ret, intraday=np.zeros(T),
                           cum=np.cumsum(np.log1p(ret)), mean_daily_pct=0.01)
    ex = rp.event_excess(panel, mkt, 0, 5, want=5)
    assert ex[0] == pytest.approx(0.0, abs=1e-12)         # D0 open→close 无涨跌
    # 第 2 个观测 = 停牌段一次性 +20%,基准 = 同一段 7 个交易日的 1.01^7-1
    assert ex[1] == pytest.approx(0.2 - (1.01 ** 7 - 1.0), abs=1e-9)
    assert ex[2] == pytest.approx(0.0 - 0.01, abs=1e-9)   # 复牌横盘 ⇒ 输给基准


# ------------------------------------------------------------------ 事件池
def test_dedupe_same_code_date_takes_max_amount(tmp_path):
    """同 (code, ann_date) 多行 ⇒ 一条事件、amount 取最大(元)。"""
    con = make_db(tmp_path)
    add_rp(con, "000001", "2024-03-01", "预案", 1e7)
    add_rp(con, "000001", "2024-03-01", "预案", 5e7)
    add_rp(con, "000002", "2024-03-01", "预案", -1.0)
    add_rp(con, "000002", "2024-03-01", "预案", 2e7)
    con.commit()
    ev = rp.load_events(con, "预案", "2021-10-01", "2026-09-10")
    assert len(ev) == 2
    assert sorted(ev["amount"]) == [2e7, 5e7]


def test_amount_sentinel_is_missing(tmp_path):
    """amount = -1.0 是主键缺失哨兵 ⇒ 研究层当 NaN(绝不当「负额度」)。"""
    con = make_db(tmp_path)
    add_rp(con, "000003", "2024-03-01", "预案", -1.0)
    con.commit()
    ev = rp.load_events(con, "预案", "2021-10-01", "2026-09-10")
    assert len(ev) == 1
    assert np.isnan(float(ev.loc[0, "amount"]))


def test_repeated_plans_are_independent_events(tmp_path):
    """同票不同 ann_date 的两次预案 = 两个独立事件(组合层再处理重叠)。"""
    con = make_db(tmp_path)
    add_rp(con, "000004", "2024-03-01", "预案", 1e7)
    add_rp(con, "000004", "2025-06-10", "预案", 3e7)
    con.commit()
    ev = rp.load_events(con, "预案", "2021-10-01", "2026-09-10")
    assert list(ev["ann_date"]) == ["2024-03-01", "2025-06-10"]


def test_completion_uses_first_row_per_code(tmp_path):
    """完成口径:该票**首个**完成行(后面的完成行不再算事件)。"""
    con = make_db(tmp_path)
    add_rp(con, "000005", "2024-05-01", "完成", 1e7)
    add_rp(con, "000005", "2025-09-01", "完成", 4e7)
    add_rp(con, "000006", "2024-05-01", "完成", -1.0)
    con.commit()
    ev = rp.load_events(con, "完成", "2021-10-01", "2026-09-10")
    assert list(ev["code"]) == ["000005", "000006"]
    assert ev.loc[ev.code == "000005", "ann_date"].item() == "2024-05-01"
    assert np.isnan(float(ev.loc[ev.code == "000006", "amount"].item()))


def test_event_pool_is_pit_bounded(tmp_path):
    """PIT:晚于评估线的 ann_date 不可见,早于行情窗的也不该混进来。"""
    con = make_db(tmp_path)
    add_rp(con, "000007", "2026-09-01", "预案", 1e7)
    add_rp(con, "000008", "2026-12-31", "预案", 1e7)          # 未来 ⇒ 不可见
    add_rp(con, "000009", "2021-05-06", "预案", 1e7)          # 早于行情窗
    con.commit()
    ev = rp.load_events(con, "预案", rp.EVENT_START, "2026-09-10")
    assert list(ev["code"]) == ["000007"]


def test_events_without_d0_or_quotes_are_dropped(tmp_path):
    """公告后无交易日 / 库内无行情 ⇒ 各自归类剔除(不许静默消失)。"""
    dates = bdays("2021-09-13", 60)
    con = make_db(tmp_path)
    for d in dates:
        add_q(con, "600001", d, 10.0, mv=1e6)
    add_stock(con, "600001", "SH")
    add_rp(con, "600001", dates[-1])                          # 末日 ⇒ 无 D0
    add_rp(con, "600099", dates[20])                          # 库里无行情
    con.commit()
    et = collect(con, load(con, dates))
    assert et.frame.empty
    assert et.funnel["no_d0"] == 1 and et.funnel["no_quotes"] == 1


# ------------------------------------------------------------------ 清洗
def test_financial_st_and_new_listing_excluded(tmp_path):
    """剔金融(行业)、剔 ST(name_history PIT)、剔次新(上市<120 交易日)。"""
    dates = bdays("2021-09-13", 800)
    con = make_db(tmp_path)
    for d in dates:
        for c in ("600001", "600002", "600003", "600004"):
            add_q(con, c, d, 10.0, mv=1e6)
    add_stock(con, "600001", "SH", "2018-01-02", "银行")       # 金融
    add_stock(con, "600002", "SH", "2018-01-02", "证券")       # 金融
    add_stock(con, "600003", "SH", "2018-01-02", "软件服务")   # ST 期间
    add_stock(con, "600004", "SH", "2024-04-01", "软件服务")   # 次新
    con.execute("INSERT INTO name_history VALUES ('600003','ST某某',"
                "'2024-01-10','2025-01-10','','')")
    ann = "2024-06-03"
    for c in ("600001", "600002", "600003", "600004"):
        add_rp(con, c, ann, amount=1e7)
    con.commit()
    et = collect(con, load(con, dates))
    assert list(et.frame["code"]) == [], "四条全被剔(2 金融 + 1 ST + 1 次新)"
    assert et.funnel["financial"] == 2
    assert et.funnel["st"] == 1
    assert et.funnel["new_listing"] == 1
    assert et.funnel["kept"] == 0


def test_cleaning_keeps_the_plain_stock(tmp_path):
    """对照:非金融、非 ST、老票、有行情 ⇒ 必须留下(防上一条假阳性)。"""
    dates = bdays("2021-09-13", 800)
    con = make_db(tmp_path)
    for d in dates:
        add_q(con, "600009", d, 10.0, mv=1e6)
    add_stock(con, "600009", "SH", "2018-01-02", "软件服务")
    add_rp(con, "600009", "2024-06-03", amount=1e7)
    con.commit()
    et = collect(con, load(con, dates))
    assert list(et.frame["code"]) == ["600009"]
    assert et.frame.loc[0, "ann_date"] == "2024-06-03"


def test_st_judgement_is_point_in_time(tmp_path):
    """同一只票:戴帽期间的公告被剔,摘帽后的公告保留。"""
    dates = bdays("2021-09-13", 500)
    con = make_db(tmp_path)
    for d in dates:
        add_q(con, "600002", d, 10.0, mv=1e6)
    add_stock(con, "600002", "SH")
    con.execute("INSERT INTO name_history VALUES ('600002','ST某某',"
                "'2022-05-05','2023-05-05','','')")
    add_rp(con, "600002", "2022-06-01", amount=1e7)            # ST 期间
    add_rp(con, "600002", "2023-06-01", amount=1e7)            # 摘帽之后
    con.commit()
    et = collect(con, load(con, dates))
    assert list(et.frame["ann_date"]) == ["2023-06-01"]


def test_old_stock_listed_before_panel_start_is_not_new(tmp_path):
    """回归:老票(面板起点前上市)不得被误判成次新而清空样本。

    行情起点 2021-09-13 ⇒ 老票在面板里的首根 bar 就是第 0 行,若用「面板内
    序号差」算年龄,2021-12 的所有事件都会被当成「只上市 55 天」而剔光。
    """
    dates = bdays("2021-09-13", 200)
    con = make_db(tmp_path)
    for d in dates:
        add_q(con, "600011", d, 10.0, mv=1e6)
    add_stock(con, "600011", "SH", "2010-01-02")
    add_rp(con, "600011", "2021-12-01", amount=1e7)
    con.commit()
    panel = load(con, dates)
    i_ann = dates.index("2021-12-01")
    assert i_ann < rp.MIN_LIST_DAYS, "面板内序号差不足 120 ⇒ 用它算年龄必误杀"
    assert panel.first_quote["600011"] == 0
    assert not rp.is_new_listing("600011", "2021-12-01", i_ann,
                                 fs.load_static_meta(con), panel)
    assert len(collect(con, panel).frame) == 1


def test_new_listing_inside_panel_uses_trading_days(tmp_path):
    """面板内上市的票:按真实交易日序号精确判 120 日线。"""
    dates = bdays("2021-09-13", 300)
    con = make_db(tmp_path)
    first = 50                                   # 2021-11 才上市
    for k, d in enumerate(dates):
        add_q(con, "600000", d, 10.0, mv=1e6)    # 老票:把行情轴撑到面板起点
        if k >= first:
            add_q(con, "301111", d, 10.0, mv=1e6)
    add_stock(con, "600000", "SH", "2018-01-02")
    add_stock(con, "301111", "SZ", dates[first])
    con.commit()
    panel = load(con, dates)
    assert panel.dates == dates
    meta = fs.load_static_meta(con)
    assert rp.is_new_listing("301111", dates[first + 100], first + 100,
                             meta, panel)                     # 只活 100 个交易日
    assert not rp.is_new_listing("301111", dates[first + 130], first + 130,
                                 meta, panel)                 # 满 120 个交易日


def test_d0_without_bar_is_untradable(tmp_path):
    """D0 当日无 bar(停牌)⇒ 不可成交,整个事件剔除而不是往前借价。"""
    dates = bdays("2021-09-13", 200)
    con = make_db(tmp_path)
    for k, d in enumerate(dates):
        add_q(con, "600000", d, 10.0, mv=1e6)     # 对照票把交易日轴撑满
        if k != 21:                                   # 事件票 D0 那天停牌
            add_q(con, "600001", d, 10.0, mv=1e6)
    add_stock(con, "600000", "SH")
    add_stock(con, "600001", "SH")
    add_rp(con, "600001", dates[20], amount=1e7)
    con.commit()
    et = collect(con, load(con, dates))
    assert et.frame.empty and et.funnel["untradable_d0"] == 1


# ------------------------------------------------------------ 单位与切片口径
def test_intensity_units_wan_yuan_to_yuan(tmp_path):
    """total_mv 是**万元**、amount 是**元** ⇒ 力度 = amount/(mv×1e4)。"""
    dates = bdays("2021-09-13", 200)
    con = make_db(tmp_path)
    for d in dates:
        add_q(con, "600001", d, 10.0, mv=1e6)                 # 1e6 万元=100亿
    add_stock(con, "600001", "SH")
    add_rp(con, "600001", dates[30], amount=1e8)              # 1 亿元 ⇒ 1%
    con.commit()
    et = collect(con, load(con, dates))
    assert et.frame.loc[0, "mv_wan"] == pytest.approx(1e6)
    assert et.frame.loc[0, "intensity"] == pytest.approx(0.01, rel=1e-5)
    assert et.frame.loc[0, "intensity"] >= rp.INTENSITY_GATE


def test_missing_amount_gives_nan_intensity(tmp_path):
    """哨兵 -1 ⇒ amount NaN ⇒ 力度 NaN,不进任何力度切片(不当 0 额度)。"""
    dates = bdays("2021-09-13", 200)
    con = make_db(tmp_path)
    for d in dates:
        add_q(con, "600001", d, 10.0, mv=1e6)
    add_stock(con, "600001", "SH")
    add_rp(con, "600001", dates[30], amount=-1.0)
    con.commit()
    et = collect(con, load(con, dates))
    assert np.isnan(et.frame.loc[0, "amount"])
    assert np.isnan(et.frame.loc[0, "intensity"])
    masks = rp.slice_masks(et, (1e5, 1e6), "预案")
    assert not masks["intensity_hi"].any() and not masks["intensity_lo"].any()


def test_mv_lookup_is_pit_last_row_on_or_before_ann(tmp_path):
    """切片市值取「≤ ann_date 的最近一行」:公告日停牌时回看,绝不读后面。"""
    dates = bdays("2021-09-13", 200)
    con = make_db(tmp_path)
    for k, d in enumerate(dates):
        mv = 1e6 if k <= 65 else (None if k < 75 else 9e6)
        add_q(con, "600001", d, 10.0, mv=mv)
    add_stock(con, "600001", "SH")
    add_rp(con, "600001", dates[70], amount=1e8)              # 公告日无市值行
    con.commit()
    panel = load(con, dates)
    assert rp.mv_at(panel, 70, 0) == pytest.approx(1e6)       # 回看到第 65 行
    et = collect(con, panel)
    assert et.frame.loc[0, "mv_wan"] == pytest.approx(1e6)
    assert et.frame.loc[0, "intensity"] == pytest.approx(0.01, rel=1e-5)


def test_size_terciles_and_masks_partition():
    """规模三档切点来自预案事件集本身;掩码互斥且只收有市值的票。"""
    n = 32
    et = rp.EventTable(pd.DataFrame({
        "code": ["60%04d" % i for i in range(n)],
        "ann_date": ["2024-01-02"] * n,
        "mv_wan": [100.0 * (i + 1) for i in range(n - 1)] + [np.nan],
        "intensity": [np.nan] * n}))
    edges = rp.size_terciles(et)
    assert edges is not None and edges[0] < edges[1]
    masks = rp.slice_masks(et, edges, "预案")
    small, mid, large = (masks["size_small"], masks["size_mid"],
                         masks["size_large"])
    assert small.sum() and mid.sum() and large.sum()
    assert not (small & mid).any() and not (mid & large).any()
    assert int((small | mid | large).sum()) == n - 1, "无市值行不进任何规模档"


def test_intensity_slice_only_for_plan_rows():
    """力度切片只属于**预案**口径(拟回购金额下限);其他口径不硬凑。"""
    et = rp.EventTable(pd.DataFrame({
        "code": ["600001", "600002", "600003"],
        "ann_date": ["2024-01-02"] * 3,
        "mv_wan": [1e6, 1e6, np.nan],
        "intensity": [0.02, 0.0001, np.nan]}))
    m_plan = rp.slice_masks(et, (2e6, 5e6), "预案")
    assert list(m_plan["intensity_hi"]) == [True, False, False]
    assert list(m_plan["intensity_lo"]) == [False, True, False]
    m_done = rp.slice_masks(et, (2e6, 5e6), "完成")
    assert "intensity_hi" not in m_done and "all" in m_done


def test_band_of_matches_size_slice_edges():
    """同规模档诊断基准的边界必须与 size_* 切片**完全一致**。"""
    edges = (1000.0, 5000.0)
    assert rp.band_of(1000.0, edges) == "size_small"
    assert rp.band_of(1000.1, edges) == "size_mid"
    assert rp.band_of(5000.0, edges) == "size_mid"
    assert rp.band_of(5000.1, edges) == "size_large"
    assert rp.band_of(float("nan"), edges) == "size_unknown"


def test_matched_benchmark_changes_only_the_benchmark(tmp_path):
    """同档基准诊断:同一批事件、同样的个股收益,只换基准 ⇒ 差 = 风格暴露。"""
    dates = bdays("2021-09-13", 200)
    con = make_db(tmp_path)
    for k, d in enumerate(dates):
        # 小档票每天 +1%、大档票每天 -1% ⇒ 全市场等权 ≈ 0,同档基准 = +1%
        add_q(con, "600001", d, 10.0 * 1.01 ** k, mv=1e5)
        add_q(con, "600999", d, 10.0 * 0.99 ** k, mv=1e7)
    add_stock(con, "600001", "SH")
    add_stock(con, "600999", "SH")
    add_rp(con, "600001", dates[20], amount=1e7)
    con.commit()
    panel = load(con, dates)
    et = collect(con, panel)
    assert len(et.frame) == 1
    car_plain, _ = et.window_car(5)
    edges = (5e6, 8e6)
    rp.attach_matched(et, panel, rp.build_band_markets(panel, edges), edges)
    car_matched, _ = et.window_car(5, matched=True)
    assert float(car_plain[0]) > 0.02                    # vs 全市场:白赚风格差
    assert float(car_matched[0]) == pytest.approx(0.0, abs=2e-3)


def test_mv_at_without_mv_column_is_nan(tmp_path):
    dates = bdays("2021-09-13", 20)
    con = make_db(tmp_path)
    for d in dates:
        add_q(con, "600001", d, 10.0)
    con.commit()
    panel = load(con, dates)
    assert np.isnan(rp.mv_at(panel, 5, 0))


# ------------------------------------------------------------------ 统计与闸
def test_summarize_slice_reports_honest_n_and_truncation():
    """右截断:满窗样本单独计数;逐年/两段/胜率/NW 滞后都对得上。"""
    car = np.array([0.02, -0.01, 0.03, 0.005, -0.002])
    nobs = np.array([20, 20, 7, 20, 20])                 # 第 3 条被右截断
    dates = np.array(["2022-01-04", "2023-01-04", "2023-02-04",
                      "2024-01-04", "2025-01-06"])
    s = rp.summarize_slice(car, nobs, dates, 20)
    assert s["n"] == 5 and s["n_full_window"] == 4
    assert s["mean_pct"] == pytest.approx(float(car.mean()) * 100)
    assert s["mean_full_only_pct"] == pytest.approx(
        float(car[[0, 1, 3, 4]].mean()) * 100)
    assert s["win_rate"] == pytest.approx(0.6)
    assert s["nw_lag"] == 19
    assert s["by_year"]["2022"]["n"] == 1
    assert s["pos_year_names"] == ["2022", "2023", "2024"]
    assert s["gate_years"] == 4, "2026 无样本 ⇒ 闸的年份分母如实是 4"
    assert s["h1_2021_2023"]["n"] == 3 and s["h2_2024_2026"]["n"] == 2


def test_pass_gate_requires_all_four_conditions():
    """门槛:均值>0 且 t≥2.5 且 胜率≥55% 且 ≥4/5 年为正 —— 缺一即不过。"""
    def s(mean=0.5, t=3.0, win=0.60, pos=4, years=5):
        return {"mean_pct": mean, "nw_t": t, "win_rate": win,
                "pos_years": pos, "gate_years": years}

    assert rp.pass_gate(s()) is True
    assert rp.pass_gate(s(mean=-0.1)) is False           # 方向反了
    assert rp.pass_gate(s(t=2.49)) is False              # 显著性差一点也不过
    assert rp.pass_gate(s(win=0.549)) is False           # 胜率闸
    assert rp.pass_gate(s(pos=3)) is False               # 逐年一致性
    assert rp.pass_gate(s(years=4, pos=4)) is True       # 4 个覆盖年全正即达标
    assert rp.pass_gate(s(years=4, pos=3)) is False      # 覆盖 4 年只 3 年为正
    assert rp.pass_gate(s(years=5, pos=4)) is True       # 5 年里 4 年为正 = 达标
    assert rp.pass_gate({"mean_pct": None}) is False
    assert rp.pass_gate({}) is False


def test_trial_ledger_covers_all_declared_variants():
    """账本:3 proc × 3 窗口 × 预声明切片(力度仅预案)= 42 个变体全入账。"""
    cfgs = rp.trial_configs()
    assert len(cfgs) == 3 * 3 * 4 + 1 * 3 * 2
    assert ("预案", "intensity_hi", 20) in cfgs
    assert all(w in rp.WINDOWS for _, _, w in cfgs), "D0 诊断窗不进门槛账本"
    assert ("完成", "intensity_hi", 20) not in cfgs
    assert {p for p, _, _ in cfgs} == set(rp.PROCS)
    assert {w for _, _, w in cfgs} == set(rp.WINDOWS)


def test_run_study_end_to_end_on_fake_db(tmp_path):
    """全流程冒烟:漏斗、账本键、主口径、同档诊断、停止口径单列。"""
    dates = bdays("2021-09-13", 400)
    con = make_db(tmp_path)
    codes = ["60%04d" % k for k in range(40)]
    for k, d in enumerate(dates):
        for j, c in enumerate(codes):
            add_q(con, c, d, 10.0 * (1.01 if j % 2 else 0.99) ** k,
                  mv=1e5 * (j + 1))
    add_stock(con, codes[0], "SH", "2019-01-02", "银行")     # 该票事件被剔
    for c in codes[1:]:
        add_stock(con, c, "SH")
    for j, c in enumerate(codes):
        add_rp(con, c, dates[200], "预案", amount=1e8 * (j + 1))
        add_rp(con, c, dates[200], "股东大会通过", amount=1e8)
        add_rp(con, c, dates[210], "完成", amount=1e8)
    add_rp(con, codes[5], dates[300], "停止", amount=1e7)
    con.commit()
    panel = load(con, dates)
    out = rp.run_study(con, panel, end_iso=dates[-1], log=lambda *a: None)
    assert out["cleaning_funnel"]["预案"]["financial"] == 1
    assert out["cleaning_funnel"]["预案"]["kept"] == 39
    assert out["primary"]["n"] == 39
    assert out["n_trials"] == len(rp.trial_configs())
    assert set(out["trials"]) == {"%s|%s|%d" % c for c in rp.trial_configs()}
    assert isinstance(out["gate_pass"], bool)
    assert len(out["size_tercile_mv_wan"]) == 2
    # 全部预案力度都 ≥0.1% ⇒ 低力度档零样本(账本仍要有这一格,不许抹掉)
    assert out["trials"]["预案|intensity_lo|20"]["n"] == 0
    assert out["trials"]["预案|intensity_lo|20"]["gate_pass"] is False
    assert "预案" in out["d0_only_diagnostic"]
    # 停止口径单列成诊断,绝不混进门槛账本
    assert out["stop_events"]["n_events"] == 1
    assert all("停止" not in k for k in out["trials"])
    assert "预案|all|20" in out["matched_benchmark_diagnostic"]
    assert out["matched_benchmark_diagnostic"]["预案|all|20"]["n"] == 39


def test_zero_events_yields_empty_trials(tmp_path):
    """库里有行情没事件 ⇒ 账本空、主口径 None,不抛异常。"""
    dates = bdays("2021-09-13", 60)
    con = make_db(tmp_path)
    for d in dates:
        add_q(con, "600001", d, 10.0, mv=1e6)
    add_stock(con, "600001", "SH")
    con.commit()
    out = rp.run_study(con, load(con, dates), end_iso=dates[-1],
                       log=lambda *a: None)
    assert out["trials"] == {} and out["primary"] is None
    assert out["gate_pass"] is False
    assert "stop_events" not in out
