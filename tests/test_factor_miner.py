# tests/test_factor_miner.py
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from quanti.agent.factor_miner import mine_factors, parse_llm_factors
from quanti.data.database import Database


def test_parse_llm_factors_extracts_name_expr_lines():
    text = ("Here are factors:\n"
            "mom_fast: Ref(close, 5) / Ref(close, 20) - 1\n"
            "vol_low: -Std(close, 20)\n"
            "garbage line without colon\n")
    out = parse_llm_factors(text)
    assert ("mom_fast", "Ref(close, 5) / Ref(close, 20) - 1") in out
    assert ("vol_low", "-Std(close, 20)") in out
    assert len(out) == 2


class _LLM:
    def __init__(self, text): self._text = text
    def create_message(self, **kw):
        return {"content": [{"type": "text", "text": self._text}],
                "stop_reason": "end_turn", "usage": {}}


class _Provider:
    def __init__(self, data):
        self._data = data
        n = max(len(v) for v in data.values())
        self._dates = [d.date() for d in pd.bdate_range(end=pd.Timestamp("2025-06-01"), periods=n)]
    def get_daily_df(self, code, start, end):
        c = self._data.get(code, [])
        df = pd.DataFrame({"date": self._dates[:len(c)], "open": c, "high": c,
                           "low": c, "close": c, "volume": [1.0]*len(c),
                           "turnover": [1.0]*len(c)})
        return df[(df["date"] >= start) & (df["date"] <= end)]


def _seed_provider():
    rng = np.random.default_rng(1)
    data = {}
    for i in range(6):
        drift = 0.5 if i < 3 else -0.5
        data[f"c{i}"] = list(100 + np.cumsum(np.full(180, drift) + rng.normal(0, 0.1, 180)))
    return _Provider(data), list(data)


def test_mine_accepts_predictive_and_rejects_unparseable(tmp_path):
    db = Database(str(tmp_path / "m.db"))
    db.initialize()
    provider, codes = _seed_provider()
    # One predictive momentum factor + one unparseable.
    llm = _LLM("good_mom: Ref(close,1)/Ref(close,21)-1\n"
               "evil: __import__('os').system('x')\n")
    results = mine_factors(llm, db, provider, codes, date(2025, 5, 20),
                           n_candidates=5, oos_ic_threshold=0.0, min_train_ic=0.0)
    by = {r.name: r for r in results}
    assert "good_mom" in by  # parsed + scored
    assert "evil" not in by  # unparseable → dropped before scoring
    # persisted
    saved = {r["name"] for r in db.list_generated_factors()}
    assert "good_mom" in saved


def test_mine_graceful_when_llm_returns_nothing(tmp_path):
    db = Database(str(tmp_path / "m.db"))
    db.initialize()
    provider, codes = _seed_provider()
    results = mine_factors(_LLM(""), db, provider, codes, date(2025, 5, 20))
    assert results == []
