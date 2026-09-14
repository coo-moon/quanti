"""回购事件组合级回测 + 过拟合闸(DSR/PBO)—— 任务书第二、三步。

主口径 = 第一步**过闸的预声明切片**(账本 data/repurchase_event_study.json:主口径
预案|all|20 自己过闸就用它,否则用升格的那个切片)。规则本身照任务书:每 20
交易日调仓日 D,候选 = ann_date ∈ (D−L, D] 的新事件(主口径 L=20;同票取最近一次),
按力度 amount/total_mv 降序 top-30 等权,**次日 open 成交**;掉出候选名单即卖,
每票最长持有 60 交易日强制卖;清洗与第一步同一套(剔金融/ST/次新/D0 不可成交),
再叠加「该票近 12 个月内有 proc='停止' 公告 ⇒ 不入池」(预案画饼尾部风险的排除项)。

成本路径与引擎同源(直接复用 fcfy_backtest 已在用的那三个对象):
  - AShareCommission:佣金万2.5 + 单笔 5 元下限、印花税只在卖出侧(2023-08-28
    前千1、之后万5)、过户费十万分之一双边;
  - VolumeImpactSlippage:5bp 底 + 5bp×√参与率,参与率用**信号日之前** 20 个
    交易日的日均成交额;
  - dividend_tax_rate:红利税三档(hfq 表达不出来的那部分现金流)。
向量化近似(与引擎的差别如实写进文档):不含整手取整、不建模涨跌停与停牌买不进
(两端必须有 bar 才成交)、冲击按 ADV20 摊到每票、红利税用股息率均值近似。

基准:(a) 全市场等权(主,与第一步同源;open→open 毛收益、不计成本 ⇒ 超额偏保守);
(b) **同规模档等权**(诊断:升格切片是小市值专属,不并排看就分不清「回购信息」和
「小盘 vs 全市场等权」的风格差);(c) 沪深300 价格指数 + 全收益 H00300.CSI
(data/idx_*.csv 本地有日线;只有对全收益比才同口径)。

多重检验账本:DSR 的 trial_sharpes 与 PBO 的 perf matrix = **全部组合层实测变体**
(3 proc × 6 预声明切片 × 3 候选窗 L)统一在同一张 20 日网格上算(空仓期如实计
0% 现金收益,不偷偷「跟上基准」)。top-N=30 不搜索。闸值不自行放宽:DSR ≥ 0.95、
PBO ≤ 0.2、年单边换手 ≤ 300%、逐年超额 ≥ 0 的年数 ≥ 4/5(2022-2026)。

market.db 只读;报告 JSON 落 data/(不提交)。

用法:
    /opt/data/quanti/.venv/bin/python scripts/repurchase_bt.py --cash 1000000 \
        --study-json data/repurchase_event_study.json --json data/repurchase_bt.json
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import fcfy_backtest as fb  # noqa: E402  成本模型/绩效度量/指数对账 同源复用
import fcfy_study as fs  # noqa: E402
import repurchase_event_study as rp  # noqa: E402
from quanti.backtest.commission import AShareCommission  # noqa: E402
from quanti.backtest.overfit import (  # noqa: E402
    deflated_sharpe_ratio, pbo_cscv, sharpe_per_obs)
from quanti.backtest.slippage import VolumeImpactSlippage  # noqa: E402

TOP_N = 30                    # 任务书钉死,不搜 N(搜了又多一层拟合)
STEP = 20                     # 调仓步长(交易日)
MAX_HOLD = 60                 # 每票最长持有(交易日)
LOOKBACKS = (20, 40, 60)      # 候选公告窗 L(交易日);任务书主口径 = 20
STOP_COOL_DAYS = 365          # 「停止回购」排除期(自然日)
GATE_DSR = 0.95
GATE_PBO = 0.20
GATE_TURNOVER = 3.0           # 年单边换手 ≤ 300%
GATE_POS_YEARS = 4
GATE_YEAR_WINDOW = ("2022", "2023", "2024", "2025", "2026")
TRADING_DAYS = fb.TRADING_DAYS


# ------------------------------------------------------------------ 候选池
def load_stop_dates(con) -> dict[str, list[str]]:
    """code → 停止回购公告日列表(排除「画饼后翻车」的票)。"""
    out: dict[str, list[str]] = {}
    for code, ann in con.execute("SELECT code, ann_date FROM repurchase_events "
                                 "WHERE proc = ?", (rp.STOP_PROC,)):
        out.setdefault(str(code), []).append(str(ann)[:10])
    return {k: sorted(v) for k, v in out.items()}


def rebalance_grid(panel: fs.Panel, step: int = STEP) -> list[int]:
    """全变体共用的调仓日栅格(同相位才可比;起点 = 事件窗下界后首日)。"""
    first = next((i for i, d in enumerate(panel.dates) if d >= rp.EVENT_START),
                 None)
    if first is None:
        return []
    # 右端裁掉「放不下 信号日→次日建仓→下期次日出清」整串的栅格点
    return list(range(first, max(0, len(panel.dates) - step), step))


def build_pool(panel: fs.Panel, et: rp.EventTable, proc: str, slice_key: str,
               lookback: int, terc, stop_map: dict[str, list[str]],
               grid: list[int], top_n: int = TOP_N) -> dict[int, list[str]]:
    """调仓日 → 目标名单(力度降序 top-N;同票多次公告取最近一次)。

    PIT:候选只用 ann_date ≤ D 的行;力度/规模都用**公告日当时**的值;并要求
    D0 ≤ D+1 ⇒ 「次日 open 建仓」那一刻该事件其实已经可交易(不含前视)。
    """
    if et.frame.empty:
        return {}
    f = et.frame
    ann_i = np.searchsorted(np.asarray(panel.dates),
                            f["ann_date"].values.astype(str), side="right") - 1
    d0_i = f["d0i"].values.astype(int)
    codes = f["code"].values.astype(str)
    ann_d = f["ann_date"].values.astype(str)
    inten = f["intensity"].values.astype(float)
    keep0 = rp.slice_masks(et, terc, proc).get(slice_key)
    if keep0 is None:
        return {}
    neg = float("-inf")
    out: dict[int, list[str]] = {}
    for i in grid:
        d_iso = panel.dates[i]
        span = (ann_i <= i) & (ann_i > i - lookback) & (d0_i <= i + 1) & keep0
        cand = np.flatnonzero(span)
        if cand.size == 0:
            continue
        cutoff = (pd.Timestamp(d_iso).date()
                  - timedelta(days=STOP_COOL_DAYS)).isoformat()
        latest: dict[str, tuple[str, float]] = {}
        for k in cand:
            code = codes[k]
            sd = stop_map.get(code)
            if sd and any(cutoff < x <= d_iso for x in sd):
                continue                          # 近 12 个月停止过回购 ⇒ 剔
            iv = float(inten[k]) if np.isfinite(inten[k]) else neg
            cur = latest.get(code)
            if cur is None or (ann_d[k], iv) > cur:
                latest[code] = (ann_d[k], iv)
        if not latest:
            continue
        ranked = sorted(latest.items(), key=lambda kv: (-kv[1][1], kv[0]))
        out[i] = [c for c, _ in ranked[:top_n]]
    return out


# ------------------------------------------------------------------ 模拟
@dataclass
class Sim:
    """一个 trial 的模拟结果:日频净收益路径(键=交易日序号)+ 逐期诊断。"""

    daily: dict[int, float] = field(default_factory=dict)
    daily_bench: dict[int, float] = field(default_factory=dict)
    daily_band: dict[int, float] = field(default_factory=dict)
    ends: list[int] = field(default_factory=list)
    periods: list[dict] = field(default_factory=list)
    turn: list[float] = field(default_factory=list)
    cost: list[float] = field(default_factory=list)

    @property
    def n_periods(self) -> int:
        return len(self.ends)


def ew_open_to_open(o: np.ndarray, e: int, x: int,
                    mask: np.ndarray | None) -> float:
    """等权基准 open→open 毛收益(不计成本 ⇒ 让超额更保守)。"""
    p0, p1 = o[e], o[x]
    ok = np.isfinite(p0) & np.isfinite(p1) & (p0 > 0)
    if mask is not None:
        ok = ok & mask
    if not ok.any():
        return 0.0
    return float(np.mean(p1[ok] / p0[ok] - 1.0))


def band_mask(panel: fs.Panel, i: int, terc, band: str) -> np.ndarray | None:
    """信号日 D 当时的规模档成员(诊断基准用;非规模档 → None = 全市场)。"""
    if terc is None or panel.mv is None or band not in rp.SIZE_BANDS:
        return None
    mv = np.asarray(panel.mv[i], dtype=np.float64)
    has = np.isfinite(mv) & (mv > 0)
    lo, hi = terc
    if band == "size_small":
        return has & (mv <= lo)
    if band == "size_mid":
        return has & (mv > lo) & (mv <= hi)
    return has & (mv > hi)


def simulate(panel: fs.Panel, pool: dict[int, list[str]], grid: list[int], *,
             cash: float, step: int = STEP, max_hold: int = MAX_HOLD,
             terc=None, band_for_bench: str = "all") -> Sim:
    """top-N 等权、每 step 交易日调仓、单票最长持有 max_hold 的全成本模拟。

    时间线(与引擎一致):信号日 D=i → **次日开盘**建仓 → 下一信号日的次日开盘
    调仓 ⇒ 持有期恰好 step 个交易日;上期持仓在 x 的 open 出清,本期再按目标权重
    建仓,交易成本与红利税计在本期首日(= 实际成交日)。无候选的期 = 空仓(收益 0,
    只付清仓成本),**不**假装有基准收益。
    """
    comm = AShareCommission()
    slip = VolumeImpactSlippage()
    sim = Sim()
    pos = {c: j for j, c in enumerate(panel.codes)}
    opn = np.asarray(panel.open_, dtype=np.float64)
    entry: dict[str, int] = {}          # 建仓时的信号日序号(算持有交易日数)
    w_prev: dict[str, float] = {}
    nav = float(cash)
    for i in grid:
        e, x = i + 1, i + step + 1
        if x >= len(panel.dates) or nav <= 0:
            break
        b = ew_open_to_open(opn, e, x, None)
        bb = ew_open_to_open(opn, e, x, band_mask(panel, i, terc,
                                                  band_for_bench))
        _spread(sim.daily_bench, e, x, b)
        _spread(sim.daily_band, e, x, bb)
        # 目标 = 候选 top-N,再把「再持有 step 就超过 max_hold」的老仓位挤出去
        target: list[str] = []
        n_forced = 0
        for c in pool.get(i, []):
            j = pos.get(c)
            if j is None:
                continue
            age = (i - entry[c]) if (c in w_prev and c in entry) else 0
            if age + step > max_hold:
                n_forced += 1
                continue
            if (np.isfinite(opn[e][j]) and opn[e][j] > 0
                    and np.isfinite(opn[x][j]) and opn[x][j] > 0):
                target.append(c)
        w_new = {c: 1.0 / len(target) for c in target} if target else {}
        buy, sell = {}, {}
        for c in set(w_new) | set(w_prev):
            delta = (w_new.get(c, 0.0) - w_prev.get(c, 0.0)) * nav
            if delta > 1.0:
                buy[c] = delta
            elif delta < -1.0:
                sell[c] = -delta
        cost = (fb.period_cost(buy, sell, fb.adv20_map(panel, i),
                               pd.Timestamp(panel.dates[e]).date(), comm, slip)
                if (buy or sell) else
                {"impact": 0.0, "fee": 0.0, "total": 0.0})
        cal_days = (pd.Timestamp(panel.dates[x]).date()
                    - pd.Timestamp(panel.dates[e]).date()).days
        dtax = fb.dividend_tax_frac(panel, i, w_new, cal_days)
        cost_frac = cost["total"] / nav + dtax
        turn = (sum(buy.values()) + sum(sell.values())) / 2.0 / nav
        if target:
            idx = [pos[c] for c in target]
            raw = np.asarray(panel.close[e:x + 1][:, idx], dtype=np.float64)
            # 停牌日无 bar → 价格冻结 ffill。**不能**把「没有报价」当成 -100%
            pc = pd.DataFrame(raw).ffill().to_numpy()
            w = np.fromiter((w_new[c] for c in target), dtype=np.float64)
            r_day = _first_day_ret(pc[0], opn[e][idx], w) - cost_frac
            _acc(sim.daily, e, r_day)
            rets = [r_day]
            for t in range(1, x - e):
                ratios = pc[t] / pc[t - 1]
                r = float(np.nansum(ratios * w) - 1.0)
                w = w * ratios / (1.0 + r) if r > -0.999 else w
                _acc(sim.daily, e + t, r)
                rets.append(r)
            r_exit = float(np.nansum((opn[x][idx] / pc[-2] - 1.0) * w))
            _acc(sim.daily, x, r_exit)
            rets.append(r_exit)
            end_val = {c: float(w[k]) for k, c in enumerate(target)}
            ssum = float(np.nansum(list(end_val.values())))
            w_prev = ({c: v / ssum for c, v in end_val.items()}
                      if ssum > 0 else {})
            entry = {c: entry.get(c, i) for c in target}
        else:
            _acc(sim.daily, e, -cost_frac)
            rets = [-cost_frac]
            w_prev, entry = {}, {}
        net = float(np.prod([1.0 + v for v in rets]) - 1.0)
        nav *= (1.0 + net)
        sim.ends.append(x)
        sim.periods.append({"signal": panel.dates[i], "entry": panel.dates[e],
                            "exit": panel.dates[x],
                            "n_cand": len(pool.get(i, [])),
                            "n_hold": len(target), "n_forced_out": n_forced,
                            "net": net, "cost_frac": cost_frac,
                            "fee": cost["fee"] / nav,
                            "impact": cost["impact"] / nav, "div_tax": dtax,
                            "turn": turn, "bench_ew": b, "bench_band": bb,
                            "names": target[:5]})
        sim.turn.append(turn)
        sim.cost.append(cost_frac)
    return sim


def _first_day_ret(close_at_entry: np.ndarray, open_at_entry: np.ndarray,
                   w: np.ndarray) -> float:
    """建仓日 open→close 的加权收益(等权 ⇒ Σ w_i·r_i)。"""
    return float(np.nansum((close_at_entry / open_at_entry - 1.0) * w))


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


# ------------------------------------------------------------------ 主流程
def trial_configs() -> list[tuple[str, str, int]]:
    """组合层全部实测变体 = proc × 预声明切片 × 候选窗 L(= DSR/PBO 账本)。

    与第一步账本同一批切片口径:**力度切片只对预案成立**(其他 proc 的 amount
    是已回购/进展金额,和「拟回购下限」不可比)⇒ 3×4×3 + 1×2×3 = 42 个变体,
    一个都不藏(过闸的主口径也只是其中之一)。
    """
    out = []
    for proc in rp.PROCS:
        for sl in rp.SLICES:
            if sl.startswith("intensity") and proc != rp.PRIMARY_PROC:
                continue
            for L in LOOKBACKS:
                out.append((proc, sl, L))
    return out


def primary_config(study: dict) -> tuple[str, str, int]:
    """主口径:第一步主口径过闸则用之,否则用**升格**的预声明切片。

    返回 (proc, slice, car_window);组合层候选窗固定 L=STEP(任务书主口径,不搜
    L ⇒ 少一层拟合),其余 L 只作为账本变体存在。
    """
    if study.get("gate_pass"):
        return (rp.PRIMARY_PROC, "all", rp.PRIMARY_WINDOW)
    for key in study.get("passing_variants") or []:
        proc, sl, w = key.split("|")
        return (proc, sl, int(w))
    return (rp.PRIMARY_PROC, "all", rp.PRIMARY_WINDOW)


def load_event_tables(con, panel: fs.Panel) -> dict[str, rp.EventTable]:
    """一次清洗,三个 proc 口径共用(与第一步同一套判定与主基准)。"""
    meta = fs.load_static_meta(con)
    st_ev = fs.st_events(con)
    mkt = rp.build_market(panel)
    return {proc: rp.collect_events(con, panel, mkt, proc, panel.dates[-1],
                                    meta=meta, st_events=st_ev)
            for proc in rp.PROCS}


def run_backtest(con, panel: fs.Panel, study: dict, *, cash: float = 1e6,
                 log=print) -> dict:
    """全部组合层变体跑完 → 主口径绩效 + DSR/PBO/换手/逐年 四道闸。"""
    terc = (tuple(study["size_tercile_mv_wan"])
            if study.get("size_tercile_mv_wan") else None)
    tables = load_event_tables(con, panel)
    stop_map = load_stop_dates(con)
    grid = rebalance_grid(panel)
    prim = primary_config(study)
    log("主口径(组合层)= %s|%s(来自 CAR +%d)候选窗 L=%d top-%d 等权;"
        "调仓日 %d 个 %s..%s" % (prim[0], prim[1], prim[2], STEP, TOP_N,
                                 len(grid), panel.dates[grid[0]],
                                 panel.dates[grid[-1]]))
    sims: dict[tuple, Sim] = {}
    for cfg in trial_configs():
        if cfg[0] not in tables:
            continue
        pool = build_pool(panel, tables[cfg[0]], cfg[0], cfg[1], cfg[2],
                          terc, stop_map, grid)
        sim = simulate(panel, pool, grid, cash=cash, terc=terc,
                       band_for_bench=cfg[1])
        if sim.ends:
            sims[cfg] = sim
    pkey = (prim[0], prim[1], STEP)
    if pkey not in sims or not sims[pkey].daily:
        return {"error": "主口径无有效模拟(候选太稀?)",
                "primary_config": list(pkey),
                "configs_with_data": [list(k) for k in sims]}
    psim = sims[pkey]
    r_p = fb.to_series(psim.daily)
    r_b = fb.to_series(psim.daily_bench)
    r_band = fb.to_series(psim.daily_band)
    perf = fb.perf_from_daily(r_p, r_b, panel, STEP)
    perf_band = fb.perf_from_daily(r_p, r_band, panel, STEP)

    # ---- 账本:全部 trial 在同一张网格上的每期超额
    ends = psim.ends
    base_b = fb.grid_returns(r_b, ends)
    cols, names, ledger, per_trial = [], [], [], {}
    degenerate: list[str] = []          # 全程零持仓的变体(切片在该 proc 无候选)
    insufficient: list[str] = []        # 有效期数 <12 的变体
    for cfg, sim in sorted(sims.items()):
        if not any(q["n_hold"] for q in sim.periods):
            # 一期都没建仓(该切片在该 proc 下无候选)⇒ 不算「实测变体」,
            # 但也绝不静默丢弃:如实记进 degenerate_configs。
            degenerate.append("%s|%s|%d" % cfg)
            continue
        p = fb.grid_returns(fb.to_series(sim.daily), ends)
        if len(p) < 12:
            insufficient.append("%s|%s|%d" % cfg)
            continue
        bb_g = fb.grid_returns(fb.to_series(sim.daily_band), ends)
        ex = p - base_b
        cols.append(pd.Series(ex, index=ends))
        names.append("%s|%s|%d" % cfg)
        sr = sharpe_per_obs(list(ex))
        ledger.append(sr)
        yrs = len(ends) / (TRADING_DAYS / STEP)
        per_trial[names[-1]] = {
            "sharpe_per_grid_period": round(sr, 4),
            "ann_excess_vs_ew": round(float(
                (np.prod(1.0 + p) / np.prod(1.0 + base_b))
                ** (1 / yrs) - 1.0), 4),
            "ann_excess_vs_own_band_ew": round(float(
                (np.prod(1.0 + p) / np.prod(1.0 + bb_g))
                ** (1 / yrs) - 1.0), 4),
            "n_periods": sim.n_periods,
            "empty_periods": int(sum(1 for q in sim.periods
                                     if q["n_hold"] == 0)),
            "mean_candidates": round(float(np.mean(
                [q["n_cand"] for q in sim.periods])), 2),
            "mean_cost_per_period_pct": round(float(np.mean(sim.cost)) * 100, 4),
            "annual_turnover_one_way": round(float(np.mean(sim.turn))
                                             * (TRADING_DAYS / STEP), 4),
        }
    mat = pd.concat(cols, axis=1)
    mat.columns = names
    # ---- 后验敏感性:账本里 Sharpe 最高的那个变体单独过一遍闸(只为回答
    # 「换个人挑最优能不能过」——它是**事后选择**,绝不能当主口径用)
    sens = []
    for nm in names:
        ex_s = np.asarray(mat[nm].values, dtype=float)
        d2 = deflated_sharpe_ratio(list(ex_s), ledger)
        cfg_sim = sims[tuple(x for x in nm.split("|")[0:2])
                       + (int(nm.split("|")[2]),)]
        sens.append({"config": nm,
                     "sharpe_per_grid_period": round(sharpe_per_obs(list(ex_s)), 4),
                     "dsr": round(d2["dsr"], 4),
                     "annual_turnover_one_way": round(
                         float(np.mean(cfg_sim.turn)) * (TRADING_DAYS / STEP), 4),
                     "pass_dsr": bool(d2["dsr"] >= GATE_DSR),
                     "pass_turnover": bool(float(np.mean(cfg_sim.turn))
                                           * (TRADING_DAYS / STEP)
                                           <= GATE_TURNOVER)})
    sens.sort(key=lambda r: -r["sharpe_per_grid_period"])
    best_post_hoc = {
        "note": "事后挑最优(有选择偏差,不作主口径);用它过闸仍要同时看换手",
        "top3": sens[:3],
        "any_trial_passes_both": bool(any(r["pass_dsr"] and r["pass_turnover"]
                                          for r in sens)),
        "n_trials": len(sens)}
    prim_ex = fb.grid_returns(r_p, ends) - base_b
    dsr = deflated_sharpe_ratio(list(prim_ex), ledger)
    pbo = pbo_cscv(mat.values, n_splits=16)
    turn_year = float(np.mean(psim.turn)) * (TRADING_DAYS / STEP)
    yrs = [y for y in GATE_YEAR_WINDOW if y in perf["by_year"]]
    pos_y = sum(1 for y in yrs if perf["by_year"][y]["excess"] >= 0)
    yrs_b = [y for y in GATE_YEAR_WINDOW if y in perf_band["by_year"]]
    pos_yb = sum(1 for y in yrs_b if perf_band["by_year"][y]["excess"] >= 0)
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
        "yearly_excess": {"positive_years": pos_y, "years": len(yrs),
                          "min": GATE_POS_YEARS,
                          "pass": bool(pos_y >= GATE_POS_YEARS),
                          "basis": "任务书主基准(全市场等权)"},
        "yearly_excess_vs_band": {"positive_years": pos_yb,
                                  "years": len(yrs_b),
                                  "min": GATE_POS_YEARS,
                                  "pass": bool(pos_yb >= GATE_POS_YEARS),
                                  "basis": "诊断:同规模档基准(不参与验收)"},
    }
    gates["all_pass"] = all(gates[k]["pass"] for k in
                            ("dsr", "pbo", "turnover", "yearly_excess"))
    idx_cmp = {label: fb.vs_index(r_p, panel, fb.load_index_csv(fn))
               for label, fn in (("csi300_price", "idx_000300_SH.csv"),
                                 ("csi300_total_return", "idx_H00300_CSI.csv"))}
    return {
        "primary": {"config": "%s|%s|%d" % pkey, "from_car_window": prim[2],
                    "cash": cash, "top_n": TOP_N, "step": STEP,
                    "max_hold": MAX_HOLD, "lookback": STEP,
                    "perf_vs_market_ew": perf, "perf_vs_band_ew": perf_band,
                    "annual_turnover_one_way": round(turn_year, 4),
                    "mean_cost_per_period_pct": round(
                        float(np.mean(psim.cost)) * 100, 4),
                    "mean_fee_per_period_pct": round(float(np.mean(
                        [p["fee"] for p in psim.periods])) * 100, 4),
                    "mean_impact_per_period_pct": round(float(np.mean(
                        [p["impact"] for p in psim.periods])) * 100, 4),
                    "mean_div_tax_per_period_pct": round(float(np.mean(
                        [p["div_tax"] for p in psim.periods])) * 100, 4),
                    "n_periods": psim.n_periods,
                    "empty_periods": int(sum(1 for q in psim.periods
                                             if q["n_hold"] == 0)),
                    "forced_exits": int(sum(q["n_forced_out"]
                                            for q in psim.periods)),
                    "nav_last": round(float((1.0 + r_p).prod()), 4),
                    "bench_ew_last": round(float((1.0 + r_b).prod()), 4),
                    "bench_band_last": round(float((1.0 + r_band).prod()), 4)},
        "gates": gates, "vs_index": idx_cmp,
        "best_post_hoc_sensitivity": best_post_hoc,
        "candidate_distribution": _candidate_distribution(psim),
        "trial_ledger": per_trial,
        "ledger_integrity": {"n_configs": len(trial_configs()),
                             "in_ledger": len(names),
                             "degenerate_zero_hold": degenerate,
                             "insufficient_periods": insufficient},
        "periods": psim.periods,
        "perf_matrix": {"rows": int(mat.shape[0]), "cols": names},
    }


def _candidate_distribution(psim: Sim) -> dict:
    """每期候选数分布(2021-2023 事件稀疏 ⇒ 组合层该段数字意义有限)。"""
    n = np.asarray([p["n_cand"] for p in psim.periods], dtype=float)
    if n.size == 0:
        return {}
    by_year: dict[str, list[dict]] = {}
    for p in psim.periods:
        by_year.setdefault(p["signal"][:4], []).append(p)

    def _avg(rows, key):
        return round(100.0 * float(np.mean([q[key] for q in rows])), 4)
    return {"min_p10_p25_p50_p75_max":
            [round(float(np.quantile(n, q)), 1)
             for q in (0, .1, .25, .5, .75, 1)],
            "mean": round(float(n.mean()), 2),
            "periods_below_top_n": int((n < TOP_N).sum()),
            "periods_with_zero": int((n == 0).sum()),
            "mean_candidates_by_year": {
                y: round(float(np.mean([q["n_cand"] for q in v])), 1)
                for y, v in sorted(by_year.items())},
            "mean_net_by_year_pct": {y: _avg(v, "net")
                                     for y, v in sorted(by_year.items())},
            "mean_bench_ew_by_year_pct": {y: _avg(v, "bench_ew")
                                          for y, v in sorted(by_year.items())}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market-db", default=rp.DEFAULT_MARKET_DB)
    ap.add_argument("--end", default="", help="默认取 daily_quotes 最后一天")
    ap.add_argument("--cash", type=float, default=1_000_000.0)
    ap.add_argument("--study-json", default="",
                    help="第一步事件研究 JSON(取升格切片与规模档切点)")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    con = fs.connect_ro(args.market_db)
    end = args.end or con.execute(
        "SELECT MAX(date) FROM daily_quotes").fetchone()[0]
    start = con.execute("SELECT MIN(date) FROM daily_quotes").fetchone()[0]
    panel = fs.load_panel(con, start, end, need_open=True, need_amount=True,
                          extra_cols=("dv_ratio",))
    if args.study_json and Path(args.study_json).exists():
        study = json.loads(Path(args.study_json).read_text(encoding="utf-8"))
    else:
        study = rp.run_study(con, panel, end_iso=str(end)[:10])
    out = run_backtest(con, panel, study, cash=args.cash)
    out["meta"] = {"market_db": str(args.market_db), "panel_start": start,
                   "panel_end": end, "cash": args.cash, "top_n": TOP_N,
                   "step": STEP, "max_hold": MAX_HOLD,
                   "lookbacks_ledger": list(LOOKBACKS),
                   "n_event_trials": study.get("n_trials"),
                   "study_gate_pass": study.get("gate_pass"),
                   "study_passing_variants": study.get("passing_variants"),
                   "cost_model": "AShareCommission(万2.5+5元下限/印花税分段/"
                                 "过户费) + VolumeImpactSlippage(5bp+5bp√参与率)"
                                 " + 红利税三档 —— 与回测引擎同源"}
    print(json.dumps({k: out[k] for k in ("primary", "gates", "vs_index",
                                          "candidate_distribution") if k in out},
                     ensure_ascii=False, indent=2, default=str))
    tl = out.get("trial_ledger") or {}
    if tl:
        print("\n%-24s %5s %9s %11s %11s %7s %6s %6s"
              % ("组合层变体", "期数", "Shp/期", "超额vs全市场", "超额vs同档",
                 "候选/期", "成本%", "换手%"))
        for k, v in sorted(tl.items(),
                           key=lambda kv: -kv[1]["sharpe_per_grid_period"]):
            print("%-24s %5d %9.4f %10.2f%% %10.2f%% %7s %6.3f %6.0f%%%s"
                  % (k, v["n_periods"], v["sharpe_per_grid_period"],
                     100 * v["ann_excess_vs_ew"],
                     100 * v["ann_excess_vs_own_band_ew"], v["mean_candidates"],
                     v["mean_cost_per_period_pct"],
                     100 * v["annual_turnover_one_way"],
                     "  ★" if k == out.get("primary", {}).get("config") else ""))
    print(">>> %s" % ("四道闸全过" if out.get("gates", {}).get("all_pass")
                      else "有闸不过 —— 见 gates 明细"))
    if args.json:
        p = Path(args.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str),
                     encoding="utf-8")
        print("saved ->", args.json)
    con.close()


if __name__ == "__main__":
    main()
