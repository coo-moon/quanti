"""FcfY 研究脚本的单测:全注入 fake sqlite,零触网、零真库。

覆盖任务书点名的口径陷阱:
  * TTM 拼接(年报 / 中报 / 三季 / 缺上年数据剔除)
  * 同 (code,end_date) 多版本按 ann_date 取最新(update_flag 平手取更正版)
  * PIT:ann_date > D 的报告绝不可见(含「上年年报在 D 尚未公告」这一刀)
  * 单位换算:daily_basic.total_mv 是万元、报表是元
  * rank-IC 手算对账、逐年符号统计、五分位(单向性)
  * 金融票剔除 / ST 按 name_history PIT 判定 / 次新剔除 / 剔北交所
  * 残差化(对 12 因子 composite 正交)手算对账
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fcfy_study as fs  # noqa: E402

DDL = """
CREATE TABLE cashflow_items (
    code TEXT NOT NULL, end_date TEXT NOT NULL, ann_date TEXT, f_ann_date TEXT,
    update_flag TEXT, n_cashflow_act REAL, c_pay_acq_const_fiolta REAL,
    PRIMARY KEY (code, end_date, ann_date, update_flag));
CREATE TABLE daily_quotes (code TEXT, date TEXT, open REAL, close REAL,
    amount REAL, turnover REAL, adj_factor REAL);
CREATE TABLE daily_basic (code TEXT, date TEXT, total_mv REAL,
    pe_ttm REAL, pb REAL, dv_ratio REAL);
CREATE TABLE stocks (code TEXT, name TEXT, exchange TEXT, list_date TEXT,
    industry TEXT, delist_date TEXT);
CREATE TABLE name_history (code TEXT, name TEXT, start_date TEXT,
    end_date TEXT, ann_date TEXT, change_reason TEXT);
CREATE TABLE financials (code TEXT, end_date TEXT, ann_date TEXT,
    report_type TEXT, roe REAL, net_profit REAL, revenue REAL,
    netprofit_yoy REAL, revenue_yoy REAL);
"""


def make_db(tmp_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(tmp_path / "fake.db")
    con.executescript(DDL)
    return con


def add_cf(con, code, end, ann, cfo, capex, flag="0"):
    con.execute("INSERT OR REPLACE INTO cashflow_items VALUES (?,?,?,?,?,?,?)",
                (code, end, ann, ann, flag, cfo, capex))


def add_q(con, code, d, close, adj=1.0, mv=None, amount=1e8, turnover=1.0,
          open_=None):
    con.execute("INSERT INTO daily_quotes VALUES (?,?,?,?,?,?,?)",
                (code, d, close if open_ is None else open_, close, amount,
                 turnover, adj))
    if mv is not None:
        con.execute("INSERT INTO daily_basic VALUES (?,?,?,?,?,?)",
                    (code, d, mv, 20.0, 2.0, 1.0))


def add_stock(con, code, exchange="SZ", list_date="2018-01-02",
              industry="机械基件", name="示例股份"):
    con.execute("INSERT INTO stocks VALUES (?,?,?,?,?,?)",
                (code, name, exchange, list_date, industry, ""))


# --------------------------------------------------------------- TTM 拼接
def test_ttm_annual_is_ytd(tmp_path):
    """年报(12-31)的 TTM = 年报累计本身,不做任何拼接。"""
    con = make_db(tmp_path)
    add_cf(con, "000001", "2022-12-31", "2023-03-20", 500.0, 100.0)
    vis = fs.visible_versions(con, "2023-04-03")
    ttm = fs.ttm_from_versions(vis, "2023-04-03")
    assert ttm.loc["000001", "cfo_ttm"] == 500.0
    assert ttm.loc["000001", "capex_ttm"] == 100.0
    assert ttm.loc["000001", "fcf_ttm"] == 400.0
    assert ttm.loc["000001", "anchor_end_date"] == "2022-12-31"


def test_ttm_q3_stitching(tmp_path):
    """三季报:TTM = 本期累计 + 上年年报 − 上年同期。"""
    con = make_db(tmp_path)
    add_cf(con, "000002", "2022-12-31", "2023-04-28", 1200.0, 240.0)
    add_cf(con, "000002", "2022-06-30", "2022-08-30", 400.0, 50.0)
    add_cf(con, "000002", "2022-09-30", "2022-10-28", 500.0, 60.0)
    add_cf(con, "000002", "2023-06-30", "2023-08-25", 600.0, 80.0)
    add_cf(con, "000002", "2023-09-30", "2023-10-27", 900.0, 130.0)
    vis = fs.visible_versions(con, "2023-11-30")
    ttm = fs.ttm_from_versions(vis, "2023-11-30")
    assert ttm.loc["000002", "anchor_end_date"] == "2023-09-30"
    assert ttm.loc["000002", "cfo_ttm"] == pytest.approx(900 + 1200 - 500)
    assert ttm.loc["000002", "capex_ttm"] == pytest.approx(130 + 240 - 60)
    assert ttm.loc["000002", "fcf_ttm"] == pytest.approx(1600 - 310)


def test_ttm_h1_stitching_picks_latest_visible_period(tmp_path):
    """三季报尚未公告时,锚退到中报:TTM = 中报累计 + 上年年报 − 上年中报。"""
    con = make_db(tmp_path)
    add_cf(con, "000002", "2022-12-31", "2023-04-28", 1200.0, 240.0)
    add_cf(con, "000002", "2022-06-30", "2022-08-30", 400.0, 50.0)
    add_cf(con, "000002", "2023-06-30", "2023-08-25", 600.0, 80.0)
    add_cf(con, "000002", "2023-09-30", "2023-10-27", 900.0, 130.0)
    ttm = fs.ttm_from_versions(fs.visible_versions(con, "2023-09-01"), "2023-09-01")
    assert ttm.loc["000002", "anchor_end_date"] == "2023-06-30"
    assert ttm.loc["000002", "fcf_ttm"] == pytest.approx(600 + 1200 - 400
                                                        - (80 + 240 - 50))


def test_ttm_missing_prior_annual_drops_row(tmp_path):
    """缺上年年报(次新 / 停报)→ 该行不可用,不做 4×单季粗估、不降级。"""
    con = make_db(tmp_path)
    add_cf(con, "301111", "2023-06-30", "2023-08-30", 300.0, 40.0)
    add_cf(con, "301111", "2023-09-30", "2023-10-30", 500.0, 70.0)
    vis = fs.visible_versions(con, "2023-11-30")
    ttm = fs.ttm_from_versions(vis, "2023-11-30")
    assert ttm.empty


def test_ttm_missing_prior_same_period_drops_row(tmp_path):
    """有上年年报但缺上年同期 → 同样整行剔除(宁缺毋滥)。"""
    con = make_db(tmp_path)
    add_cf(con, "301112", "2022-12-31", "2023-04-20", 800.0, 100.0)
    add_cf(con, "301112", "2023-09-30", "2023-10-30", 600.0, 90.0)
    vis = fs.visible_versions(con, "2023-11-30")
    assert fs.ttm_from_versions(vis, "2023-11-30").empty


def test_stale_anchor_report_dropped(tmp_path):
    """锚报告期超过 max_stale_days(停报/退市清理)→ 剔除。"""
    con = make_db(tmp_path)
    add_cf(con, "000003", "2021-12-31", "2022-04-29", 100.0, 10.0)
    vis = fs.visible_versions(con, "2023-06-30")
    assert fs.ttm_from_versions(vis, "2023-06-30").empty
    # 同一条数据在紧邻的 asof 下仍然可用(证明是「过期」而不是「缺件」)
    vis2 = fs.visible_versions(con, "2022-06-30")
    assert len(fs.ttm_from_versions(vis2, "2022-06-30")) == 1


# --------------------------------------------------------------- 多版本去重
def test_dedup_picks_latest_ann_date(tmp_path):
    """同 (code,end_date) 多版本:ann_date 最大者胜;晚公告的更正版不可提前用。"""
    con = make_db(tmp_path)
    add_cf(con, "000004", "2022-12-31", "2023-04-01", 100.0, 10.0, flag="1")
    add_cf(con, "000004", "2022-12-31", "2023-04-13", 700.0, 10.0, flag="0")
    add_cf(con, "000004", "2022-12-31", "2023-04-13", 700.0, 10.0, flag="1")
    early = fs.ttm_from_versions(fs.visible_versions(con, "2023-04-05"),
                                "2023-04-05")
    late = fs.ttm_from_versions(fs.visible_versions(con, "2023-04-20"),
                                "2023-04-20")
    assert early.loc["000004", "fcf_ttm"] == 90.0     # 只用已公告的 4/1 版
    assert late.loc["000004", "fcf_ttm"] == 690.0     # 4/13 版覆盖
    # 每 (code,end_date) 恰好一行,重复版本不会把现金流数两遍
    assert len(late) == 1


def test_pit_unannounced_report_invisible(tmp_path):
    """前视是死刑:ann_date > D 的报告在 D 日绝不可见(含上年年报未公告的情形)。"""
    con = make_db(tmp_path)
    add_cf(con, "000005", "2022-12-31", "2023-04-25", 1000.0, 100.0)
    add_cf(con, "000005", "2023-03-31", "2023-04-28", 300.0, 30.0)
    add_cf(con, "000005", "2022-03-31", "2022-04-27", 250.0, 25.0)
    # 2023-03-15:唯一可见的是 2022Q1(2022-04-27 公告),它要 2021 年报 +
    # 2021Q1 —— 库里没有 ⇒ 不可用;2022 年报(4/25 公告)与 2023Q1(4/28)都不可见
    assert set(fs.visible_versions(con, "2023-03-15")["end_date"]) == {"2022-03-31"}
    assert fs.ttm_from_versions(fs.visible_versions(con, "2023-03-15"),
                                "2023-03-15").empty
    # 4/26 之后:年报可见 → 2022 年报 + 2022Q1 可拼(锚退到 2022-12-31 的上一档,
    # 即年报本身);Q1 仍不可见(4/28 公告)
    ttm2 = fs.ttm_from_versions(fs.visible_versions(con, "2023-04-26"),
                                "2023-04-26")
    assert ttm2.loc["000005", "anchor_end_date"] == "2022-12-31"
    assert ttm2.loc["000005", "fcf_ttm"] == 900.0
    # 4/28 之后锚变 2023Q1,TTM = 300 + 1000 - 250(用上上年年报 + 上年同期拼)
    ttm3 = fs.ttm_from_versions(fs.visible_versions(con, "2023-05-05"),
                                "2023-05-05")
    assert ttm3.loc["000005", "anchor_end_date"] == "2023-03-31"
    assert ttm3.loc["000005", "cfo_ttm"] == pytest.approx(300 + 1000 - 250)
    assert ttm3.loc["000005", "capex_ttm"] == pytest.approx(30 + 100 - 25)
    assert ttm3.loc["000005", "fcf_ttm"] == pytest.approx(1050 - 105)


# --------------------------------------------------------------- 单位换算
def test_total_mv_unit_is_wan_yuan(tmp_path):
    """total_mv 万元 × 1e4 → 元:FcfY = FCF元 / 市值元(手算对账)。"""
    con = make_db(tmp_path)
    add_cf(con, "000006", "2022-12-31", "2023-03-20", 5e8, 1e8)   # FCF 4 亿元
    vis = fs.visible_versions(con, "2023-06-30")
    ttm = fs.ttm_from_versions(vis, "2023-06-30")
    assert ttm.loc["000006", "fcf_ttm"] == 4e8
    mv = pd.Series({"000006": 200_0000.0})          # 200 亿元 = 200 万(万元)
    ff = fs.factor_frame(con, "2023-06-30", mv)
    assert ff.loc["000006", "total_mv_yuan"] == 2e10
    assert ff.loc["000006", "fcfy"] == pytest.approx(4e8 / 2e10)   # 2%
    assert ff.loc["000006", "cfoy"] == pytest.approx(5e8 / 2e10)


# --------------------------------------------------------------- 宇宙清洗
UNI_CODES = ("600001", "600002", "600003", "300004", "688005",
             "830006", "002007", "000008", "000009", "001010")


def bdays(start, n):
    return [d.date().isoformat() for d in pd.bdate_range(start, periods=n)]


def _mk_universe_db(tmp_path, dates):
    """10 票小面板:每票恰好对应一个被剔除的理由。"""
    con = make_db(tmp_path)
    for i, d in enumerate(dates):
        px = 10.0 + 0.01 * i
        for code in UNI_CODES:
            if code == "000009":
                continue                       # 完全没行情
            if code == "002007" and i < 60:
                continue                       # 前 60 日无行情 = 上市未满 120 交易日
            add_q(con, code, d, px, mv=None if code == "000008" else 1e6)
    add_stock(con, "600001", "SH", "2018-01-02", "机械基件")
    add_stock(con, "600002", "SH", "2018-01-02", "全国股份银行")
    add_stock(con, "600003", "SH", "2018-01-02", "化工机械")
    add_stock(con, "300004", "SZ", "2018-01-02", "软件服务")
    add_stock(con, "688005", "SH", "2018-01-02", "半导体")
    add_stock(con, "830006", "BJ", "2018-01-02", "专用机械")
    add_stock(con, "002007", "SZ", dates[60], "专用机械")
    add_stock(con, "000008", "SZ", "2018-01-02", "元器件")
    add_stock(con, "000009", "SZ", "2018-01-02", "医疗保健")
    add_stock(con, "001010", "SZ", "2018-01-02", "家用电器", name="ST名义今天")
    con.execute("INSERT INTO name_history VALUES (?,?,?,?,?,?)",
                ("600003", "ST三机", "2021-01-04", "2022-06-01", "2021-01-04", "被ST"))
    con.execute("INSERT INTO name_history VALUES (?,?,?,?,?,?)",
                ("600003", "三机", "2022-06-01", "", "2022-06-01", "撤销ST"))
    con.commit()
    return con


def _panel(con, dates, extra_cols=(), need_turnover=False):
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM daily_quotes ORDER BY code")]
    return fs.load_panel(con, dates[0], dates[-1], need_turnover=need_turnover,
                         extra_cols=extra_cols, codes=codes)


def test_universe_filters(tmp_path):
    dates = bdays("2021-09-13", 320)
    con = _mk_universe_db(tmp_path, dates)
    panel = _panel(con, dates)
    meta = fs.load_static_meta(con)
    ev = fs.st_events(con)
    i_st = dates.index("2022-03-15")                  # ST 区间内(摘帽 6-01 之前)
    uni = set(fs.universe_at(panel, i_st, meta, fs.st_codes_at(ev, dates[i_st])))
    assert "600001" in uni                            # 普通沪主板
    assert "600002" not in uni                        # 银行:现金流口径不可比
    assert "600003" not in uni                        # 当期 ST(PIT)
    assert "830006" not in uni                        # 北交所
    assert "002007" not in uni                        # 次新(<120 交易日)
    assert "000008" not in uni                        # 无市值 → 不可算收益率
    assert "000009" not in uni                        # 无行情
    assert "001010" in uni                            # 今天名字带 ST、历史从未 ⇒ 不误杀
    i_ok = dates.index("2022-08-15")                  # 摘帽之后
    uni2 = set(fs.universe_at(panel, i_ok, meta, fs.st_codes_at(ev, dates[i_ok])))
    assert "600003" in uni2
    assert "002007" in uni2                           # 满 120 交易日后自动进样本
    mb = set(fs.universe_at(panel, i_ok, meta, fs.st_codes_at(ev, dates[i_ok]),
                            "mainboard"))
    assert "300004" not in mb and "688005" not in mb and "600001" in mb


def test_universe_minmv30_drops_small_tail(tmp_path):
    """minmv30 变体:剔掉截面最小 30% 市值(Liu-Stambaugh-Yuan 壳价值口径)。"""
    dates = bdays("2021-09-13", 140)
    con = make_db(tmp_path)
    codes = ["6000%02d" % k for k in range(10)]
    for d in dates:
        for k, code in enumerate(codes):
            add_q(con, code, d, 10.0, mv=float(1000 * (k + 1)))
    for code in codes:
        add_stock(con, code, "SH", "2018-01-02", "机械基件")
    con.commit()
    panel = _panel(con, dates)
    meta = fs.load_static_meta(con)
    uni = list(fs.universe_at(panel, 130, meta, set(), "base"))
    small = list(fs.universe_at(panel, 130, meta, set(), "minmv30"))
    assert len(uni) == 10 and len(small) == 7
    assert set(small) == set(codes[3:])               # 最小 3 只被剔


def test_st_events_empty_table_is_fail_open(tmp_path):
    """name_history 空表 → 不判任何票为 ST(与 backfill_dividends 口径一致)。"""
    con = make_db(tmp_path)
    assert fs.st_events(con) == []
    assert fs.st_codes_at([], "2024-01-02") == set()


# --------------------------------------------------------------- 评估函数
def test_rank_ic_hand_reconcile():
    pred = pd.Series({"a": 3.0, "b": 1.0, "c": 2.0, "d": 5.0, "e": 4.0})
    y = pd.Series({"a": 0.3, "b": 0.1, "c": 0.2, "d": 0.5, "e": 0.4})
    assert fs.rank_ic(pred, y) == pytest.approx(1.0)
    assert fs.rank_ic(pred, -y) == pytest.approx(-1.0)
    assert fs.rank_ic(pred, np.exp(y * 10)) == pytest.approx(1.0)   # 只看秩
    # 手算:Spearman = cov(rank)/sd 之积;这里造一个非平凡值对账
    p = pd.Series({"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0, "e": 5.0})
    r = pd.Series({"a": 5.0, "b": 4.0, "c": 3.0, "d": 1.0, "e": 2.0})
    d2 = sum((a - b) ** 2 for a, b in zip([1, 2, 3, 4, 5], [5, 4, 3, 1, 2]))
    assert fs.rank_ic(p, r) == pytest.approx(1.0 - 6.0 * d2 / (5 * 24), abs=1e-9)
    assert np.isnan(fs.rank_ic(p.head(3), r.head(3)))               # 样本太少


def test_fwd_return_requires_both_endpoints(tmp_path):
    dates = bdays("2022-01-03", 6)
    con = make_db(tmp_path)
    for i, d in enumerate(dates):
        add_q(con, "600001", d, 10.0 * (1.01 ** i), adj=2.0, mv=1e6)
        add_q(con, "600002", d, 10.0, mv=1e6)
    con.execute("UPDATE daily_quotes SET close=NULL WHERE code='600002' "
                "AND date=?", (dates[4],))            # 终点停牌/退市
    con.commit()
    panel = _panel(con, dates)
    y = fs.fwd_return(panel, 0, 4)
    assert y.loc["600001"] == pytest.approx(1.01 ** 4 - 1)     # hfq=close×adj
    assert "600002" not in y.index                            # 缺终点 → 不进样本
    assert fs.fwd_return(panel, 1, 5).empty                   # 越界 → 空


def test_summarize_ic_yearly_signs():
    dates, ics = [], []
    for y, vals in ((2022, [0.05, 0.04]), (2023, [0.06]), (2024, [-0.01]),
                    (2025, [0.02]), (2026, [0.03])):
        for k, v in enumerate(vals):
            dates.append("%d-06-%02d" % (y, k + 1))
            ics.append(v)
    s = fs.summarize_ic(ics, dates, lag=1)
    assert s["n"] == 6
    assert set(s["by_year"]) == {"2022", "2023", "2024", "2025", "2026"}
    assert s["sign_years"] == 5 and s["pos_years"] == 4        # 只有 2024 反号
    assert s["ic_mean"] == pytest.approx(float(np.mean(ics)), abs=1e-4)
    assert s["by_year"]["2024"]["ic_mean"] == pytest.approx(-0.01)
    # 门槛三条件逐一独立可判:同号年数 4/5 达标、IC 达标、t 由 _nw_tstat 决定
    assert (s["pos_years"] >= fs.GATE_YEARS) is True
    assert (abs(s["ic_mean"]) >= fs.GATE_IC) is True
    assert fs.pass_gate(s) is (abs(s["ic_t"]) >= fs.GATE_T)


def test_quintile_returns_is_ascending_by_factor(tmp_path):
    """升序分位:Q1 = 因子最低、Q5 = 最高(单向性检验的读法)。"""
    dates = bdays("2022-01-03", 4)
    con = make_db(tmp_path)
    codes = ["600%03d" % k for k in range(25)]
    for i, d in enumerate(dates):
        for k, code in enumerate(codes):
            add_q(con, code, d, 10.0 * (1.002 ** (k + 1)) ** i, mv=1e6)
    con.commit()
    panel = _panel(con, dates)
    fac = pd.Series({c: float(k) for k, c in enumerate(codes)})
    q = fs.quintile_returns(panel, 0, 3, fac)
    assert len(q) == 5
    assert all(q[j] < q[j + 1] for j in range(4))
    g = np.array([1.002 ** (k + 1) for k in range(25)])
    assert q[0] == pytest.approx(float((g[:5] ** 3 - 1).mean()))
    assert q[4] == pytest.approx(float((g[20:] ** 3 - 1).mean()))
    # 样本不足(每组 <5)→ 不硬分
    assert all(np.isnan(x) for x in fs.quintile_returns(panel, 0, 3, fac.head(10)))


def test_residualize_is_orthogonal_to_control():
    """残差与控制变量截面正交;因子若是控制变量的仿射函数 → 残差归零。"""
    x = pd.Series({c: float(k + 1) for k, c in enumerate("abcdefghijklmnopqrstuvwx")})
    r = fs.residualize(2.0 + 0.5 * x, x)
    assert np.allclose(r.values, 0.0, atol=1e-9)
    noise = np.tile([1.0, -1.0], 12)
    r2 = fs.residualize(0.5 * x + noise, x)
    assert abs(float(np.corrcoef(r2.values, x.values)[0, 1])) < 1e-9
    assert np.std(r2.values) > 0.5
    assert fs.residualize(x.head(5), x.head(5)[::-1]).empty    # 样本太少不硬算


# --------------------------------------------------------------- 端到端小宇宙
def _mk_alpha_db(tmp_path, n=40):
    """手搓一个「FcfY 有超额、CFOY 反向」的确定性宇宙。

    市值一律 1e6 万元(=1e10 元);FCF/mv 随 k 递增 ⇒ FcfY 递增;capex/mv 随 k
    递减且降幅更大 ⇒ CFOY 递减;日涨幅随 k 递增 ⇒ 前视收益与 FcfY 同序、与
    CFOY 反序。于是 FcfY 的 rank-IC 必为 +1、CFOY 必为 -1 —— 这一条同时核对了
    单位换算(万元×1e4)、分子用 FCF(扣 capex)而不是 CFO、以及整条评估链路。
    """
    dates = bdays("2021-09-13", 640)
    con = make_db(tmp_path)
    codes = ["600%03d" % k for k in range(n)]
    for i, d in enumerate(dates):
        for k, code in enumerate(codes):
            add_q(con, code, d, 10.0 * (1.0 + 0.0002 * (k + 1)) ** i,
                  mv=1e6, turnover=1.0)
    for k, code in enumerate(codes):
        add_stock(con, code, "SH", "2018-01-02", "机械基件")
        fcf = 0.01 * (k + 1) * 1e6 * fs.MV_UNIT
        capex = (0.9 / n) * (n - k) * 1e6 * fs.MV_UNIT
        add_cf(con, code, "2021-12-31", "2022-04-20", fcf + capex, capex)
        add_cf(con, code, "2022-12-31", "2023-04-20", fcf + capex, capex)
    con.commit()
    return con, dates


def test_run_study_end_to_end_on_fake_db(tmp_path):
    con, dates = _mk_alpha_db(tmp_path)
    panel = _panel(con, dates)
    out = fs.run_study(con, panel, start="2022-05-06", steps=(20,),
                       incremental=False, log=lambda *a, **k: None)
    prim = out["trials"]["fcfy|base|20"]
    assert prim["ic_mean"] == pytest.approx(1.0, abs=1e-9)
    assert prim["n"] > 5 and prim["names_per_date"] == 40
    assert prim["nw_lag"] == 19 and prim["horizon"] == 20
    # 逐年同号:fake 面板只覆盖 2022-2024 ⇒ 门槛的「≥4/5 年」结构性不可能满足;
    # 且 IC 恒为 1(零方差)时 _nw_tstat 按设计返回 NaN(不给伪 t)。两者都要如实反映。
    assert prim["sign_years"] == 3 and prim["pos_years"] == 3
    assert prim["ic_t"] != prim["ic_t"]                       # NaN
    assert prim["gate_pass"] is False
    assert prim["quantiles_pct_fwd"][0] < prim["quantiles_pct_fwd"][-1]
    # 反号对照:CFOY 在这个宇宙里必然 IC = -1(账本里的变体不是装饰)
    assert out["trials"]["cfoy|base|20"]["ic_mean"] == pytest.approx(-1.0, abs=1e-9)
    assert out["trials"]["cfoy|base|20"]["gate_pass"] is False
    assert out["gate_pass"] is False                          # 同上:窗口不足
    # 全部 18 个 trial 都入账(多重检验账本完整性)
    assert len(out["trials"]) == len(fs.trial_configs()) == 18
    assert out["primary"] is prim
    assert min(prim["by_year"]) >= "2022"
    # 前视硬核对:报表 2022-04-20 公告 ⇒ 4-15 的调仓日因子表必须是空的
    i_before = panel.i_of("2022-04-15")
    assert fs.factor_frame(con, dates[i_before],
                           panel.series(panel.mv, i_before)).empty
    assert not fs.factor_frame(con, dates[panel.i_of("2022-05-06")],
                               panel.series(panel.mv, panel.i_of("2022-05-06"))).empty


def test_run_study_reports_factor_coverage(tmp_path):
    """覆盖率诊断 = 可交易宇宙里当日算得出 FcfY 的比例(不是表长之比)。"""
    con, dates = _mk_alpha_db(tmp_path)
    panel = _panel(con, dates)
    # 制造缺口:一半股票的现金流记录一律"未公告"→ 它们不进因子表
    con.execute("UPDATE cashflow_items SET ann_date='2099-01-01' "
                "WHERE code IN (SELECT code FROM cashflow_items GROUP BY code "
                "HAVING CAST(SUBSTR(code,-1,1) AS INT) % 2 = 0)")
    con.commit()
    out = fs.run_study(con, panel, start="2022-05-06", steps=(20,),
                       incremental=False, log=lambda *a, **k: None)
    cov = out["factor_coverage_of_tradable_universe"]
    assert 0.4 < cov < 0.61                     # 一半票有 PIT 可见报表
    assert out["pit_diagnostics"]["guard_dropped_total"] == 0


def test_run_study_incremental_block(tmp_path):
    con, dates = _mk_alpha_db(tmp_path, n=60)
    panel = fs.load_panel(con, dates[0], dates[-1], need_turnover=True,
                          extra_cols=("pe_ttm", "pb", "dv_ratio"))
    out = fs.run_study(con, panel, start="2022-05-06", steps=(20,),
                       incremental=True, log=lambda *a, **k: None)
    inc = out["incremental"]
    assert inc is not None and inc["n_dates"] > 5
    assert inc["fcfy_raw_ic"]["ic_mean"] == pytest.approx(1.0, abs=1e-9)
    # fake 宇宙的 composite 里动量(+k)与反转(-k)相互抵消 ⇒ 同序但不到 1
    assert 0.5 < inc["mean_rank_corr_with_composite"] <= 1.0
    assert abs(inc["residual_ic_vs_composite"]["ic_mean"]) <= 1.0
    assert "by_year" in inc["residual_ic_vs_composite"]


def test_composite_at_is_finite_and_uses_pit_financials(tmp_path):
    """composite 复现:动量/估值/成长都有值;财报未公告的成长项不得进因子。"""
    con, dates = _mk_alpha_db(tmp_path, n=12)
    panel = fs.load_panel(con, dates[0], dates[-1], need_turnover=True,
                          extra_cols=("pe_ttm", "pb", "dv_ratio"))
    meta = fs.load_static_meta(con)
    i = panel.i_of("2022-05-06")
    fin = fs.latest_financials(con, dates[i])
    assert fin.empty                                  # fake 库没有 financials
    comp = fs.composite_at(panel, i, meta, fin)
    assert len(comp) == len(panel.codes)
    assert comp.notna().sum() >= 10                   # 12 因子的等权 masked mean
    con.execute("INSERT INTO financials VALUES (?,?,?,?,?,?,?,?,?)",
                ("600001", "2022-03-31", "2022-06-01", "", 8.0, 1.0, 10.0,
                 50.0, 30.0))
    con.commit()
    # ann_date 在 D 之后 → 不可见(PIT);把 asof 推到公告之后才可见
    assert fs.latest_financials(con, dates[i]).empty
    fin2 = fs.latest_financials(con, dates[-1])
    assert float(fin2.loc["600001", "netprofit_yoy"]) == 50.0


def test_pass_gate_conditions_are_all_required():
    """门槛三条件(|IC|、|NW t|、逐年同号)缺一不可。"""

    def s(ic, t_, pos, sign=5):
        return {"ic_mean": ic, "ic_t": t_, "pos_years": pos, "sign_years": sign}

    assert fs.pass_gate(s(0.04, 3.0, 5)) is True
    assert fs.pass_gate(s(0.02, 3.0, 5)) is False      # IC 太弱
    assert fs.pass_gate(s(0.04, 1.8, 5)) is False      # t 不足
    assert fs.pass_gate(s(0.04, 3.0, 3)) is False      # 逐年符号不一致
    assert fs.pass_gate(s(-0.05, -3.1, 5)) is True     # 反向因子同样计入(绝对值)
    assert fs.pass_gate({"ic_mean": None, "ic_t": None, "pos_years": 0,
                         "sign_years": 0}) is False    # 无样本


def test_listing_age_uses_list_date_not_panel_start(tmp_path):
    """回归:上市年龄必须按 list_date 起算。

    面板起点(库内最早行情日)晚于老票的上市日 —— 若用「面板内序号差」当
    年龄,2022 年 Q1 的整段样本会被误判成「全是次新」而宇宙清空(真实踩过)。
    """
    dates = bdays("2021-09-13", 200)
    con = make_db(tmp_path)
    old = ["6000%02d" % k for k in range(6)]          # 2018 年就上市
    new = ["6010%02d" % k for k in range(6)]          # 2021-12-20 才上市
    for i, d in enumerate(dates):
        for c in old:
            add_q(con, c, d, 10.0, mv=1e6)
        if d >= "2021-12-20":
            for c in new:
                add_q(con, c, d, 10.0, mv=1e6)
    for c in old:
        add_stock(con, c, "SH", "2018-01-02", "机械基件")
    for c in new:
        add_stock(con, c, "SH", "2021-12-20", "机械基件")
    con.commit()
    panel = fs.load_panel(con, dates[0], dates[-1])
    meta = fs.load_static_meta(con)
    i_jan = dates.index("2022-01-10")                 # 面板内只有第 84 个交易日
    uni = set(fs.universe_at(panel, i_jan, meta, set(), "base"))
    assert set(old) <= uni, "2018 年上市的老票在 2022-01 必须在样本里"
    assert not (set(new) & uni), "上市不满 120 交易日的次新必须剔除"
    late = set(fs.universe_at(panel, len(dates) - 1, meta, set(), "base"))
    assert set(new) <= late                           # 半年后自动进样本
