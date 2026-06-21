# 幸存者偏差修正数据源 (TushareAdapter v1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给回测补上退市股、消除幸存者偏差——加 `delist_date` 列 + `TushareAdapter`（拉含退市股的全量名册与历史日线）+ point-in-time 宇宙 helper + 回测入口开关。

**Architecture:** `stocks` 表新增可空 `delist_date` 列（null=在市）。`TushareAdapter` 仿 `XtdataAdapter`/`AkShareAdapter`，从 Tushare 免费/低积分档拉名册（含退市，带退市日）与 qfq 历史日线，灌进**同一** `save_daily_quotes` + `upsert_stock` 出口。`db.point_in_time_universe(start, end)` 用 `list_date`/`delist_date` 的 ISO 字符串字典序比较，返回窗口内存活过的全部代码（含中途退市股）。CLI/API 回测入口加 `survivorship_free` 开关把 `codes` 换成 PIT 宇宙；默认关，现状不变。

**Tech Stack:** Python 3.11+, SQLite (sqlite3), pandas, tushare（可选依赖，守卫导入），argparse CLI, FastAPI, pytest。

## Global Constraints

- `TUSHARE_TOKEN` 只从环境变量注入，**绝不打印/记录**到日志（错误信息不得含 token 值）。
- `tushare` 为**可选依赖**：模块顶端守卫 `try: import tushare as ts / except ImportError: ts = None`；未装/无 token 时适配器方法清晰 `raise`，但模块本身可被导入、其余模块/测试不受影响。
- `list_date` / `delist_date` 一律以 ISO `YYYY-MM-DD` 字符串落库（与 `daily_quotes.date` 列一致），PIT 查询才能用字典序比较。
- `delist_date` 可空：`null` = 仍在市；非空 = 退市日。
- 退市股 bar 的 `turnover`（换手率）置 `0`（免费档 `daily_basic` 需 ~2000 积分，拿不到）。
- `survivorship_free` 默认 `False` → 回测现状完全不变。
- 测试用项目 venv：`.venv/Scripts/python.exe -m pytest -q` 与 `.venv/Scripts/python.exe -m ruff check`（系统 python 无 pytest/ruff）。
- 所有网络调用（`stock_basic`/`pro_bar`）在测试中通过注入的 fake 替身完成，**测试绝不联网**。

---

### Task 1: `stocks.delist_date` 列 + 迁移 + 模型 + DB 访问器

新增可空 `delist_date` 列，让它在 `upsert_stock` / `get_stock` / `list_stocks` / `StockInfo` 间往返，并为旧库提供幂等迁移。关键决策：`upsert_stock` 的 ON CONFLICT 用 `COALESCE` 保留已存在的 `delist_date`——这样后续 AkShare 同步（不传 delist_date）不会把 Tushare 写入的退市日清空。

**Files:**
- Modify: `quanti/models.py:27-40`（`StockInfo` dataclass 加字段）
- Modify: `quanti/data/database.py`（`_create_tables` 的 `stocks` DDL、`_migrate` 的 `adds` 列表、`upsert_stock`、`get_stock`、`list_stocks`，新增 `_safe_delist_date`）
- Test: `tests/test_survivorship_db.py`（新建）

**Interfaces:**
- Consumes: 现有 `Database(db_path)` / `.initialize()` / `.close()`；现有 `upsert_stock(code, name, exchange, list_date, industry="")` 签名。
- Produces:
  - `StockInfo(code, name, exchange, list_date, industry="", delist_date: date | None = None)`
  - `Database.upsert_stock(code, name, exchange, list_date, industry="", delist_date: date | None = None) -> None`
  - `Database.get_stock(code) -> StockInfo | None`（携带 `delist_date`）
  - `Database.list_stocks() -> list[StockInfo]`（每个携带 `delist_date`）

- [ ] **Step 1: Write the failing test**

新建 `tests/test_survivorship_db.py`：

```python
"""Tests for the stocks.delist_date column: round-trip, COALESCE-preserve, migration."""
from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from quanti.data.database import Database


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    yield d
    d.close()


def test_upsert_and_get_carries_delist_date(db):
    db.upsert_stock("000001", "平安银行", "SZ", date(1991, 4, 3), "银行")
    db.upsert_stock("600001", "退市股", "SH", date(1999, 1, 1), "",
                    delist_date=date(2020, 5, 1))

    listed = db.get_stock("000001")
    delisted = db.get_stock("600001")
    assert listed is not None and listed.delist_date is None
    assert delisted is not None and delisted.delist_date == date(2020, 5, 1)


def test_list_stocks_carries_delist_date(db):
    db.upsert_stock("600001", "退市股", "SH", date(1999, 1, 1), "",
                    delist_date=date(2020, 5, 1))
    by_code = {s.code: s for s in db.list_stocks()}
    assert by_code["600001"].delist_date == date(2020, 5, 1)


def test_upsert_without_delist_date_preserves_existing(db):
    # Tushare sets the delist_date; a later AkShare upsert (no delist_date)
    # must NOT wipe it back to NULL — that's the COALESCE guard.
    db.upsert_stock("600001", "退市股", "SH", date(1999, 1, 1), "",
                    delist_date=date(2020, 5, 1))
    db.upsert_stock("600001", "退市股改名", "SH", date(1999, 1, 1), "综合")
    s = db.get_stock("600001")
    assert s.name == "退市股改名"
    assert s.delist_date == date(2020, 5, 1)  # preserved, not nulled


def test_legacy_db_migrates_delist_date(tmp_path):
    # A DB created before delist_date existed: stocks table without the column.
    path = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE stocks (code TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "exchange TEXT NOT NULL, list_date TEXT NOT NULL, industry TEXT DEFAULT '')"
    )
    raw.execute("INSERT INTO stocks VALUES ('000001','平安银行','SZ','1991-04-03','银行')")
    raw.commit()
    raw.close()

    d = Database(path)
    d.initialize()  # _migrate must ADD COLUMN delist_date
    try:
        cols = [r[1] for r in d.conn.execute("PRAGMA table_info(stocks)").fetchall()]
        assert "delist_date" in cols
        s = d.get_stock("000001")
        assert s is not None and s.delist_date is None  # legacy row reads as listed
    finally:
        d.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_survivorship_db.py -q`
Expected: FAIL — `TypeError: upsert_stock() got an unexpected keyword argument 'delist_date'` (and `StockInfo` has no `delist_date`).

- [ ] **Step 3: Add `delist_date` to `StockInfo`**

In `quanti/models.py`, modify the `StockInfo` dataclass (currently lines 27-40) to add the field after `industry`:

```python
@dataclass(frozen=True)
class StockInfo:
    """Basic stock information."""

    code: str  # e.g. "000001"
    name: str
    exchange: str  # "SZ" or "SH"
    list_date: date
    industry: str = ""
    delist_date: date | None = None  # None = still listed; set = delisting date

    @property
    def symbol(self) -> str:
        """Full symbol like '000001.SZ'."""
        return f"{self.code}.{self.exchange}"
```

- [ ] **Step 4: Add `delist_date` to the `stocks` DDL**

In `quanti/data/database.py`, in `_create_tables`, change the `{m}stocks` CREATE TABLE (currently lines 208-214) to:

```python
            CREATE TABLE IF NOT EXISTS {m}stocks (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                exchange TEXT NOT NULL,
                list_date TEXT NOT NULL,
                industry TEXT DEFAULT '',
                delist_date TEXT
            );
```

- [ ] **Step 5: Add the migration for legacy DBs**

In `quanti/data/database.py`, `_migrate` (around lines 174-187) has an `adds` list. Append the `stocks.delist_date` entry. `PRAGMA table_info` / `ALTER TABLE` with the unqualified name resolves to the attached `market` schema in attach mode (only place `stocks` lives) and to the main DB in single-file mode — same resolution the rest of the codebase relies on:

```python
        adds = [
            ("positions", "entry_strategy", "TEXT DEFAULT ''"),
            ("orders", "entry_strategy", "TEXT DEFAULT ''"),
            ("stocks", "delist_date", "TEXT"),
        ]
```

- [ ] **Step 6: Add `_safe_delist_date` parser**

In `quanti/data/database.py`, right after the existing `_safe_list_date` staticmethod (ends ~line 427), add:

```python
    @staticmethod
    def _safe_delist_date(v) -> date | None:
        """Parse a stored delist_date. NULL / empty / garbage → None (treated as
        still listed). Mirrors _safe_list_date's defensiveness but defaults to
        None rather than an epoch date."""
        if v is None:
            return None
        s = str(v).strip()
        if not s or s.lower() in ("none", "nan"):
            return None
        try:
            return date.fromisoformat(s[:10])
        except (ValueError, TypeError):
            return None
```

- [ ] **Step 7: Write `delist_date` in `upsert_stock` (with COALESCE preserve)**

In `quanti/data/database.py`, replace `upsert_stock` (currently lines 389-409) with:

```python
    def upsert_stock(
        self,
        code: str,
        name: str,
        exchange: str,
        list_date: date,
        industry: str = "",
        delist_date: date | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO stocks (code, name, exchange, list_date, industry, delist_date)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name=excluded.name,
                exchange=excluded.exchange,
                list_date=excluded.list_date,
                industry=excluded.industry,
                delist_date=COALESCE(excluded.delist_date, stocks.delist_date)
            """,
            (
                code,
                name,
                exchange,
                list_date.isoformat(),
                industry,
                delist_date.isoformat() if delist_date else None,
            ),
        )
        self.conn.commit()
```

- [ ] **Step 8: Read `delist_date` in `get_stock` and `list_stocks`**

In `quanti/data/database.py`, replace `get_stock` (lines 429-442) and `list_stocks` (lines 444-457) so the SELECT includes `delist_date` and it's parsed into `StockInfo`:

```python
    def get_stock(self, code: str) -> StockInfo | None:
        row = self.conn.execute(
            "SELECT code, name, exchange, list_date, industry, delist_date "
            "FROM stocks WHERE code=?",
            (code,),
        ).fetchone()
        if row is None:
            return None
        return StockInfo(
            code=row[0],
            name=row[1],
            exchange=row[2],
            list_date=self._safe_list_date(row[3]),
            industry=row[4],
            delist_date=self._safe_delist_date(row[5]),
        )

    def list_stocks(self) -> list[StockInfo]:
        rows = self.conn.execute(
            "SELECT code, name, exchange, list_date, industry, delist_date "
            "FROM stocks ORDER BY code"
        ).fetchall()
        return [
            StockInfo(
                code=r[0],
                name=r[1],
                exchange=r[2],
                list_date=self._safe_list_date(r[3]),
                industry=r[4],
                delist_date=self._safe_delist_date(r[5]),
            )
            for r in rows
        ]
```

- [ ] **Step 9: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_survivorship_db.py -q`
Expected: PASS (4 tests).

- [ ] **Step 10: Regression — existing DB / models tests still green**

Run: `.venv/Scripts/python.exe -m pytest tests/test_data_storage.py tests/test_models.py tests/test_list_date_robust.py -q`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add quanti/models.py quanti/data/database.py tests/test_survivorship_db.py
git commit -m "feat(data): stocks.delist_date column + migration + StockInfo/upsert round-trip"
```

---

### Task 2: `db.point_in_time_universe(start, end)` helper

返回在 `[start, end]` 窗口内存活过的所有代码（含窗口中途退市的票），用 ISO 字符串字典序比较实现时点正确性。

**Files:**
- Modify: `quanti/data/database.py`（在 `list_stocks` 之后新增方法）
- Test: `tests/test_survivorship_db.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `stocks.delist_date` 列与 ISO 存储约定。
- Produces: `Database.point_in_time_universe(start: date, end: date) -> list[str]`（按 code 升序的代码列表）。

- [ ] **Step 1: Write the failing test**

追加到 `tests/test_survivorship_db.py`：

```python
def test_point_in_time_universe(db):
    # listed before window, never delisted → included
    db.upsert_stock("000001", "在市", "SZ", date(2010, 1, 1), "")
    # delisted mid-window → included (it traded during the window)
    db.upsert_stock("600001", "中途退市", "SH", date(2005, 1, 1), "",
                    delist_date=date(2022, 6, 1))
    # delisted BEFORE window start → excluded
    db.upsert_stock("600002", "早已退市", "SH", date(2000, 1, 1), "",
                    delist_date=date(2019, 1, 1))
    # listed AFTER window end → excluded
    db.upsert_stock("301001", "窗后上市", "SZ", date(2023, 1, 1), "")
    # listed mid-window, still listed → included (existed for part of window)
    db.upsert_stock("000002", "窗中上市", "SZ", date(2021, 6, 1), "")

    universe = db.point_in_time_universe(date(2021, 1, 1), date(2022, 12, 31))
    assert universe == ["000001", "000002", "600001"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_survivorship_db.py::test_point_in_time_universe -q`
Expected: FAIL — `AttributeError: 'Database' object has no attribute 'point_in_time_universe'`.

- [ ] **Step 3: Implement `point_in_time_universe`**

In `quanti/data/database.py`, add immediately after `list_stocks`:

```python
    def point_in_time_universe(self, start: date, end: date) -> list[str]:
        """Codes that were alive at some point within [start, end] — the
        survivorship-bias-free backtest universe.

        A stock qualifies if it had listed on/before the window end AND had not
        yet delisted at the window start (delist_date NULL = still listed).
        list_date/delist_date are stored as ISO YYYY-MM-DD, so lexicographic
        string comparison equals date comparison.
        """
        rows = self.conn.execute(
            """
            SELECT code FROM stocks
            WHERE list_date <= ?
              AND (delist_date IS NULL OR delist_date >= ?)
            ORDER BY code
            """,
            (end.isoformat(), start.isoformat()),
        ).fetchall()
        return [r[0] for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_survivorship_db.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add quanti/data/database.py tests/test_survivorship_db.py
git commit -m "feat(data): db.point_in_time_universe — survivorship-free backtest universe"
```

---

### Task 3: `TushareAdapter`（含退市名册 + qfq 历史 + 可选依赖）

新建 `quanti/data/tushare_adapter.py`，仿 `XtdataAdapter` 的注入式构造（便于无网络测试）。`pro`（提供 `stock_basic`）与 `pro_bar`（取日线）两个注入缝；全注入时完全不碰 tushare 包。

**Files:**
- Create: `quanti/data/tushare_adapter.py`
- Modify: `pyproject.toml:28-40`（`[project.optional-dependencies]` 加 `data`）
- Test: `tests/test_tushare_adapter.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `db.upsert_stock(..., delist_date=…)`；现有 `db.save_daily_quotes(df)`、`db.get_latest_quote_date(code)`。
- Produces:
  - `TushareAdapter(db, token: str | None = None, *, pro=None, pro_bar=None)`
  - `.sync_stock_list() -> int`（合并 L/D/P 状态，含退市股 + delist_date 落库）
  - `.sync_daily_quotes(code, start: date | None = None, end: date | None = None) -> int`
  - staticmethods `_code_to_ts_code(code) -> str`、`_ts_code_to_code(ts_code) -> tuple[str, str]`、`_parse_ts_date(v) -> date | None`

- [ ] **Step 1: Write the failing test**

新建 `tests/test_tushare_adapter.py`。用 fake `pro`（带 `stock_basic`）与 fake `pro_bar`，全程无网络：

```python
"""Tests for TushareAdapter using injected fakes — never touches the network."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from quanti.data.database import Database
from quanti.data.tushare_adapter import TushareAdapter


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    yield d
    d.close()


class FakePro:
    """Stand-in for tushare's pro_api object (provides stock_basic)."""

    def stock_basic(self, list_status, fields):
        if list_status == "L":
            return pd.DataFrame([
                {"ts_code": "000001.SZ", "name": "平安银行",
                 "list_date": "19910403", "delist_date": None},
            ])
        if list_status == "D":
            return pd.DataFrame([
                {"ts_code": "600001.SH", "name": "邯郸钢铁",
                 "list_date": "19980122", "delist_date": "20100824"},
            ])
        return pd.DataFrame(columns=["ts_code", "name", "list_date", "delist_date"])


def _fake_pro_bar(ts_code, asset, adj, start_date, end_date):
    # tushare returns newest-first; columns trade_date/open/high/low/close/vol/amount
    return pd.DataFrame([
        {"ts_code": ts_code, "trade_date": "20100120", "open": 3.0, "high": 3.2,
         "low": 2.9, "close": 3.1, "vol": 1000.0, "amount": 3_100_000.0},
        {"ts_code": ts_code, "trade_date": "20100119", "open": 3.1, "high": 3.3,
         "low": 3.0, "close": 3.0, "vol": 1200.0, "amount": 3_600_000.0},
    ])


def test_code_ts_code_mapping():
    assert TushareAdapter._code_to_ts_code("600519") == "600519.SH"
    assert TushareAdapter._code_to_ts_code("000001") == "000001.SZ"
    assert TushareAdapter._code_to_ts_code("830799") == "830799.BJ"
    assert TushareAdapter._ts_code_to_code("600519.SH") == ("600519", "SH")
    assert TushareAdapter._ts_code_to_code("000001.SZ") == ("000001", "SZ")
    assert TushareAdapter._ts_code_to_code("830799.BJ") == ("830799", "BJ")


def test_sync_stock_list_includes_delisted(db):
    adapter = TushareAdapter(db, pro=FakePro())
    n = adapter.sync_stock_list()
    assert n == 2
    listed = db.get_stock("000001")
    delisted = db.get_stock("600001")
    assert listed is not None and listed.delist_date is None
    assert delisted is not None and delisted.delist_date == date(2010, 8, 24)
    assert delisted.exchange == "SH"


def test_sync_daily_quotes_lands_with_zero_turnover(db):
    db.upsert_stock("600001", "邯郸钢铁", "SH", date(1998, 1, 22), "",
                    delist_date=date(2010, 8, 24))
    adapter = TushareAdapter(db, pro=FakePro(), pro_bar=_fake_pro_bar)
    saved = adapter.sync_daily_quotes("600001", start=date(2010, 1, 1),
                                      end=date(2010, 1, 31))
    assert saved == 2
    out = db.get_daily_quotes("600001", date(2010, 1, 1), date(2010, 1, 31))
    assert len(out) == 2
    assert (out["close"] > 0).all()
    assert (out["turnover"] == 0).all()  # free tier has no turnover


def test_methods_raise_clearly_without_token(db, monkeypatch):
    # No pro injected, no TUSHARE_TOKEN → clear error, no token leak.
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    import quanti.data.tushare_adapter as mod
    monkeypatch.setattr(mod, "ts", None)  # simulate tushare not installed
    adapter = TushareAdapter(db)
    with pytest.raises(RuntimeError):
        adapter.sync_stock_list()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tushare_adapter.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'quanti.data.tushare_adapter'`.

- [ ] **Step 3: Create `quanti/data/tushare_adapter.py`**

```python
"""Tushare data adapter — A-share roster (incl. delisted) + qfq daily bars.

Closes survivorship bias for backtests: Tushare's free/low-tier `stock_basic`
returns delisted names with their delist_date, and `pro_bar` returns the full
price history of a delisted ts_code up to its delisting day. Both land through
the SAME db.upsert_stock / db.save_daily_quotes exits as AkShare/xtdata, so the
rest of the system reads SQLite unchanged.

tushare is an OPTIONAL dependency (guarded import). Without the package or a
TUSHARE_TOKEN, the adapter still imports; its methods raise a clear error.
Token is read from the TUSHARE_TOKEN env var and is never logged.

# VERIFY (real token / real box): pro_bar history depth for delisted ts_codes,
# adj='qfq' availability for delisted names, and current per-endpoint point
# thresholds (changes over time; see https://tushare.pro/document/1?doc_id=290).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime

import pandas as pd

try:
    import tushare as ts
except ImportError:  # pragma: no cover - exercised via monkeypatch
    ts = None

from quanti.data.database import Database

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds; free tier rate-limits per-minute call counts


class TushareAdapter:
    """Fetches A-share data (incl. delisted) from Tushare and saves to the DB."""

    def __init__(self, db: Database, token: str | None = None, *,
                 pro=None, pro_bar=None) -> None:
        self._db = db
        self._token = token
        self._pro = pro            # injected in tests; lazily built otherwise
        self._pro_bar_fn = pro_bar  # injected in tests; ts.pro_bar otherwise

    # --- ts_code <-> code mapping ---

    @staticmethod
    def _code_to_ts_code(code: str) -> str:
        if code.startswith("6"):
            return f"{code}.SH"
        if code.startswith(("4", "8")):
            return f"{code}.BJ"
        return f"{code}.SZ"

    @staticmethod
    def _ts_code_to_code(ts_code: str) -> tuple[str, str]:
        code, _, suffix = ts_code.partition(".")
        suffix = suffix.upper()
        exchange = suffix if suffix in ("SH", "SZ", "BJ") else (
            "SH" if code.startswith("6") else "SZ"
        )
        return code, exchange

    @staticmethod
    def _parse_ts_date(v) -> date | None:
        """Tushare gives 'YYYYMMDD' strings or None/NaN/'' → date | None."""
        if v is None:
            return None
        s = str(v).strip()
        if not s or s.lower() == "nan" or len(s) < 8:
            return None
        try:
            return datetime.strptime(s[:8], "%Y%m%d").date()
        except ValueError:
            return None

    # --- lazy network seams (never invoked when fully injected) ---

    def _ensure_pro(self):
        if self._pro is None:
            if ts is None:
                raise RuntimeError(
                    "tushare not installed; run: pip install 'quanti[data]'")
            token = self._token or os.environ.get("TUSHARE_TOKEN")
            if not token:
                raise RuntimeError("TUSHARE_TOKEN not set")
            ts.set_token(token)  # so module-level ts.pro_bar works too
            self._pro = ts.pro_api(token)
        return self._pro

    def _bar_fn(self):
        if self._pro_bar_fn is not None:
            return self._pro_bar_fn
        self._ensure_pro()  # ensures ts present + token registered
        return ts.pro_bar

    @staticmethod
    def _retry(fn, *args, **kwargs):
        last_err: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 - upstream/rate-limit transient
                last_err = e
                logger.warning("tushare call attempt %d/%d failed: %s",
                               attempt, MAX_RETRIES, e)
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
        if last_err is not None:
            raise last_err
        return None

    # --- public API ---

    def sync_stock_list(self) -> int:
        """Fetch L (listed) + D (delisted) + P (paused) rosters and upsert each,
        carrying delist_date for the delisted ones. Returns count saved."""
        pro = self._ensure_pro()
        count = 0
        for status in ("L", "D", "P"):
            df = self._retry(
                pro.stock_basic, list_status=status,
                fields="ts_code,name,list_date,delist_date")
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                code, exchange = self._ts_code_to_code(str(row["ts_code"]))
                list_date = self._parse_ts_date(row.get("list_date"))
                if list_date is None:
                    continue  # list_date is NOT NULL in schema; skip junk rows
                delist_date = self._parse_ts_date(row.get("delist_date"))
                try:
                    self._db.upsert_stock(
                        code, str(row["name"]), exchange, list_date,
                        industry="", delist_date=delist_date)
                    count += 1
                except Exception as e:  # noqa: BLE001 - one bad row shouldn't abort
                    logger.warning("save %s failed: %s", code, e)
        return count

    def sync_daily_quotes(self, code: str, start: date | None = None,
                          end: date | None = None) -> int:
        """Fetch qfq daily bars for `code` (incremental from the last stored bar
        by default) and save them. turnover is set to 0 (free tier lacks it).
        Returns rows saved."""
        if end is None:
            end = date.today()
        if start is None:
            latest = self._db.get_latest_quote_date(code)
            start = latest if latest else date(2010, 1, 1)

        bar = self._bar_fn()
        ts_code = self._code_to_ts_code(code)
        raw = self._retry(
            bar, ts_code=ts_code, asset="E", adj="qfq",
            start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
        if raw is None or raw.empty:
            return 0

        df = pd.DataFrame({
            "code": code,
            "date": pd.to_datetime(raw["trade_date"]).dt.date,
            "open": raw["open"].astype(float),
            "high": raw["high"].astype(float),
            "low": raw["low"].astype(float),
            "close": raw["close"].astype(float),
            "volume": raw["vol"].astype(float),
            "amount": raw["amount"].astype(float),
            "turnover": 0.0,
        })
        saved = self._db.save_daily_quotes(df)
        logger.info("%s: %d bars [%s~%s] via tushare", code, saved,
                    df["date"].min(), df["date"].max())
        return saved
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tushare_adapter.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Add `data` optional dependency**

In `pyproject.toml`, under `[project.optional-dependencies]`, add a `data` extra after the `llm` block (lines 38-40):

```toml
# Install via `pip install -e '.[data]'` to enable the Tushare adapter
# (delisted-stock roster + history → survivorship-free backtests). Without it,
# TushareAdapter still imports but its methods raise a clear error.
data = [
    "tushare>=1.2",
]
```

- [ ] **Step 6: Verify the module imports even without tushare installed**

Run: `.venv/Scripts/python.exe -c "import quanti.data.tushare_adapter as m; print('ts is', m.ts is not None)"`
Expected: prints `ts is False` (tushare not installed in venv) — the guarded import worked and the module loaded.

- [ ] **Step 7: Commit**

```bash
git add quanti/data/tushare_adapter.py tests/test_tushare_adapter.py pyproject.toml
git commit -m "feat(data): TushareAdapter — delisted roster + qfq history, optional dep"
```

---

### Task 4: CLI 同步入口 `quanti sync --tushare-stocks / --tushare-quotes [--delisted-only]`

把 Tushare 名册/历史同步接到现有 `quanti sync`。

**Files:**
- Modify: `quanti/cli.py`（`cmd_sync` 函数体；`sync_parser` 参数定义 ~line 304-308）
- Test: `tests/test_cli_survivorship.py`（新建）

**Interfaces:**
- Consumes: Task 3 的 `TushareAdapter(db).sync_stock_list()` / `.sync_daily_quotes(code)`；Task 1 的 `db.list_stocks()`（每个带 `delist_date`）。
- Produces: `cmd_sync(args)` 处理 `args.tushare_stocks` / `args.tushare_quotes` / `args.delisted_only`。

- [ ] **Step 1: Write the failing test**

新建 `tests/test_cli_survivorship.py`：

```python
"""Tests for CLI tushare sync flags (cmd_sync)."""
from __future__ import annotations

import types
from datetime import date

import quanti.cli as cli


def test_cmd_sync_tushare_quotes_delisted_only(tmp_path, monkeypatch):
    from quanti.data.database import Database

    dbp = str(tmp_path / "paper.db")
    seed = Database(dbp)
    seed.initialize()
    seed.upsert_stock("000001", "在市", "SZ", date(2010, 1, 1), "")
    seed.upsert_stock("600001", "退市", "SH", date(2000, 1, 1), "",
                      delist_date=date(2019, 1, 1))
    seed.close()

    def _make_db():
        d = Database(dbp)
        d.initialize()
        return d

    monkeypatch.setattr(cli, "_open_db", _make_db)

    synced: list[str] = []

    class FakeTushareAdapter:
        def __init__(self, db):
            self._db = db

        def sync_stock_list(self):
            return 0

        def sync_daily_quotes(self, code, start=None, end=None):
            synced.append(code)
            return 1

    import quanti.data.tushare_adapter as mod
    monkeypatch.setattr(mod, "TushareAdapter", FakeTushareAdapter)

    args = types.SimpleNamespace(
        calendar=False, stocks=False, quotes=False, codes=None,
        tushare_stocks=False, tushare_quotes=True, delisted_only=True,
    )
    cli.cmd_sync(args)
    assert synced == ["600001"]  # only the delisted stock


def test_cmd_sync_tushare_stocks(tmp_path, monkeypatch):
    from quanti.data.database import Database

    dbp = str(tmp_path / "paper.db")
    Database(dbp).initialize()

    monkeypatch.setattr(cli, "_open_db",
                        lambda: _init(Database(dbp)))

    called = {"n": 0}

    class FakeTushareAdapter:
        def __init__(self, db):
            pass

        def sync_stock_list(self):
            called["n"] += 1
            return 5

    import quanti.data.tushare_adapter as mod
    monkeypatch.setattr(mod, "TushareAdapter", FakeTushareAdapter)

    args = types.SimpleNamespace(
        calendar=False, stocks=False, quotes=False, codes=None,
        tushare_stocks=True, tushare_quotes=False, delisted_only=False,
    )
    cli.cmd_sync(args)
    assert called["n"] == 1


def _init(d):
    d.initialize()
    return d
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cli_survivorship.py -q`
Expected: FAIL — `cmd_sync` references `args.tushare_quotes` but doesn't act on it (the `synced`/`called` asserts fail).

- [ ] **Step 3: Add the tushare branches to `cmd_sync`**

In `quanti/cli.py`, `cmd_sync` (currently lines 25-52) — before the final `db.close()`, add:

```python
    if getattr(args, "tushare_stocks", False):
        from quanti.data.tushare_adapter import TushareAdapter
        logger.info("Syncing Tushare stock list (incl. delisted)...")
        n = TushareAdapter(db).sync_stock_list()
        logger.info(f"Synced {n} stocks from Tushare")

    if getattr(args, "tushare_quotes", False):
        from quanti.data.tushare_adapter import TushareAdapter
        adapter = TushareAdapter(db)
        stocks = db.list_stocks()
        if getattr(args, "delisted_only", False):
            stocks = [s for s in stocks if s.delist_date is not None]
        codes = [s.code for s in stocks]
        logger.info(f"Syncing Tushare quotes for {len(codes)} stocks...")
        for i, code in enumerate(codes):
            try:
                adapter.sync_daily_quotes(code)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"  {code}: {e}")
            if (i + 1) % 50 == 0:
                logger.info(f"  Progress: {i + 1}/{len(codes)}")
        logger.info("Tushare quote sync complete")
```

- [ ] **Step 4: Register the CLI flags**

In `quanti/cli.py`, after the existing `sync_parser.add_argument("--codes", type=str)` (line 308), add:

```python
    sync_parser.add_argument("--tushare-stocks", action="store_true",
                             dest="tushare_stocks",
                             help="Sync full roster incl. delisted via Tushare")
    sync_parser.add_argument("--tushare-quotes", action="store_true",
                             dest="tushare_quotes",
                             help="Sync daily history via Tushare")
    sync_parser.add_argument("--delisted-only", action="store_true",
                             dest="delisted_only",
                             help="With --tushare-quotes: only delisted stocks")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cli_survivorship.py -q`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add quanti/cli.py tests/test_cli_survivorship.py
git commit -m "feat(cli): quanti sync --tushare-stocks/--tushare-quotes [--delisted-only]"
```

---

### Task 5: 回测入口 survivorship-free 开关（CLI + API）

CLI `quanti backtest` 与 API `POST /api/backtest/run` 各加一个 `survivorship_free` 开关 + `max_universe` 上限：置位时把 `codes` 换成 PIT 宇宙（封顶）。默认关 → 现状不变。回测引擎本身零改动。

**Files:**
- Modify: `quanti/cli.py`（`cmd_backtest` 函数体 line 55-100；`bt_parser` 定义 line 311-316）
- Modify: `quanti/api/routes.py`（`BacktestRequest` line 24-31；`run_backtest` line 892-949）
- Test: `tests/test_cli_survivorship.py`（追加）、`tests/test_api_survivorship.py`（新建）

**Interfaces:**
- Consumes: Task 2 的 `db.point_in_time_universe(start, end) -> list[str]`。
- Produces:
  - CLI: `--survivorship-free`（`dest="survivorship_free"`）、`--max-universe`（`dest="max_universe"`, int, default 300）；`--codes` 不再 required。
  - API: `BacktestRequest.survivorship_free: bool = False`、`BacktestRequest.max_universe: int = 300`。

- [ ] **Step 1: Write the failing CLI test**

追加到 `tests/test_cli_survivorship.py`：

```python
def test_cmd_backtest_survivorship_free_uses_pit_universe(tmp_path, monkeypatch):
    from quanti.data.database import Database

    dbp = str(tmp_path / "paper.db")
    seed = Database(dbp)
    seed.initialize()
    seed.upsert_stock("000001", "在市", "SZ", date(2010, 1, 1), "")
    seed.upsert_stock("600001", "退市", "SH", date(2000, 1, 1), "",
                      delist_date=date(2022, 6, 1))
    seed.close()

    monkeypatch.setattr(cli, "_open_db", lambda: _init(Database(dbp)))

    captured = {}

    class FakeStrategy:
        name = "dummy"
        def init(self, params):
            pass

    class FakeLoader:
        def load_directory(self, _d):
            return [FakeStrategy()]

    class FakeResult:
        metrics = {"sharpe": 1.0}
        trades = []

    class FakeEngine:
        def __init__(self, **kw):
            pass
        def run(self, strategy, codes, start, end):
            captured["codes"] = codes
            return FakeResult()

    monkeypatch.setattr(cli, "StrategyLoader", FakeLoader, raising=False)
    import quanti.strategy.loader as loadermod
    monkeypatch.setattr(loadermod, "StrategyLoader", FakeLoader)
    import quanti.backtest.engine as enginemod
    monkeypatch.setattr(enginemod, "BacktestEngine", FakeEngine)

    args = types.SimpleNamespace(
        strategy="dummy", codes=None, start="2021-01-01", end="2022-12-31",
        cash=1_000_000, survivorship_free=True, max_universe=300,
    )
    cli.cmd_backtest(args)
    assert captured["codes"] == ["000001", "600001"]  # PIT universe, incl. delisted
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cli_survivorship.py::test_cmd_backtest_survivorship_free_uses_pit_universe -q`
Expected: FAIL — `cmd_backtest` does `args.codes.split(",")` and `args.codes` is `None` → `AttributeError`.

- [ ] **Step 3: Make `cmd_backtest` honor the switch**

In `quanti/cli.py`, in `cmd_backtest`, replace the line `codes = args.codes.split(",")` (currently line 79) with:

```python
    if getattr(args, "survivorship_free", False):
        start_d = date.fromisoformat(args.start)
        end_d = date.fromisoformat(args.end)
        all_codes = db.point_in_time_universe(start_d, end_d)
        max_u = getattr(args, "max_universe", 300)
        codes = all_codes[:max_u]
        logger.info(
            f"Survivorship-free universe: {len(all_codes)} stocks in window, "
            f"using {len(codes)} (cap {max_u})")
        if len(all_codes) > len(codes):
            logger.info(f"  dropped {len(all_codes) - len(codes)} over cap")
    else:
        if not args.codes:
            logger.error("--codes is required unless --survivorship-free is set")
            sys.exit(1)
        codes = args.codes.split(",")
```

Note: `date` is already imported at the top of `cmd_backtest` (line 57: `from datetime import date`).

- [ ] **Step 4: Register the CLI flags + relax `--codes`**

In `quanti/cli.py`, change the backtest parser block (lines 311-316). Make `--codes` optional and add the two flags:

```python
    bt_parser = subparsers.add_parser("backtest", help="Run backtest")
    bt_parser.add_argument("--strategy", required=True)
    bt_parser.add_argument("--codes", required=False, default=None)
    bt_parser.add_argument("--start", required=True)
    bt_parser.add_argument("--end", required=True)
    bt_parser.add_argument("--cash", type=float, default=1_000_000)
    bt_parser.add_argument("--survivorship-free", action="store_true",
                           dest="survivorship_free",
                           help="Backtest over the point-in-time universe "
                                "(incl. delisted) instead of --codes")
    bt_parser.add_argument("--max-universe", type=int, default=300,
                           dest="max_universe",
                           help="Cap on survivorship-free universe size")
```

- [ ] **Step 5: Run CLI test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cli_survivorship.py -q`
Expected: PASS (3 tests).

- [ ] **Step 6: Write the failing API test**

新建 `tests/test_api_survivorship.py`：

```python
"""Survivorship-free switch on POST /api/backtest/run."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
from httpx import ASGITransport, AsyncClient

from quanti.api.app import create_app
from quanti.data.database import Database
from quanti.data.provider import DataProvider


def _seed_bars(db, code, start="2021-01-04", periods=260):
    dates = pd.bdate_range(start, periods=periods)
    np.random.seed(7)
    prices = 10 + np.cumsum(np.random.randn(periods) * 0.1)
    db.save_daily_quotes(pd.DataFrame({
        "code": code,
        "date": [d.date() for d in dates],
        "open": prices - 0.1, "high": prices + 0.3, "low": prices - 0.3,
        "close": prices,
        "volume": np.random.randint(500000, 2000000, periods).astype(float),
        "amount": prices * 1_000_000, "turnover": 0.0,
    }))
    db.save_trade_calendar([d.date() for d in dates])


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    d.initialize()
    d.upsert_stock("000001", "在市", "SZ", date(2010, 1, 1), "银行")
    d.upsert_stock("600001", "退市", "SH", date(2000, 1, 1), "",
                   delist_date=date(2022, 6, 1))
    _seed_bars(d, "000001")
    _seed_bars(d, "600001")
    yield d
    d.close()


@pytest.fixture
def client(db):
    provider = DataProvider(db)
    app = create_app(db=db, provider=provider, strategies_dir="strategies")
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test"), db


@pytest.mark.asyncio
async def test_backtest_survivorship_free_consults_pit_universe(client, monkeypatch):
    ac, db = client
    captured = {}
    real = db.point_in_time_universe

    def spy(start, end):
        codes = real(start, end)
        captured["codes"] = codes
        return codes

    monkeypatch.setattr(db, "point_in_time_universe", spy)

    async with ac as c:
        # The universe substitution happens right after date parsing, BEFORE
        # strategy resolution — so the spy fires even with a bogus strategy
        # name (route then returns {"error": ...} with status 200). That keeps
        # the test independent of whatever strategies/ ships.
        r = await c.post("/api/backtest/run", json={
            "strategy_name": "does-not-exist",
            "codes": [],
            "start": "2021-06-01",
            "end": "2022-01-01",
            "survivorship_free": True,
            "max_universe": 300,
        })
    assert r.status_code == 200
    # The PIT universe for this window includes the delisted 600001.
    assert captured["codes"] == ["000001", "600001"]
```

- [ ] **Step 7: Run API test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api_survivorship.py -q`
Expected: FAIL — `BacktestRequest` rejects unknown field or ignores `survivorship_free`; `captured` never set (KeyError).

- [ ] **Step 8: Add fields to `BacktestRequest`**

In `quanti/api/routes.py`, extend `BacktestRequest` (lines 24-31):

```python
class BacktestRequest(BaseModel):
    strategy_name: str
    codes: list[str]
    start: str  # YYYY-MM-DD
    end: str
    initial_cash: float = 1_000_000.0
    params: dict = {}
    apply_risk: bool = True  # apply live exit policy (stop-loss/TP/caps)
    survivorship_free: bool = False  # backtest the point-in-time universe instead of `codes`
    max_universe: int = 300          # cap on the survivorship-free universe size
```

- [ ] **Step 9: Substitute the PIT universe in `run_backtest`**

In `quanti/api/routes.py`, in `run_backtest`, right after `start_d`/`end_d` are parsed (after line 908, the `except ValueError` block), compute the working `codes` and skip auto-sync for the survivorship path (auto-sync hits AkShare per missing code — pointless/slow for delisted; survivorship data is pre-synced via `quanti sync --tushare-*`):

```python
    if body.survivorship_free:
        codes = db.point_in_time_universe(start_d, end_d)[:body.max_universe]
        logger.info(f"Survivorship-free universe: {len(codes)} stocks "
                    f"(cap {body.max_universe})")
    else:
        codes = body.codes
        # Auto-sync: if any stock has no data, fetch it automatically
        for code in codes:
            bars = provider.get_daily_bars(code, start_d, end_d)
            if len(bars) == 0:
                logger.info(f"No data for {code}, auto-syncing...")
                try:
                    adapter = AkShareAdapter(db)
                    adapter.sync_daily_quotes(code, start=start_d, end=end_d,
                                              repair_gaps=False)
                except Exception as e:
                    logger.warning(f"Auto-sync failed for {code}: {e}")
```

Then change the `engine.run(...)` call (line 944-949) to pass the computed `codes`:

```python
    result = engine.run(
        strategy=strategy,
        codes=codes,
        start=date.fromisoformat(body.start),
        end=date.fromisoformat(body.end),
    )
```

Note: this **replaces** the existing auto-sync `for code in body.codes:` loop (lines 909-917) — fold it into the `else` branch above so it only runs for the non-survivorship path.

- [ ] **Step 10: Run API test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api_survivorship.py -q`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add quanti/cli.py quanti/api/routes.py tests/test_cli_survivorship.py tests/test_api_survivorship.py
git commit -m "feat(backtest): survivorship-free switch (CLI + API) → point-in-time universe"
```

---

### Task 6: 全量回归 + 文档

跑全量测试与 lint，更新借鉴清单状态与 README/docs。

**Files:**
- Modify: `docs/2026-06-20-reference-mature-quant-systems.md`（标记 ① 已实现）
- Modify: `README.md`（survivorship-free 用法，若 README 有 CLI/数据章节）

**Interfaces:** 无新接口。

- [ ] **Step 1: Full regression — pytest**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS（含新 `tests/test_survivorship_db.py`、`tests/test_tushare_adapter.py`、`tests/test_cli_survivorship.py`、`tests/test_api_survivorship.py`；既有套件全绿）。

- [ ] **Step 2: Lint**

Run: `.venv/Scripts/python.exe -m ruff check`
Expected: `All checks passed!`（如有 import 顺序/未用变量问题，就地修；切勿用 `# noqa` 掩盖真实问题）。

- [ ] **Step 3: Mark borrow-item ① implemented**

In `docs/2026-06-20-reference-mature-quant-systems.md`, find the ① (point-in-time / survivorship) row and mark it implemented (mirror how ②/④/⑤/⑥ were marked). Add a one-line pointer to the spec/plan:

```markdown
- ① point-in-time 数据 / 治幸存者偏差 — ✅ 已实现 (v1)：`TushareAdapter` 拉含退市股名册 + qfq 历史 → `stocks.delist_date` + `db.point_in_time_universe`；回测 `--survivorship-free`。设计见 `docs/superpowers/specs/2026-06-21-survivorship-data-design.md`，计划见 `docs/superpowers/plans/2026-06-21-survivorship-data.md`。
```

(Match the file's actual list format; the above is the content to convey.)

- [ ] **Step 4: README usage note**

If `README.md` has a data-sync / backtest section, add a short note (otherwise skip this step):

```markdown
### 无幸存者偏差回测 (survivorship-free)

```bash
# 1) 拉含退市股的全量名册 + 退市股历史（需 TUSHARE_TOKEN，可选依赖 .[data]）
export TUSHARE_TOKEN=...   # PowerShell: $env:TUSHARE_TOKEN="..."
quanti sync --tushare-stocks
quanti sync --tushare-quotes --delisted-only

# 2) 在“按日期时点正确、含退市股”的宇宙上回测
quanti backtest --strategy my_strat --start 2021-01-01 --end 2022-12-31 --survivorship-free
```
```

- [ ] **Step 5: Commit**

```bash
git add docs/2026-06-20-reference-mature-quant-systems.md README.md
git commit -m "docs: mark borrow-item ① (survivorship-free data) implemented"
```

---

## Self-Review Notes

**Spec coverage check (spec §→task):**
- §2 `TushareAdapter`（守卫导入 / token-env / sync_stock_list L·D·P / sync_daily_quotes qfq+turnover=0 / ts_code↔code / retry）→ Task 3 ✅
- §3 schema（`stocks.delist_date` + StockInfo + upsert + get/list + 迁移）→ Task 1 ✅
- §4 `point_in_time_universe` → Task 2 ✅
- §5 消费（CLI/API survivorship-free + max_universe, 默认 False, ISO 存储）→ Task 5 ✅（ISO 存储已由 Task 1 的 `list_date.isoformat()`/`delist_date.isoformat()` 保证）
- §6 同步入口（`--tushare-stocks` / `--tushare-quotes [--delisted-only]`）→ Task 4 ✅
- §7 依赖（`tushare` 可选 + 守卫导入）→ Task 3 ✅
- §8 测试（映射 / fake pro / PIT 四象限 / schema 往返 + 迁移 / CLI·API 走 PIT / 可选导入）→ Tasks 1-5 ✅
- §9 限制（turnover=0）→ Task 3 实现 + Global Constraints 记录 ✅

**Design decisions surfaced beyond the spec (intentional):**
- `upsert_stock` ON CONFLICT 用 `COALESCE(excluded.delist_date, stocks.delist_date)`：防 AkShare 后续 upsert（不传 delist_date）把 Tushare 写的退市日清空。Task 1 有专门测试。
- 迁移走现有 `_migrate` 的 `adds` 列表（PRAGMA table_info + ALTER），而非 spec §3 的独立 try/except——复用既有幂等机制，更一致。
- API survivorship-free 路径**跳过 auto-sync**（AkShare 逐只同步对退市股无意义且慢；退市数据应已由 `quanti sync --tushare-*` 预灌）。Task 5 Step 9 记录。
- `pro` 与 `pro_bar` 双注入缝：全注入时适配器完全不依赖 tushare 包，测试零联网。
