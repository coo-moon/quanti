"""scripts/baseline_returns.py 加权口径的纯函数检查(不碰 DB)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from baseline_returns import cap_weights, turnover_l1, weighted_return


def test_cap_weights_normalizes_and_drops_missing_mv():
    w = cap_weights(["a", "b", "c"], {"a": 300.0, "b": 100.0})
    assert abs(sum(w.values()) - 1.0) < 1e-12
    assert "c" not in w  # 无市值 → 剔除
    assert abs(w["a"] - 0.75) < 1e-12


def test_turnover_l1_matches_old_membership_formula_for_equal_pools():
    # 等权同规模池:1/4 成员换血 → 单边换手 0.25,与旧 1-重合度 公式等值
    prev = {c: 0.25 for c in "abcd"}
    new = {c: 0.25 for c in "abce"}
    assert abs(turnover_l1(prev, new) - 0.25) < 1e-12
    assert turnover_l1(prev, prev) == 0.0


def test_weighted_return_renormalizes_over_priced_names():
    rets = {"a": 0.10, "b": -0.10}  # "c" 无数据 → 权重重归一
    w = {"a": 0.5, "b": 0.25, "c": 0.25}
    assert abs(weighted_return(rets, w) - (0.10 * 2 / 3 - 0.10 * 1 / 3)) < 1e-12
    assert weighted_return({}, w) == 0.0
