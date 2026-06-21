"""Tests for CLI tushare sync flags (cmd_sync)."""
from __future__ import annotations

import types
from datetime import date

import quanti.cli as cli


def test_cmd_sync_tushare_quotes_delisted_only(tmp_path, monkeypatch):
    from quanti.data.database import Database

    dbp = str(tmp_path / "paper.db")
    seed = Database(dbp)
    seed.initialize()
    seed.upsert_stock("000001", "在市", "SZ", date(2010, 1, 1), "")
    seed.upsert_stock("600001", "退市", "SH", date(2000, 1, 1), "",
                      delist_date=date(2019, 1, 1))
    seed.close()

    def _make_db():
        d = Database(dbp)
        d.initialize()
        return d

    monkeypatch.setattr(cli, "_open_db", _make_db)

    synced: list[str] = []

    class FakeTushareAdapter:
        def __init__(self, db):
            self._db = db

        def sync_stock_list(self):
            return 0

        def sync_daily_quotes(self, code, start=None, end=None):
            synced.append(code)
            return 1

    import quanti.data.tushare_adapter as mod
    monkeypatch.setattr(mod, "TushareAdapter", FakeTushareAdapter)

    args = types.SimpleNamespace(
        calendar=False, stocks=False, quotes=False, codes=None,
        tushare_stocks=False, tushare_quotes=True, delisted_only=True,
    )
    cli.cmd_sync(args)
    assert synced == ["600001"]  # only the delisted stock


def test_cmd_sync_tushare_stocks(tmp_path, monkeypatch):
    from quanti.data.database import Database

    dbp = str(tmp_path / "paper.db")
    seed = Database(dbp)
    seed.initialize()
    seed.close()

    monkeypatch.setattr(cli, "_open_db",
                        lambda: _init(Database(dbp)))

    called = {"n": 0}

    class FakeTushareAdapter:
        def __init__(self, db):
            pass

        def sync_stock_list(self):
            called["n"] += 1
            return 5

    import quanti.data.tushare_adapter as mod
    monkeypatch.setattr(mod, "TushareAdapter", FakeTushareAdapter)

    args = types.SimpleNamespace(
        calendar=False, stocks=False, quotes=False, codes=None,
        tushare_stocks=True, tushare_quotes=False, delisted_only=False,
    )
    cli.cmd_sync(args)
    assert called["n"] == 1


def _init(d):
    d.initialize()
    return d
