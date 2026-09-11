"""分红 / 中证红利成分 / 曾用名 参考数据回填(market.db)。

dividend_tr_enhance 的三张参考表(见 quanti/data/database.py)都从这里落库:
  - dividends      tushare `dividend` 按**日全市场批量**(逐票拉在低积分
                   token 下是 6000+ 次调用/年,不可行);`--by ex_date` 只
                   覆盖已实施行,回填更快;
  - index_weights  tushare `index_weight` 月末成分快照(000922.CSI);
  - name_history   tushare `namechange` 曾用名史 —— PIT 判 ST 用,绝不用
                   今天的名字回判历史(那是前视)。

幂等:全部 INSERT OR REPLACE,可中断重跑。写完不需要清缓存。

用法(仓库根):
    /opt/data/quanti/.venv/bin/python scripts/backfill_dividends.py \
        --market-db /opt/data/quanti/data/market.db \
        --config-db /opt/data/quanti/data/paper.db \
        --start 2020-01-01 --by ex_date --calls-per-min 45

token:优先 $TUSHARE_TOKEN,否则读 --config-db 的 app_config.data_source_token。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def resolve_token(config_db: str) -> str:
    import os
    tok = os.environ.get("TUSHARE_TOKEN")
    if tok:
        return tok
    con = sqlite3.connect(f"file:{config_db}?mode=ro", uri=True)
    try:
        row = con.execute(
            "select data_source_token from app_config").fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        raise SystemExit("TUSHARE_TOKEN 未设置,且 app_config 里没有 token")
    return row[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market-db", default="/opt/data/quanti/data/market.db")
    ap.add_argument("--config-db", default="/opt/data/quanti/data/paper.db")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--by", default="ex_date", choices=("ann_date", "ex_date"),
                    help="分红拉取键:ann_date=全部公告行;ex_date=仅已实施行")
    ap.add_argument("--calls-per-min", type=int, default=45)
    ap.add_argument("--names-start", default="2015-01-01",
                    help="曾用名史起点(需早于回测窗,ST 判定才不留盲区)")
    ap.add_argument("--index-code", default="000922.CSI")
    ap.add_argument("--skip-dividends", action="store_true")
    ap.add_argument("--skip-index", action="store_true")
    ap.add_argument("--skip-names", action="store_true")
    args = ap.parse_args()

    from quanti.data.database import Database
    from quanti.data.tushare_adapter import TushareAdapter

    db = Database(args.config_db, market_db_path=args.market_db)
    db.initialize()
    adapter = TushareAdapter(db, token=resolve_token(args.config_db))
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    names_start = date.fromisoformat(args.names_start)

    if not args.skip_index:
        n = adapter.sync_index_weights(args.index_code, start, end)
        print(f"index_weights {args.index_code}: {n} 行")
    if not args.skip_names:
        n = adapter.sync_name_history(names_start, end)
        print(f"name_history: {n} 行")
    if not args.skip_dividends:
        total_days = (end - start).days + 1

        def progress(d: date, rows: int) -> None:
            done = (d - start).days + 1
            if done % 50 == 0 or done == total_days:
                print(f"dividends {d} ({done}/{total_days} 天, 累计 {rows} 行)",
                      flush=True)

        n = adapter.sync_dividends(start, end, by=args.by,
                                   calls_per_min=args.calls_per_min,
                                   on_progress=progress)
        print(f"dividends({args.by}): {n} 行")
    db.close()


if __name__ == "__main__":
    main()
