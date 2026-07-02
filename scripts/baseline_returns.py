#!/usr/bin/env python3
"""诚实基准 —— 把"被动等权 5y ≈ +11.78%"从 memory 里的孤零数字变成可复现脚本。

任何"系统能赚钱"的论断都要先赢过这些**可交易、含退市股、计成本**的被动标尺:
  - 全市场等权(当日在交易的全部 A 股)
  - ADV 前 1000 / 300 / 100 等权(流动性收窄的池)
两种持有方式:
  - monthly = 月度调仓等权(池内新陈代谢的换手计 0.3%/换手 成本)→ 有净值路径,给 年化/夏普/回撤
  - hold    = 期初选池、一直持有到期末(退市股按最后可得 bar 结算)→ "什么都不做"的参照

两种加权口径(2026-07-02 ADV 池宽度对抗验证的落地):
  - equal = 等权。纸面最高(全市场 +11.5%/yr、夏普0.59),但宽池的超额 = 等权小盘 size beta:
    硬等权容量中位仅 ~2.7亿(池内最小名 ADV 中位 49万/天),铺不进真钱,且 regime 翻转
    (2024-01 微盘踩踏、2026H1 均为窄池最优)。
  - cap   = 市值加权(daily_basic.total_mv,调仓日归一)。可交易口径:市值加权后
    ADV100/ADV1000 均转正 ~+4%/yr(5y 全窗 +3.7%/+4.2%),池宽梯度塌缩 →
    "池越宽越健康"是加权口径幻觉,不是流动性质量。
公平标尺 = **cap 口径的 ADV1000**(容量 ~264亿),不是全市场等权的纸面数。

幸存者偏差:按**调仓日当时在交易**取池(daily_quotes 在 [rd-40,rd] 有价的 code),
退市股的历史 bar 已在库中(quanti 补过 359 只退市股),period_returns 用最后可得 bar,
故不是"只看活到今天的赢家"。无 point-in-time 指数成分,故不含指数基准(需另拉数据)。

用法:.venv/bin/python scripts/baseline_returns.py [--start 2021-08-01 --end 2026-06-24]
临时账户库 data/agent_bt.db;行情 data/market.db。
"""
import sys
import sqlite3
import argparse
from collections import defaultdict
from datetime import date, timedelta

import numpy as np

sys.path.insert(0, ".")
from quanti.data.database import Database
from quanti.data.provider import DataProvider

ACCOUNT_DB = "data/agent_bt.db"
MARKET_DB = "data/market.db"
COST_PER_TURN = 0.003  # 计法 r*(1-turn*cost) 与 factor_backtest / breadth_regime 一致


def monthly_rebal_dates(provider, start, end):
    by_m = defaultdict(list)
    for d in provider.get_trade_dates(start, end):
        by_m[(d.year, d.month)].append(d)
    return sorted(max(ds) for ds in by_m.values())


def adv_ranked(provider, rd, n=None):
    """当日在交易的 code,按 ADV20 降序。n=None 返回全市场。"""
    adv = provider.get_adv20_map(rd - timedelta(days=40), rd)
    ranked = sorted([c for c, v in adv.items() if v and v > 0],
                    key=lambda c: adv[c], reverse=True)
    return ranked if n is None else ranked[:n]


def code_returns(provider, codes, d0, d1):
    """每只 close-to-close 收益,退市股用区间内最后可得 bar。bar<2 的跳过
    (占比 <0.3%,dead=-100% 兜底重跑不改结论,见 adv-pool-width 验证)。"""
    out = {}
    for c in codes:
        b = provider.get_daily_bars(c, d0, d1)
        if len(b) >= 2 and b[0].close > 0:
            out[c] = b[-1].close / b[0].close - 1.0
    return out


def period_returns(provider, codes, d0, d1):
    """等权区间收益(向后兼容旧口径)。"""
    rets = code_returns(provider, codes, d0, d1)
    return float(np.mean(list(rets.values()))) if rets else 0.0


def total_mv_map(rd, db_path=MARKET_DB):
    """rd(或其前最近一日)的 {code: total_mv},来自 daily_basic。"""
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT MAX(date) FROM daily_basic WHERE date<=?",
                          (rd.isoformat(),)).fetchone()
        if not row or not row[0]:
            return {}
        return dict(con.execute(
            "SELECT code, total_mv FROM daily_basic WHERE date=? AND total_mv>0",
            (row[0],)))
    finally:
        con.close()


def cap_weights(codes, mv):
    """按 total_mv 归一。无市值数据的 code 剔除(微盘,cap 权重本就≈0)。"""
    w = {c: float(mv[c]) for c in codes if mv.get(c)}
    tot = sum(w.values())
    return {c: v / tot for c, v in w.items()} if tot > 0 else {}


def turnover_l1(prev_w, new_w):
    """单边换手 = 0.5·Σ|Δw|(目标权重对比,忽略期内漂移)。
    等权同规模池下与旧的 1-重合度 公式等值。"""
    keys = set(prev_w) | set(new_w)
    return 0.5 * sum(abs(new_w.get(k, 0.0) - prev_w.get(k, 0.0)) for k in keys)


def weighted_return(rets, w):
    """Σw·r,只计有收益数据的持仓并重归一;空则 0。"""
    held = {c: wt for c, wt in w.items() if c in rets}
    tot = sum(held.values())
    return sum(rets[c] * wt for c, wt in held.items()) / tot if tot > 0 else 0.0


def stats(monthly_rets):
    if not monthly_rets:
        return dict(total=0, ann=0, sharpe=0, mdd=0, n=0)
    r = np.array(monthly_rets)
    nav = np.cumprod(1 + r)
    total = float(nav[-1] - 1)
    n = len(r)
    ann = (1 + total) ** (12.0 / n) - 1 if total > -1 else -1.0
    sh = r.mean() / r.std(ddof=0) * np.sqrt(12) if r.std(ddof=0) > 0 else 0.0
    pk = np.maximum.accumulate(nav)
    mdd = float(((nav - pk) / pk).min())
    return dict(total=total, ann=ann, sharpe=sh, mdd=mdd, n=n)


def run_monthly(provider, rebal, n_take):
    """月度调仓净值:每期收益一次加载,equal / cap 两条腿同时算。"""
    legs = {"equal": [], "cap": []}
    prev_w, prev_pool, prev_rd = None, None, None
    for rd in rebal:
        pool = adv_ranked(provider, rd, n_take)
        w_new = {"equal": {c: 1.0 / len(pool) for c in pool} if pool else {},
                 "cap": cap_weights(pool, total_mv_map(rd))}
        if prev_pool is not None:
            rets = code_returns(provider, prev_pool, prev_rd, rd)
            for leg, series in legs.items():
                r = weighted_return(rets, prev_w[leg])
                turn = turnover_l1(prev_w[leg], w_new[leg])
                series.append(r * (1 - turn * COST_PER_TURN))
        prev_w, prev_pool, prev_rd = w_new, pool, rd
    return {leg: stats(series) for leg, series in legs.items()}, len(prev_pool)


def run_hold(provider, start_rd, end_rd, n_take):
    """期初选池买入持有到期末,equal / cap 总收益(减一次买入成本)。"""
    pool = adv_ranked(provider, start_rd, n_take)
    rets = code_returns(provider, pool, start_rd, end_rd)
    mv = total_mv_map(start_rd)
    yrs = (end_rd - start_rd).days / 365.25
    out = {}
    for leg, w in (("equal", {c: 1.0 / len(pool) for c in pool} if pool else {}),
                   ("cap", cap_weights(pool, mv))):
        tot = weighted_return(rets, w) - COST_PER_TURN
        ann = (1 + tot) ** (1 / yrs) - 1 if tot > -1 and yrs > 0 else -1.0
        out[leg] = dict(total=tot, ann=ann, n=len(pool))
    return out


def main():
    ap = argparse.ArgumentParser(description="诚实被动基准")
    ap.add_argument("--start", default="2021-08-01")
    ap.add_argument("--end", default="2026-06-24")
    args = ap.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    db = Database(ACCOUNT_DB, market_db_path=MARKET_DB)
    db.initialize()
    provider = DataProvider(db)
    rebal = monthly_rebal_dates(provider, start, end)
    print(f"窗口 {rebal[0]}~{rebal[-1]}  {len(rebal)}个月  成本{COST_PER_TURN:.1%}/换手\n", flush=True)

    universes = [("全市场", None), ("ADV1000", 1000), ("ADV300", 300), ("ADV100", 100)]
    LEGS = [("equal", "等权"), ("cap", "市值加权")]

    print("=== 月度调仓(等权=纸面口径,市值加权=可交易口径)===", flush=True)
    print(f"{'池':<10} {'加权':<6} {'总收益':>9} {'年化':>8} {'夏普':>6} {'回撤':>8} {'期末池':>7}", flush=True)
    for name, n in universes:
        by_leg, sz = run_monthly(provider, rebal, n)
        for leg, label in LEGS:
            s = by_leg[leg]
            print(f"{name:<10} {label:<6} {s['total']*100:>+8.1f}% {s['ann']*100:>+7.2f}% "
                  f"{s['sharpe']:>6.2f} {s['mdd']*100:>+7.1f}% {sz:>7}", flush=True)

    print("\n=== 期初买入持有到期末(什么都不做)===", flush=True)
    print(f"{'池':<10} {'加权':<6} {'总收益':>9} {'年化':>8} {'期初池':>7}", flush=True)
    for name, n in universes:
        by_leg = run_hold(provider, rebal[0], rebal[-1], n)
        for leg, label in LEGS:
            h = by_leg[leg]
            print(f"{name:<10} {label:<6} {h['total']*100:>+8.1f}% {h['ann']*100:>+7.2f}% {h['n']:>7}", flush=True)

    print("\nDONE", flush=True)
    db.close()


if __name__ == "__main__":
    main()
