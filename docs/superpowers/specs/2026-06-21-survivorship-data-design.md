# 设计：幸存者偏差修正数据源（TushareAdapter, v1）

**日期**: 2026-06-21
**借鉴来源**: 借鉴清单 ①（point-in-time 数据 / 治幸存者偏差，Qlib/zipline+Norgate 思路）
**状态**: 已 brainstorm 收敛，待用户复核 → writing-plans

## 1. 目标与非目标

**目标**：给**回测**补上退市股,消除幸存者偏差——加一个 `TushareAdapter`,从 Tushare 免费/低积分档拉 **含退市股的全量名册(带退市日期)** 与 **退市/历史日线(qfq)** 灌进现有 `market.db`;提供一个 **point-in-time 宇宙** helper,让手动回测能在"含退市的、按日期时点正确"的宇宙上跑。

**非目标 / 明确不做（v1）**：
- 不做历史时点指数成分、as-reported 基本面 PIT（Tushare 需 ~2000 积分;免费档够不到;且当前因子用不上）。
- 不改实盘 `UniverseBuilder`（实盘本就该排除退市/ST,买不了）。
- 不接 Selector 的策略排名宇宙（route B,留紧后续——会改变选出的策略）。
- 不做全市场一次性回补（增量 + 按需）。
- 不做"退市预警强平"实盘规则（独立小功能,与本 PIT 数据无关,留后续）。

**为什么是 Tushare 而非等 QMT/xtdata**：查证确认 xtdata 接 QMT 后**也能**覆盖退市(过期板块 + `get_instrument_detail` 的 `ExpireDate` + `download_history_data` 拉退市历史),但需 QMT 真机就绪 + 给 bridge 加活;Tushare **现在就能做、不依赖 QMT**。两者产出相同、出口相同（`save_daily_quotes` + `delist_date`),将来 QMT 就绪可由 `XtdataAdapter` 接管退市同步（见 §10）。

## 2. `TushareAdapter`（`quanti/data/tushare_adapter.py`）

仿 `AkShareAdapter` 的结构(retry/限频/SyncReport 风格)。`import tushare` **守卫**（无包/无 token 不崩,清晰报错）;token 读 `TUSHARE_TOKEN` 环境变量。

- `__init__(db, token=None)`: token 缺失 → 构造时不报错,首次调用网络接口时清晰 `raise`(或返回 0 + log)。`pro = ts.pro_api(token)`。
- `sync_stock_list() -> int`: 分别 `pro.stock_basic(list_status='L'|'D'|'P', fields='ts_code,name,list_date,delist_date')`,合并 → 逐只 `db.upsert_stock(code, name, exchange, list_date, industry='', delist_date=…)`。
  - `code` = ts_code 去后缀;`exchange` 由后缀定（`.SH`→SH, `.SZ`→SZ, `.BJ`→BJ）。
  - `list_date`/`delist_date`: Tushare 给 `YYYYMMDD` 字符串 → 转 `date`;`delist_date` 空 → `None`（在市）。
- `sync_daily_quotes(code, start=None, end=None) -> int`: `ts.pro_bar(ts_code=…, asset='E', adj='qfq', start_date, end_date)` → 映射成 `save_daily_quotes` 的 df(`code,date,open,high,low,close,volume,amount,turnover`)。
  - `volume`=vol、`amount`=amount;**`turnover`(换手率)免费档(`daily_basic`)需 ~2000 积分,拿不到 → 置 0**（见 §9）。
  - `start` 缺省 = `db.get_latest_quote_date(code)` 增量;否则 `date(2010,1,1)` 起。
  - 退市股: `pro_bar` 对退市 ts_code 仍返回其历史(到退市日为止)。
- 重试/限频: 免费档每分钟调用受限 → 复用 `MAX_RETRIES`/`RETRY_DELAY` + 调用间 `sleep`;批量同步逐只增量。
- ts_code ↔ code 映射 helper（双向）。

**# VERIFY（真机/真 token 上核对，文档中标注）**：`pro_bar` 对退市股的历史深度、`adj='qfq'` 对退市股是否可用、各接口当前积分门槛(会变,以官方[积分表](https://tushare.pro/document/1?doc_id=290)为准)。

## 3. schema 改动

`stocks` 表加 `delist_date TEXT`(nullable, null=在市):
```sql
CREATE TABLE IF NOT EXISTS {m}stocks (
    code TEXT PRIMARY KEY, name TEXT NOT NULL, exchange TEXT NOT NULL,
    list_date TEXT NOT NULL, industry TEXT DEFAULT '',
    delist_date TEXT          -- 新增; null = 仍在市
);
```
**向后兼容**: 已存在的库需迁移——`_create_tables` 用 `CREATE TABLE IF NOT EXISTS`,旧库不会加列。加一个轻量迁移: `ALTER TABLE stocks ADD COLUMN delist_date TEXT`(包在 try/except,列已存在则忽略),在 `initialize()` 里调用。
- `StockInfo` 加 `delist_date: date | None = None`。
- `upsert_stock(code, name, exchange, list_date, industry='', delist_date=None)` 写入该列。
- `list_stocks` / `get_stock` 的 SELECT 带上 `delist_date` 并填进 `StockInfo`。

## 4. point-in-time 宇宙 helper

`db.point_in_time_universe(start: date, end: date) -> list[str]`:
```sql
SELECT code FROM stocks
WHERE list_date <= ?           -- 回测窗结束前已上市
  AND (delist_date IS NULL OR delist_date >= ?)   -- 窗开始时还没退市
ORDER BY code
```
（`list_date`/`delist_date` 存 ISO `YYYY-MM-DD` 字符串,字典序比较即日期序;`end`、`start` 同样 isoformat。）返回**含在窗口内存活过的退市股**的代码集——这就是无幸存者偏差的回测宇宙。

## 5. 消费（route A：手动回测接入）

回测引擎不变（本就吃 `codes`）。在**回测入口**按开关把 codes 换成 PIT 宇宙:
- **CLI** `quanti backtest` 加 `--survivorship-free` + `--max-universe`(默认 300)：置位时 `codes = db.point_in_time_universe(start, end)[:max_universe]`,并 `log` 总数与丢弃量;覆盖 `--codes`。
- **API** `/backtest/run` 请求体加可选 `survivorship_free: bool = False`（+ `max_universe: int = 300`）：置位时同样取 PIT 宇宙(同样封顶)。
- 默认 False → 现状不变。
- 注:`list_date`/`delist_date` 须以 ISO `YYYY-MM-DD` 存(与 `daily_quotes.date` 等列一致),PIT 查询才能用字典序比较;实现时确认 `upsert_stock` 的 list_date 落库格式为 ISO。

## 6. 同步入口（CLI）

`quanti sync` 扩展（或加 `tushare` 子命令）:
- `--tushare-stocks`: 跑 `TushareAdapter.sync_stock_list()`（含退市名册 → `delist_date` 落库）。
- `--tushare-quotes [--delisted-only]`: 对（退市股 / 或全量）逐只 `sync_daily_quotes` 增量灌历史。
- 复用现有 `cmd_sync` 的结构 + `_open_db()`。

## 7. 依赖

`tushare` 列为**可选依赖**（`pyproject.toml` 的 `[project.optional-dependencies]` 加 `data = ["tushare"]` 或并入现有 extra）。模块顶端守卫导入:
```python
try:
    import tushare as ts
except ImportError:
    ts = None
```
未装/无 token → 适配器方法清晰报错(或 log + 返回 0),不影响其余模块加载/测试。

## 8. 测试

- `ts_code` ↔ code 双向映射（`600519.SH`/`000001.SZ`/`8xxxxx.BJ`）。
- 注入 **fake tushare pro**（无网络）测 `sync_stock_list`：含退市股、`delist_date` 正确落库;`sync_daily_quotes`：`pro_bar` 返回 → `save_daily_quotes`(qfq、turnover=0)。
- **`point_in_time_universe`**（核心）：窗口内**中途退市**的票被纳入、窗口**结束后才上市**的排除、**窗口前已退市**的排除、在市的纳入。
- `delist_date` schema 往返（upsert/get/list）+ 旧库迁移（无列 → ALTER 加列幂等）。
- CLI/API `survivorship-free` 走 PIT 宇宙（spy/断言 codes 来自 `point_in_time_universe`）。
- 可选导入：`ts=None` 时模块可导入,适配器方法无 token/无包给清晰错误。
- 全量 pytest 绿;`ruff` 干净。

## 9. 已知 v1 限制

- **turnover 免费档缺**：Tushare 灌的退市股 bar 无换手率（置 0）→ `turnover_20d` 因子对这些票退化为 NaN,横截面 panel 里 fail-open。可接受;将来有积分/换源再补。
- 积分门槛会变,接前以官方表为准（# VERIFY）。
- 退市股**历史深度**视 Tushare 而定。
- 只治幸存者偏差;index 成分 / 基本面 PIT 不在内。

## 10. 后续 / 与 QMT 的衔接

- **QMT 就绪后**：`XtdataAdapter` 可接管退市同步——`get_sector_list()` 的"过期"板块取退市名册、`get_instrument_detail().ExpireDate/OpenDate` 取退市/上市日、`download_history_contracts`+`download_history_data` 拉退市历史,灌进**同一** `save_daily_quotes` + `delist_date` 出口。届时 Tushare 这条可作兜底或退役。（xtdata 覆盖已查证,见会话记录。）
- **route B**：让 Selector 的策略排名也用 PIT 宇宙（策略选择也无幸存者偏差）——更有价值,需为 goal 宇宙同步退市历史,且会改变选出的策略,单独评估。
- **index 成分 / 基本面 PIT**：需 Tushare ~2000 积分或专业源,按需。
- **实盘"退市预警强平"**：持仓票转 ST/退市预警时强制离场,用当前状态标志,独立小型实盘安全规则。
