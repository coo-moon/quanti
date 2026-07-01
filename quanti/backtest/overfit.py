"""过拟合严谨度闸 —— Deflated Sharpe Ratio + Probability of Backtest Overfitting。

回测里唯一比"看起来能赚钱"更重要的问题是"这个 edge 是真的还是我试了 100 次挑出来的运气"。
这个模块把 López de Prado 的两把尺子实装成纯函数,任何声称跑赢基准的配置都要过闸:

  - PSR  (Probabilistic Sharpe Ratio, Bailey & LdP 2012):考虑样本长度 + 偏度 + 峰度,
          给出"真实 Sharpe > 基准"的概率。修正了"短样本 + 肥尾"下 Sharpe 被高估。
  - DSR  (Deflated Sharpe Ratio, Bailey & LdP 2014):在 PSR 基础上把基准抬高到"试了 N 次
          能碰到的最大 Sharpe 期望",即扣掉多重检验/选择偏差。DSR<0.95 → 别信这个 edge。
  - PBO  (Probability of Backtest Overfitting, Bailey et al. 2015,CSCV 法):给一个
          (期数 × 配置数) 的收益矩阵,估计"样本内最优配置在样本外落到后一半"的概率。
          PBO≈0.5 → 纯噪声挑优;PBO 越低越可信。

无 scipy 依赖:正态 CDF 用 math.erf,逆 CDF 用 Acklam 有理逼近(误差 ~1e-9)。
自检:python -m quanti.backtest.overfit
"""
from __future__ import annotations

import math
from itertools import combinations

import numpy as np

_GAMMA = 0.5772156649015329  # Euler–Mascheroni
_E = math.e


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Acklam 逆正态 CDF 逼近。p∈(0,1)。"""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    p_low, p_high = 0.02425, 1 - 0.02425
    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= p_high:
        q = p - 0.5
        r = q*q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
            ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


def sharpe_per_obs(returns) -> float:
    """每期(非年化)Sharpe = mean / std(ddof=1)。DSR/PSR 全程用非年化口径。"""
    r = np.asarray(returns, dtype=float)
    r = r[~np.isnan(r)]
    if len(r) < 2:
        return 0.0
    sd = r.std(ddof=1)
    return float(r.mean() / sd) if sd > 0 else 0.0


def probabilistic_sharpe_from_stats(sr_per_obs: float, n_obs: int,
                                    sr_benchmark: float = 0.0,
                                    skew: float = 0.0, kurt: float = 3.0) -> float:
    """PSR 的摘要统计版:只吃标量 → P(真实每期Sharpe > sr_benchmark)。

    给「只有标量Sharpe、无完整收益序列」的调用方(如 selector)用。**口径陷阱**:
    sr_per_obs 必须是**每期(非年化)**Sharpe——若你手上是年化Sharpe(×√252),
    先除回 √(每年周期数)再传进来;n_obs 用真实观测数,否则 PSR 会严重误校准。
    skew/kurt 是收益分布的偏度/(非超额)峰度(正态=3);拿不到时留默认(正态假设)。
    """
    if n_obs < 3:
        return 0.0
    sr = float(sr_per_obs)
    denom = 1.0 - skew*sr + (kurt - 1.0)/4.0 * sr*sr
    if denom <= 0:
        denom = 1e-12
    stat = (sr - sr_benchmark) * math.sqrt(n_obs - 1) / math.sqrt(denom)
    return _norm_cdf(stat)


def probabilistic_sharpe_ratio(returns, sr_benchmark: float = 0.0) -> float:
    """P(真实每期Sharpe > sr_benchmark)。返回 [0,1]。"""
    r = np.asarray(returns, dtype=float)
    r = r[~np.isnan(r)]
    n = len(r)
    if n < 3:
        return 0.0
    sd = r.std(ddof=1)
    if sd <= 0:
        return 0.0
    sr = float(r.mean() / sd)
    z = (r - r.mean()) / sd
    skew = float(np.mean(z**3))
    kurt = float(np.mean(z**4))  # 非超额峰度(正态=3)
    return probabilistic_sharpe_from_stats(sr, n, sr_benchmark=sr_benchmark,
                                           skew=skew, kurt=kurt)


def expected_max_sharpe(trial_sharpes) -> float:
    """试了 N 次、每期Sharpe 方差为 var 时,能碰到的最大Sharpe期望(纯噪声下)。"""
    s = np.asarray(trial_sharpes, dtype=float)
    s = s[~np.isnan(s)]
    n = len(s)
    if n < 2:
        return 0.0
    sd = s.std(ddof=1)
    if sd <= 0:
        return 0.0
    return sd * ((1 - _GAMMA) * _norm_ppf(1 - 1.0/n) +
                 _GAMMA * _norm_ppf(1 - 1.0/(n * _E)))


def deflated_sharpe_ratio(returns, trial_sharpes) -> dict:
    """DSR:把 PSR 的基准抬高到 expected_max_sharpe(扣多重检验)。

    returns: 被检验配置的每期收益序列。
    trial_sharpes: 本次搜索所有配置的每期Sharpe(含自己),用于估选择偏差。
    """
    sr0 = expected_max_sharpe(trial_sharpes)
    dsr = probabilistic_sharpe_ratio(returns, sr_benchmark=sr0)
    return {"dsr": dsr, "sr0_benchmark": sr0,
            "sr_observed": sharpe_per_obs(returns),
            "n_trials": int(len(np.asarray(trial_sharpes)))}


def deflated_sharpe_from_stats(sr_per_obs: float, n_obs: int, trial_sharpes,
                               skew: float = 0.0, kurt: float = 3.0) -> dict:
    """DSR 的摘要统计版:给「只有标量Sharpe」的调用方用。

    sr_per_obs 必须是**每期(非年化)**Sharpe(见 probabilistic_sharpe_from_stats
    的口径陷阱);trial_sharpes 是本次搜索所有配置的**每期**Sharpe(含赢家),
    用来估选择偏差把基准抬高到 expected_max_sharpe。有完整收益序列时优先用
    deflated_sharpe_ratio(会自动算真实 skew/kurt,最准)。
    """
    sr0 = expected_max_sharpe(trial_sharpes)
    dsr = probabilistic_sharpe_from_stats(sr_per_obs, n_obs, sr_benchmark=sr0,
                                          skew=skew, kurt=kurt)
    return {"dsr": dsr, "sr0_benchmark": sr0,
            "sr_observed": float(sr_per_obs),
            "n_trials": int(len(np.asarray(trial_sharpes)))}


def pbo_cscv(perf_matrix, n_splits: int = 16) -> dict:
    """Probability of Backtest Overfitting via CSCV。

    perf_matrix: (T期 × N配置) 的每期收益矩阵。
    n_splits: 把 T 期切成 S 个连续等块(偶数)。C(S,S/2) 种 IS/OOS 组合。
    返回 pbo = P(样本内最优配置的样本外排名落在后一半)。
    """
    M = np.asarray(perf_matrix, dtype=float)
    if M.ndim != 2 or M.shape[1] < 2:
        raise ValueError("perf_matrix 需为 (T, N) 且 N>=2")
    T, N = M.shape
    S = n_splits - (n_splits % 2)
    S = max(2, min(S, T))
    block = T // S
    if block < 1:
        raise ValueError(f"期数 T={T} 不足以切 {S} 块")
    blocks = [M[i*block:(i+1)*block] for i in range(S)]

    def _sharpe(rows):  # 每列的每期Sharpe
        # ddof=1 与 PSR/DSR 口径一致。注:同一 block-set 内所有列行数相同,
        # ddof 只是全列共有的常数缩放 → 不改 argmax/排名 → 对 PBO 数值零影响;
        # 统一口径纯为可读性,避免复核者误判为"漏检过拟合"。
        mu = rows.mean(axis=0)
        sd = rows.std(axis=0, ddof=1)
        return np.where(sd > 0, mu / sd, 0.0)

    logits, overfit = [], 0
    all_idx = set(range(S))
    for is_combo in combinations(range(S), S // 2):
        oos_combo = tuple(sorted(all_idx - set(is_combo)))
        is_rows = np.vstack([blocks[i] for i in is_combo])
        oos_rows = np.vstack([blocks[i] for i in oos_combo])
        is_perf = _sharpe(is_rows)
        oos_perf = _sharpe(oos_rows)
        n_star = int(np.argmax(is_perf))
        # 样本外相对排名 ω∈(0,1):1=最好
        rank = float((oos_perf < oos_perf[n_star]).sum() + 1)  # 1..N
        omega = rank / (N + 1)
        omega = min(max(omega, 1e-6), 1 - 1e-6)
        lam = math.log(omega / (1 - omega))
        logits.append(lam)
        if lam <= 0:
            overfit += 1
    n_combos = len(logits)
    return {"pbo": overfit / n_combos if n_combos else float("nan"),
            "n_combos": n_combos, "n_configs": N, "n_splits": S,
            "median_logit": float(np.median(logits)) if logits else float("nan")}


def _selftest():
    rng = np.random.default_rng(42)

    # 1) PSR:长样本、正漂移 → 真实Sharpe>0 概率应很高
    r = rng.normal(0.001, 0.01, 1500)
    assert probabilistic_sharpe_ratio(r, 0.0) > 0.95, "正漂移长样本 PSR 应 >0.95"

    # 2) PSR 恒等式:基准取样本自身Sharpe → stat=0 → PSR≡0.5(确定性,不靠抽样)
    r0 = rng.normal(0.0, 0.01, 1500)
    psr_self = probabilistic_sharpe_ratio(r0, sharpe_per_obs(r0))
    assert abs(psr_self - 0.5) < 1e-6, f"PSR(基准=自身Sharpe) 应=0.5,得 {psr_self}"

    # 3) DSR:100 条纯噪声里挑Sharpe最高的一条 → DSR 应被压低(选择偏差)
    trials = [rng.normal(0.0, 0.01, 300) for _ in range(100)]
    sharpes = [sharpe_per_obs(t) for t in trials]
    best = trials[int(np.argmax(sharpes))]
    dsr_noise = deflated_sharpe_ratio(best, sharpes)["dsr"]
    assert dsr_noise < 0.9, f"噪声挑优 DSR 不该高,得 {dsr_noise:.3f}"

    # 3b) from-stats 版必须与 series 版数值一致(同 skew/kurt/n)
    z_best = (best - best.mean()) / best.std(ddof=1)
    ps_stats = probabilistic_sharpe_from_stats(
        sharpe_per_obs(best), len(best), 0.0,
        float(np.mean(z_best**3)), float(np.mean(z_best**4)))
    assert abs(ps_stats - probabilistic_sharpe_ratio(best, 0.0)) < 1e-9, \
        "from-stats PSR 应与 series 版逐位一致"
    dsr_stats = deflated_sharpe_from_stats(
        sharpe_per_obs(best), len(best), sharpes,
        float(np.mean(z_best**3)), float(np.mean(z_best**4)))["dsr"]
    assert abs(dsr_stats - dsr_noise) < 1e-9, "from-stats DSR 应与 series 版一致"

    # 4) PBO:纯噪声矩阵 → PBO≈0.5
    Mn = rng.normal(0.0, 0.01, (160, 40))
    pbo_noise = pbo_cscv(Mn, n_splits=16)["pbo"]
    assert 0.3 < pbo_noise < 0.7, f"噪声矩阵 PBO 应≈0.5,得 {pbo_noise:.3f}"

    # 5) PBO:只有 1 列有真漂移 → 它 IS/OOS 都最好 → PBO 低
    Me = rng.normal(0.0, 0.01, (160, 40))
    Me[:, 7] += 0.004  # 真 edge
    pbo_edge = pbo_cscv(Me, n_splits=16)["pbo"]
    assert pbo_edge < 0.2, f"真 edge PBO 应低,得 {pbo_edge:.3f}"

    print("overfit.py 自检通过:")
    print(f"  PSR(正漂移)={probabilistic_sharpe_ratio(r,0):.3f}  PSR(基准=自身)={psr_self:.3f}")
    print(f"  DSR(噪声挑优)={dsr_noise:.3f}")
    print(f"  PBO(噪声)={pbo_noise:.3f}  PBO(真edge)={pbo_edge:.3f}")


if __name__ == "__main__":
    _selftest()
