#!/usr/bin/env python3
"""波动率目标定仓实证 — VolTargetSizer vs FixedSizer,IS/OOS + DSR/PBO 闸。

问题:VolTargetSizer 已实现并接入生产定仓路径(compute_buy_target_value 与回测/
实盘共享),但从未被诚实闸门回测过——"低波降回撤"在 07-01 研究里是唯一稳健成立
的机械效应,定仓层面的波动率目标是否真的把回撤降下来、且不牺牲 OOS 收益,需要
用与实盘同一条成交路径(次日开盘、T+1、全成本、ATR 止损+组合熔断)来回答。

口径:
  - 宇宙: start 时点的可交易流动性宇宙(UniverseBuilder as_of=start,无前视),
    默认取前 100 只(按 ADV)。
  - 策略: --strategies 逗号分隔(默认 ma_cross,macd_cross)。
  - 期间: 默认 2022-01-01 ~ 2026-07-31;OOS 分界 2024-07-31。
  - 三臂: FixedSizer(10%) / VolTargetSizer(18%, n=10) / VolTargetSizer(30%, n=10)。
  - 闸: PBO(CSCV)≤0.2 且回撤改善在 IS/OOS 两段方向一致、OOS 夏普不显著差于
    固定定仓——风险降低类效应,看回撤+一致性(与 07-01 的 alpha 闸口径不同,
    因为被检验的命题不同)。

用法: .venv/bin/python scripts/sizer_study.py [--codes 100] [--start ...] [--end ...]
"""
import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quanti.backtest.metrics import compute_metrics
from quanti.backtest.overfit import (deflated_sharpe_ratio, pbo_cscv,
                                     sharpe_per_obs)

DEFAULT_START = date(2022, 1, 1)
DEFAULT_END = date(2026, 7, 31)
OOS_SPLIT = date(2024, 7, 31)


def build_universe(db, provider, as_of: date, n: int) -> list[str]:
    from quanti.agent.universe import resolve_tradable_universe
    params = {"liquidity_filter": True}
    codes = resolve_tradable_universe(db, provider, pool=None, params=params,
                                      as_of=as_of)
    if len(codes) > n:
        codes = codes[:n]
    print(f"universe as of {as_of}: {len(codes)} codes")
    return codes


def run_arm(provider, db, codes, start, end, sizer, strategy_name):
    from quanti.backtest.engine import BacktestEngine
    from quanti.risk.manager import RiskManager
    from quanti.strategy.loader import StrategyLoader
    strat = next(s for s in StrategyLoader().load_directory("strategies")
                 if s.name == strategy_name)
    strat.init({})
    engine = BacktestEngine(provider, initial_cash=1_000_000.0,
                            risk_manager=RiskManager(), sizer=sizer)
    return engine.run(strat, codes, start, end)


def segment_metrics(equity: pd.Series, label: str, out: dict) -> None:
    m = compute_metrics(equity)
    out[label] = {
        "annual_return": m["annual_return"],
        "sharpe": m["sharpe_ratio"],
        "max_drawdown": m["max_drawdown"],
        "n_days": int(len(equity)),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--codes", type=int, default=100)
    ap.add_argument("--start", default=DEFAULT_START.isoformat())
    ap.add_argument("--end", default=DEFAULT_END.isoformat())
    ap.add_argument("--strategies", default="ma_cross,macd_cross")
    ap.add_argument("--json", default=None, help="输出报告 JSON 路径")
    args = ap.parse_args()

    from quanti.data.database import Database
    from quanti.data.provider import DataProvider
    db = Database("data/paper.db", market_db_path="data/market.db")
    db.initialize()
    provider = DataProvider(db)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    codes = build_universe(db, provider, start, args.codes)
    if not codes:
        print("universe empty — cannot run")
        sys.exit(2)

    from quanti.risk.sizer import FixedSizer, VolTargetSizer
    arms = {
        "fixed10": FixedSizer(max_pct=0.10),
        "volt18": VolTargetSizer(target_portfolio_vol=0.18, n_target_positions=10),
        "volt30": VolTargetSizer(target_portfolio_vol=0.30, n_target_positions=10),
    }

    report = {"period": [start.isoformat(), end.isoformat()],
              "oos_split": OOS_SPLIT.isoformat(), "universe_n": len(codes),
              "strategies": {}, "gates": {}}
    daily_returns: dict[tuple[str, str], pd.Series] = {}
    for sname in [s.strip() for s in args.strategies.split(",") if s.strip()]:
        report["strategies"][sname] = {}
        for arm_name, sizer in arms.items():
            try:
                res = run_arm(provider, db, codes, start, end, sizer, sname)
            except Exception as e:  # noqa: BLE001
                print(f"run failed {sname}/{arm_name}: {e}")
                continue
            eq = res.equity_curve
            eq.index = pd.to_datetime(eq.index)
            rows = {"full": {}, "is": {}, "oos": {}}
            segment_metrics(eq, "full", rows)
            seg_is = eq[eq.index <= pd.Timestamp(OOS_SPLIT)]
            seg_oos = eq[eq.index > pd.Timestamp(OOS_SPLIT)]
            segment_metrics(seg_is, "is", rows)
            segment_metrics(seg_oos, "oos", rows)
            report["strategies"][sname][arm_name] = rows
            daily_returns[(sname, arm_name)] = eq.pct_change().dropna()
            print(f"{sname}/{arm_name}: full sharpe={rows['full']['sharpe']:.2f} "
                  f"dd={rows['full']['max_drawdown']:.1%} "
                  f"oos sharpe={rows['oos']['sharpe']:.2f} "
                  f"oos dd={rows['oos']['max_drawdown']:.1%}")

    # ---- gates ----
    for sname in report["strategies"]:
        arms_in = [a for a in arms if (sname, a) in daily_returns]
        if len(arms_in) < 2:
            continue
        trial_sharpes = [sharpe_per_obs(daily_returns[(sname, a)].values)
                         for a in arms_in]
        df = pd.DataFrame({a: daily_returns[(sname, a)] for a in arms_in})
        df = df.dropna()
        pbo = pbo_cscv(df.values, n_splits=16)
        gate = {"arms": arms_in, "trial_sharpes": [float(x) for x in trial_sharpes],
                "pbo": pbo}
        for a in arms_in:
            dsr = deflated_sharpe_ratio(daily_returns[(sname, a)].values,
                                        trial_sharpes)
            gate[a] = {"dsr": dsr["dsr"], "sr_observed": dsr["sr_observed"],
                       "sr0_benchmark": dsr["sr0_benchmark"]}
        report["gates"][sname] = gate

        f = report["strategies"][sname]
        fixed = f.get("fixed10", {})
        for a in ("volt18", "volt30"):
            if a not in f or not fixed:
                continue
            dd_ok = (f[a]["is"]["max_drawdown"] < fixed["is"]["max_drawdown"]
                     and f[a]["oos"]["max_drawdown"] < fixed["oos"]["max_drawdown"])
            sharpe_ok = f[a]["oos"]["sharpe"] >= fixed["oos"]["sharpe"] - 0.1
            pbo_ok = pbo["pbo"] <= 0.2
            verdict = "PASS" if (dd_ok and sharpe_ok and pbo_ok) else "FAIL"
            print(f"{sname}/{a}: dd_ok={dd_ok} sharpe_ok={sharpe_ok} "
                  f"pbo={pbo['pbo']:.2f} → {verdict}")
            f[a]["verdict"] = verdict

    print()
    print("gate summary:", report["gates"])
    if args.json:
        import json
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2, default=str)
        print(f"report saved: {args.json}")


if __name__ == "__main__":
    main()

