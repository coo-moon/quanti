#!/usr/bin/env python3
"""因子重评分成本基准:rescored 全库(118 因子)冷/热缓存耗时对比。

2026-08-15 背景:stack dump 显示每日挖掘钩子在进程内跑 12+ 分钟,热点是
_ic_series 里每因子×每窗口×每代码重复的 daily_basic / financials SQLite 查询
(~47k 次)。修复:DataProvider 为这两张表加了与行情同款的 per-code 全量表缓存。

本脚本在 SCRATCH 副本上跑 rescore(不碰线上 paper.db 的因子库写入),
同一 provider 实例冷/热各跑一遍,报告耗时与比值,作为回归基准。

用法: python scripts/factor_rescore_bench.py --codes 100
"""
import argparse
import shutil
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _copy_db(src: Path) -> Path:
    dst = src.with_name(src.stem + "_bench_copy.db")
    shutil.copyfile(src, dst)
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", type=int, default=100)
    ap.add_argument("--paper", default="data/paper.db")
    ap.add_argument("--market", default="data/market.db")
    args = ap.parse_args()

    from quanti.agent.factor_miner import rescore_generated_factors
    from quanti.agent.universe import resolve_tradable_universe
    from quanti.data.database import Database
    from quanti.data.provider import DataProvider

    scratch = _copy_db(Path(args.paper))
    db = Database(str(scratch), market_db_path=args.market)
    db.initialize()
    provider = DataProvider(db)
    today = date.today()
    codes = resolve_tradable_universe(db, provider, pool=None, params={}, as_of=today)
    codes = codes[:args.codes]
    n_factors = len(db.list_generated_factors())
    print("codes=%d factors=%d (scratch db: %s)" % (len(codes), n_factors, scratch))

    # Count the SQLite reads the rescore issues per phase — deterministic,
    # unlike wall time on a shared machine.
    counts = {"basic": 0, "fin": 0}
    orig_basic = db.get_daily_basic
    orig_fin = db.get_financials

    def counting_basic(*a, **kw):
        counts["basic"] += 1
        return orig_basic(*a, **kw)

    def counting_fin(*a, **kw):
        counts["fin"] += 1
        return orig_fin(*a, **kw)
    db.get_daily_basic = counting_basic
    db.get_financials = counting_fin

    for label in ("cold", "warm"):
        t0 = time.monotonic()
        rescore_generated_factors(db, provider, codes, today)
        dt = time.monotonic() - t0
        print("%s: %.1fs  (daily_basic reads=%d, financials reads=%d)"
              % (label, dt, counts["basic"], counts["fin"]))
        counts["basic"] = counts["fin"] = 0
    db.close()
    scratch.unlink()
    print("done")


if __name__ == "__main__":
    main()

