"""经营现金流/资本开支 历史报表回填(market.db 新表 cashflow_items)。

FcfY(自由现金流收益率)研究的底层数据:tushare `cashflow` 接口**逐票**拉全
历史(该 token 无 cashflow_vip 权限,逐票是唯一路径;单票一次调用即返回
2018 以来全部报告期,~5400 次调用 @45/min ≈ 2 小时)。

字段(只存研究需要的,中文口径):
  n_cashflow_act          经营活动产生的现金流量净额(累计值,报告期 YTD)
  c_pay_acq_const_fiolta  购建固定资产等支付的现金(capex,累计值)

PIT 关键:`ann_date`(公告日)必存 —— 研究层只允许用 ann_date ≤ 调仓日 的
报表,天然无前视。同一 (code,end_date) 可能有更正报告(不同 ann_date 或
update_flag),全部保留,主键含 ann_date+update_flag 防覆盖;研究层自行
按 ann_date 取最新可见版本。

幂等:INSERT OR REPLACE,可中断重跑(启动时跳过已完整拉取的 code)。
失败票落 JSON 清单供二次重跑。限速:默认 45 calls/min(留 5/min 余量给
其他消费方),分钟级超限按 patient 等待窗口重试。

用法(仓库根或 worktree 均可):
    /opt/data/quanti/.venv/bin/python scripts/backfill_cashflow.py \
        --market-db /opt/data/quanti/data/market.db --calls-per-min 45
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FIELDS = ("ts_code,ann_date,f_ann_date,end_date,update_flag,"
          "n_cashflow_act,c_pay_acq_const_fiolta")

DDL = """
CREATE TABLE IF NOT EXISTS cashflow_items (
    code TEXT NOT NULL,
    end_date TEXT NOT NULL,          -- 报告期 YYYY-MM-DD
    ann_date TEXT,                   -- 公告日 YYYY-MM-DD(PIT 关键)
    f_ann_date TEXT,                 -- 实际公告日
    update_flag TEXT,                -- 1=更正后
    n_cashflow_act REAL,             -- 经营现金流净额(累计)
    c_pay_acq_const_fiolta REAL,     -- 资本开支(累计)
    PRIMARY KEY (code, end_date, ann_date, update_flag)
)
"""

INSERT = """
INSERT OR REPLACE INTO cashflow_items
  (code, end_date, ann_date, f_ann_date, update_flag,
   n_cashflow_act, c_pay_acq_const_fiolta)
VALUES (?, ?, ?, ?, ?, ?, ?)
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
    """YYYYMMDD -> YYYY-MM-DD;None/空原样返回。"""
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


def fetch_one(pro, ts_code: str, start: str):
    """单票全历史;分钟级超限由调用方 patient 重试处理。"""
    df = pro.cashflow(ts_code=ts_code, start_date=start,
                      fields=FIELDS)
    if df is None or df.empty:
        return []
    out = []
    for row in df.to_dict("records"):
        out.append((
            ts_code.split(".")[0],
            _norm(row.get("end_date")),
            _norm(row.get("ann_date")),
            _norm(row.get("f_ann_date")),
            str(row.get("update_flag", "") or ""),
            _f(row.get("n_cashflow_act")),
            _f(row.get("c_pay_acq_const_fiolta")),
        ))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market-db",
                    default="/opt/data/quanti/data/market.db")
    ap.add_argument("--start", default="20180101",
                    help="报告期起点(YYYYMMDD)。TTM 需 2020 起可见报告,"
                         "2018 起点留足余量。")
    ap.add_argument("--calls-per-min", type=int, default=45)
    ap.add_argument("--limit", type=int, default=0,
                    help="只处理前 N 票(冒烟用);0=全量")
    ap.add_argument("--progress", default="",
                    help="进度 JSON 落盘路径(供监督方轮询)")
    ap.add_argument("--done-file", default="",
                    help="完成后写 status=done 的标记文件")
    args = ap.parse_args()

    import tushare as ts
    pro = ts.pro_api(resolve_token(args.market_db))

    con = sqlite3.connect(args.market_db)
    con.execute(DDL)
    # 宇宙:沪深(有行情的全部代码,含已退市——幸存者偏差修正),排除北交所
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT q.code FROM daily_quotes q "
        "JOIN stocks s ON s.code = q.code "
        "WHERE s.exchange IN ('SH','SZ') ORDER BY q.code").fetchall()]
    done_already = {r[0] for r in con.execute(
        "SELECT DISTINCT code FROM cashflow_items").fetchall()}
    todo = [c for c in codes if c not in done_already]
    if args.limit:
        todo = todo[:args.limit]

    interval = 60.0 / args.calls_per_min if args.calls_per_min else 0
    total = len(todo)
    fails: list[str] = []
    rows_total = 0
    t_start = time.monotonic()

    def save_progress(status: str) -> None:
        if not args.progress:
            return
        Path(args.progress).write_text(json.dumps({
            "status": status, "done": done, "total": total,
            "rows": rows_total, "fails": fails[:50],
            "elapsed_s": round(time.monotonic() - t_start),
            "eta_s": (round(time.monotonic() - t_start) / done
                      * (total - done)) if done else None,
        }, ensure_ascii=False), encoding="utf-8")

    done = 0
    save_progress("running")
    for i, code in enumerate(todo):
        t0 = time.monotonic()
        suffix = ".SH" if code.startswith("6") else ".SZ"
        # 分钟级超限:等 65s 重试,最多 5 次(共 ~5.4min)
        rows: list = []
        for attempt in range(5):
            try:
                rows = fetch_one(pro, code + suffix, args.start)
                break
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "频率超限" in msg and "分钟" in msg and attempt < 4:
                    time.sleep(65)
                    continue
                print(f"FAIL {code}: {msg[:120]}", flush=True)
                fails.append(code)
                break
        if rows:
            con.executemany(INSERT, rows)
            con.commit()
            rows_total += len(rows)
        done = i + 1
        if done % 100 == 0:
            save_progress("running")
            print(f"[{done}/{total}] rows={rows_total} "
                  f"fails={len(fails)} "
                  f"elapsed={time.monotonic() - t_start:.0f}s", flush=True)
        wait = interval - (time.monotonic() - t0)
        if wait > 0:
            time.sleep(wait)

    save_progress("done" if not fails else "done_with_fails")
    if args.done_file:
        Path(args.done_file).write_text(json.dumps(
            {"status": "done" if not fails else "done_with_fails",
             "fails": fails}, ensure_ascii=False), encoding="utf-8")
    print(f"BACKFILL FINISHED rows={rows_total} fails={len(fails)}",
          flush=True)
    con.close()


if __name__ == "__main__":
    main()
