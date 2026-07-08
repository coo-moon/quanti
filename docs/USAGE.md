# Quanti 使用文档

> 面向 A 股的 AI 自治量化交易系统。设定目标，剩下交给 Agent。

适用版本:Quanti(2026-05 升级:walk-forward 验证、截面因子、Top-K ensemble、Claude LLM agent 接入、流动性宇宙清洗;2026-07:QMT 实盘接入)。日常仍默认走 **PaperBroker 模拟盘**。**实盘**已实现直连 xtquant 后端(`XtDirectBackend` / qmt-bridge),并对江海证券 QMT 完成**只读**端到端验证;真实下单默认关闭,需 `QMT_BRIDGE_ALLOW_ORDERS=1` + `QUANTI_ACCOUNT=live` + `QUANTI_LIVE_ACK=I_KNOW_REAL_MONEY` 三重放行,且尚未在真机跑过下单冒烟。实盘接入/运维见 **[`live-trading-runbook.md`](live-trading-runbook.md)**。

---

## 目录

1. [60 秒上手](#1-60-秒上手)
2. [完整安装](#2-完整安装)
3. [核心概念](#3-核心概念)
4. [CLI 命令大全](#4-cli-命令大全)
5. [Web UI 操作手册](#5-web-ui-操作手册)
6. [AI Agent 工作流](#6-ai-agent-工作流)
7. [OpenClaw / MCP 接入](#7-openclaw--mcp-接入)
8. [REST API 参考](#8-rest-api-参考)
9. [自定义策略与选股器](#9-自定义策略与选股器)
10. [数据同步与管理](#10-数据同步与管理)
11. [回测](#11-回测)
12. [风控配置](#12-风控配置)
13. [运维与排错](#13-运维与排错)
14. [系统架构](#14-系统架构)

---

## 1. 60 秒上手

```bash
# 一次性配环境（Python 3.11+，Node 18+）
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cd web && npm install && npm run build && cd ..

# 设定目标 + 一键起飞
quanti up --target 0.20 --max-drawdown -0.20 --risk medium
```

浏览器打开 **http://127.0.0.1:8000/agent**，你会看到：
- 净值卡：当前组合价值与累计收益
- 目标卡：你和目标年化的距离
- Agent 状态卡：是否在跑
- 目标设定区：可改的目标参数
- 当前持仓表
- 最近策略评估表（Agent 是怎么选策略的）
- 决策日志

首次运行库是空的，Agent 会自动在后台拉股票列表（约 5000 只，1~3 分钟），完成后下一个 tick 就开始正经干活。

---

## 2. 完整安装

### 2.1 环境要求

- Python **3.11+**
- Node.js **18+**（仅构建前端时需要）
- 操作系统：macOS / Linux（Windows 也行，但 AkShare 在 macOS/Linux 更稳）
- 磁盘：5GB 可用（全 A 股 1 年 K 线 ≈ 2GB）

### 2.2 安装步骤

```bash
# 克隆项目（如果是新机器）
git clone <repo-url> quanti
cd quanti

# Python 后端
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"

# 前端构建（不构建的话只有 API 可用，没有 Web 页面）
cd web
npm install
npm run build
cd ..

# 验证安装
quanti --help
pytest -q              # 应该 809 passed(含实盘安全/日内闸门/订单幂等/因子挖掘/hyperopt 等)
```

### 2.3 目录结构

```
quanti/
├── data/                     # SQLite 数据库（quanti.db）
├── docs/                     # 文档
├── strategies/               # 内置 + 你自定义的策略
├── screeners/                # 内置 + 你自定义的选股器
├── tests/                    # 单元测试
├── web/                      # Vue 3 前端
└── quanti/                   # Python 包
    ├── agent/                # Agent 自治模块
    ├── api/                  # FastAPI 路由
    ├── backtest/             # 回测引擎
    ├── data/                 # 数据层（SQLite + AkShare）
    ├── execution/            # 模拟盘 / 实盘执行器
    ├── factors/              # 技术因子
    ├── risk/                 # 风控
    ├── screener/             # 选股器框架
    ├── strategy/             # 策略框架
    ├── cli.py                # quanti 命令入口
    └── mcp_server.py         # MCP server
```

---

## 3. 核心概念

| 概念 | 说明 |
|------|------|
| **Strategy（策略）** | 输入每根 K 线，输出 `Signal`（买 / 卖 / 持有）。内置 6 个，可加自定义 |
| **Screener（选股器）** | 给一只股票打分，分数越高越值得关注。用于在大量候选里筛选 |
| **Signal（信号）** | 策略产物：`stock_code + direction + strength + reason` |
| **PaperBroker（模拟盘）** | 接收 Signal，模拟撮合，记账到本地 DB，遵守 A 股 T+1 / 佣金 / 印花税 |
| **RiskManager（风控）** | 拦截违规信号（单股仓位、组合仓位、止损、ST 黑名单等） |
| **Goal（目标）** | 你设定的目标（年化、最大回撤、风险偏好等），Agent 据此挑策略 |
| **StrategySelector** | 给定 Goal，自动回测所有策略，按得分挑最佳的那个 |
| **AgentRuntime** | 后台循环线程：同步数据→选股→评估策略→生成信号→风控→PaperBroker→记日志 |
| **Decision Log（决策日志）** | Agent 每一次动作都写入 DB，可在 Web/CLI/MCP 回放 |
| **Pool（股票池）** | 你定义的一组 codes，可作为 Agent 的交易宇宙 |
| **Walk-forward(2026-05)** | 用 N 个非重叠 OOS 窗口验证策略,Selector 评分用 OOS Sharpe + 一致性。杜绝单窗 IS 过拟合 |
| **Factor Panel(2026-05)** | 截面因子(动量/反转/换手/波动)→ 横截面 z-score → 行业中性化 → 等权合成,产出每只股的 composite 分 |
| **Ensemble(2026-05)** | Selector 选 Top-K 策略按 OOS Sharpe softmax 加权,信号融合 + 因子覆盖 |
| **VolTargetSizer(2026-05)** | 反波动率仓位:低波加仓、高波减仓,目标组合年化波动 18%。opt-in |
| **LLM Agent(2026-05)** | Claude 看完候选股 + 持仓 + 历史决策,通过 `propose_orders` 工具最终落子。需要 ANTHROPIC_API_KEY |
| **UniverseBuilder(2026-05 P4)** | 流动性 + ST + 新股名称 + 上市天数过滤,把 5000 只 A 股清洗到 ~1000 只可投资股票。日缓存。opt-in |

数据流：

```
AkShare ──→ Database ──→ Provider ──→ Strategy ──→ Signal
                                          │
Pool ─→ Screener ─→ 候选 codes ───────────┘
                                          │
                                          ▼
                              RiskManager ──→ PaperBroker ──→ DB（orders/trades/positions）
                                                                 │
                                                                 ▼
                                                          Decision Log
```

---

## 4. CLI 命令大全

### 4.1 `quanti up` — 一键启动（最常用）

```bash
quanti up [--target 0.20] [--max-drawdown -0.20] [--risk low|medium|high]
         [--pool POOL_NAME] [--screener SCR_NAME] [--strategy STRAT_NAME]
         [--cash 1000000] [--host 127.0.0.1] [--port 8000] [--no-agent]
```

参数：

| 参数 | 默认 | 说明 |
|------|-----|------|
| `--target` | 0.20 | 目标年化收益（0.20 = 20%） |
| `--max-drawdown` | -0.20 | 可接受最大回撤（负数） |
| `--risk` | medium | 风险偏好；影响 Selector 给夏普/回撤的权重 |
| `--pool` | "" | 股票池名（空 = 用全部已同步股票） |
| `--screener` | "" | 选股器名（空 = 不预筛） |
| `--strategy` | "" | 指定策略名（空 = Agent 自动挑） |
| `--cash` | 1000000 | 初始资金（仅首次创建组合时生效） |
| `--no-agent` | False | 只起 Web，不自动启 Agent |

例子：

```bash
# 默认（年化 20%、平衡风险、自动挑策略）
quanti up

# 激进：年化 30%，回撤可到 -25%，高风险偏好
quanti up --target 0.30 --max-drawdown -0.25 --risk high

# 用我自己的股票池，只跑 MA 均线交叉策略
quanti up --pool my_watchlist --strategy ma_cross

# 仅起 Web，不让 Agent 跑（手动模式）
quanti up --no-agent
```

### 4.2 `quanti agent` — 不起 Web 操作 Agent

```bash
quanti agent tick                       # 立即跑一轮（同步执行，返回结果）
quanti agent status                     # 查看 Agent 状态
quanti agent goal                       # 查看当前目标
quanti agent set-goal --target 0.25 --risk high   # 修改目标
quanti agent decisions --limit 50       # 查看最近 50 条决策日志
quanti agent prune --older-than-days 90 # 手动清理 90 天前的决策日志
```

### 4.3 `quanti mcp` — 启 MCP server

```bash
quanti mcp
```

这是 stdio JSON-RPC server，给 OpenClaw / Claude Desktop / Cursor 等 MCP 客户端用的。直接在终端跑会等你输入，正常用法是让 MCP 客户端拉起它。

### 4.4 `quanti serve` — 只起 Web 不动 Agent

```bash
quanti serve --port 8000
```

`up` 的子集：不会自动同步股票列表、不会自动启 Agent。适合调试。

### 4.5 `quanti sync` — 手动同步数据

```bash
quanti sync --stocks                   # 同步股票列表
quanti sync --calendar                 # 同步交易日历
quanti sync --quotes                   # 同步所有库内股票的 K 线
quanti sync --quotes --codes 600519,000001   # 只同步指定的
```

### 4.6 `quanti backtest` — 命令行回测

```bash
quanti backtest \
  --strategy ma_cross \
  --codes 600519,000001 \
  --start 2025-01-01 \
  --end 2025-12-31 \
  --cash 1000000
```

---

## 5. Web UI 操作手册

启动 `quanti up` 后访问 http://127.0.0.1:8000

### 5.1 仪表盘（/）

总览页：
- 已同步股票数 / 股票池总数 / 最近更新时间
- 添加股票（输入代码批量加入）
- 一键下载 K 线 / 同步全 A 股池
- 任务进度条 + ETA

### 5.2 AI Agent（/agent）⭐ 核心页面

**目标设定区**：
- 目标年化收益：例如 0.20 = 20%
- 可接受最大回撤：例如 -0.20，负数
- 风险偏好：保守 / 平衡 / 激进
- 股票池：选一个已建好的 pool，留空 = 用全部
- 选股器：留空 = 不预筛
- 策略：留空 = Agent 自动挑最佳

**操作按钮**：
- 保存目标
- 立即跑一轮（强制触发一次完整 tick）
- 启动 / 停止 Agent
- 重置组合（清空持仓和交易记录）

**当前持仓表**：代码 / 名称 / 数量 / 成本 / 现价 / 市值 / 盈亏（含手动卖出按钮）

**手动下单区**：你可以越过 Agent 直接下单（OpenClaw 也走同一个接口）

**最近策略评估表**：Agent 上一次是怎么挑策略的，每个策略的年化、最大回撤、夏普、成交数和综合得分都有

**决策日志**：按颜色分类
- 蓝色 = 成交（trade）
- 黄色 = 风控拒绝（risk_reject）
- 绿色 = 周期完成 / 策略挑选 / ensemble 选定(cycle / strategy_pick / strategy_ensemble)
- **紫色 = LLM 决策(llm_cycle),含 Claude 的中文 reasoning,展开看推理理由**
- 红色 = Agent 报错(agent_error)
- 灰色 = 启停事件 + 宇宙过滤(agent_start / agent_stop / universe_filter)

页面每 15 秒自动刷新。

### 5.3 股票池（/pool）

- 创建 / 删除 pool
- 给 pool 加 / 删股票
- 一键同步 pool 内股票的 K 线（带 ETA 进度条）

### 5.4 选股中心（/screener）

- 选一个选股器
- 选数据来源（pool 或全部）
- 设定参数（lookback 天数、top N）
- 跑出来按得分排序的 top 列表

### 5.5 回测中心（/backtest）

- 选策略 + 股票代码 + 时间区间 + 初始资金
- 一键回测，看绩效曲线 + 全部交易 + 指标（夏普、年化、最大回撤等）

---

## 6. AI Agent 工作流

### 6.1 一个完整 tick 干了什么

```
1. load_goal()                        从 DB 取最新 Goal
2. resolve_universe(goal)             解析交易宇宙
   ├─ goal.universe_pool 有值 → pool 内 codes
   └─ 否则                  → DB 内所有有 K 线的 codes
3. ensure_recent_data(codes)          为缺最近 7 天数据的 codes 补 sync（一次最多 20 只）
4. run_screener(goal, codes)          可选：用 screener 筛 top-20
5. pick_strategy:
   ├─ goal.strategy_name 有值 → 用它
   └─ 否则 → StrategySelector.pick_best
       └─ 对每个策略回测最近 365 天，按 Goal 算综合得分，选最高
6. for each candidate code:
      bars = provider.get_daily_bars(code, last 365d)
      strategy.on_bar(bar) → 收集最近 3 天的 signals
7. broker.check_stop_loss()           先做止损卖出，释放现金
8. broker.execute_signals(signals)
   ├─ RiskManager.check 一道
   ├─ 通过 → 模拟撮合，写 orders / trades / positions
   └─ 拒绝 → 写 risk_reject 日志
9. broker.snapshot_portfolio()        快照写入 portfolio_snapshots
10. log_decision("cycle", summary)    周期日志
```

### 6.2 tick 频率

默认每 **4 小时**一次 tick。改的话编辑 `quanti/api/app.py` 里 `AgentRuntime(...)` 的构造参数 `tick_interval_sec`。

启动时立即跑一次，之后按间隔。

### 6.2.1 Selector 缓存（自动挑策略时的优化）

如果没钉死策略（`goal.strategy_name` 为空），Agent 默认每 **24 小时**才重新跑一次 Selector（6 策略全量回测）。中间的 tick 复用上次的选择，单次 tick 从 ~640ms 降到 ~40ms（约 **16× 提速**）。

调整：构造 `AgentRuntime` 时传 `selector_reselect_interval_sec`。设成 0 表示每次 tick 都重选。

钉死策略（`goal.strategy_name = "ma_cross"` 等）时 Selector 完全跳过，无缓存开销。

### 6.3 Goal 字段含义

| 字段 | 含义 | 示例 |
|------|------|------|
| `target_annual_return` | 期望的 CAGR | 0.20（20%） |
| `max_drawdown` | 可接受的回撤上限（负数） | -0.20 |
| `risk_tolerance` | 影响 Selector 评分 | "low" / "medium" / "high" |
| `universe_pool` | 交易宇宙的 pool 名 | "core_50"，空 = 全部 |
| `screener_name` | 预筛器 | "ma_trend"，空 = 不筛 |
| `strategy_name` | 钉死的策略 | "ma_cross"，空 = 自动挑 |
| `params` | 传给 strategy.init() 的字典 | `{"short_period": 5}` |
| `rebalance_freq` | 调仓节奏（目前仅 "daily"） | "daily" |
| `enabled` | Agent 是否在跑 | true / false |

### 6.4 StrategySelector 怎么打分

```
score = w_ret × (annual_return - target_annual_return)
      + w_dd  × min(max_drawdown - goal.max_drawdown, 0)
      + w_sharpe × sharpe_ratio
      + activity_bonus
```

权重随风险偏好变：

| risk_tolerance | w_ret | w_dd | w_sharpe |
|----------------|-------|------|----------|
| low            | 0.3   | 1.8  | 0.6      |
| medium         | 0.8   | 1.0  | 0.5      |
| high           | 1.2   | 0.6  | 0.4      |

`activity_bonus` = +1（有成交）/ -1（零成交）—— 不交易的策略不会拿高分。

### 6.5 三种 Agent 运行模式(2026-05 升级)

升级后 Agent 有三条路径,都通过 `goal.params` 切换,**默认仍是 rule 模式以保兼容**:

| 模式 | 触发条件 | 行为 |
|---|---|---|
| **rule**(默认) | 不设 `agent_mode`,或显式 `"rule"` | 走老路径:Selector 挑一个最佳策略,生成信号,直接执行。 |
| **ensemble** | `params["ensemble_enabled"]=True` 且未钉策略 | Selector 选 Top-K(默认 3)策略,softmax 按 OOS Sharpe 加权;运行所有候选策略;乘上截面因子分;按阈值过滤 → 下单。 |
| **llm** | `params["agent_mode"]="llm"` 且未钉策略 | 先走 ensemble 流程产出候选股,把候选 + 持仓 + 近期决策喂给 Claude,LLM 通过 `propose_orders` 工具决定最终下单。LLM 没装时优雅降级到 ensemble。 |

钉死策略(`goal.strategy_name="ma_cross"`)总是优先级最高,任何模式下都直接用钉选策略。

### 6.6 新增 `goal.params` 字段(2026-05 升级)

完整参考表。所有字段都有合理默认值,**全部缺省就是 rule 模式 + 老行为**。

#### 6.6.1 Walk-forward 验证(Phase 1)

Selector 默认从 IS 评分改为 OOS 评分,杜绝过拟合。

| key | 默认 | 说明 |
|---|---|---|
| `wf_enabled` | `True` | 是否启用 walk-forward。`False` 则退化为单窗 IS 回测(旧行为)。 |
| `wf_n_folds` | `3` | 滚动 OOS 测试窗数。3 个非重叠窗共约 2 个月 OOS。 |
| `wf_warmup_days` | `120` | 每个测试窗前的暖机天数(指标如 MA(20)/MACD 需要状态)。 |
| `wf_test_days` | `21` | 每个测试窗的天数(约 1 个月)。 |

数据要求:`wf_n_folds × (wf_warmup_days + wf_test_days)` 天的历史。3×141 ≈ 一年。

#### 6.6.2 Ensemble + 截面因子(Phase 2)

| key | 默认 | 说明 |
|---|---|---|
| `ensemble_enabled` | `False` | 是否启用 Top-K 策略组合。 |
| `top_k_strategies` | `3` | Top-K 个策略,按 OOS Sharpe softmax 加权。 |
| `signal_threshold` | `0.30` | 融合后 `final_score` 阈值。越高越精挑。 |
| `factor_blend` | `0.5` | 因子覆盖权重。0=只看策略投票,1=只看截面因子,0.5 平衡。 |
| `industry_neutral` | `False` | 是否做主动行业中性化(每行业最多 N 个候选)。 |
| `n_per_industry` | `2` | 启用行业中性化时,每行业最多保留几个候选。 |

#### 6.6.3 LLM 决策(Phase 3)

| key | 默认 | 说明 |
|---|---|---|
| `agent_mode` | `""` | 设为 `"llm"` 启用 Claude 决策路径。 |
| `llm_model` | `"claude-sonnet-4-5"` | Anthropic 模型 ID。可设 `claude-opus-4-7`(更强更贵)。 |
| `llm_max_tokens` | `4096` | 单次 LLM 调用上限。 |
| `llm_max_iterations` | `5` | 工具调用最大轮数(防止 LLM 无限 inspect)。 |
| `llm_max_candidates` | `20` | 传给 LLM 的候选股数量上限(控 token)。 |
| `llm_temperature` | `0.3` | 温度。0.3 偏确定性,需要点判断空间但不要太发散。 |

#### 6.6.4 候选池 + 流动性清洗(Phase 4)

| key | 默认 | 说明 |
|---|---|---|
| `liquidity_filter` | `False` | 启用 `UniverseBuilder` 清洗(5000→~1000)。日缓存。 |
| `universe_min_adv20` | `5e7`(5000万) | ADV20 阈值。1e8 = 仅中证 800 量级,1e7 = 含微盘 |
| `universe_min_active_days` | `40` | 近 60 日有效交易日下限 |
| `universe_min_age_days` | `90` | 上市天数下限 |
| `screener_top_n` | `50` | screener 评分后留几只(之前硬编码 20) |
| `no_screener_take` | `100` | 没 screener 时按 ADV20 排序取几只(之前硬编码 30 字典序) |
| `selector_max_universe` | `100` | Selector WF 内部 cap(之前硬编码 50);硬下限 20 |

### 6.7 LLM 模式接入步骤

```bash
# 1. 安装 anthropic SDK(可选依赖)
pip install -e '.[llm]'

# 2. 设环境变量
export ANTHROPIC_API_KEY="sk-ant-..."

# 3. 启用 LLM 模式
quanti agent set-goal --target 0.25 --risk medium
# 然后通过 API / MCP / SQL 改 params
curl -X POST http://127.0.0.1:8000/api/goal \
  -H 'Content-Type: application/json' \
  -d '{"params": {"agent_mode": "llm", "ensemble_enabled": true, "industry_neutral": true}}'

# 4. 跑一轮看效果
quanti agent tick
```

**安全 invariant**(代码里硬约束,LLM 突破不了):

- LLM 只能选**已经 vetted 的候选股**(ensemble 流程筛过的)
- 单股 `size_pct ≤ 0.10`(单股仓位 10% 上限)
- 单 tick 最多 5 单
- 所有提议仍经 `RiskManager.check()` 二次校验
- API key 缺失 / SDK 未装 / 网络挂掉 → 自动降级到 ensemble 路径,不会丢 tick

LLM 决策日志查看:Web `/agent` 页面紫色卡片展示 Claude 的中文 `reasoning`;或 `quanti agent decisions --limit 20`。

### 6.8 截面因子说明(Phase 2)

`quanti/factors/cross_sectional.py` 内置 5 个 A 股有实证效力的因子(已横截面 z-score + 行业中性化):

| 因子 | 计算 | 方向 |
|---|---|---|
| `momentum_3m` | t-63 到 t-21 累计收益(跳过最近 1 个月避反转污染) | 高=好 |
| `momentum_6m` | t-126 到 t-21 累计收益 | 高=好 |
| `reversal_1w` | -(t-5 到 t 累计收益) | 短期超涨 → 看空 |
| `turnover_20d` | -20 日均换手率 | 高换手 → 看空(注意力陷阱) |
| `realized_vol_20d` | -20 日年化波动率 | 低波 → 看多(低波 anomaly) |

不要无脑信:这是 starter 集,在不同市场阶段(牛/熊/震荡)各因子贡献不同。可以通过自定义 `FactorConfig.weights` 调权,或加新因子(继续往 `cross_sectional.py` 加函数 + 注册)。

### 6.9 回测真实性升级(Phase 1)

#### 6.9.1 成交量加权滑点(默认开启)

旧版固定 0.1% 滑点。新版用平方根冲击模型:

```
cost_bps = base_bps + impact_bps_per_pct × sqrt(participation_pct ^ alpha)
```

默认参数下:1% ADV 参与率 ≈ 10bps(匹配老默认),10% ≈ 16bps,100% ≈ 50bps,封顶 300bps。

回测会自动计算每只票的 20 日 ADV,如果数据缺失(新股、停牌)优雅降级到 base bps。

需要纯 flat 行为(对比老版):

```python
from quanti.backtest.slippage import FlatSlippage
engine = BacktestEngine(provider=p, slippage=FlatSlippage(bps=10))
```

#### 6.9.2 波动率目标仓位(opt-in)

`PaperBroker` 默认仍是老 sizing 逻辑(`signal.strength × cash`)。要启用 vol-targeting:

```python
from quanti.risk.sizer import VolTargetSizer
broker = PaperBroker(db, provider, initial_cash=1_000_000,
                     sizer=VolTargetSizer(target_portfolio_vol=0.18,
                                          lookback_days=60,
                                          n_target_positions=10))
```

效果:低波股加仓,高波股减仓,目标组合年化波动 18%。仍受 `RiskConfig.max_position_pct=0.10` 硬上限。

启用 vol-targeting **不是开 ensemble/LLM 的前提**,可以独立用。

### 6.10 流动性宇宙清洗 + 候选池配置(2026-05 P4)

**问题**:之前 universe → 候选股的流水线有三个硬编码截断,加起来导致系统真正"看"过的只有 20-30 只(而且 no-screener 路径还是字典序硬切前 30 只 = 000001 平安银行 ~ 000030 这些)。

**修复**:全部改成 `goal.params` 配置 + 加一层流动性清洗。

#### 6.10.1 三个候选池截断的新默认值

| key | 之前 | 现在 | 在哪一步 |
|---|---|---|---|
| `screener_top_n` | 硬编码 20 | **50** | screener 评分后留 top-N |
| `no_screener_take` | 硬编码 30(字典序!) | **100**(按 ADV20 排序) | 没 screener 时的 fallback |
| `selector_max_universe` | 硬编码 50 | **100** | Selector 跑 walk-forward 时的 cap |

`no_screener_take` 的副作用修复:之前是 `universe[:30]` 字典序硬切,现在按 20 日 ADV 降序排再切 → 不再偏向"代码靠前"的股票。

#### 6.10.2 流动性宇宙清洗(opt-in)

启用 `params["liquidity_filter"]=True` 时,在 `_resolve_universe` 阶段先把全 A 股 5000 只过一遍 `UniverseBuilder`,过滤掉:

| 维度 | 默认阈值 | param key |
|---|---|---|
| ADV20(20 日平均成交额) | ≥ 5000 万元 | `universe_min_adv20` |
| 近 60 日有效交易日 | ≥ 40 天 | `universe_min_active_days` |
| 上市天数 | ≥ 90 天 | `universe_min_age_days` |
| 名称黑名单 | 含 "ST" / "退" 自动剔除 | (固定) |

**结果**:5000 → ~1000 只可投资股票。**每日缓存**,4h tick 之间不重算。

**降级保护**:如果你设的阈值太严过滤掉所有股票,系统**自动 fallback** 到未过滤列表,不会让 Agent 死等。同时往决策日志写一条 `universe_filter` 类型,带过滤前后的数字。

#### 6.10.3 完整推荐配置(给 LLM 模式)

```bash
curl -X POST http://127.0.0.1:8000/api/goal \
  -H 'Content-Type: application/json' \
  -d '{
    "params": {
      "agent_mode": "llm",
      "ensemble_enabled": true,
      "industry_neutral": true,
      "liquidity_filter": true,
      "screener_top_n": 50,
      "no_screener_take": 100,
      "selector_max_universe": 100,
      "universe_min_adv20": 50000000,
      "wf_enabled": true,
      "wf_n_folds": 3
    }
  }'
```

这套参数下,流水线表现:

```
5000 全 A 股
  → ~1000 (流动性清洗,每日缓存)
  → ≤ 50 / ≤ 100 (screener / 无 screener fallback,每 tick)
  → ≤ 100 (Selector 内部,但 WF 缓存中)
  → fused_candidates ~30 (因子打分 + 阈值过滤)
  → LLM 看到 top 20,挑 ≤ 5 单
```

Selector 缓存 24h,流动性清洗缓存 1 天,所以**绝大多数 tick 只跑因子 + 信号生成 + LLM 一次调用**,真实开销 5-15 秒。

#### 6.10.4 怎么调

- 想让 LLM 看到更"主流"的股票 → 拉高 `universe_min_adv20`(比如 1 亿,只留中证 800 量级)
- 想包含微盘股测试 → 设 `universe_min_adv20=10000000`(1000 万 ADV)
- 想关流动性清洗看老行为 → `liquidity_filter=False`(默认)
- 想看每日清洗了多少 → 查 `list_decisions(kind="universe_filter")`

### 6.11 怎么让 Agent 一直跑

```bash
# 方式 1：CLI 启动，前台跑（关终端就停）
quanti up

# 方式 2：后台跑
nohup quanti up >/tmp/quanti.log 2>&1 &

# 方式 3：systemd（Linux）
# /etc/systemd/system/quanti.service
[Unit]
Description=Quanti AI Agent
After=network.target
[Service]
WorkingDirectory=/opt/quanti
ExecStart=/opt/quanti/.venv/bin/quanti up
Restart=on-failure
[Install]
WantedBy=multi-user.target

# 方式 4：launchd（macOS）
# ~/Library/LaunchAgents/com.quanti.agent.plist
```

---

## 7. OpenClaw / MCP 接入

### 7.1 配置 MCP 客户端

OpenClaw / Claude Desktop / Cursor 的配置文件加：

```json
{
  "mcpServers": {
    "quanti": {
      "command": "quanti",
      "args": ["mcp"]
    }
  }
}
```

如果 `quanti` 不在 PATH，用绝对路径：

```json
{
  "mcpServers": {
    "quanti": {
      "command": "/Users/you/source/quanti/.venv/bin/quanti",
      "args": ["mcp"]
    }
  }
}
```

⚠️ MCP server 用的数据库是 `data/quanti.db`（相对于 MCP 启动时的工作目录）。配置 `cwd` 字段或者用绝对路径的数据库：

```json
{
  "mcpServers": {
    "quanti": {
      "command": "/Users/you/source/quanti/.venv/bin/quanti",
      "args": ["mcp"],
      "cwd": "/Users/you/source/quanti"
    }
  }
}
```

### 7.2 可调用的 MCP 工具(19 个)

| 工具 | 干什么 |
|------|--------|
| `list_strategies` | 列出所有策略 |
| `list_screeners` | 列出所有选股器 |
| `list_pools` | 列出所有股票池 |
| `get_goal` | 读当前目标 |
| `set_goal` | 改目标（年化、回撤、风险、策略、选股、pool） |
| `agent_start` | 启动 Agent |
| `agent_stop` | 停止 Agent |
| `agent_status` | 当前状态 + 上一次决策概要 |
| `agent_tick` | 立即跑一轮 |
| `get_portfolio` | 持仓和净值 |
| `list_orders` | 最近订单 |
| `list_trades` | 最近成交 |
| `list_decisions` | 决策日志 |
| `prune_decisions` | 清理 N 天前的决策日志 |
| `place_order` | 手动下单 |
| `run_backtest` | 试跑一次回测（不影响实盘） |
| `run_screener` | 跑选股器 |
| `sync_stocks` | 同步股票列表 |
| `sync_quotes` | 同步指定股票的 K 线 |

### 7.3 OpenClaw 典型对话

> 用户："看一下当前组合，距离目标还差多少？"
> OpenClaw：调 `get_portfolio` + `get_goal` → 报告净值、累计收益、目标差距

> 用户："把目标改成年化 30%，激进点"
> OpenClaw：调 `set_goal({"target_annual_return": 0.30, "risk_tolerance": "high"})`

> 用户："让 Agent 立刻跑一轮看看"
> OpenClaw：调 `agent_tick`，回报选了什么策略、出了多少单

> 用户："帮我手动买点平安银行"
> OpenClaw：调 `place_order({"code": "000001", "direction": "buy", "strength": 0.2})`

> 用户："最近有没有什么风控拒绝？"
> OpenClaw：调 `list_decisions({"kind": "risk_reject", "limit": 20})`

---

## 8. REST API 参考

API 基地址：`http://127.0.0.1:8000/api`

### 8.1 目标 / Agent

| Method | Path | 说明 |
|--------|------|------|
| GET | `/goal` | 取当前目标 |
| POST | `/goal` | 改目标（JSON body） |
| POST | `/agent/start` | 启动 |
| POST | `/agent/stop` | 停止 |
| POST | `/agent/tick` | 强制跑一轮 |
| GET | `/agent/status` | 状态 |
| GET | `/agent/decisions?limit=50&kind=trade` | 决策日志 |
| POST | `/agent/decisions/prune?older_than_days=90` | 手动清理 N 天前的决策日志 |

### 8.2 组合 / 订单 / 成交

| Method | Path | 说明 |
|--------|------|------|
| GET | `/portfolio` | 当前组合（现金、持仓、市值、盈亏） |
| POST | `/portfolio/reset?initial_cash=1000000` | 重置（清持仓） |
| GET | `/portfolio/snapshots?limit=365` | 历史净值快照 |
| GET | `/orders?limit=200` | 订单列表 |
| GET | `/trades?limit=200` | 成交列表 |
| POST | `/orders/manual` | 手动下单 |

`/orders/manual` body：
```json
{
  "code": "600519",
  "direction": "buy",       // 或 "sell"
  "strength": 0.2,          // 0~1，作为现金占比
  "reason": "manual via API"
}
```

### 8.3 资源清单

| Method | Path | 说明 |
|--------|------|------|
| GET | `/strategies` | 所有策略 |
| GET | `/screeners` | 所有选股器 |
| GET | `/pools` | 所有股票池 |
| GET | `/stocks` | 所有股票 |
| GET | `/stocks/stats` | 统计信息 |

### 8.4 数据同步

| Method | Path | 说明 |
|--------|------|------|
| POST | `/sync/stocks` | 同步股票列表 |
| POST | `/sync/quotes` | 同步指定 codes（同步阻塞） |
| POST | `/sync/quotes/async` | 同步指定 codes（异步带进度） |
| GET | `/sync/quotes/status?job_id=xxx` | 查询进度 |

### 8.5 回测 / 选股

| Method | Path | 说明 |
|--------|------|------|
| POST | `/backtest/run` | 跑回测 |
| POST | `/screen/run` | 跑选股 |

### 8.6 curl 例子

```bash
# 改目标
curl -X POST http://127.0.0.1:8000/api/goal \
  -H 'Content-Type: application/json' \
  -d '{"target_annual_return": 0.25, "risk_tolerance": "high"}'

# 启动 Agent
curl -X POST http://127.0.0.1:8000/api/agent/start

# 立即跑一轮
curl -X POST http://127.0.0.1:8000/api/agent/tick

# 查组合
curl http://127.0.0.1:8000/api/portfolio | jq

# 手动下单
curl -X POST http://127.0.0.1:8000/api/orders/manual \
  -H 'Content-Type: application/json' \
  -d '{"code": "600519", "direction": "buy", "strength": 0.1, "reason": "test"}'

# 看最近 10 条决策
curl 'http://127.0.0.1:8000/api/agent/decisions?limit=10' | jq
```

---

## 9. 自定义策略与选股器

### 9.1 写一个策略

在 `strategies/` 下新建 `.py` 文件，继承 `BaseStrategy`：

```python
# strategies/my_breakout.py
from quanti.strategy.base import BaseStrategy
from quanti.models import BarData, Direction, Signal


class MyBreakoutStrategy(BaseStrategy):
    name = "my_breakout"

    def init(self, config: dict) -> None:
        self.window = config.get("window", 20)
        self.threshold = config.get("threshold", 1.02)
        self._highs: dict[str, list[float]] = {}
        self._holding: set[str] = set()

    def on_bar(self, bar: BarData) -> list[Signal]:
        highs = self._highs.setdefault(bar.code, [])
        highs.append(bar.high)
        if len(highs) < self.window + 1:
            return []
        recent_max = max(highs[-self.window-1:-1])
        if bar.code not in self._holding and bar.close > recent_max * self.threshold:
            self._holding.add(bar.code)
            return [Signal(stock_code=bar.code, direction=Direction.BUY,
                           strength=0.6, reason=f"突破 {self.window} 日新高")]
        if bar.code in self._holding and bar.close < recent_max * 0.95:
            self._holding.discard(bar.code)
            return [Signal(stock_code=bar.code, direction=Direction.SELL,
                           strength=1.0, reason="跌破止损线")]
        return []
```

保存后无需任何注册，Agent / Web / MCP / CLI 都能立刻看到 `my_breakout`。

### 9.2 写一个选股器

`screeners/` 下新建 `.py`，继承 `BaseScreener`：

```python
# screeners/volume_surge.py
from quanti.models import BarData
from quanti.screener.base import BaseScreener


class VolumeSurgeScreener(BaseScreener):
    name = "volume_surge"
    description = "成交量异常放大且价格上涨"

    def init(self, config: dict) -> None:
        self.lookback = config.get("lookback", 20)
        self.vol_multi = config.get("vol_multi", 2.0)

    def screen(self, code: str, bars: list[BarData]) -> float:
        if len(bars) < self.lookback + 1:
            return 0.0
        latest = bars[-1]
        baseline = bars[-self.lookback-1:-1]
        avg_vol = sum(b.volume for b in baseline) / len(baseline)
        if latest.volume < avg_vol * self.vol_multi:
            return 0.0
        if latest.close <= baseline[-1].close:
            return 0.0
        return round((latest.volume / avg_vol) * (latest.close / baseline[-1].close - 1), 4)
```

### 9.3 让 Agent 用上

```bash
# CLI
quanti agent set-goal --strategy my_breakout

# 或 Web → AI Agent 页 → 策略下拉框 → 选 my_breakout

# 或 MCP
# set_goal({"strategy_name": "my_breakout", "params": {"window": 30}})
```

---

## 10. 数据同步与管理

### 10.1 首次部署

```bash
# 1) 同步股票列表（5000 只，~30 秒）
quanti sync --stocks

# 2) 同步交易日历
quanti sync --calendar

# 3) 同步 K 线（重活，全 A 股 1 年 ~10 分钟）
quanti sync --quotes
```

或者直接 `quanti up`：首次启动会自动后台同步股票列表。K 线随用随同步（screener / agent 都会自动 sync 缺数据的）。

### 10.2 增量同步

`sync_daily_quotes` 默认从 DB 里该股票的最新一天往后拉。所以每天跑一次 `quanti sync --quotes` 就是增量更新。

```bash
# 每日定时增量（cron）
0 18 * * 1-5 cd /opt/quanti && .venv/bin/quanti sync --quotes
```

### 10.3 数据源

主源 **AkShare** → 东方财富，备源 **新浪财经**。两边都拉不到才报错。`AkShareAdapter` 内置：

- 自动重试 3 次（指数退避）
- 跨源补缺口（detect_gaps + 用另一个源回填）
- 数据完整性校验（OHLC 合法、零价检查、重复日期）
- 行数合理性检查（实际行数 vs 期望行数）

### 10.4 数据库

SQLite，路径 `data/quanti.db`，WAL 模式，单文件。

主要表：
- `stocks` / `daily_quotes` / `trade_calendar`
- `stock_pools` / `pool_stocks`
- `sync_jobs`（同步进度）
- `portfolio_state` / `positions` / `orders` / `trades` / `portfolio_snapshots`
- `agent_goal` / `agent_decisions`

备份：

```bash
cp data/quanti.db data/quanti.db.bak.$(date +%Y%m%d)
```

### 10.5 决策日志保留

Agent 每次 tick 末尾自动调用 `prune_decisions(older_than_days)`，默认保留 90 天 (~3 个月)。配置项在 `AgentRuntime` 构造参数 `decision_retention_days`。

手动清理：

```bash
quanti agent prune --older-than-days 30   # 改成保留 30 天
# 或 API
curl -X POST 'http://127.0.0.1:8000/api/agent/decisions/prune?older_than_days=30'
# 或 MCP: prune_decisions({"older_than_days": 30})
```

切换到 PostgreSQL：暂未支持，路线图里有。

### 10.6 后台同步 daemon(2026-05,默认开启)

`quanti up` 启动时会自动起一个**独立的后台线程**(`BackgroundQuoteSyncer`)持续维护 `daily_quotes` 表的新鲜度。它与 Agent 的 4 小时 tick 完全解耦,**不会因为 agent 慢/停而影响数据同步**。

#### 工作机制

```
                每次循环
                  │
   ┌──────────────▼──────────────┐
   │ 1. 扫描 stocks 表           │
   │    优先级:                  │
   │      ① 持仓股票(防 PnL 冻结) │
   │      ② 完全没数据的股票      │
   │      ③ 数据过期(默认 > 1 天)│
   │    跳过 30 分钟内失败过的    │
   └──────────────┬──────────────┘
                  │
   ┌──────────────▼──────────────┐
   │ 2. 队列非空 → 批处理         │
   │    每批 5 只,每只间隔 0.5s  │
   │    (~2 只/秒,AkShare 不限速)│
   └──────────────┬──────────────┘
                  │
            队列空了
                  │
   ┌──────────────▼──────────────┐
   │ 3. 进 IDLE,30 分钟后重扫    │
   └─────────────────────────────┘
```

#### 性能数字

| 场景 | 完成时间 |
|---|---|
| 冷启动全 A 股(5519 只无数据) | ~25-40 分钟(网络/限速决定) |
| 日常稳态(只有 1-2 天 stale) | 持续运行,负载几乎为 0 |
| 持仓 30 只全 stale | ~30 秒 |

对比老路径:agent tick 每 4 小时 sync 20 只,冷启动需要 **40+ 天**。

#### 监控

**Dashboard 右上角"后台同步"卡**显示当前状态:
- 🟢 **同步中(active)**:队列中有 code,正在处理
- 🔵 **空闲(idle)**:数据全新鲜,30 分钟后重扫
- 🟡 **已暂停(paused)**:用户主动暂停
- ⚪ **已停止(stopped)**:守护线程未运行

active/paused 状态下,Dashboard 还会显示一条**进度条**,带当前 code / 队列剩余 / 已同步 / 失败计数 / 暂停-恢复按钮。Idle 时这条不显示以减少视觉噪音。

#### API

```bash
# 看状态
curl --noproxy '*' http://127.0.0.1:8000/api/sync/background/status | jq

# 暂停(腾出带宽给一次性大量 sync)
curl -X POST --noproxy '*' http://127.0.0.1:8000/api/sync/background/pause

# 恢复
curl -X POST --noproxy '*' http://127.0.0.1:8000/api/sync/background/resume
```

#### 关掉它

启动 server 时传 `autostart_background_sync=False`(目前需要改 `create_app()` 调用)。或者运行时 pause + 永远不 resume。
正常情况下没有理由关 —— 它持续维护**整个系统的数据基础**。

#### 与 user-triggered sync 的关系

| 路径 | 用途 | 速度 |
|---|---|---|
| `BackgroundQuoteSyncer`(本节) | 默默维护,长期 | 限速 ~2 只/秒,不阻塞 |
| `quanti sync --quotes`(CLI) | 一次性大量补 | 顺序同步,有 ETA 进度 |
| Web Dashboard "同步全 A 股池" | 一次性大量补 | 异步带进度条 |
| Agent tick `_ensure_recent_data`(保险丝) | 保证当下能用 | 每 tick 20 只 |

三套同时存在:**后台守护负责日常**,**用户触发负责急用**,**agent 内嵌负责兜底**。互不干扰,sync_jobs 表也按各自 job_id 隔离记录。

---

## 11. 回测

### 11.1 命令行

```bash
quanti backtest \
  --strategy ma_cross \
  --codes 600519,000001,000858 \
  --start 2025-01-01 \
  --end 2025-12-31 \
  --cash 1000000
```

输出包含：
- 总收益 / 年化 / 年化波动 / 最大回撤
- 夏普 / 索提诺 / Calmar
- 胜率 / 总交易次数

### 11.2 Web

`/backtest` 页面提供同样能力 + 净值曲线 + 交易明细表。

### 11.3 API

```bash
curl -X POST http://127.0.0.1:8000/api/backtest/run \
  -H 'Content-Type: application/json' \
  -d '{
    "strategy_name": "ma_cross",
    "codes": ["600519", "000001"],
    "start": "2025-01-01",
    "end": "2025-12-31",
    "initial_cash": 1000000,
    "params": {"short_period": 5, "long_period": 20}
  }'
```

### 11.4 与实盘的一致性

回测引擎 (`BacktestEngine`) 和模拟盘 (`PaperBroker`) 共用同一个 `RiskManager`、同一个 `AShareCommission` 模型、同一套 T+1 规则。**回测出来什么样，模拟盘就大概率什么样**。

2026-05 升级后,回测引擎默认启用**成交量加权滑点**(`VolumeImpactSlippage`),实盘 PaperBroker 仍是 flat 0.1%。要让回测更保守(更接近实盘 / 比实盘还差):用默认即可。要让回测更乐观(对比老版):传 `slippage=0.001`(自动包装成 FlatSlippage)。详见 §6.9.1。

---

## 12. 风控配置

默认 `RiskConfig`（在 `quanti/risk/manager.py`）：

| 字段 | 默认 | 说明 |
|------|------|------|
| `max_position_pct` | 0.10 | 单股不超过组合的 10% |
| `max_industry_pct` | 0.30 | 单行业不超过 30% |
| `stop_loss_pct` | -0.08 | 单股止损 -8% |
| `portfolio_stop_loss_pct` | -0.15 | 组合止损 -15% |
| `max_daily_trades` | 20 | 每日最多 20 笔 |
| `blocked_prefixes` | ("ST", "*ST") | 黑名单前缀 |

### 12.1 改风控

目前 PaperBroker 用默认配置。要改，编辑 `quanti/api/app.py`：

```python
from quanti.risk.manager import RiskConfig
app.state.broker = PaperBroker(
    db=db, provider=app.state.provider,
    initial_cash=initial_cash,
    risk_config=RiskConfig(
        max_position_pct=0.05,        # 改成 5%
        stop_loss_pct=-0.10,          # 改成 -10%
    ),
)
```

或者新加一个 `--risk-config` 参数走 yaml 配置（小改造）。

### 12.2 波动率目标仓位(opt-in,2026-05 升级)

风控提供仓位**上限**(单股 10%、行业 30%;总仓位上限已移除,允许满仓,仅受单股/行业上限约束)。`VolTargetSizer` 提供仓位**目标**:让低波股拿到更大权重、高波股更小,组合年化波动接近某个目标值。详见 §6.9.2。

二者不冲突:Sizer 给出的目标权重仍受 `RiskConfig.max_position_pct` 硬上限约束。Sizer 是 opt-in,默认 PaperBroker 行为不变。

---

## 13. 运维与排错

### 13.1 服务起不来

```bash
# 端口被占
lsof -i :8000          # 看谁占
quanti up --port 8001  # 换端口

# Python 依赖
.venv/bin/pip install -e ".[dev]" --upgrade

# 前端没编
cd web && npm run build
```

### 13.2 Agent 不动

检查：

```bash
# 状态
quanti agent status

# 看日志（如果 nohup 启动）
tail -f /tmp/quanti.log

# 强制跑一轮看错在哪
quanti agent tick
```

常见原因：
- 数据库为空 → `quanti sync --stocks` 先
- universe_pool 设了但 pool 里没股票 → 改回空 pool 或加股票
- AkShare 接口被限流 → 等几分钟重试

### 13.3 数据没更新

```bash
# 看最近一只股票的最新日期
sqlite3 data/quanti.db 'SELECT code, MAX(date) FROM daily_quotes GROUP BY code LIMIT 5;'

# 手动补
quanti sync --quotes --codes 600519
```

### 13.4 想清空所有交易记录重新来

Web → AI Agent → 重置组合按钮。

或 API：

```bash
curl -X POST 'http://127.0.0.1:8000/api/portfolio/reset?initial_cash=1000000'
```

或直接删整个数据库：

```bash
rm data/quanti.db*
quanti up  # 会重新初始化
```

### 13.5 测试

```bash
.venv/bin/python -m pytest tests -v
```

应该 809 passed(含实盘安全 / 日内闸门 / 订单幂等 / 因子挖掘 / hyperopt 等)。

### 13.6 日志

- Uvicorn 标准输出：HTTP 请求日志
- Agent 决策日志：写到 DB 表 `agent_decisions`，永久保存
- AkShare 同步：标准输出 + DB 表 `sync_jobs`

查决策日志：

```bash
quanti agent decisions --limit 100
# 或
sqlite3 data/quanti.db 'SELECT ts, kind, summary FROM agent_decisions ORDER BY id DESC LIMIT 20;'
```

---

## 14. 系统架构

### 14.1 模块依赖

```
       ┌──────────────────────────────────────────────────┐
       │                     CLI / MCP                     │
       └──────┬──────────────────────────┬─────────────────┘
              │                          │
              ▼                          ▼
       ┌─────────────┐           ┌──────────────────┐
       │  FastAPI    │           │  MCP stdio       │
       │  (api/app)  │           │  (mcp_server)    │
       └──────┬──────┘           └────────┬─────────┘
              │                           │
              ▼                           ▼
       ┌──────────────────────────────────────────┐
       │              AgentRuntime                 │
       │  (daily loop: tick → cycle)               │
       └──┬─────────────┬──────────────┬──────────┘
          │             │              │
          ▼             ▼              ▼
   ┌────────────┐  ┌──────────┐  ┌──────────────┐
   │  Strategy   │  │ Screener │  │  PaperBroker │
   │  Selector   │  │  Loader  │  │  或实盘 QmtBroker(直连 xtquant,已实现)
   └─────┬──────┘  └────┬─────┘  └──────┬───────┘
         │              │                │
         ▼              ▼                ▼
   ┌────────────┐  ┌──────────┐  ┌──────────────┐
   │ Backtest   │  │ Strategy │  │ RiskManager  │
   │ Engine     │  │  Loader  │  └──────┬───────┘
   └─────┬──────┘  └────┬─────┘         │
         │              │                │
         ▼              ▼                ▼
   ┌──────────────────────────────────────────┐
   │             DataProvider                  │
   └──────────────────┬───────────────────────┘
                      ▼
                  ┌──────────┐
                  │ Database │ (SQLite)
                  │  WAL     │
                  └────┬─────┘
                       ▲
                       │
                ┌──────┴───────┐
                │ AkShare      │
                │ Adapter      │ (东财 / 新浪)
                └──────────────┘
```

### 14.2 关键设计选择

| 决策 | 取舍 |
|------|------|
| SQLite 单文件 | 部署简单；不适合超大规模数据，分布式时考虑 PostgreSQL |
| 文件夹 + 动态加载 = 策略/选股器 | 写完就生效，零配置；但启动稍慢 |
| 风控同时在回测和实盘 | 行为一致；缺点是回测速度有 5~10% 折损 |
| Agent 用 threading 而非 asyncio | 因为策略代码可能阻塞，线程隔离更稳 |
| MCP 走 stdio 而非 HTTP | 标准 MCP 协议，OpenClaw / Claude Desktop 都用这个；安全模型简单 |
| DB 是事实记录，broker 持仓也写 DB | 万一进程崩溃，下次启动可恢复 |

---

## 附录

### A. 内置策略

| 名称 | 说明 |
|------|------|
| `ma_cross` | 短期 MA 上穿长期 MA 买入 |
| `macd_cross` | MACD 金叉买入 / 死叉卖出 |
| `rsi_ob_os` | RSI 超卖买入 / 超买卖出 |
| `bollinger_band` | 布林带突破策略 |
| `ma_volume` | 均线 + 成交量双确认 |
| `turtle_breakout` | 海龟交易法则突破 |

### B. 内置选股器

| 名称 | 说明 |
|------|------|
| `ma_trend` | 多头排列（短>中>长 MA） |
| `new_high` | 创 N 日新高 |
| `rsi_oversold` | RSI 超卖 |
| `volume_breakout` | 量价齐升 |

### C. 相关文档

- [`README.md`](../README.md) — 项目总览
- [`live-trading-runbook.md`](live-trading-runbook.md) — **实盘接入 / 运维手册(当前权威)**
- [`TODO-live-trading.md`](TODO-live-trading.md) — 早期接入 TODO(部分已被实盘接入工作取代,实盘现状以 runbook 为准)
- [`plans/2026-05-25-smart-quant-upgrade.md`](plans/2026-05-25-smart-quant-upgrade.md) — Phase 1-3 升级的设计文档 + 红线 + 验收 gate
- `docs/plans/` — 历史实现计划

### D. 反馈渠道

- Issue：项目仓库 issues
- PR：欢迎补充策略 / 选股器 / 文档
