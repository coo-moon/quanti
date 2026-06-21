from __future__ import annotations

from quanti.data.database import Database


def _db(tmp_path):
    db = Database(str(tmp_path / "g.db"))
    db.initialize()
    return db


def test_save_list_toggle_and_load_active(tmp_path):
    db = _db(tmp_path)
    db.save_generated_factor("llm_mom", "Ref(close,21)/Ref(close,126)-1",
                             train_ic=0.05, oos_ic=0.04, accepted=True)
    db.save_generated_factor("llm_bad", "-Mean(turnover,20)",
                             train_ic=0.01, oos_ic=0.0, accepted=False)
    rows = {r["name"]: r for r in db.list_generated_factors()}
    assert rows["llm_mom"]["accepted"] is True and rows["llm_mom"]["enabled"] is True
    assert rows["llm_bad"]["accepted"] is False
    # active = accepted & enabled
    active = db.load_active_factor_fns()
    assert set(active) == {"llm_mom"}
    # toggling enabled off removes it from active
    db.set_factor_enabled("llm_mom", False)
    assert db.load_active_factor_fns() == {}


def test_load_active_skips_unparseable(tmp_path):
    db = _db(tmp_path)
    db.save_generated_factor("broken", "os.system('x')", 0.1, 0.1, accepted=True)
    assert db.load_active_factor_fns() == {}  # parse fails → skipped, no crash


def test_save_is_upsert(tmp_path):
    db = _db(tmp_path)
    db.save_generated_factor("f", "close", 0.1, 0.1, accepted=True)
    db.save_generated_factor("f", "-close", 0.2, 0.2, accepted=True)
    rows = db.list_generated_factors()
    assert len(rows) == 1 and rows[0]["expr_str"] == "-close"
