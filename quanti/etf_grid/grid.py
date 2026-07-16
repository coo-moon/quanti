"""网格核心逻辑：震荡指标、挖掘筛选、回测、参数优化(多季度样本外验证)。

指标(后复权 close, 默认近126交易日):
  er   = |区间净位移| / Σ|Δ|      Kaufman 效率比, 越低越震荡(网格友好)
  vol  = 日对数收益 std*sqrt(252)  年化波动
  amp  = (max-min)/mean            振幅(网格利润空间)
  net  = 期末/期初-1               净漂移(排单边)
  pos  = 当前在箱体位置[0,1]
  rev  = Σ|Δ| / (max-min)          穿越箱体次数(高=真震荡,非一次V型)
网格模型: 箱体[lo,hi] N 格, 目标持仓 = round(N*(hi-close)/(hi-lo)) 截断[0,N]
  越跌越买、越涨越卖; 跌破 lo 满仓(套牢), 升破 hi 空仓(踏空)。
"""
from __future__ import annotations

import numpy as np

from .data import ETF_META, EtfBars, read_etf

WINDOW = 126          # 挖掘/指标回看(~半年)
FEE = 1e-4            # ETF 佣金万1(单边), 免印花税
SLIP = 2e-4          # 滑点
C = 1_000_000.0


def _dd(eq: np.ndarray) -> float:
    peak = np.maximum.accumulate(eq)
    return float(((eq - peak) / peak).min())


def oscillation_metrics(bars: EtfBars, window: int = WINDOW) -> dict | None:
    c = bars.close[-window:]
    if len(c) < max(30, window // 2):
        return None
    rng = float(c.max() - c.min())
    if rng <= 0:
        return None
    path = float(np.sum(np.abs(np.diff(c))))
    r = np.diff(np.log(c))
    return dict(
        er=abs(c[-1] - c[0]) / path if path else 1.0,
        vol=float(r.std(ddof=1) * np.sqrt(252)),
        amp=rng / float(c.mean()),
        net=float(c[-1] / c[0] - 1),
        pos=float((c[-1] - c.min()) / rng),
        rev=path / rng,
        adv=float(bars.amount[-20:].mean()),
        days=len(c),
    )


def _box(low: np.ndarray, high: np.ndarray, trim: bool) -> tuple[float, float]:
    if trim:
        return float(np.percentile(low, 5)), float(np.percentile(high, 95))
    return float(low.min()), float(high.max())


def _run(close, high, low, e0, e1, N, lookback, rebal, geom, trim):
    """网格回测核心(数组级)。rebal<=0 表示固定箱体(仅起点设一次)。返回(净值序列, 交易数)。"""
    cash, lots, uc = C, [], C / N
    eq = np.empty(e1 - e0)
    lo = hi = None
    trades = 0
    for t in range(e0, e1):
        reset = (t == e0) if rebal <= 0 else ((t - e0) % rebal == 0)
        if reset:
            lo, hi = _box(low[t - lookback:t], high[t - lookback:t], trim)
        p = close[t]
        if hi is None or hi <= lo:
            eq[t - e0] = cash + sum(s for s, _ in lots) * p
            continue
        if geom:
            frac = np.log(hi / min(max(p, lo), hi)) / np.log(hi / lo)
        else:
            frac = (hi - min(max(p, lo), hi)) / (hi - lo)
        tgt = max(0, min(N, int(round(N * frac))))
        held = len(lots)
        if tgt > held:
            for _ in range(tgt - held):
                if cash < uc * (1 + FEE):
                    break
                cash -= uc + uc * FEE
                lots.append((uc / (p * (1 + SLIP)), t))
                trades += 1
        elif tgt < held:
            need, keep, sold = held - tgt, [], 0
            for sh, bd in reversed(lots):
                if sold < need and bd < t:        # T+1: 当日买入不卖
                    cash += sh * p * (1 - SLIP) * (1 - FEE)
                    trades += 1
                    sold += 1
                else:
                    keep.append((sh, bd))
            lots = list(reversed(keep))
        eq[t - e0] = cash + sum(s for s, _ in lots) * p
    return eq, trades


def backtest(bars: EtfBars, start: str, N: int = 10, lookback: int = 60,
             rebal: int = 0, geom: bool = False, trim: bool = True,
             with_curve: bool = True) -> dict | None:
    """从 start(ISO) 起投的网格回测 vs 买入持有。rebal=0 固定箱体(默认,更稳健)。"""
    close, high, low, dates = bars.close, bars.high, bars.low, bars.dates
    n = len(close)
    e0 = int(np.searchsorted(dates, start))
    if e0 < lookback or n - e0 < 10:
        return None
    eq, trades = _run(close, high, low, e0, n, N, lookback, rebal, geom, trim)
    hold = close[e0:] / close[e0]
    out = dict(
        grid_ret=float(eq[-1] / C - 1),
        hold_ret=float(close[-1] / close[e0] - 1),
        grid_dd=_dd(eq),
        hold_dd=_dd(hold),
        trades=trades,
        start=str(dates[e0]),
        end=str(dates[-1]),
    )
    if with_curve:
        d = dates[e0:]
        out["grid_curve"] = {str(d[i]): round(float(eq[i] / C), 4) for i in range(len(d))}
        out["hold_curve"] = {str(d[i]): round(float(hold[i]), 4) for i in range(len(d))}
    return out


def deployable_box(bars: EtfBars, lookback: int = 60, N: int = 10,
                   trim: bool = True) -> dict:
    """当前可落地箱体(原始价)+ 步长/每格资金。"""
    rl, rh = bars.raw_low[-lookback:], bars.raw_high[-lookback:]
    lo, hi = _box(rl, rh, trim)
    cur = float(bars.raw_close[-1])
    step = (hi - lo) / N
    return dict(box_lo=round(lo, 3), box_hi=round(hi, 3), price=round(cur, 3),
                grids=N, step=round(step, 3),
                step_pct=round(step / cur * 100, 2) if cur else 0.0,
                stop=round(lo * 0.97, 3))


# ---- 挖掘 ----
def _grid_gate(m: dict, adv_min: float) -> bool:
    return (m["adv"] >= adv_min and m["er"] <= 0.25 and abs(m["net"]) <= 0.15
            and m["rev"] >= 6 and 0.10 <= m["vol"] <= 0.55 and 0.20 <= m["pos"] <= 0.80)


def screen(db, window: int = WINDOW, adv_min: float = 1e8,
           quick_bt: bool = True) -> list[dict]:
    """挖掘近期适合网格「稳定套现」的 ETF：低ER + 净≈0 + 高穿越 + 足流动。
    附近半年网格回测(描述性,起投=数据末尾往前~半年)。按 grid_score 降序。
    """
    from datetime import date, timedelta

    from .data import cache_status
    st = cache_status(db)
    if not st["end"]:
        return []
    end = st["end"]
    bt_start = (date.fromisoformat(end) - timedelta(days=190)).isoformat()

    rows = []
    codes = [r[0] for r in db.conn.execute(
        "SELECT DISTINCT code FROM etf_daily").fetchall()]
    for code in codes:
        bars = read_etf(db, code)
        if bars is None:
            continue
        m = oscillation_metrics(bars, window)
        if m is None or not _grid_gate(m, adv_min):
            continue
        name, cat, t0 = ETF_META.get(code, (code, "", False))
        rec = dict(code=code, name=name, category=cat, t0=t0,
                   price=float(bars.raw_close[-1]), grid_score=m["rev"] * (1 - m["er"]),
                   **{k: round(v, 4) for k, v in m.items()})
        if quick_bt:
            bt = backtest(bars, bt_start, N=10, lookback=60, rebal=0, trim=True,
                          with_curve=False)
            if bt:
                rec.update(grid_ret=bt["grid_ret"], hold_ret=bt["hold_ret"],
                           grid_dd=bt["grid_dd"], hold_dd=bt["hold_dd"],
                           bt_trades=bt["trades"])
        rec.update(deployable_box(bars))
        rows.append(rec)
    rows.sort(key=lambda r: r["grid_score"], reverse=True)
    return rows


# ---- 参数优化 + 多季度样本外验证 ----
def _quarter_starts(dates: np.ndarray, n_quarters: int, qlen: int) -> list[tuple[str, int]]:
    """从数据末尾往回切 n 个约 qlen 交易日的季度窗,返回(标签, 起投索引)。"""
    total = len(dates)
    out = []
    for i in range(n_quarters):
        e1 = total - i * qlen
        e0 = e1 - qlen
        if e0 < 60:
            break
        out.append((str(dates[e0])[:7], e0))
    return list(reversed(out))


def optimize(bars: EtfBars, n_quarters: int = 6, qlen: int = 60,
             lookback: int = 60) -> dict | None:
    """扫描 (N, 箱体重设, 等差/等比, 裁wick) 在 n 个季度上的表现,选跨季稳健配置。
    稳健准则: 各季网格收益的 (最差季, 均值) 双高 —— 惩罚只赢单季的过拟合配置。
    """
    from itertools import product
    close, high, low, dates = bars.close, bars.high, bars.low, bars.dates
    qs = _quarter_starts(dates, n_quarters, qlen)
    if len(qs) < 3:
        return None

    # 各季买入持有基准
    holds = {}
    for label, e0 in qs:
        e1 = min(e0 + qlen, len(close))
        holds[label] = float(close[e1 - 1] / close[e0] - 1)

    combos = product([8, 10, 12, 16], [0, 20], [False, True], [False, True])  # N, rebal, geom, trim
    recs = []
    for N, rebal, geom, trim in combos:
        per, dds, tr, beat = {}, [], 0, 0
        ok = True
        for label, e0 in qs:
            e1 = min(e0 + qlen, len(close))
            if e0 < lookback:
                ok = False
                break
            eq, t = _run(close, high, low, e0, e1, N, lookback, rebal, geom, trim)
            ret = float(eq[-1] / C - 1)
            per[label] = round(ret * 100, 1)
            dds.append(_dd(eq))
            tr += t
            beat += int(ret > holds[label])
        if not ok:
            continue
        vals = list(v / 100 for v in per.values())
        recs.append(dict(
            N=N, box=("固定" if rebal <= 0 else f"每{rebal}日重设"),
            spacing=("等比" if geom else "等差"), trim=("是" if trim else "否"),
            mean=round(float(np.mean(vals)) * 100, 1),
            worst=round(float(min(vals)) * 100, 1),
            dd=round(float(np.mean(dds)) * 100, 1),
            beat_hold=f"{beat}/{len(qs)}", trades=tr, per_quarter=per,
        ))
    if not recs:
        return None
    # 稳健排序: 先最差季、后均值
    robust = sorted(recs, key=lambda r: (r["worst"], r["mean"]), reverse=True)
    # 过拟合反例: 只看最近一季最优
    last_q = qs[-1][0]
    overfit = sorted(recs, key=lambda r: r["per_quarter"].get(last_q, -999), reverse=True)
    best = robust[0]
    box = deployable_box(bars, lookback=lookback, N=best["N"], trim=(best["trim"] == "是"))
    return dict(
        quarters=[label for label, _ in qs],
        holds={k: round(v * 100, 1) for k, v in holds.items()},
        robust=robust[:6],
        overfit=overfit[:3],
        best=best,
        deploy=box,
    )
