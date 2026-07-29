"""Tests for the daily market-regime snapshot (quanti.regime — the report the
UI card renders; NOT quanti.agent.regime, which is the agent's own detector).

Network and LLM are never hit — the breadth layer is stubbed with synthetic
metrics and the LLM with a scripted client. What actually needs guarding:

  * the LLM call keeps ``tools=None`` — a forced tool_choice makes DeepSeek v4
    400 in thinking mode, and the existing client silently disables thinking to
    work around it. Someone "just adding a tool for structured output" would
    quietly downgrade the whole report; this test is the tripwire.
  * free-form LLM answers collapse back onto the fixed regime/action enums the
    UI colors by ("震荡偏空" → "震荡", "观望一下" → "观望")
  * an LLM failure still persists the deterministic data layer instead of
    leaving the day blank
  * same-day re-runs UPSERT rather than pile up rows
  * the 17:30 gate: earlier is a no-op, later runs exactly once per day
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from quanti.data.background_sync import BackgroundQuoteSyncer
from quanti.data.database import Database
from quanti.regime import report as R


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "acct.db"),
                 market_db_path=str(tmp_path / "market.db"))
    d.initialize()
    yield d
    d.close()


def _fake_breadth(latest: str = "2026-07-29") -> dict:
    """Minimal shape of breadth.build()'s return — enough for report.generate."""
    ind = pd.DataFrame({"mean": [12.0, -20.0], "count": [10, 30]},
                       index=["黄金", "半导体"])
    return {
        "latest": latest, "n_stocks": 5000,
        "above20": 45.0, "above50": 21.0, "above200": 14.0,
        "cap1": 0.9, "eq1": 1.3, "cap5": -0.6, "eq5": 2.7,
        "cap20": -3.8, "eq20": -9.2,
        "up": 4252, "dn": 1215, "fl": 56, "ad_ratio": 3.5,
        "nh": 676, "nl": 485,
        "amt_today": 23117.0, "amt5": 21187.0, "amt20": 26751.0,
        "amt_chg": -20.8, "turn": 2.22,
        "label": "震荡(区间/分化)", "score": -1, "reasons": ["MA50上方仅21%"],
        "ind_top": ind, "ind_bot": ind, "ind5_top": ind,
    }


class ScriptedLLM:
    """Returns a canned answer and records the kwargs it was called with."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict] = []

    def create_message(self, **kw):
        self.calls.append(kw)
        return {"stop_reason": "end_turn",
                "content": [{"type": "text", "text": self.text}]}


GOOD = ('```json\n{"regime":"震荡偏空","confidence":72,"headline":"缩量普涨",'
        '"drivers":["MA50上方仅21%"],"sectors_favored":["黄金"],'
        '"sectors_avoid":["半导体"],"action":"观望一下","risk_notes":["量能不足"]}\n```\n\n'
        '**正文标题**\n\n这里是报告正文。')


@pytest.fixture
def stub_breadth(monkeypatch):
    monkeypatch.setattr(R.breadth, "build", lambda path: _fake_breadth())


# --------------------------------------------------------------- parsing

class TestParsing:
    def test_free_text_collapses_to_enums(self):
        parsed = R._normalize(R._extract_json(GOOD))
        assert parsed["regime"] == "震荡"      # 「震荡偏空」→ 枚举
        assert parsed["action"] == "观望"      # 「观望一下」→ 枚举
        assert parsed["confidence"] == 72
        assert parsed["sectors_avoid"] == ["半导体"]

    def test_unparseable_json_degrades_to_blank_fields(self):
        parsed = R._normalize(R._extract_json("没有 json 块,只有正文"))
        assert parsed["regime"] == "" and parsed["action"] == ""
        assert parsed["confidence"] == 0

    def test_out_of_range_confidence_is_clamped(self):
        assert R._normalize({"confidence": 999})["confidence"] == 100
        assert R._normalize({"confidence": -5})["confidence"] == 0
        assert R._normalize({"confidence": "n/a"})["confidence"] == 0

    def test_body_strips_the_json_block(self):
        body, parsed, _ = R.run_llm("prompt", llm=ScriptedLLM(GOOD))
        assert "```json" not in body
        assert "这里是报告正文。" in body
        assert parsed["regime"] == "震荡"


class TestThinkingModePreserved:
    """DeepSeek v4 disables thinking whenever a single tool is forced — so the
    regime call must never pass tools. See openai_compat._thinking_on_by_default.
    """

    def test_llm_called_without_tools(self):
        llm = ScriptedLLM(GOOD)
        R.run_llm("prompt", llm=llm)
        assert llm.calls[0]["tools"] is None
        assert llm.calls[0]["model"] == "deepseek-v4-pro"

    def test_empty_response_is_an_error_not_a_blank_report(self):
        class Empty:
            def create_message(self, **kw):
                return {"stop_reason": "end_turn", "content": []}

        with pytest.raises(RuntimeError):
            R.run_llm("prompt", llm=Empty())


# --------------------------------------------------------------- persistence

class TestPersistence:
    def test_generate_persists_both_layers(self, db, stub_breadth):
        snap = R.generate(db, llm=ScriptedLLM(GOOD), with_news=False)
        assert snap["date"] == "2026-07-29"
        assert snap["rule_label"] == "震荡(区间/分化)" and snap["rule_score"] == -1
        assert snap["llm_regime"] == "震荡" and snap["action"] == "观望"

        loaded = R.load_latest(db)
        assert loaded["date"] == snap["date"]
        assert loaded["metrics"]["above50"] == 21.0
        assert loaded["sectors"]["top20"][0]["industry"] == "黄金"
        assert "报告正文" in loaded["report_md"]

    def test_llm_failure_still_saves_data_layer(self, db, stub_breadth):
        class Boom:
            def create_message(self, **kw):
                raise RuntimeError("api down")

        snap = R.generate(db, llm=Boom(), with_news=False)
        assert snap["rule_label"] == "震荡(区间/分化)"  # 数据面照常落库
        assert snap["llm_regime"] == "" and snap["model"] == ""
        assert snap["metrics"]["above50"] == 21.0
        assert "LLM 报告生成失败" in R.load_latest(db)["report_md"]

    def test_same_day_rerun_upserts(self, db, stub_breadth):
        R.generate(db, llm=ScriptedLLM(GOOD), with_news=False)
        R.generate(db, llm=ScriptedLLM(GOOD), with_news=False)
        assert len(R.load_history(db)) == 1

    def test_history_omits_heavy_fields(self, db, stub_breadth):
        R.generate(db, llm=ScriptedLLM(GOOD), with_news=False)
        row = R.load_history(db)[0]
        assert "report_md" not in row and "news" not in row
        assert row["metrics"]["ad_ratio"] == 3.5  # 画时间轴要用的还在

    def test_load_one_and_missing_day(self, db, stub_breadth):
        R.generate(db, llm=ScriptedLLM(GOOD), with_news=False)
        assert R.load_one(db, "2026-07-29")["rule_score"] == -1
        assert R.load_one(db, "1999-01-01") is None

    def test_snapshot_lives_in_the_market_db(self, db, stub_breadth):
        """Market-wide history must be shared by paper and live, not stuck in
        one account DB (and must not shadow the attached copy)."""
        R.generate(db, llm=ScriptedLLM(GOOD), with_news=False)
        n = db.conn.execute(
            "SELECT COUNT(*) FROM market.regime_snapshots").fetchone()[0]
        assert n == 1
        in_main = db.conn.execute(
            "SELECT 1 FROM main.sqlite_master WHERE type='table' "
            "AND name='regime_snapshots'").fetchone()
        assert in_main is None


# --------------------------------------------------------------- scheduling

class TestDailySchedule:
    """17:30 gate — the snapshot reads the day's closing bars, so running it
    in the morning would burn the day's slot on yesterday's data."""

    def _syncer(self, db, now: datetime, calls: list):
        return BackgroundQuoteSyncer(
            db=db, now_fn=lambda: now, regime_fn=lambda: calls.append(now))

    def test_before_1730_does_not_run(self, db):
        calls = []
        self._syncer(db, datetime(2026, 7, 29, 17, 29), calls)._maybe_run_regime()
        assert calls == []

    def test_after_1730_runs(self, db):
        calls = []
        self._syncer(db, datetime(2026, 7, 29, 17, 31), calls)._maybe_run_regime()
        assert len(calls) == 1

    def test_runs_only_once_per_day(self, db):
        calls = []
        s = self._syncer(db, datetime(2026, 7, 29, 18, 0), calls)
        s._maybe_run_regime()
        s._maybe_run_regime()
        s._maybe_run_regime()
        assert len(calls) == 1

    def test_next_day_runs_again(self, db):
        calls = []
        now = [datetime(2026, 7, 29, 18, 0)]
        s = BackgroundQuoteSyncer(db=db, now_fn=lambda: now[0],
                                  regime_fn=lambda: calls.append(now[0]))
        s._maybe_run_regime()
        now[0] = datetime(2026, 7, 30, 18, 0)
        s._maybe_run_regime()
        assert len(calls) == 2

    def test_failure_does_not_kill_the_loop_or_retry_same_day(self, db):
        hits = []

        def boom():
            hits.append(1)
            raise RuntimeError("nope")

        s = BackgroundQuoteSyncer(db=db, now_fn=lambda: datetime(2026, 7, 29, 18, 0),
                                  regime_fn=boom)
        s._maybe_run_regime()  # 不抛出
        s._maybe_run_regime()
        assert len(hits) == 1

    def test_no_regime_fn_is_a_noop(self, db):
        BackgroundQuoteSyncer(db=db)._maybe_run_regime()  # 不抛出


# --------------------------------------------------------------- news

class TestNews:
    def test_render_marks_missing_sources_explicitly(self):
        """空源要显式说「未取到」,否则 LLM 会把它读成「今天没消息」并编出
        一个平静的政策面。"""
        from quanti.regime import news as N
        out = N.render_news({"cctv": [], "flash": []})
        assert out.count("未取到") == 2

    def test_fetch_failure_degrades_to_empty(self, monkeypatch):
        from quanti.regime import news as N
        monkeypatch.setattr(N, "_with_timeout", lambda fn, timeout=0: None)
        assert N.fetch_cctv(date(2026, 7, 29)) == []
        assert N.fetch_flash() == []
