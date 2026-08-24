"""sse_enhance 研究证据复现 — 十年指数代理 + 5 年个股向量化.

三个实验(docs/2026-08-24-sse-enhance.md 的数字来源):
  A. 股息垫十年恒正:沪深300 全收益(H00300.CSI) − 价格(000300.SH) 逐年差
  B. 风格倾斜证伪:红利/300/500 vs 上证滚动 250d 胜率(11 年)
  C. 5 年个股向量化:沪市全样本市值加权复制(剔新股),tilt 扫描

依赖 data/idx_*.csv(不存在则经 tushare 拉取,token 读 paper.db)。

用法: .venv/bin/python scripts/sse_enhance_research.py
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

IDX = {
    "000001.SH": "上证综指",
    "000300.SH": "沪深300",
    "H00300.CSI": "沪深300全收益",
    "000922.CSI": "中证红利",
    "H00922.CSI": "中证红利全收益",
    "000905.SH": "中证500",
}


def load_idx(code: str) -> pd.Series:
    """指数收盘序列(index=ISO 日期);缺失则 tushare 拉取落盘."""
    p = ROOT / "data" / f"idx_{code.replace('.', '_')}.csv"
    if not p.exists():
        import tushare as ts
        tok = sqlite3.connect(ROOT / "data" / "paper.db").execute(
            "select data_source_token from app_config").fetchone()[0]
        df = ts.pro_api(tok).index_daily(
            ts_code=code, start_date="20150801",
            end_date=date.today().strftime("%Y%m%d"))
        if df is None or df.empty:
            raise SystemExit(f"tushare index_daily {code} 拉取失败")
        df.to_csv(p, index=False)
    df = pd.read_csv(p, dtype={"trade_date": str}).sort_values("trade_date")
    iso = (df.trade_date.str[:4] + "-" + df.trade_date.str[4:6]
           + "-" + df.trade_date.str[6:])
    return pd.Series(df.close.values, index=iso)


def roll_stats(cand: pd.Series, bench: pd.Series, win: int) -> str:
    idx = cand.index.intersection(bench.index)
    rc = cand[idx] / cand[idx].shift(win) - 1
    rb = bench[idx] / bench[idx].shift(win) - 1
    ex = (rc - rb).dropna()
    return (f"n={len(ex)} 胜率={(ex > 0).mean():.1%} 最差={ex.min():+.1%} "
            f"p5={ex.quantile(0.05):+.1%}")


def experiment_a_b() -> None:
    sh = load_idx("000001.SH")
    pr300, tr300 = load_idx("000300.SH"), load_idx("H00300.CSI")

    def yearly(s: pd.Series) -> pd.Series:
        g = s.groupby(s.index.str[:4]).agg(["first", "last"])
        return g["last"] / g["first"] - 1

    gap = (yearly(tr300) - yearly(pr300)).round(4)
    print("=== A. 沪深300 全收益−价格 逐年股息垫(应恒正) ===")
    print(gap.to_string())
    assert (gap.iloc[1:] > 0).all(), "股息垫出现负年——数据或口径有问题"

    print("\n=== B. 风格候选 vs 上证 滚动250d(倾斜毁稳定) ===")
    for code in ("H00922.CSI", "H00300.CSI", "000905.SH"):
        print(f"{IDX[code]:12s} {roll_stats(load_idx(code), sh, 250)}")


def experiment_c() -> None:
    con = sqlite3.connect(ROOT / "data" / "market.db")
    px = pd.read_sql(
        """
        select q.code, q.date, q.close*q.adj_factor as aclose, b.total_mv
        from daily_quotes q join daily_basic b
          on q.code=b.code and q.date=b.date
        where q.code like '60%' or q.code like '68%'
        """, con)
    lst = pd.read_sql("select code, list_date from stocks", con).set_index("code")
    con.close()

    aclose = px.pivot(index="date", columns="code", values="aclose").sort_index()
    mv = px.pivot(index="date", columns="code", values="total_mv").sort_index()
    ret = aclose.pct_change(fill_method=None)
    dates = ret.index.tolist()

    ld = pd.to_datetime(lst["list_date"].reindex(mv.columns), errors="coerce")
    dt = pd.to_datetime(pd.Series(dates, index=dates))
    eligible = pd.DataFrame({c: (dt - ld[c]).dt.days >= 365 for c in mv.columns})
    eligible.index = dates
    w = mv.where(eligible).shift(1)
    w = w.div(w.sum(axis=1), axis=0)
    port = (w * ret).sum(axis=1, min_count=1).dropna()

    sh = load_idx("000001.SH").pct_change()
    idx = port.index.intersection(sh.dropna().index)
    ex = port[idx] - sh[idx]
    cum_p = (1 + port[idx]).cumprod()
    cum_b = (1 + sh[idx]).cumprod()
    n = len(idx)
    ann_ex = (cum_p.iloc[-1] / cum_b.iloc[-1]) ** (252 / n) - 1
    te = ex.std() * np.sqrt(252)
    print("\n=== C. 5年个股向量化: 全样本市值加权复制(剔新股) ===")
    print(f"年超额={ann_ex:+.2%} TE={te:.2%} IR={ann_ex / te:.2f}")
    for win, lab in ((250, "滚1y"), (750, "滚3y")):
        print(f"{lab}: {roll_stats(cum_p, cum_b, win)}")


if __name__ == "__main__":
    experiment_a_b()
    experiment_c()
