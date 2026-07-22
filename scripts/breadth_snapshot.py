#!/usr/bin/env python3
"""市场宽度 regime 快照:基于 market.db 全A股行情,判断当日大盘趋势(涨/跌/震荡)、
板块轮动、资金流向。纯观测(observe-only),不产生交易信号,给人/LLM 参考。

用法: .venv/bin/python scripts/breadth_snapshot.py [--json]
输出宽度指标、趋势判定、行业强弱榜、成交额趋势。约 10-20s。
"""
import sqlite3
import sys
from datetime import date

import numpy as np
import pandas as pd

DB = "data/market.db"
LOOKBACK = 230  # 交易日,够算 MA200


def load(db_path=DB):
    con = sqlite3.connect(db_path)
    dates = pd.read_sql(
        "SELECT DISTINCT date FROM daily_quotes ORDER BY date DESC LIMIT ?",
        con, params=(LOOKBACK,))["date"].tolist()
    start = min(dates)
    q = pd.read_sql(
        "SELECT code,date,close,adj_factor,amount,turnover FROM daily_quotes "
        "WHERE date>=? ", con, params=(start,))
    # 最新市值(市值加权代表"大盘")与换手
    latest = q["date"].max()
    mv = pd.read_sql(
        "SELECT code,total_mv,turnover_rate FROM daily_basic WHERE date=?",
        con, params=(latest,)).set_index("code")
    stk = pd.read_sql("SELECT code,name,industry,delist_date FROM stocks", con)
    con.close()
    # 剔除已退市(PIT:delist_date 非空且 <= 最新日则本不该有当日行情,稳妥再滤)
    dead = set(stk.loc[stk["delist_date"].notna() & (stk["delist_date"] != "")
                       & (stk["delist_date"] <= latest), "code"])
    q = q[~q["code"].isin(dead)]
    return q, mv, stk.set_index("code"), latest


def pivot_adj(q):
    q = q.copy()
    q["adj"] = q["close"] * q["adj_factor"].fillna(1.0)
    px = q.pivot_table(index="date", values="adj", columns="code")
    return px.sort_index()


def pct_above_ma(px, win):
    ma = px.rolling(win, min_periods=win).mean()
    last = px.iloc[-1]
    m = ma.iloc[-1]
    valid = last.notna() & m.notna()
    return float((last[valid] > m[valid]).mean()) * 100, int(valid.sum())


def cap_vs_equal(px, mv, days):
    """市值加权(大盘)vs 等权(普涨/小盘)近 N 交易日收益 %"""
    if len(px) <= days:
        return None, None
    r = px.iloc[-1] / px.iloc[-1 - days] - 1.0
    r = r.replace([np.inf, -np.inf], np.nan).dropna()
    eq = float(r.mean()) * 100
    w = mv["total_mv"].reindex(r.index).fillna(0.0)
    cap = float((r * w).sum() / w.sum()) * 100 if w.sum() > 0 else None
    return cap, eq


def new_hi_lo(px, win=20):
    last = px.iloc[-1]
    hi = px.iloc[-win:].max()
    lo = px.iloc[-win:].min()
    valid = last.notna()
    nh = int((last[valid] >= hi[valid]).sum())
    nl = int((last[valid] <= lo[valid]).sum())
    return nh, nl


def adv_decline(px):
    r = px.iloc[-1] / px.iloc[-2] - 1.0
    r = r.dropna()
    return int((r > 0.0001).sum()), int((r < -0.0001).sum()), int((r.abs() <= 0.0001).sum())


def industry_rot(px, stk, days=20):
    r = (px.iloc[-1] / px.iloc[-1 - days] - 1.0).replace([np.inf, -np.inf], np.nan).dropna()
    df = pd.DataFrame({"ret": r})
    df["ind"] = stk["industry"].reindex(df.index)
    df = df[df["ind"].notna() & (df["ind"] != "") & (df["ind"] != "nan")]
    g = df.groupby("ind")["ret"].agg(["mean", "count"])
    g = g[g["count"] >= 5]
    g["mean"] *= 100
    return g.sort_values("mean", ascending=False)


def amount_trend(q):
    a = q.groupby("date")["amount"].sum() / 1e8  # 亿元
    return a.sort_index()


def classify(above20, above50, above200, cap5, eq5, ad_ratio, amt_chg):
    """趋势判定:多因子投票,输出 (标签, 理由列表)。"""
    votes = []
    reasons = []
    # 中期结构:MA50 上方占比
    if above50 >= 60:
        votes.append(1); reasons.append(f"MA50上方{above50:.0f}%(多头结构)")
    elif above50 <= 40:
        votes.append(-1); reasons.append(f"MA50上方仅{above50:.0f}%(空头结构)")
    else:
        votes.append(0); reasons.append(f"MA50上方{above50:.0f}%(分化)")
    # 长期趋势:MA200
    if above200 >= 55:
        votes.append(1); reasons.append(f"MA200上方{above200:.0f}%(长期多头)")
    elif above200 <= 40:
        votes.append(-1); reasons.append(f"MA200上方仅{above200:.0f}%(长期承压)")
    else:
        votes.append(0); reasons.append(f"MA200上方{above200:.0f}%(中性)")
    # 近5日大盘动能
    if cap5 is not None:
        if cap5 >= 1.5:
            votes.append(1); reasons.append(f"大盘近5日+{cap5:.1f}%")
        elif cap5 <= -1.5:
            votes.append(-1); reasons.append(f"大盘近5日{cap5:.1f}%")
        else:
            votes.append(0); reasons.append(f"大盘近5日{cap5:+.1f}%(横盘)")
    # 涨跌家数
    if ad_ratio >= 1.5:
        votes.append(1); reasons.append(f"涨跌比{ad_ratio:.2f}(普涨)")
    elif ad_ratio <= 0.67:
        votes.append(-1); reasons.append(f"涨跌比{ad_ratio:.2f}(普跌)")
    else:
        votes.append(0); reasons.append(f"涨跌比{ad_ratio:.2f}(均衡)")
    s = sum(votes)
    if s >= 2:
        label = "上涨(多头)"
    elif s <= -2:
        label = "下跌(空头)"
    else:
        label = "震荡(区间/分化)"
    return label, reasons, s


def build(db_path=DB):
    q, mv, stk, latest = load(db_path)
    px = pivot_adj(q)
    a20, n20 = pct_above_ma(px, 20)
    a50, _ = pct_above_ma(px, 50)
    a200, _ = pct_above_ma(px, 200)
    cap5, eq5 = cap_vs_equal(px, mv, 5)
    cap20, eq20 = cap_vs_equal(px, mv, 20)
    cap1, eq1 = cap_vs_equal(px, mv, 1)
    up, dn, fl = adv_decline(px)
    ad_ratio = up / max(dn, 1)
    nh, nl = new_hi_lo(px, 20)
    ind = industry_rot(px, stk, 20)
    ind5 = industry_rot(px, stk, 5)
    amt = amount_trend(q)
    amt5 = float(amt.iloc[-5:].mean())
    amt20 = float(amt.iloc[-20:].mean())
    amt_chg = (amt5 / amt20 - 1) * 100 if amt20 else 0.0
    turn = float(mv["turnover_rate"].median())
    label, reasons, score = classify(a20, a50, a200, cap5, eq5, ad_ratio, amt_chg)
    return dict(
        latest=latest, n_stocks=int(px.iloc[-1].notna().sum()),
        above20=a20, above50=a50, above200=a200,
        cap1=cap1, eq1=eq1, cap5=cap5, eq5=eq5, cap20=cap20, eq20=eq20,
        up=up, dn=dn, fl=fl, ad_ratio=ad_ratio, nh=nh, nl=nl,
        amt_today=float(amt.iloc[-1]), amt5=amt5, amt20=amt20, amt_chg=amt_chg,
        turn=turn, label=label, reasons=reasons, score=score,
        ind_top=ind.head(8), ind_bot=ind.tail(8), ind5_top=ind5.head(8),
    )


def render(r):
    L = []
    L.append(f"# 市场宽度 regime 快照 — {r['latest']}")
    L.append(f"\n覆盖 {r['n_stocks']} 只有效个股(市值加权=大盘,等权=普涨/小盘)\n")
    L.append(f"## 趋势判定:【{r['label']}】(投票分 {r['score']:+d})")
    for x in r["reasons"]:
        L.append(f"- {x}")
    L.append("\n## 宽度指标")
    L.append(f"- 站上 MA20: {r['above20']:.0f}%  | MA50: {r['above50']:.0f}%  | MA200: {r['above200']:.0f}%")
    L.append(f"- 今日涨/跌/平: {r['up']}/{r['dn']}/{r['fl']}  (涨跌比 {r['ad_ratio']:.2f})")
    L.append(f"- 20日新高/新低: {r['nh']}/{r['nl']}")
    L.append("\n## 大盘 vs 小盘(区间收益 %)")
    def f(x): return f"{x:+.2f}%" if x is not None else "—"
    L.append(f"- 近1日: 大盘 {f(r['cap1'])} / 等权 {f(r['eq1'])}")
    L.append(f"- 近5日: 大盘 {f(r['cap5'])} / 等权 {f(r['eq5'])}")
    L.append(f"- 近20日: 大盘 {f(r['cap20'])} / 等权 {f(r['eq20'])}")
    L.append("\n## 资金/情绪")
    L.append(f"- 今日成交额: {r['amt_today']:.0f} 亿  (5日均 {r['amt5']:.0f} / 20日均 {r['amt20']:.0f}, 5v20 {r['amt_chg']:+.1f}%)")
    L.append(f"- 全市场换手率中位数: {r['turn']:.2f}%")
    L.append("\n## 板块轮动(20日等权收益,前8强)")
    for ind, row in r["ind_top"].iterrows():
        L.append(f"- {ind}: {row['mean']:+.1f}%  (n={int(row['count'])})")
    L.append("\n### 20日最弱8")
    for ind, row in r["ind_bot"].iterrows():
        L.append(f"- {ind}: {row['mean']:+.1f}%  (n={int(row['count'])})")
    L.append("\n### 近5日领涨(短期资金流入)")
    for ind, row in r["ind5_top"].iterrows():
        L.append(f"- {ind}: {row['mean']:+.1f}%  (n={int(row['count'])})")
    return "\n".join(L)


def _selfcheck():
    # 合成:全部股票单调上涨 → 应判上涨,MA 上方~100%
    idx = pd.date_range("2024-01-01", periods=230, freq="B")
    codes = [f"C{i}" for i in range(50)]
    px = pd.DataFrame(
        {c: np.linspace(10, 20, 230) * (1 + 0.001 * i) for i, c in enumerate(codes)},
        index=[d.strftime("%Y-%m-%d") for d in idx])
    a50, _ = pct_above_ma(px, 50)
    a200, _ = pct_above_ma(px, 200)
    assert a50 > 95 and a200 > 95, (a50, a200)
    up, dn, _ = adv_decline(px)
    assert up == 50 and dn == 0, (up, dn)
    mv = pd.DataFrame({"total_mv": [1.0] * 50, "turnover_rate": [1.0] * 50}, index=codes)
    cap5, eq5 = cap_vs_equal(px, mv, 5)
    assert cap5 > 0 and eq5 > 0
    label, _, score = classify(99, 99, 99, cap5, eq5, up / max(dn, 1), 0)
    assert label.startswith("上涨") and score >= 2, (label, score)
    print("selfcheck OK")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    elif "--json" in sys.argv:
        import json
        r = build()
        r["ind_top"] = r["ind_top"].reset_index().to_dict("records")
        r["ind_bot"] = r["ind_bot"].reset_index().to_dict("records")
        r["ind5_top"] = r["ind5_top"].reset_index().to_dict("records")
        print(json.dumps(r, ensure_ascii=False, default=str))
    else:
        print(render(build()))
