#!/usr/bin/env python3
"""中证A500 池内增强回测 —— 时点(PIT)成分 + 截面因子加权。

用法: .venv/bin/python scripts/a500_backtest.py [--mode exact|approx] [--factors full|pv|fund]
      [--top-k 50] [--lam 3] [--band 2] [--cost 0.0015] [--start ... --end ...]

宇宙:
  exact  = data/a500_membership.json 时点成分回放(2024-09-23 起可用; 初始名单=官方
           Wayback 快照, 事件与官方调样公告 PDF 逐项核对, 首尾对账 500/500)。
  approx = 当日 circ_mv top500 近似(用于 A500 发布前的长窗机制验证; 与真实成分重合
           率仅 ~63%, 结论只用于「因子在大盘池内是否有效」, 不用于对指数超额报数)。

口径:
  信号 = rebal 日(月末)收盘 compute_factor_panel (PIT);
  执行 = 次一交易日开盘(hfq), 停牌顺延 ≤9 天, 否则剔除重归一;
  成本 = 单边 --cost, 按 Σ|Δw| 换手计;
  基准 = 同宇宙 circ_mv 加权(bench_cw, 同口径含分红) + 官方 000510 价格指数
         + 官方TR近似(价格指数+成分市值加权股息率)。跑赢指数报数看官方TR行。
  注意 bench_cw ≠ 官方指数: 官方用自由流通市值加权, circ_mv 含国有等非自由流通
  股份, 本窗内两者年化差可达数个百分点 —— 对指数的超额一律以官方TR近似为准。

方案: bench_cw / ew500(等权对照) / fac_top(因子 top-K 等权, 买入带 band 缓冲)
      / fac_top_cw(同选股按市值加权) / fac_top_cw10(市值加权+单股10%上限, 最终配置)
      / fac_tilt(全池 circ_mv·exp(λ·z) 倾斜)。

结论(2026-07, 红队裁决后, 详见 docs/2026-07-16-a500-enhance.md): 头条数字
(exact 窗 fac_top_cw10 +23.4%/年)被对抗验证推翻——补回被静默截断的 2026-07 期
后 +12.6%/年; 剔 AI 月转 -2.5%/年; 唯一跨 regime 样本(2021-24) -3.5%/年;
honest DSR 0.40 < 0.95 闸; 20万资金整手约束下可行子集超额 ≈0。前瞻超额按
0~负理解, 小资金正确动作=直接买 A500 ETF。本引擎与时点成分数据的价值在于
口径与基础设施, 不在头条数字。
"""
import sys, json, argparse, sqlite3, time
from datetime import date, timedelta
from collections import defaultdict
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.factors.cross_sectional import (compute_factor_panel, FactorConfig,
                                            DEFAULT_FACTORS)
from quanti.backtest.overfit import deflated_sharpe_ratio, sharpe_per_obs

ROOT = Path(__file__).resolve().parent.parent
MARKET_DB = str(ROOT / "data/market.db")
ACCOUNT_DB = str(ROOT / "data/agent_bt.db")
MEMBERSHIP = ROOT / "data/a500_membership.json"
INDEX_CSV = ROOT / "data/a500_index_000510.csv"

SCHEMES = ["bench_cw", "ew500", "fac_top", "fac_top_cw", "fac_top_cw10", "fac_tilt"]
PV_FACTORS = ("momentum_3m", "momentum_6m", "reversal_1w",
              "realized_vol_20d", "turnover_20d")


# ---------------------------------------------------------------- membership
def load_membership():
    spec = json.loads(MEMBERSHIP.read_text())
    base = set(spec["base"])
    events = sorted(spec["events"], key=lambda e: e["effective_trade_date"])

    def members(d: date) -> frozenset:
        cur = set(base)
        for ev in events:
            if date.fromisoformat(ev["effective_trade_date"]) <= d:
                cur -= set(ev["out"]); cur |= set(ev["in"])
        return frozenset(cur)
    return members


def members_approx_factory(con):
    stocks = {r[0]: r for r in con.execute(
        "SELECT code, name, exchange, list_date, delist_date FROM stocks")}

    def members(d: date) -> frozenset:
        rows = con.execute(
            "SELECT code, circ_mv FROM daily_basic WHERE date=? AND circ_mv IS NOT NULL",
            (asof_basic_date(con, d),)).fetchall()
        ds = d.isoformat()
        cand = []
        for c, mv in rows:
            s = stocks.get(c)
            if not s:
                continue
            _, name, ex, ld, dd = s
            if ex not in ("SH", "SZ"):
                continue
            if ld and ld > (d - timedelta(days=365)).isoformat():
                continue
            if dd and dd <= ds:
                continue
            if name and ("ST" in name or "退" in name):
                continue
            cand.append((c, mv))
        cand.sort(key=lambda x: -x[1])
        return frozenset(c for c, _ in cand[:500])
    return members


# ---------------------------------------------------------------- data
def price_matrix(con, codes, start, end):
    q = f"""SELECT code, date, open*adj_factor AS px FROM daily_quotes
            WHERE date BETWEEN ? AND ? AND code IN ({','.join('?'*len(codes))})
            AND open > 0 AND volume > 0"""
    df = pd.read_sql_query(q, con, params=[start.isoformat(), end.isoformat(), *codes])
    return df.pivot(index="date", columns="code", values="px").sort_index()


def asof_basic_date(con, d):
    """daily_basic 偶有整天缺失, 回退到 ≤d 最近有数据的日期。"""
    row = con.execute("SELECT MAX(date) FROM daily_basic WHERE date<=?",
                      (d.isoformat(),)).fetchone()
    if not row or not row[0]:
        raise RuntimeError(f"daily_basic 在 {d} 前无数据")
    return row[0]


def circ_mv_at(con, codes, d):
    ds = asof_basic_date(con, d)
    q = f"""SELECT code, circ_mv FROM daily_basic
            WHERE date=? AND code IN ({','.join('?'*len(codes))})"""
    return dict(con.execute(q, [ds, *codes]).fetchall())


def monthly_rebal_dates(provider, start, end):
    tds = provider.get_trade_dates(start, end)
    by_m = defaultdict(list)
    for d in tds:
        by_m[(d.year, d.month)].append(d)
    return sorted(max(ds) for ds in by_m.values())


def first_open_after(opens, d, days=9):
    """d 起(含) days 天内每只股票第一个可交易开盘价 → Series[code]。"""
    sub = opens.loc[d.isoformat():(d + timedelta(days=days)).isoformat()]
    if sub.empty:
        return pd.Series(dtype=float)
    return sub.bfill().iloc[0]


# ---------------------------------------------------------------- weights
def scheme_weights(panel, mv, members, top_k=50, lam=3.0, band=2.0, held=None):
    """band: 已持仓股票 composite 排名仍在 top_k*band 内则保留(降换手)。"""
    out = {}
    mvs = pd.Series({c: mv.get(c, np.nan) for c in members}).dropna()
    if mvs.empty:
        return out
    out["bench_cw"] = mvs / mvs.sum()
    out["ew500"] = pd.Series(1.0 / len(members), index=sorted(members))
    if panel is not None and not panel.empty and "composite" in panel:
        comp = panel["composite"].dropna()
        comp = comp[comp.index.isin(members)]
        if len(comp) >= top_k:
            ranked = comp.sort_values(ascending=False)
            keep = []
            if held:
                band_set = set(ranked.head(int(top_k * band)).index)
                keep = [c for c in held if c in band_set]
            new = [c for c in ranked.index if c not in keep][:max(top_k - len(keep), 0)]
            sel = keep + new
            out["fac_top"] = pd.Series(1.0 / len(sel), index=sel)
            selmv = mvs[mvs.index.isin(sel)]
            if selmv.sum() > 0:
                out["fac_top_cw"] = selmv / selmv.sum()
                out["fac_top_cw10"] = cap_weights(out["fac_top_cw"], 0.10)
            z = comp.clip(-3, 3)
            common = mvs.index.intersection(z.index)
            tilt = mvs[common] * np.exp(lam * z[common])
            out["fac_tilt"] = tilt / tilt.sum()
    return out


def cap_weights(w, cap):
    """单股权重上限, 超额部分按市值比例分给未触顶者(迭代)。"""
    w = w.copy()
    for _ in range(20):
        over = w[w > cap]
        if over.empty:
            break
        excess = float((over - cap).sum())
        w[over.index] = cap
        room = w[w < cap]
        if room.empty:
            break
        w[room.index] += excess * room / room.sum()
    return w / w.sum()


def eff_n(w) -> float:
    return float(1.0 / (w ** 2).sum()) if len(w) else 0.0


# ---------------------------------------------------------------- engine
def run(args):
    db = Database(ACCOUNT_DB, market_db_path=MARKET_DB)
    db.initialize()
    provider = DataProvider(db)
    con = sqlite3.connect(f"file:{MARKET_DB}?mode=ro", uri=True)

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    rebal = monthly_rebal_dates(provider, start, end)
    members_fn = members_approx_factory(con) if args.mode == "approx" else load_membership()

    tds = provider.get_trade_dates(start, end + timedelta(days=20))
    def next_td(d):
        for t in tds:
            if t > d:
                return t
        return None

    all_codes = sorted(set().union(*[members_fn(rd) for rd in rebal]))
    print(f"rebal {len(rebal)} 期 {rebal[0]}~{rebal[-1]}, 宇宙并集 {len(all_codes)} 只", flush=True)
    opens = price_matrix(con, all_codes, start, end + timedelta(days=20))

    cfg = None
    if args.factors == "pv":
        cfg = FactorConfig(factors={k: v for k, v in DEFAULT_FACTORS.items()
                                    if k in PV_FACTORS})
    elif args.factors == "fund":
        cfg = FactorConfig(factors={k: v for k, v in DEFAULT_FACTORS.items()
                                    if k not in PV_FACTORS})

    rets = {s: [] for s in SCHEMES}
    turns = {s: [] for s in SCHEMES}
    effns = {s: [] for s in SCHEMES}
    prev = {s: None for s in SCHEMES}   # (target_w, per-stock ret, port_ret)
    period_dates, cw_dvs = [], []

    for i, rd in enumerate(rebal[:-1]):
        t0 = time.time()
        mem = frozenset(c for c in members_fn(rd) if c in opens.columns)
        panel = compute_factor_panel(provider, db, sorted(mem), as_of=rd, config=cfg)
        mv = circ_mv_at(con, sorted(mem), rd)
        held = list(prev["fac_top"][0].index) if prev.get("fac_top") else None
        W = scheme_weights(panel, mv, mem, top_k=args.top_k, lam=args.lam,
                           band=args.band, held=held)

        d_exec, d_next = next_td(rd), next_td(rebal[i + 1])
        if d_exec is None or d_next is None:
            break
        p0 = first_open_after(opens, d_exec)
        p1 = first_open_after(opens, d_next)
        pr = (p1 / p0 - 1.0).dropna()
        if pr.empty:
            # fail-loud: 静默丢期曾把 2026-07 大跌月吞掉, 头条虚高 ~10pp/yr(红队复核)
            print(f"!! WARNING: {rd} 起的期次因 {d_next} 无行情被截断 —— "
                  f"窗口实际止于 {rebal[i-1] if i else start}, 头条数字不含该期")
            break
        period_dates.append((rd, d_exec, d_next))

        dv_rows = con.execute(
            f"""SELECT code, circ_mv, dv_ratio FROM daily_basic WHERE date=?
                AND code IN ({','.join('?'*len(mem))})""",
            [asof_basic_date(con, rd), *sorted(mem)]).fetchall()
        tot_mv = sum(r[1] for r in dv_rows if r[1])
        cw_dvs.append(sum((r[1] or 0) * (r[2] or 0) for r in dv_rows) / tot_mv
                      if tot_mv else 0.0)

        for s in SCHEMES:
            w = W.get(s)
            if w is None or len(w) == 0:
                rets[s].append(0.0); turns[s].append(0.0); effns[s].append(0.0)
                prev[s] = None
                continue
            valid = w[w.index.isin(pr.index)]
            if valid.sum() <= 0:
                rets[s].append(0.0); turns[s].append(0.0); effns[s].append(0.0)
                prev[s] = None
                continue
            valid = valid / valid.sum()
            R = float((valid * pr[valid.index]).sum())
            if prev[s] is None:
                turn = 1.0
            else:
                pw, ppr, pR = prev[s]
                drifted = pw * (1 + ppr.reindex(pw.index).fillna(0.0)) / (1 + pR)
                allc = valid.index.union(drifted.index)
                turn = float((valid.reindex(allc).fillna(0) -
                              drifted.reindex(allc).fillna(0)).abs().sum()) / 2
            rets[s].append(R - 2 * turn * args.cost)
            turns[s].append(turn)
            effns[s].append(eff_n(valid))
            prev[s] = (valid, pr, R)
        print(f"{rd} mem={len(mem)} " +
              " ".join(f"{s}={rets[s][-1]*100:+.2f}%" for s in SCHEMES) +
              f" ({time.time()-t0:.1f}s)", flush=True)

    # ---- 官方指数对照(开盘-开盘同口径)
    idx_rets = None
    if INDEX_CSV.exists():
        idx = pd.read_csv(INDEX_CSV)
        idx["日期"] = pd.to_datetime(idx["日期"]).dt.date.astype(str)
        idx = idx.set_index("日期")["开盘"].astype(float)
        idx_rets = []
        for rd, d_exec, d_next in period_dates:
            try:
                idx_rets.append(idx.loc[d_next.isoformat()] / idx.loc[d_exec.isoformat()] - 1.0)
            except KeyError:
                idx_rets.append(np.nan)
    else:
        print(f"!! {INDEX_CSV} 不存在, 跳过官方指数对照 "
              "(akshare stock_zh_index_hist_csindex(symbol='000510') 可重建)")

    def stats(r):
        r = np.asarray(r, dtype=float)
        nav = np.cumprod(1 + r); tot = nav[-1] - 1; n = len(r)
        ann = (1 + tot) ** (12 / n) - 1
        sh = r.mean() / r.std(ddof=0) * np.sqrt(12) if r.std(ddof=0) > 0 else 0
        pk = np.maximum.accumulate(nav); mdd = float(((nav - pk) / pk).min())
        return tot, ann, sh, mdd

    print("\n=== 结果 ===")
    truncated = len(period_dates) < len(rebal) - 1
    report = {"args": vars(args), "n_periods": len(period_dates),
              "truncated": truncated,
              "period_dates": [[str(x) for x in p] for p in period_dates]}
    if truncated:
        print(f"!! 窗口被截断: 请求 {len(rebal)-1} 期, 实际 {len(period_dates)} 期")
    for s in SCHEMES:
        tot, ann, sh, mdd = stats(rets[s])
        print(f"{s:10s}: 总{tot*100:+7.1f}% 年化{ann*100:+6.2f}% 夏普{sh:5.2f} "
              f"回撤{mdd*100:6.1f}% 均换手{np.mean(turns[s][1:] or [0])*100:3.0f}% "
              f"effN{np.mean([x for x in effns[s] if x] or [0]):5.0f}")
        report[s] = dict(total=tot, ann=ann, sharpe=sh, mdd=mdd,
                         rets=[float(x) for x in rets[s]],
                         turns=[float(x) for x in turns[s]],
                         eff_n=float(np.mean([x for x in effns[s] if x] or [0])))

    variants = [x for x in SCHEMES if x != "bench_cw"]
    if idx_rets is not None:
        v = np.asarray(idx_rets, float)
        if np.isfinite(v).sum() >= len(v) - 1:
            vv = np.nan_to_num(v)
            tot, ann, _, _ = stats(vv)
            print(f"{'official':10s}: 总{tot*100:+7.1f}% 年化{ann*100:+6.2f}% (价格指数,不含分红)")
            report["official"] = dict(total=tot, ann=ann, rets=[float(x) for x in vv])
            b = np.asarray(rets["bench_cw"])
            corr = float(np.corrcoef(b[np.isfinite(v)], v[np.isfinite(v)])[0, 1])
            print(f"自建基准 vs 官方指数: 月收益相关 {corr:.4f}, "
                  f"年化差 {(report['bench_cw']['ann']-ann)*100:+.2f}%")
            report["bench_vs_official_corr"] = corr

            dv = np.asarray(cw_dvs[:len(period_dates)], float) / 100.0 / 12.0
            tr = vv + dv
            tot, ann, _, _ = stats(tr)
            print(f"\nofficial_TR近似: 年化{ann*100:+6.2f}% "
                  f"(价格指数+股息率, 均值{np.mean(cw_dvs):.2f}%/yr) —— 对指数超额以此行为准")
            report["official_tr"] = dict(total=tot, ann=ann, rets=[float(x) for x in tr])
            for s in variants:
                e = np.asarray(rets[s], float) - tr
                ann_e = ((np.prod(1 + np.asarray(rets[s])) ** (12 / len(tr))) -
                         (np.prod(1 + tr) ** (12 / len(tr))))
                te = e.std(ddof=0) * np.sqrt(12)
                ir = e.mean() / e.std(ddof=0) * np.sqrt(12) if e.std(ddof=0) > 0 else 0
                print(f"  {s:10s} vs 官方TR: 超额年化{ann_e*100:+6.2f}% TE{te*100:5.2f}% "
                      f"IR{ir:5.2f} 月胜率{(e>0).mean()*100:.0f}%")
                report[s].update(excess_ann_tr=float(ann_e), ir_tr=float(ir), te_tr=float(te))

    b = np.asarray(rets["bench_cw"], float)
    n = len(b)
    variant_sharpes = [sharpe_per_obs(list(np.asarray(rets[s], float) - b))
                       for s in variants]
    print("\n--- 相对自建基准(bench_cw) ---")
    for s, spo in zip(variants, variant_sharpes):
        e = np.asarray(rets[s], float) - b
        ann_e = ((np.prod(1 + np.asarray(rets[s])) ** (12 / n)) -
                 (np.prod(1 + b) ** (12 / n)))
        te = e.std(ddof=0) * np.sqrt(12)
        ir = e.mean() / e.std(ddof=0) * np.sqrt(12) if e.std(ddof=0) > 0 else 0
        d = deflated_sharpe_ratio(list(e), variant_sharpes * max(1, args.trials // len(variants)))
        print(f"{s:10s}: 超额年化{ann_e*100:+6.2f}% TE{te*100:5.2f}% IR{ir:5.2f} "
              f"DSR{d['dsr']:.3f} 月胜率{(e>0).mean()*100:.0f}%")
        report[s].update(excess_ann=float(ann_e), te=float(te), ir=float(ir),
                         dsr=float(d["dsr"]))

    out = ROOT / f"data/a500_bt_{args.mode}_{args.factors}_k{args.top_k}_lam{args.lam}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print("saved:", out)
    db.close(); con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="A500 池内增强回测")
    ap.add_argument("--start", default="2024-09-23")
    ap.add_argument("--end", default="2026-07-15")
    ap.add_argument("--mode", choices=["exact", "approx"], default="exact")
    ap.add_argument("--factors", choices=["full", "pv", "fund"], default="fund")
    ap.add_argument("--top-k", type=int, default=50)
    ap.add_argument("--lam", type=float, default=3.0)
    ap.add_argument("--band", type=float, default=2.0)
    ap.add_argument("--cost", type=float, default=0.0015)
    ap.add_argument("--trials", type=int, default=12,
                    help="研究全程尝试过的配置数(多重检验入账), 默认12=本次研究实际试验数")
    run(ap.parse_args())
