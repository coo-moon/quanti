#!/usr/bin/env python3
"""可转债策略「每年收益率表」+ 独立指数交叉验证 + 过拟合闸。

产出:逐日历年收益(我的全等权 / 低价top40 / 独立的中证转债指数000832)+ 每年券数/换手,
供多 agent 对抗式验证「可转债能否稳定赚钱」+ 判过拟合 + 判数据可信度。

数据可信度硬检验:把我从 cb.db(akshare 单券日线)自算的全等权净值,和官方**中证转债指数
000832**(akshare 独立拉)逐年比对——若两者形状/量级吻合,证明 cb.db 数据没被系统性做假/漏。
注意:2018 年前可转债只有几十只,等权不具代表性(表中会显示券数)。

用法:.venv/bin/python scripts/cb_annual.py [--start 2015-01-01 --end 2026-06-30]
"""
import sys
import sqlite3
import argparse
from collections import defaultdict

import numpy as np

sys.path.insert(0, ".")
from quanti.backtest.overfit import deflated_sharpe_ratio, pbo_cscv, sharpe_per_obs

CB_DB = "data/cb.db"
COST = 0.001


def load_cb(con, start, end):
    px = defaultdict(dict)
    for code, d, c in con.execute(
            "SELECT code,date,close FROM cb_daily WHERE date BETWEEN ? AND ? AND close>0",
            (start, end)):
        px[code][d] = c
    return px


def month_ends(px):
    alld = sorted({d for m in px.values() for d in m})
    by_m = defaultdict(list)
    for d in alld:
        by_m[d[:7]].append(d)
    return [max(v) for _, v in sorted(by_m.items())]


def price_on(px, code, rd):
    m = px[code]
    if rd in m:
        return m[rd]
    pr = [d for d in m if d <= rd]
    return m[max(pr)] if pr else None


def fwd(px, code, rd, nrd):
    m = px[code]
    p0 = price_on(px, code, rd)
    if not p0 or p0 <= 0:
        return None
    w = [d for d in m if rd < d <= nrd]
    return (m[max(w)] / p0 - 1.0) if w else None


def pool_at(px, rd):
    return {c: price_on(px, c, rd) for c in px if price_on(px, c, rd)}


def strat_monthly(px, rebals, top_n=None):
    """top_n=None → 全等权;否则买价格最低 top_n。返回 (月末date, 净收益)。"""
    out, prev = [], None
    for i in range(len(rebals)):
        pool = pool_at(px, rebals[i])
        hold = list(pool) if top_n is None else sorted(pool, key=lambda c: pool[c])[:top_n]
        if prev is not None and i > 0:
            rs = [fwd(px, c, rebals[i-1], rebals[i]) for c in prev]
            rs = [r for r in rs if r is not None]
            r = float(np.mean(rs)) if rs else 0.0
            turn = 1.0 - len(set(prev) & set(hold)) / max(len(prev), 1)
            out.append((rebals[i], r * (1 - turn * COST), len(pool), turn))
        prev = hold
    return out


def index_monthly(rebals):
    """中证转债指数000832 月度收益(独立数据源交叉验证)。"""
    import akshare as ak
    d = ak.stock_zh_index_daily_em(symbol="sh000832")
    idx = {str(r["date"])[:10]: float(r["close"]) for _, r in d.iterrows()}
    def on(rd):
        if rd in idx:
            return idx[rd]
        pr = [x for x in idx if x <= rd]
        return idx[max(pr)] if pr else None
    out = {}
    for i in range(1, len(rebals)):
        a, b = on(rebals[i-1]), on(rebals[i])
        if a and b:
            out[rebals[i]] = b / a - 1.0
    return out


def by_year(monthly_pairs):
    """[(date, ret, ...)] → {year: 复利年收益}。"""
    yr = defaultdict(list)
    for row in monthly_pairs:
        yr[row[0][:4]].append(row[1])
    return {y: float(np.prod([1+x for x in v]) - 1) for y, v in sorted(yr.items())}


def full_stat(rets):
    r = np.asarray(rets)
    nav = np.cumprod(1 + r)
    n = len(r)
    ann = (1 + (nav[-1]-1)) ** (12.0/n) - 1
    sd = r.std(ddof=0)
    sh = r.mean()/sd*np.sqrt(12) if sd > 0 else 0
    pk = np.maximum.accumulate(nav)
    return dict(ann=ann, sharpe=sh, mdd=float(((nav-pk)/pk).min()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default="2026-06-30")
    args = ap.parse_args()

    con = sqlite3.connect(CB_DB)
    px = load_cb(con, args.start, args.end)
    con.close()
    rebals = month_ends(px)

    ew = strat_monthly(px, rebals, None)
    lp = strat_monthly(px, rebals, 40)
    try:
        idx = index_monthly(rebals)
        idx_ok = True
    except Exception as e:
        print(f"(指数拉取失败,跳过交叉验证: {repr(e)[:80]})", flush=True)
        idx, idx_ok = {}, False

    ew_y, lp_y = by_year(ew), by_year(lp)
    idx_y = by_year([(d, r) for d, r in idx.items()]) if idx_ok else {}
    nbyyear = defaultdict(list)
    tbyyear = defaultdict(list)
    for d, r, n, t in ew:
        nbyyear[d[:4]].append(n)
        tbyyear[d[:4]].append(t)

    print(f"\n窗口 {rebals[0]}~{rebals[-1]}  成本{COST:.1%}/换手\n", flush=True)
    print(f"{'年份':<6}{'全等权':>9}{'低价top40':>11}{'中证转债idx':>12}{'均券数':>7}{'年换手':>7}", flush=True)
    print("-"*54, flush=True)
    for y in sorted(ew_y):
        ne = int(np.mean(nbyyear[y])) if nbyyear[y] else 0
        tu = float(np.mean(tbyyear[y])) if tbyyear[y] else 0
        idxs = f"{idx_y[y]*100:>+10.1f}%" if y in idx_y else f"{'—':>11}"
        thin = "  ← 券太少,不具代表性" if ne < 60 else ""
        print(f"{y:<6}{ew_y[y]*100:>+8.1f}%{lp_y[y]*100:>+10.1f}%{idxs}{ne:>7}{tu*100:>6.0f}%{thin}", flush=True)

    print("\n=== 全期(仅券数≥60 的成熟期更可信)===", flush=True)
    for name, series in [("全等权", ew), ("低价top40", lp)]:
        s = full_stat([r for _, r, _, _ in series])
        print(f"  {name:<10} 年化{s['ann']*100:+6.2f}% 夏普{s['sharpe']:.2f} 回撤{s['mdd']*100:.1f}%", flush=True)
    if idx_ok:
        s = full_stat(list(idx.values()))
        print(f"  {'中证转债idx':<10} 年化{s['ann']*100:+6.2f}% 夏普{s['sharpe']:.2f} 回撤{s['mdd']*100:.1f}%", flush=True)

    # 数据交叉验证:我的全等权 vs 官方指数 月度相关
    if idx_ok:
        common = [d for d, *_ in ew if d in idx]
        a = np.array([r for d, r, _, _ in ew if d in idx])
        b = np.array([idx[d] for d in common])
        if len(a) > 5:
            corr = float(np.corrcoef(a, b)[0, 1])
            print(f"\n数据交叉验证:我的全等权 vs 中证转债指数 月度相关={corr:.3f} "
                  f"(高相关→cb.db 数据可信;{len(a)}个月重叠)", flush=True)

    # 过拟合闸:全等权 beta(无可调 config)+ 低价top40(有 config)
    ew_r = np.array([r for _, r, _, _ in ew])
    lp_r = np.array([r for _, r, _, _ in lp])
    pbo = pbo_cscv(np.column_stack([ew_r, lp_r]), n_splits=min(12, (len(ew_r)//2)*2))
    dsr_ew = deflated_sharpe_ratio(ew_r, [sharpe_per_obs(ew_r), sharpe_per_obs(lp_r)])
    print(f"\n过拟合闸:全等权 DSR={dsr_ew['dsr']:.3f}(beta 无可调参→非挑优,DSR 是纯 PSR 意义);"
          f"PBO(等权vs低价)={pbo['pbo']:.3f}", flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
