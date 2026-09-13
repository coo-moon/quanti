"""A股回购事件流水回填(market.db 新表 repurchase_events)。

回购公告事件研究(2026-09-13)的底层数据:tushare `repurchase` 接口按
ann_date 范围拉 2018-01 至今全市场回购事件流水。接口单次返回上限 2000 行,
超限会**静默截断**(实测 2024 全年/2018 下半年各撞 2000)——故按月查询,
命中 2000 自动对半细分到旬,旬仍撞顶则细分到单日逐日拉。

字段(只存研究需要的):
  proc       实施进度 ∈ {预案, 股东大会通过, 实施, 完成, 停止}
  ann_date   公告日(YYYY-MM-DD,PIT 关键)
  end_date   进展截止日期(仅 实施/完成 行有值)
  exp_date   到期日(仅 预案 行有值)
  vol        已回购股数(股);amount 已回购/拟回购金额(元)
  high_limit/low_limit  价格上下限(预案=回购价上限;实施=区间)

主键 (code,ann_date,proc,amount):同票同日同进度若金额不同视为不同方案
(多次预案并存);完全重复行 INSERT OR REPLACE 天然幂等,可中断重跑。
amount 为 NULL 时落 **-1.0 哨兵**(SQLite 主键里 NULL 互不相等,真存 NULL
会让重跑产生永久重复行);研究层把 amount=-1 当缺失处理。

限速:默认 45 calls/min(与 backfill_cashflow 同一 token,留 5/min 余量),
分钟级超限按 patient 等待重试。

用法:
    /opt/data/quanti/.venv/bin/python scripts/backfill_repurchases.py \
        --market-db /opt/data/quanti/data/market.db --calls-per-min 45
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ROW_CAP = 2000  # tushare repurchase 单次返回上限

FIELDS = ("ts_code,ann_date,end_date,proc,exp_date,vol,amount,"
          "high_limit,low_limit")

DDL = """
CREATE TABLE IF NOT EXISTS repurchase_events (
    code TEXT NOT NULL,              -- 6 位代码
    ann_date TEXT NOT NULL,          -- 公告日 YYYY-MM-DD(PIT 关键)
    end_date TEXT,                   -- 进展截止日(实施/完成行)
    proc TEXT NOT NULL,              -- 预案/股东大会通过/实施/完成/停止
    exp_date TEXT,                   -- 预案到期日
    vol REAL,                        -- 已回购股数(股)
    amount REAL,                     -- 拟回购/已回购金额(元)
    high_limit REAL,
    low_limit REAL,
    PRIMARY KEY (code, ann_date, proc, amount)
)
"""

INSERT = """
INSERT OR REPLACE INTO repurchase_events
  (code, ann_date, end_date, proc, exp_date, vol, amount,
   high_limit, low_limit)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def resolve_token(market_db: str) -> str:
    tok = os.environ.get("TUSHARE_TOKEN")
    if tok:
        return tok
    con = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
    try:
        row = con.execute(
            "select data_source_token from app_config").fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        raise SystemExit("TUSHARE_TOKEN 未设置,且 app_config 里没有 token")
    return row[0]


def _norm(d: str | None) -> str | None:
    s = "" if d is None else str(d).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s or None


def _f(v):
    try:
        f = float(v)
        return f if f == f else None  # NaN -> None
    except (TypeError, ValueError):
        return None


def _rate_limited(msg: str) -> bool:
    return "频率超限" in msg or "每分钟" in msg or "access frequency" in msg


class Puller:
    """限速 + 撞顶细分的月份拉取器。calls/rows/stats 汇总在实例上。"""

    def __init__(self, pro, con, interval: float):
        self.pro = pro
        self.con = con
        self.interval = interval
        self.calls = 0
        self.rows_total = 0
        self.fails: list[str] = []
        self.split_days = 0

    def _sleep(self):
        if self.interval > 0:
            time.sleep(self.interval)

    def _query(self, start: str, end: str):
        for attempt in range(5):
            t0 = time.monotonic()
            try:
                df = self.pro.repurchase(start_date=start, end_date=end)
                self.calls += 1
                return df
            except Exception as e:  # noqa: BLE001
                self.calls += 1
                msg = str(e)
                if _rate_limited(msg) and attempt < 4:
                    time.sleep(65)
                    continue
                print(f"FAIL {start}~{end}: {msg[:120]}", flush=True)
                self.fails.append(f"{start}~{end}")
                return None
            finally:
                wait = self.interval - (time.monotonic() - t0)
                if wait > 0:
                    time.sleep(wait)
        return None

    def _store(self, df) -> int:
        rows = []
        for r in df.to_dict("records"):
            ts_code = str(r.get("ts_code") or "")
            code = ts_code.split(".")[0]
            proc = str(r.get("proc") or "").strip()
            ann = _norm(r.get("ann_date"))
            if not code or not proc or not ann:
                continue
            rows.append((
                code, ann, _norm(r.get("end_date")), proc,
                _norm(r.get("exp_date")), _f(r.get("vol")),
                # amount 是主键成分:NULL 归一为 -1.0 哨兵(幂等去重必需)
                _f(r.get("amount")) if _f(r.get("amount")) is not None else -1.0,
                _f(r.get("high_limit")), _f(r.get("low_limit")),
            ))
        if rows:
            self.con.executemany(INSERT, rows)
            self.con.commit()
        return len(rows)

    def pull_range(self, start: str, end: str) -> int:
        """拉 [start,end](YYYYMMDD)闭区间;返回行数。撞 2000 自动细分。"""
        df = self._query(start, end)
        if df is None:
            return 0
        n = len(df)
        if n < ROW_CAP:
            self.rows_total += self._store(df)
            return n
        # 撞顶 ⇒ 截断嫌疑,对半细分(单日不可再分则照收并标注)
        s = date(int(start[:4]), int(start[4:6]), int(start[6:]))
        e = date(int(end[:4]), int(end[4:6]), int(end[6:]))
        if s >= e:
            self.split_days += 1
            self.rows_total += self._store(df)
            print(f"WARN single-day cap hit {start} ({n} rows, "
                  f">={ROW_CAP} 可能仍有截断)", flush=True)
            return n
        mid = s + (e - s) // 2
        a = self.pull_range(s.strftime("%Y%m%d"), mid.strftime("%Y%m%d"))
        b = self.pull_range(
            (mid + timedelta(days=1)).strftime("%Y%m%d"), e.strftime("%Y%m%d"))
        return a + b


def months(start: str, end: str):
    """生成 [YYYYMM, YYYYMM] 的月份闭区间列表。"""
    y, m = int(start[:4]), int(start[4:6])
    ye, me = int(end[:4]), int(end[4:6])
    out = []
    while (y, m) <= (ye, me):
        out.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def month_span(y: int, m: int) -> tuple[str, str]:
    s = date(y, m, 1)
    e = (date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1))
    return s.strftime("%Y%m%d"), e.strftime("%Y%m%d")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market-db",
                    default="/opt/data/quanti/data/market.db")
    ap.add_argument("--start", default="20180101",
                    help="公告日起点(YYYYMMDD)")
    ap.add_argument("--end", default=date.today().strftime("%Y%m%d"))
    ap.add_argument("--calls-per-min", type=int, default=45)
    ap.add_argument("--progress", default="",
                    help="进度 JSON 落盘路径(供监督方轮询)")
    ap.add_argument("--done-file", default="",
                    help="完成后写 status=done 的标记文件")
    args = ap.parse_args()

    import tushare as ts
    pro = ts.pro_api(resolve_token(args.market_db))
    con = sqlite3.connect(args.market_db)
    con.execute(DDL)

    interval = 60.0 / args.calls_per_min if args.calls_per_min else 0
    ms = months(args.start, args.end)
    total = len(ms)
    puller = Puller(pro, con, interval)
    t_start = time.monotonic()

    def save_progress(status: str) -> None:
        if not args.progress:
            return
        Path(args.progress).write_text(json.dumps({
            "status": status, "done": done, "total": total,
            "rows": puller.rows_total, "calls": puller.calls,
            "fails": puller.fails[:50],
            "elapsed_s": round(time.monotonic() - t_start),
        }, ensure_ascii=False), encoding="utf-8")

    done = 0
    save_progress("running")
    for i, (y, m) in enumerate(ms):
        s, e = month_span(y, m)
        # 已完整拉过的月跳过(幂等重跑):该月 ann_date 行数存在且无截断嫌疑
        have = con.execute(
            "SELECT COUNT(*) FROM repurchase_events "
            "WHERE ann_date >= ? AND ann_date <= ?",
            (f"{s[:4]}-{s[4:]}-{s[6:]}", f"{e[:4]}-{e[4:]}-{e[6:]}"),
        ).fetchone()[0]
        if have >= ROW_CAP:
            done = i + 1
            continue  # 该月历史已回填(细分逻辑在首轮已处理)
        puller.pull_range(s, e)
        done = i + 1
        if done % 6 == 0:
            save_progress("running")
            print(f"[{done}/{total}] months rows={puller.rows_total} "
                  f"calls={puller.calls} fails={len(puller.fails)}",
                  flush=True)

    save_progress("done" if not puller.fails else "done_with_fails")
    if args.done_file:
        Path(args.done_file).write_text(json.dumps(
            {"status": "done" if not puller.fails else "done_with_fails",
             "rows": puller.rows_total, "calls": puller.calls,
             "fails": puller.fails}, ensure_ascii=False), encoding="utf-8")
    print(f"BACKFILL FINISHED rows={puller.rows_total} "
          f"calls={puller.calls} fails={len(puller.fails)}", flush=True)
    con.close()


if __name__ == "__main__":
    main()
