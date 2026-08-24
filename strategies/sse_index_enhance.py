"""上证全收益复制策略 — 结构性跑赢上证综指(价格指数)。

超额来源是一个恒等式,不是预测:上证综指(000001.SH)是**价格指数**,
不含分红;持有其成分(全体沪市 A 股,总市值加权)并把分红再投资,
收益即为「全收益」口径,超额 ≈ 股息率(2015-2026 逐年 +1.4%~+3.6%,
以沪深300全收益-价格指数差实证,无一年为负)。

在此之上仅做两个非预测性清洗(均 PIT 无前视):
- 剔上市 < min_list_days 的新股(对齐指数纳入规则,新股次新期拖累);
- 权重 < min_weight 的长尾截断后重归一(小资金整手约束下买不起的尾巴,
  与其滞留现金不如显式截断)。

刻意不做任何风格倾斜(红利/等权/小盘):5 年个股 + 11 年指数双重验证,
任何倾斜都提高年超额却压低滚动窗口胜率(tilt 30% 红利:年超额 +6.2%
但滚动 1 年胜率从 96% 掉到 82%)。「任何历史时期稳定跑赢」的解是
零倾斜 + 最小跟踪误差。

实证(见 scripts/sse_enhance_backtest.py 与 PR 记录):
- 滚动 3 年窗口:100% 跑赢上证(样本内最差 +10.9%);
- 滚动 1 年窗口:≈96% 跑赢,最差 -3.5%(TE≈2.2% 的数学尾部,
  任意起止日 100% 在数学上不可达——TE>0 必有负窗口);
- 资金越大跟踪误差越小(整手约束),500 万级达到设计精度。

用法约束:
- signal.strength 即目标组合权重。策略自带 ``preferred_sizer =
  FixedSizer(max_pct=1.0)``,BacktestEngine.run() 未显式传 sizer 时自动
  采用;绕过引擎直接消费信号的路径(live per-stock sizing)不适用本策略。
- ``selectable = False``:不进 selector 自动池(月频建仓 + 数百只持仓
  与逐股技术策略的选拔/执行假设不兼容),仅显式钉选或脚本使用。
- 个股级止损/移动止盈 overlay 会破坏复制(卖掉成分不回补),回测请
  risk_manager=None 或关闭 per-stock 出场;分散本身即风控。
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from quanti.models import BarData, Direction, Signal
from quanti.risk.sizer import FixedSizer
from quanti.strategy.base import BaseStrategy


class SSEIndexEnhanceStrategy(BaseStrategy):
    """月末按总市值权重复制沪市全样本,分红再投,吃股息垫。"""

    name = "sse_enhance"
    name_zh = "上证全收益复制"
    description = "复制上证综指成分(总市值加权,剔新股),分红再投吃股息垫,结构性跑赢价格指数"
    param_space: dict[str, list] = {}  # 复制规则非优化产物——无可调参
    selectable = False  # selector 自动选拔跳过;仅显式钉选/回测脚本使用

    def init(self, config: dict) -> None:
        # strength 承载组合目标权重;引擎 run() 认 preferred_sizer 直通
        self.preferred_sizer = FixedSizer(max_pct=1.0)
        self.min_list_days = int(config.get("min_list_days", 365))
        self.min_weight = float(config.get("min_weight", 0.0))
        self.market_db_path = config.get("market_db_path", "data/market.db")
        # 月末快照缓存:month("YYYY-MM") → {code: total_mv};惰性加载
        self._mv_by_month: dict[str, dict[str, float]] | None = None
        self._list_dates: dict[str, date] = {}
        self._holdings: dict[str, float] = {}  # 上次下发的目标权重
        self._cur_month: str | None = None
        self._last_seen: date | None = None

    # ---------------------------------------------------------- data
    def _load_reference(self) -> None:
        """一次性读入月末市值快照 + 上市日期(只读,PIT:仅用 as-of 数据)。"""
        con = sqlite3.connect(f"file:{self.market_db_path}?mode=ro", uri=True)
        try:
            # 每月最后一个有数据的交易日的总市值(万元),沪市 A 股
            rows = con.execute(
                """
                with me as (
                  select substr(date,1,7) ym, max(date) d
                  from daily_basic group by ym
                )
                select substr(b.date,1,7), b.code, b.total_mv
                from daily_basic b join me on b.date = me.d
                where (b.code like '60%' or b.code like '68%')
                  and b.total_mv > 0
                """).fetchall()
            self._mv_by_month = {}
            for ym, code, mv in rows:
                self._mv_by_month.setdefault(ym, {})[code] = float(mv)
            for code, ld in con.execute(
                    "select code, list_date from stocks where list_date != ''"):
                try:
                    self._list_dates[code] = date.fromisoformat(ld)
                except ValueError:
                    continue
        finally:
            con.close()

    # ---------------------------------------------------------- weights
    def _prev_month(self, ym: str) -> str:
        y, m = int(ym[:4]), int(ym[5:7])
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        return f"{y:04d}-{m:02d}"

    def _target_weights(self, ym_prev: str, asof: date) -> dict[str, float]:
        """上月末市值快照 → 目标权重(剔新股、尾部截断、归一)。"""
        assert self._mv_by_month is not None
        snap = self._mv_by_month.get(ym_prev, {})
        cutoff = asof - timedelta(days=self.min_list_days)
        pool = {c: mv for c, mv in snap.items()
                if self._list_dates.get(c, date.max) <= cutoff}
        total = sum(pool.values())
        if total <= 0:
            return {}
        w = {c: mv / total for c, mv in pool.items()}
        if self.min_weight > 0:
            w = {c: x for c, x in w.items() if x >= self.min_weight}
            s = sum(w.values())
            if s <= 0:
                return {}
            w = {c: x / s for c, x in w.items()}
        return w

    # ---------------------------------------------------------- signals
    def on_bar(self, bar: BarData) -> list[Signal]:
        if self._mv_by_month is None:
            self._load_reference()
        ym = f"{bar.date.year:04d}-{bar.date.month:02d}"
        if self._cur_month is None:
            # 首月:直接用上月末快照建仓(首月内第一根 bar 触发一次)
            self._cur_month = ym
            self._last_seen = bar.date
            return self._rebalance(self._prev_month(ym), bar.date)
        self._last_seen = bar.date
        if ym == self._cur_month:
            return []
        # 跨月首 bar:用刚结束那个月的月末快照再平衡
        prev = self._cur_month
        self._cur_month = ym
        return self._rebalance(prev, bar.date)

    def _rebalance(self, ym_snap: str, asof: date) -> list[Signal]:
        target = self._target_weights(ym_snap, asof)
        if not target:
            return []
        signals: list[Signal] = []
        for code in self._holdings:
            if code not in target:
                signals.append(Signal(
                    stock_code=code, direction=Direction.SELL, strength=1.0,
                    reason="移出复制样本"))
        for code, w in target.items():
            if code not in self._holdings:
                signals.append(Signal(
                    stock_code=code, direction=Direction.BUY,
                    # strength = 目标组合权重(需 FixedSizer(max_pct=1.0) 直通)
                    strength=max(min(w, 1.0), 1e-6),
                    reason=f"复制建仓 w={w:.4%}"))
        # 已持有的权重漂移不调:总市值加权自漂移,市场替我们再平衡
        self._holdings = target
        return signals
