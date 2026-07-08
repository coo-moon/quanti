# -*- coding: utf-8 -*-
"""Tests for make_broker's live-startup guards (factory.py)."""

from datetime import timedelta

import pytest

import quanti.execution.factory as factory
from quanti.data.database import Database


def test_cn_tz_assert_passes_on_beijing(monkeypatch):
    monkeypatch.delenv("QUANTI_ALLOW_NON_CN_TZ", raising=False)
    monkeypatch.setattr(factory, "_local_utc_offset", lambda: timedelta(hours=8))
    factory._assert_cn_timezone()  # no raise


def test_cn_tz_assert_raises_on_utc(monkeypatch):
    monkeypatch.delenv("QUANTI_ALLOW_NON_CN_TZ", raising=False)
    monkeypatch.setattr(factory, "_local_utc_offset", lambda: timedelta(0))
    with pytest.raises(RuntimeError):
        factory._assert_cn_timezone()


def test_cn_tz_assert_bypass_env(monkeypatch):
    monkeypatch.setenv("QUANTI_ALLOW_NON_CN_TZ", "1")
    monkeypatch.setattr(factory, "_local_utc_offset", lambda: timedelta(0))
    factory._assert_cn_timezone()  # bypassed → no raise


def test_make_broker_live_blocks_wrong_tz(monkeypatch, tmp_path):
    """make_broker(live) must refuse to build a real broker on a non-Beijing
    host (the tz assertion fires before QmtBroker is constructed)."""
    monkeypatch.setenv("QUANTI_LIVE_ACK", "I_KNOW_REAL_MONEY")
    monkeypatch.delenv("QUANTI_ALLOW_NON_CN_TZ", raising=False)
    monkeypatch.setattr(factory, "_local_utc_offset", lambda: timedelta(0))
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    try:
        with pytest.raises(RuntimeError):
            factory.make_broker(db, None, account="live")
    finally:
        db.close()


def test_make_broker_live_needs_ack(monkeypatch, tmp_path):
    """No/absent ack → refuse (unchanged gate), checked before the tz assert."""
    monkeypatch.delenv("QUANTI_LIVE_ACK", raising=False)
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    try:
        with pytest.raises(RuntimeError):
            factory.make_broker(db, None, account="live")
    finally:
        db.close()
