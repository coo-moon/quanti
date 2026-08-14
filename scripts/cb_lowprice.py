#!/usr/bin/env python3
"""可转债低价策略回测 —— 跨出股票边界,测 A 股结构性最稳、long-only 无需融券的方向。

逻辑:可转债 = 债底(到期还本付息)+ 转股期权。**低价**可转债 ≈ 贴近债底 → 下行有限、
上行不对称,是"双低"策略的防御核心,只需券价历史即可严格回测。买价格最低 N 只等权,
月度调仓;对照 = 全等权可转债(可转债指数 beta)。成本极低(无印花税,~0.1%/换手)。
幸存者正确:cb.db 含强赎/到期/违约退出,period return 用区间内最后可得价(强赎≈130 计盈、
违约计亏)。左尾 = 信用违约(数据已捕获)。

判据:IS/OOS 双半程稳健 + DSR/PBO。用法:.venv/bin/python scripts/cb_lowprice.py
数据:data/cb.db(先跑 scripts/fetch_convertibles.py)。
"""
import sys
import sqlite3
import argparse
from collections import defaultdict

import numpy as np

sys.path.insert(0, ".")
from quanti.backtest.overfit import deflated_sharpe_ratio, pbo_cscv, sharpe_per_obs

CB_DB = "data/cb.db"
COST = 0.001          # 可转债:无印花税,佣金极低;0.1%/换手 保守
# 默认 2018-2026(102 月,含 2018/2022 两次熊市,验 beta 跨 regime 稳健);--start 可调
START, END = "2018-01-01", "2026-06-30"
# 变体:(名字, top_n, 价格上限=剔除临近强赎的高价券)
VARIANTS = [
    ("低价top15", 15, None), ("低价top20", 20, None),
    ("低价top30", 30, None), ("低价top40", 40, None),
    ("低价top30·剔>135", 30, 135.0), ("低价top30·剔>130剔<95", 30, 130.0),
]
APRIORI = "低价top30"


def load(con):
    px = defaultdict(dict)   # code -> {date: close}
    for code, d, c in con.execute(
            "SELECT code,date,close FROM cb_daily WHERE date BETWEEN ? AND ? AND close>0",
            (START, END)):
        px[code][d] = c
    return px


def month_ends(px):
    alldates = sorted({d for m in px.values() for d in m})
    by_m = defaultdict(list)
    for d in alldates:
        by_m[d[:7]].append(d)
    return [max(v) for _, v in sorted(by_m.items())]


def price_on(px, code, rd):
    """rd 当天或之前最近的价(容缺)。"""
    m = px[code]
    if rd in m:
        return m[rd]
    prior = [d for d in m if d <= rd]
    return m[max(prior)] if prior else None


def fwd_return(px, code, rd, nrd):
    """rd→nrd 收益;退出券用区间内最后可得价。"""
    m = px[code]
    p0 = price_on(px, code, rd)
    if not p0 or p0 <= 0:
        return None
    window = [d for d in m if rd < d <= nrd]
    if not window:
        return None
    return m[max(window)] / p0 - 1.0


def pool_at(px, rd, lo_cap=None):
    """rd 在交易的券(当天/近日有价)。lo_cap:(low,high) 价格带。"""
    out = {}
    for code in px:
        p = price_on(px, code, rd)
        if p and p > 0:
            out[code] = p
    return out


def run_variant(px, rebals, top_n, price_cap):
    rets, prev = [], None
    for i in range(len(rebals)):
        rd = rebals[i]
        pool = pool_at(px, rd)
        # 价格带过滤
        if price_cap is not None:
            if price_cap == 130.0:   # 剔>130 且 剔<95(避强赎顶 + 避深度违约风险)
                pool = {c: p for c, p in pool.items() if 95 <= p <= 130}
            else:
                pool = {c: p for c, p in pool.items() if p <= price_cap}
        hold = sorted(pool, key=lambda c: pool[c])[:top_n]
        if prev is not None and i > 0:
            rs = [fwd_return(px, c, rebals[i-1], rd) for c in prev]
            rs = [r for r in rs if r is not None]
            r = float(np.mean(rs)) if rs else 0.0
            turn = 1.0 - len(set(prev) & set(hold)) / max(len(prev), 1)
            rets.append(r * (1 - turn * COST))
        prev = hold
    return np.array(rets)


def run_baseline(px, rebals):
    rets, prev = [], None
    for i in range(len(rebals)):
        pool = list(pool_at(px, rebals[i]))
        if prev is not None and i > 0:
            rs = [fwd_return(px, c, rebals[i-1], rebals[i]) for c in prev]
            rs = [r for r in rs if r is not None]
            rets.append(float(np.mean(rs)) if rs else 0.0)
        prev = pool
    return np.array(rets)


def stat(r):
    r = np.asarray(r)
    if len(r) == 0:
        return dict(total=0, ann=0, sharpe=0, mdd=0, n=0)
    nav = np.cumprod(1 + r)
    n = len(r)
    tot = float(nav[-1] - 1)
    ann = (1 + tot) ** (12.0 / n) - 1 if tot > -1 else -1.0
    sd = r.std(ddof=0)
    sh = r.mean() / sd * np.sqrt(12) if sd > 0 else 0.0
    pk = np.maximum.accumulate(nav)
    return dict(total=tot, ann=ann, sharpe=sh, mdd=float(((nav - pk) / pk).min()), n=n)


def main():
    global START, END
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=START)
    ap.add_argument("--end", default=END)
    args = ap.parse_args()
    START, END = args.start, args.end
    con = sqlite3.connect(CB_DB)
    px = load(con)
    con.close()
    if not px:
        print("cb.db 空,先跑 fetch_convertibles.py")
        return
    rebals = month_ends(px)
    oos = int(len(rebals) * 0.6) - 1
    npool = [len(pool_at(px, rd)) for rd in rebals]
    print(f"券池 {len(px)} 只;调仓 {len(rebals)} 月 {rebals[0]}~{rebals[-1]};"
          f"IS前{oos}/OOS后{len(rebals)-1-oos} 分界≈{rebals[oos+1]};在场券数 {min(npool)}~{max(npool)}", flush=True)

    base = run_baseline(px, rebals)
    b, bi, bo = stat(base), stat(base[:oos]), stat(base[oos:])
    print(f"\n{'策略':<20}| {'年化':>8}{'夏普':>6}{'回撤':>8} | {'IS夏普':>7}{'OOS夏普':>8}{'OOS年化':>8}", flush=True)
    print(f"{'全等权(可转债beta)':<20}| {b['ann']*100:>+7.2f}%{b['sharpe']:>6.2f}{b['mdd']*100:>+7.1f}% | "
          f"{bi['sharpe']:>7.2f}{bo['sharpe']:>8.2f}{bo['ann']*100:>+7.2f}%", flush=True)
    series = {}
    for name, n, cap in VARIANTS:
        r = run_variant(px, rebals, n, cap)
        series[name] = r
        f, i, o = stat(r), stat(r[:oos]), stat(r[oos:])
        print(f"{name:<20}| {f['ann']*100:>+7.2f}%{f['sharpe']:>6.2f}{f['mdd']*100:>+7.1f}% | "
              f"{i['sharpe']:>7.2f}{o['sharpe']:>8.2f}{o['ann']*100:>+7.2f}%", flush=True)

    print("\n=== 风险调整后稳健性(夏普 IS&OOS 双超基准?)===", flush=True)
    for name in series:
        r = series[name]
        ok_is = stat(r[:oos])['sharpe'] > bi['sharpe']
        ok_oos = stat(r[oos:])['sharpe'] > bo['sharpe']
        print(f"  {name:<20} IS={'✓' if ok_is else '✗'} OOS={'✓' if ok_oos else '✗'}"
              f"{'  ← 双超(稳健)' if ok_is and ok_oos else ''}", flush=True)

    names = list(series)
    trials = [sharpe_per_obs(series[n]) for n in names]
    best = names[int(np.argmax(trials))]
    dsr_ap = deflated_sharpe_ratio(series[APRIORI], trials)
    dsr_best = deflated_sharpe_ratio(series[best], trials)
    M = np.column_stack([series[n] for n in names])
    pbo = pbo_cscv(M, n_splits=min(12, (len(rebals)//2)*2))
    print("\n=== 过拟合闸 ===", flush=True)
    print(f"  先验={APRIORI}: DSR={dsr_ap['dsr']:.3f} (sr_obs={dsr_ap['sr_observed']:.3f} vs sr0={dsr_ap['sr0_benchmark']:.3f})", flush=True)
    print(f"  挑最优={best}: DSR={dsr_best['dsr']:.3f}", flush=True)
    print(f"  PBO={pbo['pbo']:.3f} ({pbo['n_configs']}变体/{pbo['n_combos']}组合)", flush=True)
    print("\nDONE", flush=True)


if __name__ == "__main__":
    main()
