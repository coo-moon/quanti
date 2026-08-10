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
  * **the data-sufficiency gate** (TestDataSufficiencyGate below): a day whose
    quotes only half-landed must not produce a normal-looking rule label. This
    one runs against a *synthetic market.db* rather than a stub, because the
    bug lived in the arithmetic (empty slice → NaN → every comparison False).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from quanti.data.background_sync import BackgroundQuoteSyncer
from quanti.data.database import Database
from quanti.regime import breadth as B
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
        "usable": True, "unusable_reason": "",
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


# ------------------------------------------------------- 非有限浮点(NaN/Inf)

class TestNonFiniteMetrics:
    """NaN/±Inf 不是合法 JSON,一旦落库整条 /api/regime/* 就会 500。

    真实发生过(2026-08-06):那天全市场只同步到 1 只股票,breadth 对空切片求
    mean/median 得 NaN(above20/50/200、eq1、turn),`json.dumps` 默认
    allow_nan=True 把裸 `NaN` 写进 metrics_json;读回是 float('nan'),而
    starlette 的 JSONResponse 用 allow_nan=False 序列化 → ValueError → 500。

    指标算不出来的语义就是「无值」= JSON null,前端 num() 已按 null 显示「—」。
    """

    def test_non_finite_metrics_persist_as_null(self, db, monkeypatch):
        r = _fake_breadth()
        r.update(above20=float("nan"), turn=float("nan"), eq1=float("inf"))
        monkeypatch.setattr(R.breadth, "build", lambda path: r)
        R.generate(db, llm=ScriptedLLM(GOOD), with_news=False)

        raw = db.conn.execute(
            "SELECT metrics_json FROM regime_snapshots").fetchone()[0]
        assert "NaN" not in raw and "Infinity" not in raw, f"裸 NaN 落库了: {raw}"

        m = R.load_latest(db)["metrics"]
        assert m["above20"] is None and m["turn"] is None and m["eq1"] is None
        assert m["above50"] == 21.0      # 正常值不受影响

    def test_legacy_nan_row_still_loads_and_serializes(self, db):
        """修复前写进库的污染行也必须能读出来 —— 否则历史接口永远 500,
        除非手工改库。这条钉住「读侧也做净化」。"""
        db.conn.execute(
            "INSERT INTO regime_snapshots (date, rule_label, rule_score, "
            "llm_regime, llm_confidence, headline, action, metrics_json, "
            "sectors_json, llm_json, report_md, news_json, model, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-08-06", "震荡(区间/分化)", -1, "", 0, "", "",
             # 老代码写出来的形态:裸 NaN(非法 JSON)
             '{"above20": NaN, "above50": 44.6, "turn": NaN, "n_stocks": 1}',
             '{"top20": [{"industry": "黄金", "ret": NaN, "n": 5}]}',
             "{}", "", "{}", "", "2026-08-06T17:35:00"))
        db.conn.commit()

        rows = R.load_history(db)
        assert rows[0]["metrics"]["above20"] is None
        assert rows[0]["metrics"]["above50"] == 44.6
        # starlette 就是这么序列化的 —— 不许抛
        json.dumps({"items": rows}, allow_nan=False)

        full = R.load_one(db, "2026-08-06")
        assert full["sectors"]["top20"][0]["ret"] is None
        json.dumps(full, allow_nan=False)


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


# ------------------------------------------------- 数据充足性闸(合成 market.db)
#
# 这一组不 stub breadth —— bug 就长在算术里(空切片 .mean() → NaN,而 NaN 的
# 每一个比较都是 False),只有跑真的 build() 才钉得住。

HIST_DAYS = 210          # 够算 MA200(rolling min_periods=200)
N_CODES = 600            # > breadth.MIN_STOCKS,健康对照组必须过闸
LATEST = "2026-08-06"    # 事故当天


def _seed_market(db, *, days=HIST_DAYS, n_codes=N_CODES, latest_codes=None,
                 latest_are_new=False, basic=True, latest=LATEST) -> str:
    """在 market.db 里造一份合成全市场行情,返回最后一个交易日。

    价格单调上行 → 健康对照组会被判「上涨(多头)」,和「数据不足」区分得开。

    `latest_codes` 限制最后一天落了多少只票 —— 这就是 2026-08-06 的形态:
    17:30 的快照撞上当天行情还没同步完。`latest_are_new=True` 时这几只票
    **只有最后一天这一行**,线上那只票正是如此,所以 MA/涨跌家数全部算不
    出来;`False` 则它们有完整历史,MA 算得出来(只是只覆盖 1 只票)——
    两种形态都必须被挡住,不能只靠「算出 NaN」来发现问题。
    """
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(end=latest, periods=days)]
    codes = [f"{600000 + i:06d}" for i in range(n_codes)]
    n_last = n_codes if latest_codes is None else latest_codes
    hist_days = dates[:-1] if latest_are_new else dates
    quotes, stocks = [], []
    for i, c in enumerate(codes):
        stocks.append((c, f"股{i}", "SH", dates[0], f"行业{i // 100}", None))
        keep = hist_days if (latest_are_new or i < n_last) else hist_days[:-1]
        quotes += [(c, d, p, p, p, p, 1e6, p * 1e6, 1.0, 1.0, "t")
                   for k, d in enumerate(keep)
                   for p in (10.0 + 0.01 * k + 0.001 * i,)]
    last_codes = codes[:n_last]
    if latest_are_new:                      # 当天唯一有行情的票是「没有历史」的新票
        last_codes = [f"{301000 + i:06d}" for i in range(n_last)]
        for i, c in enumerate(last_codes):
            stocks.append((c, f"新股{i}", "SZ", dates[-1], "行业新", None))
        quotes += [(c, dates[-1], 12.0, 12.0, 12.0, 12.0, 1e6, 3.965e8 / n_last,
                    1.0, 1.0, "t") for c in last_codes]
    db.conn.executemany(
        "INSERT OR REPLACE INTO market.daily_quotes (code,date,open,high,low,"
        "close,volume,amount,turnover,adj_factor,source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", quotes)
    db.conn.executemany(
        "INSERT OR REPLACE INTO market.stocks (code,name,exchange,list_date,"
        "industry,delist_date) VALUES (?,?,?,?,?,?)", stocks)
    if basic:      # 事故当天 daily_basic 一行都没有 → turn 是空 median
        db.conn.executemany(
            "INSERT OR REPLACE INTO market.daily_basic (code,date,total_mv,"
            "turnover_rate) VALUES (?,?,?,?)",
            [(c, dates[-1], 1e10, 2.0) for c in last_codes])
    db.conn.commit()
    return dates[-1]


@pytest.fixture
def market_path(tmp_path):
    return str(tmp_path / "market.db")


class TestDataSufficiencyGate:
    """2026-08-06 复现:那天 17:30 的快照撞上当天行情还没同步完(daily_quotes
    只有 1 只票、daily_basic 一行都没有)。上游算出 NaN 只是症状,真正的隐患
    是 `classify()` 拿 NaN 做比较 —— `nan >= 60` 和 `nan <= 40` **都是 False**,
    于是静默落进 else 分支,垃圾数据产出一个看着完全正常的「震荡(区间/分化)」
    标签和投票分,还会被注入 LLM 决策 prompt。

    PR #153 修的是序列化层(NaN 不再落库),这一组钉的是根因。
    """

    def test_healthy_day_still_classifies(self, db, market_path):
        """先立对照组:完整的一天必须照常产出正常判定,否则这道闸就是在
        制造假阴性 —— 那比原来的假阳性更糟。"""
        _seed_market(db)
        r = B.build(market_path)
        assert r["usable"] is True and r["unusable_reason"] == ""
        assert r["n_stocks"] == N_CODES
        assert r["label"].startswith("上涨")      # 合成数据单调上行
        assert r["above20"] is not None and r["above200"] is not None

    def test_thin_day_is_marked_unusable(self, db, market_path):
        """事故形态:当天只有 1 只**没有历史**的票 → 宽度指标全部算不出来。"""
        _seed_market(db, latest_codes=1, latest_are_new=True, basic=False)
        r = B.build(market_path)
        assert r["n_stocks"] == 1
        assert r["usable"] is False
        assert r["unusable_reason"], "不可用必须给出原因,否则没法排查"
        # 核心断言:不许再产出一个看着正常的判定
        assert r["label"] == B.UNUSABLE_LABEL
        for normal in ("上涨", "震荡", "下跌"):
            assert normal not in r["label"], f"垃圾数据产出了「{normal}」判定"
        assert r["score"] == 0
        # NaN 换成 None:语义是「无值」,且 None 不会静默穿过比较
        for k in ("above20", "above50", "above200", "turn", "eq1"):
            assert r[k] is None, f"{k} 应为 None,实际 {r[k]!r}"

    def test_thin_day_with_computable_ma_is_still_unusable(self, db, market_path):
        """同样只覆盖 1 只票,但这只票有完整历史 → above* 算得出来且是 100%,
        一个 NaN 都没有。这比 NaN 更危险:100% 看着像「全市场普涨」,而真相是
        「那一只涨了」—— 拿真库倒回事故当天复放时正好撞上这一种。光靠「算出
        NaN」发现不了它,必须有覆盖度闸。"""
        _seed_market(db, latest_codes=1, latest_are_new=False)
        r = B.build(market_path)
        assert r["n_stocks"] == 1
        assert r["above20"] == 100.0, "前提变了:这条测的就是没有 NaN 的情况"
        # 这几个数字喂给 classify 会得到一个货真价实的「上涨(多头)」——
        # 也就是说,救下这一天的只能是覆盖度闸,不是 NaN 的可疑性。
        assert B.classify(100.0, 100.0, 100.0, None, None,
                          1.0, 0.0)[0].startswith("上涨")
        assert r["usable"] is False and r["label"] == B.UNUSABLE_LABEL

    def test_half_synced_day_is_unusable(self, db, market_path, monkeypatch):
        """同步到一半就快照:票数过了绝对下限,但只有平时的一半 —— 算出来的
        宽度是有偏的,数值上却看不出任何异常。这条钉相对覆盖度闸。"""
        monkeypatch.setattr(B, "MIN_STOCKS", 100)   # 关掉绝对闸,单独验相对闸
        _seed_market(db, latest_codes=N_CODES // 2)
        r = B.build(market_path)
        assert r["n_stocks"] == N_CODES // 2
        assert r["usable"] is False and r["label"] == B.UNUSABLE_LABEL
        assert "覆盖" in r["unusable_reason"]

    def test_render_survives_missing_metrics(self, db, market_path):
        """render() 原来用 `f"{r['above20']:.0f}%"`,对 None 直接 TypeError ——
        而不可用那天的 report_md 正是靠它生成的。"""
        _seed_market(db, latest_codes=1, latest_are_new=True, basic=False)
        out = B.render(B.build(market_path))
        assert B.UNUSABLE_LABEL in out
        assert "—" in out                     # 算不出来的指标显示为破折号

    def test_classify_refuses_nan_structure(self):
        """兜底:即使有人绕过 build() 的闸直接调 classify(),NaN 也不许再
        变成「震荡」。`nan >= 60` 与 `nan <= 40` 同时为 False 就是原来的 bug。"""
        nan = float("nan")
        label, reasons, score = B.classify(nan, nan, nan, None, None, 0.0, 0.0)
        assert label == B.UNUSABLE_LABEL and score == 0
        assert reasons, "至少要说清哪个指标缺失"
        label2, _, _ = B.classify(50.0, 50.0, None, 1.0, 1.0, 1.0, 0.0)
        assert label2 == B.UNUSABLE_LABEL     # None 同样挡

    def test_classify_unchanged_for_good_input(self):
        """签名和既有行为不许动 —— _selfcheck 和调用方都依赖它。"""
        assert B.classify(99, 99, 99, 3.0, 3.0, 5.0, 0)[0].startswith("上涨")
        assert B.classify(10, 10, 10, -3.0, -3.0, 0.2, 0)[0].startswith("下跌")
        assert B.classify(50, 50, 45, 0.0, 0.0, 1.0, 0)[0].startswith("震荡")


class TestUnusableReasonPredicate:
    """同一把尺子既量新算出来的 dict,也量**已落库的行** —— 包括修复前那些
    带裸 NaN(读侧被 _json_safe 读成 None)的污染行,所以不需要数据迁移。"""

    def test_polluted_legacy_row_is_flagged(self):
        """2026-08-06 真实落库的 metrics(NaN 经读侧净化后变 None)。"""
        assert B.unusable_reason(
            {"above20": None, "above50": None, "above200": None,
             "turn": None, "n_stocks": 1}, "震荡(区间/分化)")

    def test_healthy_row_passes(self):
        assert B.unusable_reason(
            {"above20": 87.0, "above50": 44.6, "above200": 21.0,
             "n_stocks": 5535}, "震荡(区间/分化)") == ""

    def test_unusable_label_alone_is_enough(self):
        """相对覆盖度闸算不出来时(落库的 metrics 里没有历史中位数),标签
        本身就是凭据。"""
        assert B.unusable_reason({"n_stocks": 3000}, B.UNUSABLE_LABEL)

    def test_absent_keys_are_not_evidence(self):
        """键缺失 ≠ 数据不足。_metrics_payload 会丢掉 None,老快照也可能只存
        了几个字段 —— 把「没写」当成「不足」会把历史快照全部误杀。"""
        assert B.unusable_reason({"above50": 21.0}) == ""
        assert B.unusable_reason({}) == ""


class TestUnusableSnapshotPersistence:
    """不可用的一天:落库留痕(比开天窗好排查),但不喂 LLM、不进 prompt。"""

    @pytest.fixture
    def thin(self, monkeypatch):
        r = _fake_breadth("2026-08-06")
        r.update(usable=False, unusable_reason="当日仅 1 只个股有行情",
                 label=B.UNUSABLE_LABEL, score=0, reasons=["当日仅 1 只个股有行情"],
                 n_stocks=1, above20=None, above50=None, above200=None, turn=None)
        monkeypatch.setattr(R.breadth, "build", lambda path: r)
        return r

    def test_saves_marker_and_skips_the_llm(self, db, thin):
        """拿 1 只票的宽度让模型写千字报告,只会得到一篇自信的胡话,还要花
        一次思考调用。"""
        llm = ScriptedLLM(GOOD)
        snap = R.generate(db, llm=llm, with_news=False)
        assert llm.calls == [], "数据不足还是调了 LLM"
        assert snap["usable"] is False
        assert snap["rule_label"] == B.UNUSABLE_LABEL and snap["rule_score"] == 0
        assert snap["llm_regime"] == "" and snap["headline"] == ""
        loaded = R.load_latest(db)
        assert loaded["rule_label"] == B.UNUSABLE_LABEL
        assert "数据不足" in loaded["report_md"]

    def test_marker_row_serializes_clean(self, db, thin):
        R.generate(db, llm=ScriptedLLM(GOOD), with_news=False)
        raw = db.conn.execute(
            "SELECT metrics_json FROM regime_snapshots").fetchone()[0]
        assert "NaN" not in raw and "Infinity" not in raw
        json.dumps(R.load_history(db), allow_nan=False)

    def test_marker_row_carries_no_market_metrics(self, db, monkeypatch):
        """不可用那天的「指标」量的是当天恰好落库的那几只票,不是市场。若那只票
        正好有完整历史,above20 就是 100% —— 前端会照着画一个「九成个股站上
        MA20」的卡片。这种数字比 NaN 更危险,一律不落库。"""
        r = _fake_breadth("2026-08-06")
        r.update(usable=False, unusable_reason="当日仅 1 只个股有行情",
                 label=B.UNUSABLE_LABEL, score=0, reasons=["x"], n_stocks=1,
                 above20=100.0, above50=100.0, above200=100.0)
        monkeypatch.setattr(R.breadth, "build", lambda path: r)
        m = R.generate(db, llm=ScriptedLLM(GOOD), with_news=False)["metrics"]
        assert m == {"n_stocks": 1}, f"垃圾指标落库了: {m}"
        # 只剩 n_stocks 也照样挡得住注入(绝对覆盖 + 标签,双保险)
        assert B.unusable_reason(m, B.UNUSABLE_LABEL)
        assert B.unusable_reason(m)

    def test_not_injected_into_prompt(self, db, thin):
        """最重要的一条:垃圾快照不许进决策 prompt。"""
        R.generate(db, llm=ScriptedLLM(GOOD), with_news=False)
        now = datetime(2026, 8, 7, 16, 0)      # 次日,陈旧闸放行
        snap, reason = P.latest_usable(db, now=now)
        assert snap is not None, "仍要返回行,tick 日志才能说出「有快照但不可用」"
        assert "数据不足" in reason
        block, meta = P.regime_block(db, _goal(), _Broker(), now=now)
        assert block == ""
        assert meta["regime_injected"] is False
        assert "数据不足" in meta["regime_skip_reason"]

    def test_legacy_polluted_row_is_not_injected(self, db):
        """修复前落库的行(rule_label 是正常的「震荡」、metrics 带 NaN)也必须
        被挡 —— 否则要么手工改库,要么它一直在往 prompt 里注入垃圾。"""
        db.conn.execute(
            "INSERT INTO regime_snapshots (date, rule_label, rule_score, "
            "llm_regime, llm_confidence, headline, action, metrics_json, "
            "sectors_json, llm_json, report_md, news_json, model, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-08-06", "震荡(区间/分化)", -1, "", 0, "", "",
             '{"above20": NaN, "above50": NaN, "above200": NaN, "n_stocks": 1}',
             "{}", "{}", "", "{}", "", "2026-08-06T17:35:00"))
        db.conn.commit()
        block, meta = P.regime_block(db, _goal(), _Broker(),
                                     now=datetime(2026, 8, 7, 16, 0))
        assert block == "" and meta["regime_injected"] is False
        assert "数据不足" in meta["regime_skip_reason"]

    def test_usable_day_still_injects(self, db, stub_breadth):
        """对照组:正常的一天照旧注入,这道闸没有顺手把好数据也挡了。"""
        R.generate(db, llm=ScriptedLLM(GOOD), with_news=False)   # 2026-07-29
        block, meta = P.regime_block(db, _goal(), _Broker(),
                                     now=datetime(2026, 7, 30, 16, 0))
        assert meta["regime_injected"] is True and "市场环境" in block


class TestRegimeRetryOnInsufficientData:
    """17:30 的快照可能撞上当天行情还没同步完(2026-08-06 就是)。不重试的话
    这一天永久是个「数据不足」的洞 —— 生成器一天只有一次机会。"""

    def _syncer(self, db, now_box, results):
        return BackgroundQuoteSyncer(
            db=db, now_fn=lambda: now_box[0], regime_fn=lambda: results.pop(0))

    def test_unusable_result_retries_after_cooldown(self, db):
        now = [datetime(2026, 8, 6, 17, 31)]
        results = [{"usable": False, "unusable_reason": "仅 1 只"},
                   {"usable": True}]
        s = self._syncer(db, now, results)
        s._maybe_run_regime()
        assert len(results) == 1, "第一次没跑"
        now[0] += timedelta(minutes=1)          # 冷却期内不许重复全市场扫描
        s._maybe_run_regime()
        assert len(results) == 1, "冷却期内又跑了一次"
        now[0] += timedelta(seconds=s._cfg.regime_retry_sec)
        s._maybe_run_regime()
        assert results == [], "补齐后没有重试"

    def test_usable_result_latches_the_day(self, db):
        now = [datetime(2026, 8, 6, 17, 31)]
        results = [{"usable": True}, {"usable": True}]
        s = self._syncer(db, now, results)
        s._maybe_run_regime()
        now[0] += timedelta(hours=3)
        s._maybe_run_regime()
        assert len(results) == 1, "成功之后当天不该再跑"

    def test_non_dict_result_still_latches(self, db):
        """老口径 regime_fn 返回 None(app.py 以前就是),不能因此变成天天重试。"""
        calls = []
        s = BackgroundQuoteSyncer(db=db, now_fn=lambda: datetime(2026, 8, 6, 18, 0),
                                  regime_fn=lambda: calls.append(1))
        s._maybe_run_regime()
        s._maybe_run_regime()
        assert len(calls) == 1

    def test_app_wiring_returns_the_snapshot(self):
        """`_daily_regime` 必须把 snap 传出来,否则重试判断永远拿不到
        usable=False —— 这个 wiring 断了不会有任何报错,只是静默失效。"""
        import inspect

        from quanti.api import app as app_mod
        src = inspect.getsource(app_mod.create_app)
        assert "return regime_report.generate(" in src, \
            "_daily_regime 丢掉了返回值,数据不足重试会静默失效"
