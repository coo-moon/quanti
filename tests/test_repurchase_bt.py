"""回购组合回测单测:全注入 fake 面板/fake sqlite,零触网、零真库。

覆盖任务书点名的组合层口径:
  * 新事件入池:ann_date ∈ (D−L, D](左开)、且 D0 不晚于建仓日;未来公告不可见
  * 同票多次公告取最近一次;力度降序 top-N;amount 缺失排最后
  * 「近 12 个月内有 proc='停止'」的票不入池(超过 365 自然日恢复资格)
  * 每票最长持有 60 交易日强制卖;掉出候选名单即卖
  * 次日 open 成交、下期次日 open 出清(手算净值对账)
  * 空仓期 = 现金 0 收益(不冒充基准),清仓成本照扣
  * 成本公式与引擎同源(佣金 5 元下限随资金量生效、印花税只在卖出侧、红利税三档)
  * 主基准 = 全市场等权 open→open;同规模档诊断基准的边界与切片一致
  * 升格切片选取规则(primary_config)与组合层账本规模(42 变体,一个不藏)
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
import repurchase_bt as bt  # noqa: E402
import repurchase_event_study as rp  # noqa: E402

DDL = """
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

TERC = (5e5, 2e6)          # 规模档切点(万元)


def bdays(start, n):
    return [d.date().isoformat() for d in pd.bdate_range(start, periods=n)]


def mk_panel(codes, dates, mv=1e6):
    T, N = len(dates), len(codes)
    close = np.full((T, N), 10.0)
    return fs.Panel(dates=list(dates), codes=list(codes), close=close,
                    open_=close.copy(),
                    amount=np.full((T, N), 1e9), turnover=None,
                    mv=np.full((T, N), float(mv)),
                    extra={"dv_ratio": np.zeros((T, N))})


def flat_panel(codes, dates, close=None, open_=None, mv=1e6, adv=1e9, dv=0.0):
    T, N = len(dates), len(codes)
    close = np.full((T, N), 10.0) if close is None else np.asarray(
        close, dtype=np.float64)
    open_ = close.copy() if open_ is None else np.asarray(open_, dtype=np.float64)
    return fs.Panel(dates=list(dates), codes=list(codes), close=close,
                    open_=open_, amount=np.full((T, N), float(adv)),
                    turnover=None, mv=np.full((T, N), float(mv)),
                    extra={"dv_ratio": np.full((T, N), float(dv))})


def mk_table(rows):
    """rows: [(code, ann_date, d0i, amount, mv_wan)] → EventTable。"""
    f = pd.DataFrame({
        "code": [r[0] for r in rows], "ann_date": [r[1] for r in rows],
        "d0i": [r[2] for r in rows], "d0": ["-"] * len(rows),
        "amount": [r[3] for r in rows], "mv_wan": [r[4] for r in rows],
        "intensity": [(r[3] / (r[4] * rp.MV_UNIT))
                      if (r[3] == r[3] and r[4] == r[4] and r[4] > 0)
                      else np.nan for r in rows]})
    return rp.EventTable(f)


# ------------------------------------------------------------------ 候选池
def test_pool_only_sees_announced_and_recent(tmp_path):
    """PIT + 候选窗:未来公告不可见;超出 L 的旧公告出池(左开 (D−L, D])。"""
    dates = bdays("2022-01-03", 120)
    panel = mk_panel(["600001", "600002", "600003"], dates)
    et = mk_table([
        ("600001", dates[85], 86, 5e7, 1e6),      # 15 个交易日前 ⇒ L=20 窗内
        ("600002", dates[80], 81, 5e7, 1e6),      # 正好 20 个 ⇒ 左开区间 ⇒ 出池
        ("600003", dates[110], 111, 5e7, 1e6)])   # 未来 ⇒ 不可见
    pool = bt.build_pool(panel, et, "预案", "all", 20, TERC, {}, [100])
    assert pool[100] == ["600001"]
    pool40 = bt.build_pool(panel, et, "预案", "all", 40, TERC, {}, [100])
    assert set(pool40[100]) == {"600001", "600002"}
    assert "600003" not in pool40[100], "ann_date > D 的行绝不能进池"


def test_pool_requires_event_tradeable_by_entry_day(tmp_path):
    """公告在 D 当天(D0=D+1)允许;D0 被停牌拖到晚于建仓日 ⇒ 出池。"""
    dates = bdays("2022-01-03", 120)
    panel = mk_panel(["600001"], dates)
    on_d = mk_table([("600001", dates[100], 101, 5e7, 1e6)])
    assert bt.build_pool(panel, on_d, "预案", "all", 20, TERC, {}, [100])[100] \
        == ["600001"], "D 日收盘出信号、D+1(=D0)开盘买入,合法"
    late = mk_table([("600001", dates[100], 110, 5e7, 1e6)])   # D0 拖到 +9 日
    assert bt.build_pool(panel, late, "预案", "all", 20, TERC, {}, [100]) == {}


def test_pool_ranks_by_intensity_and_caps_top_n(tmp_path):
    """力度 amount/total_mv 降序;top-N 截断;缺力度的排最后。"""
    dates = bdays("2022-01-03", 120)
    panel = mk_panel(["6000%02d" % k for k in range(4)], dates)
    et = mk_table([("600000", dates[95], 96, 1e7, 1e6),       # 力度 1%
                   ("600001", dates[96], 97, 5e7, 1e6),       # 力度 5%
                   ("600002", dates[97], 98, 3e7, 1e6),       # 力度 3%
                   ("600003", dates[98], 99, np.nan, 1e6)])   # 缺 amount
    pool = bt.build_pool(panel, et, "预案", "all", 20, TERC, {}, [100], top_n=2)
    assert pool[100] == ["600001", "600002"]
    full = bt.build_pool(panel, et, "预案", "all", 20, TERC, {}, [100], top_n=99)
    assert full[100] == ["600001", "600002", "600000", "600003"]


def test_pool_same_code_takes_latest_announcement(tmp_path):
    """同票两条公告在窗内 ⇒ 只占一个仓位,用最近一次的力度。"""
    dates = bdays("2022-01-03", 120)
    panel = mk_panel(["600001", "600002"], dates)
    et = mk_table([("600001", dates[85], 86, 9e7, 1e6),      # 老:力度 9%
                   ("600001", dates[95], 96, 1e7, 1e6),      # 新:力度 1%
                   ("600002", dates[96], 97, 2e7, 1e6)])     # 力度 2%
    pool = bt.build_pool(panel, et, "预案", "all", 20, TERC, {}, [100])
    assert pool[100] == ["600002", "600001"], "按**最新**公告的力度排序"


def test_pool_excludes_recent_stop_then_recovers(tmp_path):
    """近 12 个月内有 proc='停止' ⇒ 不入池;超过 365 自然日恢复资格。"""
    dates = bdays("2022-01-03", 500)
    panel = mk_panel(["600001", "600002"], dates)
    et = mk_table([("600001", dates[430], 431, 5e7, 1e6),
                   ("600002", dates[430], 431, 6e7, 1e6)])
    pool = bt.build_pool(panel, et, "预案", "all", 20, TERC,
                         {"600001": [dates[420]]}, [435])
    assert pool[435] == ["600002"]
    pool2 = bt.build_pool(panel, et, "预案", "all", 20, TERC,
                          {"600001": [dates[100]]}, [435])
    # 600002 力度更大(6e7 vs 5e7)⇒ 排在前,600001 冷静期过后回到名单
    assert pool2[435] == ["600002", "600001"], "已过冷静期 ⇒ 恢复资格"
    assert bt.load_stop_dates.__doc__


def test_load_stop_dates_reads_stop_rows(tmp_path):
    con = sqlite3.connect(tmp_path / "f.db")
    con.executescript(DDL)
    con.execute("INSERT INTO repurchase_events (code, ann_date, proc, amount) "
                "VALUES ('600001','2024-03-01','停止',1e7)")
    con.execute("INSERT INTO repurchase_events (code, ann_date, proc, amount) "
                "VALUES ('600001','2024-01-05','预案',1e7)")
    con.execute("INSERT INTO repurchase_events (code, ann_date, proc, amount) "
                "VALUES ('600002','2024-02-05','停止',-1.0)")
    con.commit()
    assert bt.load_stop_dates(con) == {"600001": ["2024-03-01"],
                                       "600002": ["2024-02-05"]}


def test_pool_respects_size_slice(tmp_path):
    """规模切片用**公告日当时**的市值(万元),与第一步同一批边界。"""
    dates = bdays("2022-01-03", 120)
    panel = mk_panel(["600001", "600002"], dates)
    et = mk_table([("600001", dates[95], 96, 5e7, 1e5),      # 小盘(1e5 万元)
                   ("600002", dates[96], 97, 5e7, 9e6)])     # 大盘
    assert bt.build_pool(panel, et, "预案", "size_small", 20, TERC, {},
                         [100])[100] == ["600001"]
    assert bt.build_pool(panel, et, "预案", "size_large", 20, TERC, {},
                         [100])[100] == ["600002"]
    assert bt.build_pool(panel, et, "完成", "intensity_hi", 20, TERC, {},
                         [100]) == {}, "力度切片只属预案口径"
    # 600001 力度 5%=5e7/1e9元 ≥0.1% ⇒ hi;600002 只有 0.055% ⇒ lo
    assert bt.build_pool(panel, et, "预案", "intensity_hi", 20, TERC, {},
                         [100])[100] == ["600001"]
    assert bt.build_pool(panel, et, "预案", "intensity_lo", 20, TERC, {},
                         [100])[100] == ["600002"]


# ------------------------------------------------------------------ 模拟
def test_entry_and_exit_use_next_day_open(tmp_path):
    """信号日 D=i → 次日 open 建仓;下期次日 open 出清(手算净值)。"""
    dates = bdays("2022-01-03", 40)
    open_ = np.full((40, 1), 10.0)
    open_[16, 0] = 12.0            # 出清日 = 下一信号日(30)+1 = 16 的开盘价
    panel = flat_panel(["600001"], dates, open_=open_)
    sim = bt.simulate(panel, {10: ["600001"]}, [10, 30], cash=1e6, step=5)
    per = sim.periods[0]
    assert per["signal"] == dates[10]
    assert per["entry"] == dates[11] and per["exit"] == dates[16]
    # 建仓价 open(11)=10 ⇒ 持有段无涨跌;出清价 open(16)=12 ⇒ 毛收益 +20%
    assert per["net"] == pytest.approx(0.2 - 1.2 * per["cost_frac"], abs=1e-9)
    assert sim.periods[1]["n_hold"] == 0
    assert sim.periods[1]["net"] == pytest.approx(-sim.periods[1]["cost_frac"])


def test_empty_period_is_cash_not_benchmark(tmp_path):
    """无候选期收益 0(扣清仓成本),基准另算 ⇒ 不会偷偷「跟上指数」。"""
    dates = bdays("2022-01-03", 40)
    close = np.linspace(10, 30, 40).reshape(-1, 1)
    panel = flat_panel(["600001"], dates, close=close)
    sim = bt.simulate(panel, {}, [10, 20], cash=1e6)
    assert all(p["net"] == pytest.approx(0.0) for p in sim.periods)
    assert sim.daily and all(v == 0.0 for v in sim.daily.values())
    assert any(v != 0.0 for v in sim.daily_bench.values()), "基准仍在记录"


def test_max_hold_forces_exit_after_60_days(tmp_path):
    """每票最长持有 60 交易日:第 4 期(再持有会满 80 日)强制挤出。"""
    dates = bdays("2022-01-03", 120)
    panel = mk_panel(["600001"], dates)
    grid = [0, 20, 40, 60, 80]
    sim = bt.simulate(panel, {i: ["600001"] for i in grid}, grid, cash=1e6,
                      step=20, max_hold=60)
    holds = [p["n_hold"] for p in sim.periods]
    forced = [p["n_forced_out"] for p in sim.periods]
    assert holds[:3] == [1, 1, 1] and forced[:3] == [0, 0, 0]
    assert holds[3] == 0 and forced[3] == 1, "满 60 交易日强退"
    assert holds[4] == 1, "退掉后若仍在候选名单可重买(持有期重新计时)"


def test_dropped_from_pool_is_sold_next_open(tmp_path):
    """掉出候选名单 ⇒ 下一期次日开盘卖出,只留新名单。"""
    dates = bdays("2022-01-03", 60)
    panel = mk_panel(["600001", "600002"], dates)
    sim = bt.simulate(panel, {0: ["600001", "600002"], 20: ["600001"]},
                      [0, 20], cash=1e6, step=20)
    assert sim.periods[0]["n_hold"] == 2
    assert sim.periods[1]["n_hold"] == 1
    assert sim.periods[1]["names"] == ["600001"]
    assert sim.periods[1]["turn"] > 0.2, "卖出半仓 ⇒ 单边换手 >20%"


def test_cost_floor_bites_at_small_cash(tmp_path):
    """5 元佣金下限逐票生效 ⇒ 小资金档成本率显著更高(容量效应)。"""
    dates = bdays("2022-01-03", 40)
    codes = ["6000%02d" % k for k in range(10)]
    panel = flat_panel(codes, dates, adv=5e7)
    pool = {10: list(codes)}
    big = bt.simulate(panel, pool, [10], cash=2e6, step=5)
    small = bt.simulate(panel, pool, [10], cash=2e4, step=5)
    # 大资金档 20 万元/票:佣金万2.5 + 过户费十万分之一 = 2.6bp(不触下限)
    assert big.periods[0]["fee"] == pytest.approx(0.00026, abs=3e-5)
    # 小资金档 2000 元/票:5 元下限 ⇒ 光佣金就 25bp
    assert small.periods[0]["fee"] > big.periods[0]["fee"] * 3


def test_sell_side_stamp_duty_only_when_rotating(tmp_path):
    """买入期只有佣金+过户费;换手期多一道印花税(卖出侧)。"""
    dates = bdays("2022-01-03", 60)
    panel = mk_panel(["600001", "600002"], dates)
    sim = bt.simulate(panel, {0: ["600001"], 20: ["600002"]}, [0, 20],
                      cash=1e6, step=20)
    buy_only, swap = sim.periods[0], sim.periods[1]
    assert swap["fee"] > buy_only["fee"] * 2


def test_dividend_tax_uses_three_tier_rate(tmp_path):
    """红利税 = dv_ratio × 持有年数 × 三档税率(hfq 表达不出来的那部分)。"""
    from quanti.backtest.commission import dividend_tax_rate
    dates = bdays("2022-01-03", 60)
    panel = flat_panel(["600001"], dates, dv=4.0)
    sim = bt.simulate(panel, {0: ["600001"]}, [0], cash=1e6, step=20)
    per = sim.periods[0]
    cal = (pd.Timestamp(per["exit"]).date() - pd.Timestamp(per["entry"]).date())
    rate = dividend_tax_rate(max(0, cal.days))
    assert rate > 0
    assert per["div_tax"] == pytest.approx(0.04 * (cal.days / 365.0) * rate)


def test_benchmarks_equal_weight_open_to_open(tmp_path):
    """全市场等权基准 = open→open 均值;同规模档基准只统计档内票。"""
    dates = bdays("2022-01-03", 30)
    close = np.column_stack([np.linspace(10, 20, 30), np.linspace(10, 12, 30)])
    mv = np.column_stack([np.full(30, 1e5), np.full(30, 1e7)])
    panel = fs.Panel(dates=dates, codes=["600001", "600999"], close=close,
                     open_=close.copy(), amount=np.full((30, 2), 1e9), mv=mv,
                     extra={"dv_ratio": np.zeros((30, 2))})
    assert bt.ew_open_to_open(panel.open_, 5, 15, None) == pytest.approx(
        float((close[15] / close[5] - 1).mean()))
    small = bt.band_mask(panel, 5, TERC, "size_small")
    assert bt.ew_open_to_open(panel.open_, 5, 15, small) == pytest.approx(
        close[15][0] / close[5][0] - 1)
    assert bt.band_mask(panel, 5, TERC, "all") is None
    assert bt.band_mask(panel, 5, None, "size_small") is None


# ------------------------------------------------------------------ 账本/主口径
def test_primary_config_prefers_own_primary_when_it_passes():
    study = {"gate_pass": True, "passing_variants": ["预案|all|20"]}
    assert bt.primary_config(study) == ("预案", "all", 20)


def test_primary_config_promotes_declared_slice():
    """主口径不过闸时,升格**第一个**过闸的预声明切片(不挑最好看的)。"""
    study = {"gate_pass": False,
             "passing_variants": ["完成|size_small|60", "预案|size_small|20"]}
    assert bt.primary_config(study) == ("完成", "size_small", 60)


def test_primary_config_falls_back_when_nothing_passes():
    study = {"gate_pass": False, "passing_variants": []}
    assert bt.primary_config(study) == ("预案", "all", 20)


def test_bt_ledger_covers_every_declared_variant():
    """组合层账本 = 3 proc × 预声明切片 × 3 候选窗(力度仅预案)= 42。"""
    cfgs = bt.trial_configs()
    assert len(cfgs) == 42
    assert ("预案", "intensity_hi", 40) in cfgs
    assert ("完成", "intensity_hi", 20) not in cfgs
    assert {L for _, _, L in cfgs} == set(bt.LOOKBACKS)


def test_rebalance_grid_is_common_and_fits(tmp_path):
    dates = bdays("2021-09-13", 100)
    con = sqlite3.connect(tmp_path / "f.db")
    con.executescript(DDL)
    for d in dates:
        for c in ("600001", "600002"):
            con.execute("INSERT INTO daily_quotes VALUES (?,?,?,?,?,?,?)",
                        (c, d, 10.0, 10.0, 1e8, 1.0, 1.0))
            con.execute("INSERT INTO daily_basic VALUES (?,?,?,?,?,?)",
                        (c, d, 1e6, 10.0, 1.0, 1.0))
    con.commit()
    panel = fs.load_panel(con, dates[0], dates[-1], need_open=True)
    grid = bt.rebalance_grid(panel)
    assert grid[0] == next(i for i, d in enumerate(dates)
                           if d >= rp.EVENT_START)
    assert set(np.diff(grid)) == {bt.STEP}
    assert grid[-1] + bt.STEP + 1 < len(dates), "栅格必须放得下建仓+出清"


def test_run_backtest_end_to_end_on_fake_db(tmp_path):
    """假库跑通全链:清洗 → 42 变体 → 闸矩阵结构完整、账本诚实。"""
    dates = bdays("2021-10-04", 420)
    con = sqlite3.connect(tmp_path / "f.db")
    con.executescript(DDL)
    codes = ["60%04d" % k for k in range(20)]
    for k, d in enumerate(dates):
        for j, c in enumerate(codes):
            px = 10.0 * 1.001 ** k
            con.execute("INSERT INTO daily_quotes VALUES (?,?,?,?,?,?,?)",
                        (c, d, px, px, 1e8, 1.0, 1.0))
            con.execute("INSERT INTO daily_basic VALUES (?,?,?,?,?,?)",
                        (c, d, 1e5 * (j + 1), 10.0, 1.0, 1.0))
    for c in codes:
        con.execute("INSERT INTO stocks VALUES (?,?,?,?,?,?)",
                    (c, "示例", "SH", "2018-01-02", "机械基件", ""))
    for k in range(20, 400, 30):
        for j, c in enumerate(codes[:8]):
            for proc in ("完成", "预案"):
                con.execute("INSERT INTO repurchase_events "
                            "(code, ann_date, proc, amount) VALUES (?,?,?,?)",
                            (c, dates[k], proc, 1e7 * (j + 1)))
    con.commit()
    panel = fs.load_panel(con, dates[0], dates[-1], need_open=True,
                          need_amount=True, extra_cols=("dv_ratio",))
    study = rp.run_study(con, panel, end_iso=dates[-1], log=lambda *a: None)
    out = bt.run_backtest(con, panel, study, cash=1e6, log=lambda *a: None)
    assert "error" not in out, out.get("error")
    g = out["gates"]
    assert set(g) >= {"dsr", "pbo", "turnover", "yearly_excess",
                      "yearly_excess_vs_band", "all_pass"}
    assert g["dsr"]["n_trials"] == len(out["trial_ledger"])
    li = out["ledger_integrity"]
    assert li["n_configs"] == 42 and li["in_ledger"] == len(out["trial_ledger"])
    assert out["primary"]["config"] == "%s|%s|%d" % (
        *(bt.primary_config(study)[:2]), bt.STEP)
    assert out["primary"]["n_periods"] >= 12
    assert all(p["n_hold"] <= bt.TOP_N for p in out["periods"])
    assert out["candidate_distribution"]["mean"] >= 1
    assert isinstance(g["all_pass"], bool)
    top = out["best_post_hoc_sensitivity"]["top3"][0]
    assert top["sharpe_per_grid_period"] >= max(
        v["sharpe_per_grid_period"] for v in out["trial_ledger"].values())
