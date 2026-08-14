"""DSR/PBO 数学的 pytest 化——此前只有模块内 _selftest,不进 CI。

用例即 _selftest 的断言拆分(种子固定,确定性),外加 expected_max_sharpe
的边界行为(它是因子挖掘 DSR haircut 的基准来源)。
"""

from __future__ import annotations

import numpy as np
import pytest

from quanti.backtest.overfit import (
    deflated_sharpe_from_stats,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    pbo_cscv,
    probabilistic_sharpe_from_stats,
    probabilistic_sharpe_ratio,
    sharpe_per_obs,
)


@pytest.fixture
def rng():
    return np.random.default_rng(42)


def test_psr_positive_drift_long_sample(rng):
    r = rng.normal(0.001, 0.01, 1500)
    assert probabilistic_sharpe_ratio(r, 0.0) > 0.95


def test_psr_identity_benchmark_equals_self(rng):
    r0 = rng.normal(0.0, 0.01, 1500)
    assert probabilistic_sharpe_ratio(r0, sharpe_per_obs(r0)) == pytest.approx(0.5, abs=1e-6)


def test_dsr_suppresses_best_of_noise(rng):
    trials = [rng.normal(0.0, 0.01, 300) for _ in range(100)]
    sharpes = [sharpe_per_obs(t) for t in trials]
    best = trials[int(np.argmax(sharpes))]
    assert deflated_sharpe_ratio(best, sharpes)["dsr"] < 0.9


def test_from_stats_matches_series_version(rng):
    trials = [rng.normal(0.0, 0.01, 300) for _ in range(100)]
    sharpes = [sharpe_per_obs(t) for t in trials]
    best = trials[int(np.argmax(sharpes))]
    z = (best - best.mean()) / best.std(ddof=1)
    skew, kurt = float(np.mean(z**3)), float(np.mean(z**4))
    ps = probabilistic_sharpe_from_stats(sharpe_per_obs(best), len(best), 0.0, skew, kurt)
    assert ps == pytest.approx(probabilistic_sharpe_ratio(best, 0.0), abs=1e-9)
    d = deflated_sharpe_from_stats(sharpe_per_obs(best), len(best), sharpes, skew, kurt)
    assert d["dsr"] == pytest.approx(
        deflated_sharpe_ratio(best, sharpes)["dsr"], abs=1e-9)


def test_pbo_noise_near_half(rng):
    M = rng.normal(0.0, 0.01, (160, 40))
    assert 0.3 < pbo_cscv(M, n_splits=16)["pbo"] < 0.7


def test_pbo_true_edge_low(rng):
    M = rng.normal(0.0, 0.01, (160, 40))
    M[:, 7] += 0.004
    assert pbo_cscv(M, n_splits=16)["pbo"] < 0.2


def test_expected_max_sharpe_boundaries():
    # <2 条试验 / 零方差 → 0(DSR 退化为 PSR,首批宽松的来源)
    assert expected_max_sharpe([]) == 0.0
    assert expected_max_sharpe([0.5]) == 0.0
    assert expected_max_sharpe([0.3, 0.3, 0.3]) == 0.0
    # 试验越多、方差越大 → 噪声最大值期望越高
    rng = np.random.default_rng(7)
    few = expected_max_sharpe(rng.normal(0, 0.1, 5))
    many = expected_max_sharpe(rng.normal(0, 0.1, 500))
    assert many > few > 0.0
