"""回购公告事件研究 —— 任务书第一步(事件族里从未测过的「公司行为信号」)。

外部证据(动机与对照表见 docs/2026-09-13-repurchase-alpha.md):Ikenberry 等 1995
美股开放市场回购公告后 4 年 +12.1% 异常收益(市场反应不完全 ⇒ 长期漂移);湖大报
社科版 2021 A股 316 事件 CAR[-1,1] = +2%(t=5.46);源达 2025 增持类比首次公告后
90 日 +3.4%(小市值 +7.0%)。但**预案≠落地**、指数口径不可搬、A 股多数事件 T+1 内
就被定价完(中金 2025),所以要用本地数据 + 本地清洗 + 本地门槛重做一遍。

数据:`repurchase_events`(scripts/backfill_repurchases.py 回填,tushare `repurchase`
按 ann_date 范围,proc ∈ {预案,股东大会通过,实施,完成,停止})。**研究层只读**。

口径(前视红线):
  * 事件参考日 D0 = **晚于** ann_date 的第一个交易日(公告盘后发布是常态 ⇒
    ann_date 当天收盘不可得);
  * CAR 从 D0 **开盘**起算:首日 = close(D0)/open(D0) − 1(hfq:open 与 close 同乘
    adj_factor ⇒ 比值不变),之后 close-to-close;
  * 基准 = 同期全市场等权日收益(daily_quotes 全票日收益均值,与 fcfy 研究同源);
    个股停牌跨多日时基准按同一段行情轴几何对齐(见 MarketContext);
  * CAR(1,t2) = Σ(D0..D0+t2−1 的超额),门槛窗口 (+1,+5)/(+1,+20)/(+1,+60);
    另把 (+1,+1)(= D0 当日反应)作为诊断口径单列,不进门槛账本;
  * 切片阈值用**公告日当时**的 total_mv(daily_basic ≤ ann_date 最近一行,万元)。

主口径 = proc='预案' × 全部事件 × CAR(+1,+20)。门槛(先过才有第二步):
均值 >0 且 NW t ≥ 2.5 且 胜率 ≥ 55% 且 2022-2026 逐年均值 ≥4/5 年为正。
主口径不过但某个**预声明**切片过 ⇒ 该切片升为主口径(账本如实记全部多重性);
全灭 ⇒ 证否文档收尾。

market.db 一律 `file:...?mode=ro` 只读打开;Panel/清洗/NW t 全部复用
scripts/fcfy_study.py(同一套骨架,换信号源)。

用法:
    /opt/data/quanti/.venv/bin/python scripts/repurchase_event_study.py \
        --market-db /opt/data/quanti/data/market.db --json data/repurchase_event_study.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import fcfy_study as fs  # noqa: E402  Panel/load_panel/清洗/NW t 骨架同源
from quanti.factors.evaluation import _nw_tstat  # noqa: E402

DEFAULT_MARKET_DB = "/opt/data/quanti/data/market.db"

# 评估窗 = 本地行情覆盖(daily_quotes 2021-09-13 起)⇒ 事件 ann_date 下界取
# 2021-10-01(保证 D0 与次新判定都有真实历史可看)
EVENT_START = "2021-10-01"
# 门槛账本用任务书规定的三个窗口;D0 当日 (+1,+1) 单独作为**诊断**口径
# (回答「市场是不是在第一个可交易时段就把公告定价完了」——中金 2025 的判断),
# 不参与门槛与选择,免得「1 日 CAR」冒充可交易信号升格成组合层主口径。
WINDOWS = (5, 20, 60)
D0_WINDOW = 1
PRIMARY_PROC = "预案"
PROCS = ("预案", "股东大会通过", "完成")
STOP_PROC = "停止"
PRIMARY_WINDOW = 20

# 第一步门槛(任务书钉死,不得自行放宽)
GATE_T = 2.5
GATE_WIN = 0.55
GATE_YEARS = 4
GATE_YEAR_MIN, GATE_YEAR_MAX = 2022, 2026

MIN_LIST_DAYS = fs.MIN_LIST_DAYS          # 120 交易日次新剔除,与 fcfy 同源
MV_UNIT = fs.MV_UNIT                      # daily_basic.total_mv 是**万元**
INTENSITY_GATE = 0.001                    # 力度 = amount/市值 ≥ 0.1%
AMOUNT_MISSING = -1.0                     # 回填层的主键哨兵 ⇒ 当缺失
MV_LOOKBACK = 10                          # 公告日停牌时最多回看几行找市值
# 预声明切片(全部进多重性账本)
SLICES = ("all", "size_small", "size_mid", "size_large",
          "intensity_hi", "intensity_lo")
SIZE_BANDS = ("size_small", "size_mid", "size_large")


# ------------------------------------------------------------------ 事件流水
SQL_PLAN = """
SELECT code, ann_date, MAX(amount) AS amount
FROM repurchase_events
WHERE proc = ? AND ann_date >= ? AND ann_date <= ?
GROUP BY code, ann_date
"""

# 完成:该票**首个**完成行(同 (code,ann_date) 多行取 amount 最大)
SQL_FIRST_DONE = """
WITH g AS (
  SELECT code, ann_date, MAX(amount) AS amount
  FROM repurchase_events
  WHERE proc = '完成' AND ann_date >= ? AND ann_date <= ?
  GROUP BY code, ann_date),
r AS (SELECT code, ann_date, amount,
           ROW_NUMBER() OVER (PARTITION BY code ORDER BY ann_date) AS rn
    FROM g)
SELECT code, ann_date, amount FROM r WHERE rn = 1
"""


def load_events(con: sqlite3.Connection, proc: str, start: str,
                end: str) -> pd.DataFrame:
    """事件池(只按已知 ann_date 的行取样 ⇒ 天然 PIT)。同 (code,ann_date) 去重。

    `amount` 单位是元;AMOUNT_MISSING 哨兵归 NaN。同票多次公告 = 独立事件
    (组合层的重叠在第二步处理)。
    """
    if proc == "完成":
        df = pd.read_sql_query(SQL_FIRST_DONE, con, params=(start, end))
    else:
        df = pd.read_sql_query(SQL_PLAN, con, params=(proc, start, end))
    if df is None or df.empty:
        return pd.DataFrame(columns=["code", "ann_date", "amount"])
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df.loc[df["amount"] <= AMOUNT_MISSING, "amount"] = np.nan
    df["ann_date"] = df["ann_date"].astype(str).str[:10]
    return df.sort_values(["code", "ann_date"], kind="stable").reset_index(
        drop=True)


# ------------------------------------------------------------------ 市场基准
@dataclass
class MarketContext:
    """全市场等权日收益(close-to-close 与 D0 的 open→close)+ 累计对数轴。

    个股停牌 ⇒ 它的「下一根 bar」可能隔了很多天。为了不把基准错配成一天,
    基准用累计对数轴做**同区间**收益:exp(cum[b] − cum[a]) − 1。
    """

    ret: np.ndarray              # [T] 等权 close-to-close(面板首日无收益 → 0)
    intraday: np.ndarray         # [T] 等权 open→close(仅 D0 首日用)
    cum: np.ndarray              # [T] Σ log(1+ret) 累计
    mean_daily_pct: float


def _ew_mean_where(mat: np.ndarray, ok: np.ndarray) -> np.ndarray:
    """按行(单日截面)求等权均值;全 NaN 的交易日给 0(而不是 NaN + 警告)。"""
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        m = np.where(ok.any(axis=1), np.nanmean(np.where(ok, mat, np.nan),
                                                axis=1), np.nan)
    return np.nan_to_num(np.asarray(m, dtype=float), nan=0.0)


def build_market(panel: fs.Panel) -> MarketContext:
    """主基准:全市场等权日收益(任务书口径,门槛与选择只认它)。"""
    return _market_from(panel, np.ones(np.shape(panel.close), dtype=bool))


def build_band_markets(panel: fs.Panel, edges: tuple[float, float]
                       ) -> dict[str, MarketContext]:
    """**同规模档内**等权收益(诊断基准,不进门槛)。

    要回答的致命问题:小市值事件组的正 CAR 里有多少只是「小盘 vs 全市场等权」
    的风格差而非回购信息。本地 2021-09~2026-09 恰恰是小微盘**跑输**全市场等权
    (全票等权日均 +0.045% vs 最小档 +0.001%),所以全市场等权基准会**低估**小盘
    事件的超额、**高估**大盘事件的超额 —— 这一层不归清就会双向误判。

    边界与 size_* 切片同一批切点(小 ≤lo / 中 (lo,hi] / 大 >hi);无市值的票
    单独一档,免得「缺数据」被塞进某档污染基准。
    """
    lo, hi = edges
    mv = (np.asarray(panel.mv, dtype=np.float64) if panel.mv is not None
          else np.full(np.shape(panel.close), np.nan))
    has = np.isfinite(mv) & (mv > 0)
    bounds = {"size_small": has & (mv <= lo),
              "size_mid": has & (mv > lo) & (mv <= hi),
              "size_large": has & (mv > hi),
              "size_unknown": ~has}
    return {k: _market_from(panel, m) for k, m in bounds.items()}


def _market_from(panel: fs.Panel, in_band: np.ndarray) -> MarketContext:
    """等权日收益(只统计 in_band 为 True 的「票-日」)。"""
    c = np.asarray(panel.close, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.full(c.shape, np.nan)
        r[1:] = c[1:] / c[:-1] - 1.0
    ret = _ew_mean_where(np.where(in_band, r, np.nan),
                         np.isfinite(r) & in_band).copy()
    ret[0] = 0.0                                    # 面板首日无 close-to-close
    if panel.open_ is not None:
        o = np.asarray(panel.open_, dtype=np.float64)
        with np.errstate(divide="ignore", invalid="ignore"):
            ri = c / o - 1.0
        intr = _ew_mean_where(np.where(in_band, ri, np.nan),
                              np.isfinite(ri) & (o > 0) & in_band)
    else:
        intr = np.zeros(c.shape[0])
    return MarketContext(ret=ret, intraday=intr,
                         cum=np.cumsum(np.log1p(ret)),
                         mean_daily_pct=round(float(ret[1:].mean()) * 100, 4))


# ------------------------------------------------------------------ 清洗判定
def is_new_listing(code: str, ann_iso: str, i_ann: int, meta: dict[str, dict],
                   panel: fs.Panel, dates_arr: np.ndarray | None = None,
                   min_days: int = MIN_LIST_DAYS) -> bool:
    """公告日时点上市不满 `min_days` 个交易日 ⇒ 次新,剔除。

    年龄**必须**按 stocks.list_date 起算:库内行情起点(2021-09-13)晚于绝大多数
    票的上市日,若只按「面板内首根 bar 的序号差」算年龄,2022 年初的所有老票都会
    被误判成次新而清空样本(fcfy 踩过,见 Panel.age_ok_matrix 注释)。
    list_date 缺失时才退回「库内首根 bar 起算」;首根 bar 就是面板起点行(无从
    判断)时不剔除。
    """
    ld = ((meta.get(code) or {}).get("list_date") or "")[:10]
    if len(ld) == 10 and ld >= "1900-01-01":
        if ld >= panel.dates[0]:
            # 面板内上市 ⇒ 把上市日落到行情轴上,用**真实交易日序号**精确计数
            if dates_arr is None:
                dates_arr = np.asarray(panel.dates)
            i_list = int(np.searchsorted(dates_arr, ld))
            return (i_ann - i_list) < min_days
        # 面板起点之前上市:busday 近似(比交易日略严,方向保守)
        return int(np.busday_count(ld, ann_iso)) < min_days
    first = panel.first_quote.get(code)
    if first is None or first == 0:
        return False                 # 无从判断:面板起点即有行情 ⇒ 按老票处理
    return (i_ann - first) < min_days


def mv_at(panel: fs.Panel, i: int, j: int,
          lookback: int = MV_LOOKBACK) -> float:
    """票 j 在面板日 i **或之前最近一行**的 total_mv(万元)——PIT 切片用。

    公告日可能停牌/不落在行情轴上,故回看至多 `lookback` 行;再找不到算缺失。
    """
    if panel.mv is None or i < 0:
        return float("nan")
    col = np.asarray(panel.mv[max(0, i - lookback):i + 1, j], dtype=np.float64)
    good = np.flatnonzero(np.isfinite(col) & (col > 0))
    return float(col[good[-1]]) if len(good) else float("nan")


# ------------------------------------------------------------------ CAR 计算
@dataclass
class EventTable:
    """一个 proc 口径下清洗后的事件集 + 每事件的逐日超额序列。"""

    frame: pd.DataFrame                       # code, ann_date, d0i, amount, mv...
    excess: list[np.ndarray] = field(default_factory=list)   # 与 frame 行对齐
    excess_matched: list[np.ndarray] = field(default_factory=list)
    funnel: dict = field(default_factory=dict)

    def window_car(self, w: int, matched: bool = False
                   ) -> tuple[np.ndarray, np.ndarray]:
        """CAR(1,w) = 前 w 个观测的超额和(不足 w 按可得 bar 截断)。

        返回 (car, n_obs),n_obs = 该事件实际用到的超额天数(右截断如实上报)。
        `matched=True` ⇒ 用同规模档基准(纯诊断,不参与门槛与选择)。
        """
        src = (self.excess_matched if (matched and self.excess_matched)
               else self.excess)
        cars, nobs = [], []
        for arr in src:
            if not len(arr):
                continue
            cars.append(float(np.sum(arr[:w])))
            nobs.append(int(min(w, len(arr))))
        return np.asarray(cars, dtype=float), np.asarray(nobs, dtype=int)


def collect_events(con: sqlite3.Connection, panel: fs.Panel,
                   mkt: MarketContext, proc: str, end_iso: str, *,
                   meta: dict[str, dict], st_events: list) -> EventTable:
    """事件池 → D0 定位 → 清洗 → 逐日超额。判定只用 ann_date 当时可得的信息。"""
    ev = load_events(con, proc, EVENT_START, end_iso)
    fun = {"raw": len(ev)}
    pos = {c: j for j, c in enumerate(panel.codes)}
    dates_arr = np.asarray(panel.dates)
    rows: list[dict] = []
    excess: list[np.ndarray] = []
    for code, ann, amount in ev.itertuples(index=False):
        d0i = _first_trading_day_after(panel, dates_arr, ann)
        if d0i is None:
            fun["no_d0"] = fun.get("no_d0", 0) + 1        # 公告后行情轴已到末端
            continue
        j = pos.get(code)
        if j is None:
            fun["no_quotes"] = fun.get("no_quotes", 0) + 1
            continue
        m = meta.get(code)
        if m is None:
            fun["no_meta"] = fun.get("no_meta", 0) + 1
            continue
        if fs.is_financial(m["industry"]):
            fun["financial"] = fun.get("financial", 0) + 1
            continue
        if code in fs.st_codes_at(st_events, ann):
            fun["st"] = fun.get("st", 0) + 1
            continue
        i_ann = _last_trading_day_on_or_before(panel, dates_arr, ann)
        if (i_ann is not None
                and is_new_listing(code, ann, i_ann, meta, panel, dates_arr)):
            fun["new_listing"] = fun.get("new_listing", 0) + 1
            continue
        ex = event_excess(panel, mkt, j, d0i, max(WINDOWS))
        if ex is None:
            fun["untradable_d0"] = fun.get("untradable_d0", 0) + 1
            continue
        mv = mv_at(panel, i_ann if i_ann is not None else d0i, j)
        amt = float(amount) if pd.notna(amount) else float("nan")
        inten = (amt / (mv * MV_UNIT)
                 if (np.isfinite(amt) and amt > 0 and np.isfinite(mv))
                 else float("nan"))
        rows.append({"code": code, "ann_date": ann, "d0i": d0i,
                     "d0": panel.dates[d0i], "amount": amt,
                     "mv_wan": mv, "intensity": inten})
        excess.append(ex)
    frame = (pd.DataFrame(rows) if rows
             else pd.DataFrame(columns=["code", "ann_date", "d0i", "d0",
                                        "amount", "mv_wan", "intensity"]))
    fun["kept"] = len(frame)
    return EventTable(frame, excess, [], fun)


def band_of(mv_wan: float, edges: tuple[float, float]) -> str:
    """事件票的规模档(与 size_* 切片同一批边界,万元口径)。"""
    lo, hi = edges
    if not np.isfinite(mv_wan):
        return "size_unknown"            # 无市值:单列一档,不污染任何规模档
    if mv_wan <= lo:
        return "size_small"
    if mv_wan > hi:
        return "size_large"
    return "size_mid"


def attach_matched(et: EventTable, panel: fs.Panel,
                   bands: dict[str, MarketContext],
                   edges: tuple[float, float]) -> None:
    """给已清洗的事件表补一版「同规模档基准」的超额(纯诊断,原地写入)。

    不重复清洗、不改变样本:同一批事件、同一批个股收益,只换基准 ⇒ 两者之差
    只能来自基准的风格暴露,正好用来归因「这是回购信息还是小盘暴露」。
    """
    if et.frame.empty or not bands:
        return
    pos = {c: j for j, c in enumerate(panel.codes)}
    want = max(WINDOWS)
    out: list[np.ndarray] = []
    for row in et.frame.itertuples(index=False):
        mc = bands.get(band_of(row.mv_wan, edges))
        ex = (event_excess(panel, mc, pos[row.code], int(row.d0i), want)
              if mc is not None else None)
        out.append(np.array([], dtype=float) if ex is None else ex)
    et.excess_matched = out


def _first_trading_day_after(panel: fs.Panel, dates_arr: np.ndarray,
                             ann_iso: str) -> int | None:
    """D0 = **严格晚于** ann_date 的第一个交易日(前视红线)。"""
    idx = int(np.searchsorted(dates_arr, ann_iso, side="right"))
    return idx if idx < len(panel.dates) else None


def _last_trading_day_on_or_before(panel: fs.Panel, dates_arr: np.ndarray,
                                   ann_iso: str) -> int | None:
    idx = int(np.searchsorted(dates_arr, ann_iso, side="right")) - 1
    return idx if idx >= 0 else None


def event_excess(panel: fs.Panel, mkt: MarketContext, j: int, d0i: int,
                 want: int) -> np.ndarray | None:
    """单事件逐日超额:D0 open→close,之后 close-to-close(停牌跨段对齐基准)。

    D0 必须有有效 open 与 close(否则不可成交 ⇒ 整个事件剔除)。停牌日无 bar ⇒
    该日不产生观测,到下一根有效 bar 一次性结算,基准取同一段日期的几何收益。
    """
    close = np.asarray(panel.close[:, j], dtype=np.float64)
    open_ = np.asarray(panel.open_[:, j], dtype=np.float64)
    if not (np.isfinite(close[d0i]) and close[d0i] > 0
            and np.isfinite(open_[d0i]) and open_[d0i] > 0):
        return None
    tail = close[d0i:d0i + want * 8]                # 停牌最多回看 8 倍跨度
    offs = np.flatnonzero(np.isfinite(tail) & (tail > 0))
    out = [float(close[d0i]) / float(open_[d0i]) - 1.0 - mkt.intraday[d0i]]
    for k in range(1, min(want, offs.size)):
        a, b = offs[k - 1], offs[k]
        bench = float(np.exp(mkt.cum[d0i + b] - mkt.cum[d0i + a]) - 1.0)
        out.append(float(tail[b]) / float(tail[a]) - 1.0 - bench)
    return np.asarray(out, dtype=float)


# ------------------------------------------------------------------ 统计
def summarize_slice(car: np.ndarray, nobs: np.ndarray, ann_dates: np.ndarray,
                    w: int) -> dict:
    """一个切片 × 窗口:均值/中位/NW t(滞后 w−1)/胜率/逐年/两段。"""
    m = np.isfinite(car)
    v, nb, ad = car[m], nobs[m], ann_dates[m]
    n = int(v.size)
    if n < 3:
        return {"n": n, "mean_pct": None, "nw_t": None}
    yrs = np.asarray([int(str(d)[:4]) for d in ad])
    by_year = {str(y): {"n": int((yrs == y).sum()),
                        "mean_pct": round(float(v[yrs == y].mean()) * 100, 4)}
               for y in sorted(set(yrs.tolist()))}
    gate_years = [y for y in range(GATE_YEAR_MIN, GATE_YEAR_MAX + 1)
                  if str(y) in by_year]
    pos_names = [str(y) for y in gate_years if by_year[str(y)]["mean_pct"] > 0]
    early, late = v[yrs <= 2023], v[yrs >= 2024]
    full = v[nb >= w]
    return {
        "n": n, "window": w, "nw_lag": w - 1,
        "mean_pct": round(float(v.mean()) * 100, 4),
        "median_pct": round(float(np.median(v)) * 100, 4),
        "nw_t": round(float(_nw_tstat(list(v), w - 1)), 3),
        "win_rate": round(float((v > 0).mean()), 4),
        "n_full_window": int(full.size),
        "mean_full_only_pct": (round(float(full.mean()) * 100, 4)
                               if full.size else None),
        "by_year": by_year,
        "gate_years": len(gate_years), "pos_years": len(pos_names),
        "pos_year_names": pos_names,
        "h1_2021_2023": {"n": int(early.size),
                         "mean_pct": (round(float(early.mean()) * 100, 4)
                                      if early.size else None)},
        "h2_2024_2026": {"n": int(late.size),
                         "mean_pct": (round(float(late.mean()) * 100, 4)
                                      if late.size else None)},
    }


def pass_gate(s: dict) -> bool:
    """第一步门槛:均值 >0 且 NW t ≥ 2.5 且 胜率 ≥ 55% 且 ≥4/5 年为正。"""
    if not s or s.get("mean_pct") is None:
        return False
    return (s["mean_pct"] > 0 and (s.get("nw_t") or 0) >= GATE_T
            and (s.get("win_rate") or 0) >= GATE_WIN
            and s.get("pos_years", 0) >= GATE_YEARS
            and s.get("gate_years", 0) >= GATE_YEARS)


def slice_masks(et: EventTable, terciles: tuple[float, float] | None,
                proc: str) -> dict[str, np.ndarray]:
    """预声明切片掩码(规模三档 / 力度两档 / 全样本)。

    力度只对**预案**行有意义(任务书口径:预案的 amount 是拟回购金额下限);
    amount 缺失(哨兵 -1)或无市值 ⇒ 不进力度切片。
    """
    f = et.frame
    n = len(f)
    if n == 0:
        return {}
    masks = {"all": np.ones(n, dtype=bool)}
    mv = f["mv_wan"].values.astype(float)
    if terciles:
        lo, hi = terciles
        masks["size_small"] = np.isfinite(mv) & (mv <= lo)
        masks["size_mid"] = np.isfinite(mv) & (mv > lo) & (mv <= hi)
        masks["size_large"] = np.isfinite(mv) & (mv > hi)
    if proc == PRIMARY_PROC:
        inten = f["intensity"].values.astype(float)
        ok = np.isfinite(inten)
        masks["intensity_hi"] = ok & (inten >= INTENSITY_GATE)
        masks["intensity_lo"] = ok & (inten < INTENSITY_GATE)
    return masks


def size_terciles(et: EventTable) -> tuple[float, float] | None:
    """规模档切点:主口径(预案)事件集公告日 total_mv 的 1/3、2/3 分位。

    用**同一组**切点套到其他 proc 口径,免得每个口径各自分箱后不可比。
    """
    mv = et.frame["mv_wan"].values.astype(float)
    mv = mv[np.isfinite(mv)]
    if mv.size < 30:
        return None
    return (float(np.quantile(mv, 1 / 3)), float(np.quantile(mv, 2 / 3)))


# ------------------------------------------------------------------ 驱动
def trial_configs() -> list[tuple[str, str, int]]:
    """账本:全部实测变体 = proc × 预声明切片 × 3 窗口(力度仅预案)= 42 格。"""
    out = []
    for proc in PROCS:
        for sl in SLICES:
            if sl.startswith("intensity") and proc != PRIMARY_PROC:
                continue
            for w in WINDOWS:
                out.append((proc, sl, w))
    return out


def run_study(con: sqlite3.Connection, panel: fs.Panel, *, end_iso: str,
              log=print) -> dict:
    meta = fs.load_static_meta(con)
    st_ev = fs.st_events(con)
    mkt = build_market(panel)
    log("panel %s..%s T=%d codes=%d | 等权日均收益 %s%%"
        % (panel.dates[0], panel.dates[-1], len(panel.dates), len(panel.codes),
           mkt.mean_daily_pct))

    tables: dict[str, EventTable] = {}
    for proc in (*PROCS, STOP_PROC):
        et = collect_events(con, panel, mkt, proc, end_iso, meta=meta,
                            st_events=st_ev)
        tables[proc] = et
        log("  %-8s 事件 %6d 清洗后 %6d %s"
            % (proc, et.funnel.get("raw", 0), et.funnel.get("kept", 0),
               {k: v for k, v in et.funnel.items() if k not in ("raw", "kept")}))

    terc = size_terciles(tables[PRIMARY_PROC])
    bands = build_band_markets(panel, terc) if terc else {}
    for et in tables.values():
        attach_matched(et, panel, bands, terc)
    out: dict = {
        "panel": {"start": panel.dates[0], "end": panel.dates[-1],
                  "codes": len(panel.codes), "dates": len(panel.dates)},
        "event_window": [EVENT_START, end_iso],
        "benchmark": {"kind": "daily_quotes 全票等权日收益(与 fcfy 同源)",
                      "mean_daily_pct": mkt.mean_daily_pct,
                      "band_mean_daily_pct": {k: v.mean_daily_pct
                                              for k, v in bands.items()}},
        "size_tercile_mv_wan": [round(x, 1) for x in terc] if terc else None,
        "cleaning_funnel": {p: tables[p].funnel for p in tables},
        "trials": {}, "primary": None, "gate_pass": False,
        "passing_variants": [],
    }
    for proc, sl, w in trial_configs():
        et = tables[proc]
        if et.frame.empty:
            continue
        masks = slice_masks(et, terc, proc)
        if sl not in masks:
            continue
        car, nobs = et.window_car(w)
        mk = masks[sl]
        s = summarize_slice(car[mk], nobs[mk],
                            et.frame["ann_date"].values[mk], w)
        s.update({"proc": proc, "slice": sl, "window": w,
                  "gate_pass": pass_gate(s)})
        out["trials"]["%s|%s|%d" % (proc, sl, w)] = s
        if s["gate_pass"]:
            out["passing_variants"].append("%s|%s|%d" % (proc, sl, w))
        # 诊断(不参与门槛/选择):同一批事件换成**同规模档**基准后的 CAR
        if et.excess_matched:
            car2, nobs2 = et.window_car(w, matched=True)
            s2 = summarize_slice(car2[mk], nobs2[mk],
                                 et.frame["ann_date"].values[mk], w)
            out.setdefault("matched_benchmark_diagnostic", {})[
                "%s|%s|%d" % (proc, sl, w)] = {
                    "n": s2.get("n"), "mean_pct": s2.get("mean_pct"),
                    "nw_t": s2.get("nw_t"), "win_rate": s2.get("win_rate")}

    # D0 当日(开盘起算)超额:回答「市场是不是在第一个可交易时段就定价完了」。
    # 诊断口径,不参与门槛与选择(主口径的窗口是 (+1,+20))。
    out["d0_only_diagnostic"] = {}
    for proc in PROCS:
        et = tables[proc]
        if et.frame.empty:
            continue
        car1, nobs1 = et.window_car(D0_WINDOW)
        out["d0_only_diagnostic"][proc] = summarize_slice(
            car1, nobs1, et.frame["ann_date"].values, 1)

    key = "%s|all|%d" % (PRIMARY_PROC, PRIMARY_WINDOW)
    out["primary"] = out["trials"].get(key)
    out["gate_pass"] = bool(out["primary"] and out["primary"]["gate_pass"])
    out["n_trials"] = len(out["trials"])
    # 停止回购:负面尾部(任务书问题 c)——只做刻画,不进主口径账本
    stp = tables[STOP_PROC]
    if not stp.frame.empty:
        out["stop_events"] = {"n_events": len(stp.frame),
                              "codes": int(stp.frame["code"].nunique()),
                              "windows": {}}
        for w in WINDOWS:
            car, nobs = stp.window_car(w)
            s = summarize_slice(car, nobs, stp.frame["ann_date"].values, w)
            s["gate_pass"] = None
            out["stop_events"]["windows"][str(w)] = s
    return out


def _print_summary(out: dict) -> None:
    print("\n%-26s %6s %9s %7s %7s %8s %6s %5s"
          % ("变体", "n", "CAR均值%", "NW t", "胜率", "满窗n", "逐年+", "过闸"))
    rows = sorted(out["trials"].values(),
                  key=lambda r: -(r["nw_t"] or 0) if r.get("nw_t") else 1)
    for r in rows:
        name = "%s|%s|%d" % (r["proc"], r["slice"], r["window"])
        star = ("★" if (r["proc"], r["slice"], r["window"])
                == (PRIMARY_PROC, "all", PRIMARY_WINDOW) else " ")
        print("%-26s %6d %9s %7s %7s %8s %4d/%-2d %5s %s"
              % (name, r["n"], r.get("mean_pct"), r.get("nw_t"),
                 ("%.1f%%" % (100 * r["win_rate"]))
                 if r.get("win_rate") is not None else "-",
                 r.get("n_full_window", r["n"]),
                 r.get("pos_years", 0), r.get("gate_years", 0),
                 "PASS" if r["gate_pass"] else "-", star))
        if r.get("by_year"):
            print("      逐年%%: " + "  ".join(
                "%s %+.2f(n=%d)" % (y, v["mean_pct"], v["n"])
                for y, v in sorted(r["by_year"].items())))
            print("      两段%%: 2021-23 %s (n=%d) | 2024-26 %s (n=%d)"
                  % (r["h1_2021_2023"]["mean_pct"], r["h1_2021_2023"]["n"],
                     r["h2_2024_2026"]["mean_pct"], r["h2_2024_2026"]["n"]))
    print("\n主口径 %s|all|%d → %s"
          % (PRIMARY_PROC, PRIMARY_WINDOW, out.get("primary")))
    print("门槛:CAR均值>0 且 NW t≥%.1f 且 胜率≥%.0f%% 且 %d-%d 逐年为正≥%d/%d"
          % (GATE_T, 100 * GATE_WIN, GATE_YEAR_MIN, GATE_YEAR_MAX, GATE_YEARS,
             GATE_YEAR_MAX - GATE_YEAR_MIN + 1))
    print("第一步判定:%s | 过闸变体 %d 个: %s"
          % ("过" if out["gate_pass"] else "不过", len(out["passing_variants"]),
             out["passing_variants"][:12]))
    d0 = out.get("d0_only_diagnostic") or {}
    if d0:
        print("D0 当日(开盘起算)超额诊断 %%:" + "  ".join(
            "%s %s (t=%s, 胜率 %s)" % (k, v.get("mean_pct"), v.get("nw_t"),
                                       v.get("win_rate"))
            for k, v in d0.items()))
    md = out.get("matched_benchmark_diagnostic") or {}
    if md:
        print("同规模档基准诊断(不参与门槛/选择),窗口 %d:" % PRIMARY_WINDOW)
        for k, v in sorted(md.items()):
            if k.endswith("|%d" % PRIMARY_WINDOW):
                base = out["trials"].get(k, {})
                print("   %-26s 全市场基准 %8s → 同档基准 %8s (t %s→%s)"
                      % (k, base.get("mean_pct"), v["mean_pct"],
                         base.get("nw_t"), v["nw_t"]))
    se = out.get("stop_events")
    if se:
        print("停止回购(n=%d 事件 / %d 票):%s"
              % (se["n_events"], se["codes"],
                 {w: (v.get("mean_pct"), v.get("nw_t"))
                  for w, v in se["windows"].items()}))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market-db", default=DEFAULT_MARKET_DB)
    ap.add_argument("--end", default="", help="默认取 daily_quotes 最后一天")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    con = fs.connect_ro(args.market_db)
    end = args.end or con.execute(
        "SELECT MAX(date) FROM daily_quotes").fetchone()[0]
    start = con.execute("SELECT MIN(date) FROM daily_quotes").fetchone()[0]
    panel = fs.load_panel(con, start, end, need_open=True)
    n_rp = con.execute("SELECT COUNT(*) FROM repurchase_events").fetchone()[0]
    out = run_study(con, panel, end_iso=str(end)[:10])
    out["meta"] = {"market_db": str(args.market_db), "panel_start": start,
                   "panel_end": end, "repurchase_rows": n_rp,
                   "procs": list(PROCS), "windows": list(WINDOWS),
                   "slices": list(SLICES),
                   "primary": [PRIMARY_PROC, "all", PRIMARY_WINDOW],
                   "gate": {"mean_gt": 0, "nw_t": GATE_T,
                            "win_rate": GATE_WIN, "pos_years": GATE_YEARS,
                            "years": [GATE_YEAR_MIN, GATE_YEAR_MAX]}}
    _print_summary(out)
    if args.json:
        p = Path(args.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=2,
                                default=str), encoding="utf-8")
        print("saved ->", args.json)
    con.close()


if __name__ == "__main__":
    main()
