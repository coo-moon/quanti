"""ETF 日线数据：从 tushare 拉取(fund_daily + fund_adj)并缓存到 market.db 的 etf_daily 表。

market.db 只有股票(daily_quotes)、数据管道不支持基金，故此模块独立拉 ETF。token 经
quanti.data.source.tushare_token(db) 读取(app_config > env)，绝不硬编码。tushare 客户端
默认 https；本模块用 requests 直连,先 https 后 http 兜底(某些环境 https 被挡)。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
import requests

# 精选流动性 ETF 全集(code, 名称, 类别, 是否T+0)。可扩展；screen 会再按实际 ADV 过滤。
# T+0: 债券/黄金/跨境(QDII) 当日可回转,网格效率更高(本日线回测按 T+1 保守计,标注供参考)。
ETF_UNIVERSE: list[tuple[str, str, str, bool]] = [
    # 宽基
    ("510300.SH", "沪深300ETF", "宽基", False),
    ("510500.SH", "中证500ETF", "宽基", False),
    ("510050.SH", "上证50ETF", "宽基", False),
    ("159915.SZ", "创业板ETF", "宽基", False),
    ("159949.SZ", "创业板50", "宽基", False),
    ("588000.SH", "科创50ETF", "宽基", False),
    ("512100.SH", "中证1000ETF", "宽基", False),
    ("510880.SH", "红利ETF", "宽基", False),
    ("510180.SH", "上证180ETF", "宽基", False),
    ("515000.SH", "科技ETF", "宽基", False),
    # 行业/主题
    ("512880.SH", "证券ETF", "行业", False),
    ("512480.SH", "半导体ETF", "行业", False),
    ("159813.SZ", "半导体SZ", "行业", False),
    ("512170.SH", "医疗ETF", "行业", False),
    ("512010.SH", "医药ETF", "行业", False),
    ("159992.SZ", "创新药ETF", "行业", False),
    ("512660.SH", "军工ETF", "行业", False),
    ("512690.SH", "酒ETF", "行业", False),
    ("512800.SH", "银行ETF", "行业", False),
    ("512200.SH", "地产ETF", "行业", False),
    ("515790.SH", "光伏ETF", "行业", False),
    ("516160.SH", "新能源ETF", "行业", False),
    ("512400.SH", "有色ETF", "行业", False),
    ("159928.SZ", "消费ETF", "行业", False),
    ("512980.SH", "传媒ETF", "行业", False),
    ("515050.SH", "5G通信ETF", "行业", False),
    ("512760.SH", "芯片ETF", "行业", False),
    ("516970.SH", "基建ETF", "行业", False),
    ("159611.SZ", "电力ETF", "行业", False),
    ("512580.SH", "环保ETF", "行业", False),
    # T+0(债券/黄金/跨境)
    ("511380.SH", "转债ETF", "债券T0", True),
    ("511010.SH", "国债ETF", "债券T0", True),
    ("511260.SH", "十年国债ETF", "债券T0", True),
    ("511090.SH", "30年国债ETF", "债券T0", True),
    ("518880.SH", "黄金ETF", "黄金T0", True),
    ("159934.SZ", "黄金ETFSZ", "黄金T0", True),
    ("513100.SH", "纳指ETF", "跨境T0", True),
    ("513500.SH", "标普500ETF", "跨境T0", True),
    ("513050.SH", "中概互联ETF", "跨境T0", True),
    ("513180.SH", "恒生科技ETF", "跨境T0", True),
    ("159941.SZ", "纳指ETFSZ", "跨境T0", True),
]
ETF_META = {c: (name, cat, t0) for c, name, cat, t0 in ETF_UNIVERSE}

_TS_HOSTS = ("https://api.tushare.pro", "http://api.tushare.pro")


class EtfDataError(RuntimeError):
    """token 缺失/无权限/网络不可达等 ETF 数据错误。"""


def _tushare(api: str, token: str, **params) -> tuple[list[str], list[list]]:
    """调用 tushare API，返回 (fields, items)。https 优先，http 兜底。"""
    if not token:
        raise EtfDataError("未配置 tushare token（去『数据源配置』填入，需有基金接口权限）")
    payload = {"api_name": api, "token": token, "params": params, "fields": ""}
    last = None
    for host in _TS_HOSTS:
        for _ in range(3):
            try:
                r = requests.post(host, json=payload, timeout=20)
                j = r.json()
                code = j.get("code")
                if code == 40203 or (code and "权限" in str(j.get("msg", ""))):
                    raise EtfDataError(
                        f"tushare token 无 {api} 接口权限（基金接口需 2000 积分）：{j.get('msg', '')[:60]}")
                if code != 0:
                    raise RuntimeError(j.get("msg", f"tushare code={code}"))
                d = j["data"]
                return d["fields"], d["items"]
            except EtfDataError:
                raise
            except Exception as e:  # noqa: BLE001 网络/解析,换 host 或重试
                last = e
                time.sleep(1)
    raise EtfDataError(f"tushare 不可达（{api}）：{type(last).__name__} {last}")


def fetch_etf_daily(code: str, start: str, end: str, token: str) -> list[tuple]:
    """拉单只 ETF 后复权所需的日线。start/end 为 YYYYMMDD。
    返回 rows: (code, date_iso, open, high, low, close, amount_yuan, adj_factor)。
    """
    df, di = _tushare("fund_daily", token, ts_code=code, start_date=start, end_date=end)
    if not di:
        return []
    fa, ai = _tushare("fund_adj", token, ts_code=code, start_date=start, end_date=end)
    ix = {n: i for i, n in enumerate(df)}
    adj = {row[fa.index("trade_date")]: float(row[fa.index("adj_factor")]) for row in ai}
    # adj 可能缺当日，用最近一个向后/向前填
    out = []
    for row in di:
        td = row[ix["trade_date"]]
        o, h, low_, c = (row[ix[k]] for k in ("open", "high", "low", "close"))
        amt = row[ix["amount"]]  # 千元
        if o is None or c is None:
            continue
        af = adj.get(td)
        iso = f"{td[:4]}-{td[4:6]}-{td[6:]}"
        out.append((code, iso, float(o), float(h), float(low_), float(c),
                    float(amt) * 1000.0, af))
    out.sort(key=lambda r: r[1])
    # 填复权因子空洞
    last_af = 1.0
    filled = []
    for r in out:
        af = r[7] if r[7] is not None else last_af
        last_af = af
        filled.append((r[0], r[1], r[2], r[3], r[4], r[5], r[6], af))
    return filled


def ensure_table(db) -> None:
    db.conn.execute(
        """
        CREATE TABLE IF NOT EXISTS etf_daily (
            code TEXT NOT NULL, date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            amount REAL, adj_factor REAL DEFAULT 1.0,
            PRIMARY KEY (code, date)
        )
        """
    )
    db.conn.execute("CREATE INDEX IF NOT EXISTS idx_etf_daily_code ON etf_daily(code)")


def sync_universe(db, token: str, start: str, end: str,
                  progress=None, codes: list[str] | None = None) -> dict:
    """拉全集(或指定 codes)写入 etf_daily。progress(current,total,msg) 回调可选。"""
    ensure_table(db)
    codes = codes or [c for c, *_ in ETF_UNIVERSE]
    total = len(codes)
    ok, failed = 0, []
    for i, code in enumerate(codes):
        try:
            rows = fetch_etf_daily(code, start, end, token)
            if rows:
                db.conn.executemany(
                    "INSERT OR REPLACE INTO etf_daily "
                    "(code,date,open,high,low,close,amount,adj_factor) "
                    "VALUES (?,?,?,?,?,?,?,?)", rows)
                ok += 1
            else:
                failed.append(code)
        except EtfDataError:
            raise  # token/权限问题：整体失败，让上层报清楚
        except Exception as e:  # noqa: BLE001 单只失败不阻断
            failed.append(f"{code}:{type(e).__name__}")
        if progress:
            progress(i + 1, total, code)
        time.sleep(0.3)  # 节流,避免 tushare 频控
    return {"ok": ok, "failed": failed, "total": total}


@dataclass
class EtfBars:
    code: str
    dates: np.ndarray       # ISO 字符串
    close: np.ndarray       # 后复权
    high: np.ndarray
    low: np.ndarray
    raw_close: np.ndarray   # 原始价(设网格/展示用)
    raw_low: np.ndarray
    raw_high: np.ndarray
    amount: np.ndarray      # 元


def read_etf(db, code: str, start: str | None = None, end: str | None = None) -> EtfBars | None:
    """从 etf_daily 读单只,返回后复权 + 原始价数组。start/end 为 ISO(YYYY-MM-DD)。"""
    sql = "SELECT date,open,high,low,close,amount,adj_factor FROM etf_daily WHERE code=?"
    args: list = [code]
    if start:
        sql += " AND date>=?"
        args.append(start)
    if end:
        sql += " AND date<=?"
        args.append(end)
    sql += " ORDER BY date"
    rows = db.conn.execute(sql, args).fetchall()
    if not rows:
        return None
    a = np.array(rows, dtype=object)
    dates = a[:, 0].astype(str)
    rc = a[:, 4].astype(float)
    rl = a[:, 3].astype(float)
    rh = a[:, 2].astype(float)
    af = a[:, 6].astype(float)
    return EtfBars(code=code, dates=dates, close=rc * af, high=rh * af, low=rl * af,
                   raw_close=rc, raw_low=rl, raw_high=rh, amount=a[:, 5].astype(float))


def cache_status(db) -> dict:
    ensure_table(db)
    rows = db.conn.execute(
        "SELECT COUNT(DISTINCT code), MIN(date), MAX(date), COUNT(*) FROM etf_daily").fetchone()
    return {"codes": rows[0] or 0, "start": rows[1], "end": rows[2], "rows": rows[3] or 0}
