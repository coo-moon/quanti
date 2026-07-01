"""Concentration-trim (削峰) #1: partial-sell primitive + one-sided trim band."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from quanti.data.database import Database
from quanti.data.provider import DataProvider
from quanti.execution.paper_broker import PaperBroker
from quanti.models import Direction, Portfolio, Position, Signal
from quanti.risk.manager import RiskConfig, RiskManager, risk_config_from_dict
from quanti.utils.market import lot_round_strength


def test_lot_round_strength():
    assert lot_round_strength(500, 1.0) == 500     # full exit unchanged
    assert lot_round_strength(500, 1.5) == 500     # >=1 → unchanged
    assert lot_round_strength(500, 0.4) == 200     # 200, whole lots
    assert lot_round_strength(300, 0.5) == 100     # 150 → floor to 100
    assert lot_round_strength(150, 0.5) == 0       # 75 → sub-lot → 0 (no-op)
    assert lot_round_strength(500, 0.0) == 0
    assert lot_round_strength(500, -1.0) == 0
    assert lot_round_strength(10000, 0.57) == 5700  # round() before floor (not 5600)


def _overweight_portfolio() -> Portfolio:
    # BIG: 13000 / 101000 = 12.87% (> 12.5% trigger). SMALL: 0.99% (< trigger).
    return Portfolio(cash=87_000.0, positions={
        "BIG": Position("BIG", 1000, 10.0, 13.0),
        "SMALL": Position("SMALL", 100, 10.0, 10.0),
    })


def test_check_drift_trims_trims_only_overweight():
    cfg = RiskConfig(drift_trim_enabled=True)  # to_pct 0.10, band 0.25 → trigger 0.125
    trims = RiskManager(cfg).check_drift_trims(_overweight_portfolio())
    assert len(trims) == 1
    t = trims[0]
    assert t.stock_code == "BIG" and t.direction == Direction.SELL
    # shave back to the 10% edge: (0.1287-0.10)/0.1287 ≈ 0.223
    assert t.strength == pytest.approx((13000 / 101000 - 0.10) / (13000 / 101000),
                                       abs=1e-6)
    assert 0.15 < t.strength < 0.30


def test_check_drift_trims_one_sided_never_tops_up():
    # SMALL is far UNDER target — must never produce a buy or any signal.
    trims = RiskManager(RiskConfig(drift_trim_enabled=True)).check_drift_trims(
        _overweight_portfolio())
    assert all(s.direction == Direction.SELL for s in trims)
    assert all(s.stock_code != "SMALL" for s in trims)


def test_check_drift_trims_off_by_default_and_exclude():
    pf = _overweight_portfolio()
    assert RiskManager(RiskConfig()).check_drift_trims(pf) == []          # default OFF
    cfg = RiskConfig(drift_trim_enabled=True)
    assert RiskManager(cfg).check_drift_trims(pf, exclude={"BIG"}) == []  # already exiting


def test_check_drift_trims_wide_band_means_no_trim_just_above_target():
    # 11% weight: above the 10% target but BELOW the 12.5% band → no churn.
    pf = Portfolio(cash=89_000.0, positions={
        "A": Position("A", 1000, 10.0, 11.0)})   # 11000/100000 = 11%
    assert RiskManager(RiskConfig(drift_trim_enabled=True)).check_drift_trims(pf) == []


def test_risk_config_roundtrip_drift_fields(tmp_path):
    db = Database(str(tmp_path / "rc.db"))
    db.initialize()
    try:
        db.upsert_risk_config({
            "stop_loss_pct": -0.15, "portfolio_stop_loss_pct": -0.30,
            "take_profit_activate_pct": 0.15, "take_profit_trail_pct": 0.10,
            "strategy_exit_enabled": True, "atr_stop_k": 2.0, "atr_stop_n": 14,
            "drift_trim_enabled": True, "drift_trim_to_pct": 0.12,
            "drift_trim_band": 0.30,
        })
        got = db.get_risk_config()
        assert got["drift_trim_enabled"] is True
        assert got["drift_trim_to_pct"] == pytest.approx(0.12)
        assert got["drift_trim_band"] == pytest.approx(0.30)
        cfg = risk_config_from_dict(got)
        assert cfg.drift_trim_enabled is True and cfg.drift_trim_to_pct == pytest.approx(0.12)
    finally:
        db.close()


def test_risk_config_roundtrip_concentration_caps(tmp_path):
    db = Database(str(tmp_path / "rc_caps.db"))
    db.initialize()
    try:
        db.upsert_risk_config({
            "stop_loss_pct": -0.15, "portfolio_stop_loss_pct": -0.30,
            "take_profit_activate_pct": 0.15, "take_profit_trail_pct": 0.10,
            "strategy_exit_enabled": True, "atr_stop_k": 2.0, "atr_stop_n": 14,
            "max_position_pct": 0.25, "max_industry_pct": 0.40,
        })
        got = db.get_risk_config()
        assert got["max_position_pct"] == pytest.approx(0.25)
        assert got["max_industry_pct"] == pytest.approx(0.40)
        cfg = risk_config_from_dict(got)
        assert cfg.max_position_pct == pytest.approx(0.25)
        assert cfg.max_industry_pct == pytest.approx(0.40)
    finally:
        db.close()


def _broker_with_position(tmp_path, qty: int):
    db = Database(str(tmp_path / "pb.db"))
    db.initialize()
    db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=15)
    prices = np.full(len(dates), 10.0)
    db.save_daily_quotes(pd.DataFrame({
        "code": "000001", "date": [d.date() for d in dates],
        "open": prices, "high": prices, "low": prices, "close": prices,
        "volume": np.full(len(dates), 5_000_000.0),
        "amount": prices * 5_000_000, "turnover": np.full(len(dates), 1.0),
    }))
    broker = PaperBroker(db, DataProvider(db), initial_cash=200_000,
                         fill_mode="immediate")
    # Bought long ago → not T+1-frozen, fully sellable.
    db.upsert_position("000001", qty, 10.0, 10.0, date(2020, 1, 1))
    return db, broker


def test_partial_sell_honors_strength(tmp_path):
    db, broker = _broker_with_position(tmp_path, 1000)
    try:
        # strength 0.4 → sell 400, leaving 600 (the partial-sell primitive).
        broker.execute_signal(
            Signal("000001", Direction.SELL, 0.4, "削峰"), "drift_trim")
        pos = {p["code"]: p for p in db.list_positions()}
        assert pos["000001"]["quantity"] == 600
    finally:
        db.close()


def test_full_sell_still_clears_position(tmp_path):
    db, broker = _broker_with_position(tmp_path, 1000)
    try:
        # strength 1.0 (the default for stop-loss/TP/flatten) → full exit.
        broker.execute_signal(
            Signal("000001", Direction.SELL, 1.0, "止损"), "risk_exit")
        assert all(p["code"] != "000001" for p in db.list_positions())
    finally:
        db.close()


def test_strategy_sell_with_low_strength_fully_exits(tmp_path):
    # REGRESSION GUARD: only drift_trim partial-sells. A strategy closing SELL
    # with strength<1.0 (connors_rsi2/cci_reversion/supertrend/kdj_cross all do)
    # must FULLY exit — not leave a stub.
    db, broker = _broker_with_position(tmp_path, 1000)
    try:
        broker.execute_signal(
            Signal("000001", Direction.SELL, 0.7, "策略离场"), "rsi_strategy")
        assert all(p["code"] != "000001" for p in db.list_positions())
    finally:
        db.close()


def test_trim_leaves_settled_remainder_sellable(tmp_path):
    # After a partial 削峰 trim of a SETTLED position, the unsold remainder must
    # NOT be re-frozen (else a same-session stop would be blocked by T+1).
    db, broker = _broker_with_position(tmp_path, 1000)
    try:
        broker.execute_signal(
            Signal("000001", Direction.SELL, 0.4, "削峰"), "drift_trim")
        pos = {p["code"]: p for p in db.list_positions()}["000001"]
        assert pos["quantity"] == 600
        assert (pos.get("frozen_qty") or 0) == 0   # settled remainder stays sellable
    finally:
        db.close()
