"""打新增强估计器 — 无策略依赖:账户市值档位 → 逐年期望增强(不预测,只用制度价差)。

要回答的问题:沪市市值 10/30/50/100/500 万的账户,把「打新」叠加在长仓
(如 sse_enhance 的沪市复制组合)上,每年期望加多少?——收益来源不是预测
能力,而是发行定价的制度性限价(23 倍市盈率窗口指导等)与二级市场首日
溢价之间的价差,所以可以用**期望值**直接算,不需要 Monte Carlo。

口径(全部显式,便于对抗式复核):

1. 可申购额度 = min(档位市值 ÷ 市值单位 × 单位股数, 网上发行量 × 1‰,
   接口返回的申购上限),再向下取整到申购单位:

   ==================  ============  ==========  ==========================
   板块                市值单位      单位股数    备注
   ==================  ============  ==========  ==========================
   沪市主板(60x)      1 万元        1000 股     —
   科创板(688/689)    5000 元       500 股      需开通权限(50 万资产,本
                                                 脚本以档位 ≥50 万代理)
   深市主板(00x)      5000 元       500 股      本策略只有沪市市值 → 深市
                                                 额度为 0;`--sz-mv-ratio`
                                                 给出「同等深市市值」敏感性
   创业板(30x)        5000 元       500 股      需开通权限(10 万资产)
   北交所(4/8/92x)    —             —           **直接排除**:打新需全额
                                                 资金预缴,与市值配售不是
                                                 同一套规则,不吃市值
   ==================  ============  ==========  ==========================

2. 期望中签股数 = 申购股数 × ballot(tushare `new_share` 返回的真实中签率,%)。
3. 每股收益 = 卖出价 − 发行价;卖出价默认**首日开盘价**(上市首日签购结果
   已缴款,首日即可卖,不必等 T+4)。一字板(open==high==low==close)或
   注册制前主板 44% 顶格收盘 → 按**次日开盘价**计(首日卖不出)。破发按
   实际亏损计,**不截断**:2021-09~2022 的注册制破发潮必须在样本里。
4. 年化增强 = Σ期望利润 ÷ 档位市值,逐年报告;收益免税(个人转让上市公司
   股票免征个人所得税),成本只算卖出侧(中签缴款无佣金)。

排除规则(写明并计数):北交所、ST/退市整理(新股上市时点几乎不会出现,
仍显式过滤)、缺发行价/中签率/首日行情的样本。

数据来源:

- tushare `new_share`(start_date=20180101 分页拉全,单次 limit ≤300 行);
  限速按低积分档 50 次/分(相邻调用 ≥1.2s),分钟级限流等一个窗口重试。
  结果落 `--new-share-cache`(默认 data/new_share_raw.csv,**不提交**)。
- 首日行情优先取本地 market.db(2021-09-13 起全市场覆盖;更早的新股在本地
  库里没有上市首日那根 bar)。缺口可用 `--fetch-missing-first-day` 经
  tushare `daily`(按上市日整市场一次调用,同样限速)补齐并落
  `--first-day-cache`;不补齐则如实标注样本覆盖,不猜、不外推。

用法:
    /opt/data/quanti/.venv/bin/python scripts/ipo_yield_study.py \
        --market-db /opt/data/quanti/data/market.db \
        --config-db /opt/data/quanti/data/paper.db \
        --tiers 100000,300000,500000,1000000,5000000 \
        --out data/ipo_yield_study.json

报告 JSON 落 data/(不提交)。测试经注入的 fake pro 走完全程,绝不触网
(见 tests/test_ipo_yield_study.py)。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_MARKET_DB = "/opt/data/quanti/data/market.db"
DEFAULT_CONFIG_DB = "/opt/data/quanti/data/paper.db"
DEFAULT_TIERS = (100_000, 300_000, 500_000, 1_000_000, 5_000_000)
GATE_TIERS = (500_000, 1_000_000)
NEW_SHARE_START = "20180101"

# --- 限速(低积分档:tushare 每分钟调用次数) ---
PAGE_SIZE = 300            # new_share 单次上限 300 行
RATE_LIMIT_PER_MIN = 50    # daily / new_share 低积分档 50 次/分
CALL_PAUSE = 60.0 / RATE_LIMIT_PER_MIN
RATE_LIMIT_WAIT = 61.0     # 分钟级限流:等一个窗口再重试
MAX_RETRIES = 3

# --- 制度口径 ---
BOARD_UNITS = {
    "SH_MAIN": (10_000.0, 1000),   # 沪市主板:每 1 万元市值 1000 股
    "SH_STAR": (5_000.0, 500),     # 科创板:每 5000 元市值 500 股(需权限)
    "SZ_MAIN": (5_000.0, 500),     # 深市主板:每 5000 元市值 500 股
    "SZ_GEM": (5_000.0, 500),      # 创业板:每 5000 元市值 500 股(需权限)
}
SH_BOARDS = ("SH_MAIN", "SH_STAR")
STAR_MIN_TIER = 500_000.0          # 科创板权限:开通前 20 日日均资产 ≥50 万
GEM_MIN_TIER = 100_000.0           # 创业板权限:10 万
QUOTA_CAP_FRAC = 1 / 1000          # 额度上限 ≈ 网上初始发行量的千分之一
MAIN_BOARD_44_END = date(2023, 2, 17)   # 全面注册制后主板新股前 5 日不设涨跌幅
GEM_44_END = date(2020, 8, 24)          # 创业板注册制后首日不设涨跌幅
LIMIT_UP_44 = 1.44                      # 注册制前主板/创业板首日涨幅上限

# --- 成本:中签缴款无佣金,卖出侧只有佣金 + 印花税 + 过户费 ---
COMMISSION_RATE = 0.00025          # 万 2.5
STAMP_HALVED_FROM = date(2023, 8, 28)
STAMP_RATE_PRE = 0.001             # 千 1(2023-08-28 前)
STAMP_RATE_NOW = 0.0005            # 万 5(2023-08-28 起减半)
TRANSFER_FEE_RATE = 0.00001        # 过户费 0.001%


# --------------------------------------------------------------------------
# 数据获取(fake pro 注入即全程不触网)
# --------------------------------------------------------------------------
@dataclass
class RateLimiter:
    """相邻调用间隔 ≥`min_interval` 秒;sleep/clock 可注入,测试不真睡。"""

    min_interval: float = CALL_PAUSE
    _sleep: object = time.sleep
    _clock: object = time.monotonic
    _last: float | None = None
    sleeps: list = field(default_factory=list)

    def wait(self) -> float:
        now = self._clock()  # type: ignore[operator]
        waited = 0.0
        if self._last is not None:
            waited = max(self.min_interval - (now - self._last), 0.0)
            if waited > 0:
                self._sleep(waited)  # type: ignore[operator]
                self.sleeps.append(waited)
        self._last = self._clock()  # type: ignore[operator]
        return waited


def _call_with_retry(fn, *args, limiter: RateLimiter | None = None,
                     retries: int = MAX_RETRIES, sleep=None, **kwargs):
    """分钟级限流等一个窗口再重试;其它异常短退避重试后抛出。"""
    sleep = sleep or time.sleep      # 运行期解析,便于测试 monkeypatch
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        if limiter is not None:
            limiter.wait()
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - 上游/限流瞬时错误
            last_err = e
            if attempt >= retries:
                break
            msg = str(e)
            # tushare 的分钟级限流文案:「抱歉，您每分钟最多访问该接口 N 次」
            sleep(RATE_LIMIT_WAIT if ("每分钟" in msg or "频率超限" in msg)
                  else 2.0 * attempt)
    assert last_err is not None
    raise last_err


def fetch_new_share(pro, start: str = NEW_SHARE_START,
                    end: str | None = None, *,
                    page_size: int = PAGE_SIZE,
                    limiter: RateLimiter | None = None,
                    max_pages: int = 200) -> pd.DataFrame:
    """tushare `new_share` 按 offset/limit 分页拉全(单次 ≤300 行)。

    `pro` 由调用方注入(测试用 FakePro,不触网)。分页终止条件:返回行数
    < page_size(含 0)或触达 max_pages 保护。
    """
    end = end or date.today().strftime("%Y%m%d")
    frames: list[pd.DataFrame] = []
    offset = 0
    for _ in range(max_pages):
        df = _call_with_retry(pro.new_share, start_date=start, end_date=end,
                              offset=offset, limit=page_size, limiter=limiter)
        if df is None or len(df) == 0:
            break
        frames.append(df)
        if len(df) < page_size:
            break
        offset += len(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def resolve_token(config_db: str) -> str | None:
    """token 优先取环境变量,其次 config DB 的 app_config(从不打印)。"""
    if os.environ.get("TUSHARE_TOKEN"):
        return os.environ["TUSHARE_TOKEN"]
    try:
        con = sqlite3.connect(f"file:{config_db}?mode=ro", uri=True)
        try:
            row = con.execute(
                "select data_source_token from app_config where id = 1").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return row[0] if row and row[0] else None


def build_pro(config_db: str):
    """惰性构造 tushare pro(只有 main 里真正要拉数据时才走到这里)。"""
    token = resolve_token(config_db)
    if not token:
        raise SystemExit("TUSHARE_TOKEN 未设置,且 config DB 里没有 token")
    import tushare as ts
    return ts.pro_api(token)


def latest_cached_date(path: Path) -> str | None:
    """缓存 CSV 里最新一个 ipo_date(YYYYMMDD);非 8 位 → None。"""
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, dtype=str, usecols=["ipo_date"])
    except (ValueError, OSError):
        return None
    vals = [v for v in df.ipo_date.dropna().astype(str)
            if len(v) == 8 and v.isdigit()]
    return max(vals) if vals else None


# --------------------------------------------------------------------------
# 板块 / 额度 / 卖出规则
# --------------------------------------------------------------------------
def classify_board(ts_code: str) -> str | None:
    """ts_code → 板块;北交所(4/8/92 开头)与未知代码返回 None(排除)。"""
    code, _, suffix = str(ts_code).partition(".")
    if suffix == "BJ" or code[:2] in ("43", "83", "87", "88", "92"):
        return None
    if code.startswith(("688", "689")):
        return "SH_STAR"
    if code.startswith("60"):
        return "SH_MAIN"
    if code.startswith("00"):
        return "SZ_MAIN"
    if code.startswith("30"):
        return "SZ_GEM"
    return None


def board_permitted(board: str, *, sh_mv: float, sz_mv: float) -> bool:
    """权限与市值是否够得着这个板块(科创板 ≥50 万、创业板 ≥10 万)。"""
    if board == "SH_STAR":
        return sh_mv >= STAR_MIN_TIER
    if board == "SZ_GEM":
        return sz_mv >= GEM_MIN_TIER
    return True


def subscription_shares(board: str, market_value: float, *,
                        limit_amount_wan: float | None = None,
                        market_amount_wan: float | None = None) -> int:
    """可申购股数(向下取整到申购单位),三个上限取最小:

    市值额度(档位 ÷ 市值单位 × 单位股数)、网上发行量千分之一、接口申购上限。
    """
    unit_mv, unit_shares = BOARD_UNITS[board]
    quota = math.floor(max(market_value, 0.0) / unit_mv) * unit_shares
    caps = [quota]
    if market_amount_wan and market_amount_wan > 0:
        caps.append(market_amount_wan * 10_000 * QUOTA_CAP_FRAC)
    if limit_amount_wan and limit_amount_wan > 0:
        caps.append(limit_amount_wan * 10_000)
    return int(math.floor(min(caps) / unit_shares) * unit_shares)


def capped_first_day(board: str, issue_date: date) -> bool:
    """首日是否有 44% 涨跌幅上限(注册制前主板 / 创业板)。"""
    if board in ("SH_MAIN", "SZ_MAIN"):
        return issue_date < MAIN_BOARD_44_END
    if board == "SZ_GEM":
        return issue_date < GEM_44_END
    return False


def _nearly(a: float, b: float, *, rel: float = 1e-9) -> bool:
    return abs(a - b) <= max(abs(a), abs(b)) * rel + 1e-12


def choose_sell_price(bars: list[dict], issue_price: float, board: str,
                      issue_date: date, *, sell_rule: str = "next-open",
                      ) -> tuple[float | None, str]:
    """首日开盘卖出;一字板 / 44% 顶格 → 次日开盘(卖不出)。

    返回 (卖出价, 规则标签);次日无行情 → (None, "unsellable")。
    `sell_rule="first-open"` 关闭一字板回退(敏感性对照,见研究文档)。
    """
    if not bars:
        return None, "no_quote"
    b1 = bars[0]
    one_word = (_nearly(b1["open"], b1["high"]) and _nearly(b1["high"], b1["low"])
                and _nearly(b1["low"], b1["close"]))
    at_44 = (capped_first_day(board, issue_date)
             and _nearly(b1["close"], issue_price * LIMIT_UP_44, rel=0.006))
    if sell_rule == "next-open" and (one_word or at_44):
        if len(bars) < 2:
            return None, "unsellable"
        return float(bars[1]["open"]), "next_open"
    return float(b1["open"]), "first_open"


def sell_cost_rate(sell_date: date) -> float:
    """卖出侧成本率:佣金 + 印花税(2023-08-28 减半)+ 过户费。"""
    stamp = STAMP_RATE_PRE if sell_date < STAMP_HALVED_FROM else STAMP_RATE_NOW
    return COMMISSION_RATE + stamp + TRANSFER_FEE_RATE


# --------------------------------------------------------------------------
# 首日行情:本地 market.db + 可选 tushare 回填
# --------------------------------------------------------------------------
BAR_FIELDS = ("date", "open", "high", "low", "close")


def load_local_first_days(market_db: str, listings: dict[str, str], *,
                          span_days: int = 20) -> dict[str, list[dict]]:
    """按 (code, 上市日) 从本地 daily_quotes 取上市后 `span_days` 内的 bar。

    本地库 2021-09-13 起覆盖:更早上市的新股在这里取不到上市首日那根,
    返回空(由调用方决定回填或如实标注缺口)。
    """
    if not listings:
        return {}
    out: dict[str, list[dict]] = {}
    con = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
    try:
        con.execute("create temp table _listings (code text primary key, d text)")
        # daily_quotes.date 是 ISO('YYYY-MM-DD'),new_share 的上市日是
        # 'YYYYMMDD' —— 统一成 ISO 再比,否则字符串比较会跨过所有行。
        iso = {c: (f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 and d.isdigit() else d)
               for c, d in listings.items()}
        con.executemany("insert into _listings values (?, ?)", list(iso.items()))
        rows = con.execute(
            """
            select q.code, q.date, q.open, q.high, q.low, q.close
            from daily_quotes q join _listings l on q.code = l.code
            where q.date >= l.d and q.date <= date(l.d, '+' || ? || ' day')
            order by q.code, q.date
            """, (span_days,)).fetchall()
    finally:
        con.close()
    for code, dt, o, h, low_, c in rows:
        out.setdefault(code, []).append(
            {"date": str(dt), "open": float(o), "high": float(h),
             "low": float(low_), "close": float(c)})
    return out


def roster_cross_check(market_db: str, stocks: pd.DataFrame) -> dict:
    """用本地 `stocks` 表交叉核对板块判定(代码前缀)与上市日,写进报告。

    板块判定以代码前缀为准(60/68 → 沪,00/30 → 深,4/8/92 → 北交所),
    `stocks.exchange` / `stocks.list_date` 只做**交叉验证**:不一致的样本会被
    计数(而不是静默改口径)——本地 `stocks` 来自 tushare `stock_basic`,
    退市股也在册,所以对 2018 年以来的新股应该有完整覆盖。
    """
    if len(stocks) == 0:
        return {}
    con = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
    try:
        rows = con.execute("select code, exchange, list_date from stocks").fetchall()
    except sqlite3.Error as e:      # 缺 stocks 表(最小研究库)→ 如实标注
        return {"n": int(len(stocks)), "stocks_table": f"unavailable: {e}"}
    finally:
        con.close()
    exchange = {c: (ex or "").upper() for c, ex, _ in rows}
    list_date = {c: str(ld or "") for c, _, ld in rows}
    missing, ex_bad, ld_bad = [], [], []
    for st in stocks.itertuples(index=False):
        if st.code not in exchange:
            missing.append(st.code)
            continue
        want = "SH" if str(st.board).startswith("SH") else "SZ"
        if exchange[st.code] and exchange[st.code] != want:
            ex_bad.append(st.code)
        want_date = st.issue_date.isoformat()
        if list_date[st.code] and list_date[st.code] != want_date:
            ld_bad.append(st.code)
    return {
        "n": int(len(stocks)),
        "missing_from_stocks": len(missing),
        "exchange_mismatch": len(ex_bad),
        "list_date_mismatch": len(ld_bad),
        "note": "板块按代码前缀判定;stocks 表仅交叉核对(exchange/list_date)",
    }


def load_first_day_cache(path: Path) -> dict[str, list[dict]]:
    """读回填缓存(code,date,open,high,low,close,source),按 code 归组。"""
    if not path.exists():
        return {}
    df = pd.read_csv(path, dtype={"code": str, "date": str})
    out: dict[str, list[dict]] = {}
    for rec in df.to_dict("records"):
        code = rec["code"]
        if not code:
            continue
        out.setdefault(code, []).append({f: rec[f] for f in BAR_FIELDS})
    for bars in out.values():
        bars.sort(key=lambda b: b["date"])
        for b in bars:
            for f in ("open", "high", "low", "close"):
                b[f] = float(b[f])
    return out


def append_first_day_cache(path: Path, bars: dict[str, list[dict]]) -> int:
    """回填结果追加到缓存 CSV(已存在的 code 先剔除,保持幂等)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = [{"code": c, **b, "source": "tushare"}
             for c, bs in bars.items() for b in bs]
    if not fresh:
        return 0
    new_df = pd.DataFrame(fresh)
    if path.exists():
        old = pd.read_csv(path, dtype={"code": str, "date": str})
        old = old[~old.code.isin(new_df.code.unique())]
        new_df = pd.concat([old, new_df], ignore_index=True)
    new_df.to_csv(path, index=False)
    return len(fresh)


def fetch_missing_first_days(pro, listings: dict[str, str], *,
                             limiter: RateLimiter | None = None,
                             max_dates: int | None = None,
                             flush_path: Path | None = None,
                             flush_every: int = 50) -> dict[str, list[dict]]:
    """按上市日整市场拉 `daily`(一天一次调用),只留我们关心的新股。

    552 个上市日 × 1 次 = 552 次调用,按 50 次/分 ≈ 12 分钟 —— 这是
    **一次性研究回填**,缓存后重跑不再触网。`flush_path` 每 `flush_every`
    个交易日落一次盘:进程被打断也能续跑,不用从头再来。
    """
    by_date: dict[str, dict[str, None]] = {}
    for code, d in listings.items():
        by_date.setdefault(d, {})[code] = None
    dates = sorted(by_date)
    if max_dates is not None:
        dates = dates[:max_dates]
    out: dict[str, list[dict]] = {}
    pending: dict[str, list[dict]] = {}
    for i, d in enumerate(dates, 1):
        wanted = by_date[d]
        stamp = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        df = _call_with_retry(pro.daily, trade_date=d, limiter=limiter)
        if df is None or len(df) == 0:
            continue
        for rec in df.to_dict("records"):
            code, _, _ = str(rec["ts_code"]).partition(".")
            if code not in wanted:
                continue
            bar = {
                "date": stamp,
                "open": float(rec["open"]), "high": float(rec["high"]),
                "low": float(rec["low"]), "close": float(rec["close"]),
            }
            out.setdefault(code, []).append(bar)
            pending.setdefault(code, []).append(bar)
        if flush_path is not None and pending and i % flush_every == 0:
            append_first_day_cache(flush_path, pending)
            pending = {}
        if i % 50 == 0:
            print(f"  [first-day 回填] {i}/{len(dates)} 个上市日,"
                  f"已覆盖 {len(out)}/{len(listings)} 只", flush=True)
    if flush_path is not None and pending:
        append_first_day_cache(flush_path, pending)
    return out


def code_to_ts_code(code: str) -> str:
    """6 位代码 → ts_code(与 TushareAdapter._code_to_ts_code 同口径)。"""
    if code.startswith("6"):
        return f"{code}.SH"
    if code.startswith(("4", "8", "9")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def codes_needing_next_day(stocks: pd.DataFrame, first_days: dict[str, list[dict]],
                           *, sell_rule: str = "next-open") -> dict[str, str]:
    """首日 bar 触发了次日回退、但手里只有一根 bar 的 code → 上市日。

    按上市日整市场拉的那一遍只有**一根** bar;注册制前主板 44% 顶格收盘
    (或一字板)要按次日开盘卖,必须逐票补拉上市日之后几天的日线。
    """
    need: dict[str, str] = {}
    for st in stocks.itertuples(index=False):
        bars = first_days.get(st.code) or []
        if len(bars) >= 2:
            continue
        _, rule = choose_sell_price(bars, float(st.price), str(st.board),
                                    st.issue_date, sell_rule=sell_rule)
        if rule == "unsellable":
            need[st.code] = st.issue_date.strftime("%Y%m%d")
    return need


def fetch_next_day_bars(pro, needed: dict[str, str], *, span_days: int = 20,
                        limiter: RateLimiter | None = None,
                        flush_path: Path | None = None,
                        flush_every: int = 50) -> dict[str, list[dict]]:
    """逐票补拉 `[上市日, 上市日+span_days]` 日线(1 票 1 次调用)。

    只对「首日卖不出」的样本调用(注册制前主板/创业板),量级几百只。
    """
    out: dict[str, list[dict]] = {}
    pending: dict[str, list[dict]] = {}
    for i, (code, d) in enumerate(sorted(needed.items()), 1):
        df = _call_with_retry(pro.daily, ts_code=code_to_ts_code(code),
                              start_date=d,
                              end_date=(date.fromisoformat(
                                  f"{d[:4]}-{d[4:6]}-{d[6:]}")
                                  + timedelta(days=span_days)).strftime("%Y%m%d"),
                              limiter=limiter)
        if df is None or len(df) == 0:
            continue
        bars: list[dict] = []
        for rec in df.to_dict("records"):
            bars.append({
                "date": (f"{str(rec['trade_date'])[:4]}-{str(rec['trade_date'])[4:6]}"
                         f"-{str(rec['trade_date'])[6:8]}"),
                "open": float(rec["open"]), "high": float(rec["high"]),
                "low": float(rec["low"]), "close": float(rec["close"]),
            })
        bars.sort(key=lambda b: b["date"])
        out[code] = bars
        pending[code] = bars
        if flush_path is not None and pending and i % flush_every == 0:
            append_first_day_cache(flush_path, pending)
            pending = {}
        if i % 50 == 0:
            print(f"  [next-day 回填] {i}/{len(needed)} 只", flush=True)
    if flush_path is not None and pending:
        append_first_day_cache(flush_path, pending)
    return out


# --------------------------------------------------------------------------
# 主计算:新股样本 → 逐股期望利润 → 逐年增强
# --------------------------------------------------------------------------
def normalize_new_share(raw: pd.DataFrame) -> pd.DataFrame:
    """列名/类型归一 + 显式排除(北交所 / ST / 缺字段),返回带板块的样本。"""
    if raw is None or len(raw) == 0:
        return pd.DataFrame(columns=[
            "code", "name", "board", "ipo_date", "issue_date", "price",
            "amount_wan", "market_amount_wan", "limit_amount_wan", "ballot_pct"])
    df = raw.copy()
    for c in ("ts_code", "sub_code", "name", "ipo_date", "issue_date"):
        if c in df.columns:
            df[c] = (df[c].astype(str).str.strip()
                     .replace({"nan": "", "None": ""}))
    for c in ("price", "amount", "market_amount", "limit_amount", "ballot"):
        df[c] = pd.to_numeric(df.get(c), errors="coerce")
    df["board"] = df.ts_code.map(classify_board)
    df["issue"] = pd.to_datetime(df.issue_date, format="%Y%m%d", errors="coerce")
    df["ipo"] = pd.to_datetime(df.ipo_date, format="%Y%m%d", errors="coerce")
    # 名称里的 ST/退市整理在**上市时点**不会出现,但显式过滤,避免未来样本
    # (如转板/重新上市/风险警示股)混进来。
    is_st = df.name.str.contains("ST|退", regex=True, na=False)
    keep = (df.board.notna() & df.issue.notna() & (df.price > 0) & (df.ballot > 0)
            & (~is_st))
    out = pd.DataFrame({
        "code": df.ts_code.str.slice(0, 6),
        "name": df.name,
        "board": df.board,
        "ipo_date": df.ipo.dt.date,
        "issue_date": df.issue.dt.date,
        "price": df.price,
        "amount_wan": df.amount,
        "market_amount_wan": df.market_amount,
        "limit_amount_wan": df.limit_amount,
        "ballot_pct": df.ballot,
    })[keep]
    return out.reset_index(drop=True)


def excluded_counts(raw: pd.DataFrame) -> dict[str, int]:
    """排除计数(北交所 / ST / 缺发行价 / 缺中签率 / 未上市),写进报告。"""
    if raw is None or len(raw) == 0:
        return {}
    df = raw.copy()
    for c in ("price", "ballot"):
        df[c] = pd.to_numeric(df.get(c), errors="coerce")
    issue = pd.to_datetime(
        df.get("issue_date", pd.Series("", index=df.index)).astype(str).str.strip()
        .replace({"nan": ""}), format="%Y%m%d", errors="coerce")
    board = df.ts_code.astype(str).map(classify_board)
    name = df.get("name", pd.Series("", index=df.index)).astype(str)
    return {
        "new_share_rows": int(len(df)),
        "excluded_bj_or_unknown_board": int(board.isna().sum()),
        "excluded_st_or_delist": int(name.str.contains("ST|退", regex=True).sum()),
        "excluded_missing_price": int((~(df.price > 0)).sum()),
        "excluded_missing_ballot": int((~(df.ballot > 0)).sum()),
        "excluded_not_listed_yet": int(issue.isna().sum()),
    }


def compute_tier(stocks: pd.DataFrame, first_days: dict[str, list[dict]], *,
                 tier: float, sz_mv: float = 0.0,
                 sell_rule: str = "next-open") -> pd.DataFrame:
    """单一档位下逐股期望利润(元)。未覆盖/不可卖的样本剔除并计数。"""
    rows: list[dict] = []
    for st in stocks.itertuples(index=False):
        board = str(st.board)
        market_value = tier if board in SH_BOARDS else sz_mv
        if not board_permitted(board, sh_mv=tier, sz_mv=sz_mv):
            continue
        bars = first_days.get(st.code) or []
        sell, rule = choose_sell_price(bars, float(st.price), board,
                                       st.issue_date, sell_rule=sell_rule)
        if sell is None:
            continue
        sub = subscription_shares(
            board, market_value, limit_amount_wan=st.limit_amount_wan,
            market_amount_wan=st.market_amount_wan)
        if sub <= 0:
            continue
        unit_shares = BOARD_UNITS[board][1]
        exp_shares = sub * float(st.ballot_pct) / 100.0
        cost = sell * sell_cost_rate(st.issue_date + timedelta(days=10))
        profit = exp_shares * (sell - float(st.price) - cost)
        rows.append({
            "code": st.code, "name": st.name, "board": board,
            "issue_date": st.issue_date.isoformat(), "year": st.issue_date.year,
            "price": float(st.price), "sell": sell, "sell_rule": rule,
            "ballot_pct": float(st.ballot_pct), "sub_shares": int(sub),
            "exp_shares": exp_shares, "exp_lots": exp_shares / unit_shares,
            "first_day_ret": sell / float(st.price) - 1.0,
            "exp_profit": profit,
        })
    return pd.DataFrame(rows)


YEARS_ALL = [str(y) for y in range(2018, 2027)]
GATE_YEARS = ["2022", "2023", "2024", "2025", "2026"]


def yearly_table(rows: pd.DataFrame, tier: float) -> dict[str, dict]:
    """逐自然年:期望利润、增强(%/年)、分位数、破发率、贡献集中度。"""
    out: dict[str, dict] = {}
    for year in YEARS_ALL:
        y = rows[rows.year == int(year)] if len(rows) else rows
        if len(y) == 0:
            out[year] = {"n_ipos": 0, "note": "无样本"}
            continue
        profit = y.exp_profit
        pos, neg = float(profit[profit > 0].sum()), float(profit[profit < 0].sum())
        top5 = float(profit.nlargest(5).sum())
        q = profit.quantile([0.05, 0.25, 0.5, 0.75, 0.95])
        ret = y.first_day_ret
        out[year] = {
            "n_ipos": int(len(y)),
            "exp_profit_yuan": round(float(profit.sum()), 2),
            "enhancement": round(float(profit.sum()) / tier, 6),
            "yuan_per_million": round(float(profit.sum()) / tier * 1e6, 1),
            "exp_lots": round(float(y.exp_lots.sum()), 3),
            # 期望签数是小数,实际是「中/不中」:至少中一签的概率(各新股
            # 独立,1-Π(1-p));p = 申购股数 × 中签率 ÷ 每签股数。
            "prob_any_lot": round(
                float(1 - (1 - y.exp_lots.clip(upper=0.999)).prod()), 4),
            "n_boards": {b: int((y.board == b).sum())
                         for b in sorted(y.board.unique())},
            "profit_q05": round(float(q.loc[0.05]), 2),
            "profit_q25": round(float(q.loc[0.25]), 2),
            "profit_q50": round(float(q.loc[0.50]), 2),
            "profit_q75": round(float(q.loc[0.75]), 2),
            "profit_q95": round(float(q.loc[0.95]), 2),
            "profit_pos_total": round(pos, 2),
            "profit_neg_total": round(neg, 2),
            "share_negative": round(float((profit < 0).mean()), 4),
            "top5_share_of_pos": round(top5 / pos, 4) if pos > 0 else None,
            "first_day_ret_mean": round(float(ret.mean()), 4),
            "first_day_ret_q05": round(float(ret.quantile(0.05)), 4),
            "first_day_ret_q50": round(float(ret.quantile(0.50)), 4),
            "first_day_ret_q95": round(float(ret.quantile(0.95)), 4),
            "break_issue_rate": round(float((ret < 0).mean()), 4),
        }
    return out


REGIMES = [
    ("2018-01-01", "2019-07-21", "核准制:主板/创业板 44% 首日上限"),
    ("2019-07-22", "2021-09-17", "科创板开板 → 注册制询价新规前"),
    ("2021-09-18", "2023-02-16", "询价新规/破发潮"),
    ("2023-02-17", "2023-08-27", "全面注册制(主板前 5 日不限价)"),
    ("2023-08-28", "2024-08-31", "发行收紧/阶段性暂停"),
    ("2024-09-01", "2026-12-31", "重启:低供给 + 高首日溢价"),
]


def regime_table(rows: pd.DataFrame, tier: float) -> dict[str, dict]:
    """分段年化增强(按段内天数折算成 %/年),回答「是不是 regime 依赖」。"""
    out: dict[str, dict] = {}
    for start, end, label in REGIMES:
        if len(rows) == 0:
            continue
        seg = rows[(rows.issue_date >= start) & (rows.issue_date <= end)]
        if len(seg) == 0:
            continue
        d0 = max(date.fromisoformat(start), date.fromisoformat(seg.issue_date.min()))
        d1 = min(date.fromisoformat(end), date.fromisoformat(seg.issue_date.max()))
        span_days = max((d1 - d0).days, 30)
        total = float(seg.exp_profit.sum()) / tier
        out[f"{start}~{end}"] = {
            "label": label,
            "n_ipos": int(len(seg)),
            "span_days": span_days,
            "period_return": round(total, 6),
            "annualized": round(total * 365.0 / span_days, 6),
            "break_issue_rate": round(float((seg.first_day_ret < 0).mean()), 4),
        }
    return out


def rolling_windows(rows: pd.DataFrame, tier: float, *, window_days: int = 365,
                    step_days: int = 30) -> dict[str, float]:
    """滚动 12 个月窗口的增强(%)分布 —— 均值之外也看尾部。"""
    if len(rows) == 0:
        return {}
    s = rows.copy()
    s["d"] = pd.to_datetime(s.issue_date)
    start, end = s.d.min().date(), s.d.max().date()
    vals: list[float] = []
    cur = start
    while cur + timedelta(days=window_days) <= end:
        seg = s[(s.d.dt.date >= cur)
                & (s.d.dt.date < cur + timedelta(days=window_days))]
        vals.append(float(seg.exp_profit.sum()) / tier)
        cur += timedelta(days=step_days)
    if not vals:
        return {}
    v = pd.Series(vals)
    return {
        "n_windows": int(len(v)),
        "min": round(float(v.min()), 6),
        "p25": round(float(v.quantile(0.25)), 6),
        "median": round(float(v.median()), 6),
        "p75": round(float(v.quantile(0.75)), 6),
        "max": round(float(v.max()), 6),
        "share_positive": round(float((v > 0).mean()), 4),
    }


def evaluate_gate(yearly: dict[str, dict], *, years: list[str] | None = None,
                  min_worst: float = 0.005) -> dict:
    """任务书闸:近 5 年逐年增强 > 0,且最差年 > +0.5%/年。"""
    years = years or GATE_YEARS
    vals = {y: (yearly.get(y) or {}).get("enhancement") for y in years}
    have = {y: v for y, v in vals.items() if v is not None}
    worst = min(have.values()) if have else None
    return {
        "years": years,
        "enhancement_by_year": vals,
        "worst": worst,
        "min_required": min_worst,
        "pass": bool(have and len(have) == len(years) and worst is not None
                     and worst > min_worst
                     and all(v > 0 for v in have.values())),
    }


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------
def build_report(stocks: pd.DataFrame, first_days: dict[str, list[dict]], *,
                 tiers: tuple[float, ...] = DEFAULT_TIERS,
                 sz_mv_ratios: tuple[float, ...] = (0.0,),
                 sell_rule: str = "next-open",
                 coverage: dict | None = None,
                 detail_tiers: tuple[float, ...] = GATE_TIERS) -> dict:
    """档位 → 逐年/分段/滚动窗口增强 + 闸门;含逐股明细(默认两个闸门档)。"""
    coverage = coverage or {}
    report: dict = {
        "period": ([str(stocks.issue_date.min()), str(stocks.issue_date.max())]
                   if len(stocks) else []),
        "sell_rule": sell_rule,
        "sample": coverage,
        "assumptions": {
            "quota": "min(市值额度, 网上发行量×1‰, 接口申购上限),向下取整到申购单位",
            "board_units": {b: {"unit_mv": u[0], "unit_shares": u[1]}
                            for b, u in BOARD_UNITS.items()},
            "star_permission_tier": STAR_MIN_TIER,
            "gem_permission_tier": GEM_MIN_TIER,
            "sell": "首日开盘价;一字板/注册制前 44% 顶格 → 次日开盘价",
            "cost": {"commission": COMMISSION_RATE,
                     "stamp_pre_20230828": STAMP_RATE_PRE,
                     "stamp_now": STAMP_RATE_NOW,
                     "transfer_fee": TRANSFER_FEE_RATE,
                     "note": "中签缴款无佣金;卖出侧成本按卖出日档位"},
            "tax": "个人转让上市公司股票免征个人所得税 → 打新收益免税",
            "excluded": "北交所(全额资金预缴)、ST/退市整理、缺价格/中签率/行情",
        },
        "tiers": {},
        "sz_sensitivity": {},
        "gate": {"tiers": [str(int(t)) for t in GATE_TIERS],
                 "rule": "近 5 年逐年增强 > 0 且最差年 > +0.5%/年"},
    }
    for tier in tiers:
        rows = compute_tier(stocks, first_days, tier=float(tier), sz_mv=0.0,
                            sell_rule=sell_rule)
        yearly = yearly_table(rows, float(tier))
        entry = {
            "tier": float(tier),
            "n_subscribed": int(len(rows)),
            "boards": ({b: int((rows.board == b).sum())
                        for b in sorted(rows.board.unique())}
                       if len(rows) else {}),
            "annual": yearly,
            "regimes": regime_table(rows, float(tier)),
            "rolling_12m": rolling_windows(rows, float(tier)),
            "gate": evaluate_gate(yearly),
        }
        if float(tier) in [float(t) for t in detail_tiers]:
            entry["per_stock"] = [
                {**rec, "exp_profit": round(rec["exp_profit"], 4),
                 "exp_shares": round(rec["exp_shares"], 6),
                 "exp_lots": round(rec["exp_lots"], 6),
                 "turnover": round(rec["sell"] * rec["exp_shares"], 2)}
                for rec in rows.to_dict("records")]
        report["tiers"][str(int(tier))] = entry

    for ratio in sz_mv_ratios:
        if ratio <= 0:
            continue
        key = f"sz_mv_eq_{ratio:g}x_sh"
        cells = {}
        for tier in tiers:
            rows = compute_tier(stocks, first_days, tier=float(tier),
                                sz_mv=float(tier) * ratio, sell_rule=sell_rule)
            # 分母用**总市值**(沪 + 深),否则「增强」会被分母偏小的沪市值放大
            total_capital = float(tier) * (1.0 + ratio)
            cells[str(int(tier))] = {
                "n_subscribed": int(len(rows)),
                "total_capital": total_capital,
                "annual": {y: v.get("enhancement") for y, v in
                           yearly_table(rows, total_capital).items()},
            }
        report["sz_sensitivity"][key] = cells
    return report


def print_summary(report: dict) -> None:
    print("\n=== 打新增强估计(期望值,不预测)===")
    print(f"样本区间: {report['period']}  卖出规则: {report['sell_rule']}")
    for tier, entry in report["tiers"].items():
        print(f"\n档位 {int(tier) / 10_000:.0f} 万(沪市市值)"
              f"  样本 {entry['n_subscribed']} 只")
        for year, cell in entry["annual"].items():
            if not cell.get("n_ipos"):
                print(f"  {year}: 无样本")
                continue
            print(f"  {year}: n={cell['n_ipos']:>3}  增强 "
                  f"{cell['enhancement'] * 100:+.3f}%/年  "
                  f"({cell['yuan_per_million']:+.0f} 元/年/百万)  "
                  f"破发率 {cell['break_issue_rate']:.1%}  "
                  f"负贡献 {cell['profit_neg_total']:+.0f} 元")
        gate = entry["gate"]
        print(f"  闸门(近5年最差 > +0.5%/年): "
              f"{'通过' if gate['pass'] else '未通过'} (最差 {gate['worst']})")


def parse_tiers(raw: str) -> tuple[float, ...]:
    return tuple(float(x) for x in str(raw).replace(" ", "").split(",") if x)


def main() -> None:
    ap = argparse.ArgumentParser(description="打新增强估计器(无策略依赖)")
    ap.add_argument("--market-db", default=DEFAULT_MARKET_DB)
    ap.add_argument("--config-db", default=DEFAULT_CONFIG_DB)
    ap.add_argument("--tiers", default=",".join(str(t) for t in DEFAULT_TIERS),
                    help="沪市市值档位(元),逗号分隔")
    ap.add_argument("--sell-rule", choices=["next-open", "first-open"],
                    default="next-open",
                    help="next-open=一字板/44%% 顶格按次日开盘(任务书口径);"
                         "first-open=全部按首日开盘(敏感性对照)")
    ap.add_argument("--sz-mv-ratio", default="0",
                    help="深市市值 ÷ 沪市市值(敏感性;默认 0 = sse_enhance 持有形态)")
    ap.add_argument("--start", default=NEW_SHARE_START)
    ap.add_argument("--end", default=date.today().strftime("%Y%m%d"))
    ap.add_argument("--new-share-cache",
                    default=str(ROOT / "data" / "new_share_raw.csv"))
    ap.add_argument("--first-day-cache",
                    default=str(ROOT / "data" / "ipo_first_day_backfill.csv"))
    ap.add_argument("--fetch-missing-first-day", action="store_true",
                    help="用 tushare daily(按上市日整市场)补齐本地库覆盖前的新股首日行情")
    ap.add_argument("--refresh-new-share", action="store_true",
                    help="忽略本地 new_share 缓存重新分页拉取")
    ap.add_argument("--detail-tiers",
                    default=",".join(str(int(t)) for t in GATE_TIERS))
    ap.add_argument("--out", default=str(ROOT / "data" / "ipo_yield_study.json"))
    args = ap.parse_args()

    tiers = parse_tiers(args.tiers)
    cache = Path(args.new_share_cache)
    need_fetch = args.refresh_new_share or not cache.exists()
    if not need_fetch:
        last = latest_cached_date(cache)
        # 缓存「足够新」即可复用:最后一笔 ipo_date 晚于 7 天前就不必再打网。
        fresh_enough = (date.today() - timedelta(days=7)).strftime("%Y%m%d")
        need_fetch = last is None or last < fresh_enough
        if need_fetch:
            print(f"new_share 缓存最新 {last} 已过期(>7 天)→ 重新拉取")
    pro = None
    if need_fetch:
        pro = build_pro(args.config_db)
        print(f"tushare new_share 分页拉取 {args.start}~{args.end}"
              f"(≤{PAGE_SIZE} 行/次,{RATE_LIMIT_PER_MIN} 次/分限速)…", flush=True)
        raw = fetch_new_share(pro, args.start, args.end, limiter=RateLimiter())
        if len(raw) == 0:
            raise SystemExit("new_share 拉取为空")
        cache.parent.mkdir(parents=True, exist_ok=True)
        raw.to_csv(cache, index=False)
        print(f"  → {len(raw)} 行落 {cache}")
    else:
        raw = pd.read_csv(cache, dtype=str)
        print(f"new_share 用缓存 {cache}({len(raw)} 行)")

    stocks = normalize_new_share(raw)
    exclusions = excluded_counts(raw)
    print(f"样本: {len(stocks)} 只(排除计数 {exclusions})")

    listings = {r.code: r.issue_date.strftime("%Y%m%d")
                for r in stocks.itertuples(index=False)}
    first_days = load_local_first_days(args.market_db, listings)
    local_n = len(first_days)
    missing = {c: d for c, d in listings.items() if c not in first_days}
    fdcache = Path(args.first_day_cache)
    cached = load_first_day_cache(fdcache)
    for c in list(missing):
        if c in cached:
            first_days[c] = cached[c]
            del missing[c]
    cache_hits = len(first_days) - local_n
    backfill_n = 0
    if missing and args.fetch_missing_first_day:
        if pro is None:
            pro = build_pro(args.config_db)
        print(f"首日行情缺口 {len(missing)} 只 → tushare daily 按上市日回填"
              f"({len(set(missing.values()))} 个交易日,"
              f"限速 {RATE_LIMIT_PER_MIN} 次/分)")
        # 每 50 个交易日增量落盘:进程被打断可续跑(已缓存的 code 自动跳过)
        fresh = fetch_missing_first_days(pro, missing, limiter=RateLimiter(),
                                         flush_path=fdcache, flush_every=50)
        append_first_day_cache(fdcache, fresh)
        first_days.update(fresh)
        backfill_n = len(fresh)
        missing = {c: d for c, d in missing.items() if c not in fresh}
    if missing:
        print(f"⚠ 首日行情仍缺 {len(missing)} 只(缺行情的年份只报额度/中签率,"
              f"不猜收益)")
    next_n = 0
    if args.fetch_missing_first_day:
        need_next = codes_needing_next_day(stocks, first_days,
                                           sell_rule=args.sell_rule)
        if need_next:
            if pro is None:
                pro = build_pro(args.config_db)
            print(f"首日卖不出(一字板/44% 顶格)且缺次日行情的样本 "
                  f"{len(need_next)} 只 → 逐票补拉(1 票 1 次调用)")
            more = fetch_next_day_bars(pro, need_next, limiter=RateLimiter(),
                                       flush_path=fdcache, flush_every=50)
            first_days.update(more)
            next_n = len(more)

    covered = set(first_days)
    coverage = {
        **exclusions,
        "n_valid_stocks": int(len(stocks)),
        "first_day_local_db": local_n,
        "first_day_cache": cache_hits,
        "first_day_fetched": backfill_n,
        "first_day_next_fetched": next_n,
        "first_day_missing": int(len(missing)),
        "coverage_by_year": {
            str(y): {
                "n_ipos": int((stocks.issue_date.map(lambda d, y=y: d.year == y)).sum()),
                "n_with_first_day": int(
                    stocks[stocks.issue_date.map(lambda d, y=y: d.year == y)]
                    .code.isin(covered).sum()),
            } for y in sorted({d.year for d in stocks.issue_date})
        },
        "market_db": args.market_db,
        "roster_cross_check": roster_cross_check(args.market_db, stocks),
        "note": "本地 daily_quotes 自 2021-09-13 起覆盖;更早新股首日行情"
                "需 --fetch-missing-first-day 回填,否则该年只有额度/中签率口径",
    }
    report = build_report(stocks, first_days, tiers=tiers,
                          sz_mv_ratios=parse_tiers(args.sz_mv_ratio),
                          sell_rule=args.sell_rule, coverage=coverage,
                          detail_tiers=parse_tiers(args.detail_tiers))
    report["gate"]["results"] = {
        str(int(t)): report["tiers"][str(int(t))]["gate"]["pass"]
        for t in GATE_TIERS if str(int(t)) in report["tiers"]}
    report["gate"]["all_pass"] = all(report["gate"]["results"].values())
    print_summary(report)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(f"\n闸门: {report['gate']['results']} → "
          f"{'通过(可写策略)' if report['gate']['all_pass'] else '未通过(证否:只交研究文档)'}")
    print(f"saved -> {out}")


if __name__ == "__main__":
    main()
