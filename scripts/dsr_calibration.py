#!/usr/bin/env python3
"""DSR 选股门校准回测 —— 对着历史决策验 DSR 是否校准正确 + 定 dsr_min 默认。

为什么:selector 的 DSR 门(quanti/agent/selector.py,PR #104)默认关。启用前要确认
  (1) DSR 估计器在真实数据上行为合理(不是恒 0 / 恒 1,分位有梯度);
  (2) 通过门(DSR 高)的赢家是否真比被门掉的(DSR 低)更能延续到「选择点之后」的
      样本外 —— 若无区分力,门就是摆设,该保持关闭并把 dsr_min 定在保守统计值。

做法(严格 PIT,不看未来):对历史每个月末 d:
  - ADV top-N 池(as_of=d)
  - StrategySelector.evaluate(Goal(), pool, as_of=d) —— 复用**生产同一条** WF 评估路径
  - 赢家 DSR = StrategySelector._winner_dsr(ranking[0], ranking, True) —— 生产同一函数
  - 前瞻验证:top-K 各策略在 (d, d+fwd] 的真·OOS 收益(严格晚于选择点 d)。
    门关=softmax(按 OOS 夏普集中,≈押赢家);门开=top-K 等权(命中后的退路)。
    门有用 ⇔ 低 DSR 月里 等权 fwd > softmax fwd(集中押注反被噪声反噬)。

输出:DSR 分布分位 + 前瞻持续性分桶 + dsr_min 扫描(各阈值下的前瞻收益)+ 推荐默认。
另把逐月明细写 data/dsr_calib_rows.json 供复算。

用法:
  .venv/bin/python scripts/dsr_calibration.py \
      [--start 2022-01-01 --end 2026-06-01 --pool 100 --step-months 1 \
       --fwd-days 21 --max-dates 0]
用临时账户库 data/dsr_calib.db + 只读行情库 data/market.db。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, ".")

import numpy as np

from quanti.agent.goal import Goal
from quanti.agent.params import resolve_params
from quanti.agent.selector import StrategySelector, _MIN_OOS_TRADES
from quanti.backtest.engine import BacktestEngine
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.risk.manager import RiskConfig, RiskManager
from quanti.risk.protections import ProtectionManager
from quanti.strategy.loader import StrategyLoader

MARKET_DB = "data/market.db"
ACCOUNT_DB = "data/dsr_calib.db"
ROWS_OUT = "data/dsr_calib_rows.json"
CAL_CODE = "000001"  # 平安银行:1991 上市,做交易日历代理


def month_end_dates(provider, start: date, end: date, step_months: int) -> list[date]:
    """区间内每(step_months)个月的最后一个交易日。"""
    bars = provider.get_daily_bars(CAL_CODE, start, end)
    days = [b.date for b in bars]
    if not days:
        return []
    ends: list[date] = []
    for i, d in enumerate(days):
        if i + 1 >= len(days) or days[i + 1].month != d.month:
            ends.append(d)
    return ends[::step_months] if step_months > 1 else ends


def adv_pool(provider, d: date, n: int) -> list[str]:
    """ADV top-N 流动性池(as_of=d)—— 最中立的校准池,隔离 screener 噪声。"""
    adv = provider.get_adv20_map(d - timedelta(days=40), d)
    ranked = sorted((c for c, v in adv.items() if v and v > 0),
                    key=lambda c: adv[c], reverse=True)
    return ranked[:n]


def forward_return(engine, strat, cfg, pool, d: date,
                   warmup_days: int, fwd_days: int) -> float:
    """一个策略在 (d, d+fwd] 的实现收益 —— 严格晚于选择点 d 的真·OOS。
    暖机尾在 d 之前(指标预热),只取 d 之后的净值段。无成交→持币→0。"""
    inst = type(strat)()
    inst.init(dict(cfg))
    try:
        bt = engine.clone().run(strategy=inst, codes=pool,
                                start=d - timedelta(days=warmup_days),
                                end=d + timedelta(days=fwd_days))
    except Exception:
        return 0.0
    ec = bt.equity_curve
    if ec is None or len(ec) == 0:
        return 0.0
    fwd = ec[[(dt > d) and (dt <= d + timedelta(days=fwd_days)) for dt in ec.index]]
    if len(fwd) < 2:
        return 0.0
    return float(fwd.iloc[-1] / fwd.iloc[0] - 1.0)


def softmax_weights(top, min_trades: int) -> list[float]:
    """精确复刻 pick_topk 的资本权重:OOS 夏普(<min_trades 归零)→ temp=1 softmax。"""
    sharpes = []
    for ev in top:
        s = (ev.oos_sharpe if ev.oos_trades >= min_trades else 0.0) if ev.n_folds > 0 else ev.sharpe
        sharpes.append(max(0.0, s))
    total = sum(sharpes)
    if total <= 0:
        return [1.0 / len(top)] * len(top)
    exps = [math.exp(s) for s in sharpes]
    ze = sum(exps)
    return [e / ze for e in exps]


def run(args) -> None:
    db = Database(ACCOUNT_DB, market_db_path=MARKET_DB)
    db.initialize()
    provider = DataProvider(db)
    loader = StrategyLoader()
    by_name = {s.name: s for s in loader.load_directory("strategies")}
    goal = Goal()
    params = goal.params or {}
    warmup_days = int(params.get("wf_warmup_days", 120))
    k = int(params.get("top_k_strategies", 3))
    min_trades = int(params.get("wf_min_oos_trades", _MIN_OOS_TRADES))

    engine = BacktestEngine(provider=provider, initial_cash=1_000_000.0,
                            risk_manager=RiskManager(RiskConfig()),
                            protection_manager=ProtectionManager())
    selector = StrategySelector(db, provider, strategies_dir="strategies")

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    dates = month_end_dates(provider, start, end, args.step_months)
    if args.max_dates > 0:
        dates = dates[: args.max_dates]
    print(f"校准区间 {start}..{end}  月末点 {len(dates)}  池 ADV top-{args.pool}  "
          f"fwd={args.fwd_days}d  k={k}", flush=True)

    rows = []
    for d in dates:
        t0 = time.time()
        pool = adv_pool(provider, d, args.pool)
        if len(pool) < 20:
            print(f"{d}  skip: 池仅 {len(pool)} 只", flush=True)
            continue
        ranking = selector.evaluate(goal, pool, as_of=d)
        ranking = [r for r in ranking if r.score > -900]  # 丢彻底失败的
        if len(ranking) < 2:
            print(f"{d}  skip: 有效策略 {len(ranking)}", flush=True)
            continue
        winner = ranking[0]
        dsr_info = StrategySelector._winner_dsr(winner, ranking, wf_enabled=True)
        if dsr_info is None:
            print(f"{d}  skip: DSR None (n_obs={winner.n_obs})", flush=True)
            continue

        top = ranking[:k]
        weights = softmax_weights(top, min_trades)
        fwd = [forward_return(engine, by_name[ev.strategy_name],
                              resolve_params(db, ev.strategy_name, goal),
                              pool, d, warmup_days, args.fwd_days)
               for ev in top]
        softmax_fwd = float(sum(w * f for w, f in zip(weights, fwd)))
        equal_fwd = float(np.mean(fwd))

        row = {
            "date": d.isoformat(),
            "winner": winner.strategy_name,
            "dsr": round(dsr_info["dsr"], 4),
            "sr_obs": round(dsr_info["sr_observed"], 4),
            "sr0": round(dsr_info["sr0_benchmark"], 4),
            "n_trials": dsr_info["n_trials"],
            "n_obs": dsr_info["n_obs"],
            "winner_oos_sharpe_ann": round(winner.oos_sharpe, 3),
            "winner_oos_trades": winner.oos_trades,
            "softmax_fwd": round(softmax_fwd, 5),
            "equal_fwd": round(equal_fwd, 5),
            "winner_fwd": round(fwd[0], 5),
            "top_weights": [round(w, 3) for w in weights],
        }
        rows.append(row)
        print(f"{d}  win={winner.strategy_name:<16} dsr={row['dsr']:.3f} "
              f"sr_obs={row['sr_obs']:+.3f} sr0={row['sr0']:.3f} n_obs={row['n_obs']:<3} "
              f"smax_fwd={softmax_fwd*100:+.2f}% eq_fwd={equal_fwd*100:+.2f}%  "
              f"({time.time()-t0:.1f}s)", flush=True)

    db.close()
    with open(ROWS_OUT, "w") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    analyze(rows, args)


def analyze(rows: list[dict], args) -> None:
    if not rows:
        print("\n无有效样本,无法分析。")
        return
    dsr = np.array([r["dsr"] for r in rows])
    n = len(dsr)
    print("\n" + "=" * 72)
    print(f"样本 {n} 个历史决策点")
    print("\n[1] DSR 分布(校准 sanity:应有梯度,非恒 0/恒 1)")
    for p in (0, 10, 25, 50, 75, 90, 100):
        print(f"    P{p:<3} = {np.percentile(dsr, p):.3f}")
    for thr in (0.90, 0.95):
        print(f"    DSR < {thr}: {int((dsr < thr).sum())}/{n} = {(dsr < thr).mean()*100:.0f}%  "
              f"(该阈值下门触发退等权的比例)")

    # [2] 前瞻持续性:DSR 高的赢家,softmax 集中押注是否真更强?
    #     gate_benefit = equal_fwd - softmax_fwd(>0 表示等权更好=集中押注被噪声反噬)
    benefit = np.array([r["equal_fwd"] - r["softmax_fwd"] for r in rows])
    print("\n[2] 前瞻持续性(gate_benefit = 等权fwd − softmax fwd;>0 = 门有用)")
    for lo, hi, name in [(-1.0, 0.5, "DSR<0.5 "), (0.5, 0.9, "0.5≤DSR<0.9"),
                         (0.9, 1.01, "DSR≥0.9 ")]:
        m = (dsr >= lo) & (dsr < hi)
        if m.sum() == 0:
            print(f"    {name}: 无样本")
            continue
        print(f"    {name}: n={int(m.sum()):<3} 平均 gate_benefit={benefit[m].mean()*100:+.2f}%  "
              f"softmax_fwd={np.array([r['softmax_fwd'] for r in rows])[m].mean()*100:+.2f}%  "
              f"equal_fwd={np.array([r['equal_fwd'] for r in rows])[m].mean()*100:+.2f}%")
    if n > 2:
        corr = float(np.corrcoef(dsr, [r["softmax_fwd"] for r in rows])[0, 1])
        print(f"    corr(DSR, softmax_fwd) = {corr:+.3f}  (>0 = 高DSR赢家前瞻更好=DSR有预测力)")

    # [3] dsr_min 扫描:门策略 = DSR<thr 用等权,否则用 softmax。比基线。
    smax = np.array([r["softmax_fwd"] for r in rows])
    eq = np.array([r["equal_fwd"] for r in rows])
    print("\n[3] dsr_min 扫描(每月前瞻收益均值;门策略 vs 基线)")
    print(f"    基线 always-softmax(门全关) = {smax.mean()*100:+.3f}%/月")
    print(f"    基线 always-equal  (门全开) = {eq.mean()*100:+.3f}%/月")
    best_thr, best_ret = None, -1e9
    for thr in [0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 0.95, 0.99]:
        gated = np.where(dsr < thr, eq, smax)
        fire = (dsr < thr).mean()
        print(f"    dsr_min={thr:.2f}: {gated.mean()*100:+.3f}%/月  (门触发 {fire*100:.0f}%)")
        if gated.mean() > best_ret:
            best_thr, best_ret = thr, gated.mean()

    print("\n[4] 推荐")
    if best_ret <= max(smax.mean(), eq.mean()) + 1e-9:
        winner_base = "always-equal(门全开)" if eq.mean() >= smax.mean() else "always-softmax(门全关)"
        print(f"    无 dsr_min 阈值能超过基线;{winner_base} 更优 → 门在此数据上无净增益。")
        print("    → 保持 dsr_gate 默认关;dsr_min 取保守统计默认 0.90(LdP 惯例区间 0.90~0.95)。")
    else:
        print(f"    最优 dsr_min ≈ {best_thr:.2f}(前瞻 {best_ret*100:+.3f}%/月,超两条基线)。")
        print(f"    → dsr_gate 仍建议默认关(policy),但 dsr_min 默认可定 {best_thr:.2f}。")
    print("=" * 72)


def main() -> None:
    ap = argparse.ArgumentParser(description="DSR 选股门校准回测")
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default="2026-06-01")
    ap.add_argument("--pool", type=int, default=100)
    ap.add_argument("--step-months", type=int, default=1)
    ap.add_argument("--fwd-days", type=int, default=21)
    ap.add_argument("--max-dates", type=int, default=0, help="0=全部;>0 用于冒烟测试")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
