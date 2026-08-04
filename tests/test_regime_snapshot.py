"""Tests for the daily market-regime snapshot and its use inside a tick.

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
  * **the three gates on prompt injection** (TestRegimePromptGates below):
    opt-in off / immediate fill mode / stale snapshot each yield no context,
    and the payload never carries the report's LLM-written position advice.
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from quanti.data.background_sync import BackgroundQuoteSyncer
from quanti.data.database import Database
from quanti.regime import prompt as P
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
        assert llm.calls[0]["model"] == "deepseek-v4-flash"

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


# --------------------------------------------------- prompt injection gates

class _Broker:
    """Only the attribute the gate reads. Live QmtBroker has no _fill_mode at
    all — that path is covered by test_live_broker_without_fill_mode_passes."""

    def __init__(self, fill_mode="pending"):
        self._fill_mode = fill_mode


class _LiveBroker:
    """No _fill_mode attribute — mirrors QmtBroker (real venue matching, so
    there is no same-bar-close fill to guard against)."""


def _goal(**params):
    return SimpleNamespace(params=params)


@pytest.fixture
def snap_db(db):
    """A db holding one snapshot dated 2026-07-28 (a Tuesday)."""
    monkey = _fake_breadth("2026-07-28")
    R.save(db, {
        "date": monkey["latest"], "rule_label": monkey["label"],
        "rule_score": monkey["score"], "llm_regime": "震荡",
        "llm_confidence": 75, "headline": "防御资产继续占优", "action": "观望",
        "metrics": R._metrics_payload(monkey), "sectors": R._sectors_payload(monkey),
        "llm": {"regime": "震荡", "action": "观望",
                "sectors_favored": ["黄金"], "sectors_avoid": ["半导体"]},
        "report_md": "正文", "news": {}, "model": "deepseek-v4-pro",
        "created_at": "2026-07-28T17:35:00",
    })
    return db


WED_1600 = datetime(2026, 7, 29, 16, 0)   # 周三收盘后、17:30 快照生成前


class TestRegimePromptGates:
    def test_on_by_default(self, snap_db):
        """键缺失 = 开(口径同 wf_enabled)。已落库的老 goal 没有这个键,
        默认开就必须对它们也生效,否则「默认开」只是新建 goal 的默认值。"""
        block, meta = P.regime_block(snap_db, _goal(), _Broker(), now=WED_1600)
        assert meta["regime_injected"] is True
        assert "市场环境" in block

    def test_explicit_false_turns_it_off(self, snap_db):
        block, meta = P.regime_block(snap_db, _goal(regime_in_prompt=False),
                                     _Broker(), now=WED_1600)
        assert block == ""
        assert meta["regime_injected"] is False
        assert meta["regime_skip_reason"] == "已显式关闭"

    def test_enabled_reads_both_flags(self):
        assert P.enabled(_goal()) is True
        assert P.enabled(_goal(), P.DETECT_PARAM) is True
        assert P.enabled(_goal(regime_detect=False), P.DETECT_PARAM) is False
        assert P.enabled(_goal(regime_detect=False)) is True  # 两个开关互不影响

    def test_injects_when_enabled(self, snap_db):
        block, meta = P.regime_block(snap_db, _goal(regime_in_prompt=True),
                                     _Broker(), now=WED_1600)
        assert meta["regime_injected"] is True
        assert meta["regime_snapshot_date"] == "2026-07-28"
        assert "市场环境" in block and "2026-07-28" in block
        assert "MA20/50/200" in block and "21.0%" in block   # above50
        assert "震荡(区间/分化)" in block and "投票分 -1" in block

    def test_immediate_fill_mode_never_injects(self, snap_db):
        """CLI/MCP tick fills same-bar at close — feeding it T's closing
        breadth would be textbook look-ahead."""
        block, meta = P.regime_block(snap_db, _goal(regime_in_prompt=True),
                                     _Broker("immediate"), now=WED_1600)
        assert block == ""
        assert "immediate" in meta["regime_skip_reason"]

    def test_live_broker_without_fill_mode_passes(self, snap_db):
        block, _ = P.regime_block(snap_db, _goal(regime_in_prompt=True),
                                  _LiveBroker(), now=WED_1600)
        assert block != ""

    def test_stale_snapshot_is_dropped(self, snap_db):
        """快照 2026-07-28(周二) vs 决策日 2026-08-03(下周一) = 4 个交易日。"""
        block, meta = P.regime_block(snap_db, _goal(regime_in_prompt=True),
                                     _Broker(), now=datetime(2026, 8, 3, 16, 0))
        assert block == ""
        assert "陈旧" in meta["regime_skip_reason"]

    def test_no_snapshot_is_not_an_error(self, db):
        block, meta = P.regime_block(db, _goal(regime_in_prompt=True),
                                     _Broker(), now=WED_1600)
        assert block == "" and meta["regime_skip_reason"] == "无快照"

    def test_broken_db_degrades_to_empty(self, snap_db):
        class Boom:
            @property
            def conn(self):
                raise RuntimeError("db gone")

        block, meta = P.regime_block(Boom(), _goal(regime_in_prompt=True),
                                     _Broker(), now=WED_1600)
        assert block == "" and meta["regime_skip_reason"].startswith("异常")

    def test_payload_carries_no_position_advice(self, snap_db):
        """快照里 LLM 写的仓位指令与板块推荐一律不得进 prompt:
        action 是另一个 LLM 的仓位命令,板块 20 日动量对未来 20 日 rank IC
        为负(-0.0725, t=-9.27)且与 industry_neutral 对冲。"""
        block, _ = P.regime_block(snap_db, _goal(regime_in_prompt=True),
                                  _Broker(), now=WED_1600)
        for banned in ("加仓", "减仓", "观望", "黄金", "半导体",
                       "防御资产继续占优", "正文"):
            assert banned not in block, f"{banned!r} 不该出现在注入内容里"

    def test_payload_carries_the_no_resize_ban(self, snap_db):
        """客观数字必须和禁令一起注入,否则就是把被自己回测否定的择时信号
        直接喂给决策者。"""
        block, _ = P.regime_block(snap_db, _goal(regime_in_prompt=True),
                                  _Broker(), now=WED_1600)
        assert "不得据此调整 size_pct" in block
        assert "择时无 alpha" in block


class TestLatestUsable:
    def test_reports_reason_but_still_returns_the_row(self, snap_db):
        """陈旧时仍返回快照 —— tick 日志要能说出「有快照但太旧」。"""
        snap, reason = P.latest_usable(snap_db, now=datetime(2026, 8, 3, 16, 0))
        assert snap is not None and snap["date"] == "2026-07-28"
        assert "陈旧" in reason

    def test_fresh_has_empty_reason(self, snap_db):
        snap, reason = P.latest_usable(snap_db, now=WED_1600)
        assert snap["date"] == "2026-07-28" and reason == ""

    def test_snapshot_newer_than_decision_day_is_refused(self, snap_db):
        """快照比决策日还新 = 未来数据。`count_trading_days_between` 在
        start >= end 时返回 0,陈旧闸会把它读成「很新鲜」,所以要单独一道
        方向闸 —— 否则任何拿历史 now 回放的调用者都会吃到今天的宽度。"""
        snap, reason = P.latest_usable(snap_db, now=datetime(2025, 3, 14, 16, 0))
        assert snap is not None and "晚于决策日" in reason
        block, meta = P.regime_block(snap_db, _goal(), _Broker(),
                                     now=datetime(2025, 3, 14, 16, 0))
        assert block == "" and meta["regime_injected"] is False

    def test_holiday_calendar_needs_the_provider(self, snap_db):
        """陈旧闸算的是交易日距离。不传 provider 就退化成「工作日」近似,
        长假会被按交易日计 —— 注入端(llm_runtime)与 tick 日志端必须共用
        同一份日历,否则日志写「已注入」而 prompt 里一个字都没有。"""
        class _Cal:      # 2026-10-01..10-07 国庆休市(provider 的日历接口)
            @staticmethod
            def is_trade_date(d):
                return d.weekday() < 5 and not (
                    d.year == 2026 and d.month == 10 and 1 <= d.day <= 7)

            @staticmethod
            def get_trade_dates(a, b):
                return [a]      # 非空 = 日历有数据,缺席日按休市处理

        # 快照 = 节前最后一个交易日(09-30),决策日 = 节后首个交易日(10-08)
        R.save(snap_db, {
            "date": "2026-09-30", "rule_label": "震荡(区间/分化)", "rule_score": -1,
            "llm_regime": "", "llm_confidence": 0, "headline": "", "action": "",
            "metrics": {"above50": 21.0}, "sectors": {}, "llm": {},
            "report_md": "", "news": {}, "model": "", "created_at": "x"})
        after = datetime(2026, 10, 8, 16, 0)
        _, no_cal = P.latest_usable(snap_db, now=after)          # 工作日近似
        _, with_cal = P.latest_usable(snap_db, provider=_Cal(), now=after)
        assert "陈旧" in no_cal, "工作日近似把长假算成了交易日,前提变了"
        assert with_cal == "", "真日历下 09-30→10-08 只隔 1 个交易日,应放行"

    def test_timeout_actually_caps(self):
        """`with ThreadPoolExecutor(...)` 的 __exit__ 会 shutdown(wait=True),
        在超时之后继续死等挂住的线程 —— 45s 超时形同虚设,后台同步 daemon
        跟着一起卡死(实测发生过)。这条钉住「超时真的封顶」。"""
        import time
        from quanti.regime import news as N
        t0 = time.perf_counter()
        got = N._with_timeout(lambda: time.sleep(30) or "never", timeout=0.5)
        assert got is None
        assert time.perf_counter() - t0 < 3.0, "超时没封住,又退回死等了"
