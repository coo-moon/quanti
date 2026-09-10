"""dividend_tr_enhance 全链路引擎回测 — vs 中证红利全收益(H00922.CSI)+ 沪深300。

跑 BacktestEngine(真实成本/整手/T+1/涨跌停/红利税),股票池 = 策略自己的
PIT 池(主板 ∪ 中证红利成分中过去 12 个月实施过现金分红的股票)。

三个口径必须分清(混了就得出错误的"超额"):
  - 000922.CSI  中证红利**价格**指数 —— 恒等式的对照物:策略吃到的股息垫
    就是它和全收益之间的差(2021-09~2026-09 实测 +1.1%/年 vs +6.2%/年);
  - H00922.CSI  中证红利**全收益** —— "完美复制 + 免税"上界:A 股规则下
    长期持有(>1 年)本就免红利税,所以这个上界不是不可及的神话,而是同一
    个数学再减去成本/税/跟踪误差;
  - 000300.SH   沪深300 价格指数 —— 旁证,不是验收基准。

验收闸(每个资金档都算):
  DSR ≥ 0.95、PBO ≤ 0.2、对 H00922 的滚动 1 年胜率 ≥ 90%
  (滚动窗口口径与 scripts/sse_enhance_backtest.py 完全一致:250 交易日
   累计收益之差,>0 记胜)。

用法:
    /opt/data/quanti/.venv/bin/python scripts/dividend_tr_backtest.py \
        --market-db /opt/data/quanti/data/market.db \
        --config-db /opt/data/quanti/data/paper.db \
        --start 2021-10-01 --end 2026-09-11

market.db 一律**只读**打开(strategy / 引擎都不写)。报告 JSON 落 data/。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_MARKET_DB = "/opt/data/quanti/data/market.db"
DEFAULT_CONFIG_DB = "/opt/data/quanti/data/paper.db"
INDEX_CODES = ("H00922.CSI", "000922.CSI", "000300.SH")


def resolve_token(config_db: str) -> str | None:
    import os
    if os.environ.get("TUSHARE_TOKEN"):
        return os.environ["TUSHARE_TOKEN"]
    try:
        con = sqlite3.connect(f"file:{config_db}?mode=ro", uri=True)
        try:
            row = con.execute(
                "select data_source_token from app_config").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def load_index(code: str, end: date, config_db: str) -> pd.Series:
    """指数收盘序列(index=ISO 日期)。本地 CSV 缓存落后于回测终点时增量拉取。

    指数只用于对账,不进策略(PIT 数据全在 market.db)。
    """
    p = ROOT / "data" / f"idx_{code.replace('.', '_')}.csv"
    if p.exists():
        # 读入时**保持文件原行序**(缓存文件是 tushare 的倒序快照),只在返回
        # 时排序 —— 这样增量刷新只追加新行,不会把整个文件写成一次大 diff。
        df = pd.read_csv(p, dtype={"trade_date": str})
    else:
        df = pd.DataFrame(columns=["trade_date", "close"])
    last = df.trade_date.max() if len(df) else "0"
    if last < end.strftime("%Y%m%d"):
        tok = resolve_token(config_db)
        if tok:
            import tushare as ts
            start = (date.fromisoformat(f"{last[:4]}-{last[4:6]}-{last[6:8]}")
                     + timedelta(days=1) if len(last) == 8 else date(2015, 1, 1))
            fresh = ts.pro_api(tok).index_daily(
                ts_code=code, start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"))
            if fresh is not None and not fresh.empty:
                # 追加而不是重排:本地 CSV 是 tushare 的**倒序**快照,重排会让
                # 整个文件显示成 diff;保持原行序,只补新行。
                df = pd.concat([df, fresh], ignore_index=True)
                df = df.drop_duplicates("trade_date", keep="first")
                df.to_csv(p, index=False)
    df = df.sort_values("trade_date")
    iso = (df.trade_date.str[:4] + "-" + df.trade_date.str[4:6] + "-"
           + df.trade_date.str[6:])
    return pd.Series(df.close.astype(float).values, index=iso)


def build_dividend_lookup(market_db: str, start: date, end: date
                          ) -> tuple[dict[date, dict[str, float]], float]:
    """{ex_date: {code: 每股税前分红}} —— 引擎红利税账本的输入。

    同一除息日的不同报告期(中期+年度同日除息)求和;同一笔分红的多行
    (预案/股东大会/实施)在 SQL 层已按 (code, end_date, ex_date) 去重,
    否则会把一笔分红数两遍。
    """
    con = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "with d as ("
            "  select code, ex_date, end_date, max(cash_div_tax) cash"
            "  from dividends"
            "  where div_proc='实施' and cash_div_tax > 0"
            "    and ex_date is not null and ex_date != ''"
            "    and ex_date >= ? and ex_date <= ?"
            "  group by code, ex_date, end_date)"
            " select ex_date, code, sum(cash) from d group by ex_date, code",
            ((start - timedelta(days=10)).isoformat(),
             (end + timedelta(days=10)).isoformat())).fetchall()
    finally:
        con.close()
    lookup: dict[date, dict[str, float]] = {}
    total = 0.0
    for ex_date, code, cash in rows:
        lookup.setdefault(date.fromisoformat(ex_date), {})[code] = float(cash)
        total += float(cash)
    return lookup, total


def build_universe(market_db: str, start: date, end: date,
                   index_code: str, pool_mode: str = "union") -> list[str]:
    """引擎需要加载的代码 = 策略**可能持有**的股票(PIT 池的超集)。

    这不是回测前视:池子本身在每个调仓日由策略重新筛,这里只是把"永远进不了
    池"的股票(没分过红 / 非主板非成分)排除在数据加载之外,否则整市场加载
    既慢又爆内存。
    """
    con = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
    try:
        idx = {r[0] for r in con.execute(
            "select distinct code from index_weights where index_code=?",
            (index_code,))}
        payers = {r[0] for r in con.execute(
            "select distinct code from dividends"
            " where div_proc='实施' and cash_div_tax > 0"
            "   and ex_date >= ? and ex_date <= ?",
            ((start - timedelta(days=400)).isoformat(),
             end.isoformat()))}
        traded = {r[0] for r in con.execute(
            "select distinct code from daily_quotes where date >= ? and date <= ?",
            (start.isoformat(), end.isoformat()))}
    finally:
        con.close()
    main_board = {c for c in traded
                  if c.startswith(("600", "601", "603", "605", "000", "001",
                                   "002", "003"))}
    if pool_mode == "index_only":
        return sorted(idx & payers)
    return sorted((main_board | idx) & payers)


def run_cell(*, market_db: str, cash: float, min_weight: float,
             start: date, end: date, codes: list[str],
             pool_mode: str = "union") -> dict:
    """跑一个 (资金, 截断阈值) 配置,返回该配置的日频收益与全部指标。"""
    from quanti.data.database import Database
    from quanti.data.provider import DataProvider
    from quanti.backtest.engine import BacktestEngine

    sys.path.insert(0, str(ROOT / "strategies"))
    from dividend_tr_enhance import DividendTREnhanceStrategy

    lookup, _ = build_dividend_lookup(market_db, start, end)
    # market.db 只读:主库用临时 scratch(SQLite 需要一个主库挂载 market schema)。
    scratch = Path(tempfile.mkdtemp(prefix="divtr_bt_")) / "scratch.db"
    db = Database(str(scratch), market_db_path=market_db)
    db.initialize()
    strategy = DividendTREnhanceStrategy()
    strategy.init({"market_db_path": market_db, "min_weight": min_weight,
                   "pool_mode": pool_mode,
                   "snapshot_start": (start - timedelta(days=45)).isoformat(),
                   "snapshot_end": end.isoformat()})
    # risk_manager=None:个股止损/组合熔断会中途清仓且停止策略交易,破坏
    # "复制+再投"的恒等式(与 sse_enhance 同口径)。红利税的熔断路径由
    # tests/test_dividend_tax.py 的单测覆盖。
    engine = BacktestEngine(provider=DataProvider(db), initial_cash=cash,
                            risk_manager=None, dividend_lookup=lookup)
    result = engine.run(strategy=strategy, codes=codes, start=start, end=end)
    eq = result.equity_curve.copy()  # 副本:下面改索引不污染引擎结果
    db.close()
    eq.index = [d.isoformat() for d in eq.index]
    # 仓位暴露(整手约束的直接后果:小资金买不起长尾 → 现金滞留)
    invested = _invested_ratio(result)
    bought = {t.stock_code for t in result.trades if t.direction.name == "BUY"}
    turnovers = [t.price * t.quantity for t in result.trades]
    months = max(len(eq) / 21.0, 1e-9)
    return {
        "cash": cash, "min_weight": min_weight, "pool_mode": pool_mode,
        "equity": eq,
        "pool_stats": getattr(strategy, "pool_stats", {}),
        "avg_invested": invested["avg"],
        "min_invested": invested["min"],
        "ann_return": float(_annualized(eq)),
        "max_drawdown": float((eq / eq.cummax() - 1).min()),
        "trades": len(result.trades), "distinct_buys": len(bought),
        "commission_total": float(sum(t.commission for t in result.trades)),
        "dividend_tax_total": float(result.dividend_tax_total),
        "dividend_cash_total": float(result.dividend_cash_total),
        "turnover_per_month": float(
            (sum(turnovers) / 2) / eq.mean() / months),
        "halted": bool(result.halted),
    }


def _annualized(eq: pd.Series) -> float:
    if len(eq) < 2 or eq.iloc[0] <= 0:
        return 0.0
    return (eq.iloc[-1] / eq.iloc[0]) ** (252 / len(eq)) - 1


def _invested_ratio(result) -> dict:
    """仓位暴露:1 − 现金/总资产(逐日,来自持仓市值+现金的对账)。

    整手约束下小资金买不起长尾 → 现金滞留,这个数字解释"为什么年化不上去"。
    无法从 equity_curve 反推,所以这里用引擎的 trades 重建每日现金:买入扣
    (成交额+费用+红利税),卖出加(成交额−费用−红利税)。
    """
    eq = result.equity_curve
    if not len(eq):
        return {"avg": 0.0, "min": 0.0}
    cash = float(eq.iloc[0])
    by_date: dict = {}
    for t in result.trades:
        by_date.setdefault(t.date.isoformat(), []).append(t)
    ratios = []
    for d, v in eq.items():
        for t in by_date.get(d.isoformat(), []):
            notional = t.price * t.quantity
            if t.direction.name == "BUY":
                cash -= notional + t.commission
            else:
                cash += notional - t.commission - t.dividend_tax
        ratios.append(1.0 - cash / v if v > 0 else 0.0)
    return {"avg": round(float(np.mean(ratios)), 4),
            "min": round(float(np.min(ratios)), 4)}


def _roll(cum_p: pd.Series, cum_b: pd.Series, win: int) -> dict:
    """滚动 win 日窗口的相对强弱(与 sse_enhance_backtest.py 同口径)。"""
    rp = cum_p / cum_p.shift(win) - 1
    rb = cum_b / cum_b.shift(win) - 1
    rex = (rp - rb).dropna()
    if not len(rex):
        return {"n": 0}
    return {"n": int(len(rex)),
            "win_rate": round(float((rex > 0).mean()), 4),
            "worst": round(float(rex.min()), 4),
            "p5": round(float(rex.quantile(0.05)), 4)}


def evaluate(cell: dict, bench: pd.Series) -> dict:
    """单个配置 vs 基准:年化超额 / 滚动 1 年胜率 / TE / 回撤。"""
    eq = cell["equity"]
    common = eq.index.intersection(bench.index)
    eq, b = eq[common], bench[common]
    n = len(common)
    if n < 2:
        return {}
    cum_p, cum_b = eq / eq.iloc[0], b / b.iloc[0]
    pr, br = eq.pct_change().dropna(), b.pct_change().dropna()
    ex_d = (pr - br).dropna()
    return {
        "n_days": int(n),
        "ann_return": round(float(_annualized(eq)), 4),
        "ann_benchmark": round(float((cum_b.iloc[-1]) ** (252 / n) - 1), 4),
        "ann_excess": round(float((cum_p.iloc[-1] / cum_b.iloc[-1])
                                  ** (252 / n) - 1), 4),
        "tracking_error": round(float(ex_d.std() * np.sqrt(252)), 4),
        "excess_sharpe": round(float(ex_d.mean() / ex_d.std()
                                     * np.sqrt(252)) if ex_d.std() > 0 else 0.0, 4),
        "max_drawdown": round(float((eq / eq.cummax() - 1).min()), 4),
        "rolling_250d": _roll(cum_p, cum_b, 250),
        "rolling_750d": _roll(cum_p, cum_b, 750),
        "excess_returns": ex_d,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market-db", default=DEFAULT_MARKET_DB)
    ap.add_argument("--config-db", default=DEFAULT_CONFIG_DB)
    ap.add_argument("--start", default="2021-10-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--cash-list", default="200000,500000,1000000")
    ap.add_argument("--tags", default="", help="只跑指定资金档(逗号分隔)")
    # 配置网格 "<池子模式>:<min_weight 列表>;..."。union = 任务书口径;
    # index_only = 诊断对照(只持中证红利成分自身),用来分离"篮子不同"与
    # "分红再投机制"对超额的贡献,不作出货配置。min_weight 是执行约束的
    # 暴露面(整手买不起的尾巴),不是选股/择时参数。
    ap.add_argument("--grid", default="union:0,0.002;index_only:0.002")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    cashes = [float(x) for x in args.cash_list.split(",") if x]
    if args.tags:
        want = {float(x) for x in args.tags.split(",")}
        cashes = [c for c in cashes if c in want]
    grid: list[tuple[str, list[float]]] = []
    for part in args.grid.split(";"):
        part = part.strip()
        if not part:
            continue
        mode, _, ws = part.partition(":")
        grid.append((mode or "union",
                     [float(x) for x in ws.split(",") if x.strip()]))

    bench_tr = load_index("H00922.CSI", end, args.config_db)
    bench_px = load_index("000922.CSI", end, args.config_db)
    bench_hs = load_index("000300.SH", end, args.config_db)
    print(f"基准 {len(bench_tr)} 天 [{bench_tr.index[0]} ~ {bench_tr.index[-1]}]")

    universes = {m: build_universe(args.market_db, start, end, "000922.CSI",
                                   pool_mode=m)
                 for m, _ in grid}
    for m, codes in universes.items():
        print(f"引擎加载池[{m}]: {len(codes)} 只")

    cells = []
    for mode, weights in grid:
        codes = universes[mode]
        for cash in cashes:
            for mw in weights:
                cell = run_cell(market_db=args.market_db, cash=cash,
                                min_weight=mw, start=start, end=end,
                                codes=codes, pool_mode=mode)
                ev_tr = evaluate(cell, bench_tr)
                ev_px = evaluate(cell, bench_px)
                ev_hs = evaluate(cell, bench_hs)
                excess = ev_tr.pop("excess_returns")
                ev_px.pop("excess_returns")
                ev_hs.pop("excess_returns")
                tag = f"{mode}_{int(cash / 10000)}w_mw{mw:g}"
                cells.append({
                    "tag": tag, "pool_mode": mode, "cash": cash,
                    "min_weight": mw,
                    "engine": {k: v for k, v in cell.items() if k != "equity"},
                    "vs_H00922_TR": ev_tr, "vs_000922_price": ev_px,
                    "vs_000300": ev_hs,
                    "_excess": excess,
                })
                print(f"[{tag}] 年化 {ev_tr['ann_return']:+.2%} vs TR "
                      f"{ev_tr['ann_benchmark']:+.2%} 超额 "
                      f"{ev_tr['ann_excess']:+.2%} 滚1年胜率(H00922) "
                      f"{ev_tr['rolling_250d'].get('win_rate', 0):.1%} "
                      f"滚1年胜率(价格) "
                      f"{ev_px['rolling_250d'].get('win_rate', 0):.1%} "
                      f"回撤 {cell['max_drawdown']:.1%} 换手/月 "
                      f"{cell['turnover_per_month']:.1%} 红利税 "
                      f"{cell['dividend_tax_total']:,.0f} 元", flush=True)

    # --- 严谨度闸:DSR(扣多重检验)+ PBO(配置间挑优是否只是噪声) ---
    from quanti.backtest.overfit import deflated_sharpe_from_stats, pbo_cscv, \
        sharpe_per_obs
    trial_sharpes = [sharpe_per_obs(c["_excess"]) for c in cells]
    matrix = pd.concat([c["_excess"] for c in cells], axis=1).dropna()
    pbo = pbo_cscv(matrix.to_numpy(), n_splits=16) if matrix.shape[1] >= 2 else {}
    for c in cells:
        ex = c.pop("_excess")
        sr = sharpe_per_obs(ex)
        z = (ex - ex.mean()) / ex.std(ddof=1) if ex.std(ddof=1) > 0 else ex * 0
        dsr = deflated_sharpe_from_stats(
            sr, len(ex), trial_sharpes,
            skew=float((z ** 3).mean()), kurt=float((z ** 4).mean()))
        c["robustness"] = {
            "dsr": round(float(dsr["dsr"]), 4),
            "sr_per_obs": round(float(sr), 5),
            "sr0_benchmark": round(float(dsr["sr0_benchmark"]), 5),
            "n_trials": dsr["n_trials"],
            "pbo": round(float(pbo.get("pbo", float("nan"))), 4),
            "pbo_configs": pbo.get("n_configs"),
        }
        wr = c["vs_H00922_TR"].get("rolling_250d", {}).get("win_rate", 0.0)
        c["gate"] = {
            "dsr_ge_0.95": bool(c["robustness"]["dsr"] >= 0.95),
            "pbo_le_0.2": bool(c["robustness"]["pbo"] <= 0.2),
            "rolling_1y_win_ge_0.90": bool(wr >= 0.90),
        }
        c["gate"]["pass"] = all(c["gate"].values())

    report = {
        "period": [start.isoformat(), end.isoformat()],
        "benchmark": {"H00922.CSI 中证红利全收益": "上界(完美复制+免税)",
                      "000922.CSI 中证红利价格": "恒等式对照",
                      "000300.SH 沪深300": "旁证"},
        "universe_codes": {m: len(c) for m, c in universes.items()},
        "cells": cells,
        "gates_passed": [c["tag"] for c in cells if c["gate"]["pass"]],
        "gates_failed": [c["tag"] for c in cells if not c["gate"]["pass"]],
    }
    out = Path(args.out) if args.out else (
        ROOT / "data" / "dividend_tr_bt_report.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                              default=str))
    print(f"\n通过验收闸的配置: {report['gates_passed'] or '无'}")
    print(f"未通过: {report['gates_failed'] or '无'}")
    print(f"saved -> {out}")


if __name__ == "__main__":
    main()
