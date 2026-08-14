# Selector sweep 成本实证 —— 结论:sweep 本体 88 秒,19 分钟冷启动是共存开销;syncer 暖机整体让路

日期:2026-08-14
脚本:`scripts/selector_cost_study.py`(可复跑,子进程隔离测量)

## 问题

冷启动首轮 tick 的 selector sweep 实测 19-21 分钟(第 8-10 轮),两个假设:
(a) thread_map 的 8 worker 在纯 Python 回测引擎上受 GIL 拖累;
(b) 40 折全历史 walk-forward 本身太贵。

## 测量(40 码宇宙、4 个未剔除策略、子进程冷缓存)

| workers | max_folds | 耗时 | 排名 |
| --- | --- | --- | --- |
| 1 | 8 | 30.3s | kdj > rsi > boll > macd |
| 2 | 8 | 30.5s | 同 |
| 4 | 8 | 30.9s | 同 |
| 8 | 8 | 31.0s | 同 |
| 1 | 16 | 43.4s | rsi > kdj > macd > boll |
| 1 | 40 | 43.6s | 同 |
| 1 | 40 × 100 码 | **88.5s** | kdj > macd > boll > rsi |

## 结论

1. **worker 数无影响**(1→8 全 ~30s)——纯 Python 引擎在 GIL 下并行无效,但也不
   更慢;不调整。
2. **16 折与 40 折耗时/观测数完全相同**(n_obs=1076)——5 年数据按 126 天块最多
   铺出 ~8 折,`wf_max_folds=40` 本来就是空转;不调整。
3. **sweep 本体只要 88.5 秒(100 码 40 折)**——真实 tick 的 19-21 分钟全是冷启动
   共存开销:收盘后 syncer 的全市场队列同步、regime 全市场扫描、financials 拉取
   与 sweep 争 CPU 和 DB 锁(第 10 轮暖机闸只挡了 doctor/闸门/重评,没挡队列与
   regime)。

## 修复(本 PR)

暖机闸升级为 syncer 整体让路:`heavy_warmup_sec` 内整个 syncer 循环只睡不干
(财务同步、regime、收盘队列、因子重评全部推迟),状态置 warming 供仪表盘可见;
暖机到期后恢复常规调度(日锁在暖机后才建立,当天钩子不丢)。

## 复跑

```bash
python scripts/selector_cost_study.py --workers 1 --max-folds 40 --codes 100
```

