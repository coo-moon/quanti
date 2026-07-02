# Pending Order Lifecycle — 真实化 A 股订单语义

**Date**: 2026-06-01
**Trigger**: 用户看到决策日志里 "2026-06-01 18:49:16 卖出..." — 时间戳在收盘后 4 小时,容易误以为系统在非交易时段成交。
**Status**: in progress

## 问题

当前 PaperBroker:
1. Agent 每 4h tick 一次,不看交易时段。
2. 每个 tick 调 `execute_signal()` 立即用"最近一根 daily bar 的 close + 滑点"撮合。
3. orders 表的 `filled_at` / `created_at` 都用 `datetime.now()`,得到 18:49 这种时间戳。

结果:**成交价是真实的(昨日收盘),但时间戳是墙上时钟**。两者拼起来很误导,且和真实 QMT 实盘行为不一致(实盘非交易时段下不去单,要等次日 9:30)。

## 设计目标

把 PaperBroker 的订单生命周期改成与真实 A 股一致:

```
Signal 产生
     │
     ▼
 创建 Order(status=pending) ──┬─ T+1 / 风控 / 黑名单不通过 → status=rejected(终态)
                              │
                              ▼
                   等待"下一交易日的 bar 出现"
                              │
   ┌──────────────────────────┼──────────────────────┐
   ▼                          ▼                      ▼
 bar 出现                 N 个交易日仍未              用户/系统主动撤单
 + 风控复查通过           出现 fillable bar         (未来扩展)
   │                       │                          │
   ▼                       ▼                          ▼
 status=filled         status=cancelled            status=cancelled
 (用 bar.open + 滑点)  reason="expired"           reason="manual"
```

关键不变量:

* **所有 BUY/SELL 信号都先进 pending**,即使在交易时段也一样 — 因为我们只有日 bar,任何"立即用昨日收盘成交"都是欺骗。
* **Fill 价用次日 OPEN** (不是 close)。这模拟集合竞价场景:你今天下的单进了明天 9:25 集合竞价,9:30 撮合。OPEN 是最贴近的可观测价。
* **每次 fill 前重跑 RiskManager.check()** — 因为 pending 在队列里期间,组合可能变了(其它单成交导致仓位变化、cash 不够、行业上限触发等)。
* **同 code 同方向已有 pending → 新信号被去重**(避免一个标的同时挂 3 张同方向单,造成"成交叠加")。
* **3 个交易日未 fill 自动 cancel**(默认 `pending_ttl_trading_days=3`)。避免 pending 队列无限增长。
* **Stop-loss 卖单也走 pending**。check_stop_loss 不再立即卖,而是先 queue。这避免"凌晨突然卖出"。
* **回测引擎(BacktestEngine)不受影响**。它有自己的内置撮合循环,已经按 bar 顺序处理,语义本来就比 PaperBroker 接近真实。
* **T+1 自然满足**:今日 BUY 进 pending → 次日 OPEN 才 fill → 同日不可能再 SELL(因为 SELL 也走 pending,要等再下一天 OPEN)。

## 实现切片

### C-1 市场时段工具(本文档 + utils)

`quanti/utils/market.py`:

```python
def is_market_open(now: datetime | None = None) -> bool:
    """Beijing time, A-shares: Mon-Fri, 09:30-11:30 + 13:00-15:00."""

def next_trading_bar(provider, code: str, after_date: date) -> BarData | None:
    """Earliest available bar with date > after_date. None if not yet synced."""
```

`is_market_open` 暂时不查 trade_calendar(还是空的);只用 weekday + 时间 + 已知节假日的快速判定。后续可升级。

### C-2 PaperBroker 重构

* `execute_signal(signal)` 改为"必落 pending"。立即返回的 BrokerResult.filled=0,accepted/rejected 分类不变,但增加 `pending` 计数。
* 新增 `try_fill_pending_orders() -> dict`:
  - 扫 `orders.status='pending'` 的所有行
  - 每条:`next_trading_bar(code, after=order.created_date)`,有则尝试 fill
  - Fill 前重跑 RiskManager,失败 → status=rejected(reason 标记 "risk at fill")
  - 成功 → 写 trade,更新 portfolio,status=filled
  - `created_date > now - pending_ttl_trading_days` 且 bar 不来 → status=cancelled(expired)
* `check_stop_loss()` 调 execute_signal,自动走 pending。
* 配置项:`pending_ttl_trading_days`(默认 3),`fill_price_basis`(默认 `"open"`,可选 `"close"`)。

### C-3 Agent + decision log

* `_run_one_cycle` 第一步加 `broker.try_fill_pending_orders()`。返回 dict 含 `filled / cancelled / still_pending`,写入 cycle summary。
* 新 decision kind:
  - `order_queued` — 新 pending 单产生时(execute_signal 内部),记录 code/direction/qty 估算。
  - `order_filled_pending` — pending 成交时,记录 fill_price + bar_date + queued duration。
  - `order_expired_pending` — 3 日未成交自动 cancel 时。

### C-4 测试 + UI

测试:
1. 非交易时段(eg. 周六晚 22:00)execute_signal → orders 表新增 pending 行,trades 表无变化。
2. 模拟"次日 bar 已写入" → try_fill_pending 用 bar.open + 滑点成交。
3. Fill 时组合已变 → 风控拒绝 → status=rejected(reason 含 "risk")。
4. 同一 code 已有 pending BUY,新 BUY signal → 被去重,不产生新 pending 行。
5. T+1:BUY pending fill 后,同 bar 试 SELL 应该拒绝(SELL pending → 等下一 bar fill,自然延后)。
6. 3 个交易日未 fill → 自动 cancel。

UI:
* `/api/orders` 已返回 status,前端 orders 表给 pending 行加待成交色(amber)。
* Agent 顶部状态卡加"待成交订单"数字(查 `count where status='pending'`)。

## 不动 / 显式不做

* ❌ 实时 tick / 分钟 bar / 集合竞价撮合细节 — 仍是日级,fill 用 bar.open。
* ❌ 限价单 / 委托类型 — 全部按"市价单(模拟开盘成交)"处理。`Order.price_type` 当前固定 MARKET。
* ❌ 部分成交 — status=PARTIAL 枚举存在但不用。pending 要么全成要么全不成。
* ❌ BacktestEngine 改动。回测路径保持现状(自身已是 bar-by-bar)。

## 追记 2026-07-02: created_date = 决策数据基准日

agent `daily_run_time=23:30` 启动 LLM cycle,流水线 ~2h,订单 `created_at`
常落在次日凌晨(如 01:32)。原实现直接取 `created_at` 的墙钟日期做
`next_trading_bar` 的 `after_date`(严格大于),导致基于 07-01 收盘的信号
最早 07-03 开盘成交——比本设计(次日 OPEN、滞后 1 个交易日)多滞后一天。

修复: `quanti/utils/market.py::order_decision_date` — 凌晨(<09:25 集合竞价
撮合前)创建的订单归属上一交易日;09:25 后保持墙钟日期。无前视:当日开盘价
在 09:25 前不存在,该订单仍可参与当日竞价。TTL 同步按决策日计龄(跨午夜的
单早一天过期,与其数据实际年龄一致)。

* `pytest -q` 仍全过(168 → 170+ 新测试)。
* 重启 server 后,18:00 的 tick 不再产生 `trade` 记录,但产生 `order_queued`。
* 次日早上 BackgroundQuoteSyncer 拉到新 bar 后,下一个 tick 出现 `order_filled_pending` + 真实 trade,filled_at 落在该 tick 的时间。
* UI Agent 页面"运行模式" badge 旁能看到当前 pending 数量。
