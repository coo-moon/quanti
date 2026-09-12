"""FcfY 组合回测 + 过拟合闸的单测:全注入 fake 面板,零触网零真库。

覆盖成本口径(与引擎同源的那三个对象)、次日开盘成交、日频路径复利、
网格抽样对齐、红利税三档、绩效度量。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import fcfy_backtest as fb  # noqa: E402
import fcfy_study as fs  # noqa: E402
from quanti.backtest.commission import AShareCommission  # noqa: E402
from quanti.backtest.slippage import VolumeImpactSlippage  # noqa: E402


def mk_panel(codes, dates, close, open_=None, amount=None, dv=None):
    T, N = len(dates), len(codes)
    close = np.asarray(close, dtype=np.float64)
    panel = fs.Panel(
        dates=list(dates), codes=list(codes), close=close,
        open_=np.asarray(open_, dtype=np.float64) if open_ is not None else close.copy(),
        amount=np.asarray(amount, dtype=np.float64) if amount is not None
        else np.full((T, N), 1e8),
        turnover=None,
        mv=np.full((T, N), 1e5),
        extra={"dv_ratio": np.asarray(dv, dtype=np.float64) if dv is not None
               else np.full((T, N), 4.0)})
    return panel


# --------------------------------------------------------------------- 成本
def test_period_cost_components_match_engine():
    comm, slip = AShareCommission(), VolumeImpactSlippage()
    adv = {"a": 1e9}
    # 买 100 万元、ADV 1e9 ⇒ 参与率 100*1e6/1e9 = 0.1% → 冲击 = 5 + 5*√0.1 bp
    c = fb.period_cost({"a": 1e6}, {}, adv, pd.Timestamp("2024-01-02").date(),
                       comm, slip)
    exp_impact = (5.0 + 5.0 * (0.1 ** 0.5)) / 1e4 * 1e6
    assert c["impact"] == pytest.approx(exp_impact)
    assert c["fee"] == pytest.approx(1e6 * 0.00025 + 1e6 * 0.00001)   # 佣金+过户费
    assert c["total"] == pytest.approx(c["impact"] + c["fee"])
    # 卖出多一道印花税(2023-08-28 之后万5;之前千1)
    s24 = fb.period_cost({}, {"a": 1e6}, adv, pd.Timestamp("2024-01-02").date(),
                         comm, slip)
    s22 = fb.period_cost({}, {"a": 1e6}, adv, pd.Timestamp("2022-01-04").date(),
                         comm, slip)
    assert s24["fee"] - c["fee"] == pytest.approx(1e6 * 0.0005)
    assert s22["fee"] - c["fee"] == pytest.approx(1e6 * 0.001)


def test_period_cost_min_commission_floor_bites_small_accounts():
    """小资金:5 元下限按**每票子单**生效(容量结论的关键机制之一)。"""
    comm, slip = AShareCommission(), VolumeImpactSlippage()
    notional = {("a%d" % k): 1000.0 for k in range(30)}      # 每票 1000 元
    c = fb.period_cost(notional, {}, {}, pd.Timestamp("2024-01-02").date(),
                       comm, slip)
    assert c["fee"] == pytest.approx(30 * (5.0 + 1000 * 1e-5))   # 下限 + 过户费
    big = fb.period_cost({"a": 30_000.0}, {}, {}, pd.Timestamp("2024-01-02").date(),
                         comm, slip)
    assert big["fee"] == pytest.approx(max(30_000 * 0.00025, 5.0) + 30_000 * 0.00001)


def test_adv20_uses_only_data_before_signal_day():
    dates = ["2024-01-%02d" % k for k in range(1, 26)]
    amount = np.full((25, 1), 100.0)
    panel = mk_panel(["600001"], dates, close=np.full((25, 1), 10.0), amount=amount)
    adv = fb.adv20_map(panel, 20)
    assert adv["600001"] == pytest.approx(100.0)
    panel.amount[20, 0] = 9e9            # 信号日**当天及之后**不得影响 ADV20
    panel.amount[21, 0] = 9e9
    assert fb.adv20_map(panel, 20)["600001"] == pytest.approx(100.0)
    panel.amount[19, 0] = 200.0          # 窗口内(D 之前)的最后一行必须生效
    assert fb.adv20_map(panel, 20)["600001"] == pytest.approx((19 * 100 + 200) / 20)


def test_dividend_tax_frac_three_bands():
    dates = ["2024-01-0%d" % k for k in range(1, 5)]
    panel = mk_panel(["600001"], dates, close=np.full((4, 1), 10.0),
                     dv=np.full((4, 1), 4.0))              # 股息率 4%
    w = {"600001": 1.0}
    assert fb.dividend_tax_frac(panel, 0, w, 28) == pytest.approx(
        0.04 * (28 / 365) * 0.20)        # ≤30 天 → 20%
    assert fb.dividend_tax_frac(panel, 0, w, 100) == pytest.approx(
        0.04 * (100 / 365) * 0.10)       # ≤1 年 → 10%
    assert fb.dividend_tax_frac(panel, 0, w, 400) == 0.0   # >1 年 → 免


# ------------------------------------------------------------------ 组合模拟
def test_simulate_enters_at_next_open_and_drifts_weights():
    """信号日 D 的次日**开盘**建仓;持有期等权 → 权重漂移;成本一次性扣在建仓日。"""
    dates = ["2024-01-%02d" % k for k in range(1, 23)]
    T = len(dates)
    codes = ["600001", "600002"]
    close = np.array([[10.0, 20.0]] * T)
    close[2:, 0] = 11.0                       # 建仓次日:600001 +10%
    close[2:, 1] = 19.0                       #                     600002 -5%
    open_ = close.copy()
    open_[0, :] = [9.0, 21.0]                 # 信号日的价格不得参与成交
    panel = mk_panel(codes, dates, close, open_=open_,
                     amount=np.full((T, 2), 1e10), dv=np.zeros((T, 2)))
    ranked = {0: ["600001", "600002"]}
    uni = {0: pd.Index(codes)}
    sim = fb.simulate(panel, ranked, uni, step=20, cash=1_000_000.0)
    assert sim.n_periods == 1
    per = sim.periods[0]
    assert per["entry"] == dates[1] and per["exit"] == dates[21]   # 次日开盘
    # 建仓日就扣掉全部成本(买入 100% 仓位),当日 open==close ⇒ 只有成本
    assert sim.daily[1] < 0.0
    gross = 0.5 * (11.0 / 10.0 - 1.0) + 0.5 * (19.0 / 20.0 - 1.0)      # +2.5%
    # 只有建仓日(成本)与次日(价格移动)有非零日收益,之后一路走平
    assert sim.daily[2] == pytest.approx(gross)
    assert per["net"] == pytest.approx((1.0 + sim.daily[1]) * (1.0 + sim.daily[2])
                                       - 1.0)
    assert per["net"] < gross                     # 成本吃掉一部分
    assert per["n_hold"] == 2
    assert per["fee"] > 0 and per["impact"] > 0
    assert per["div_tax"] == 0.0                  # dv=0 ⇒ 无税


def test_simulate_no_rebalance_when_ranking_unchanged():
    """同一批持仓不变 ⇒ 第二期只有再平衡换手(等权漂移回来),换手 < 100%。"""
    dates = ["2024-%02d-%02d" % (m, d) for m in (1, 2, 3) for d in range(1, 21)]
    T, codes = len(dates), ["600001", "600002"]
    close = np.tile(np.array([10.0, 10.0]), (T, 1))
    panel = mk_panel(codes, dates, close, amount=np.full((T, 2), 1e10),
                     dv=np.zeros((T, 2)))
    ranked = {0: codes, 20: codes}
    uni = {0: pd.Index(codes), 20: pd.Index(codes)}
    sim = fb.simulate(panel, ranked, uni, step=20, cash=1_000_000.0)
    assert sim.n_periods == 2
    assert sim.turn[0] == pytest.approx(0.5)          # 首次建仓:买 100% → 单边 0.5
    assert sim.turn[1] == pytest.approx(0.0, abs=1e-9)  # 无价格变动 ⇒ 不需调仓


def test_grid_returns_samples_real_daily_path():
    daily = pd.Series({0: 0.01, 1: 0.01, 2: 0.01, 3: -0.02, 4: 0.05})
    g = fb.grid_returns(daily, [2, 4])
    assert g[0] == pytest.approx(1.01 ** 3 - 1)
    assert g[1] == pytest.approx((0.98 * 1.05) - 1)
    # 抽样不发明观测:整段复利一致
    assert float(np.prod(1.0 + g)) == pytest.approx(float(np.prod(1.0 + daily)))
    assert fb.grid_returns(pd.Series(dtype=float), [1]).size == 0


# ------------------------------------------------------------------ 绩效度量
def test_perf_from_daily_metrics():
    dates = ["2024-01-%02d" % k for k in range(1, 21)]
    panel = mk_panel(["600001"], dates, close=np.full((20, 1), 10.0))
    p = pd.Series({i: 0.001 for i in range(20)})
    b = pd.Series({i: 0.0 for i in range(20)})
    perf = fb.perf_from_daily(p, b, panel, step=20)
    assert perf["obs"] == 20
    # 年化口径:20 个观测按 252 交易日/年折算
    assert perf["ann_return"] == pytest.approx(1.001 ** 252 - 1, abs=1e-4)
    assert perf["ann_excess_vs_ew"] == pytest.approx(1.001 ** 252 - 1, abs=1e-4)
    assert perf["te_vs_ew"] == pytest.approx(0.0)
    assert perf["max_drawdown"] == 0.0
    assert perf["by_year"]["2024"]["port"] == pytest.approx(1.001 ** 20 - 1, abs=1e-4)
    # 回撤:先涨 10% 再跌 20% → 峰谷 -20%
    p2 = pd.Series({0: 0.10, 1: -0.20})
    b2 = pd.Series({0: 0.0, 1: 0.0})
    panel2 = mk_panel(["600001"], ["2024-01-01", "2024-01-02"],
                      close=np.full((2, 1), 10.0))
    assert fb.perf_from_daily(p2, b2, panel2, 20)["max_drawdown"] == pytest.approx(-0.2)


def test_run_backtest_gates_on_fake_panel(monkeypatch, tmp_path):
    """小 fake 宇宙跑通 run_backtest:18 个 trial 全部入账、闸结构完整。"""
    sys.path.insert(0, str(ROOT / "tests"))
    import test_fcfy_study as ts
    con, dates = ts._mk_alpha_db(tmp_path, n=40)
    panel = fs.load_panel(con, dates[0], dates[-1], need_open=True,
                          need_amount=True, need_turnover=True,
                          extra_cols=("pe_ttm", "pb", "dv_ratio"))
    out = fb.run_backtest(con, panel, start="2022-05-06", steps=(20,), cash=1e6,
                          primary=fs.PRIMARY, sweep_cash=(1e7,),
                          log=lambda *a, **k: None)
    assert "error" not in out
    assert out["primary"]["n_periods"] > 5
    assert set(out["gates"]) >= {"dsr", "pbo", "turnover", "yearly_excess",
                                 "all_pass"}
    assert len(out["trial_ledger"]) >= 1
    assert out["perf_matrix"]["cols"] == list(out["trial_ledger"])
    assert 1e7 in out["capacity_sweep"]
    # fake 宇宙里 FcfY 完美预测 ⇒ 主口径必然正超额
    assert out["primary"]["perf"]["ann_excess_vs_ew"] > 0


def test_suspended_day_freezes_price_instead_of_zeroing_it():
    """持有期内停牌(当日无 bar):价格冻结,不能被当成归零。"""
    dates = ["2024-01-%02d" % k for k in range(1, 23)]
    T, codes = len(dates), ["600001", "600002"]
    close = np.tile(np.array([10.0, 20.0]), (T, 1))
    close[2:, 0] = 12.0                       # 建仓次日 +20% 后一路走平
    close[3:21, 1] = np.nan                   # 600002 中间停牌 18 天(价格不变)
    panel = mk_panel(codes, dates, close, amount=np.full((T, 2), 1e10),
                     dv=np.zeros((T, 2)))
    sim = fb.simulate(panel, {0: codes}, {0: pd.Index(codes)}, step=20, cash=1e6)
    per = sim.periods[0]
    # 冻结后 600002 全程 0% 贡献;若不冻结,停牌日会被当 -100% ⇒ 净收益爆负
    assert per["net"] > 0.05
    # 不冻结的话,停牌首日会被算成约 -50%(该票权重凭空蒸发)
    assert min(v for k, v in sim.daily.items() if k > 1) > -0.05
    assert sim.daily[2] == pytest.approx(0.5 * 0.20)   # 只有 600001 动了 +20%
