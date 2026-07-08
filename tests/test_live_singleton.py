# -*- coding: utf-8 -*-
"""Cross-process live-trading singleton lock — two live agents must never drive
the same account concurrently (duplicate / conflicting real orders)."""

from datetime import datetime, timedelta

import pytest

from quanti.agent.runtime import AgentRuntime
from quanti.data.database import Database
from quanti.data.provider import DataProvider


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    yield d
    d.close()


# ---- DB-level claim / refresh / release / stale ----------------------------

def test_claim_is_exclusive(db):
    assert db.claim_live_singleton("A") is True
    assert db.claim_live_singleton("B") is False        # A holds it (fresh)
    assert db.claim_live_singleton("A") is True         # re-claim by owner is ok


def test_release_frees_it(db):
    db.claim_live_singleton("A")
    db.release_live_singleton("A")
    assert db.claim_live_singleton("B") is True         # free after release


def test_stale_lock_is_reclaimable(db):
    db.claim_live_singleton("A")
    db.conn.execute("UPDATE live_singleton SET heartbeat_at=? WHERE id=1",
                    ((datetime.now() - timedelta(seconds=600)).isoformat(),))
    db.conn.commit()
    # A's heartbeat is 600s old → beyond the 120s window → B may take over.
    assert db.claim_live_singleton("B", stale_seconds=120) is True
    assert db.refresh_live_singleton("A") is False      # A lost it (split-brain)
    assert db.refresh_live_singleton("B") is True


def test_refresh_only_owner(db):
    db.claim_live_singleton("A")
    assert db.refresh_live_singleton("A") is True
    assert db.refresh_live_singleton("B") is False


def test_release_only_owner_noop(db):
    db.claim_live_singleton("A")
    db.release_live_singleton("B")                      # not owner → no-op
    assert db.claim_live_singleton("C") is False         # A still holds it


# ---- runtime: refuse a 2nd live agent, no exclusion for paper --------------

class _LiveBrokerStub:
    _require_live = True


class _PaperBrokerStub:
    _require_live = False


def _rt(db, broker):
    return AgentRuntime(db, DataProvider(db), broker)


def test_second_live_agent_refused(db):
    rt1 = _rt(db, _LiveBrokerStub())
    rt1._acquire_live_singleton()                       # holds it
    assert rt1._singleton_owner is not None
    rt2 = _rt(db, _LiveBrokerStub())
    with pytest.raises(RuntimeError):
        rt2._acquire_live_singleton()                   # refused — first still live
    rt1._release_live_singleton()
    rt2._acquire_live_singleton()                       # free now
    assert rt2._singleton_owner is not None


def test_paper_agent_takes_no_singleton(db):
    rt1 = _rt(db, _PaperBrokerStub())
    rt2 = _rt(db, _PaperBrokerStub())
    rt1._acquire_live_singleton()                       # no-op for paper
    rt2._acquire_live_singleton()                       # no exclusion for paper
    assert rt1._singleton_owner is None
    assert rt2._singleton_owner is None
