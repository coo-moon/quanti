"""Regression: a malformed stocks.list_date must not crash get_stock /
list_stocks. One bad row would otherwise kill the whole agent tick — the
universe builder calls get_stock for every candidate, and an unparseable /
non-str list_date raised `TypeError: fromisoformat: argument must be str`,
taking down trading, regime detection, everything for that cycle.
"""

from __future__ import annotations

from datetime import date

from quanti.data.database import Database


def test_safe_list_date_helper():
    f = Database._safe_list_date
    assert f("2026-06-11") == date(2026, 6, 11)
    assert f(date(2020, 1, 1)) == date(2020, 1, 1)          # already a date
    assert f("2026-06-11 00:00:00") == date(2026, 6, 11)    # tolerates time suffix
    assert f(None) == date(1990, 1, 1)                      # None → fallback
    assert f(20200101) == date(2020, 1, 1)                  # int ISO-basic → parsed
    assert f(3.14) == date(1990, 1, 1)                      # unparseable → fallback
    assert f("garbage") == date(1990, 1, 1)                 # bad str → fallback


def test_bad_list_date_does_not_crash(tmp_path):
    db = Database(str(tmp_path / "ld.db"))
    db.initialize()
    db.upsert_stock("000001", "good", "SZ", date(1991, 4, 3), "银行")
    # Insert a row with a garbage list_date directly (bypasses upsert's str path).
    db.conn.execute(
        "INSERT INTO stocks (code, name, exchange, list_date, industry) "
        "VALUES (?, ?, ?, ?, ?)",
        ("920999", "weird-newboard", "BJ", "not-a-date", "x"))
    db.conn.commit()

    # Neither path may raise; the bad row resolves to the safe fallback.
    bad = db.get_stock("920999")
    assert bad is not None and bad.list_date == date(1990, 1, 1)
    assert db.get_stock("000001").list_date == date(1991, 4, 3)

    codes = {s.code for s in db.list_stocks()}
    assert {"000001", "920999"} <= codes
    db.close()


def test_garbage_quote_date_does_not_crash(tmp_path):
    """get_latest_quote_date must degrade to None on an unparseable stored
    date (same crash class as list_date, seen once in bg-sync). None means
    "no usable data" → the next sync cold-starts and overwrites the row."""
    db = Database(str(tmp_path / "gq.db"))
    db.initialize()
    db.conn.execute(
        "INSERT INTO daily_quotes (code, date, open, high, low, close,"
        " volume, amount, turnover) VALUES (?,?,?,?,?,?,?,?,?)",
        ("600176", "not-a-date", 1, 1, 1, 1, 1, 1, 1))
    db.conn.commit()

    assert db.get_latest_quote_date("600176") is None
    assert db.get_latest_quote_date("no-rows") is None

    # Sanity: a good row still parses, and tolerates a time suffix.
    db.conn.execute(
        "INSERT INTO daily_quotes (code, date, open, high, low, close,"
        " volume, amount, turnover) VALUES (?,?,?,?,?,?,?,?,?)",
        ("000001", "2026-06-10 00:00:00", 1, 1, 1, 1, 1, 1, 1))
    db.conn.commit()
    assert db.get_latest_quote_date("000001") == date(2026, 6, 10)
    db.close()


def test_market_latest_quote_date_ignores_stray_fresh_codes(tmp_path):
    """get_market_latest_quote_date anchors by-date sync freshness: a date
    backed by only 1-2 per-code top-ups must NOT count as the market's
    latest (that made bg-sync idle while 5000+ codes sat a day behind);
    a date with market-scale coverage must."""
    db = Database(str(tmp_path / "ml.db"))
    db.initialize()
    assert db.get_market_latest_quote_date() is None      # empty table

    def put(code: str, d: str) -> None:
        db.conn.execute(
            "INSERT INTO daily_quotes (code, date, open, high, low, close,"
            " volume, amount, turnover) VALUES (?,?,?,?,?,?,?,?,?)",
            (code, d, 1, 1, 1, 1, 1, 1, 1))

    codes = [f"{i:06d}" for i in range(20)]
    for c in codes:
        put(c, "2026-07-01")
    for c in codes[:2]:               # per-code path wrote today's stray bars
        put(c, "2026-07-02")
    db.conn.commit()
    assert db.get_market_latest_quote_date() == date(2026, 7, 1)
    # Dashboard semantics unchanged: global MAX still sees the stray bars.
    assert db.get_global_latest_quote_date() == date(2026, 7, 2)

    # A by-date sync lands (2 halted codes missing) → today now counts.
    for c in codes[2:18]:
        put(c, "2026-07-02")
    db.conn.commit()
    assert db.get_market_latest_quote_date() == date(2026, 7, 2)
    db.close()


def test_market_latest_quote_date_single_code_db(tmp_path):
    """A tiny watchlist DB (one code) has no 'market' to compare against —
    every date is a full day, so the newest bar wins, same as global MAX."""
    db = Database(str(tmp_path / "ml1.db"))
    db.initialize()
    db.conn.execute(
        "INSERT INTO daily_quotes (code, date, open, high, low, close,"
        " volume, amount, turnover) VALUES (?,?,?,?,?,?,?,?,?)",
        ("000001", "2026-07-02", 1, 1, 1, 1, 1, 1, 1))
    db.conn.commit()
    assert db.get_market_latest_quote_date() == date(2026, 7, 2)
    db.close()


def test_global_latest_quote_date(tmp_path):
    """Max bar date across all codes (feeds the dashboard 最近更新 card);
    None on an empty table."""
    db = Database(str(tmp_path / "gl.db"))
    db.initialize()
    assert db.get_global_latest_quote_date() is None

    for code, d in (("000001", "2026-06-10"), ("920985", "2026-06-11")):
        db.conn.execute(
            "INSERT INTO daily_quotes (code, date, open, high, low, close,"
            " volume, amount, turnover) VALUES (?,?,?,?,?,?,?,?,?)",
            (code, d, 1, 1, 1, 1, 1, 1, 1))
    db.conn.commit()
    assert db.get_global_latest_quote_date() == date(2026, 6, 11)
    db.close()


def test_global_earliest_quote_date(tmp_path):
    """Min bar date across all codes (bounds walk-forward's full-history span);
    None on an empty table."""
    db = Database(str(tmp_path / "ge.db"))
    db.initialize()
    assert db.get_global_earliest_quote_date() is None

    for code, d in (("000001", "2016-06-27"), ("920985", "2020-01-02")):
        db.conn.execute(
            "INSERT INTO daily_quotes (code, date, open, high, low, close,"
            " volume, amount, turnover) VALUES (?,?,?,?,?,?,?,?,?)",
            (code, d, 1, 1, 1, 1, 1, 1, 1))
    db.conn.commit()
    assert db.get_global_earliest_quote_date() == date(2016, 6, 27)
    db.close()
