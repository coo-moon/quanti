"""Tests for the shared tradable-universe resolver used by optimize / mining.

The old optimize/mine path did `list_stocks()[:N]` — a *dictionary-order*
slice (always 000001, 000002, …) that ignored liquidity and tradeability,
so hyperopt/factor-IC was measured on a non-representative, low-quality
sample. `resolve_tradable_universe` replaces it with the same selection the
live agent uses: pool-trust → optional UniverseBuilder filter → rank by ADV →
cap. These tests pin that behavior.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from quanti.agent.universe import (
    UniverseConfig,
    resolve_tradable_universe,
    universe_config_from_params,
)
from quanti.data.database import Database
from quanti.data.provider import DataProvider


def _seed_stock(db, code, name, industry, list_date, prices,
                amount_per_bar=5e7, end=None):
    """Seed one stock with N daily bars at `amount_per_bar` notional turnover.

    Bars end at `end` (default today) so the liquidity window can see them.
    """
    db.upsert_stock(code, name, "SZ", list_date, industry)
    n = len(prices)
    end_ts = pd.Timestamp(end) if end is not None else pd.Timestamp.today().normalize()
    dates = pd.bdate_range(end=end_ts, periods=n)
    df = pd.DataFrame({
        "code": code,
        "date": [d.date() for d in dates],
        "open": prices, "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices], "close": prices,
        "volume": [amount_per_bar / p for p in prices],
        "amount": [amount_per_bar] * n,
        "turnover": [1.0] * n,
    })
    db.save_daily_quotes(df)


def _many_stocks_db(tmp_path, n=25):
    """n liquid old stocks whose ADV strictly INCREASES with code index.

    So dictionary order (ascending code) == ascending ADV: the dict-order
    slice keeps the *least* liquid names, the ADV ranking keeps the most.
    Lets a single fixture prove "no longer dictionary-biased".
    """
    db = Database(str(tmp_path / "many.db"))
    db.initialize()
    long_ago = date(2010, 1, 1)
    for i in range(n):
        code = f"{i:06d}"
        _seed_stock(db, code, f"股票{i}", "行业", long_ago,
                    prices=[10 + j * 0.01 for j in range(80)],
                    amount_per_bar=1e8 + i * 1e7)  # ADV rises with i
    return db


class TestAdvRankingAndCap:
    def test_picks_most_liquid_not_dictionary_first(self, tmp_path):
        """With 25 candidates and cap 20, the 5 dropped must be the lowest-ADV
        (= lowest codes), NOT the highest codes the old slice would have cut."""
        db = _many_stocks_db(tmp_path, n=25)
        try:
            params = {"selector_max_universe": 20}
            codes = resolve_tradable_universe(
                db, DataProvider(db), pool=None, params=params, as_of=date.today())
            assert len(codes) == 20
            # Lowest-ADV (lowest codes) dropped; highest-ADV (highest code) kept.
            assert "000000" not in codes
            assert "000004" not in codes
            assert "000024" in codes
            # Highest ADV ranks first.
            assert codes[0] == "000024"
        finally:
            db.close()

    def test_cap_floor_is_20(self, tmp_path):
        """selector_max_universe below 20 is floored to 20 (never optimize on
        a handful of names) — preserves the old `max(20, N)` guard."""
        db = _many_stocks_db(tmp_path, n=25)
        try:
            codes = resolve_tradable_universe(
                db, DataProvider(db), pool=None,
                params={"selector_max_universe": 1}, as_of=date.today())
            assert len(codes) == 20
        finally:
            db.close()


def _mixed_db(tmp_path):
    """Small universe spanning every filter rule (mirrors test_universe.py)."""
    db = Database(str(tmp_path / "mixed.db"))
    db.initialize()
    long_ago = date(2010, 1, 1)
    last_month = date.today() - timedelta(days=30)
    _seed_stock(db, "000001", "平安银行", "银行", long_ago,
                [10 + i * 0.01 for i in range(80)], amount_per_bar=2e8)
    _seed_stock(db, "000003", "某小盘", "化工", long_ago,
                [3 + i * 0.005 for i in range(80)], amount_per_bar=1e7)  # illiquid
    _seed_stock(db, "600001", "*ST华夏", "钢铁", long_ago,
                [5 + i * 0.01 for i in range(80)], amount_per_bar=1e8)  # ST
    _seed_stock(db, "600002", "新股一号", "科技", last_month,
                [30 + i * 0.05 for i in range(20)], amount_per_bar=1e8)  # new IPO
    return db


class TestLiquidityFilterGate:
    def test_filter_drops_st_ipo_illiquid_when_enabled(self, tmp_path):
        db = _mixed_db(tmp_path)
        try:
            codes = resolve_tradable_universe(
                db, DataProvider(db), pool=None,
                params={"liquidity_filter": True}, as_of=date.today())
            assert "600001" not in codes  # ST
            assert "600002" not in codes  # new IPO
            assert "000003" not in codes  # illiquid
            assert "000001" in codes      # liquid blue-chip survives
        finally:
            db.close()

    def test_filter_off_by_default_keeps_all_adv_ranked(self, tmp_path):
        """Default params (no liquidity_filter): no ST/IPO filtering — just
        ADV rank + cap, mirroring the live agent's default."""
        db = _mixed_db(tmp_path)
        try:
            codes = resolve_tradable_universe(
                db, DataProvider(db), pool=None, params={}, as_of=date.today())
            assert "600001" in codes  # ST not dropped when filter is off
            assert set(codes) == {"000001", "000003", "600001", "600002"}
        finally:
            db.close()


class TestPoolPath:
    def test_pool_is_trusted_not_metadata_filtered(self, tmp_path):
        """A user-curated pool is used as-is — even an ST name stays — but is
        still ADV-ranked and capped (matches the live agent's pool-trust)."""
        db = _mixed_db(tmp_path)
        try:
            db.create_pool("mine")
            db.add_stocks_to_pool("mine", ["600001", "000001"])  # ST + blue-chip
            codes = resolve_tradable_universe(
                db, DataProvider(db), pool="mine",
                params={"liquidity_filter": True}, as_of=date.today())
            assert "600001" in codes  # ST trusted because it's in the pool
            assert "000001" in codes
            assert codes[0] == "000001"  # higher ADV ranks first
        finally:
            db.close()


class TestAsOfForwarded:
    def test_as_of_passed_to_adv_sort(self, tmp_path, monkeypatch):
        """`as_of` must reach sort_by_adv20 so liquidity is point-in-time, not
        today's — important when optimizing over a historical `end`."""
        import quanti.agent.universe as uni

        seen = {}
        real = uni.sort_by_adv20

        def spy(provider, codes, as_of=None, lookback_days=90):
            seen["as_of"] = as_of
            return real(provider, codes, as_of=as_of, lookback_days=lookback_days)

        monkeypatch.setattr(uni, "sort_by_adv20", spy)
        db = _many_stocks_db(tmp_path, n=22)
        try:
            target = date(2020, 5, 4)
            resolve_tradable_universe(
                db, DataProvider(db), pool=None, params={}, as_of=target)
            assert seen["as_of"] == target
        finally:
            db.close()


class TestAdv20MapBatched:
    """sort_by_adv20 must rank a whole universe in ONE query, not N per-code
    reads (5000 codes was ~22s). get_adv20_map is that batched read."""

    def _seed_amounts(self, db, code, amounts, end=None):
        db.upsert_stock(code, f"n{code}", "SZ", date(2010, 1, 1), "x")
        n = len(amounts)
        end_ts = pd.Timestamp(end) if end else pd.Timestamp.today().normalize()
        dates = pd.bdate_range(end=end_ts, periods=n)
        db.save_daily_quotes(pd.DataFrame({
            "code": code, "date": [d.date() for d in dates],
            "open": [10.0] * n, "high": [10.0] * n, "low": [10.0] * n,
            "close": [10.0] * n, "volume": [1.0] * n,
            "amount": amounts, "turnover": [1.0] * n,
        }))

    def test_means_only_most_recent_window(self, tmp_path):
        """25 bars: 5 huge then 20 tiny. ADV20 must reflect only the last 20."""
        db = Database(str(tmp_path / "adv.db"))
        db.initialize()
        try:
            self._seed_amounts(db, "000001", [9e9] * 5 + [1e6] * 20)
            m = db.get_adv20_map(date(2009, 1, 1), date.today())
            assert m["000001"] == __import__("pytest").approx(1e6)
        finally:
            db.close()

    def test_missing_code_absent(self, tmp_path):
        db = Database(str(tmp_path / "adv.db"))
        db.initialize()
        try:
            self._seed_amounts(db, "000001", [5e7] * 30)
            m = db.get_adv20_map(date(2009, 1, 1), date.today())
            assert "000001" in m
            assert "999999" not in m  # no bars → not in the map
        finally:
            db.close()


class TestConfigFromParams:
    def test_defaults_when_params_empty(self):
        cfg = universe_config_from_params({})
        assert cfg.min_adv20_yuan == UniverseConfig.min_adv20_yuan
        assert cfg.min_active_days_60 == UniverseConfig.min_active_days_60
        assert cfg.min_age_days == UniverseConfig.min_age_days

    def test_reads_overrides(self):
        cfg = universe_config_from_params({
            "universe_min_adv20": 1.23e8,
            "universe_min_active_days": 30,
            "universe_min_age_days": 200,
        })
        assert cfg.min_adv20_yuan == 1.23e8
        assert cfg.min_active_days_60 == 30
        assert cfg.min_age_days == 200
