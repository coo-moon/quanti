"""ADX 的唯一覆盖(deterministic, offline)。

这个文件原是 quanti/agent/regime.py(regime detection v1,等权合成指数 +
Kaufman 效率比 + 波动分位)的测试。该模块已删除 —— 它在生产里跑了 45 天、
65 条决策日志**全部**是 high_vol(波动分位一票否决恒成立),零辨别力;而且它
的「市场」是 universe 前 120 个代码里能取到数据的 98 只,与全市场宽度口径
相差 25 个百分点。tick 现在读 quanti/regime 的全市场快照(见
tests/test_regime_snapshot.py)。

`compute_adx` 与它无关,只是当年顺手放在这里,而这是全仓唯一覆盖 —— 所以
留下,文件改名副其实。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from quanti.factors.technical import compute_adx


def _ohlc(closes: np.ndarray, band: float = 0.005) -> pd.DataFrame:
    return pd.DataFrame({"high": closes * (1 + band),
                         "low": closes * (1 - band),
                         "close": closes})


class TestADX:
    def test_trend_higher_than_chop(self):
        trend = compute_adx(_ohlc(np.linspace(10, 25, 150)))
        chop = compute_adx(_ohlc(10 + np.sin(np.arange(150) * 0.6)))
        assert trend.iloc[-1] > chop.iloc[-1]
        assert trend.iloc[-1] > 40       # clean trend → very high ADX
        assert chop.iloc[-1] < 40
