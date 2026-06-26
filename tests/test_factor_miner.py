# tests/test_factor_miner.py
from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from quanti.agent.factor_miner import (
    _bh_discoveries,
    _ic_pvalue,
    _student_t_sf,
    mine_factors,
    parse_llm_factors,
)
from quanti.factors.evaluation import _nw_tstat
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
    # Predictive momentum + unparseable (dropped) + non-predictive constant.
    llm = _LLM("good_mom: Ref(close,1)/Ref(close,21)-1\n"
               "evil: __import__('os').system('x')\n"
               "flat: close / close\n")
    results = mine_factors(llm, db, provider, codes, date(2025, 5, 20),
                           n_candidates=5, oos_ic_threshold=0.0, min_train_ic=0.0)
    by = {r.name: r for r in results}
    assert "good_mom" in by  # parsed + scored
    assert "evil" not in by  # unparseable → dropped before scoring
    # The gate ACCEPTS the strongly-predictive factor (clears floor + FDR)...
    assert by["good_mom"].accepted is True
    # ...and REJECTS the non-predictive constant (degenerate IC → floor fails).
    assert by["flat"].accepted is False
    # persisted (accepted + rejected rows both kept for audit)
    saved = {r["name"] for r in db.list_generated_factors()}
    assert {"good_mom", "flat"} <= saved


def test_mine_graceful_when_llm_returns_nothing(tmp_path):
    db = Database(str(tmp_path / "m.db"))
    db.initialize()
    provider, codes = _seed_provider()
    results = mine_factors(_LLM(""), db, provider, codes, date(2025, 5, 20))
    assert results == []


# -------------------- multiple-testing gate (pure functions) ---------------

def test_bh_discoveries_family_size_controls_strictness():
    # q=0.10. p=0.001 clears BH at family size 3; p=0.2/0.8 don't.
    assert _bh_discoveries([0.001, 0.2, 0.8], 0.10, 3) == {0}
    # Larger family size raises the bar: a borderline p that passes at m=1...
    assert _bh_discoveries([0.02], 0.10, 1) == {0}      # 0.02 <= 1/1*0.10
    # ...is rejected at family size 10.
    assert _bh_discoveries([0.02], 0.10, 10) == set()   # 0.02 > 1/10*0.10
    # Empty / all-insignificant → nothing.
    assert _bh_discoveries([], 0.10, 5) == set()
    assert _bh_discoveries([0.9, 0.95], 0.10, 2) == set()


def test_student_t_sf_matches_known_quantiles():
    # P(T>0)=0.5 for any df; one-sided 5% crit values; →normal as df→∞.
    assert _student_t_sf(0.0, 10) == 0.5
    assert _student_t_sf(1.812, 10) == pytest.approx(0.05, abs=2e-3)   # t_10 95%
    assert _student_t_sf(1.725, 20) == pytest.approx(0.05, abs=2e-3)   # t_20 95%
    assert _student_t_sf(1.645, 100000) == pytest.approx(0.05, abs=2e-3)  # ~normal
    assert _student_t_sf(-1.812, 10) == pytest.approx(0.95, abs=2e-3)  # symmetry
    # heavier-tailed than normal: same t → larger one-sided p at small df
    assert _student_t_sf(2.0, 10) > 0.5 * math.erfc(2.0 / math.sqrt(2.0))


def test_ic_pvalue_overlap_df_and_nan():
    # n=60 daily ICs, fwd=5 → ~12 independent obs → df≈11.
    assert _ic_pvalue(float("nan"), 60, 5) == 1.0
    assert _ic_pvalue(None, 60, 5) == 1.0
    assert _ic_pvalue(2.0, 1, 5) == 1.0                 # n<2 untestable
    assert _ic_pvalue(0.0, 60, 5) == 0.5
    # higher t → lower one-sided p
    assert _ic_pvalue(3.0, 60, 5) < _ic_pvalue(1.0, 60, 5) < _ic_pvalue(0.0, 60, 5)
    # overlap-reduced df makes it heavier-tailed (less significant) than naive
    # normal-on-full-n would be — i.e. the gate is stricter, not looser.
    assert _ic_pvalue(2.0, 60, 5) > 0.5 * math.erfc(2.0 / math.sqrt(2.0))


def test_nw_tstat_overlap_correction_lowers_t():
    # Positively autocorrelated IC series (what overlapping fwd windows induce):
    # the Newey-West t (lag>0) must be SMALLER than the iid t (lag=0) — i.e. the
    # naive t over-states significance. A monotone ramp is strongly positively
    # autocorrelated at all lags.
    base = [0.03 + 0.003 * i for i in range(20)]
    t_iid = _nw_tstat(base, 0)
    t_hac = _nw_tstat(base, 4)
    assert t_iid > 0 and t_hac > 0
    assert t_hac < t_iid
    # degenerate / too-short series → NaN, never a spurious finite t
    import numpy as np
    assert np.isnan(_nw_tstat([0.05], 4))
    assert np.isnan(_nw_tstat([0.05, 0.05, 0.05], 1))  # zero variance
