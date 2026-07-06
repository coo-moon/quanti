"""Tests for the tradable-universe filter.

Three layers to verify:
  1. Metadata filter (age, name keywords) — pure stocks-table logic.
  2. Liquidity filter (ADV20, active days) — needs recent bars.
  3. ADV-sorted ordering helper for the no-screener fallback path.

We seed a small but realistic mini-universe so we can assert exact pass/fail
on each filter rule.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from quanti.agent.universe import (
    UniverseBuilder,
    UniverseConfig,
    sort_by_adv20,
)
from quanti.data.database import Database
from quanti.data.provider import DataProvider


def _seed_stock(db: Database, code: str, name: str, industry: str,
                list_date: date, prices: list[float],
                amount_per_bar: float = 5e7):
    """Seed one stock with N bars at `amount_per_bar` notional volume."""
    db.upsert_stock(code, name, "SZ", list_date, industry)
    n = len(prices)
    today = pd.Timestamp.today().normalize()
    dates = pd.bdate_range(end=today, periods=n)
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


@pytest.fixture
def populated(tmp_path):
    """Seven stocks covering the full filter-rule surface."""
    db = Database(str(tmp_path / "uni.db"))
    db.initialize()

    today = date.today()
    long_ago = date(2010, 1, 1)
    last_month = today - pd.Timedelta(days=30).to_pytimedelta()

    # 1) Big-cap, liquid, old → should always pass.
    _seed_stock(db, "000001", "平安银行", "银行", long_ago,
                prices=[10 + i * 0.01 for i in range(80)],
                amount_per_bar=2e8)  # 2亿/天 ADV

    # 2) Mid-cap with just-above-threshold liquidity.
    _seed_stock(db, "000002", "万科A", "地产", long_ago,
                prices=[20 + i * 0.02 for i in range(80)],
                amount_per_bar=6e7)  # 6千万/天 ADV (just above 5000万 default)

    # 3) Low-liquidity penny stock — should be dropped.
    _seed_stock(db, "000003", "某小盘", "化工", long_ago,
                prices=[3 + i * 0.005 for i in range(80)],
                amount_per_bar=1e7)  # 1千万/天 ADV (below threshold)

    # 4) ST stock — name-filter should reject.
    _seed_stock(db, "600001", "*ST华夏", "钢铁", long_ago,
                prices=[5 + i * 0.01 for i in range(80)],
                amount_per_bar=1e8)

    # 5) Recent IPO — age-filter should reject.
    _seed_stock(db, "600002", "新股一号", "科技", last_month,
                prices=[30 + i * 0.05 for i in range(20)],
                amount_per_bar=1e8)

    # 6) Halted-style: few bars in lookback → fails active-days.
    _seed_stock(db, "600003", "停牌股", "通讯", long_ago,
                prices=[12.0] * 10,  # only 10 bars
                amount_per_bar=1e8)

    # 7) Delisting warning '退' in name.
    _seed_stock(db, "600004", "某某退", "其他", long_ago,
                prices=[1.5] * 80, amount_per_bar=2e8)

    yield db
    db.close()


class TestMetadataFilter:
    def test_new_ipo_rejected(self, populated):
        builder = UniverseBuilder(populated, DataProvider(populated))
        codes = builder.build()
        assert "600002" not in codes, "recent IPO should be filtered"

    def test_st_name_rejected(self, populated):
        builder = UniverseBuilder(populated, DataProvider(populated))
        codes = builder.build()
        assert "600001" not in codes, "*ST stock should be filtered by name"

    def test_delisting_name_rejected(self, populated):
        builder = UniverseBuilder(populated, DataProvider(populated))
        codes = builder.build()
        assert "600004" not in codes, "stock with '退' in name should be filtered"

    def test_metadata_only_can_skip_liquidity(self, populated):
        """Set liquidity threshold to zero — only the metadata rules apply."""
        cfg = UniverseConfig(min_adv20_yuan=0, min_active_days_60=0)
        builder = UniverseBuilder(populated, DataProvider(populated), cfg)
        codes = builder.build()
        # Survives: 000001, 000002, 000003 (low ADV is OK with threshold=0),
        # 600003 (few bars OK with active=0). Rejected by metadata: 600001 (ST),
        # 600002 (new), 600004 (退).
        assert set(codes) == {"000001", "000002", "000003", "600003"}


class TestLiquidityFilter:
    def test_low_adv_rejected(self, populated):
        builder = UniverseBuilder(populated, DataProvider(populated))
        codes = builder.build()
        assert "000003" not in codes, "low-ADV stock should be filtered"

    def test_inactive_rejected(self, populated):
        """Stock with only 10 bars in the 60-day window should fail active-days."""
        builder = UniverseBuilder(populated, DataProvider(populated))
        codes = builder.build()
        assert "600003" not in codes

    def test_above_threshold_kept(self, populated):
        """Mid-cap with 6e7 ADV (just above 5e7 default) survives."""
        builder = UniverseBuilder(populated, DataProvider(populated))
        codes = builder.build()
        assert "000002" in codes
        assert "000001" in codes

    def test_filter_result_attribution(self, populated):
        """Returning details should show what got dropped at each stage."""
        builder = UniverseBuilder(populated, DataProvider(populated))
        codes, result = builder.build(return_details=True)
        assert result.initial == 7
        # After metadata: 7 - {ST (1), new (1), 退 (1)} = 4
        assert result.after_metadata == 4
        # After liquidity: drop low-ADV (1) and halted (1) → 2 survive.
        assert result.after_liquidity == 2
        assert result.final == codes
        assert result.skipped_low_adv >= 1
        assert result.skipped_inactive >= 1


class TestCustomConfig:
    def test_loose_age_allows_recent_ipo(self, populated):
        """Loosening age alone isn't enough — recent IPO also has few bars,
        which trips the active-days filter. Relax both to confirm age is
        the gating constraint in the metadata stage."""
        cfg = UniverseConfig(min_age_days=10, min_active_days_60=10)
        builder = UniverseBuilder(populated, DataProvider(populated), cfg)
        codes = builder.build()
        assert "600002" in codes

    def test_custom_exclude_keywords(self, populated):
        cfg = UniverseConfig(exclude_name_keywords=("银行",))
        builder = UniverseBuilder(populated, DataProvider(populated), cfg)
        codes = builder.build()
        assert "000001" not in codes  # 平安"银行" matches

    def test_no_keywords_means_no_name_filter(self, populated):
        cfg = UniverseConfig(exclude_name_keywords=())
        builder = UniverseBuilder(populated, DataProvider(populated), cfg)
        codes = builder.build()
        assert "600001" in codes  # ST stock now passes (will still fail later
                                  # at risk manager if it were used, but the
                                  # universe layer per-config doesn't block it)


class TestSortByADV:
    def test_sort_descending_by_adv(self, populated):
        codes = ["000003", "000002", "000001"]  # low → mid → high ADV
        sorted_codes = sort_by_adv20(DataProvider(populated), codes)
        # Highest ADV first
        assert sorted_codes[0] == "000001"
        assert sorted_codes[-1] == "000003"

    def test_codes_without_data_sink_to_bottom(self, populated):
        codes = ["NOT_IN_DB", "000001"]
        sorted_codes = sort_by_adv20(DataProvider(populated), codes)
        assert sorted_codes[0] == "000001"
        assert sorted_codes[-1] == "NOT_IN_DB"


class TestCandidateRestriction:
    def test_build_can_restrict_to_subset(self, populated):
        """Passing `candidates` should make the builder filter only that subset."""
        builder = UniverseBuilder(populated, DataProvider(populated))
        codes = builder.build(candidates=["000001", "600001"])
        # Only 000001 should survive (600001 is ST, rejected).
        assert codes == ["000001"]


class TestPointInTimeMetadata:
    """Survivorship / look-ahead fix (see UniverseBuilder._filter_metadata).

    On historical replay the stored `stocks.name` is the LATEST name (upsert
    overwrites it every sync), so the ST/退 match is not point-in-time and is
    dropped; delisting is judged by the point-in-time delist_date instead. On
    the live path (as_of >= today) the current name IS point-in-time, so the
    name match stays exactly as before — this class locks both behaviors.
    """

    @pytest.fixture
    def db(self, tmp_path):
        db = Database(str(tmp_path / "pit.db"))
        db.initialize()
        long_ago = date(2010, 1, 1)
        db.upsert_stock("000001", "平安银行", "SZ", long_ago, "银行")  # clean control
        db.upsert_stock("000010", "*ST美丽", "SZ", long_ago, "环保")  # ST now, still listed
        # Clean name today, delisted 2023-06-26 — name never trips the filter,
        # so this isolates the delist_date boundary. Dates safely in the past.
        db.upsert_stock("600421", "华嵘控股", "SH", long_ago, "综合",
                        delist_date=date(2023, 6, 26))
        # "退"-named today, delisted 2024-01-01 — the dominant survivorship case:
        # was a normal tradeable name in 2022, only later delisted.
        db.upsert_stock("600002", "某某退", "SH", long_ago, "其他",
                        delist_date=date(2024, 1, 1))
        yield db
        db.close()

    def _meta(self, db, as_of):
        b = UniverseBuilder(db, DataProvider(db))
        return set(b._filter_metadata(
            ["000001", "000010", "600421", "600002"], as_of))

    def test_delist_boundary(self, db):
        """Kept the day before delisting (still trading), dropped on/after."""
        assert "600421" in self._meta(db, date(2023, 6, 25))
        assert "600421" not in self._meta(db, date(2023, 6, 26))
        assert "600421" not in self._meta(db, date(2023, 12, 31))

    def test_replay_keeps_future_delisted_and_st(self, db):
        """At a past as_of, names that are ST/退 or delisted TODAY but were
        active then must all be kept — no look-ahead removal."""
        survivors = self._meta(db, date(2022, 6, 30))
        assert survivors == {"000001", "000010", "600421", "600002"}

    def test_live_filters_current_name(self, db):
        """Live (as_of=today): current name is point-in-time, so ST/退 are
        dropped exactly as before — locks the live agent against regression."""
        survivors = self._meta(db, date.today())
        assert "000010" not in survivors  # *ST dropped by name (live)
        assert "600002" not in survivors  # 退 dropped by name + delisted
        assert "000001" in survivors      # clean name kept
