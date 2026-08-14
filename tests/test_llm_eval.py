"""Tests for the offline LLM decision-layer evaluation harness."""

from __future__ import annotations

import json
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from quanti.agent import llm_eval
from quanti.data.database import Database


# ---------------------------------------------------------------- fakes
class FakeLLM:
    def __init__(self, text: str):
        self._text = text

    def create_message(self, **kw):
        return {"content": [{"type": "text", "text": self._text}],
                "stop_reason": "end_turn", "usage": {}}


class FakeProvider:
    """Minimal provider: trade dates + daily bars over a synthetic market."""

    def __init__(self, dates, prices):
        self._dates = dates
        self._prices = prices

    def get_trade_dates(self, start, end):
        return [d for d in self._dates if start <= d <= end]

    def get_daily_df(self, code, start, end):
        """Returns bars in [start, end] — mirrors the real DataProvider's
        contract; a missing filter here silently reintroduces lookahead."""
        n = len(self._prices[code])
        ds = self._dates[:n]
        p = self._prices[code]
        df = pd.DataFrame({
            "date": ds, "open": p, "high": [v * 1.01 for v in p],
            "low": [v * 0.99 for v in p], "close": p,
            "volume": [1e6] * n, "amount": [1e8] * n, "turnover": [1.0] * n,
        })
        return df[(df["date"] >= start) & (df["date"] <= end)]

    def get_daily_bars(self, code, start, end):
        from quanti.models import BarData
        df = self.get_daily_df(code, start, end)
        df = df[(df["date"] >= start) & (df["date"] <= end)]
        bars = []
        for _, r in df.iterrows():
            d = r["date"]
            bars.append(BarData(
                code=code,
                date=d.date() if hasattr(d, "date") else d,
                open=float(r["open"]), high=float(r["high"]),
                low=float(r["low"]), close=float(r["close"]),
                volume=float(r["volume"]), amount=float(r["amount"]),
                turnover=float(r["turnover"])))
        return bars


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "e.db"))
    d.initialize()
    yield d
    d.close()


def _market(n_days=40, end=None):
    end = end or date(2026, 7, 31)
    ds = [d.date() for d in pd.bdate_range(end=pd.Timestamp(end), periods=n_days)]
    rng = np.random.default_rng(3)
    prices = {}
    for i, code in enumerate(["000001", "000002", "600519", "000807", "601888"]):
        drift = 0.02 if i % 2 == 0 else -0.02
        prices[code] = list(10.0 + np.cumsum(drift + rng.normal(0, 0.05, n_days)))
    return ds, prices


# ------------------------------------------------------- day selection
def test_build_eval_days_spacing_and_order(db):
    ds, prices = _market(n_days=40)
    provider = FakeProvider(ds, prices)
    days = llm_eval.build_eval_days(provider, end=ds[-1], n_days=4, stride=5)
    assert len(days) == 4
    assert days == sorted(days)
    idx = [ds.index(d) for d in days]
    assert all(b - a == 5 for a, b in zip(idx, idx[1:]))


# ------------------------------------------------------- candidate universe
def test_resolve_candidates_bounded_and_sorted(db, monkeypatch):
    monkeypatch.setattr(
        "quanti.agent.universe.resolve_tradable_universe",
        lambda db, provider, pool=None, params=None, as_of=None:
        ["000002", "000001", "600519", "601888"])
    got = llm_eval.resolve_candidates(db, None, date(2026, 7, 1), max_codes=2)
    assert got == ["000001", "000002"]

    monkeypatch.setattr(
        "quanti.agent.universe.resolve_tradable_universe",
        lambda db, provider, pool=None, params=None, as_of=None: [])
    assert llm_eval.resolve_candidates(db, None, date(2026, 7, 1)) == []


# ------------------------------------------------------- mechanical rank
def test_mechanical_rank_no_lookahead(db):
    """Ranking at D must not change when the FUTURE is sabotaged — if any
    strategy or factor read a post-D bar, the 10000x jump would move scores."""
    ds, prices = _market(n_days=40)
    d0 = ds[20]
    codes = sorted(prices)
    rank_full = llm_eval.mechanical_rank(db, FakeProvider(ds, prices), codes, d0)
    # Sabotage everything after D: every close x10000.
    prices_sabotaged = {
        c: [p if ds[i] <= d0 else p * 10000.0 for i, p in enumerate(v)]
        for c, v in prices.items()
    }
    rank_sabotaged = llm_eval.mechanical_rank(
        db, FakeProvider(ds, prices_sabotaged), codes, d0)
    assert [c for c, _ in rank_full] == [c for c, _ in rank_sabotaged]
    assert len(rank_full) == len(codes)
    assert all(a >= b for (_, a), (_, b) in zip(rank_full, rank_full[1:]))


# ------------------------------------------------------- forward returns
def test_forward_returns_uses_decision_day_close(db):
    """A jump between D and D+5 must not corrupt the base price (close[D])."""
    ds, _ = _market(n_days=30)
    d0 = ds[10]
    p = [10.0] * len(ds)
    p[11:] = [20.0] * (len(p) - 11)
    provider = FakeProvider(ds, {"000001": p})
    fwd = llm_eval.forward_returns(provider, ["000001"], d0, horizons=(5,))
    assert fwd["000001"][5] == pytest.approx(1.0)  # 20/10 - 1

def test_forward_returns_none_when_data_short(db):
    ds, _ = _market(n_days=30)
    d0 = ds[24]  # 6 bars remain: D+5 exists, D+10 does not
    provider = FakeProvider(ds, {"000001": [10.0] * len(ds)})
    fwd = llm_eval.forward_returns(provider, ["000001"], d0, horizons=(5, 10))
    assert fwd["000001"][5] is not None
    assert fwd["000001"][10] is None


# ------------------------------------------------------- LLM response guard
def test_parse_llm_codes_extracts_json_from_prose():
    text = "我的选择如下:\n[\"600519\", \"000001\"]\n理由略。"
    assert llm_eval._parse_llm_codes(text) == ["600519", "000001"]

def test_parse_llm_codes_defensive():
    assert llm_eval._parse_llm_codes("") == []
    assert llm_eval._parse_llm_codes("没有 JSON") == []
    assert llm_eval._parse_llm_codes("{\"not\": \"an array\"}") == []
    assert llm_eval._parse_llm_codes("\"just a string\"") == []

def test_llm_picks_restricts_to_candidates_and_caps_k(db):
    candidates = ["000001", "000002", "600519"]
    llm = FakeLLM(json.dumps(["600519", "999999", "000001", "600519", "300001"]))
    picks, err, raw = llm_eval.llm_picks(llm, db, candidates, date(2026, 7, 1), k=2)
    assert picks == ["600519", "000001"]
    assert err == ""
    assert raw  # response text is recorded for offline inspection

def test_llm_picks_failure_is_explicit(db):
    # Non-JSON twice (initial + corrective retry) → hard error, no picks.
    picks, err, raw = llm_eval.llm_picks(FakeLLM("没有 JSON"), db, ["000001"],
                                         date(2026, 7, 1), k=2)
    assert picks == []
    assert err
    assert raw
    # Retry recovers when the SECOND answer parses.
    class _RetryLLM:
        def __init__(self):
            self.calls = 0
        def create_message(self, **kw):
            self.calls += 1
            text = "\"没有 JSON\"" if self.calls == 1 else "[\"000001\"]"
            return {"content": [{"type": "text", "text": text}],
                    "stop_reason": "end_turn", "usage": {}}
    llm2 = _RetryLLM()
    picks2, err2, _ = llm_eval.llm_picks(llm2, db, ["000001"],
                                         date(2026, 7, 1), k=1)
    assert picks2 == ["000001"]
    assert err2 == ""
    assert llm2.calls == 2
    # Parseable but fewer valid picks than k → degraded day, explicit reason.
    picks3, err3, _ = llm_eval.llm_picks(FakeLLM("[\"000001\"]"), db, ["000001"],
                                         date(2026, 7, 1), k=2)
    assert picks3 == ["000001"]
    assert "不足" in err3


# ------------------------------------------------------- aggregation
def test_build_report_math(db):
    from quanti.agent.llm_eval import EvalDayResult
    d = date(2026, 7, 1)
    r1 = EvalDayResult(day=d, n_candidates=3, baseline=["a", "b"],
                       llm=["a", "b"], llm_error="",
                       forward={5: {"baseline": 0.10, "llm": 0.10,
                                   "candidates_mean": 0.05},
                                10: {"baseline": None, "llm": None,
                                     "candidates_mean": None}})
    r2 = EvalDayResult(day=d + timedelta(days=1), n_candidates=3,
                       baseline=["a", "c"], llm=["c", "b"], llm_error="",
                       forward={5: {"baseline": -0.02, "llm": 0.03,
                                   "candidates_mean": 0.01},
                                10: {"baseline": 0.01, "llm": 0.05,
                                     "candidates_mean": 0.02}})
    report = llm_eval.build_report([r1, r2], horizons=(5, 10), k=2)
    s5 = report["summary"]["5d"]
    assert s5["baseline_mean"] == pytest.approx((0.10 - 0.02) / 2)
    assert s5["llm_mean"] == pytest.approx((0.10 + 0.03) / 2)
    assert s5["candidates_mean"] == pytest.approx((0.05 + 0.01) / 2)
    ag = [row["agreement"] for row in report["days"]]
    assert ag == [1.0, 0.5]  # r1: {a,b}∩{a,b}=2/2; r2: {a,c}∩{c,b}=1/2
    assert report["n_llm_error_days"] == 0
    assert report["summary"]["10d"]["baseline_n"] == 1


# ------------------------------------------------------- end-to-end
def test_evaluate_end_to_end_with_fake_llm(db, monkeypatch):
    ds, prices = _market(n_days=40)
    provider = FakeProvider(ds, prices)
    d0 = ds[20]
    monkeypatch.setattr(llm_eval, "resolve_candidates",
                        lambda db, provider, as_of, **kw: sorted(prices))
    monkeypatch.setattr(llm_eval, "llm_picks",
                        lambda llm, db, candidates, as_of, k=5, **kw:
                        (candidates[:k], "", "[\"x\"]"))
    report = llm_eval.evaluate(db, provider, FakeLLM("[]"),
                               end=d0, n_days=3, stride=5, k=3)
    assert report["n_days"] == 3
    assert report["n_llm_error_days"] == 0
    assert "5d" in report["summary"] and "10d" in report["summary"]
    assert len(report["days"]) == 3
    assert all(len(row["baseline"]) == 3 for row in report["days"])

def test_evaluate_counts_llm_failures(db, monkeypatch):
    ds, prices = _market(n_days=40)
    provider = FakeProvider(ds, prices)
    d0 = ds[20]
    monkeypatch.setattr(llm_eval, "resolve_candidates",
                        lambda db, provider, as_of, **kw: sorted(prices))
    monkeypatch.setattr(llm_eval, "llm_picks",
                        lambda llm, db, candidates, as_of, k=5, **kw:
                        ([], "无法解析 JSON 数组", "raw"))
    report = llm_eval.evaluate(db, provider, FakeLLM("x"),
                               end=d0, n_days=2, stride=5, k=3)
    assert report["n_days"] == 2
    assert report["n_llm_error_days"] == 2
    assert report["summary"]["5d"]["llm_mean"] is None

