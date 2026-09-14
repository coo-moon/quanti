"""前向模拟盘 runner(scripts/paper_forward.py)。

全 fake:临时目录里自建「共享行情库 + 独立账户库」,注入合成日线与月末总
市值快照;基准用同目录里的 idx_*.csv 缓存文件。**不打网络**(tushare 只在
`report --refresh-bench` 分支里 import,测试永不触达;策略读的也是临时库,
绝不碰部署机的 /opt/data 行情库)。

时间轴刻意用**历史日期**(2026 年 8/9 月)而不是「今天减 N 天」:PaperBroker
的挂单靠「created_at 之后的第一根 bar」成交,把 bar 落在确定的历史交易日上,
「次日开盘成交」才有确定答案(created_at 被改写到那个决策日的 12:00 —— 与
tests/test_pending_orders.py 同一手法)。副作用是持仓 mark 会落到
`current_price/avg_cost` 兜底(PaperBroker 的 `_latest_close` 只看最近 30
天),所以本文件只断言「订单/成交/权重」这些与日期无关的量,回撤另用注入的
净值序列验。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from quanti.data.database import Database

ROOT = Path(__file__).resolve().parents[1]


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "paper_forward", ROOT / "scripts" / "paper_forward.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pf = _load_runner()

# 八月首个可用信号周(工作日):2026-08-03..08-07 + 08-10
AUG = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06",
       "2026-08-07", "2026-08-10"]

# 合成池:60xxxx/68xxxx 才进得了 sse_enhance 的沪市全样本口径。
# 六月末市值快照(万元,Σ=1 亿 → 权重即目标权重)。
JULY_MV = {"600001": 3000.0, "600002": 2200.0, "600003": 1500.0,
           "600004": 1100.0, "600006": 700.0, "600007": 600.0,
           "600008": 500.0, "688010": 400.0,
           "688011": 300.0}      # 上市不满一年 → 该被 min_list_days 剔掉
JULY_W = {k: v / 10000.0 for k, v in JULY_MV.items() if k != "688011"}
# 八月末快照:688010 出样、600012 新入样,余下权重漂移
AUG_MV = {"600001": 3200.0, "600002": 2100.0, "600003": 1400.0,
          "600004": 1200.0, "600006": 650.0, "600007": 550.0,
          "600008": 480.0, "600012": 300.0, "688011": 300.0}
PRICES = {"600001": 12.0, "600002": 8.5, "600003": 21.0, "600004": 5.4,
          "600006": 30.0, "600007": 3.8, "600008": 16.5, "688010": 9.9,
          "688011": 4.0, "600012": 7.0}


class Sim:
    """合成行情库:股票 + 月末总市值快照 + 逐日平盘 bar(开=收,无跳空)。"""

    def __init__(self, data_dir: Path, prices: dict[str, float] | None = None
                 ) -> None:
        self.data = data_dir
        self.data.mkdir(parents=True, exist_ok=True)
        self.px: dict[str, float] = dict(prices or PRICES)
        self.market_db = self.data / "market.db"
        self.db = Database(str(self.market_db))
        self.db.initialize()
        self.universe: list[str] = []

    def stock(self, code: str, list_date: date = date(2010, 1, 1)) -> None:
        self.db.upsert_stock(code, f"测试{code}", "SH", list_date, "综合")
        self.universe.append(code)

    def snapshot(self, day: str, mv: dict[str, float]) -> None:
        """某月末的总市值快照(万元)——策略按它算下个月的目标权重。"""
        self.db.save_daily_basic(pd.DataFrame(
            [{"code": c, "date": day, "total_mv": v} for c, v in mv.items()]))

    def session(self, day: str, prices: dict[str, float] | None = None) -> None:
        """全池补一根当日 bar;prices 可覆盖个别代码(造暴跌/跳空)。"""
        px = {**self.px, **(prices or {})}
        self.db.save_daily_quotes(pd.DataFrame([{
            "code": code, "date": day, "open": px[code],
            "high": px[code] * 1.01, "low": px[code] * 0.99,
            "close": px[code], "volume": 5e7, "amount": px[code] * 5e7,
            "turnover": 1.0,
        } for code in self.universe]))

    def close(self) -> None:
        self.db.close()


@pytest.fixture
def sim(tmp_path):
    s = Sim(tmp_path / "data")
    for code in PRICES:
        s.stock(code, list_date=(date(2026, 6, 1) if code == "688011"
                                 else date(2010, 1, 1)))
    s.snapshot("2026-07-31", JULY_MV)
    for d in AUG:
        s.session(d)
    yield s
    s.close()


@pytest.fixture
def bare_strategies(tmp_path):
    """一个只含「空 param_space 但没有组合权重 sizer」策略的策略目录:
    单独验 preferred_sizer 那道判据(与 selectable/param_space 无关)。"""
    d = tmp_path / "strategies"
    d.mkdir()
    (d / "flat_no_sizer.py").write_text(
        '"""test-only:无 preferred_sizer 的逐股策略。"""\n'
        "from quanti.models import Direction, Signal\n"
        "from quanti.strategy.base import BaseStrategy\n\n\n"
        "class FlatNoSizer(BaseStrategy):\n"
        '    name = "flat_no_sizer"\n'
        "    param_space = {}\n"
        "    selectable = False\n"
        "    def init(self, config: dict) -> None:\n"
        "        pass\n"
        "    def on_bar(self, bar):\n"
        "        return [Signal(stock_code=bar.code, direction=Direction.BUY,\n"
        '                       strength=0.2, reason="per-stock")]\n')
    return str(d)


# ------------------------------------------------------------- 调用助手

def _call(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = pf.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def _run(sim: Sim, cmd: str, account: str = "fwdtest",
         extra: list[str] | None = None,
         strategies_dir: str = "") -> dict:
    rc, out, err = _call(*_argv(sim, cmd, account, strategies_dir),
                         *(extra or []))
    assert rc == 0, f"{cmd} 退出码 {rc}:{err}"
    return json.loads(out[out.index("{"):])


def _fail(sim: Sim, cmd: str, account: str = "fwdtest",
          extra: list[str] | None = None,
          strategies_dir: str = "") -> str:
    rc, _out, err = _call(*_argv(sim, cmd, account, strategies_dir),
                          *(extra or []))
    assert rc == 2, f"期望 fail-loud,实际 rc={rc}:err={err}"
    return err


def _argv(sim: Sim, cmd: str, account: str, strategies_dir: str = "") -> list:
    return [cmd, "--account", account, "--data-dir", str(sim.data),
            "--market-db", str(sim.market_db),
            "--strategies-dir", strategies_dir or str(ROOT / "strategies")]


def _acct_db(sim: Sim, account: str = "fwdtest") -> Database:
    db = Database(str(sim.data / f"{account}.db"),
                  market_db_path=str(sim.market_db))
    db.initialize()
    return db


def _backdate(db: Database, day: str) -> None:
    """把 pending 挂单的 created_at 改到决策日 12:00(09:25 之后 → 决策日
    就是当天),于是它的成交 bar 恰好是次日那根。"""
    ts = datetime.fromisoformat(f"{day}T12:00:00").isoformat()
    db.conn.execute("UPDATE orders SET created_at=? WHERE status='pending'",
                    (ts,))
    db.conn.commit()


def _all_orders(db: Database) -> list[dict]:
    return db.list_orders(limit=2000)


INIT = ["--strategy", "sse_enhance", "--cash", "1000000"]


# ---------------------------------------------------------------- 建仓

class TestInit:
    def test_registers_account_and_queues_first_batch(self, sim):
        out = _run(sim, "init", extra=INIT)
        db = _acct_db(sim)
        try:
            cfg = db.get_forward_config()
            assert cfg["strategy"] == "sse_enhance"
            assert cfg["initial_cash"] == 1000000.0
            # 账龄起点按**信号日**登记(数据滞后时不虚增天数)
            assert cfg["started_at"] == "2026-08-10"
            assert db.get_portfolio_state()["cash"] == 1000000.0
            # 信号日 = 行情库最新全市交易日;七月末快照 → 八月目标权重
            assert out["signal_date"] == "2026-08-10"
            assert out["data_asof"] == "2026-08-10"
            assert out["target_n"] == 8
            assert out["queued"] == {"buy": 8, "sell": 0}
            assert out["skipped_unaffordable"] == 0
            pend = db.list_orders(status="pending")
            assert {o["code"] for o in pend} == set(JULY_W)
            # strength = 目标组合权重;上市不满一年的 688011 被策略自己剔掉
            assert {round(o["strength"], 6) for o in pend} == {
                round(w, 6) for w in JULY_W.values()}
            assert "688011" not in {o["code"] for o in pend}
            # 只挂单不成交(T+1 次日开盘),且当天就留下一行数据日净值
            assert db.list_positions() == []
            assert db.list_trades() == []
            snaps = {r["snapshot_date"]: r["total_value"]
                     for r in db.get_portfolio_snapshots()}
            # 净值点锚在**数据日**上(空仓时 broker 按墙钟落库,runner 补一行
            # asof 口径,报告只认 <= asof 的序列)
            assert snaps["2026-08-10"] == 1000000.0
        finally:
            db.close()

    def test_strategy_config_is_persisted_and_passed_through(self, sim):
        """min_weight 截断重归一由策略自己做,runner 只透传参数。"""
        out = _run(sim, "init", extra=INIT + ["--strategy-config",
                                              '{"min_weight": 0.06}'])
        kept = {c: w for c, w in JULY_W.items() if w >= 0.06}
        total = sum(kept.values())
        pend = _acct_db(sim).list_orders(status="pending")
        assert out["queued"]["buy"] == len(kept)
        assert {o["code"] for o in pend} == set(kept)
        assert {round(o["strength"], 6) for o in pend} == {
            round(w / total, 6) for w in kept.values()}

    def test_bench_code_defaults_to_the_strategies_declaration(self, sim):
        ctx = pf.Ctx(account="fwdtest", data_dir=sim.data,
                     market_db=sim.market_db,
                     strategies_dir=str(ROOT / "strategies"))
        try:
            assert pf.bench_code_for(ctx, {}, "sse_enhance") == "000001.SH"
            assert pf.bench_code_for(ctx, {"bench_code": "000300.SH"},
                                     "sse_enhance") == "000300.SH"
        finally:
            ctx.db.close()

    def test_reinit_refuses_without_force_and_never_reanchors_pnl(self, sim):
        _run(sim, "init", extra=INIT)
        assert "已登记" in _fail(sim, "init", extra=INIT)
        # --force 只允许**改登记**,净值起点永远沿用账本:换个 --cash 重登
        # 不会把 pnl 悄悄清零(那等于篡改历史)。
        out = _run(sim, "init", extra=["--strategy", "sse_enhance",
                                       "--cash", "2000000", "--force"])
        db = _acct_db(sim)
        try:
            assert db.get_forward_config()["initial_cash"] == 1000000.0
            assert out["cash"] == 1000000.0
        finally:
            db.close()

    def test_reregistration_clears_stale_overrides(self, sim):
        """改登记以命令行参数为唯一事实源:没再传的 bench_code 不粘残值。"""
        _run(sim, "init", extra=INIT + ["--bench-code", "999999.SH"])
        assert _acct_db(sim).get_forward_config()["bench_code"] == "999999.SH"
        _run(sim, "init", extra=INIT + ["--force"])
        cfg = _acct_db(sim).get_forward_config()
        assert not cfg.get("bench_code")
        ctx = pf.Ctx(account="fwdtest", data_dir=sim.data,
                     market_db=sim.market_db,
                     strategies_dir=str(ROOT / "strategies"))
        try:
            assert pf.bench_code_for(ctx, cfg, "sse_enhance") == "000001.SH"
        finally:
            ctx.db.close()

    def test_interactive_account_names_are_refused(self, sim):
        for acct in ("paper", "live"):
            assert "独立账户" in _fail(sim, "init", account=acct, extra=INIT)


# ---------------------------------------------------------------- tick

class TestTick:
    def test_next_day_open_fill_lands_target_weights(self, sim):
        _run(sim, "init", extra=INIT)
        db = _acct_db(sim)
        try:
            _backdate(db, "2026-08-10")
            sim.session("2026-08-11")                # 次日 bar 落库
            out = _run(sim, "tick")
            assert out["fills"]["filled"] == 8
            assert out["fills"]["rejected"] == 0
            # 目标已全部持有 → 本月不再挂单
            assert out["queued"] == {"buy": 0, "sell": 0}
            pos = {p["code"]: p for p in db.list_positions()}
            assert set(pos) == set(JULY_W)
            assert all(p["quantity"] % 100 == 0 for p in pos.values())
            assert all(p["entry_strategy"] == "sse_enhance"
                       for p in pos.values())
            r = _run(sim, "report")
            total = r["total_value"]
            # 权重对账:整手取整 + 最后一腿的现金缓冲误差内命中目标权重
            # (100 万资金、一手 ≤3000 元 → 单票偏差 <0.5%)
            for code, w in JULY_W.items():
                got = pos[code]["quantity"] * PRICES[code] / total
                assert abs(got - w) < 0.005, f"{code} 权重 {got:.4f} vs {w}"
            assert total == pytest.approx(r["cash"] + r["market_value"],
                                          abs=0.01)
            assert r["positions"] == 8
            assert r["nav"] < 1.0                    # 费用 + 滑点必然小亏
            assert r["pnl_pct"] == pytest.approx(r["nav"] - 1.0, abs=1e-6)
            assert r["nav_days"] >= 2
        finally:
            db.close()

    def test_same_signal_date_tick_is_idempotent(self, sim):
        _run(sim, "init", extra=INIT)
        db = _acct_db(sim)
        first = db.list_orders(status="pending")
        again = _run(sim, "tick")
        try:
            assert again["idempotent_skip"] is True
            assert again["target_n"] == 0            # 查重命中,没再算目标
            pend = db.list_orders(status="pending")
            assert len(pend) == len(first) == 8
            assert len({o["code"] for o in pend}) == 8   # 每码一批
            assert len(_all_orders(db)) == 8             # 没有第二批单
            assert db.list_trades() == []
        finally:
            db.close()

    def test_mid_month_tick_does_not_churn(self, sim):
        """月中新信号日:目标权重不变 → 一单不挂(月频节奏来自策略内部)。"""
        _run(sim, "init", extra=INIT)
        db = _acct_db(sim)
        _backdate(db, "2026-08-10")
        sim.session("2026-08-11")
        _run(sim, "tick")
        sim.session("2026-08-12")
        out = _run(sim, "tick")
        n_orders = len(_all_orders(db))
        db.close()
        assert out["signal_date"] == "2026-08-12"
        assert out["idempotent_skip"] is False       # 真的重算了目标权重
        assert out["target_n"] == 8
        assert out["queued"] == {"buy": 0, "sell": 0}
        assert n_orders == 8                          # 成交后无新增挂单

    def test_month_end_rebalance_sells_out_and_buys_in(self, sim):
        """跨月首个信号日用**上月末快照**重建目标:出样本的卖、新进的买,
        留下的老持仓一律不动(策略「漂移不调」的口径)。"""
        _run(sim, "init", extra=INIT)
        db = _acct_db(sim)
        _backdate(db, "2026-08-10")
        sim.session("2026-08-11")
        _run(sim, "tick")
        sim.snapshot("2026-08-31", AUG_MV)           # 688010 出样,600012 入样
        sim.session("2026-09-01")
        out = _run(sim, "tick")
        assert out["signal_date"] == "2026-09-01"
        assert out["queued"] == {"buy": 1, "sell": 1}
        pend = db.list_orders(status="pending")
        assert {(o["code"], o["direction"]) for o in pend} == {
            ("688010", "sell"), ("600012", "buy")}
        # 留下的持仓不会被重新下一遍买单;次新 688011 依然进不来
        assert {o["code"] for o in pend} & set(JULY_W) == {"688010"}
        _backdate(db, "2026-09-01")
        sim.session("2026-09-02")
        out2 = _run(sim, "tick")
        held = {p["code"] for p in db.list_positions()}
        db.close()
        assert out2["fills"]["filled"] == 2
        assert held == (set(JULY_W) - {"688010"}) | {"600012"}

    def test_no_per_stock_stop_overlay_on_a_crash(self, sim):
        """复制盘不得带个股止损/止盈/策略离场 overlay:暴跌 40% 也不许被
        自动卖掉(卖掉成分不回补 = 跟踪误差)。"""
        _run(sim, "init", extra=INIT)
        db = _acct_db(sim)
        _backdate(db, "2026-08-10")
        sim.session("2026-08-11")
        _run(sim, "tick")
        sim.session("2026-08-12", prices={"600001": 7.2})   # -40%
        out = _run(sim, "tick")
        held = {p["code"] for p in db.list_positions()}
        sells = [o for o in _all_orders(db) if o["direction"] == "sell"]
        risk_rows = [o for o in _all_orders(db) if o["strategy_name"]
                     in ("risk_exit", "drift_trim", "llm_guard", "ensemble")]
        db.close()
        assert out["queued"]["sell"] == 0
        assert held == set(JULY_W)
        assert sells == []
        assert risk_rows == []
        assert _run(sim, "report")["positions"] == 8

    def test_missing_snapshot_never_flattens_the_book(self, sim):
        """参考数据断供 ≠ 目标清空:上月末快照里没有一只合格成分时,必须
        **不动持仓**(否则一次数据缺口就把整张前向账砸了)。"""
        _run(sim, "init", extra=INIT)
        db = _acct_db(sim)
        _backdate(db, "2026-08-10")
        sim.session("2026-08-11")
        _run(sim, "tick")
        # 八月末快照只剩上市不满一年的 688011 → 被 min_list_days 剔光 → 目标空
        sim.snapshot("2026-08-31", {"688011": 300.0})
        sim.session("2026-09-01")
        out = _run(sim, "tick")
        held = {p["code"] for p in db.list_positions()}
        sells = [o for o in _all_orders(db) if o["direction"] == "sell"]
        db.close()
        assert out["signal_date"] == "2026-09-01"
        assert out["target_n"] == 0
        assert out["queued"] == {"buy": 0, "sell": 0}
        assert sells == []
        assert held == set(JULY_W)
        r = _run(sim, "report")
        assert any("目标权重" in a for a in r["alerts"])

    def test_whole_universe_is_not_choked_by_daily_trade_cap(self, tmp_path):
        """一篮子 40 只必须全挂上:默认 RiskConfig 的 20 单/日会把复制盘
        静默砍剩 20 只 —— 前向账户的宽松风控正为堵这个。"""
        prices = {f"6003{i:02d}": 10.0 for i in range(40)}
        s = Sim(tmp_path / "wide", prices=prices)
        for code in prices:
            s.stock(code)
        s.snapshot("2026-07-31", {c: 100.0 for c in prices})
        for d in AUG:
            s.session(d)
        try:
            out = _run(s, "init", account="fwdfull", extra=INIT)
            assert out["target_n"] == 40
            assert out["queued"]["buy"] == 40
            assert out["rejected_submit"] == 0
        finally:
            s.close()

    def test_lot_floor_skips_unaffordable_names_without_lying(self, sim):
        """3000 元买不起任何一手:一单不挂,但如实记账(不静默改权重)。"""
        out = _run(sim, "init", extra=["--strategy", "sse_enhance",
                                       "--cash", "3000"])
        assert out["queued"] == {"buy": 0, "sell": 0}
        assert out["target_n"] == 8
        assert out["skipped_unaffordable"] == 8
        assert out["dropped_weight"] == pytest.approx(1.0, abs=0.01)
        r = _run(sim, "report")
        assert r["pending_orders"]["count"] == 0
        assert any("整手" in a for a in r["alerts"])


# ------------------------------------------------------------- fail-loud

class TestStrategyGate:
    def test_unknown_strategy_name(self, sim):
        assert "没有名为" in _fail(sim, "init",
                                   extra=["--strategy", "no_such_alpha",
                                          "--cash", "1000000"])

    def test_per_stock_strategy_is_refused(self, sim):
        """ma_cross 是可选逐股策略:有 param_space、无组合权重 sizer。"""
        err = _fail(sim, "init", extra=["--strategy", "ma_cross",
                                        "--cash", "1000000"])
        assert "不是组合权重类策略" in err
        assert "param_space" in err and "preferred_sizer" in err

    def test_weight_sizer_is_required_even_without_param_space(
            self, sim, bare_strategies):
        err = _fail(sim, "init", extra=["--strategy", "flat_no_sizer",
                                        "--cash", "1000000"],
                    strategies_dir=bare_strategies)
        assert "preferred_sizer" in err

    def test_empty_target_weights_is_refused(self, tmp_path):
        """没有上月末市值快照 → 目标为空,不许挂一张空账。"""
        s = Sim(tmp_path / "bare", prices={"600001": 10.0})
        s.stock("600001")
        for d in AUG:
            s.session(d)
        try:
            assert "给不出目标权重" in _fail(s, "init", extra=INIT)
        finally:
            s.close()

    def test_unregistered_account_tick_and_report_fail(self, sim):
        assert "未登记" in _fail(sim, "tick", account="never_init")
        assert "未登记" in _fail(sim, "report", account="never_init")

    def test_missing_market_db_is_refused(self, tmp_path):
        s = Sim(tmp_path / "nodata")
        s.close()
        rc, _out, err = _call("tick", "--account", "fwdtest",
                              "--data-dir", str(s.data),
                              "--market-db", str(s.data / "nope.db"))
        assert rc == 2 and "共享行情库不存在" in err


# ---------------------------------------------------------------- 报告

class TestReport:
    def _filled_account(self, sim) -> None:
        _run(sim, "init", extra=INIT)
        db = _acct_db(sim)
        _backdate(db, "2026-08-10")
        sim.session("2026-08-11")
        _run(sim, "tick")
        db.close()

    def test_report_fields_and_bench_window(self, sim):
        (sim.data / "idx_000001_SH.csv").write_text(
            "ts_code,trade_date,close\n"
            "000001.SH,20260810,3500.0\n"
            "000001.SH,20260811,3605.0\n")           # 基准同期 +3%
        self._filled_account(sim)
        r = _run(sim, "report")
        assert r["account"] == "fwdtest"
        assert r["strategy"] == "sse_enhance"
        assert r["as_of"] == r["data_asof"] == "2026-08-11"
        assert r["started_at"] == "2026-08-10"
        assert r["days_running"] == 1
        assert r["bench"]["code"] == "000001.SH"
        assert r["bench"]["pnl_pct"] == pytest.approx(0.03, abs=1e-6)
        assert r["bench"]["start_date"] == "2026-08-10"
        # 组合小亏(费用+滑点) vs 基准 +3% → 超额必为负且口径自洽
        assert r["excess"] == pytest.approx(r["pnl_pct"] - 0.03, abs=1e-6)
        assert r["excess"] < 0
        assert r["max_drawdown"] <= 0.0
        assert r["turnover_last_month"] > 0.0
        assert r["pending_orders"]["count"] == 0
        for key in ("nav", "total_value", "pnl_pct", "positions", "alerts",
                    "last_build"):
            assert key in r

    def test_bench_code_override_is_honoured(self, sim):
        (sim.data / "idx_999999_SH.csv").write_text(
            "ts_code,trade_date,close\n"
            "999999.SH,20260810,100.0\n999999.SH,20260811,90.0\n")
        _run(sim, "init", extra=INIT + ["--bench-code", "999999.SH"])
        db = _acct_db(sim)
        _backdate(db, "2026-08-10")
        sim.session("2026-08-11")
        _run(sim, "tick")
        db.close()
        r = _run(sim, "report")
        assert r["bench"]["code"] == "999999.SH"
        assert r["bench"]["pnl_pct"] == pytest.approx(-0.1, abs=1e-6)
        assert r["excess"] > 0                       # 小亏跑赢 -10%

    def test_max_drawdown_from_nav_series(self, sim):
        """回撤按净值序列峰谷算,并在越过 -8% 时告警(前向盘不自动清仓)。"""
        _run(sim, "init", extra=INIT)
        # 手工注一个峰点(报告的回撤只看 portfolio_snapshots;不去蹭 broker
        # 那个 30 天的 mark 窗口,免得上面那套历史日期时灵时不灵)
        db = _acct_db(sim)
        db.save_portfolio_snapshot(date(2026, 8, 6), 40_000.0, 1_060_000.0,
                                   1_100_000.0)
        db.close()
        r = _run(sim, "report")
        assert r["max_drawdown"] == pytest.approx(1_000_000.0 / 1_100_000.0 - 1,
                                                  abs=1e-6)
        assert any("回撤" in a for a in r["alerts"])

    def test_turnover_is_one_sided_of_both_legs(self, sim):
        self._filled_account(sim)
        db = _acct_db(sim)
        trades = db.list_trades()
        db.close()
        r = _run(sim, "report")
        amt = sum(t["price"] * t["quantity"] for t in trades)
        assert len(trades) == 8
        assert r["trades_last_month"] == 8
        assert r["turnover_last_month"] == pytest.approx(
            amt / 2 / r["total_value"], rel=0.03)

    def test_missing_bench_file_is_an_alert_not_a_crash(self, sim):
        self._filled_account(sim)
        r = _run(sim, "report")
        assert r["bench"]["pnl_pct"] is None
        assert any("基准" in a for a in r["alerts"])

    def test_stale_market_data_is_flagged(self, sim):
        """时段外/断供时 tick 照跑,但 data_asof 必须被标出来。"""
        _run(sim, "init", extra=INIT)
        r = _run(sim, "report")
        assert r["data_asof"] == "2026-08-10"
        assert any("滞后" in a for a in r["alerts"])


# ------------------------------------------------------------ 登记表

class TestForwardConfig:
    def test_keys_live_in_app_config_without_touching_others(self, sim):
        _run(sim, "init", extra=INIT)
        db = _acct_db(sim)
        try:
            db.upsert_app_config("akshare", "tok-123")
            assert db.get_forward_config()["strategy"] == "sse_enhance"
            app = db.get_app_config()
            assert app["data_source"] == "akshare"
            assert app["data_source_token"] == "tok-123"
            # 合并写:只改传入的键,其余原样
            db.set_forward_config(bench_code="000300.SH")
            cfg = db.get_forward_config()
            assert cfg["bench_code"] == "000300.SH"
            assert cfg["strategy"] == "sse_enhance"
            assert cfg["initial_cash"] == 1000000.0
        finally:
            db.close()

    def test_legacy_db_gets_the_column_added(self, tmp_path):
        """旧库(无 forward_config 列)重新打开即补列,既有行不动。"""
        p = tmp_path / "legacy.db"
        db = Database(str(p))
        db.initialize()
        db.upsert_app_config("tushare", "abc")
        db.conn.execute("ALTER TABLE app_config DROP COLUMN forward_config")
        db.conn.commit()
        db.close()
        reopened = Database(str(p))
        reopened.initialize()
        try:
            assert reopened.get_forward_config() == {}
            reopened.set_forward_config(strategy="sse_enhance",
                                        initial_cash=5.0)
            cfg = reopened.get_forward_config()
            assert cfg == {"strategy": "sse_enhance", "initial_cash": 5.0}
            assert reopened.get_app_config()["data_source_token"] == "abc"
        finally:
            reopened.close()
