"""中证红利全收益增强 — 结构性跑赢红利价格指数(000922.CSI)。

超额来源是恒等式,不是预测:**价格指数不含分红**;持有一篮子正在现金分红
的股票并把分红再投资,拿到的是全收益口径,天然多出一个股息率垫子。中证
红利指数族股息率 4~6%/年,2021-09~2026-09 实测:000922.CSI(价格)+1.1%/年
vs H00922.CSI(全收益)+6.2%/年 —— 垫子 ≈ 5%/年,没有一年为负。

股票池(月末快照,PIT):
  1) 候选 = 主板 A 股 ∪ 中证红利(000922.CSI)成分(用截止当月的最近一期
     官方权重快照,不引入未来成分);
  2) 过去 12 个月内**实际除息**(ex_date)过现金分红(div_proc='实施',
     cash_div_tax>0)的股票 —— 垫子的物质基础。`ann_date` 只作 PIT 可见性
     键(公告可见但除息未发生不算);
  3) 剔 ST/*ST:用 name_history(曾用名历史)还原**该时点**的名字。用今天的
     名字回判历史是前视(见 docs/2026-08-24-sse-enhance.md);
  4) 剔上市 <365 天的新股(对齐指数纳入规则);
  5) **面值退市高危 fail-loud 过滤**:月末收盘价 <1.5 元 或 总市值 <5 亿元。
     高股息筛选天然会捞到价值陷阱/退市风险票(2024 国九条后 1 元面值退市是
     硬规则),这是与 sse_enhance 唯一的本质区别。

权重:池内**总市值加权**,不做质量/低波/股息率打分,不做择时。sse_enhance
的证伪实验(11 年指数 + 5 年个股)已经说明:任何预测性 tilt 都提高年超额却
压低滚动窗口胜率(30% 红利 tilt:年超额 +6.2% 但滚 1 年胜率 96%→82%)。

分红再投与红利税:
- 再投是**口径**不是交易:DataProvider 默认返回后复权(hfq)价,`close×
  adj_factor` 的日收益已等于 tushare 的 close/pre_close 口径,除息日不掉价、
  分红自动留在组合里,等价于"分红当月再投"。
- 红利税是这个价格序列唯一表达不出来的现金流:A 股在除息日不扣税,税递延
  到**卖出时**按 FIFO 批次的持股期限补缴(≤1 月 20% / ≤1 年 10% / >1 年免,
  见 quanti/backtest/commission.py)。回测脚本给引擎接上 `dividend_lookup`,
  由引擎按 lot 记账、卖出时从现金里补扣。

实证结论(2026-09-11,全链路引擎回测 2021-10~2026-09,报告 JSON 与
docs/2026-09-11-dividend-tr-enhance.md):
- **恒等式成立**:分红再投 vs **价格**指数(000922.CSI)的垫子 +4.72%/年、
  滚 1 年 100% 为正;本策略对 000922 价格的滚 1 年胜率 62.3%~96.0%,
  换手 0.2%~1.9%/月(远低于 10% 目标),池内加权滚动股息率 2.8%~4.8%。
- **对 H00922.CSI(全收益)的验收闸未过:0/9 配置。** 任务书口径(主板 ∪
  中证红利成分 ∩ 12 个月分红实施、市值加权)对 H00922 的滚 1 年胜率最好
  73.8%(50 万/0.002)、最差 13.7%,DSR 0.020~0.114。根因是定义层面的:
  H00922 是**同一篮子**的"完美复制 + 免税"上界,而本策略池是"全主板分红股"
  (跟踪误差 9.7%~14.4%),两者不是同一个东西——要 90% 的滚动窗口跑赢上界,
  只剩预测性 tilt(已证否)与权重口径套利(见下)两条路。
- 诊断对照 `pool_mode="index_only"`(只持 000922 成分、市值加权)能过胜率闸
  (90.6%~91.6%)与 PBO 闸(0.0),但 DSR 仍只有 0.48~0.52(单配置 PSR 0.92),
  且超额可被完全归因到**权重口径**:同成分、官方(股息率)权重 +4.89%/年
  vs 市值权重 +14.28%/年,差 +8.95%/年。这是"大市值红利"的风格暴露,不是
  分红恒等式(2021-2026 恰好顺风),故默认不出货。
- 红利税实测:100 万档 5 年收税前分红 15.8 万元、补缴 358 元(>98% 落在
  >1 年免税档)——月度调仓下税不是主要矛盾,但引擎按真实三档计,首月除息/
  熔断清仓的路径不再高估。
- 整手约束的方向:**小资金收益更高**(20 万 +8.29% vs 100 万 +7.81%,回撤
  -7.5% vs -14.0%),因为 20 万级只买得起市值最大的少数分红股——资金档改变
  的是**持仓子集**,不是精度。

参数(无可调优项:复制规则非优化产物,`param_space = {}`):
- pool_mode:默认 ``"union"`` = 任务书口径(主板 ∪ 中证红利成分)。``"index_only"``
  是**诊断对照**不是出货配置:把池子缩到 000922 成分自身,用来分离"篮子选得
  不一样"和"分红再投机制本身"对超额的贡献(见 docs 研究记录)。
- min_weight:长尾截断阈值(小资金整手约束下买不起的尾巴显式截断后重归一)。
  回测脚本按资金档做敏感性;这不是择时/选股参数。
- min_close / min_total_mv / min_list_days / div_lookback_days:硬规则清洗。

用法约束(与 sse_enhance 相同):
- signal.strength = 目标组合权重;策略自带 ``preferred_sizer =
  FixedSizer(max_pct=1.0)``,BacktestEngine.run() 未显式传 sizer 时自动采用。
- ``selectable = False``:不进 selector 自动池(月频建仓 + 数百只持仓与逐股
  技术策略的选拔/执行假设不兼容),仅显式钉选或脚本使用。
- 个股级止损/移动止盈会破坏复制(卖掉成分不回补),回测请 risk_manager=None;
  分散本身即风控,退市风险由面值/市值硬规则在月末 fail-loud 剔除。
"""

from __future__ import annotations

import bisect
import sqlite3
from datetime import date, timedelta

from quanti.models import BarData, Direction, Signal
from quanti.risk.sizer import FixedSizer
from quanti.strategy.base import BaseStrategy

# 名称以这些前缀开头的 = ST/*ST(退市风险警示)。name_history 的 PIT 视图中
# 未出现的股票 = 期间没改过名,按非 ST 处理(fail-open,已用 2015 年起的
# 全市场曾用名史覆盖本策略回测窗)。
_ST_PREFIXES = ("ST", "*ST", "SST", "S*ST")


def is_st_name(name: str) -> bool:
    """名字是否带 ST/*ST 退市风险警示(去空格后判前缀)。"""
    return str(name or "").strip().upper().startswith(_ST_PREFIXES)


class DividendTREnhanceStrategy(BaseStrategy):
    """月末按总市值权重复制「分红实施中的主板 ∪ 中证红利成分」,吃股息垫。"""

    name = "dividend_tr_enhance"
    name_zh = "中证红利全收益增强"
    description = ("持有过去 12 个月实施过现金分红的主板/中证红利成分(市值加权,"
                   "剔 ST/新股/面值退市高危),分红再投吃股息垫,结构性跑赢红利价格指数")
    param_space: dict[str, list] = {}  # 复制规则非优化产物——无可调参
    selectable = False  # selector 自动选拔跳过;仅显式钉选/回测脚本使用

    def init(self, config: dict) -> None:
        # strength 承载组合目标权重;引擎 run() 认 preferred_sizer 直通
        self.preferred_sizer = FixedSizer(max_pct=1.0)
        self.market_db_path = config.get("market_db_path", "data/market.db")
        self.index_code = str(config.get("index_code", "000922.CSI"))
        self.pool_mode = str(config.get("pool_mode", "union"))
        if self.pool_mode not in ("union", "index_only"):
            raise ValueError(f"pool_mode must be union|index_only, got {self.pool_mode!r}")
        self.min_list_days = int(config.get("min_list_days", 365))
        self.div_lookback_days = int(config.get("div_lookback_days", 365))
        # 面值退市高危硬规则(2024 国九条):1.5 元 / 5 亿元
        self.min_close = float(config.get("min_close", 1.5))
        self.min_total_mv = float(config.get("min_total_mv", 50_000.0))  # 万元
        # 长尾截断(小资金整手买不进的名字,持仓前显式截断后重归一)
        self.min_weight = float(config.get("min_weight", 0.0))
        # 可选:把月末快照扫描限制在回测窗内(研究脚本传;不传则全表)
        self.snapshot_start = str(config.get("snapshot_start", "") or "")
        self.snapshot_end = str(config.get("snapshot_end", "") or "")
        # 月末快照缓存:month("YYYY-MM") → {code: (total_mv, raw_close)}
        self._snap_by_month: dict[str, dict[str, tuple[float, float]]] | None = None
        self._list_dates: dict[str, date] = {}
        self._index_snaps: list[tuple[date, set[str]]] = []
        # 分红实施事件(按 ex_date 升序的 ISO 串 + ann_date + code),PIT 查询用二分
        self._div_ex_dates: list[str] = []
        self._div_rows: list[tuple[str, str, float]] = []  # (ann_date, code, 每股税前)
        self._st_events: list[tuple[str, str]] = []  # (生效日, "+code"/"-code")
        self._holdings: dict[str, float] = {}  # 上次下发的目标权重
        self.pool_stats: dict[str, dict] = {}  # 月末诊断:池子大小/加权股息率
        self._cur_month: str | None = None
        self._last_seen: date | None = None

    # ---------------------------------------------------------- data
    def _ensure_loaded(self) -> None:
        """惰性加载参考数据(首次 on_bar / 首次直接查询时各一次)。"""
        if self._snap_by_month is None:
            self._load_reference()

    def _load_reference(self) -> None:
        """一次性读入参考数据(只读,PIT:仅用 as-of 之前的数据)。

        无幸存者偏差:月末快照取自 daily_basic 全表(含已退市股票当时的行),
        daily_quotes 同理(定稿时沪深两市退市股在库,见 sse_enhance 研究记录)。
        """
        con = sqlite3.connect(f"file:{self.market_db_path}?mode=ro", uri=True)
        try:
            # 每月最后一个有 daily_basic 的交易日:总市值(万元)+ 当日原始收盘价
            rows = con.execute(
                """
                with me as (
                  select substr(date,1,7) ym, max(date) d
                  from daily_basic
                  where date >= ? and date <= ?
                  group by ym
                )
                select substr(b.date,1,7), b.code, b.total_mv,
                       coalesce(q.close, 0)
                from daily_basic b
                join me on b.date = me.d
                left join daily_quotes q
                  on q.code = b.code and q.date = b.date
                where b.total_mv > 0
                """,
                (self.snapshot_start or "0000-01-01",
                 self.snapshot_end or "9999-12-31")).fetchall()
            self._snap_by_month = {}
            for ym, code, mv, close in rows:
                self._snap_by_month.setdefault(ym, {})[code] = (
                    float(mv), float(close or 0.0))
            for code, ld in con.execute(
                    "select code, list_date from stocks where list_date != ''"):
                try:
                    self._list_dates[code] = date.fromisoformat(ld)
                except ValueError:
                    continue
            # 指数成分:截止每月末的最近一期官方权重快照
            idx_rows = con.execute(
                "select trade_date, code from index_weights"
                " where index_code = ? order by trade_date",
                (self.index_code,)).fetchall()
            snaps: dict[str, set[str]] = {}
            for td, code in idx_rows:
                snaps.setdefault(td, set()).add(code)
            self._index_snaps = [
                (date.fromisoformat(td), codes)
                for td, codes in sorted(snaps.items())]
            # 分红实施记录(除息日为主键;公告日是 PIT 可见性键)。
            # 查询侧再按 ann_date <= asof 判可见性——公告晚于除息日的数据
            # 毛刺不会漏进池子。
            self._div_ex_dates = []
            self._div_rows = []
            for ex_date, ann_date, code, cash in con.execute(
                    # 同一笔分红在预案/股东大会/实施各阶段有多行(ann_date 不同),
                    # 按 (ex_date, code, end_date) 去重取 max + 最早公告日 ——
                    # 否则诊断口径的股息率会把这笔钱数两遍。
                    "select ex_date, min(ann_date), code, max(cash_div_tax)"
                    " from dividends"
                    " where div_proc = '实施' and cash_div_tax > 0"
                    "   and ex_date is not null and ex_date != ''"
                    "   and ann_date <= ex_date"
                    " group by ex_date, code, end_date"
                    " order by ex_date"):
                self._div_ex_dates.append(str(ex_date))
                self._div_rows.append((str(ann_date), code, float(cash or 0.0)))
            # 曾用名:ST 判定按 as-of 生效的名字,不用今天的名字回判历史
            st_events: list[tuple[str, str]] = []
            for code, name, start_date, end_date in con.execute(
                    "select code, name, start_date, end_date from name_history"):
                if not is_st_name(name):
                    continue
                try:
                    st = date.fromisoformat(str(start_date)[:10])
                except ValueError:
                    continue
                st_events.append((st.isoformat(), f"+{code}"))
                if end_date:
                    try:  # 撤销 ST(名称变回去)的生效日
                        st_events.append(
                            (date.fromisoformat(str(end_date)[:10]).isoformat(),
                             f"-{code}"))
                    except ValueError:
                        pass
            self._st_events = sorted(st_events)
        finally:
            con.close()

    def _index_members(self, asof: date) -> set[str]:
        """截止 asof 的最近一期指数成分(PIT)。"""
        self._ensure_loaded()
        best: set[str] = set()
        for td, codes in self._index_snaps:
            if td <= asof:
                best = codes
            else:
                break
        return best

    def _dividend_payers(self, asof: date) -> set[str]:
        """asof 前 div_lookback_days 天内实际除息过现金分红的股票。"""
        self._ensure_loaded()
        return {code for _, code, _ in self._trailing_dividends(asof)}

    def _trailing_dividends(self, asof: date) -> list[tuple[str, str, float]]:
        """asof 之前 div_lookback_days 天内已除息且已公告的分红行。"""
        self._ensure_loaded()
        start = asof - timedelta(days=self.div_lookback_days)
        i = bisect.bisect_right(self._div_ex_dates, start.isoformat())
        j = bisect.bisect_right(self._div_ex_dates, asof.isoformat())
        end_iso = asof.isoformat()
        return [row for row in self._div_rows[i:j] if row[0] <= end_iso]

    def _st_codes(self, asof: date) -> set[str]:
        """asof 时点处于 ST/*ST 的股票(由名称变更区间还原)。"""
        self._ensure_loaded()
        active: set[str] = set()
        end_iso = asof.isoformat()
        for eff, token in self._st_events:
            if eff > end_iso:
                break
            if token[0] == "+":
                active.add(token[1:])
            else:
                active.discard(token[1:])
        return active

    # ---------------------------------------------------------- weights
    @staticmethod
    def _is_main_board(code: str) -> bool:
        """主板 A 股:沪 60x / 深 000 001 002 003(不含创业板 300/301/302、
        科创板 688/689、北交所 4xx/8xx/920)。"""
        return (code.startswith(("600", "601", "603", "605"))
                or code.startswith(("000", "001", "002", "003")))

    def _prev_month(self, ym: str) -> str:
        y, m = int(ym[:4]), int(ym[5:7])
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        return f"{y:04d}-{m:02d}"

    def _target_weights(self, ym_prev: str, asof: date) -> dict[str, float]:
        """上月末快照 → 目标权重(清洗、截断、归一)。"""
        assert self._snap_by_month is not None
        snap = self._snap_by_month.get(ym_prev, {})
        if not snap:
            return {}
        idx_members = self._index_members(asof)
        payers = self._dividend_payers(asof)
        st_codes = self._st_codes(asof)
        cutoff = asof - timedelta(days=self.min_list_days)
        pool: dict[str, float] = {}
        for code, (mv, close) in snap.items():
            if code not in payers:
                continue                       # 无现金分红实施记录
            if self.pool_mode == "index_only":
                if code not in idx_members:
                    continue                   # 诊断对照:只留指数成分
            elif not (self._is_main_board(code) or code in idx_members):
                continue                       # 不在主板 ∪ 中证红利成分内
            if code in st_codes or self._list_dates.get(code, date.max) > cutoff:
                continue                       # ST/*ST、上市 <1 年
            if close < self.min_close or close <= 0:
                continue                       # 面值退市高危
            if mv < self.min_total_mv:
                continue                       # 市值退市高危
            pool[code] = mv
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
        # 诊断(非信号):池内市值加权滚动股息率 —— 用来对账"垫子从哪来"
        dps: dict[str, float] = {}
        for _, code, cash in self._trailing_dividends(asof):
            dps[code] = dps.get(code, 0.0) + cash
        y = sum(wi * dps.get(c, 0.0) / snap[c][1]
                for c, wi in w.items() if snap[c][1] > 0)
        self.pool_stats[ym_prev] = {"n": len(w), "div_yield": round(y, 6)}
        return w

    # ---------------------------------------------------------- signals
    def on_bar(self, bar: BarData) -> list[Signal]:
        self._ensure_loaded()
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
        for code in sorted(self._holdings):
            if code not in target:
                signals.append(Signal(
                    stock_code=code, direction=Direction.SELL, strength=1.0,
                    reason="移出红利池(未分红/被剔)"))
        # 大权重优先:引擎按队列顺序扣现金,小资金下先保证主要成分买得上
        for code, w in sorted(target.items(), key=lambda kv: -kv[1]):
            if code not in self._holdings:
                signals.append(Signal(
                    stock_code=code, direction=Direction.BUY,
                    # strength = 目标组合权重(需 FixedSizer(max_pct=1.0) 直通)
                    strength=max(min(w, 1.0), 1e-6),
                    reason=f"红利建仓 w={w:.4%}"))
        # 已持有的权重漂移不调:总市值加权自漂移,市场替我们再平衡
        self._holdings = target
        return signals
