"""ETF 网格核心逻辑测试(合成数据, 无网络/无DB)。

验证: 震荡序列 er 低+网格盈利+回撤小于持有; 趋势序列 er 高; 落地箱体合理。
"""
from __future__ import annotations

import numpy as np

from quanti.etf_grid.data import EtfBars
from quanti.etf_grid.grid import backtest, deployable_box, oscillation_metrics


def _bars(close: np.ndarray) -> EtfBars:
    n = len(close)
    dates = np.array([f"2025-{1 + i // 21:02d}-{1 + i % 21:02d}" for i in range(n)])
    # 假 high/low 贴着 close, amount 恒定
    return EtfBars(code="T.SH", dates=dates, close=close.copy(),
                   high=close * 1.005, low=close * 0.995,
                   raw_close=close.copy(), raw_low=close * 0.995,
                   raw_high=close * 1.005, amount=np.full(n, 2e8))


def _sawtooth(n=180, lo=10.0, hi=12.0, period=20) -> np.ndarray:
    """在 [lo,hi] 间反复来回的三角波(净涨跌≈0, 强震荡)。"""
    t = np.arange(n)
    tri = np.abs(((t % period) / period) * 2 - 1)   # 0..1..0
    return lo + (hi - lo) * tri


def test_oscillation_metrics_ranging_vs_trending():
    osc = oscillation_metrics(_bars(_sawtooth()))
    assert osc is not None
    assert osc["er"] < 0.15          # 来回震荡 -> 效率比低
    assert osc["rev"] > 5            # 多次穿越
    assert abs(osc["net"]) < 0.10    # 净漂移小

    trend = oscillation_metrics(_bars(np.linspace(10.0, 20.0, 180)))
    assert trend["er"] > 0.8         # 单边趋势 -> 效率比高


def test_grid_harvests_oscillation_and_cuts_drawdown():
    bars = _bars(_sawtooth())
    r = backtest(bars, start="2025-04-01", N=10, lookback=60, rebal=0, trim=False)
    assert r is not None
    assert r["trades"] > 0                       # 有网格买卖
    assert r["grid_ret"] > 0                      # 震荡里网格应盈利
    assert r["grid_dd"] >= r["hold_dd"]           # 网格回撤不深于持有(更接近0)
    assert len(r["grid_curve"]) == len(r["hold_curve"]) > 0


def test_grid_lags_hold_in_uptrend():
    bars = _bars(np.linspace(10.0, 20.0, 180))
    r = backtest(bars, start="2025-04-01", N=10, lookback=60, rebal=0, trim=False)
    assert r is not None
    assert r["hold_ret"] > r["grid_ret"]          # 单边上涨: 网格卖飞, 跑输持有


def test_deployable_box_sane():
    bars = _bars(_sawtooth())
    d = deployable_box(bars, lookback=60, N=10)
    assert d["box_lo"] < d["price"] < d["box_hi"] or d["box_lo"] <= d["box_hi"]
    assert d["step"] > 0 and d["grids"] == 10
    assert d["stop"] < d["box_lo"]                # 止损在箱体下沿之下


if __name__ == "__main__":
    test_oscillation_metrics_ranging_vs_trending()
    test_grid_harvests_oscillation_and_cuts_drawdown()
    test_grid_lags_hold_in_uptrend()
    test_deployable_box_sane()
    print("all etf_grid tests passed")
