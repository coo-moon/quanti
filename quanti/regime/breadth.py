#!/usr/bin/env python3
"""市场宽度 regime 快照:基于 market.db 全A股行情,判断当日大盘趋势(涨/跌/震荡)、
板块轮动、资金流向。纯观测(observe-only),不产生交易信号,给人/LLM 参考。

规则层判定——确定性、可复现、不依赖 LLM。:mod:`quanti.regime.report` 在它
之上叠加时事面与 LLM 解读;两层都会落库,背离本身就是信号。

CLI: .venv/bin/python scripts/breadth_snapshot.py [--json|--selfcheck]
输出宽度指标、趋势判定、行业强弱榜、成交额趋势。约 10-20s。
"""
import math
import sqlite3
import sys

import numpy as np
import pandas as pd

DB = "data/market.db"
LOOKBACK = 230  # 交易日,够算 MA200

MIN_STOCKS = 500
"""最新交易日至少要有这么多只票有收盘价,否则整张快照不可用。

A 股全市场 5500+,任何一个完整交易日都远在此之上;低于它只可能是「当天行情
还没同步完就跑了快照」(2026-08-06 实测:那一刻 daily_quotes 只有 1 只票)。"""

MIN_COVERAGE = 0.8
"""相对闸:最新日覆盖数 / 前 20 日覆盖数中位数。

绝对闸挡不住「同步到 3000 只就快照」—— 半截数据算出来的宽度是有偏的,数值上
却看不出任何异常。实测日间覆盖数只随新股上市单调微增(5528→5538,<1%),
0.8 留了极大余量,连大面积停牌都误伤不到。"""

UNUSABLE_LABEL = "数据不足"
"""数据面不可用时的规则层标签。

**必须**不含「上涨/震荡/下跌」任一子串::mod:`quanti.regime.prompt` 用它挡
prompt 注入,前端 RegimeCard 的 `regimeOf()` 靠子串匹配上色 —— 含了就会被
当成一个正常判定,那正是这次要修的病。"""


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
    """→ (站上 MA 的占比 %, 参与计算的股票数)。

    一只票都算不出该均线时返回 ``(None, 0)`` 而不是 NaN。空切片的 `.mean()`
    是 NaN,而 NaN 会**静默穿过** :func:`classify` 的每一个比较(`nan >= 60`
    和 `nan <= 40` 同时为 False),最后落进 else 分支 —— 垃圾数据于是产出一个
    看着完全正常的「震荡(区间/分化)」标签。None 会被 :func:`build` 的闸拦住。
    """
    ma = px.rolling(win, min_periods=win).mean()
    last = px.iloc[-1]
    m = ma.iloc[-1]
    valid = last.notna() & m.notna()
    n = int(valid.sum())
    if n == 0:
        return None, 0
    return float((last[valid] > m[valid]).mean()) * 100, n


def cap_vs_equal(px, mv, days):
    """市值加权(大盘)vs 等权(普涨/小盘)近 N 交易日收益 %

    没有一只票算得出区间收益(当日行情没落库 → 空切片)时两个都是 None。
    """
    if len(px) <= days:
        return None, None
    r = px.iloc[-1] / px.iloc[-1 - days] - 1.0
    r = r.replace([np.inf, -np.inf], np.nan).dropna()
    if r.empty:
        return None, None
    eq = float(r.mean()) * 100
    w = mv["total_mv"].reindex(r.index).fillna(0.0)
    cap = float((r * w).sum() / w.sum()) * 100 if w.sum() > 0 else None
    return cap, eq


def median_turnover(mv):
    """全市场换手率中位数。daily_basic 当日一行都没落库(同步没跑完)时 mv 是
    空表,空 median 又是 NaN —— 返回 None。"""
    if "turnover_rate" not in getattr(mv, "columns", ()):
        return None
    s = mv["turnover_rate"].replace([np.inf, -np.inf], np.nan).dropna()
    return float(s.median()) if not s.empty else None


def coverage(px):
    """→ (最新日有收盘价的股票数, 前 20 日该数的中位数或 None)。

    比绝对票数更能识别「同步半截」:全市场票数随新股上市缓慢增长,但一天之内
    不会掉一大截。历史不足 5 天时返回 None(冷启动,交给绝对闸和均线闸)。
    """
    cnt = px.notna().sum(axis=1)
    prior = cnt.iloc[-21:-1]
    return int(cnt.iloc[-1]), (float(prior.median()) if len(prior) >= 5 else None)


def _num(v) -> bool:
    """有值且是有限数。None 和 NaN 都要挡 —— NaN 的所有比较都是 False,
    这是 2026-08-06 那个假判定的直接成因。"""
    return v is not None and not (isinstance(v, float) and not math.isfinite(v))


def unusable_reason(metrics: dict, rule_label: str = "") -> str:
    """数据面是否不可用 → 不足原因;空串 = 可用。

    刻意只吃一个 metrics 映射(而不是 :func:`build` 的完整返回),这样**已落库
    的行**都能用同一把尺子复判 —— 包括修复前那些带裸 NaN、被 `_json_safe` 读成
    None 的污染行,不需要数据迁移。:func:`quanti.regime.prompt.latest_usable`
    的注入闸就靠这个。

    **键缺失不算证据**。`_metrics_payload` 会丢掉 None,老快照也可能只存了几个
    字段;把「没写」当成「不足」会把历史快照全部误杀。
    """
    if rule_label and UNUSABLE_LABEL in str(rule_label):
        return "规则层已判定数据不足"
    n = metrics.get("n_stocks")
    if n is not None and int(n) < MIN_STOCKS:
        return f"当日仅 {int(n)} 只个股有行情(下限 {MIN_STOCKS})"
    blank = [k for k in ("above20", "above50", "above200")
             if k in metrics and not _num(metrics[k])]
    if blank:
        return f"宽度指标算不出来:{'/'.join(blank)}"
    return ""


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
    """趋势判定:多因子投票,输出 (标签, 理由列表, 投票分)。

    结构性指标(above50/above200)缺失时直接返回 :data:`UNUSABLE_LABEL`、**不
    投票**。以前它们可能是 NaN,而 `nan >= 60` 和 `nan <= 40` 同时为 False,于是
    静默落进 else 分支 —— 2026-08-06 那天全市场只同步到 1 只票,照样输出了
    「震荡(区间/分化)」和投票分 -1,还进了 LLM 决策 prompt。

    这道判断是**兜底**:正常路径上 :func:`build` 的闸已经不会走到这里,但
    classify 是公开函数,不能指望每个调用方都先自己验一遍数据。
    """
    if not (_num(above50) and _num(above200)):
        miss = "/".join(n for n, v in (("MA50", above50), ("MA200", above200))
                        if not _num(v))
        return UNUSABLE_LABEL, [f"{miss}上方占比算不出来,不做趋势判定"], 0
    votes = []
    reasons = []
    # 中期结构:MA50 上方占比
    if above50 >= 60:
        votes.append(1)
        reasons.append(f"MA50上方{above50:.0f}%(多头结构)")
    elif above50 <= 40:
        votes.append(-1)
        reasons.append(f"MA50上方仅{above50:.0f}%(空头结构)")
    else:
        votes.append(0)
        reasons.append(f"MA50上方{above50:.0f}%(分化)")
    # 长期趋势:MA200
    if above200 >= 55:
        votes.append(1)
        reasons.append(f"MA200上方{above200:.0f}%(长期多头)")
    elif above200 <= 40:
        votes.append(-1)
        reasons.append(f"MA200上方仅{above200:.0f}%(长期承压)")
    else:
        votes.append(0)
        reasons.append(f"MA200上方{above200:.0f}%(中性)")
    # 近5日大盘动能
    if cap5 is not None:
        if cap5 >= 1.5:
            votes.append(1)
            reasons.append(f"大盘近5日+{cap5:.1f}%")
        elif cap5 <= -1.5:
            votes.append(-1)
            reasons.append(f"大盘近5日{cap5:.1f}%")
        else:
            votes.append(0)
            reasons.append(f"大盘近5日{cap5:+.1f}%(横盘)")
    # 涨跌家数
    if ad_ratio >= 1.5:
        votes.append(1)
        reasons.append(f"涨跌比{ad_ratio:.2f}(普涨)")
    elif ad_ratio <= 0.67:
        votes.append(-1)
        reasons.append(f"涨跌比{ad_ratio:.2f}(普跌)")
    else:
        votes.append(0)
        reasons.append(f"涨跌比{ad_ratio:.2f}(均衡)")
    s = sum(votes)
    if s >= 2:
        label = "上涨(多头)"
    elif s <= -2:
        label = "下跌(空头)"
    else:
        label = "震荡(区间/分化)"
    return label, reasons, s


def build(db_path=DB):
    """全市场宽度快照。额外返回 `usable` / `unusable_reason` —— **调用方必须先
    看 usable**,不可用时 `label` 是 :data:`UNUSABLE_LABEL`、指标多为 None。

    数据充足性闸(2026-08-06 事故的根因修复):17:30 的快照可能撞上当天行情还
    没同步完,那时算出来的一切都是垃圾,而垃圾在数值上未必看得出来 —— 所以闸
    设在这里,而不是等下游发现 NaN。三条,任一条不过就整张作废:

    1. **绝对覆盖**:当日有行情的票数 < :data:`MIN_STOCKS`。
    2. **相对覆盖**:当日票数 < 前 20 日中位数的 :data:`MIN_COVERAGE`
       —— 挡「同步到一半就快照」,这种情况指标算得出来但是有偏的。
    3. **均线算不出来**:above20/50/200 任一为 None(没有一只票有足够历史)。
    """
    q, mv, stk, latest = load(db_path)
    px = pivot_adj(q)
    n_stocks, cov_base = coverage(px)
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
    turn = median_turnover(mv)
    r = dict(
        latest=latest, n_stocks=n_stocks,
        above20=a20, above50=a50, above200=a200,
        cap1=cap1, eq1=eq1, cap5=cap5, eq5=eq5, cap20=cap20, eq20=eq20,
        up=up, dn=dn, fl=fl, ad_ratio=ad_ratio, nh=nh, nl=nl,
        amt_today=float(amt.iloc[-1]), amt5=amt5, amt20=amt20, amt_chg=amt_chg,
        turn=turn, ind_top=ind.head(8), ind_bot=ind.tail(8), ind5_top=ind5.head(8),
    )
    reason = unusable_reason(r)
    if not reason and cov_base and n_stocks < MIN_COVERAGE * cov_base:
        reason = (f"当日覆盖 {n_stocks} 只,仅为近 20 日中位数 {cov_base:.0f} 的 "
                  f"{n_stocks / cov_base:.0%}(下限 {MIN_COVERAGE:.0%}),"
                  f"当日行情疑似没同步完")
    if reason:
        # 不投票、不编标签。规则层这一行是 prompt.py 的挡板依据,也是 UI 上
        # 「今天没数据」和「今天震荡」的唯一区别。
        r.update(usable=False, unusable_reason=reason,
                 label=UNUSABLE_LABEL, reasons=[reason], score=0)
        return r
    label, reasons, score = classify(a20, a50, a200, cap5, eq5, ad_ratio, amt_chg)
    r.update(usable=True, unusable_reason="",
             label=label, reasons=reasons, score=score)
    return r


def _pct(x, digits=0, sign=False) -> str:
    """算不出来的指标一律显示破折号。**不要退回 0** —— 「MA20 上方 0%」是一个
    极端看空的读数,而真相是「没算出来」,那是两件完全不同的事。"""
    if not _num(x):
        return "—"
    return f"{x:+.{digits}f}%" if sign else f"{x:.{digits}f}%"


def render(r):
    L = []
    L.append(f"# 市场宽度 regime 快照 — {r['latest']}")
    L.append(f"\n覆盖 {r['n_stocks']} 只有效个股(市值加权=大盘,等权=普涨/小盘)\n")
    if not r.get("usable", True):
        L.append(f"> ⚠️ **数据不足,本次快照不可用**:{r.get('unusable_reason', '')}\n"
                 "> 下面的数字是残缺数据算出来的,不构成任何市场判断。\n")
    L.append(f"## 趋势判定:【{r['label']}】(投票分 {r['score']:+d})")
    for x in r["reasons"]:
        L.append(f"- {x}")
    L.append("\n## 宽度指标")
    L.append(f"- 站上 MA20: {_pct(r['above20'])}  | MA50: {_pct(r['above50'])}"
             f"  | MA200: {_pct(r['above200'])}")
    L.append(f"- 今日涨/跌/平: {r['up']}/{r['dn']}/{r['fl']}  (涨跌比 {r['ad_ratio']:.2f})")
    L.append(f"- 20日新高/新低: {r['nh']}/{r['nl']}")
    L.append("\n## 大盘 vs 小盘(区间收益 %)")
    def f(x): return _pct(x, 2, sign=True)
    L.append(f"- 近1日: 大盘 {f(r['cap1'])} / 等权 {f(r['eq1'])}")
    L.append(f"- 近5日: 大盘 {f(r['cap5'])} / 等权 {f(r['eq5'])}")
    L.append(f"- 近20日: 大盘 {f(r['cap20'])} / 等权 {f(r['eq20'])}")
    L.append("\n## 资金/情绪")
    L.append(f"- 今日成交额: {r['amt_today']:.0f} 亿  (5日均 {r['amt5']:.0f} / 20日均 {r['amt20']:.0f}, 5v20 {r['amt_chg']:+.1f}%)")
    L.append(f"- 全市场换手率中位数: {_pct(r['turn'], 2)}")
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
    # 数据充足性闸:空切片给 None 而不是 NaN,classify 拒判而不是静默投「分化」
    nothing = px.reindex(columns=["nope"])      # 一只票都算不出来的极端切片
    assert pct_above_ma(nothing, 20) == (None, 0)
    assert cap_vs_equal(nothing, mv, 5) == (None, None)
    assert median_turnover(pd.DataFrame({"turnover_rate": []})) is None
    bad, why, s = classify(float("nan"), float("nan"), None, None, None, 0.0, 0)
    assert bad == UNUSABLE_LABEL and s == 0 and why, (bad, s)
    assert not any(k in bad for k in ("上涨", "震荡", "下跌")), bad
    assert unusable_reason({"n_stocks": 1}) and unusable_reason({}) == ""
    print("selfcheck OK")


def main(argv=None) -> None:
    argv = sys.argv if argv is None else argv
    if "--selfcheck" in argv:
        _selfcheck()
    elif "--json" in argv:
        import json
        r = build()
        r["ind_top"] = r["ind_top"].reset_index().to_dict("records")
        r["ind_bot"] = r["ind_bot"].reset_index().to_dict("records")
        r["ind5_top"] = r["ind5_top"].reset_index().to_dict("records")
        print(json.dumps(r, ensure_ascii=False, default=str))
    else:
        print(render(build()))


if __name__ == "__main__":
    main()
