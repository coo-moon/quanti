"""FcfY(自由现金流收益率)截面 alpha 实证 —— 第一步:因子构造 + rank-IC。

外部证据(动机与对照表见 docs/2026-09-12-fcfy-alpha.md):Novy-Marx 2013 毛利/资产
FF3 alpha +0.52%/月(t=4.49)、国信 2025 港股 FCF 比率组合 23 年年化超额 +8% 且
**单向**(空端无效、long-only 可用)、广发 2025 A股自由现金流率多头年化 >10%。
原 13 因子 composite 里没有任何现金流口径因子(EP/PB/动量/低波/规模…),所以
FcfY 是「新数据接口带来的未测因子」—— 口径全部自己做:本地 market.db + tushare
`cashflow` 回填的 `cashflow_items`,自己的成本模型,闸在项目 overfit.py 上过。

因子(单位统一到 元/元):
    FCF_TTM = CFO_TTM − capex_TTM        (CFO=n_cashflow_act,
                                          capex=c_pay_acq_const_fiolta,均 YTD 累计)
    FcfY    = FCF_TTM / (total_mv × 1e4) (daily_basic.total_mv 是**万元**口径)
    CFOY    = CFO_TTM / (total_mv × 1e4) (对照:不扣 capex 的纯经营现金流收益率)

TTM 拼接(PIT,绝不前视):
  1. 同 (code, end_date) 多版本(原报告 + 更正报告)按 (ann_date, update_flag)
     取最大者 —— SQL 窗口函数里强制 `ann_date ≤ D`,不是「先全取再丢日期列」;
  2. 锚 = 该票在 D 日**可见的最新报告期**(max end_date);
  3. 年报(12-31):TTM = 本期累计;中报/一季/三季:
     TTM = 本期累计 + 上年年报累计 − 上年同期累计;上年年报或上年同期在 D 不可见
     /缺失 → 该行整体不可用(不做 4×单季粗估,也不退到更老的报告期冒充新鲜数据)。
     ⇒ 3 月调仓日最新可见报告是上年三季,拼的是上上年年报 + 上年三季 − 上年同期。

EV 口径(FCF/EV)**不做**:需要资产负债表的有息负债与货币资金,本 token 无
`balancesheet` 权限,任务书层面砍掉(文档说明原因)。

评估:调仓日步长 20 交易日(低换手结构性效应的正确尺度;5 日尺度是给动量用的),
rank-IC vs 前视 20 日 hfq 收益(close×adj_factor,与引擎同复权口径),NW t(滞后 19),
对照等权全市场。主口径不过门槛(|IC|≥0.03 且 |NW t|≥2.5 且 2022-2026 逐年符号
≥4/5 同号)就没有第二步。

market.db 一律 `file:...?mode=ro` 只读打开。

用法:
    /opt/data/quanti/.venv/bin/python scripts/fcfy_study.py \\
        --market-db /opt/data/quanti/data/market.db --json data/fcfy_study.json
    # 冒烟(跨市场均匀抽 N 票,不代表全市场):--codes 400
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import warnings
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quanti.factors.evaluation import _nw_tstat  # noqa: E402

DEFAULT_MARKET_DB = "/opt/data/quanti/data/market.db"

# 万元 → 元(daily_basic.total_mv 是万元,报表金额是元)
MV_UNIT = 1e4
# 第一步门槛
GATE_IC = 0.03
GATE_T = 2.5
GATE_YEARS = 4
GATE_YEAR_MIN, GATE_YEAR_MAX = 2022, 2026
# 锚报告期最多允许多旧(天):超期 = 停报/退市清理,剔除
MAX_STALE_DAYS = 400
# 上市不足该交易日数的次新不纳入宇宙
MIN_LIST_DAYS = 120
# 现金流口径对金融票无意义(券商/银行/保险),剔除
EXCLUDED_INDUSTRY_KEYS = ("银行", "证券", "保险", "信托", "多元金融")
# 脏数据护栏:|FCF 收益率| > 100% 只能是口径事故(负市值/一次性巨额退税/
# capex 为负…),不让它霸榜
MAX_ABS_YIELD = 1.0

# 主口径 + 全部试验变体(多重检验账本如实入账,见 run_study / trial_configs)
FACTORS = ("fcfy", "cfoy")
UNIVERSES = ("base", "minmv30", "mainboard")
STEPS = (5, 20, 60)
PRIMARY = ("fcfy", "base", 20)
START = "2022-01-01"


def connect_ro(market_db: str | Path) -> sqlite3.Connection:
    """只读 URI 打开 market.db —— 研究层不允许写库。"""
    con = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
    con.execute("pragma busy_timeout=15000")
    return con


# ------------------------------------------------------------------ PIT 报表
SQL_VISIBLE_VERSIONS = """
WITH v AS (
  SELECT code, end_date, ann_date, update_flag,
         n_cashflow_act, c_pay_acq_const_fiolta,
         ROW_NUMBER() OVER (PARTITION BY code, end_date
                            ORDER BY ann_date DESC, update_flag DESC) AS rn
  FROM cashflow_items
  WHERE ann_date IS NOT NULL AND ann_date != '' AND ann_date <= ?
)
SELECT code, end_date, ann_date, n_cashflow_act, c_pay_acq_const_fiolta
FROM v WHERE rn = 1
"""


def visible_versions(con: sqlite3.Connection, d: date | str) -> pd.DataFrame:
    """D 日**可见**的报表版本集:每 (code,end_date) 只剩 ann_date 最大(同
    ann_date 取 update_flag 大的更正报告)的一行。`ann_date ≤ D` 在 SQL 层强制。"""
    iso = d if isinstance(d, str) else pd.Timestamp(d).date().isoformat()
    return pd.read_sql_query(SQL_VISIBLE_VERSIONS, con, params=(iso,))


def ttm_from_versions(vis: pd.DataFrame, asof: date | str, *,
                      max_stale_days: int = MAX_STALE_DAYS) -> pd.DataFrame:
    """把「D 日可见的版本集」拼成每票一行的 TTM。

    返回 index=code,列 cfo_ttm / capex_ttm / fcf_ttm / anchor_end_date /
    anchor_ann_date。锚 = 可见报告里 end_date 最大的那期;锚拼不出 TTM(缺上年
    年报或上年同期)则整票该日不可用 —— **不降级、不粗估**。
    """
    cols = ["cfo_ttm", "capex_ttm", "fcf_ttm", "anchor_end_date", "anchor_ann_date"]
    if vis.empty:
        return pd.DataFrame(columns=cols)
    df = vis.copy()
    df["end_date"] = df["end_date"].astype(str)
    df["year"] = df["end_date"].str[:4].astype(int)
    df["md"] = df["end_date"].str[5:]
    df["year_m1"] = df["year"] - 1
    for c in ("n_cashflow_act", "c_pay_acq_const_fiolta"):
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # 上年年报(12-31):给 md != 12-31 的行当「+ 上年全年」的加项
    ann = (df.loc[df["md"] == "12-31",
                  ["code", "year", "n_cashflow_act", "c_pay_acq_const_fiolta"]]
           .rename(columns={"year": "year_m1",
                            "n_cashflow_act": "prev_ann_cfo",
                            "c_pay_acq_const_fiolta": "prev_ann_capex"}))
    # 上年同期(同 md、year-1):减项
    same = (df[["code", "year", "md", "n_cashflow_act", "c_pay_acq_const_fiolta"]]
            .rename(columns={"year": "year_m1",
                             "n_cashflow_act": "prev_same_cfo",
                             "c_pay_acq_const_fiolta": "prev_same_capex"}))
    df = df.merge(ann, on=["code", "year_m1"], how="left")
    df = df.merge(same, on=["code", "year_m1", "md"], how="left")

    is_annual = df["md"].values == "12-31"
    cfo = np.where(is_annual, df["n_cashflow_act"].values,
                   df["n_cashflow_act"].values + df["prev_ann_cfo"].values
                   - df["prev_same_cfo"].values)
    capex = np.where(is_annual, df["c_pay_acq_const_fiolta"].values,
                     df["c_pay_acq_const_fiolta"].values + df["prev_ann_capex"].values
                     - df["prev_same_capex"].values)
    df["cfo_ttm"] = cfo
    df["capex_ttm"] = capex
    df["fcf_ttm"] = df["cfo_ttm"] - df["capex_ttm"]

    # 锚:每票可见的最新 end_date(平手取 ann_date 最新)
    df = df.sort_values(["end_date", "ann_date"], kind="stable")
    anchor = df.groupby("code", as_index=False).tail(1).set_index("code")
    out = anchor[["cfo_ttm", "capex_ttm", "fcf_ttm", "end_date", "ann_date"]].rename(
        columns={"end_date": "anchor_end_date", "ann_date": "anchor_ann_date"})
    cutoff = (_as_iso(asof)[:10] if isinstance(asof, str)
              else pd.Timestamp(asof).date().isoformat())
    cutoff = (pd.Timestamp(cutoff) - pd.Timedelta(days=max_stale_days)).date().isoformat()
    out = out.loc[out["anchor_end_date"].astype(str) >= cutoff]
    # 锚拼不出 TTM(缺上年年报或上年同期)→ 整票该日不可用,不降级、不粗估
    out = out[np.isfinite(out["cfo_ttm"])]
    return out[cols]


def factor_frame(con: sqlite3.Connection, d: date | str, mv_row: pd.Series, *,
                 max_stale_days: int = MAX_STALE_DAYS,
                 stats: dict | None = None) -> pd.DataFrame:
    """D 日的因子横截面:index=code,列 fcfy / cfoy(无量纲收益率)+ 诊断列。

    mv_row:code → total_mv(**万元**)。无市值不可算收益率,不进表。
    """
    ttm = ttm_from_versions(visible_versions(con, d), d,
                            max_stale_days=max_stale_days)
    if stats is not None:
        stats["ttm_rows"] = stats.get("ttm_rows", 0) + len(ttm)
    if ttm.empty:
        return pd.DataFrame(columns=[*FACTORS, "anchor_end_date",
                                     "anchor_ann_date", "total_mv_yuan"])
    mv = pd.to_numeric(mv_row, errors="coerce")
    mv = mv[mv > 0]
    ttm = ttm.loc[ttm.index.intersection(mv.index)]
    if ttm.empty:
        return pd.DataFrame(columns=[*FACTORS, "anchor_end_date",
                                     "anchor_ann_date", "total_mv_yuan"])
    denom = mv.loc[ttm.index] * MV_UNIT
    out = pd.DataFrame({
        "fcfy": ttm["fcf_ttm"].values / denom.values,
        "cfoy": ttm["cfo_ttm"].values / denom.values,
        "anchor_end_date": ttm["anchor_end_date"].values,
        "anchor_ann_date": ttm["anchor_ann_date"].values,
        "total_mv_yuan": denom.values,
    }, index=ttm.index)
    out = out[np.isfinite(out["fcfy"]) | np.isfinite(out["cfoy"])]
    # 护栏只对**有值**的行生效:NaN 不能被比较判成「越界」而误杀 CFOY 样本
    bad = (out["fcfy"].abs() > MAX_ABS_YIELD) | (out["cfoy"].abs() > MAX_ABS_YIELD)
    out = out[~bad.fillna(False)]
    if stats is not None:
        stats["guard_dropped"] = stats.get("guard_dropped", 0) + int(bad.sum())
        stats["kept"] = stats.get("kept", 0) + len(out)
    return out


# ------------------------------------------------------------------ 行情面板
@dataclass
class Panel:
    """日频面板矩阵(T×N),NaN = 当日无行情/无数据。"""

    dates: list[str]
    codes: list[str]
    close: np.ndarray                      # hfq 收盘价(close × adj_factor)
    open_: np.ndarray | None = None        # hfq 开盘价(回测成交价)
    amount: np.ndarray | None = None       # 成交额(元)
    turnover: np.ndarray | None = None     # 换手率(%)
    mv: np.ndarray | None = None           # total_mv(万元)
    extra: dict = field(default_factory=dict)  # daily_basic 附加列(pe_ttm/pb/dv_ratio)
    _date_pos: dict = field(default_factory=dict)
    _code_pos: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._date_pos = {d: i for i, d in enumerate(self.dates)}
        self._code_pos = {c: i for i, c in enumerate(self.codes)}

    def i_of(self, d: date | str) -> int:
        return self._date_pos[_as_iso(d)]

    def series(self, arr: np.ndarray | None, i: int) -> pd.Series:
        """矩阵第 i 行 → Series(index=code)。存储 float32(省一半内存),
        行视图升 float64(单日截面计算要够精度)。"""
        if arr is None:
            return pd.Series(dtype=float)
        return pd.Series(np.asarray(arr[i], dtype=np.float64),
                         index=pd.Index(self.codes, name="code"))

    def age_ok_matrix(self, meta: dict[str, dict], min_days: int) -> np.ndarray:
        """(T×N) 布尔:该票在该日是否已上市满 `min_days` 个交易日。

        上市年龄必须按 **stocks.list_date** 起算,不能用「面板内的序号差」——
        面板起点(库内最早行情日)晚于多数票的上市日,用序号差会把 2022 年初
        的整段样本误判成「全是次新」而清空宇宙(实测踩过)。
        A 股日历用工作日近似(周一~周五,`np.busday_count`):比真实交易日
        多算约 8%(法定假日),即这一刀比「120 交易日」略严一点,方向保守。
        list_date 缺失时退回「库内首根 bar 起算 ≥ min_days」。
        """
        cached = self.__dict__.get("_age_ok")
        if cached is not None:
            return cached
        dts = np.asarray(self.dates, dtype="datetime64[D]")
        out = np.ones((len(self.dates), len(self.codes)), dtype=bool)
        first = self.first_quote
        for j, code in enumerate(self.codes):
            ld = ((meta.get(code) or {}).get("list_date") or "")[:10]
            if len(ld) == 10 and ld >= "1900-01-01":
                try:
                    start = np.datetime64(ld, "D")
                except TypeError:
                    start = None
                if start is not None:
                    ages = np.busday_count(start, dts)
                    col = ages >= min_days
                    col[dts < start] = False           # 上市前不可能有行情
                    out[:, j] = col
                    continue
            k = first.get(code, 0)
            out[:k + min_days, j] = False
        self.__dict__["_age_ok"] = out
        return out

    @property
    def first_quote(self) -> dict[str, int]:
        """每票第一个有行情的交易日序号(上市日代理,用于次新剔除)。"""
        cached = self.__dict__.get("_first")
        if cached is not None:
            return cached
        finite = np.isfinite(self.close)
        first = np.full(self.close.shape[1], -1, dtype=np.int64)
        for t in range(self.close.shape[0]):
            new = (first < 0) & finite[t]
            first[new] = t
        cached = {self.codes[j]: int(first[j]) for j in range(len(self.codes))
                  if first[j] >= 0}
        self.__dict__["_first"] = cached
        return cached


def _as_iso(d: date | str) -> str:
    return d if isinstance(d, str) else pd.Timestamp(d).date().isoformat()


def load_panel(con: sqlite3.Connection, start: date | str, end: date | str, *,
               need_open: bool = False, need_amount: bool = False,
               need_turnover: bool = False, need_mv: bool = True,
               extra_cols: tuple[str, ...] = (),
               codes: list[str] | None = None) -> Panel:
    """把 [start, end] 的 daily_quotes(+ daily_basic 市值)读成矩阵。

    低内存路径:游标分块 fetchmany + numpy 散点写入。容器只有 ~3GB,整表
    read_sql(6.3M 行字符串列)会打爆。
    """
    s, e = _as_iso(start), _as_iso(end)
    dts = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM daily_quotes WHERE date>=? AND date<=? "
        "ORDER BY date", (s, e))]
    if not dts:
        raise SystemExit(f"daily_quotes 在 {s}..{e} 无数据")
    if codes is None:
        codes = [r[0] for r in con.execute(
            "SELECT DISTINCT code FROM daily_quotes WHERE date>=? AND date<=? "
            "ORDER BY code", (s, e))]
    T, N = len(dts), len(codes)
    di = {d: i for i, d in enumerate(dts)}
    ci = {c: i for i, c in enumerate(codes)}
    blank = np.float32("nan")
    raw = {"close": np.full((T, N), blank, dtype=np.float32),
           "adj": np.full((T, N), blank, dtype=np.float32)}
    if need_open:
        raw["open"] = np.full((T, N), blank, dtype=np.float32)
    if need_amount:
        raw["amount"] = np.full((T, N), blank, dtype=np.float32)
    if need_turnover:
        raw["turnover"] = np.full((T, N), blank, dtype=np.float32)
    cols = ", ".join(["code", "date", *(["close", "adj_factor"]
                                        + (["open"] if need_open else [])
                                        + (["amount"] if need_amount else [])
                                        + (["turnover"] if need_turnover else []))])
    cur = con.execute(f"SELECT {cols} FROM daily_quotes WHERE date>=? AND date<=?",
                      (s, e))
    _scatter_quotes(cur, ci, di, raw)

    def hfq(key: str) -> np.ndarray:
        return raw[key] * raw["adj"]                  # float32 矩阵(省内存)

    basic_cols = ([("total_mv", "mv")] if need_mv else []) + [
        (c, c) for c in extra_cols]
    basic: dict[str, np.ndarray] = {}
    if basic_cols:
        names = ", ".join(c for c, _ in basic_cols)
        arrs = {alias: np.full((T, N), blank, dtype=np.float32)
                for _, alias in basic_cols}
        cur2 = con.execute(f"SELECT code, date, {names} FROM daily_basic "
                           "WHERE date>=? AND date<=?", (s, e))
        for rows in _chunks(cur2, 200_000):
            cs = np.fromiter((ci.get(r[0], -1) for r in rows), dtype=np.int64,
                             count=len(rows))
            ds = np.fromiter((di.get(r[1], -1) for r in rows), dtype=np.int64,
                             count=len(rows))
            good = (cs >= 0) & (ds >= 0)
            jj, ii = cs[good], ds[good]
            for k, (_, alias) in enumerate(basic_cols):
                vals = np.fromiter(((r[2 + k] if r[2 + k] is not None else np.nan)
                                    for r in rows), dtype=np.float32,
                                   count=len(rows))
                arrs[alias][ii, jj] = vals[good]
        basic = arrs
    return Panel(dates=dts, codes=list(codes), close=hfq("close"),
                 open_=hfq("open") if need_open else None,
                 amount=raw["amount"] if need_amount else None,
                 turnover=raw["turnover"] if need_turnover else None,
                 mv=basic.get("mv"),
                 extra={c: basic[c] for c in extra_cols if c in basic})


def _scatter_quotes(cur, ci: dict, di: dict, raw: dict[str, np.ndarray]) -> None:
    """daily_quotes 游标 → 各矩阵(分块,低内存)。"""
    keys = list(raw)
    for rows in _chunks(cur, 200_000):
        cs = np.fromiter((ci.get(r[0], -1) for r in rows), dtype=np.int64,
                         count=len(rows))
        ds = np.fromiter((di.get(r[1], -1) for r in rows), dtype=np.int64,
                         count=len(rows))
        good = (cs >= 0) & (ds >= 0)
        jj, ii = cs[good], ds[good]
        for k, key in enumerate(keys):
            vals = np.fromiter(((r[2 + k] if r[2 + k] is not None else np.nan)
                                for r in rows), dtype=np.float32, count=len(rows))
            raw[key][ii, jj] = vals[good]


def _chunks(cur, size: int):
    while True:
        rows = cur.fetchmany(size)
        if not rows:
            return
        yield rows


# ------------------------------------------------------------------ 宇宙清洗
def is_st_name(name: str) -> bool:
    return "ST" in (name or "").upper()


def load_static_meta(con: sqlite3.Connection) -> dict[str, dict]:
    """code → {exchange, list_date, industry}(stocks 表)。

    注意:industry 是**当前**快照(tushare 无行业历史接口权限),用它剔金融票
    是时间不变属性(银行/券商不会年年换行业),文档里明说这个口径边界。
    """
    out = {}
    for code, exch, ld, ind in con.execute(
            "SELECT code, exchange, list_date, industry FROM stocks"):
        out[code] = {"exchange": exch or "", "list_date": ld or "",
                     "industry": ind or ""}
    return out


def st_events(con: sqlite3.Connection) -> list[tuple[str, str]]:
    """(生效日, "+code"/"-code") 事件流 —— PIT 判 ST 用 `name_history`,
    绝不拿今天的名字回判历史(那是前视)。与 backfill_dividends 同一套路。"""
    ev: list[tuple[str, str]] = []
    try:
        rows = con.execute("SELECT code, name, start_date, end_date "
                           "FROM name_history").fetchall()
    except sqlite3.Error:
        return []
    for code, name, sd, ed in rows:
        if not is_st_name(name or "") or not sd:
            continue
        ev.append((str(sd)[:10], f"+{code}"))
        if ed:
            ev.append((str(ed)[:10], f"-{code}"))
    return sorted(ev)


def st_codes_at(events: list[tuple[str, str]], asof_iso: str) -> set[str]:
    """asof 时点处于 ST/*ST 的股票(由名称变更区间还原)。"""
    active: set[str] = set()
    for eff, token in events:
        if eff > asof_iso:
            break
        if token[0] == "+":
            active.add(token[1:])
        else:
            active.discard(token[1:])
    return active


def is_main_board(code: str) -> bool:
    """沪深主板:沪 60x,深 000/001/002/003(不含创业板 30x、科创板 68x、北交所)。"""
    return (code.startswith(("600", "601", "603", "605"))
            or code.startswith(("000", "001", "002", "003")))


def is_financial(industry: str) -> bool:
    return any(k in (industry or "") for k in EXCLUDED_INDUSTRY_KEYS)


def universe_at(panel: Panel, i: int, meta: dict[str, dict], st: set[str],
                universe: str = "base") -> pd.Index:
    """D 日宇宙(PIT):有行情 + 有市值 + 沪深A + 非金融 + 非ST + 上市满
    MIN_LIST_DAYS 个交易日;变体再叠加市值下限/主板限制。"""
    close = np.asarray(panel.close[i], dtype=np.float64)
    mv = (np.asarray(panel.mv[i], dtype=np.float64) if panel.mv is not None
          else np.full_like(close, np.nan))
    ok = np.isfinite(close) & (close > 0) & np.isfinite(mv) & (mv > 0)
    ages = panel.age_ok_matrix(meta, MIN_LIST_DAYS)
    ok = ok & ages[i]
    codes = panel.codes
    keep: list[str] = []
    for j in np.flatnonzero(ok):
        code = codes[j]
        m = meta.get(code)
        if m is None or m["exchange"] not in ("SH", "SZ"):
            continue                       # 剔北交所(未回填 + 流动性不可交易)
        if is_financial(m["industry"]):
            continue
        if code in st:
            continue
        if universe == "mainboard" and not is_main_board(code):
            continue
        keep.append(code)
    idx = pd.Index(keep, name="code")
    if universe == "minmv30" and len(idx) > 4 and panel.mv is not None:
        # Liu-Stambaugh-Yuan(2019 JFE)口径:A股 size 因子须剔最小 30%(壳价值)
        s = pd.Series(mv[[panel._code_pos[c] for c in idx]], index=idx)
        idx = s[s >= s.quantile(0.30)].index
    return idx


# ------------------------------------------------------------------ 评估
def rank_ic(pred: pd.Series, y: pd.Series) -> float:
    """横截面 Spearman rank-IC(与 earnings_revision_study 同口径)。"""
    a, b = pred.align(y, join="inner")
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    a, b = a.align(b, join="inner")
    if len(a) < 5:
        return float("nan")
    ra, rb = a.rank(), b.rank()
    if ra.std() == 0 or rb.std() == 0:
        return float("nan")
    return float(np.corrcoef(ra, rb)[0, 1])


def fwd_return(panel: Panel, i: int, h: int) -> pd.Series:
    """D→D+h 交易日 hfq 收益(两端都要有行情:停牌/退市者不进该日样本)。"""
    if i + h >= len(panel.dates):
        return pd.Series(dtype=float)
    p0 = np.asarray(panel.close[i], dtype=np.float64)
    p1 = np.asarray(panel.close[i + h], dtype=np.float64)
    ok = np.isfinite(p0) & np.isfinite(p1) & (p0 > 0)
    return pd.Series(p1[ok] / p0[ok] - 1.0,
                     index=pd.Index(np.asarray(panel.codes)[ok], name="code"))


def quintile_returns(panel: Panel, i: int, h: int, fac: pd.Series,
                     nq: int = 5) -> list[float]:
    """因子**升序**分 nq 组,各组等权前视收益(单向性检验:Q1 是否跑输)。"""
    y = fwd_return(panel, i, h)
    f, yy = fac.align(y, join="inner")
    m = np.isfinite(f) & np.isfinite(yy)
    f, yy = f[m], yy[m]
    if len(f) < nq * 5:
        return [float("nan")] * nq
    lab = pd.qcut(f.rank(method="first"), nq, labels=False)
    return [float(yy[lab == q].mean()) if (lab == q).any() else float("nan")
            for q in range(nq)]


# ------------------------------------------------------- 因子表(可缓存共用)
@dataclass
class Frames:
    frames: dict                    # 调仓日序号 i → 因子横截面 DataFrame
    stats: dict                     # 口径诊断(ttm_rows / kept / guard_dropped)


def build_frames(con: sqlite3.Connection, panel: Panel, every_i: list[int], *,
                 market_db: str = "", cache: str = "", log=print) -> Frames:
    """逐调仓日算 PIT 因子表;`cache` 给出路径则跨脚本复用(签名一致才复用)。

    签名含 market.db 的 mtime+size、面板日期范围与票数 ⇒ 回填进程一写库,
    旧缓存立刻失效,不会拿「半截回填」的表去算正式数字。
    """
    st: dict = {}
    try:
        # 只对**因子表的真实输入**取指纹:cashflow_items 的规模 + 最晚公告日。
        # 用整库 mtime 会让回填/同步进程把缓存天天判失效(实测发生过)。
        cur = con.execute("SELECT COUNT(*), MAX(rowid), MAX(ann_date) "
                          "FROM cashflow_items").fetchone()
        db_stamp = "cf:%s:%s:%s@%s" % (cur[0], cur[1], cur[2], market_db)
    except Exception:  # noqa: BLE001 - 取不到指纹就不写/不读缓存
        db_stamp = "stamp:" + str(os.path.getmtime(market_db)) if market_db else ""
    sig = hashlib.md5("|".join([
        db_stamp, panel.dates[0], panel.dates[-1], str(len(panel.codes)),
        str(len(every_i)), str(MAX_STALE_DAYS), str(MAX_ABS_YIELD),
        str(MIN_LIST_DAYS)]).encode()).hexdigest()
    path = Path(cache) if cache else None
    if path is not None and path.exists():
        try:
            got = pd.read_pickle(path)
            if got.get("sig") == sig:
                log("复用因子表缓存 %s(%d 个调仓日)" % (path, len(got["frames"])))
                return Frames(got["frames"], got.get("stats", {}))
            log("缓存签名不符 → 重算(库已更新?)")
        except Exception as e:  # noqa: BLE001 - 缓存坏了就重算,不影响正确性
            log("缓存读取失败(%s)→ 重算" % e)
    frames: dict[int, pd.DataFrame] = {}
    for n, i in enumerate(every_i):
        frames[i] = factor_frame(con, panel.dates[i], panel.series(panel.mv, i),
                                 stats=st)
        if (n + 1) % 40 == 0:
            log("  因子表 %d/%d @ %s" % (n + 1, len(every_i), panel.dates[i]))
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.to_pickle({"sig": sig, "frames": frames, "stats": st}, path)
        log("因子表缓存 -> %s" % path)
    return Frames(frames, st)


def panel_start(con: sqlite3.Connection, end: str) -> str:
    """面板起点:库内最早行情日(momentum_6m 需要 126 交易日前置历史)。"""
    return con.execute("SELECT MIN(date) FROM daily_quotes").fetchone()[0]


def summarize_ic(ics: list[float], dates: list[str], lag: int) -> dict:
    """全样本 IC 均值 + Newey-West t + 逐年 IC 与符号一致性(2022-2026)。"""
    pairs = [(d, x) for d, x in zip(dates, ics) if np.isfinite(x)]
    arr = np.asarray([x for _, x in pairs], dtype=float)
    if arr.size < 3:
        return {"n": int(arr.size), "ic_mean": None, "ic_t": None,
                "by_year": {}, "pos_years": 0, "sign_years": 0}
    by_year: dict[str, dict] = {}
    for y in sorted({int(d[:4]) for d, _ in pairs}):
        vals = [x for d, x in pairs if int(d[:4]) == y]
        by_year[str(y)] = {"n": len(vals), "ic_mean": round(float(np.mean(vals)), 4)}
    window = [v["ic_mean"] for k, v in by_year.items()
              if GATE_YEAR_MIN <= int(k) <= GATE_YEAR_MAX]
    sign = 1.0 if arr.mean() >= 0 else -1.0
    return {"n": int(arr.size), "ic_mean": round(float(arr.mean()), 4),
            "ic_t": round(float(_nw_tstat([x for _, x in pairs], lag)), 3),
            "by_year": by_year,
            "pos_years": int(sum(1 for v in window if v * sign > 0)),
            "sign_years": int(len(window))}


def pass_gate(s: dict) -> bool:
    """第一步门槛:|IC|≥0.03 且 |NW t|≥2.5 且 2022-2026 逐年同号 ≥4/5。"""
    if s.get("ic_mean") is None:
        return False
    return (abs(s["ic_mean"]) >= GATE_IC and s.get("ic_t") is not None
            and abs(s["ic_t"]) >= GATE_T and s.get("pos_years", 0) >= GATE_YEARS)


# ------------------------------------------ 与已证否的 12 因子 composite 正交检验
SQL_FINANCE_PIT = (
    "WITH v AS ("
    "  SELECT code, end_date, ann_date, roe, netprofit_yoy, revenue_yoy,"
    "         ROW_NUMBER() OVER (PARTITION BY code"
    "                            ORDER BY ann_date DESC, end_date DESC) AS rn"
    "  FROM financials"
    "  WHERE ann_date IS NOT NULL AND ann_date != '' AND ann_date <= ?)"
    "SELECT code, end_date, ann_date, roe, netprofit_yoy, revenue_yoy"
    " FROM v WHERE rn = 1"
)

COMPOSITE_FACTORS = ("momentum_3m", "momentum_6m", "reversal_1w", "turnover_20d",
                     "realized_vol_20d", "value_ep", "value_bp", "dividend_yield",
                     "size", "quality_roe", "growth_earnings", "growth_revenue")


def latest_financials(con: sqlite3.Connection, d: date | str) -> pd.DataFrame:
    """D 日可见的最新一期财报(ann_date ≤ D;平手取更晚报告期)——PIT。"""
    return pd.read_sql_query(SQL_FINANCE_PIT, con,
                             params=(_as_iso(d),)).set_index("code")


def _winsorize(s: pd.Series, pct: float = 0.01) -> pd.Series:
    if s.dropna().empty:
        return s
    return s.clip(lower=s.quantile(pct), upper=s.quantile(1 - pct))


def _zscore(s: pd.Series) -> pd.Series:
    v = s.dropna()
    if len(v) < 2 or v.std() == 0:
        return s * 0.0
    return (s - v.mean()) / v.std()


def _lag_ratio(c: np.ndarray, i: int, a: int, b: int) -> np.ndarray:
    """close[i-a]/close[i-b] - 1(全市场日历位移;越界/缺价给 NaN)。"""
    out = np.full(c.shape[1], np.nan)
    if min(i - a, i - b) < 0:
        return out
    p0 = np.asarray(c[i - a], dtype=np.float64)
    p1 = np.asarray(c[i - b], dtype=np.float64)
    ok = np.isfinite(p0) & np.isfinite(p1) & (p0 > 0) & (p1 > 0)
    out[ok] = p0[ok] / p1[ok] - 1.0
    return out



def _basic_col(panel: "Panel", col: str, i: int, as_yield: bool) -> np.ndarray:
    """daily_basic 某列的第 i 行;as_yield=True 时取 1/x(EP/BP,负 PE 自动排后)。"""
    arr = panel.extra.get(col)
    n = len(panel.codes)
    if arr is None:
        return np.full(n, np.nan)
    v = arr[i].astype(float)
    if not as_yield:
        return v
    with np.errstate(divide="ignore", invalid="ignore"):
        return 1.0 / np.where(v == 0, np.nan, v)


def composite_at(panel: Panel, i: int, meta: dict[str, dict],
                 fin: pd.DataFrame) -> pd.Series:
    """生产 composite 的矩阵化复现(同 12 因子、同清洗管线)。

    `quanti/factors/cross_sectional.DEFAULT_FACTORS` = 12 因子(量价/反转/波动/
    流动性 + 估值/股息/规模/质量/成长,**没有一个是现金流口径**);管线 =
    winsorize(1%) → 截面 z → 行业去均值 → 等权 NaN-masked mean。生产那套逐票
    provider 循环在 5400 票 × 60 日的尺度上跑不动,这里按定义向量化重算。

    已知口径差(文档如实记):生产在**每票自己的 bar 序列**上滚动窗口,这里用
    全市场日历位移 ⇒ 停牌票窗口内出现 NaN 就整个因子 NaN(更保守:缺数据不投票)。
    """
    c = panel.close
    n = len(panel.codes)
    idx = pd.Index(panel.codes, name="code")
    raw: dict[str, np.ndarray] = {
        "momentum_3m": _lag_ratio(c, i, 21, 63),
        "momentum_6m": _lag_ratio(c, i, 21, 126),
        "reversal_1w": -_lag_ratio(c, i, 0, 5),
    }
    if panel.turnover is not None:
        w = np.asarray(panel.turnover[max(0, i - 19):i + 1], dtype=np.float64)
        with np.errstate(invalid="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)   # 全 NaN 列必报
            mean_turn = np.where(np.all(~np.isfinite(w), axis=0), np.nan,
                                 np.nanmean(w, axis=0))
        raw["turnover_20d"] = -mean_turn
    seg = np.asarray(c[max(0, i - 20):i + 1], dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        lr = np.log(seg[1:] / seg[:-1])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        vol = (np.nanstd(lr, axis=0, ddof=1) * np.sqrt(252.0)
               if lr.shape[0] >= 2 else np.full(n, np.nan))
    raw["realized_vol_20d"] = -vol
    raw["value_ep"] = _basic_col(panel, "pe_ttm", i, True)
    raw["value_bp"] = _basic_col(panel, "pb", i, True)
    raw["dividend_yield"] = _basic_col(panel, "dv_ratio", i, False)
    mv = panel.mv[i].astype(np.float64) if panel.mv is not None \
        else np.full(n, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        raw["size"] = -np.log(np.where(mv > 0, mv, np.nan))
    src = {"quality_roe": "roe", "growth_earnings": "netprofit_yoy",
           "growth_revenue": "revenue_yoy"}
    for col, name in src.items():
        s = pd.Series(dtype=float) if (fin is None or fin.empty) else fin[name]
        raw[col] = s.reindex(idx).values.astype(float)

    df = pd.DataFrame(raw, index=idx)
    ind = pd.Series([meta.get(x, {}).get("industry", "") for x in panel.codes],
                    index=idx)
    for col in df.columns:
        s = _zscore(_winsorize(df[col].replace([np.inf, -np.inf], np.nan)))
        s = s.where(np.isfinite(s))
        for name, sub in s.groupby(ind, dropna=True):
            if name and len(sub) >= 2:
                s.loc[sub.index] = sub - sub.mean()
        df[col] = s
    w = np.ones(len(COMPOSITE_FACTORS)) / len(COMPOSITE_FACTORS)
    vals = df[list(COMPOSITE_FACTORS)].values
    mask = ~np.isnan(vals)
    num = np.nansum(np.where(mask, vals, 0.0) * w, axis=1)
    den = mask.astype(float) @ w
    with np.errstate(invalid="ignore"):
        comp = np.where(den > 0, num / np.maximum(den, 1e-12), np.nan)
    return pd.Series(comp, index=idx, name="composite")


def residualize(fac: pd.Series, ctrl: pd.Series) -> pd.Series:
    """因子对控制变量做截面 OLS(含截距)后的残差 —— 增量检验用。"""
    f, x = fac.align(ctrl, join="inner")
    m = np.isfinite(f) & np.isfinite(x)
    f, x = f[m], x[m]
    if len(f) < 20:
        return pd.Series(dtype=float)
    X = np.column_stack([np.ones(len(x)), x.values])
    beta, *_ = np.linalg.lstsq(X, f.values, rcond=None)
    return pd.Series(f.values - X @ beta, index=f.index)



# ------------------------------------------------------------------ 试验网格
def trial_configs() -> list[tuple[str, str, int]]:
    """全部试过的变体 = 多重检验账本:FcfY/CFOY × 宇宙变体 × 步长 5/20/60。"""
    return [(f, u, s) for f in FACTORS for u in UNIVERSES for s in STEPS]


def rebalance_indices(panel: Panel, start_iso: str,
                      steps: tuple[int, ...]) -> dict[int, list[int]]:
    """每种步长的调仓日序号(不同步长共用同一批因子计算结果)。"""
    first = next((i for i, d in enumerate(panel.dates) if d >= start_iso), None)
    if first is None:
        raise SystemExit(f"面板 {panel.dates[-1]} 早于 start={start_iso}")
    return {s: list(range(first, len(panel.dates) - s, s)) for s in steps}


def run_study(con: sqlite3.Connection, panel: Panel, *, start: str = START,
              steps: tuple[int, ...] = STEPS, incremental: bool = True,
              market_db: str = "", frames_cache: str = "",
              log=print) -> dict:
    """跑完整试验网格:每个 (factor, universe, step) 一条 rank-IC 序列。

    标签视野 = 该 trial 的步长(与调仓间隔对齐 ⇒ 相邻样本不重叠),NW 滞后 =
    视野 - 1 ⇒ 主口径正好是任务书规定的滞后 19。
    """
    meta = load_static_meta(con)
    ev = st_events(con)
    grids = rebalance_indices(panel, start, steps)
    every_i = sorted({i for idxs in grids.values() for i in idxs})
    log("panel %s..%s T=%d codes=%d | 调仓日 %d 个 %s"
        % (panel.dates[0], panel.dates[-1], len(panel.dates), len(panel.codes),
           len(every_i), {s: len(v) for s, v in grids.items()}))

    acc: dict[tuple, dict] = {k: {"ic": [], "dates": [], "q": [], "mkt": [],
                                  "med": [], "n": []} for k in trial_configs()}
    inc: dict[str, list] = {"ic": [], "resid": [], "comp": [], "corr": [],
                            "dates": [], "n": []}
    fr = build_frames(con, panel, every_i, market_db=market_db,
                      cache=frames_cache, log=log)
    fstats = fr.stats
    n_cov: list[float] = []
    fresh: list[float] = []
    for k, i in enumerate(every_i):
        d_iso = panel.dates[i]
        ff = fr.frames[i]
        if len(ff):
            lag = (pd.to_datetime(d_iso)
                   - pd.to_datetime(ff["anchor_ann_date"].astype(str))).dt.days
            fresh.append(float(np.median(lag)))
        if ff.empty:
            continue
        st = st_codes_at(ev, d_iso)
        raw_uni = {u: universe_at(panel, i, meta, st, u) for u in UNIVERSES}
        uni = {u: raw_uni[u].intersection(ff.index) for u in UNIVERSES}
        # 覆盖率 = 可交易宇宙里**当日算得出 FcfY** 的比例(不是宇宙/因子表之比)
        n_cov.append(len(uni["base"]) / max(len(raw_uni["base"]), 1))
        for s in steps:
            if i not in grids[s]:
                continue
            y = fwd_return(panel, i, s)
            # 对照 = 等权全市场(base 宇宙**全体**,含当日算不出 FcfY 的票)的前视收益
            yb = y.reindex(raw_uni["base"]).dropna()
            ew_ref = float(yb.mean()) if len(yb) else float("nan")
            ew_med = float(yb.median()) if len(yb) else float("nan")
            for f in FACTORS:
                for u in UNIVERSES:
                    fac = ff.loc[uni[u], f].dropna()
                    if len(fac) < 30:
                        continue
                    slot = acc[(f, u, s)]
                    slot["ic"].append(rank_ic(fac, y))
                    slot["dates"].append(d_iso)
                    slot["q"].append(quintile_returns(panel, i, s, fac))
                    slot["n"].append(len(fac))
                    slot["mkt"].append(ew_ref)
                    slot["med"].append(ew_med)
        if incremental:
            idx = uni["base"]
            f20 = ff.loc[idx, "fcfy"].dropna()
            if len(f20) >= 50:
                comp = composite_at(panel, i, meta, latest_financials(con, d_iso))
                y20 = fwd_return(panel, i, 20)
                keep = f20.index.intersection(comp.dropna().index)
                if len(keep) >= 50:
                    r = residualize(f20.loc[keep], comp.loc[keep])
                    inc["dates"].append(d_iso)
                    inc["n"].append(len(keep))
                    inc["ic"].append(rank_ic(f20.loc[keep], y20))
                    inc["comp"].append(rank_ic(comp.loc[keep], y20))
                    inc["resid"].append(rank_ic(r, y20.reindex(r.index)))
                    a, b = f20.loc[keep].rank(), comp.loc[keep].rank()
                    inc["corr"].append(float(np.corrcoef(a, b)[0, 1]))
        if (k + 1) % 25 == 0:
            log("  %d/%d 调仓日 %s" % (k + 1, len(every_i), d_iso))

    out: dict = {"panel": {"start": panel.dates[0], "end": panel.dates[-1],
                           "codes": len(panel.codes),
                           "factor_dates": len(every_i)},
                 "pit_diagnostics": {
                     "median_report_age_days": round(float(np.median(fresh)), 1)
                     if fresh else None,
                     "ttm_rows_total": fstats.get("ttm_rows", 0),
                     "kept_total": fstats.get("kept", 0),
                     "guard_dropped_total": fstats.get("guard_dropped", 0)},
                 "factor_coverage_of_tradable_universe":
                     round(float(np.mean(n_cov)), 4) if n_cov else None,
                 "trials": {}, "primary": None, "gate_pass": False,
                 "incremental": None}
    for (f, u, s), slot in acc.items():
        summ = summarize_ic(slot["ic"], slot["dates"], lag=max(0, s - 1))
        qm = (np.nanmean(np.asarray(slot["q"], dtype=float), axis=0)
              if slot["q"] else np.array([]))
        summ.update({
            "factor": f, "universe": u, "step": s, "horizon": s,
            "nw_lag": max(0, s - 1),
            "names_per_date": int(np.mean(slot["n"])) if slot["n"] else 0,
            "ic_std": round(float(np.nanstd(slot["ic"], ddof=1)), 4)
            if len(slot["ic"]) > 1 else None,
            "ic_pos_ratio": round(float(np.mean([x > 0 for x in slot["ic"]
                                                 if np.isfinite(x)])), 4)
            if slot["ic"] else None,
            # 升序五分位:Q1 = 因子最低(最贵的现金流),Q5 = 最高(最便宜)
            "quantiles_pct_fwd": [round(float(x) * 100, 4)
                                  for x in np.atleast_1d(qm)],
            "ew_market_mean_fwd_pct": round(float(np.nanmean(slot["mkt"])) * 100, 4)
            if slot["mkt"] else None,
            "ew_market_median_fwd_pct": round(float(np.nanmean(slot["med"])) * 100, 4)
            if slot["med"] else None,
            "gate_pass": pass_gate(summ),
        })
        out["trials"]["%s|%s|%d" % (f, u, s)] = summ
    key = "%s|%s|%d" % PRIMARY
    out["primary"] = out["trials"][key]
    out["gate_pass"] = out["primary"]["gate_pass"]
    if incremental and inc["ic"]:
        out["incremental"] = {
            "n_dates": len(inc["ic"]),
            "mean_names": int(np.mean(inc["n"])) if inc["n"] else 0,
            "fcfy_raw_ic": summarize_ic(inc["ic"], inc["dates"], lag=19),
            "residual_ic_vs_composite": summarize_ic(inc["resid"], inc["dates"],
                                                     lag=19),
            "composite_ic": summarize_ic(inc["comp"], inc["dates"], lag=19),
            "mean_rank_corr_with_composite": round(float(np.nanmean(inc["corr"])), 4)
            if inc["corr"] else None,
        }
    return out


def _print_summary(out: dict) -> None:
    """按 |t| 降序打印账本(主口径单独标出)。"""
    cols = ("config", "n", "ic_mean", "ic_t", "IC>0占比", "逐年IC", "过闸")
    print("\n%-22s %5s %8s %7s %8s %9s %5s" % cols)
    rows = sorted(out["trials"].values(),
                  key=lambda r: -abs(r["ic_t"] or 0) if r["ic_t"] else 1)
    for r in rows:
        name = "%s|%s|%d" % (r["factor"], r["universe"], r["step"])
        yrs = " ".join("%s%+.3f" % (y[2:], v["ic_mean"])
                       for y, v in sorted(r["by_year"].items()))
        star = "*" if (r["factor"], r["universe"], r["step"]) == PRIMARY else " "
        print("%-22s %5d %8.4f %7.2f %8s %9s %5s%s"
              % (name, r["n"], r["ic_mean"] or float("nan"),
                 r["ic_t"] or float("nan"),
                 ("%.0f%%" % (100 * r["ic_pos_ratio"]))
                 if r["ic_pos_ratio"] is not None else "-",
                 yrs[:34], "PASS" if r["gate_pass"] else "-", star))
    prim = out["primary"]
    print("\n主口径 %s|%s|%d → IC=%s NW t=%s 逐年同号 %d/%d  门槛 |IC|>=%.2f "
          "|t|>=%.1f 同号>=%d → %s"
          % (*PRIMARY, prim["ic_mean"], prim["ic_t"], prim["pos_years"],
             prim["sign_years"], GATE_IC, GATE_T, GATE_YEARS,
             "过" if out["gate_pass"] else "不过"))
    inc = out.get("incremental")
    if inc:
        print("增量检验(vs 12 因子 composite):原始 IC=%s t=%s | 残差 IC=%s t=%s | "
              "composite 自身 IC=%s t=%s | 秩相关=%s"
              % (inc["fcfy_raw_ic"]["ic_mean"], inc["fcfy_raw_ic"]["ic_t"],
                 inc["residual_ic_vs_composite"]["ic_mean"],
                 inc["residual_ic_vs_composite"]["ic_t"],
                 inc["composite_ic"]["ic_mean"], inc["composite_ic"]["ic_t"],
                 inc["mean_rank_corr_with_composite"]))
    for r in rows:
        if (r["factor"], r["universe"], r["step"]) == PRIMARY and r["quantiles_pct_fwd"]:
            print("主口径五分位前视收益%%(Q1低FcfY→Q5高FcfY): %s | 市场均值 %s"
                  % (r["quantiles_pct_fwd"], r["ew_market_mean_fwd_pct"]))
            print("等权全市场中位数前视收益%%: %s"
                  % r["ew_market_median_fwd_pct"])
            print("主口径五分位超额%%(vs 等权全市场): %s"
                  % [round(q - r["ew_market_mean_fwd_pct"], 4)
                     for q in r["quantiles_pct_fwd"]])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market-db", default=DEFAULT_MARKET_DB)
    ap.add_argument("--start", default=START)
    ap.add_argument("--end", default="", help="默认取 daily_quotes 最后一天")
    ap.add_argument("--codes", type=int, default=0,
                    help="跨市场均匀抽 N 票冒烟;0=全市场")
    ap.add_argument("--steps", default=",".join(str(s) for s in STEPS))
    ap.add_argument("--no-incremental", action="store_true",
                    help="跳过与 12 因子 composite 的正交检验(省时间)")
    ap.add_argument("--frames-cache", default="",
                    help="因子表 pickle 缓存路径(与 fcfy_backtest.py 共用)")
    ap.add_argument("--json", default=None, help="结果 JSON 落盘路径")
    args = ap.parse_args()

    steps = tuple(int(x) for x in str(args.steps).split(","))
    con = connect_ro(args.market_db)
    end = args.end or con.execute(
        "SELECT MAX(date) FROM daily_quotes").fetchone()[0]
    codes = None
    if args.codes:
        allc = [r[0] for r in con.execute(
            "SELECT DISTINCT code FROM daily_quotes WHERE date<=? ORDER BY code",
            (end,))]
        stride = max(1, len(allc) // args.codes)
        codes = allc[::stride][:args.codes]      # 跨沪/深/创/科均匀抽样
    panel = load_panel(con, panel_start(con, end), end, need_turnover=True,
                       extra_cols=("pe_ttm", "pb", "dv_ratio"), codes=codes)
    n_cf = con.execute("SELECT COUNT(DISTINCT code) FROM cashflow_items").fetchone()[0]
    n_q = con.execute("SELECT COUNT(DISTINCT code) FROM daily_quotes").fetchone()[0]
    out = run_study(con, panel, start=args.start, steps=steps,
                    incremental=not args.no_incremental,
                    market_db=str(args.market_db),
                    frames_cache=args.frames_cache)
    out["meta"] = {"market_db": str(args.market_db), "start": args.start,
                   "end": end, "codes_limit": args.codes or None,
                   "cashflow_codes": n_cf, "quote_codes": n_q,
                   "cashflow_coverage": round(n_cf / max(n_q, 1), 4),
                   "smoke": bool(args.codes) or n_cf < 0.9 * n_q,
                   "factors": list(FACTORS), "universes": list(UNIVERSES),
                   "steps": list(steps), "primary": list(PRIMARY),
                   "gate": {"abs_ic": GATE_IC, "abs_t": GATE_T,
                            "years": GATE_YEARS,
                            "window": [GATE_YEAR_MIN, GATE_YEAR_MAX]}}
    _print_summary(out)
    print("\ncashflow_items 覆盖 %d/%d 票 = %.1f%%%s"
          % (n_cf, n_q, 100 * n_cf / max(n_q, 1),
             "  ⚠ 回填未完成 → 数字是冒烟值,正式值待 /tmp/cf_done.json 后重跑"
             if out["meta"]["smoke"] else ""))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2,
                                              default=str), encoding="utf-8")
        print("saved ->", args.json)
    con.close()


if __name__ == "__main__":
    main()
