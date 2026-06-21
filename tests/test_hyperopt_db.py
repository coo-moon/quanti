from __future__ import annotations

from quanti.data.database import Database


def test_save_get_list_optimization(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.save_optimization("ma_cross", {"short_period": 8, "long_period": 30},
                         oos_sharpe=1.5, baseline_oos_sharpe=0.4, accepted=True,
                         n_combos=12, universe_size=100)
    db.save_optimization("rsi_ob_os", {"period": 7}, oos_sharpe=0.2,
                         baseline_oos_sharpe=0.3, accepted=False, n_combos=8,
                         universe_size=100)
    # Accepted → params returned; rejected → None.
    assert db.get_active_params("ma_cross") == {"short_period": 8, "long_period": 30}
    assert db.get_active_params("rsi_ob_os") is None
    assert db.get_active_params("never_tuned") is None
    rows = {r["strategy_name"]: r for r in db.list_optimization_results()}
    assert rows["ma_cross"]["accepted"] is True
    assert rows["rsi_ob_os"]["accepted"] is False
    assert rows["ma_cross"]["oos_sharpe"] == 1.5


def test_save_optimization_upserts(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.initialize()
    db.save_optimization("ma_cross", {"short_period": 5}, 1.0, 0.5, True, 12, 100)
    db.save_optimization("ma_cross", {"short_period": 10}, 2.0, 0.5, True, 12, 100)
    assert db.get_active_params("ma_cross") == {"short_period": 10}
    assert len(db.list_optimization_results()) == 1
