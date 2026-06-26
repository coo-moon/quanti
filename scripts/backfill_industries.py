"""Backfill stocks.industry from tushare `pro.stock_basic` (the configured source).

Why this exists: every row in `stocks` had an empty `industry`, so the factor
cross-sectional pipeline's `_industry_demean` was a silent no-op — every stock
passed through unchanged and `FactorConfig.industry_neutralize=True` did nothing.

Source choice: the system's configured data source is tushare, and its
`pro.stock_basic` carries an `industry` field (~99.8% non-empty for listed
names) in the same taxonomy the roster sync now writes (see
TushareAdapter.sync_stock_list). Backfilling from the same source keeps one
consistent industry scheme across existing + future rows. Token is read from
app_config / TUSHARE_TOKEN via quanti.data.source — never printed.

This only touches the `industry` column (UPDATE), leaving name/list_date/exchange
to the roster sync. Per the project's fail-loud rule we ABORT if tushare is
unavailable or returns no industries rather than leave industry blank.

Usage:
    python scripts/backfill_industries.py            # writes data/market.db
    python scripts/backfill_industries.py --dry-run  # fetch + report, no write
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time

import tushare as ts

from quanti.data.database import Database
from quanti.data import source

MAX_RETRIES = 3
RETRY_DELAY = 2.0


def _ts_code_to_code(ts_code: str) -> str:
    """'000001.SZ' -> '000001' (drop exchange suffix to match stocks.code)."""
    return ts_code.split(".")[0].strip()


def fetch_tushare_map(db) -> dict[str, str]:
    """Build {6-digit code -> industry} from stock_basic over L/D/P rosters.

    Fail-loud: no token, or an empty result for every status, raises — a blank
    map would silently leave industry empty, i.e. the bug we're fixing.
    """
    tok = source.tushare_token(db)
    if not tok:
        raise RuntimeError(
            "未找到 tushare token(app_config.data_source_token / TUSHARE_TOKEN)"
            " — 行业源不可用,拒绝静默降级。请配置 token 后重试。")
    pro = ts.pro_api(tok)

    code2ind: dict[str, str] = {}
    for status in ("L", "D", "P"):
        df = None
        last = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                df = pro.stock_basic(list_status=status,
                                     fields="ts_code,industry")
                break
            except Exception as e:  # noqa: BLE001 - upstream/rate-limit transient
                last = e
                print(f"  [{attempt}/{MAX_RETRIES}] stock_basic({status}) 失败: {e}",
                      file=sys.stderr)
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
        else:
            raise RuntimeError(f"stock_basic({status}) 重试仍失败: {last}")
        if df is None or df.empty:
            continue
        n = 0
        for _, r in df.iterrows():
            ind = str(r.get("industry") or "").strip()
            if ind:
                code2ind[_ts_code_to_code(str(r["ts_code"]))] = ind
                n += 1
        print(f"  status={status}: {len(df)} 只,带行业 {n} 只")

    if not code2ind:
        raise RuntimeError("tushare stock_basic 未返回任何行业 — 拒绝静默降级")
    return code2ind


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/market.db", help="market DB 路径")
    ap.add_argument("--token-db", default="data/paper.db",
                    help="读取 tushare token 的账户库(app_config)")
    ap.add_argument("--dry-run", action="store_true", help="只抓取+报告,不写库")
    args = ap.parse_args()

    print("抓取 tushare 行业映射 ...")
    token_db = Database(args.token_db); token_db.initialize()
    try:
        code2ind = fetch_tushare_map(token_db)
    finally:
        token_db.close()
    print(f"映射就绪: {len(code2ind)} 只票 / {len(set(code2ind.values()))} 个行业")

    conn = sqlite3.connect(args.db)
    try:
        db_codes = [r[0] for r in conn.execute("SELECT code FROM stocks")]
        rows = [(code2ind[c], c) for c in db_codes if c in code2ind]
        miss = len(db_codes) - len(rows)
        print(f"DB {len(db_codes)} 只,命中 {len(rows)},未命中 {miss} "
              f"(退市/未分类 → 留空,demean 自动跳过这些票)")
        if args.dry_run:
            print("dry-run:不写库")
            return
        conn.executemany("UPDATE stocks SET industry=? WHERE code=?", rows)
        conn.commit()
        n = conn.execute(
            "SELECT count(*) FROM stocks WHERE industry IS NOT NULL "
            "AND industry != ''").fetchone()[0]
        print(f"已写入。stocks.industry 非空: {n} 只")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
