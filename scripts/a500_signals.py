#!/usr/bin/env python3
"""生成中证A500增强组合的目标持仓文件(给 QMT 执行器用)。

用法: .venv/bin/python scripts/a500_signals.py [--top-k 50] [--band 2] [--out data/a500_target.csv]
建议每月最后一个交易日收盘后跑(cron), QMT 侧次日开盘执行。

逻辑与 scripts/a500_backtest.py 的 fac_top_cw10(fund 因子) 完全一致:
  成分 = data/a500_membership.json 回放到今日(可 --refresh 用 akshare 官方名单校验);
  打分 = compute_factor_panel 基本面7因子(估值/股息/规模/质量/成长, 行业中性化);
  选股 = top-K, 已持仓在 top-K*band 内保留(band 降换手);
  加权 = 默认 cw10(circ_mv 加权+单股10%上限, 与 RiskManager 单股红线一致),
         --weighting ew 可选等权;
  上期持仓从上一份 target csv 读取(首次运行=全新建仓)。

fail-loud: 行情/估值数据不新鲜、打分覆盖率不足、成分校验偏差过大 → 直接报错退出,
不产出文件(QMT 侧因文件过期而拒绝交易)。
"""
import argparse, csv, json, sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.factors.cross_sectional import (compute_factor_panel, FactorConfig,
                                            DEFAULT_FACTORS)

ROOT = Path(__file__).resolve().parent.parent
MEMBERSHIP = ROOT / "data/a500_membership.json"
PV_FACTORS = ("momentum_3m", "momentum_6m", "reversal_1w",
              "realized_vol_20d", "turnover_20d")
MAX_STALE_TRADE_DAYS = 3      # 行情最新日期距今超过则拒绝出信号
MIN_SCORED = 400              # 500 只里至少要有这么多只能打出分


def current_members() -> set[str]:
    spec = json.loads(MEMBERSHIP.read_text())
    cur = set(spec["base"])
    today = date.today().isoformat()
    for ev in sorted(spec["events"], key=lambda e: e["effective_trade_date"]):
        if ev["effective_trade_date"] <= today:
            cur -= set(ev["out"]); cur |= set(ev["in"])
    return cur


def refresh_check(members: set[str]) -> None:
    """与中证官网当前名单比对; 偏差 > 10 只说明 membership 文件过期 → 报错。"""
    import akshare as ak
    official = set(ak.index_stock_cons_csindex(symbol="000510")
                   ["成分券代码"].astype(str).str.zfill(6))
    diff = len(official ^ members)
    if diff > 10:
        raise RuntimeError(
            f"a500_membership.json 与官方名单差 {diff} 只(>10), 需要更新调样事件: "
            f"官方有而本地无 {sorted(official - members)[:5]}...")
    if diff:
        print(f"! 与官方名单差 {diff} 只(容忍): {sorted(official ^ members)}")


def qmt_code(code: str) -> str:
    return f"{code}.SH" if code.startswith("6") else f"{code}.SZ"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=50)
    ap.add_argument("--band", type=float, default=2.0)
    ap.add_argument("--weighting", choices=["cw10", "ew"], default="cw10")
    ap.add_argument("--out", default=str(ROOT / "data/a500_target.csv"))
    ap.add_argument("--refresh", action="store_true",
                    help="用 akshare 官方名单校验 membership 文件")
    args = ap.parse_args()

    db = Database(str(ROOT / "data/market.db"))
    db.initialize()
    provider = DataProvider(db)

    today = date.today()
    import sqlite3
    con = sqlite3.connect(f"file:{ROOT/'data/market.db'}?mode=ro", uri=True)
    row = con.execute("SELECT MAX(date) FROM daily_basic WHERE circ_mv IS NOT NULL").fetchone()
    con.close()
    if not row or not row[0]:
        raise RuntimeError("daily_basic 无数据, market.db 异常")
    as_of = date.fromisoformat(row[0])
    tds = provider.get_trade_dates(as_of + timedelta(days=1), today)
    if len(tds) > MAX_STALE_TRADE_DAYS:
        raise RuntimeError(f"估值数据最新日 {as_of} 距今已 {len(tds)} 个交易日, 先同步数据")

    members = current_members()
    if args.refresh:
        refresh_check(members)

    cfg = FactorConfig(factors={k: v for k, v in DEFAULT_FACTORS.items()
                                if k not in PV_FACTORS})
    panel = compute_factor_panel(provider, db, sorted(members), as_of=as_of, config=cfg)
    if panel is None or panel.empty or "composite" not in panel:
        raise RuntimeError("因子面板为空")
    comp = panel["composite"].dropna()
    comp = comp[comp.index.isin(members)]
    if len(comp) < MIN_SCORED:
        raise RuntimeError(f"打分覆盖率不足: {len(comp)}/{len(members)} < {MIN_SCORED}")

    ranked = comp.sort_values(ascending=False)
    out_path = Path(args.out)
    held: list[str] = []
    if out_path.exists():
        with out_path.open() as f:
            held = [row["code"] for row in csv.DictReader(f)]
    band_set = set(ranked.head(int(args.top_k * args.band)).index)
    keep = [c for c in held if c in band_set]
    new = [c for c in ranked.index if c not in keep][:max(args.top_k - len(keep), 0)]
    sel = keep + new

    exec_from = next((d for d in provider.get_trade_dates(
        as_of + timedelta(days=1), as_of + timedelta(days=15))), None)
    if exec_from is None:
        raise RuntimeError("找不到下一交易日")

    if args.weighting == "cw10":
        import sqlite3
        con = sqlite3.connect(f"file:{ROOT/'data/market.db'}?mode=ro", uri=True)
        q = f"""SELECT code, circ_mv FROM daily_basic WHERE date=?
                AND code IN ({','.join('?'*len(sel))})"""
        mv = dict(con.execute(q, [as_of.isoformat(), *sel]).fetchall())
        con.close()
        if len(mv) < len(sel) * 0.95:
            raise RuntimeError(f"circ_mv 覆盖不足 {len(mv)}/{len(sel)}")
        weights = {c: mv.get(c, 0.0) for c in sel}
        tot = sum(weights.values())
        weights = {c: v / tot for c, v in weights.items()}
        for _ in range(20):                     # 单股 10% 上限, 迭代重分配
            over = {c: v for c, v in weights.items() if v > 0.10}
            if not over:
                break
            excess = sum(v - 0.10 for v in over.values())
            for c in over:
                weights[c] = 0.10
            room = {c: v for c, v in weights.items() if v < 0.10}
            rs = sum(room.values())
            for c in room:
                weights[c] += excess * room[c] / rs
        tot = sum(weights.values())
        weights = {c: v / tot for c, v in weights.items()}
    else:
        weights = {c: 1.0 / len(sel) for c in sel}

    tmp = out_path.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["as_of", as_of.isoformat(), "execute_from", exec_from.isoformat(),
                     "top_k", args.top_k, "band", args.band, "n", len(sel),
                     "weighting", args.weighting])
        wr.writerow(["code", "qmt_code", "weight", "composite"])
        for c in sel:
            wr.writerow([c, qmt_code(c), f"{weights[c]:.6f}",
                         f"{ranked.get(c, float('nan')):.4f}"])
    tmp.replace(out_path)
    turnover = 1 - len(set(held) & set(sel)) / max(len(sel), 1)
    print(f"as_of={as_of} execute_from={exec_from} 持仓{len(sel)}只 "
          f"vs 上期换手{turnover*100:.0f}% -> {out_path}")


if __name__ == "__main__":
    main()
