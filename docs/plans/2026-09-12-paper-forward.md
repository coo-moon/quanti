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
