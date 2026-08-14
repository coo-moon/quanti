"""A500 时点成分对账 + QMT 执行器纯逻辑测试。"""
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "strategies/qmt"))

from a500_enhance_qmt import (parse_target_csv, signal_fresh, plan_orders,  # noqa: E402
                              MAX_SINGLE_WEIGHT)


def _load():
    return json.loads((ROOT / "data/a500_membership.json").read_text())


def test_membership_replay_consistency():
    """初始名单 + 事件回放: 每步保持 500 只, 无幽灵调出/重复调入。"""
    spec = _load()
    cur = set(spec["base"])
    assert len(cur) == 500
    for ev in sorted(spec["events"], key=lambda e: e["effective_trade_date"]):
        ins, outs = set(ev["in"]), set(ev["out"])
        assert outs <= cur, f"{ev['effective_trade_date']} 幽灵调出: {outs - cur}"
        assert not (ins & cur), f"{ev['effective_trade_date']} 重复调入: {ins & cur}"
        assert len(ins) == len(outs)
        cur = (cur - outs) | ins
        assert len(cur) == 500


def test_membership_events_sorted_and_dated():
    spec = _load()
    dates = [ev["effective_trade_date"] for ev in spec["events"]]
    assert dates == sorted(dates)
    assert dates[0] >= "2024-12-01"      # 首次定期调样
    for d in dates:
        date.fromisoformat(d)            # 都是合法日期


# ---------------------------------------------------------- QMT 执行器逻辑
CSV_TEXT = (
    "as_of,2026-07-15,execute_from,2026-07-16,top_k,50,band,2.0,n,2\n"
    "code,qmt_code,weight,composite\n"
    "600519,600519.SH,0.60,1.2\n"
    "000001,000001.SZ,0.40,0.9\n")


def test_parse_target_csv_caps_and_renormalizes():
    meta, targets = parse_target_csv(CSV_TEXT)
    w = dict(targets)
    assert meta["execute_from"] == "2026-07-16"
    assert abs(sum(w.values()) - 1.0) < 1e-9
    # 0.6/0.4 都被 clip 到 0.10 → 等权
    assert all(abs(x - 0.5) < 1e-9 for x in w.values())
    assert MAX_SINGLE_WEIGHT == 0.10


def test_signal_freshness_gate():
    meta, _ = parse_target_csv(CSV_TEXT)
    assert signal_fresh(meta, date(2026, 7, 16))[0]
    assert not signal_fresh(meta, date(2026, 7, 15))[0]   # 未到执行日
    assert not signal_fresh(meta, date(2026, 7, 30))[0]   # 过期


def test_plan_orders_diff_gap_fuse_and_liquidation():
    targets = [("A.SH", 0.5), ("B.SZ", 0.5)]
    positions = {"B.SZ": 10000, "C.SH": 30}
    prices = {"A.SH": 10.0, "B.SZ": 20.0, "C.SH": 5.0}
    prev = {"A.SH": 9.5, "B.SZ": 20.0, "C.SH": 5.0}
    orders, skipped, inf_w = plan_orders(targets, positions, prices, prev, 100000.0)
    od = {(c, s): sh for c, s, sh in orders}
    assert od[("A.SH", "buy")] == 5000
    assert od[("B.SZ", "sell")] == 7500
    assert od[("C.SH", "sell")] == 30          # 清仓不受碎单/一手限制
    assert orders[0][1] == "sell"              # 卖先于买
    assert inf_w == 0.0
    # 极端高开熔断: +11% 开盘的票跳过买入
    prices2 = dict(prices)
    prices2["A.SH"] = 10.6
    prev2 = dict(prev)
    prev2["A.SH"] = 9.5
    orders2, skipped2, _ = plan_orders(targets, positions, prices2, prev2, 100000.0)
    assert not any(c == "A.SH" and s == "buy" for c, s, _ in orders2)
    assert any("gap" in r for _, r in skipped2)
    # 停牌(无价)跳过
    prices3 = dict(prices)
    prices3.pop("A.SH")
    orders3, skipped3, _ = plan_orders(targets, positions, prices3, prev, 100000.0)
    assert any("suspended" in r for _, r in skipped3)


def test_plan_orders_lot_feasibility_guard():
    """红队护栏: 一手成本 > 目标市值 → 剔除并记录不可行权重(小资金拒跑走样版)。"""
    targets = [("HI.SH", 0.5), ("LO.SZ", 0.5)]
    prices = {"HI.SH": 1500.0, "LO.SZ": 10.0}   # 一手15万 > 目标10万
    orders, skipped, inf_w = plan_orders(targets, {}, prices, dict(prices), 200000.0)
    assert abs(inf_w - 0.5) < 1e-9
    assert any("infeasible" in r for _, r in skipped)
    assert not any(c == "HI.SH" for c, _, _ in orders)
