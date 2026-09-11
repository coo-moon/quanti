"""打新增强估计器:额度/权限口径、卖出规则、分页限速、闸门。

全部经注入的 fake pro 走完(含 main 端到端),绝不触网;限速用注入的
sleep/clock 断言,不真睡。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ipo_yield_study as ipo  # noqa: E402


class FakePro:
    """tushare pro_api 的替身:new_share 分页 + daily 按交易日。"""

    def __init__(self, rows: list[dict] | None = None,
                 daily: dict[str, list[dict]] | None = None,
                 ranges: dict[str, list[dict]] | None = None,
                 fail_first_n: int = 0,
                 fail_msg: str = "抱歉,您每分钟最多访问该接口200次"):
        self.rows = rows if rows is not None else []
        self.daily_map = daily or {}
        self.range_map = ranges or {}
        self.new_share_calls: list[dict] = []
        self.daily_calls: list[str] = []
        self.range_calls: list[dict] = []
        self.fail_first_n = fail_first_n
        self.fail_msg = fail_msg

    def new_share(self, start_date=None, end_date=None, offset=0, limit=300):
        self.new_share_calls.append(
            {"start_date": start_date, "end_date": end_date,
             "offset": offset, "limit": limit})
        if self.fail_first_n > 0:
            self.fail_first_n -= 1
            raise RuntimeError(self.fail_msg)
        return pd.DataFrame(self.rows[offset:offset + limit])

    def daily(self, trade_date=None, ts_code=None, start_date=None,
              end_date=None):
        if ts_code is not None:
            self.range_calls.append({"ts_code": ts_code, "start_date": start_date,
                                     "end_date": end_date})
            return pd.DataFrame(self.range_map.get(ts_code, []))
        self.daily_calls.append(trade_date)
        return pd.DataFrame(self.daily_map.get(trade_date, []))


class FakeClock:
    """单调时钟 + 记录 sleep:让限速断言不依赖真实时间。"""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def _row(ts_code: str, **kw) -> dict:
    return {
        "ts_code": ts_code, "sub_code": ts_code[:6],
        "name": kw.get("name", "示例"),
        "ipo_date": kw.get("ipo_date", "20240605"),
        "issue_date": kw.get("issue_date", "20240612"),
        "amount": kw.get("amount", 5000.0),
        "market_amount": kw.get("market_amount", 2000.0),
        "price": kw.get("price", 10.0),
        "pe": 23.0, "limit_amount": kw.get("limit_amount", 1.0),
        "funds": 5.0, "ballot": kw.get("ballot", 0.05),
    }


def _bars(*rows: tuple) -> list[dict]:
    return [{"date": d, "open": o, "high": h, "low": low, "close": c}
            for d, o, h, low, c in rows]


def _mk_market_db(path: Path, quotes: list[tuple]) -> None:
    """最小 market.db:daily_quotes + stocks(估计器只读这两张表)。"""
    con = sqlite3.connect(path)
    try:
        con.executescript(
            "create table daily_quotes (code text, date text, open real,"
            " high real, low real, close real, volume real, amount real,"
            " turnover real default 0, adj_factor real default 1.0,"
            " source text default '', primary key (code, date));"
            "create table stocks (code text primary key, name text,"
            " exchange text, list_date text, industry text, delist_date text);")
        con.executemany(
            "insert into daily_quotes (code, date, open, high, low, close,"
            " volume, amount) values (?,?,?,?,?,?,?,?)", quotes)
        con.commit()
    finally:
        con.close()


def _with_stocks(path: Path, rows: list[tuple]) -> None:
    """往最小 market.db 的 stocks 表写 (code, exchange, list_date)。"""
    con = sqlite3.connect(path)
    try:
        con.executemany(
            "insert into stocks (code, name, exchange, list_date, industry)"
            " values (?, '示例', ?, ?, '')", rows)
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------- 板块与额度
class TestBoardAndQuota:

    def test_classify_board_excludes_bj(self):
        assert ipo.classify_board("688001.SH") == "SH_STAR"
        assert ipo.classify_board("689009.SH") == "SH_STAR"
        assert ipo.classify_board("601728.SH") == "SH_MAIN"
        assert ipo.classify_board("000001.SZ") == "SZ_MAIN"
        assert ipo.classify_board("301589.SZ") == "SZ_GEM"
        for code in ("920229.BJ", "830001.BJ", "430047.BJ", "870508.BJ"):
            assert ipo.classify_board(code) is None

    def test_quota_units_floor_to_lot(self):
        # 沪市主板:每 1 万 1000 股;不足 1 万的部分不计入
        assert ipo.subscription_shares("SH_MAIN", 100_000) == 10_000
        assert ipo.subscription_shares("SH_MAIN", 35_000) == 3_000
        # 科创板/深市:每 5000 元 500 股
        assert ipo.subscription_shares("SH_STAR", 50_000) == 5_000
        assert ipo.subscription_shares("SZ_GEM", 9_999) == 500

    def test_quota_capped_by_issue_and_limit(self):
        # 千分之一上限:网上发行量 2000 万股 → 2 万股
        assert ipo.subscription_shares(
            "SH_MAIN", 5_000_000, market_amount_wan=2000.0) == 20_000
        # 申购上限(万股)优先;14500 不是 1000 股整数倍 → 取整到 14000
        assert ipo.subscription_shares(
            "SH_MAIN", 5_000_000, market_amount_wan=2000.0,
            limit_amount_wan=1.45) == 14_000
        assert ipo.subscription_shares(
            "SH_STAR", 5_000_000, market_amount_wan=2000.0,
            limit_amount_wan=1.45) == 14_500      # 科创板单位 500 股
        assert ipo.subscription_shares("SH_STAR", 0) == 0

    def test_star_and_gem_need_permission(self):
        assert not ipo.board_permitted("SH_STAR", sh_mv=100_000, sz_mv=0)
        assert ipo.board_permitted("SH_STAR", sh_mv=500_000, sz_mv=0)
        assert not ipo.board_permitted("SZ_GEM", sh_mv=1_000_000, sz_mv=50_000)
        assert ipo.board_permitted("SZ_GEM", sh_mv=1_000_000, sz_mv=100_000)
        assert ipo.board_permitted("SZ_MAIN", sh_mv=0, sz_mv=10_000)


# ---------------------------------------------------------------- 卖出规则
class TestSellRule:

    def test_first_open_is_default(self):
        bars = _bars(("2024-06-12", 30.0, 33.0, 25.0, 31.0),
                     ("2024-06-13", 32.0, 35.0, 30.0, 34.0))
        px, rule = ipo.choose_sell_price(bars, 10.0, "SH_STAR", date(2024, 6, 12))
        assert (px, rule) == (30.0, "first_open")

    def test_one_word_board_sells_next_open(self):
        bars = _bars(("2024-06-12", 14.4, 14.4, 14.4, 14.4),
                     ("2024-06-13", 15.8, 15.8, 15.8, 15.8))
        px, rule = ipo.choose_sell_price(bars, 10.0, "SH_STAR", date(2024, 6, 12))
        assert (px, rule) == (15.8, "next_open")

    def test_one_word_without_next_bar_is_unsellable(self):
        bars = _bars(("2024-06-12", 14.4, 14.4, 14.4, 14.4))
        px, rule = ipo.choose_sell_price(bars, 10.0, "SH_MAIN", date(2024, 6, 12))
        assert px is None and rule == "unsellable"

    def test_44pct_cap_only_before_registration(self):
        # 注册制前主板:收盘顶格 44% → 按次日开盘
        bars = _bars(("2021-11-16", 20.84, 25.01, 20.84, 25.01),
                     ("2021-11-17", 27.51, 27.51, 27.51, 27.51))
        px, rule = ipo.choose_sell_price(bars, 17.37, "SH_MAIN", date(2021, 11, 16))
        assert (px, rule) == (27.51, "next_open")
        # 全面注册制后主板前 5 日不限价:同样的形态按首日开盘
        bars = _bars(("2024-11-26", 65.0, 160.99, 60.0, 160.99),
                     ("2024-11-27", 140.0, 150.0, 130.0, 141.0))
        px, rule = ipo.choose_sell_price(bars, 7.98, "SH_MAIN", date(2024, 11, 26))
        assert (px, rule) == (65.0, "first_open")

    def test_first_open_rule_disables_fallback(self):
        bars = _bars(("2021-11-16", 20.84, 25.01, 20.84, 25.01),
                     ("2021-11-17", 27.51, 27.51, 27.51, 27.51))
        px, rule = ipo.choose_sell_price(bars, 17.37, "SH_MAIN", date(2021, 11, 16),
                                         sell_rule="first-open")
        assert (px, rule) == (20.84, "first_open")

    def test_sell_cost_rate_halves_stamp_duty(self):
        assert ipo.sell_cost_rate(date(2023, 8, 25)) > \
            ipo.sell_cost_rate(date(2023, 8, 28))


# ---------------------------------------------------------------- 逐股期望
class TestComputeTier:

    def _stocks(self) -> pd.DataFrame:
        raw = pd.DataFrame([
            _row("688001.SH", name="科创示例", price=10.0, market_amount=2000.0,
                 limit_amount=1.0, ballot=0.05),
            _row("603001.SH", name="主板示例", price=20.0, market_amount=6000.0,
                 limit_amount=2.0, ballot=0.04),
            _row("920001.BJ", name="北交示例"),
        ])
        return ipo.normalize_new_share(raw)

    def test_bj_excluded_and_star_needs_permission(self):
        stocks = self._stocks()
        assert list(stocks.code) == ["688001", "603001"]
        bars = {"688001": _bars(("2024-06-12", 30.0, 31.0, 29.0, 30.5)),
                "603001": _bars(("2024-06-12", 25.0, 27.0, 24.0, 26.0))}
        low = ipo.compute_tier(stocks, bars, tier=100_000)
        assert set(low.code) == {"603001"}          # 科创板要 50 万权限
        full = ipo.compute_tier(stocks, bars, tier=500_000)
        assert set(full.code) == {"688001", "603001"}

    def test_expected_profit_handcalc(self):
        stocks = self._stocks()
        bars = {"688001": _bars(("2024-06-12", 30.0, 31.0, 29.0, 30.5)),
                "603001": _bars(("2024-06-12", 25.0, 27.0, 24.0, 26.0))}
        rows = ipo.compute_tier(stocks, bars, tier=1_000_000).set_index("code")
        r = rows.loc["688001"]
        assert r.sub_shares == 10_000                    # min(10万, 2万, 1万)
        assert r.exp_shares == pytest.approx(10_000 * 0.05 / 100)
        cost = 30.0 * ipo.sell_cost_rate(date(2024, 6, 22))
        assert r.exp_profit == pytest.approx(r.exp_shares * (30.0 - 10.0 - cost))

    def test_break_issue_keeps_negative_profit(self):
        stocks = self._stocks()
        bars = {"688001": _bars(("2024-06-12", 9.0, 9.5, 8.5, 8.8)),
                "603001": _bars(("2024-06-12", 25.0, 27.0, 24.0, 26.0))}
        rows = ipo.compute_tier(stocks, bars, tier=500_000)
        assert rows.set_index("code").loc["688001", "exp_profit"] < 0

    def test_missing_quote_is_dropped(self):
        stocks = self._stocks()
        rows = ipo.compute_tier(
            stocks, {"603001": _bars(("2024-06-12", 25.0, 27.0, 24.0, 26.0))},
            tier=500_000)
        assert set(rows.code) == {"603001"}

    def test_sz_sensitivity_uses_sz_market_value(self):
        raw = pd.DataFrame([_row("301001.SZ", name="创业示例",
                                 issue_date="20240619")])
        stocks = ipo.normalize_new_share(raw)
        bars = {"301001": _bars(("2024-06-19", 50.0, 52.0, 48.0, 51.0))}
        assert len(ipo.compute_tier(stocks, bars, tier=500_000, sz_mv=0.0)) == 0
        got = ipo.compute_tier(stocks, bars, tier=500_000, sz_mv=500_000)
        assert len(got) == 1 and got.iloc[0].sub_shares > 0


class TestNormalize:

    def test_exclusions_counted(self):
        raw = pd.DataFrame([
            _row("601728.SH"),
            _row("920229.BJ"),
            _row("603999.SH", name="ST示例"),
            _row("603998.SH", price=None),
            _row("603997.SH", ballot=None),
            _row("603996.SH", issue_date=""),
        ])
        counts = ipo.excluded_counts(raw)
        assert counts["new_share_rows"] == 6
        assert counts["excluded_bj_or_unknown_board"] == 1
        assert counts["excluded_st_or_delist"] == 1
        assert counts["excluded_missing_price"] == 1
        assert counts["excluded_missing_ballot"] == 1
        assert counts["excluded_not_listed_yet"] == 1
        assert len(ipo.normalize_new_share(raw)) == 1

    def test_empty_input(self):
        assert len(ipo.normalize_new_share(pd.DataFrame())) == 0
        assert ipo.excluded_counts(pd.DataFrame()) == {}


# ---------------------------------------------------------------- 行情来源
class TestFirstDaySources:

    def test_local_db_dates_are_iso_matched(self, tmp_path):
        db = tmp_path / "market.db"
        _mk_market_db(db, [
            ("603001", "2024-06-12", 25.0, 27.0, 24.0, 26.0, 1e6, 1e7),
            ("603001", "2024-06-13", 26.5, 28.0, 26.0, 27.5, 1e6, 1e7),
            # 2021-09-13 之前上市:本地库第一条不是上市首日 → 不应被当成首日
            ("601728", "2021-09-13", 5.0, 5.1, 4.9, 5.0, 1e6, 1e7),
        ])
        bars = ipo.load_local_first_days(
            str(db), {"603001": "20240612", "601728": "20210820"})
        assert [b["date"] for b in bars["603001"]] == ["2024-06-12", "2024-06-13"]
        assert "601728" not in bars          # 缺口留给回填/标注,不猜

    def test_roster_cross_check_counts_mismatches(self, tmp_path):
        db = tmp_path / "market.db"
        _mk_market_db(db, [])
        _with_stocks(db, [("603001", "SH", "2024-06-12"),
                          ("688001", "SZ", "2024-06-11"),      # 交易所 + 上市日都错
                          ("301001", "SZ", "2024-06-19")])
        raw = pd.DataFrame([_row("603001.SH", issue_date="20240612"),
                            _row("688001.SH", issue_date="20240612"),
                            _row("002001.SZ", issue_date="20240612")])
        stocks = ipo.normalize_new_share(raw)
        chk = ipo.roster_cross_check(str(db), stocks)
        assert chk["n"] == 3
        assert chk["missing_from_stocks"] == 1        # 002001 不在册
        assert chk["exchange_mismatch"] == 1          # 688001 表里写成 SZ
        assert chk["list_date_mismatch"] == 1         # 688001 上市日差一天
        assert "stocks_table" not in chk
        # 最小库缺 stocks 表时不炸,如实标注
        bare = tmp_path / "bare.db"
        bare_con = sqlite3.connect(bare)
        bare_con.execute("create table daily_quotes (code text, date text)")
        bare_con.commit()
        bare_con.close()
        assert "stocks_table" in ipo.roster_cross_check(str(bare), stocks)

    def test_cache_roundtrip_is_idempotent(self, tmp_path):
        p = tmp_path / "cache.csv"
        first = {"688001": _bars(("2024-06-12", 30.0, 31.0, 29.0, 30.5))}
        assert ipo.append_first_day_cache(p, first) == 1
        assert ipo.append_first_day_cache(p, first) == 1     # 覆盖而不是重复
        loaded = ipo.load_first_day_cache(p)
        assert loaded["688001"][0]["open"] == 30.0
        assert len(pd.read_csv(p)) == 1
        assert ipo.load_first_day_cache(tmp_path / "none.csv") == {}

    def test_fetch_missing_first_days_filters_to_wanted_codes(self):
        pro = FakePro(daily={"20240612": [
            {"ts_code": "688001.SH", "open": 30.0, "high": 31.0, "low": 29.0,
             "close": 30.5},
            {"ts_code": "600000.SH", "open": 8.0, "high": 8.1, "low": 7.9,
             "close": 8.0},                     # 同一天别的股票 → 丢掉
        ]})
        got = ipo.fetch_missing_first_days(pro, {"688001": "20240612"})
        assert pro.daily_calls == ["20240612"]
        assert list(got) == ["688001"]
        assert got["688001"][0]["date"] == "2024-06-12"

    def test_backfill_flushes_incrementally(self, tmp_path):
        pro = FakePro(daily={
            "20240612": [{"ts_code": "688001.SH", "open": 30.0, "high": 31.0,
                          "low": 29.0, "close": 30.5}],
            "20240619": [{"ts_code": "301001.SZ", "open": 50.0, "high": 52.0,
                          "low": 48.0, "close": 51.0}],
        })
        cache = tmp_path / "backfill.csv"
        got = ipo.fetch_missing_first_days(
            pro, {"688001": "20240612", "301001": "20240619"},
            flush_path=cache, flush_every=1)
        assert set(got) == {"688001", "301001"}
        # 每个交易日都落了盘 → 进程中断也不丢已拉的行情
        assert len(pd.read_csv(cache)) == 2
        assert set(ipo.load_first_day_cache(cache)) == {"688001", "301001"}

    def test_next_day_backfill_for_44pct_capped_sample(self, tmp_path):
        """注册制前主板 44% 顶格:只有一根 bar 时要逐票补拉次日行情。"""
        raw = pd.DataFrame([_row("603001.SH", price=17.37, issue_date="20211116")])
        stocks = ipo.normalize_new_share(raw)
        one_bar = {"603001": _bars(("2021-11-16", 20.84, 25.01, 20.84, 25.01))}
        need = ipo.codes_needing_next_day(stocks, one_bar)
        assert need == {"603001": "20211116"}
        assert ipo.codes_needing_next_day(
            stocks, {"603001": _bars(("2021-11-16", 20.84, 25.01, 20.84, 25.01),
                                     ("2021-11-17", 27.51, 27.51, 27.51, 27.51))}) == {}
        assert ipo.code_to_ts_code("603001") == "603001.SH"
        assert ipo.code_to_ts_code("300750") == "300750.SZ"
        assert ipo.code_to_ts_code("920229") == "920229.BJ"
        pro = FakePro(ranges={"603001.SH": [
            {"trade_date": "20211117", "open": 27.51, "high": 27.51,
             "low": 27.51, "close": 27.51},
            {"trade_date": "20211116", "open": 20.84, "high": 25.01,
             "low": 20.84, "close": 25.01},
        ]})
        cache = tmp_path / "next.csv"
        got = ipo.fetch_next_day_bars(pro, need, flush_path=cache, flush_every=50)
        assert [b["date"] for b in got["603001"]] == ["2021-11-16", "2021-11-17"]
        assert pro.range_calls == [{"ts_code": "603001.SH",
                                    "start_date": "20211116",
                                    "end_date": "20211206"}]
        assert len(pd.read_csv(cache)) == 2


# ---------------------------------------------------------------- 分页与限速
class TestFetchNewShare:

    def test_pagination_offsets_and_pause(self):
        rows = [_row(f"60{i:04d}.SH") for i in range(7)]
        pro = FakePro(rows)
        clock = FakeClock()
        limiter = ipo.RateLimiter(min_interval=1.2, _sleep=clock.sleep,
                                  _clock=clock.clock)
        df = ipo.fetch_new_share(pro, "20180101", "20260911", page_size=3,
                                 limiter=limiter)
        assert len(df) == 7
        assert [(c["offset"], c["limit"]) for c in pro.new_share_calls] == [
            (0, 3), (3, 3), (6, 3)]
        # 3 次调用 → 2 次间隔(第 1 次不睡)
        assert clock.sleeps == pytest.approx([1.2, 1.2])
        assert all(c["limit"] <= 300 for c in pro.new_share_calls)

    def test_empty_and_short_pages_stop(self):
        pro = FakePro([_row("601728.SH")])
        df = ipo.fetch_new_share(pro, "20180101", "20260911", page_size=300)
        assert len(df) == 1 and len(pro.new_share_calls) == 1
        pro2 = FakePro([])
        assert len(ipo.fetch_new_share(pro2, "20180101", "20260911")) == 0

    def test_rate_limit_error_waits_a_full_window(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr(ipo.time, "sleep", slept.append)
        pro = FakePro([_row("601728.SH")], fail_first_n=1)
        df = ipo.fetch_new_share(pro, "20180101", "20260911", page_size=300)
        assert len(df) == 1
        assert slept == [ipo.RATE_LIMIT_WAIT]      # 分钟级限流等一个窗口

    def test_non_rate_limit_error_uses_short_backoff(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr(ipo.time, "sleep", slept.append)
        pro = FakePro([_row("601728.SH")], fail_first_n=1, fail_msg="boom")
        ipo.fetch_new_share(pro, "20180101", "20260911", page_size=300)
        assert slept == [2.0]


# ---------------------------------------------------------------- 汇总与闸门
class TestAggregation:

    def _rows(self) -> pd.DataFrame:
        return pd.DataFrame([
            {"year": 2024, "board": "SH_MAIN", "issue_date": "2024-06-12",
             "exp_profit": 300.0, "exp_lots": 0.5, "first_day_ret": 1.0},
            {"year": 2024, "board": "SH_STAR", "issue_date": "2024-09-12",
             "exp_profit": -100.0, "exp_lots": 0.2, "first_day_ret": -0.2},
            {"year": 2025, "board": "SH_STAR", "issue_date": "2025-03-12",
             "exp_profit": 500.0, "exp_lots": 0.3, "first_day_ret": 0.8},
        ])

    def test_yearly_table(self):
        t = ipo.yearly_table(self._rows(), 100_000.0)
        assert t["2024"]["n_ipos"] == 2
        assert t["2024"]["exp_profit_yuan"] == 200.0
        assert t["2024"]["enhancement"] == pytest.approx(0.002)
        assert t["2024"]["yuan_per_million"] == pytest.approx(2000.0)
        assert t["2024"]["profit_neg_total"] == -100.0
        assert t["2024"]["break_issue_rate"] == 0.5
        # 至少中一签的概率 = 1 - (1-0.5)(1-0.2)
        assert t["2024"]["prob_any_lot"] == pytest.approx(0.6)
        assert t["2025"]["enhancement"] == pytest.approx(0.005)
        assert t["2018"]["n_ipos"] == 0

    def test_sz_sensitivity_denominator_is_total_capital(self):
        raw = pd.DataFrame([_row("301001.SZ", issue_date="20240619")])
        stocks = ipo.normalize_new_share(raw)
        bars = {"301001": _bars(("2024-06-19", 50.0, 52.0, 48.0, 51.0))}
        rep = ipo.build_report(stocks, bars, tiers=(500_000.0,),
                               sz_mv_ratios=(1.0,), detail_tiers=())
        assert rep["tiers"]["500000"]["n_subscribed"] == 0   # 深市额度默认 0
        cell = rep["sz_sensitivity"]["sz_mv_eq_1x_sh"]["500000"]
        assert cell["total_capital"] == 1_000_000.0
        rows = ipo.compute_tier(stocks, bars, tier=500_000, sz_mv=500_000)
        expect = ipo.yearly_table(rows, 1_000_000.0)["2024"]["enhancement"]
        assert cell["annual"]["2024"] == pytest.approx(expect)
        assert cell["annual"]["2024"] < 0.01

    def test_evaluate_gate_needs_every_year_above_threshold(self):
        good = {y: {"enhancement": 0.01} for y in ipo.GATE_YEARS}
        assert ipo.evaluate_gate(good)["pass"] is True
        bad = {**good, "2024": {"enhancement": 0.0049}}
        assert ipo.evaluate_gate(bad)["pass"] is False
        neg = {**good, "2023": {"enhancement": -0.001}}
        assert ipo.evaluate_gate(neg)["pass"] is False
        assert ipo.evaluate_gate({"2022": {"enhancement": 0.1}})["pass"] is False

    def test_rolling_window_and_regime(self):
        rows = self._rows()
        roll = ipo.rolling_windows(rows, 100_000.0, window_days=180,
                                   step_days=30)
        assert roll["n_windows"] >= 1
        assert 0.0 <= roll["share_positive"] <= 1.0
        reg = ipo.regime_table(rows, 100_000.0)
        assert any("2023-08-28" in k for k in reg)


# ---------------------------------------------------------------- 端到端
def test_main_end_to_end_with_fake_pro(tmp_path, monkeypatch):
    """main() 全链路:fake new_share 分页 + 本地库首日 + fake daily 回填。"""
    db = tmp_path / "market.db"
    _mk_market_db(db, [
        ("603001", "2024-06-12", 25.0, 27.0, 24.0, 26.0, 1e6, 1e7),
        ("603001", "2024-06-13", 26.5, 28.0, 26.0, 27.5, 1e6, 1e7),
    ])
    _with_stocks(db, [("603001", "SH", "2024-06-12"), ("688001", "SH", "2024-06-12")])
    rows = [
        _row("603001.SH", name="主板示例", price=20.0, limit_amount=2.0,
             ballot=0.04),
        _row("688001.SH", name="科创示例", price=10.0, limit_amount=1.0,
             ballot=0.05),
        _row("920001.BJ", name="北交示例"),
    ]
    pro = FakePro(
        rows,
        daily={"20240612": [
            # 一字板 → 任务书口径要按次日开盘卖 → 触发逐票 next-day 补拉
            {"ts_code": "688001.SH", "open": 30.0, "high": 30.0, "low": 30.0,
             "close": 30.0},
            {"ts_code": "603001.SH", "open": 25.0, "high": 27.0, "low": 24.0,
             "close": 26.0},
        ]},
        ranges={"688001.SH": [
            {"trade_date": "20240613", "open": 33.0, "high": 34.0,
             "low": 32.0, "close": 33.5},
            {"trade_date": "20240612", "open": 30.0, "high": 30.0,
             "low": 30.0, "close": 30.0},
        ]})
    monkeypatch.setattr(ipo, "build_pro", lambda config_db: pro)
    out = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", [
        "ipo_yield_study.py", "--market-db", str(db),
        "--new-share-cache", str(tmp_path / "new_share.csv"),
        "--first-day-cache", str(tmp_path / "first_day.csv"),
        "--fetch-missing-first-day", "--tiers", "500000,1000000",
        "--detail-tiers", "500000", "--out", str(out),
    ])
    ipo.main()

    report = json.loads(out.read_text())
    assert report["sample"]["new_share_rows"] == 3
    assert report["sample"]["excluded_bj_or_unknown_board"] == 1
    assert report["sample"]["first_day_local_db"] == 1
    assert report["sample"]["first_day_fetched"] == 1
    assert report["sample"]["first_day_next_fetched"] == 1
    assert report["sample"]["first_day_missing"] == 0
    assert report["tiers"]["500000"]["n_subscribed"] == 2
    assert len(report["tiers"]["500000"]["per_stock"]) == 2
    assert "per_stock" not in report["tiers"]["1000000"]
    assert report["gate"]["results"] == {"500000": False, "1000000": False}
    assert report["tiers"]["500000"]["boards"] == {"SH_MAIN": 1, "SH_STAR": 1}
    # 回填只按上市日整市场拉一次,且缓存落盘可复用
    assert pro.daily_calls == ["20240612"]
    assert len(pro.range_calls) == 1
    star = report["tiers"]["500000"]["per_stock"]
    star_row = next(r for r in star if r["code"] == "688001")
    assert (star_row["sell"], star_row["sell_rule"]) == (33.0, "next_open")
    assert (tmp_path / "first_day.csv").exists()
    assert len(pd.read_csv(tmp_path / "new_share.csv")) == 3
