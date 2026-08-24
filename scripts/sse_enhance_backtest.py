"""sse_enhance 全链路引擎回测 — vs 上证综指滚动窗口验证.

跑 BacktestEngine(真实成本/整手/T+1/涨跌停),沪市全样本池,
对比上证综指(000001.SH,价格指数)的滚动 250 日窗口超额分布。

用法:
    .venv/bin/python scripts/sse_enhance_backtest.py [--cash 5000000]
        [--start 2021-07-01] [--end 2026-08-24] [--min-weight 0]

上证指数日线自动经 tushare 拉取(token 读 paper.db app_config),
落盘 data/idx_000001_SH.csv 后复用。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

IDX_CSV = ROOT / "data" / "idx_000001_SH.csv"


def load_sse_index() -> pd.Series:
    """上证综指收盘序列(index=ISO 日期字符串)。缺失则经 tushare 拉取落盘。"""
    if not IDX_CSV.exists():
        import tushare as ts
        tok = sqlite3.connect(ROOT / "data" / "paper.db").execute(
            "select data_source_token from app_config").fetchone()[0]
        df = ts.pro_api(tok).index_daily(
            ts_code="000001.SH", start_date="20150801",
            end_date=date.today().strftime("%Y%m%d"))
        if df is None or df.empty:
            raise SystemExit("tushare index_daily 000001.SH 拉取失败")
        df.to_csv(IDX_CSV, index=False)
    df = pd.read_csv(IDX_CSV, dtype={"trade_date": str}).sort_values("trade_date")
    idx = (df.trade_date.str[:4] + "-" + df.trade_date.str[4:6]
           + "-" + df.trade_date.str[6:])
    return pd.Series(df.close.values, index=idx)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cash", type=float, default=5_000_000)
    ap.add_argument("--start", default="2021-07-01")
    ap.add_argument("--end", default="2026-08-24")
    ap.add_argument("--min-weight", type=float, default=0.0)
    args = ap.parse_args()

    from quanti.backtest.engine import BacktestEngine
    from quanti.data.database import Database
    from quanti.data.provider import DataProvider
    from quanti.models import Direction

    sys.path.insert(0, str(ROOT / "strategies"))
    from sse_index_enhance import SSEIndexEnhanceStrategy

    db = Database(str(ROOT / "data" / "paper.db"),
                  market_db_path=str(ROOT / "data" / "market.db"))
    db.initialize()

    mcon = sqlite3.connect(ROOT / "data" / "market.db")
    codes = [r[0] for r in mcon.execute(
        "select distinct code from daily_quotes"
        " where code like '60%' or code like '68%'")]
    mcon.close()
    print(f"池: 沪市 {len(codes)} 只(含已退市,无幸存者偏差)")

    strategy = SSEIndexEnhanceStrategy()
    strategy.init({"market_db_path": str(ROOT / "data" / "market.db"),
                   "min_weight": args.min_weight})

    # risk_manager=None: 个股止损/移动止盈会卖掉成分不回补,破坏复制;
    # 全样本分散本身即风控。成本/整手/T+1/涨跌停照常生效。
    engine = BacktestEngine(provider=DataProvider(db),
                            initial_cash=args.cash, risk_manager=None)
    result = engine.run(strategy=strategy, codes=codes,
                        start=date.fromisoformat(args.start),
                        end=date.fromisoformat(args.end))

    eq = result.equity_curve
    eq.index = [d.isoformat() for d in eq.index]
    sse = load_sse_index()
    common = eq.index.intersection(sse.index)
    eq, sse = eq[common], sse[common]
    n = len(common)
    pr, br = eq.pct_change(), sse.pct_change()
    ex_d = (pr - br).dropna()
    cum_p, cum_b = eq / eq.iloc[0], sse / sse.iloc[0]
    ann_p = cum_p.iloc[-1] ** (252 / n) - 1
    ann_b = cum_b.iloc[-1] ** (252 / n) - 1
    ann_ex = (cum_p.iloc[-1] / cum_b.iloc[-1]) ** (252 / n) - 1
    te = ex_d.std() * np.sqrt(252)

    def roll(win: int) -> dict:
        rp = cum_p / cum_p.shift(win) - 1
        rb = cum_b / cum_b.shift(win) - 1
        rex = (rp - rb).dropna()
        if not len(rex):
            return {}
        return {"n": int(len(rex)), "win_rate": round(float((rex > 0).mean()), 4),
                "worst": round(float(rex.min()), 4),
                "p5": round(float(rex.quantile(0.05)), 4)}

    commission = sum(t.commission for t in result.trades)
    bought = {t.stock_code for t in result.trades
              if t.direction == Direction.BUY}
    report = {
        "period": [args.start, args.end], "cash": args.cash,
        "min_weight": args.min_weight,
        "codes": len(codes), "trades": len(result.trades),
        "distinct_buys": len(bought),
        "commission_total": round(commission, 2),
        "ann_return": round(float(ann_p), 4),
        "ann_benchmark": round(float(ann_b), 4),
        "ann_excess": round(float(ann_ex), 4),
        "tracking_error": round(float(te), 4),
        "rolling_250d": roll(250), "rolling_750d": roll(750),
        "halted": result.halted,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    tag = f"{int(args.cash / 1_000_000)}m"
    if args.min_weight:
        tag += f"_mw{args.min_weight:g}"
    out = ROOT / "data" / f"sse_enhance_bt_report_{tag}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"saved -> {out}")
    db.close()


if __name__ == "__main__":
    main()
