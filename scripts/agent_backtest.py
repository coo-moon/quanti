#!/usr/bin/env python3
"""Agent 路径选股回测(简化版)—— 多策略信号 + 横截面因子融合 → top-K。

用法:.venv/bin/python scripts/agent_backtest.py [--start 2021-08-01 --end 2026-06-24 --recent N]

对应 runtime._run_cycle_body 的选股核心(_compute_fused_candidates / fuse_buy_signals),
作为月度调仓回测跑全 5 年,验证 agent 路径横截面选股 vs 被动等权基准。

简化(相对真实 agent runtime,标注以免误读):
  - 固定策略集等权(去 selector 动态选——它靠 15 天 OOS Sharpe softmax,审计判为噪声)
  - 无 LLM 情绪(sentiment_blend=0,与 agent 默认一致)
  - 月度等权手算净值(无逐日 T+1/涨跌停/止损摩擦 → 净值偏乐观一点点,选股 alpha 方向准)
PIT 正确:collect_signals_per_strategy(end=rd)、compute_factor_panel(as_of=rd);财报按公告日。
临时 account 库 data/agent_bt.db(可删,不碰 paper/live);行情读 data/market.db。
结论见 memory: stoploss-regime-research-findings(2026-06-26)。
"""
import sys
import time
import argparse
from datetime import date, timedelta
from collections import defaultdict
import numpy as np

sys.path.insert(0, ".")
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.factors.cross_sectional import compute_factor_panel
from quanti.agent.signal_pipeline import (
    collect_signals_per_strategy, fuse_buy_signals, filter_by_threshold)
from quanti.strategy.loader import StrategyLoader

ACCOUNT_DB = "data/agent_bt.db"   # 临时 account 库(建空表,可删,不碰 paper/live)
MARKET_DB = "data/market.db"
N_CAND = 100          # 流动性候选池(= runtime no_screener_take 默认 100,大盘偏)
TOP_K = 10            # 每月选 top-K
FACTOR_BLEND = 0.5    # agent 默认:一半策略投票,一半因子模型
THRESHOLD = 0.30
STRAT_NAMES = ["ma_cross", "macd_cross", "kdj_cross", "bollinger_band"]
COST_PER_TURN = 0.003  # 0.3% 单边换手(佣金+印花+滑点)


def monthly_rebal_dates(provider, start, end):
    tds = provider.get_trade_dates(start, end)
    by_m = defaultdict(list)
    for d in tds:
        by_m[(d.year, d.month)].append(d)
    return sorted(max(ds) for ds in by_m.values())


def liquidity_pool(provider, rd, n):
    adv = provider.get_adv20_map(rd - timedelta(days=40), rd)
    ranked = sorted([c for c, v in adv.items() if v and v > 0],
                    key=lambda c: adv[c], reverse=True)
    return ranked[:n]


def fresh_strategies(loader):
    """每月新建干净实例,避免 on_bar 价格序列跨月累积。"""
    out = []
    for name in STRAT_NAMES:
        cls = type(loader[name])
        s = cls()
        s.init({})
        out.append(s)
    return out


def select_holdings(provider, db, loader, candidates, rd):
    if not candidates:
        return []
    strats = fresh_strategies(loader)
    pairs = [(s, 1.0 / len(strats)) for s in strats]
    per_strat, weights = collect_signals_per_strategy(
        pairs, candidates, provider, end=rd)
    panel = compute_factor_panel(provider, db, candidates, as_of=rd)
    fused = fuse_buy_signals(per_strat, weights,
                             factor_panel=panel, factor_blend=FACTOR_BLEND)
    fused = filter_by_threshold(fused, threshold=THRESHOLD)
    holdings = [c.code for c in fused[:TOP_K]]
    # 信号不足 K → 用因子 composite 补足(纯因子兜底)
    if len(holdings) < TOP_K and panel is not None and "composite" in panel.columns:
        comp = panel["composite"].dropna().sort_values(ascending=False)
        for c in comp.index:
            if c not in holdings:
                holdings.append(c)
                if len(holdings) >= TOP_K:
                    break
    return holdings


def period_returns(provider, codes, d0, d1):
    """等权 codes 从 d0→d1 的 hfq 收益率(每只首尾 close 比)。"""
    rets = []
    for code in codes:
        bars = provider.get_daily_bars(code, d0, d1)
        if len(bars) >= 2 and bars[0].close > 0:
            rets.append(bars[-1].close / bars[0].close - 1.0)
    return float(np.mean(rets)) if rets else 0.0, len(rets)


def stats(monthly_returns):
    if not monthly_returns:
        return dict(total=0, ann=0, ann_vol=0, sharpe=0, mdd=0, n_months=0)
    r = np.array(monthly_returns)
    nav = np.cumprod(1 + r)
    total = nav[-1] - 1
    n = len(r)
    ann = (1 + total) ** (12.0 / n) - 1 if total > -1 else -1
    ann_vol = r.std(ddof=0) * np.sqrt(12)
    sharpe = (r.mean() / r.std(ddof=0) * np.sqrt(12)) if r.std(ddof=0) > 0 else 0
    peak = np.maximum.accumulate(nav)
    mdd = float(((nav - peak) / peak).min())
    return dict(total=total, ann=ann, ann_vol=ann_vol, sharpe=sharpe, mdd=mdd, n_months=n)


def main():
    ap = argparse.ArgumentParser(description="Agent 路径选股回测(简化版)")
    ap.add_argument("--start", default="2021-08-01")
    ap.add_argument("--end", default="2026-06-24")
    ap.add_argument("--recent", type=int, default=0, help="只跑最近N个月(小测)")
    args = ap.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    db = Database(ACCOUNT_DB, market_db_path=MARKET_DB)
    db.initialize()
    provider = DataProvider(db)
    loader = {s.name: s for s in StrategyLoader().load_directory("strategies")}

    rebal = monthly_rebal_dates(provider, start, end)
    if args.recent:
        rebal = rebal[-args.recent:]
    print(f"调仓月数={len(rebal)} 起{rebal[0]} 止{rebal[-1]}", flush=True)

    nav = bench_nav = 1.0
    agent_rets, bench_rets = [], []
    prev_hold, prev_pool, prev_rd = None, None, None

    for rd in rebal:
        t = time.time()
        pool = liquidity_pool(provider, rd, N_CAND)
        hold = select_holdings(provider, db, loader, pool, rd)
        if prev_hold is not None:
            ar, _ = period_returns(provider, prev_hold, prev_rd, rd)
            br, _ = period_returns(provider, prev_pool, prev_rd, rd)
            a_turn = 1.0 - len(set(prev_hold) & set(hold)) / max(len(prev_hold), 1)
            b_turn = 1.0 - len(set(prev_pool) & set(pool)) / max(len(prev_pool), 1)
            ar *= (1 - a_turn * COST_PER_TURN)
            br *= (1 - b_turn * COST_PER_TURN)
            nav *= (1 + ar)
            bench_nav *= (1 + br)
            agent_rets.append(ar)
            bench_rets.append(br)
            print(f"{rd} hold={len(hold)} pool={len(pool)} "
                  f"agent={ar*100:+.2f}% bench={br*100:+.2f}% "
                  f"NAV={nav:.3f} bench={bench_nav:.3f} ({time.time()-t:.1f}s)", flush=True)
        else:
            print(f"{rd} 首月 选{len(hold)}只(池{len(pool)}) 不结算 ({time.time()-t:.1f}s)", flush=True)
        prev_hold, prev_pool, prev_rd = hold, pool, rd

    a, b = stats(agent_rets), stats(bench_rets)
    print("\n=== 结果 ===")
    print(f"Agent选股 : 总{a['total']*100:+.1f}% 年化{a['ann']*100:+.2f}% "
          f"夏普{a['sharpe']:.2f} 回撤{a['mdd']*100:.1f}% ({a['n_months']}月)")
    print(f"等权池基准: 总{b['total']*100:+.1f}% 年化{b['ann']*100:+.2f}% "
          f"夏普{b['sharpe']:.2f} 回撤{b['mdd']*100:.1f}%")
    print(f"选股超额(年化): {(a['ann']-b['ann'])*100:+.2f}%")
    db.close()


if __name__ == "__main__":
    main()
