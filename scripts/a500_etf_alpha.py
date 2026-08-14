#!/usr/bin/env python3
"""中证A500(000510) 指数层 alpha 挖掘 —— 单标的择时/波动管理/隔夜/日历候选严验。

问题: 「挖掘 A500 ETF 指数的 alpha」= 在 A500 这个广基指数单标的上, 有没有
能净跑赢「买入持有 ETF」的择时/波动管理/日内-隔夜/日历 overlay?

与 scripts/a500_backtest.py 的区别: 那个做**池内选股**(已被红队证否); 本脚本做
**指数层单标的择时**(此前全部择时结论跑在个股/横截面, 从未直接在 A500 标的上验)。

口径(诚实, 对买入持有不放水):
  - 研究对象 = 官方 000510 指数日线(CSI 回溯重构历史, 2020-01~, 含 2021顶/2022熊/
    2024踩踏+修复/2025-26 AI牛, regime 足够杂)。ETF 只是跟踪它+跟踪误差+费率的可交易壳。
  - TR 近似: 在场日给股息日累计(默认 2.6%/yr); 空仓日给现金收益(默认 1.5%/yr)。
    => 空仓的择时策略会如实少吃股息(对买入持有不放水)。
  - 成本: 单边 --cost bps 作用于 |Δ敞口|。ETF 免印花税, 佣金≈1~3bps → 默认 5bps 偏保守。
  - 敞口 e[t]∈[0,1] 仅用 t-1 及之前信息决定(无前视), 作用于 t 日收益; long-only 不加杠杆
    (散户 ETF 不易融资), target_vol 封顶 1.0。

闸(复用 quanti/backtest/overfit.py):
  - 净跑赢: full 与 OOS 两段都要净超额 > 0(超额=策略日收益 - 买入持有日收益)。
  - DSR: 超额序列的 deflated Sharpe(扣本次全部候选数的多重检验), ≥0.95 才算显著。
  - PBO(CSCV): IS 最优候选滑到 OOS 后一半的概率, ≤~0.2 才算非过拟合。

用法: .venv/bin/python scripts/a500_etf_alpha.py [--cost 5] [--div 2.6] [--cash 1.5]
"""
import sys
import argparse
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quanti.backtest.overfit import (deflated_sharpe_ratio, sharpe_per_obs,
                                     pbo_cscv, probabilistic_sharpe_ratio)

ROOT = Path(__file__).resolve().parent.parent
INDEX_CSV = ROOT / "data/a500_index_000510.csv"
TD_PER_YR = 242
OOS_START = "2023-07-01"   # 前半 IS / 后半 OOS 分界(样本约一半一半)


def load_index():
    df = pd.read_csv(INDEX_CSV)
    df = df.rename(columns={"日期": "d", "开盘": "o", "最高": "h",
                            "最低": "l", "收盘": "c", "成交量": "v"})
    df["d"] = pd.to_datetime(df["d"])
    df = df[df["d"].dt.dayofweek < 5]
    # 去节假日填充(与前一行 OHLCV 完全相同)+ 去无效价
    dup = (df[["o", "h", "l", "c", "v"]].shift() == df[["o", "h", "l", "c", "v"]]).all(axis=1)
    df = df[~dup]
    df = df[(df["o"] > 0) & (df["c"] > 0)].reset_index(drop=True)
    return df[["d", "o", "c"]]


def build_returns(df, div, cash):
    """返回 DataFrame: index=交易日, 列 cc/on/id(收盘-收盘/隔夜/日内 原始收益),
    以及每日股息累计 div_d、现金日收益 cash_d。"""
    c, o = df["c"].values, df["o"].values
    cc = np.full(len(df), np.nan)
    cc[1:] = c[1:] / c[:-1] - 1.0
    on = np.full(len(df), np.nan)
    on[1:] = o[1:] / c[:-1] - 1.0        # 昨收→今开
    idr = c / o - 1.0                                                    # 今开→今收
    out = pd.DataFrame({"d": df["d"], "cc": cc, "on": on, "id": idr}).iloc[1:].reset_index(drop=True)
    out["div_d"] = (1 + div / 100.0) ** (1 / TD_PER_YR) - 1
    out["cash_d"] = (1 + cash / 100.0) ** (1 / TD_PER_YR) - 1
    return out


# --------------------------------------------------------------- 候选策略
def ma(x, n):
    return pd.Series(x).rolling(n).mean().values


def exposures(df):
    """返回 {name: e[]}，e[t]∈[0,1] 只用 t-1 及之前信息(shift(1) 保证无前视)。"""
    c = df["close"].values
    n = len(c)
    E = {}
    E["buy_hold"] = np.ones(n)
    for w in (20, 50, 120, 200):
        sig = (c > ma(c, w)).astype(float)
        E[f"ma{w}"] = pd.Series(sig).shift(1).fillna(0).values
    for f, s in ((20, 60), (50, 200)):
        sig = (ma(c, f) > ma(c, s)).astype(float)
        E[f"dma{f}_{s}"] = pd.Series(sig).shift(1).fillna(0).values
    for w in (120, 242):
        past = pd.Series(c).pct_change(w).values
        E[f"tsmom{w}"] = pd.Series((past > 0).astype(float)).shift(1).fillna(0).values
    # 波动目标: e = min(1, target/realized_vol_20d), target=全期实现波动中位数 → 均敞口≈1
    rv = pd.Series(df["cc_raw"].values).rolling(20).std().values
    tgt = np.nanmedian(rv)
    tv = np.clip(tgt / rv, 0, 1.0)
    E["target_vol"] = pd.Series(tv).shift(1).fillna(0).values
    # 逆波动(不封中位, 归一到均敞口≈1 后封顶)
    iv = 1.0 / rv
    iv = iv / np.nanmean(iv)
    E["inv_vol"] = pd.Series(np.clip(iv, 0, 1.0)).shift(1).fillna(0).values
    # 月末效应: 每月最后 1 个交易日 + 次月前 3 个交易日在场
    d = df["d"]
    tom = np.zeros(n)
    month = d.dt.to_period("M")
    is_last = month.values != np.append(month.values[1:], month.values[-1])
    idx_last = np.where(is_last)[0]
    for j in idx_last:
        for k in range(j, min(j + 4, n)):   # 月末当日 + 次月前3日
            tom[k] = 1.0
    E["turn_of_month"] = pd.Series(tom).shift(1).fillna(0).values
    return E


def apply_strategy(df, e, cost_bps, mode="cc"):
    """把敞口序列 e 作用到收益: 在场吃 mode 收益+股息, 空仓吃现金; 成本按 |Δe|。"""
    r_mkt = df[mode].values + df["div_d"].values          # 在场总收益(含股息)
    r_cash = df["cash_d"].values
    de = np.abs(np.diff(e, prepend=e[0]))
    cost = de * cost_bps / 1e4
    return e * r_mkt + (1 - e) * r_cash - cost


def overnight_intraday(df, cost_bps):
    """隔夜/日内定式: 每日两次换手(买入/卖出), 成本 ×2/日。"""
    on = df["on"].values + df["div_d"].values   # 隔夜也吃一份股息(粗略)
    idr = df["id"].values
    daily_cost = 2 * cost_bps / 1e4
    return {"overnight_only": on - daily_cost + 0,   # 简化: 每日两边换手
            "intraday_only": idr - daily_cost}


# --------------------------------------------------------------- 统计
def stats(r):
    r = np.asarray(r, float)
    r = r[~np.isnan(r)]
    nav = np.cumprod(1 + r)
    n = len(r)
    cagr = nav[-1] ** (TD_PER_YR / n) - 1
    sh = r.mean() / r.std(ddof=0) * np.sqrt(TD_PER_YR) if r.std(ddof=0) > 0 else 0
    pk = np.maximum.accumulate(nav)
    mdd = float(((nav - pk) / pk).min())
    return dict(cagr=cagr, sharpe=sh, mdd=mdd, n=n)


def turnover(e):
    return float(np.abs(np.diff(e, prepend=e[0])).sum() / (len(e) / TD_PER_YR))


def run(args):
    df0 = load_index()
    r = build_returns(df0, args.div, args.cash)
    r["close"] = df0["c"].values[1:]        # 对齐 build_returns 去了首行
    r["cc_raw"] = r["cc"]                    # 供波动计算(原始收盘收益)
    E = exposures(r)

    oos_mask = (r["d"] >= OOS_START).values
    is_mask = ~oos_mask

    # 所有 cc-based 择时候选净收益
    series = {}
    turns = {}
    for name, e in E.items():
        series[name] = apply_strategy(r, e, args.cost, mode="cc")
        turns[name] = turnover(e)
    # 隔夜/日内(换手口径不同, 单列)
    oi = overnight_intraday(r, args.cost)
    series.update(oi)
    turns.update({"overnight_only": TD_PER_YR * 2, "intraday_only": TD_PER_YR * 2})

    bh = series["buy_hold"]
    names = list(series.keys())

    # ---- 报表
    print(f"样本 {r['d'].iloc[0].date()}~{r['d'].iloc[-1].date()} "
          f"共 {len(r)} 交易日; IS<{OOS_START}={is_mask.sum()} OOS>={OOS_START}={oos_mask.sum()}")
    print(f"成本 {args.cost}bps/边, 股息 {args.div}%/yr, 现金 {args.cash}%/yr\n")
    hdr = f"{'策略':14s} {'全CAGR':>7s} {'全夏普':>6s} {'全回撤':>7s} " \
          f"{'超额full':>8s} {'超额OOS':>8s} {'OOS夏普':>6s} {'换手/yr':>7s}"
    print(hdr)
    print("-" * len(hdr))

    excess_full = {}   # 每日超额序列(用于 DSR)
    rows = []
    for name in names:
        s = series[name]
        st = stats(s)
        st_oos = stats(s[oos_mask])
        exc = s - bh
        excess_full[name] = exc
        ann_exc_full = stats(s)["cagr"] - stats(bh)["cagr"]
        ann_exc_oos = stats(s[oos_mask])["cagr"] - stats(bh[oos_mask])["cagr"]
        rows.append((name, st, st_oos, ann_exc_full, ann_exc_oos))
        print(f"{name:14s} {st['cagr']*100:+6.2f}% {st['sharpe']:6.2f} "
              f"{st['mdd']*100:6.1f}% {ann_exc_full*100:+7.2f}% {ann_exc_oos*100:+7.2f}% "
              f"{st_oos['sharpe']:6.2f} {turns[name]:7.1f}")

    # ---- DSR: 对每个候选的「超额序列」扣多重检验(trials=全部候选)
    variants = [n for n in names if n != "buy_hold"]
    trial_sh = [sharpe_per_obs(list(excess_full[n])) for n in variants]
    print("\n--- 超额 vs 买入持有 的显著性(DSR 扣多重检验, PBO 过拟合) ---")
    print(f"{'策略':14s} {'超额夏普':>7s} {'DSR':>6s} {'PSR>0':>6s} {'判定':>8s}")
    survivors = []
    for name in variants:
        exc = excess_full[name]
        d = deflated_sharpe_ratio(list(exc), trial_sh)
        psr = probabilistic_sharpe_ratio(list(exc))
        exc_sh = sharpe_per_obs(list(exc)) * np.sqrt(TD_PER_YR)
        full_ok = stats(series[name])["cagr"] > stats(bh)["cagr"]
        oos_ok = stats(series[name][oos_mask])["cagr"] > stats(bh[oos_mask])["cagr"]
        passed = full_ok and oos_ok and d["dsr"] >= 0.95
        if passed:
            survivors.append(name)
        verdict = "★幸存" if passed else ("双净超" if (full_ok and oos_ok) else "否")
        print(f"{name:14s} {exc_sh:+6.2f} {d['dsr']:6.3f} {psr:6.3f} {verdict:>8s}")

    # ---- PBO: 所有 cc-based 候选的净收益矩阵(含 buy_hold)
    cc_names = [n for n in names if n not in ("overnight_only", "intraday_only")]
    M = np.column_stack([series[n] for n in cc_names])
    pbo = pbo_cscv(M, n_splits=16)
    print(f"\nPBO(CSCV, {len(cc_names)} 个 cc 候选含买入持有): {pbo['pbo']:.3f} "
          f"(IS 最优滑到 OOS 后一半的概率; ≤0.2 才算非过拟合)")

    print("\n=== 结论 ===")
    print(f"买入持有(TR近似): 全 CAGR {stats(bh)['cagr']*100:+.2f}% 夏普 {stats(bh)['sharpe']:.2f} "
          f"回撤 {stats(bh)['mdd']*100:.1f}%; OOS CAGR {stats(bh[oos_mask])['cagr']*100:+.2f}%")
    if survivors:
        print(f"★ 幸存候选(full&OOS 双净超 + DSR≥0.95): {survivors}")
    else:
        print("✗ 无候选能 full&OOS 双净超买入持有并过 DSR≥0.95 闸 —— 指数层择时无可实盘 alpha。")


def _selftest():
    """无前视 + 恒等式自检。"""
    # 造 20 天数据: close 单调涨, 隔夜=日内各一半
    df = pd.DataFrame({"d": pd.bdate_range("2024-01-01", periods=20),
                       "o": np.linspace(100, 118, 20),
                       "c": np.linspace(101, 119, 20)})
    r = build_returns(df, 0.0, 0.0)
    # 恒等式: (1+on)(1+id) ≈ 1+cc(用同日 o,c 与昨 c)
    lhs = (1 + r["on"]) * (1 + r["id"]) - 1
    assert np.allclose(lhs.values, r["cc"].values, atol=1e-9), "隔夜×日内 ≠ 收盘收益"
    # 无前视: buy_hold 敞口恒 1; ma20 首 20 日敞口应为 0(未够窗口 + shift)
    r["close"] = df["c"].values[1:]
    r["cc_raw"] = r["cc"]
    E = exposures(r)
    assert (E["buy_hold"] == 1).all()
    assert E["ma20"][:20].sum() == 0, "ma20 早期应无仓(无前视)"
    # apply: 全 0 成本、无股息现金时 buy_hold 净收益==cc
    s = apply_strategy(r, E["buy_hold"], 0.0, "cc")
    assert np.allclose(s, r["cc"].values, atol=1e-12)
    print("selftest OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost", type=float, default=5.0, help="单边成本 bps")
    ap.add_argument("--div", type=float, default=2.6, help="股息率 pct/yr")
    ap.add_argument("--cash", type=float, default=1.5, help="空仓现金收益 pct/yr")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
    else:
        run(a)
