# 任务：前向模拟盘 runner（paper_forward）——新策略上闸后自动挂账跟踪

## 背景（先读）

- 仓库已完成 9 方向证否 + 1 个过闸策略 `strategies/sse_index_enhance.py`
  (#197)。用户要求：今后每个过闸新策略都要跑**前向模拟盘**（forward paper
  trading），定期出净值/超额/回撤报告。
- 关键机制约束（不要违反）：
  1. sse_enhance 类策略 `selectable=False`、signal.strength=组合权重，
     **不走** agent 的逐股信号路径（其 docstring 明示 live per-stock sizing
     不适用本策略），也不能带 per-stock 止损 overlay（会破坏复制）。
  2. 账户按 `QUANTI_ACCOUNT` env 分文件：`data/{account}.db`，market.db
     共享。前向模拟盘必须用**独立账户**（约定 `QUANTI_ACCOUNT=fwd_sse` 等），
     绝不与交互式 paper 账户混用。
  3. PaperBroker 已有：挂单 T+1 次日开盘成交、涨跌停/一字板闸门、A股费用
     模型、红利税（#199 新增）。复用，不另造撮合。

## 要做什么

### 交付物 1：`scripts/paper_forward.py` —— 可复用的前向 runner

```
用法: .venv/bin/python scripts/paper_forward.py init   --account fwd_sse --strategy sse_enhance --cash 500000
      .venv/bin/python scripts/paper_forward.py tick   --account fwd_sse          # 幂等,可日多次调
      .venv/bin/python scripts/paper_forward.py report --account fwd_sse          # 输出 JSON 到 stdout
```

- **init**：建账户 DB（复用 Database.initialize），把 strategy 名与
  初始资金写入该账户 app_config（新增两个 key，不动现有列）；立即跑一次
  tick 建仓。策略名必须是 main 上 `strategies/` 里真实存在且
  `param_space`/接口满足的组合权重类策略（校验失败 fail-loud）。
- **tick**：取行情截至的最新交易日为信号日；策略 on_bar 生成目标权重
  → 与当前持仓 diff → 生成 买/卖 挂单（市价=次日开盘，走 PaperBroker
  现有挂单队列）；月末调仓日内只在建仓/调仓窗口挂单（跟 sse_enhance 的
  月频节奏一致——以策略自身信号为准，脚本不做二次择时）。幂等：同日重复
  tick 不双挂单（用决策日志查重）。停牌/无行情票按现有引擎逻辑跳过。
- **report**：从账户 DB 净值历史（若无逐日净值表则按现有 holdings+成交
  回放计算，选择项目内已有能力，缺什么补什么但**不得**改 engine/
  paper_broker 的既有语义）输出：
  `{account, strategy, as_of, days_running, nav, total_value, pnl_pct,
    bench:{code, pnl_pct同期}, excess, max_drawdown, turnover_last_month,
    pending_orders, alerts:[...]}`
  bench 默认 = 策略声明的基准（sse_enhance → 000001.SH 价格指数；
  app_config 可覆盖 bench_code）。

### 交付物 2：`tests/test_paper_forward.py`

全 fake（临时目录建账户库 + 内存行情注入，参考 tests/test_agent.py 的
fixture 模式）：init→建仓挂单→次日 tick 成交→持仓=目标权重（整手误差内）
→report 数字对账；幂等（同日双 tick 只一批挂单）；月末/月中调仓节奏；
策略不存在/非组合权重类时 fail-loud。

### 交付物 3：立即实跑——挂上 sse_enhance 的前向模拟盘

本环境直接执行：`QUANTI_ACCOUNT=fwd_sse python scripts/paper_forward.py
init --account fwd_sse --strategy sse_enhance --cash 500000`，报告首笔
建仓结果（挂单数、金额分布），并跑一次 report 贴出 JSON。此后 tick 由
外部调度器负责（不在本任务内）。

### 交付物 4：README 段

docs/USAGE.md 增加「前向模拟盘」小节：三命令 + 新策略上线 checklist
（过闸 → init 新账户 → 调度器加 tick → 周报订阅）。

## 约束

- 不改 engine.py / paper_broker.py 语义（只调用）；需要钩子时在
  paper_forward.py 侧适配。
- 中文注释风格与现有 scripts/ 一致；pytest 全绿；ruff 干净。
- 组合权重→整手挂单的取整误差允许（100 万资金下单票 <0.5% 偏差）。
- tick 必须能在交易时段外安全跑（无当日行情时用最新可得数据，标记
  data_asof）。

## 完成定义

4 项交付全齐；实跑 init+report 的 JSON 贴在最终汇报里。

---

## 实跑记录(2026-09-14,fwd_sse 账户上线)

`init --account fwd_sse --strategy sse_enhance --cash 500000` 在真实行情库
(数据截至 2026-09-10)上的结果,如实归档:

- 策略目标权重 **2269 只**(Σw=1,七月末→八月末快照口径);
- 整手约束下 **只有 47 只买得起一手**,其余 **2222 只(合计权重 62.66%)**
  被跳过 → 建仓满仓后仍约 **63% 现金滞留**,这笔前向账跟踪的不是"上证全收益",
  而是"上证 47 大 + 63% 现金"。runner 不静默改权重,把退化如实记进 alerts。
- 单笔挂单金额分布(元):max 18,354(601398 w=3.67%)/ p75 5,713 /
  中位 1,142 / p25 686 / min 218;Σ 目标金额 186,693 = 37.3% NAV。
- 600519 是被整手挡掉的典型:权重 1.92%(目标 9,600 元),但一手 hfq 价
  1,486.9 元 = 账户的 29.7% → 买不起,只能滞留现金。
- 与既有回测结论一致(**资金-精度曲线**):500 万档 min_weight=0 也只有
  773 只成交,5000 万档 1857 只;百万级以下本策略定论是"直接买指数 ETF"。
  要这张前向账真正跟踪全收益,应改 `--cash 5000000` 或
  `--strategy-config '{"min_weight": 6e-05}'` 重挂一张账(账本不可改起点)。

结论:runner 本身按设计工作(挂单 T+1 次日开盘、幂等、告警如实),而这张
50 万档的账恰好演示了策略文档里那条"小资金复制退化"——前向盘的价值正在于
把回测里被平均掉的物理约束暴露出来。
