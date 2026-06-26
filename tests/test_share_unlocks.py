"""Share-unlock (限售解禁) storage + akshare fetch parsing."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from quanti.data import akshare_adapter as ak_mod
from quanti.data.database import Database


def test_get_upcoming_unlocks_window_max_and_skips(tmp_path):
    db = Database(str(tmp_path / "u.db"))
    db.initialize()
    try:
        as_of = date(2030, 1, 1)
        db.save_share_unlocks([
            {"code": "A", "free_date": date(2030, 1, 11), "float_pct": 0.08},
            {"code": "A", "free_date": date(2030, 1, 6), "float_pct": 0.06},   # same code → MAX
            {"code": "B", "free_date": date(2030, 4, 1), "float_pct": 0.20},   # > 30d → out
            {"code": "C", "free_date": date(2030, 1, 10), "float_pct": None},  # null pct → skip
            {"code": "D", "free_date": date(2030, 1, 1), "float_pct": 0.09},   # == as_of → strictly-after excludes
        ])
        up = db.get_upcoming_unlocks(as_of, 30)
        assert up == {"A": pytest.approx(0.08)}
    finally:
        db.close()


def test_save_share_unlocks_skips_bad_rows(tmp_path):
    db = Database(str(tmp_path / "u.db"))
    db.initialize()
    try:
        n = db.save_share_unlocks([
            {"code": "", "free_date": date(2030, 1, 11), "float_pct": 0.08},   # no code
            {"code": "A", "free_date": None, "float_pct": 0.08},                # no date
            {"code": "A", "free_date": date(2030, 1, 11), "float_pct": 0.08},   # good
        ])
        assert n == 1
    finally:
        db.close()


def test_sync_share_unlocks_parses_percent_to_fraction(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "u.db"))
    db.initialize()
    try:
        fake = pd.DataFrame({
            "股票代码": ["600519", "000001"],
            "解禁时间": ["2030-01-15", "2030-02-25"],
            "占总股本比例": [5.0, 1.0],   # akshare gives PERCENT
        })
        monkeypatch.setattr(ak_mod.ak, "stock_restricted_release_detail_em",
                            lambda **kw: fake, raising=False)
        n = ak_mod.AkShareAdapter(db).sync_share_unlocks(
            date(2030, 1, 1), date(2030, 3, 1))
        assert n == 2
        # 600519 unlocks 01-15, within 30d of 01-10 → 5.0% stored as 0.05.
        up = db.get_upcoming_unlocks(date(2030, 1, 10), 30)
        assert up["600519"] == pytest.approx(0.05)
        assert "000001" not in up   # 02-25 is > 30d from 01-10
    finally:
        db.close()


def test_sync_share_unlocks_survives_string_sentinel_cells(tmp_path, monkeypatch):
    # akshare returns '-' / '--' for missing pct; pd.isna doesn't catch those.
    # One bad cell must NOT abort the batch — it stores NULL (skipped), the
    # good row survives.
    db = Database(str(tmp_path / "u.db"))
    db.initialize()
    try:
        fake = pd.DataFrame({
            "股票代码": ["600519", "000001"],
            "解禁时间": ["2030-01-15", "2030-01-16"],
            "占总股本比例": ["-", 5.0],   # first cell is a string sentinel
        })
        monkeypatch.setattr(ak_mod.ak, "stock_restricted_release_detail_em",
                            lambda **kw: fake, raising=False)
        n = ak_mod.AkShareAdapter(db).sync_share_unlocks(
            date(2030, 1, 1), date(2030, 3, 1))
        assert n == 2   # both rows stored; the bad pct became NULL, not a crash
        up = db.get_upcoming_unlocks(date(2030, 1, 10), 30)
        assert "600519" not in up          # NULL pct → skipped by the guard query
        assert up["000001"] == pytest.approx(0.05)
    finally:
        db.close()


def test_sync_share_unlocks_degrades_on_column_drift(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "u.db"))
    db.initialize()
    try:
        monkeypatch.setattr(ak_mod.ak, "stock_restricted_release_detail_em",
                            lambda **kw: pd.DataFrame({"unexpected": [1]}),
                            raising=False)
        n = ak_mod.AkShareAdapter(db).sync_share_unlocks(
            date(2030, 1, 1), date(2030, 3, 1))
        assert n == 0   # unknown columns → logged + skipped, never crashes
    finally:
        db.close()
