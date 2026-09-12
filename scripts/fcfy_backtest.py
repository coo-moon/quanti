"""FcfY 组合级回测 + 过拟合闸(DSR/PBO)—— 任务书第二、三步。

主口径:FcfY 最高的 top-30 等权(国信「FCF30」复刻口径),每 20 交易日
(≈1 个月)调一次仓。基准:(a) 宇宙内全市场等权(诚实基准,+10.49%/5y 同源)、
(b) 沪深300 价格指数 000300.SH 与全收益 H00300.CSI(hfq 净值含股息,只有对
全收益比才同口径,对价格指数比会虚高)。

**成本路径与引擎同源** —— 不另造假设,直接 import 引擎在用的对象:
  - `quanti.backtest.commission.AShareCommission`:佣金万2.5 + 单笔 5 元下限、
    印花税只在卖出侧(2023-08-28 前千1、之后万5)、过户费十万分之一双边;
  - `quanti.backtest.slippage.VolumeImpactSlippage`:平方根冲击
    5bp 底 + 5bp×√参与率,参与率用信号日**之前** 20 个交易日的日均成交额;
  - `quanti.backtest.commission.dividend_tax_rate`:红利税三档(≤30天 20% /
    ≤1年 10% / >1年免)。hfq 复权价已把分红再投计进收益,税是它唯一表达不出
    来的现金流;月频轮动几乎全落在 10%/20% 档 ⇒ 必须补扣。
向量化近似(与引擎的差别如实记进文档):不含整手取整、不建模涨跌停与停牌买不进
(按开盘价全额成交)、冲击按 ADV20 摊到每票、红利税用股息率均值近似。

多重检验账本:DSR 的 trial_sharpes 与 PBO 的 perf matrix 用**第一步跑过的全部
18 个变体**(FcfY/CFOY × base/minmv30/mainboard × 步长 5/20/60),并把各自的
日频净值路径统一抽样到主口径的 20 日网格后再算 —— 不同步长的「每期」单位不同,
直接混在一起比 Sharpe 是错的。top-N=30 不搜索。闸值不自行放宽:DSR ≥ 0.95、
PBO ≤ 0.2、年单边换手 ≤ 300%、逐年超额 ≥ 0 的年数 ≥ 4/5(2022-2026)。

market.db 只读(`file:...?mode=ro`);报告 JSON 落 data/(.gitignore,不提交)。

用法:
    /opt/data/quanti/.venv/bin/python scripts/fcfy_backtest.py \\
        --market-db /opt/data/quanti/data/market.db --cash 1000000 \\
        --json data/fcfy_backtest.json
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import fcfy_study as fs  # noqa: E402
from quanti.backtest.commission import (  # noqa: E402
    AShareCommission, dividend_tax_rate)
from quanti.backtest.overfit import (  # noqa: E402
    deflated_sharpe_ratio, pbo_cscv, sharpe_per_obs)
from quanti.backtest.slippage import VolumeImpactSlippage  # noqa: E402
from quanti.models import Direction  # noqa: E402

TOP_N = 30                 # FCF30 复刻口径(不搜 N,免得又多一层拟合)
GATE_DSR = 0.95
GATE_PBO = 0.20
GATE_TURNOVER = 3.0        # 年单边换手 ≤ 300%
GATE_POS_YEARS = 4
GATE_YEAR_WINDOW = ("2022", "2023", "2024", "2025", "2026")
TRADING_DAYS = 252


# ------------------------------------------------------------------ 成本模型
def period_cost(buy: dict[str, float], sell: dict[str, float],
                adv: dict[str, float], trade_day: date,
                comm: AShareCommission, slip: VolumeImpactSlippage) -> dict:
    """一个调仓日的交易成本(元),按**每票一笔子单**计。

    5 元佣金下限逐票生效(引擎也是逐票下单,不是把整个调仓合成一张大单),
    所以小资金档会被下限显著吃掉 —— 这正是要如实报告的容量效应。
    """
    out = {"impact": 0.0, "fee": 0.0}
    for side, notional in (("buy", buy), ("sell", sell)):
        for code, val in notional.items():
            if val <= 0:
                continue
            direction = Direction.BUY if side == "buy" else Direction.SELL
            qty = int(round(val))
            frac = slip.adjust(code=code, price=1.0, qty=qty, direction=direction,
                               adv20=float(adv.get(code, 0.0) or 0.0))
            out["impact"] += frac * val
            out["fee"] += comm.calculate(price=1.0, quantity=qty,
                                         direction=direction, trade_date=trade_day)
    out["total"] = out["impact"] + out["fee"]
    return out


def adv20_map(panel: fs.Panel, i: int) -> dict[str, float]:
    """ADV20:信号日 D **之前** 20 个交易日的日均成交额(不含 D 及之后)。"""
    if panel.amount is None or i <= 0:
        return {}
    w = np.asarray(panel.amount[max(0, i - 20):i], dtype=np.float64)
    if w.size == 0:
        return {}
    all_nan = np.all(~np.isfinite(w), axis=0)
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        m = np.where(all_nan, np.nan, np.nanmean(w, axis=0))
    return {c: float(v) for c, v in zip(panel.codes, m) if np.isfinite(v)}


def dividend_tax_frac(panel: fs.Panel, i: int, weights: dict[str, float],
                      cal_days: int) -> float:
    """持有期红利税占净值比例:dv_ratio(TTM%) × 持有年数 × 三档税率(近似)。

    与引擎 DividendTaxLedger 同一动机(逐笔除息 → FIFO 批次),这里把逐笔换成
    股息率均值近似。月频轮动下两者差异在 bps 级,但**完全不扣**是系统性高估。
    """
    rate = dividend_tax_rate(max(0, cal_days))
    if rate <= 0:
        return 0.0
    dv = panel.extra.get("dv_ratio")
    if dv is None:
        return 0.0
    row = np.asarray(dv[i], dtype=np.float64)
    tot = 0.0
    for c, w in weights.items():
        j = panel._code_pos.get(c)
        if j is None or not np.isfinite(row[j]) or row[j] <= 0:
            continue
        tot += w * (row[j] / 100.0) * (cal_days / 365.0) * rate
    return float(tot)


# ------------------------------------------------------------------ 组合模拟
@dataclass
class Sim:
    """一个 trial 的模拟结果:日频净收益路径(键=交易日序号)+ 逐期诊断。"""

    daily: dict[int, float] = field(default_factory=dict)
    daily_bench: dict[int, float] = field(default_factory=dict)
    ends: list[int] = field(default_factory=list)        # 每期退出日序号
    periods: list[dict] = field(default_factory=list)
    turn: list[float] = field(default_factory=list)
    cost: list[float] = field(default_factory=list)

    @property
    def n_periods(self) -> int:
        return len(self.ends)


def simulate(panel: fs.Panel, ranked: dict[int, list[str]],
             uni: dict[int, pd.Index], step: int, *, cash: float,
             top_n: int = TOP_N) -> Sim:
    """top-N 等权、每 step 交易日调一次仓的全成本日频模拟。

    时间线(与引擎一致):信号日 D=i → **次日开盘**建仓 → 下一个信号日的次日
    开盘调仓 ⇒ 持有期恰好 step 个交易日,T+1 天然满足。日频路径:建仓日
    open→close(当日一次性扣交易成本 + 红利税),期间 close→close 按漂移权重,
    退出日 close(x-1)→open(x)。
    """
    comm = AShareCommission()
    slip = VolumeImpactSlippage()
    sim = Sim()
    entry = panel.open_ if panel.open_ is not None else panel.close
    pos = {c: j for j, c in enumerate(panel.codes)}
    reb = [i for i in sorted(ranked) if i + step + 1 < len(panel.dates)]
    w_prev: dict[str, float] = {}
    nav = float(cash)
    for i in reb:
        e, x = i + 1, i + step + 1
        pe = np.asarray(entry[e], dtype=np.float64)
        px_open = np.asarray(entry[x], dtype=np.float64)
        # 基准:同期全市场等权(open→open 毛收益,不计成本 —— 让超额更保守)
        b = 0.0
        ub = uni.get(i)
        if ub is not None and len(ub):
            jj = np.fromiter((pos[c] for c in ub if c in pos), dtype=np.int64)
            p0, p1 = pe[jj], px_open[jj]
            ok = np.isfinite(p0) & np.isfinite(p1) & (p0 > 0)
            if ok.any():
                b = float(np.mean(p1[ok] / p0[ok] - 1.0))
        _spread(sim.daily_bench, e, x, b)
        hold = [c for c in ranked[i][:top_n]
                if c in pos and np.isfinite(pe[pos[c]]) and pe[pos[c]] > 0
                and np.isfinite(px_open[pos[c]])]
        if not hold or nav <= 0:
            w_prev, nav = {}, 0.0
            continue
        w_new = {c: 1.0 / len(hold) for c in hold}
        buy, sell = {}, {}
        for c in set(w_new) | set(w_prev):
            delta = (w_new.get(c, 0.0) - w_prev.get(c, 0.0)) * nav
            if delta > 1.0:
                buy[c] = delta
            elif delta < -1.0:
                sell[c] = -delta
        cost = period_cost(buy, sell, adv20_map(panel, i),
                           pd.Timestamp(panel.dates[e]).date(), comm, slip)
        cal_days = (pd.Timestamp(panel.dates[x]).date()
                    - pd.Timestamp(panel.dates[e]).date()).days
        dtax = dividend_tax_frac(panel, i, w_new, cal_days)
        cost_frac = cost["total"] / nav + dtax
        turn = (sum(buy.values()) + sum(sell.values())) / 2.0 / nav
        # ---- 日频路径
        idx = [pos[c] for c in hold]
        # 持有期内遇到停牌(当日无 bar)→ 价格冻结 ffill。**不能**把「没有报价」
        # 当成 -100%:nansum 会把缺失项当 0,收益被系统性低估(实测差点踩到)。
        # 两端(建仓日 open、下期建仓日 open)仍必须有 bar —— 不可成交就不进组合。
        raw_pc = np.asarray(panel.close[e:x + 1][:, idx], dtype=np.float64)
        pc = pd.DataFrame(raw_pc).ffill().to_numpy()
        w = np.fromiter((w_new[c] for c in hold), dtype=np.float64)
        r_day = float(np.nansum((pc[0] / pe[idx] - 1.0) * w)) - cost_frac
        _acc(sim.daily, e, r_day)
        rets = [r_day]
        for t in range(1, x - e):
            ratios = pc[t] / pc[t - 1]
            r = float(np.nansum(ratios * w) - 1.0)          # 净收益,不是总倍数
            w = w * ratios / (1.0 + r) if r > -0.999 else w  # 防全组合归零除爆
            _acc(sim.daily, e + t, r)
            rets.append(r)
        if len(pc) > 1:
            r_exit = float(np.nansum((px_open[idx] / pc[-2] - 1.0) * w))
            _acc(sim.daily, x, r_exit)
            rets.append(r_exit)
        net = float(np.prod([1.0 + v for v in rets]) - 1.0)
        nav *= (1.0 + net)
        end_val = {c: float(w[k]) for k, c in enumerate(hold)}
        s = sum(end_val.values())
        w_prev = {c: v / s for c, v in end_val.items()} if s > 0 else {}
        sim.ends.append(x)
        sim.periods.append({"signal": panel.dates[i], "entry": panel.dates[e],
                            "exit": panel.dates[x], "n_hold": len(hold),
                            "net": net, "cost_frac": cost_frac,
                            "fee": cost["fee"] / nav, "impact": cost["impact"] / nav,
                            "div_tax": dtax, "turn": turn, "bench_ew": b,
                            "top_fcfy_names": hold[:5]})
        sim.turn.append(turn)
        sim.cost.append(cost_frac)
    return sim


def sim_gross(sim: Sim, panel: fs.Panel) -> dict[int, float]:
    """把成本还原回去 → 毛收益日频路径(判定死因:IC 弱 or 成本吃光)。

    成本一次性计在建仓日,所以毛路径 = 把该日的 cost_frac 加回来。
    """
    out = dict(sim.daily)
    for per in sim.periods:
        k = panel._date_pos[per["entry"]]
        out[k] = (1.0 + out.get(k, 0.0)) / (1.0 - per["cost_frac"]) - 1.0
    return out


def _acc(dst: dict[int, float], k: int, r: float) -> None:
    """把某日收益复利累进(同一日多段收益合并)。"""
    if not np.isfinite(r):
        r = 0.0
    dst[k] = (1.0 + dst.get(k, 0.0)) * (1.0 + r) - 1.0


def _spread(dst: dict[int, float], e: int, x: int, r_total: float) -> None:
    """把基准的区间收益几何摊到区间内每一天(只为与日频轴对齐)。"""
    n = max(1, x - e)
    if not np.isfinite(r_total) or r_total <= -1.0:
        return
    step_r = (1.0 + r_total) ** (1.0 / n) - 1.0
    for k in range(e, x):
        _acc(dst, k, step_r)


# ------------------------------------------------------------------ 对齐与度量
def to_series(daily: dict[int, float]) -> pd.Series:
    return pd.Series(daily, dtype=float).sort_index()


def grid_returns(daily: pd.Series, ends: list[int]) -> np.ndarray:
    """把日频净收益路径抽样到 `ends`(每期结束日)→ 区间收益数组。

    真实路径的**子抽样**,不发明观测:这样步长 5/20/60 的 trial 才能放在同一
    张 perf matrix 里比 Sharpe(每期的时间长度一致)。
    """
    if daily.empty or not ends:
        return np.array([])
    lognav = np.log1p(daily.clip(lower=-0.99)).cumsum()
    idx = lognav.index.values
    prev = 0.0
    out = []
    for k in ends:
        upto = idx[idx <= k]
        cur = float(lognav.loc[upto[-1]]) if len(upto) else 0.0
        out.append(float(np.exp(cur - prev) - 1.0))
        prev = cur
    return np.asarray(out, dtype=float)


def perf_from_daily(daily: pd.Series, bench: pd.Series, panel: fs.Panel,
                    step: int) -> dict:
    """日频净收益 → 年化 / TE / IR / 最大回撤 / 逐年超额。"""
    if daily.empty:
        return {}
    idx = sorted(set(daily.index) | set(bench.index))
    p = daily.reindex(idx).fillna(0.0).astype(float)
    b = bench.reindex(idx).fillna(0.0).astype(float)
    nav_p, nav_b = (1.0 + p).cumprod(), (1.0 + b).cumprod()
    mdd_b = float((nav_b / nav_b.cummax() - 1.0).min())
    yrs = len(idx) / TRADING_DAYS
    ann_p = float(nav_p.iloc[-1] ** (1 / yrs) - 1) if yrs > 0 else float("nan")
    ann_b = float(nav_b.iloc[-1] ** (1 / yrs) - 1) if yrs > 0 else float("nan")
    ann_ex = float((nav_p.iloc[-1] / nav_b.iloc[-1]) ** (1 / yrs) - 1)
    ex = p - b
    te = float(ex.std(ddof=1) * np.sqrt(TRADING_DAYS))
    mdd = float((nav_p / nav_p.cummax() - 1.0).min())
    by_year: dict[str, dict] = {}
    for y in GATE_YEAR_WINDOW:
        keys = [k for k in idx if panel.dates[k][:4] == y]
        if not keys:
            continue
        gp = float(np.prod([1.0 + p[k] for k in keys]))
        gb = float(np.prod([1.0 + b[k] for k in keys]))
        by_year[y] = {"port": round(gp - 1.0, 4), "bench": round(gb - 1.0, 4),
                      "excess": round(gp / gb - 1.0, 4)}
    return {"obs": len(idx), "from": panel.dates[idx[0]], "to": panel.dates[idx[-1]],
            "ann_return": round(ann_p, 4), "ann_bench_ew": round(ann_b, 4),
            "ann_excess_vs_ew": round(ann_ex, 4), "te_vs_ew": round(te, 4),
            "ir_vs_ew": round(ann_ex / te, 3) if te > 0 else None,
            "max_drawdown": round(mdd, 4), "bench_max_drawdown": round(mdd_b, 4),
            "by_year": by_year}


def load_index_csv(name: str) -> pd.Series:
    """data/idx_*.csv → 收盘序列(index=ISO)。仅对账用,绝不进策略。"""
    path = ROOT / "data" / name
    if not path.exists():
        return pd.Series(dtype=float)
    df = pd.read_csv(path, dtype={"trade_date": str}).sort_values("trade_date")
    iso = (df.trade_date.str[:4] + "-" + df.trade_date.str[4:6] + "-"
           + df.trade_date.str[6:])
    return pd.Series(df.close.astype(float).values, index=iso).groupby(level=0).last()


def vs_index(daily: pd.Series, panel: fs.Panel, idx: pd.Series) -> dict:
    """组合净值 vs 指数收盘(区间几何年化差;共同有指数的日子)。"""
    if idx.empty or daily.empty:
        return {}
    days = [panel.dates[k] for k in daily.index]
    common = [d for d in days if d in idx.index]
    if len(common) < 60:
        return {"n": len(common)}
    by = {panel.dates[k]: k for k in daily.index}
    nav = float(np.prod([1.0 + float(daily.loc[by[d]]) for d in common]))
    base = float(idx[common[-1]] / idx[common[0]])
    yrs = len(common) / TRADING_DAYS
    return {"n": len(common), "start": common[0], "end": common[-1],
            "port_ann": round(nav ** (1 / yrs) - 1, 4),
            "index_ann": round(base ** (1 / yrs) - 1, 4),
            "ann_excess": round((nav / base) ** (1 / yrs) - 1, 4)}


# ------------------------------------------------------------------ 驱动
def build_grids(panel: fs.Panel, meta: dict, ev: list, steps: tuple[int, ...],
                frames: dict[int, pd.DataFrame], start: str, log=print):
    """调仓日 × 因子表 → {(factor,universe,step): {i: [code 降序]}} + 基准宇宙。

    `frames` 由 `fs.build_frames` 产出(PIT 因子表,与 fcfy_study.py 同一份,
    可用 --frames-cache 复用)。宇宙清洗在这里做,每票每日一次。
    """
    grids = fs.rebalance_indices(panel, start, steps)
    every_i = sorted({i for v in grids.values() for i in v})
    ranked = {(f, u, s): {} for f, u, s in fs.trial_configs()}
    uni_by_i: dict[int, pd.Index] = {}
    for n, i in enumerate(every_i):
        d_iso = panel.dates[i]
        ff = frames.get(i)
        if ff is None or ff.empty:
            continue
        st = fs.st_codes_at(ev, d_iso)
        raw_uni = {u: fs.universe_at(panel, i, meta, st, u) for u in fs.UNIVERSES}
        uni = {u: raw_uni[u].intersection(ff.index) for u in fs.UNIVERSES}
        # 基准 = **可交易全宇宙**等权(含当日算不出 FcfY 的票);用交集当基准
        # 会把「没有现金流的长尾」从基准里偷偷删掉,超额被系统性高估。
        uni_by_i[i] = raw_uni["base"]
        for s in steps:
            if i not in grids[s]:
                continue
            for f in fs.FACTORS:
                for u in fs.UNIVERSES:
                    ser = ff.loc[uni[u], f].dropna()
                    if len(ser) < 30:
                        continue
                    ranked[(f, u, s)][i] = list(ser.sort_values(ascending=False).index)
    return ranked, uni_by_i, grids


def run_backtest(con, panel: fs.Panel, *, start: str = fs.START,
                 steps: tuple[int, ...] = fs.STEPS, cash: float = 1e6,
                 primary=fs.PRIMARY, sweep_cash: tuple[float, ...] = (),
                 market_db: str = "", frames_cache: str = "", log=print) -> dict:
    """18 个 trial 全跑 → 主口径绩效 + DSR/PBO/换手/逐年 四道闸。"""
    meta = fs.load_static_meta(con)
    ev = fs.st_events(con)
    grids = fs.rebalance_indices(panel, start, steps)
    every_i = sorted({i for v in grids.values() for i in v})
    frames = fs.build_frames(con, panel, every_i, market_db=market_db,
                             cache=frames_cache, log=log)
    ranked, uni_by_i, _ = build_grids(panel, meta, ev, steps, frames.frames,
                                      start, log)
    sims = {cfg: simulate(panel, r, uni_by_i, cfg[2], cash=cash)
            for cfg, r in ranked.items() if r}
    if primary not in sims or not sims[primary].daily:
        return {"error": "主口径无有效模拟(数据覆盖不足?)",
                "trials_with_data": [list(k) for k in sims]}
    prim = sims[primary]
    rp, rb = to_series(prim.daily), to_series(prim.daily_bench)
    perf = perf_from_daily(rp, rb, panel, primary[2])
    rg = to_series({k: v for k, v in sim_gross(prim, panel).items()})
    perf_gross = perf_from_daily(rg, rb, panel, primary[2])
    # ---- 账本:全部 trial 抽样到主口径网格后的每期超额
    cols, names, ledger = [], [], []
    per_trial: dict[str, dict] = {}
    for cfg, sim in sorted(sims.items()):
        if not sim.daily or not sim.ends:
            continue
        common_ends = _common_ends(prim.ends, sim)
        p = grid_returns(to_series(sim.daily), common_ends)
        b = grid_returns(to_series(sim.daily_bench), common_ends)
        if len(p) < 12:
            continue
        ex = p - b
        cols.append(pd.Series(ex, index=common_ends))
        names.append("%s|%s|%d" % cfg)
        sr = sharpe_per_obs(list(ex))
        ledger.append(sr)
        yrs = len(common_ends) / (TRADING_DAYS / primary[2])
        # 年化超额用**净值比**口径(与 perf_from_daily 一致),不用算术差:
        # prod(1+p)/prod(1+b) 才是"同期同基准"的真实差,算术差会把波动漏算进去。
        per_trial[names[-1]] = {
            "sharpe_per_grid_period": round(sr, 4),
            "ann_excess_vs_ew": round(float(
                (np.prod(1.0 + p) / np.prod(1.0 + b)) ** (1 / yrs) - 1.0), 4),
            "ann_return": round(float(np.prod(1.0 + p) ** (1 / yrs) - 1.0), 4),
            "mean_cost_per_period_pct": round(float(np.mean(sim.cost)) * 100, 4),
            "annual_turnover_one_way": round(float(np.mean(sim.turn))
                                             * (TRADING_DAYS / cfg[2]), 4),
            "grid_periods": len(common_ends), "n_periods": sim.n_periods,
        }
    mat = pd.concat(cols, axis=1)
    mat.columns = names
    prim_ex = grid_returns(rp, prim.ends) - grid_returns(rb, prim.ends)
    dsr = deflated_sharpe_ratio(list(prim_ex), ledger)
    pbo = pbo_cscv(mat.values, n_splits=16)
    turn_year = float(np.mean(prim.turn)) * (TRADING_DAYS / primary[2])
    yrs = [y for y in GATE_YEAR_WINDOW if y in perf["by_year"]]
    pos = sum(1 for y in yrs if perf["by_year"][y]["excess"] >= 0)
    gates = {
        "dsr": {"value": round(dsr["dsr"], 4), "min": GATE_DSR,
                "pass": bool(dsr["dsr"] >= GATE_DSR),
                "sr0_benchmark_per_period": round(dsr["sr0_benchmark"], 5),
                "sr_observed_per_period": round(dsr["sr_observed"], 5),
                "n_trials": dsr["n_trials"], "n_obs": int(len(prim_ex))},
        "pbo": {"value": round(pbo["pbo"], 4), "max": GATE_PBO,
                "pass": bool(pbo["pbo"] <= GATE_PBO),
                "n_configs": pbo["n_configs"], "n_splits": pbo["n_splits"],
                "median_logit": round(pbo["median_logit"], 3)},
        "turnover": {"value": round(turn_year, 4), "max": GATE_TURNOVER,
                     "pass": bool(turn_year <= GATE_TURNOVER)},
        "yearly_excess": {"positive_years": pos, "years": len(yrs),
                          "min": GATE_POS_YEARS, "pass": bool(pos >= GATE_POS_YEARS)},
    }
    gates["all_pass"] = all(g["pass"] for g in gates.values() if isinstance(g, dict))
    idx_cmp = {label: vs_index(rp, panel, load_index_csv(fn))
               for label, fn in (("csi300_price", "idx_000300_SH.csv"),
                                 ("csi300_total_return", "idx_H00300_CSI.csv"),
                                 ("sse_composite_price", "idx_000001_SH.csv"))}
    sweep: dict[str, dict] = {}
    for c in sweep_cash:
        sim = simulate(panel, ranked.get(primary, {}), uni_by_i, primary[2], cash=c)
        if not sim.daily:
            continue
        p_c, b_c = to_series(sim.daily), to_series(sim.daily_bench)
        pf = perf_from_daily(p_c, b_c, panel, primary[2])
        sweep[int(c)] = {"ann_return": pf["ann_return"],
                         "ann_excess_vs_ew": pf["ann_excess_vs_ew"],
                         "mean_cost_per_period_pct": round(
                             float(np.mean(sim.cost)) * 100, 4),
                         "mean_impact_per_period_pct": round(float(np.mean(
                             [x["impact"] for x in sim.periods])) * 100, 4),
                         "annual_turnover": round(float(np.mean(sim.turn))
                                                  * (TRADING_DAYS / primary[2]), 4)}
    return {
        "capacity_sweep": sweep,
        "primary": {"config": "%s|%s|%d" % primary, "cash": cash, "top_n": TOP_N,
                    "perf": perf, "annual_turnover_one_way": round(turn_year, 4),
                    "mean_cost_per_period_pct": round(float(np.mean(prim.cost)) * 100, 4),
                    "mean_fee_per_period_pct": round(float(np.mean(
                        [p["fee"] for p in prim.periods])) * 100, 4),
                    "mean_impact_per_period_pct": round(float(np.mean(
                        [p["impact"] for p in prim.periods])) * 100, 4),
                    "mean_div_tax_per_period_pct": round(float(np.mean(
                        [p["div_tax"] for p in prim.periods])) * 100, 4),
                    "n_periods": prim.n_periods,
                    "ann_excess_vs_ew_gross": perf_gross["ann_excess_vs_ew"],
                    "ann_return_gross": perf_gross["ann_return"],
                    "nav_last": round(float((1.0 + rp).prod()), 4),
                    "bench_ew_last": round(float((1.0 + rb).prod()), 4)},
        "gates": gates, "vs_index": idx_cmp,
        "trial_ledger": per_trial,
        "periods": prim.periods,
        "perf_matrix": {"rows": int(mat.shape[0]), "cols": names},
    }


def _common_ends(ends: list[int], sim: Sim) -> list[int]:
    """主口径的网格结束日(其他 trial 的日频路径覆盖同一批日历日)。"""
    return list(ends)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market-db", default=fs.DEFAULT_MARKET_DB)
    ap.add_argument("--start", default=fs.START)
    ap.add_argument("--end", default="")
    ap.add_argument("--steps", default=",".join(str(s) for s in fs.STEPS))
    ap.add_argument("--cash", type=float, default=1_000_000.0)
    ap.add_argument("--cash-sweep", default="1000000,10000000,100000000",
                    help="容量对照:同口径逐档重跑主口径(不进账本)")
    ap.add_argument("--codes", type=int, default=0, help="冒烟:跨市场均匀抽 N 票")
    ap.add_argument("--frames-cache", default="",
                    help="因子表 pickle 缓存路径(与 fcfy_study.py 共用)")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    steps = tuple(int(x) for x in str(args.steps).split(","))
    con = fs.connect_ro(args.market_db)
    end = args.end or con.execute("SELECT MAX(date) FROM daily_quotes").fetchone()[0]
    codes = None
    if args.codes:
        allc = [r[0] for r in con.execute(
            "SELECT DISTINCT code FROM daily_quotes WHERE date<=? ORDER BY code",
            (end,))]
        stride = max(1, len(allc) // args.codes)
        codes = allc[::stride][:args.codes]
    panel = fs.load_panel(con, fs.panel_start(con, end), end, need_open=True,
                          need_amount=True, need_turnover=True, codes=codes,
                          extra_cols=("pe_ttm", "pb", "dv_ratio"))
    sweep_cash = tuple(float(x) for x in str(args.cash_sweep).split(",") if x)
    out = run_backtest(con, panel, start=args.start, steps=steps, cash=args.cash,
                       sweep_cash=sweep_cash, market_db=str(args.market_db),
                       frames_cache=args.frames_cache)
    n_cf = con.execute("SELECT COUNT(DISTINCT code) FROM cashflow_items").fetchone()[0]
    n_q = con.execute("SELECT COUNT(DISTINCT code) FROM daily_quotes").fetchone()[0]
    out["meta"] = {"market_db": str(args.market_db), "start": args.start,
                   "end": end, "codes_in_panel": len(panel.codes),
                   "codes_limit": args.codes or None, "cash": args.cash,
                   "top_n": TOP_N, "steps": list(steps),
                   "cashflow_codes": n_cf, "quote_codes": n_q,
                   "cashflow_coverage": round(n_cf / max(n_q, 1), 4),
                   "smoke": bool(args.codes) or n_cf < 0.9 * n_q,
                   "cost_model": "AShareCommission(万2.5+5元下限/印花税分段/过户费)"
                                 " + VolumeImpactSlippage(5bp+5bp√参与率)"
                                 " + 红利税三档 —— 与回测引擎同源"}
    print(json.dumps({k: out[k] for k in
                      ("primary", "gates", "vs_index", "capacity_sweep", "meta")
                      if k in out}, ensure_ascii=False, indent=2, default=str))
    if out.get("gates", {}).get("all_pass"):
        print(">>> 四道闸全过")
    else:
        print(">>> 有闸不过 —— 见 gates 明细")
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2,
                                             default=str), encoding="utf-8")
        print("saved ->", args.json)


if __name__ == "__main__":
    main()
