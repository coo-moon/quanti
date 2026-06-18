"""Tests for the news/sentiment overlay.

Covers three layers, all without network or a real LLM:
  * `fuse_buy_signals` 3-way blend — backward-compatible when off, tilts
    ranking when on.
  * `sentiment.score_candidates` — batched scoring via a StubLLMClient,
    per-(code, date) caching, and graceful degradation (no news / no LLM /
    fetcher error all resolve to neutral 0.0).
  * Database `news_sentiment` upsert/get roundtrip.
"""

from __future__ import annotations

from datetime import date

import pytest

from quanti.agent.sentiment import SentimentConfig, score_candidates
from quanti.agent.signal_pipeline import fuse_buy_signals
from quanti.data.database import Database
from quanti.models import Direction, Signal


# ----- StubLLMClient (mirrors tests/test_llm_runtime.py) ----------------

class StubLLMClient:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create_message(self, **kw) -> dict:
        self.calls.append(kw)
        if not self._responses:
            raise AssertionError("StubLLMClient ran out of scripted responses")
        return self._responses.pop(0)


def _sentiment_block(scores: list[dict]) -> dict:
    return {
        "stop_reason": "tool_use",
        "content": [{
            "type": "tool_use", "id": "s1",
            "name": "submit_sentiment", "input": {"scores": scores},
        }],
        "usage": {"input_tokens": 20, "output_tokens": 10},
    }


def _buy(code: str, strength: float = 0.7) -> Signal:
    return Signal(stock_code=code, direction=Direction.BUY,
                  strength=strength, reason="t")


def _fake_news(newsy: set[str]):
    def fetch(code: str, **kw) -> list[dict]:
        return [{"title": f"{code} 业绩预增"}] if code in newsy else []
    return fetch


# ----- fuse_buy_signals blend -------------------------------------------

class TestBlend:
    def test_sentiment_off_is_backward_compatible(self):
        """sentiment_blend=0 (default) must reproduce the prior 2-way blend
        exactly, even if sentiment_scores are passed."""
        per = {"s1": [_buy("000001", 0.8), _buy("000002", 0.6)]}
        w = {"s1": 1.0}
        base = {c.code: c.final_score
                for c in fuse_buy_signals(per, w, factor_blend=0.5)}
        withs = fuse_buy_signals(per, w, factor_blend=0.5,
                                 sentiment_scores={"000001": 1.0},
                                 sentiment_blend=0.0)
        for c in withs:
            assert c.final_score == pytest.approx(base[c.code])
            assert c.sentiment_score == 0.0

    def test_positive_sentiment_ranks_higher(self):
        per = {"s1": [_buy("000001", 0.7), _buy("000002", 0.7)]}
        w = {"s1": 1.0}
        fused = fuse_buy_signals(
            per, w, factor_blend=0.0,
            sentiment_scores={"000001": 1.0, "000002": -1.0},
            sentiment_blend=0.5)
        by = {c.code: c for c in fused}
        assert by["000001"].final_score > by["000002"].final_score
        assert by["000001"].sentiment_score == 1.0
        assert by["000002"].sentiment_score == -1.0
        assert fused[0].code == "000001"  # sorted desc by final_score

    def test_blends_over_one_are_renormalized(self):
        """factor_blend + sentiment_blend > 1 must not drive strat_w negative."""
        per = {"s1": [_buy("000001", 1.0)]}
        fused = fuse_buy_signals(per, {"s1": 1.0}, factor_blend=0.8,
                                 sentiment_scores={"000001": 1.0},
                                 sentiment_blend=0.8)
        assert 0.0 <= fused[0].final_score <= 1.0


# ----- score_candidates --------------------------------------------------

@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "sent.db"))
    d.initialize()
    yield d
    d.close()


class TestScoreCandidates:
    def test_scores_cache_and_neutral_for_no_news(self, db):
        client = StubLLMClient([
            _sentiment_block([{"code": "000001", "score": 0.8, "reason": "利好"}]),
        ])
        scores = score_candidates(
            db, ["000001", "000002"], client,
            as_of=date(2026, 6, 1), news_fetcher=_fake_news({"000001"}))
        assert scores["000001"] == pytest.approx(0.8)
        assert scores["000002"] == 0.0  # no news → neutral
        # Both cached for the day.
        assert db.get_news_sentiment("000001", "2026-06-01")["score"] == pytest.approx(0.8)
        assert db.get_news_sentiment("000002", "2026-06-01") is not None
        # Exactly one batched LLM call (only the newsy code needed scoring).
        assert len(client.calls) == 1

    def test_records_resolved_model_not_alias(self, db):
        """The cache's `model` must be what actually served the call. A client
        exposing resolved_model() (DeepSeek remaps claude-* → deepseek-v4-pro)
        → that resolved name is stored; no-news rows record an empty model."""
        class RemappingStub(StubLLMClient):
            def resolved_model(self, model: str) -> str:
                return "deepseek-v4-pro" if str(model).startswith("claude") else model

        client = RemappingStub([
            _sentiment_block([{"code": "000001", "score": 0.3, "reason": "x"}])])
        score_candidates(db, ["000001", "000002"], client,
                         as_of=date(2026, 6, 5), news_fetcher=_fake_news({"000001"}),
                         cfg=SentimentConfig(model="claude-sonnet-4-5"))
        assert db.get_news_sentiment("000001", "2026-06-05")["model"] == "deepseek-v4-pro"
        # No-news code: empty model (nothing scored it).
        assert db.get_news_sentiment("000002", "2026-06-05")["model"] == ""

    def test_second_call_hits_cache_no_llm(self, db):
        fetch = _fake_news({"000001"})
        score_candidates(db, ["000001"], StubLLMClient([
            _sentiment_block([{"code": "000001", "score": 0.5}])]),
            as_of=date(2026, 6, 1), news_fetcher=fetch)
        # A tripwire client that raises if create_message is ever called.
        tripwire = StubLLMClient([])
        scores = score_candidates(db, ["000001"], tripwire,
                                  as_of=date(2026, 6, 1), news_fetcher=fetch)
        assert scores["000001"] == pytest.approx(0.5)
        assert tripwire.calls == []  # served entirely from cache

    def test_no_llm_returns_neutral_and_does_not_cache(self, db):
        scores = score_candidates(db, ["000001"], None,
                                  as_of=date(2026, 6, 2),
                                  news_fetcher=_fake_news({"000001"}))
        assert scores["000001"] == 0.0
        # Not cached, so a later LLM-enabled tick can still score it.
        assert db.get_news_sentiment("000001", "2026-06-02") is None

    def test_fetcher_error_degrades_to_neutral(self, db):
        def boom(code, **kw):
            raise RuntimeError("network down")
        client = StubLLMClient([])  # must not be called: no headlines to score
        scores = score_candidates(db, ["000001"], client,
                                  as_of=date(2026, 6, 3), news_fetcher=boom)
        assert scores["000001"] == 0.0
        assert client.calls == []

    def test_max_codes_caps_work(self, db):
        client = StubLLMClient([_sentiment_block([
            {"code": f"00000{i}", "score": 0.1} for i in range(1, 3)])])
        cfg = SentimentConfig(max_codes=2)
        scores = score_candidates(
            db, ["000001", "000002", "000003"], client,
            as_of=date(2026, 6, 4), cfg=cfg,
            news_fetcher=_fake_news({"000001", "000002", "000003"}))
        assert set(scores.keys()) == {"000001", "000002"}  # 3rd dropped by cap


# ----- DB roundtrip ------------------------------------------------------

class TestDBSentiment:
    def test_upsert_and_get(self, db):
        db.upsert_news_sentiment("600519", "2026-06-01", 0.5,
                                 reason="a", n_news=3, model="m")
        row = db.get_news_sentiment("600519", "2026-06-01")
        assert row["score"] == pytest.approx(0.5)
        assert row["n_news"] == 3
        assert row["reason"] == "a"

    def test_upsert_conflict_updates(self, db):
        db.upsert_news_sentiment("600519", "2026-06-01", 0.5, n_news=3)
        db.upsert_news_sentiment("600519", "2026-06-01", -0.2,
                                 reason="b", n_news=1, model="m2")
        row = db.get_news_sentiment("600519", "2026-06-01")
        assert row["score"] == pytest.approx(-0.2)
        assert row["reason"] == "b"
        assert row["n_news"] == 1

    def test_get_missing_returns_none(self, db):
        assert db.get_news_sentiment("999999", "2026-06-01") is None
