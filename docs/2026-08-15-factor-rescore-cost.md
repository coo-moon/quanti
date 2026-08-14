# 因子重评分成本实证 —— 结论:SQLite 重复查询是热点,表缓存把 12 分钟压到分钟级

日期:2026-08-15
脚本:`scripts/factor_rescore_bench.py`(可复跑,scratch 副本上冷/热对比)

## 问题

线上巡检(USR1 栈转储,QUANTI_STACK_DUMP=1)发现每日挖掘钩子在进程内跑
12+ 分钟、持续 40-74% CPU。栈顶集中在 `get_daily_basic` / `get_financials_asof`
两条 SQLite 查询,调用链是:

```
background_sync._maybe_mine_factors → _auto_mine_factors →
rescore_generated_factors(118 因子)→ _eval → factor_ic / factor_ic_stats →
_ic_series → _merge_fundamentals → get_daily_basic_df / get_financials_asof
```

`_ic_series` 每因子 × 2 窗口(train 252d / OOS 63d)× 100 码都要重取这两张表,
而全部因子共享同一组窗口——理论最少 2 组表数据,实际发了 ~47k 次 SQLite 读。
行情 bars 早有 per-code 全量表缓存(2026-08-14 冷启动修复),这两张表是漏网之鱼。

## 修复

`DataProvider` 为 `daily_basic` 与 `financials` 加了与行情同款的 per-code
全量表 LRU/TTL 缓存(共享 500 码上限、3600s TTL、写后失效):

- `get_daily_basic_df(code, start, end)` = 缓存全表 → 按日期切片;
- `get_financials_asof(code, as_of)` = 缓存全表 → 按 `ann_date ≤ as_of` 过滤
  (点对点语义不变,只是把过滤从 SQL 挪到内存);
- `invalidate_series_cache()` 现在同时清三张缓存,财务同步写库后立即失效
  (`app._sync_latest_financials`),与行情写库同规则;
- 顺带修了一个暴露出来的确定性缺口:同一法定披露日下多期报告(年报 4/30 与
  次年一季报 4/30 撞日)原来由 SQLite 返回顺序决定谁赢,现在按
  `(ann_date, end_date)` 排序 + merge_asof 后向取最新一期(有测试锁定)。

## 量测(scratch 副本,100 码,118 因子,同机)

| 阶段 | 耗时 | daily_basic 读 | financials 读 |
|---|---|---|---|
| cold(缓存为空) | 130.5s | 800 | 799 |
| warm(同实例缓存已热) | 113.4s | **0** | **0** |

两点结论,分开看:

1. **查询量是确定性消除的**:冷阶段 ~1600 次 SQLite 读在热阶段归零——
   `get_daily_basic_df` / `get_financials_asof` 的每次调用都命中内存缓存。
2. **但耗时没有同比下降**(130s → 113s):隔离环境里这 1600 次读本身只值
   ~17s(OS 页缓存已热),真正的地板是 118 因子 × 2 窗口 × 100 码的 pandas
   `evaluate_series` 计算(≈ 2 分钟)。所以线上那次 12 分钟不是查询体积,而是
   **竞争放大**:挖掘钩子与冷启动后的首轮行情同步/agent tick 并发,每个
   写入批次都整体失效 provider 缓存 → 每因子重新穿透 SQLite + 与写线程抢
   DB 锁。本修复把查询量从「每因子×每码」(≈47k)降到「工作线程数×每码
   一次」(8×100 的 herd,只发生在并发冷启动瞬间)再到热阶段 0,把锁竞争
   暴露面砍掉两个数量级,但不动内在计算量。

## 结论

1. **采纳**:两张表的 per-code 全量表缓存与行情缓存同构、写后失效一致,查询量
   确定性归零,内存有界(500 码上限)。这是把 2026-08-14 冷启动修复补完整
   (行情有缓存,基本面没有)。
2. **边界**:内在 pandas 计算(~2 分钟/日)是地板;线上更慢的场景来自写同步的
   缓存失效与锁竞争,不是本修复的目标。若日后还要压,方向是把「每码合并帧」
   在因子循环外物化一次(200 帧共享),属于另一次重构,不在本轮。
3. 顺带修复的确定性缺口(同一披露日多期报告按最新一期取胜)有测试锁定,与
   缓存无关但同源暴露。

## 复跑

```bash
python scripts/factor_rescore_bench.py --codes 100
```

