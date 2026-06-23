# Quanti 命令行使用大全

`quanti` 是项目唯一的命令行入口(`pyproject.toml` → `quanti.cli:main`)。安装后即可全局调用:

```bash
pip install -e ".[dev,data]"   # data extra 带 tushare;dev 带测试/ruff
quanti <子命令> [参数]
```

子命令一览:

| 子命令 | 作用 |
|--------|------|
| [`sync`](#sync--数据同步) | 同步/回填/删除市场数据(行情、名册、日历、基本面、财报) |
| [`backtest`](#backtest--回测) | 跑单策略回测 |
| [`optimize`](#optimize--走查式调参) | 走查式参数寻优(防过拟合) |
| [`serve`](#serve--只起-web) | 只启动 Web 服务(不启 Agent) |
| [`up`](#up--一键启动) | 一键:数据 + 目标 + Web + Agent |
| [`agent`](#agent--命令行操作-agent) | 无 server 模式下观察/触发 Agent |
| [`mine-factors`](#mine-factors--llm-因子挖掘) | LLM 因子挖掘 |
| [`mcp`](#mcp--mcp-server) | 启动 MCP server(stdio,供 OpenClaw/Claude Desktop 接入) |

---

## 全局环境变量

这些环境变量影响所有子命令(无对应 CLI 参数,或作为默认值):

| 变量 | 默认 | 作用 |
|------|------|------|
| `QUANTI_ACCOUNT` | `paper` | 账户名 → 交易库 `data/<账户>.db`(持仓/订单/成交/目标/决策)。设 `live` 即切到 `data/live.db`。共享行情库 `data/market.db` 全账户共用 |
| `QUANTI_DATA_SOURCE` | (未设) | 历史源解析的一环,优先级:CLI `--source` > DB `app_config` > 本变量 > `tushare` |
| `TUSHARE_TOKEN` | (未设) | Tushare token;也可由 Web「数据源」面板存入 DB。tushare 选中但无 token → **报错不静默回退**,须显式 `--source akshare` |
| `DEEPSEEK_API_KEY` | (未设) | LLM 增强层 / 因子挖掘用 DeepSeek(默认供应商,零额外依赖) |
| `ANTHROPIC_API_KEY` | (未设) | LLM 供应商选 `anthropic` 时用(需 `pip install -e ".[llm]"`) |

> **数据分库**:交易状态按账户独立(`paper.db`/`live.db`),行情/基本面/财报在共享 `market.db`,同步一次全账户可用。

---

## `sync` — 数据同步

同步、批量回填、删除市场数据。多个开关可组合(按顺序执行);`--clear` 例外,单独执行后即返回。

| 参数 | 说明 |
|------|------|
| `--stocks` | 同步全 A 股股票列表(名称/行业/交易所/上市日;tushare 含退市股) |
| `--calendar` | 同步交易日历 |
| `--quotes` | 同步日线;配 `--codes` 限定,否则全市场 |
| `--codes 000001,600519` | 逗号分隔的股票代码(用于 `--quotes` / `--clear`) |
| `--refetch` | 全量重拉历史(覆盖旧数据)。从 qfq 切「原始价+复权因子」后须跑一次 |
| `--backfill` | 逐交易日全市场批量回填(含退市股,高效/可断点续);配 `--years` |
| `--years N` | 回填/财报覆盖年数,默认 **5** |
| `--financials` | 拉财务指标(ROE/净利/营收及同比,按公告日 PIT)—— **akshare 业绩报表,免费、无需 token**,按 `--years` 覆盖报告期 |
| `--source {tushare,akshare,xtdata}` | 指定历史源;默认按 `app_config > env > tushare`,无 token 时报错(不静默回退) |
| `--tushare-stocks` | 同步含退市股的全量名册(需 `TUSHARE_TOKEN`) |
| `--tushare-quotes` | 经 Tushare 同步日线(配 `--delisted-only` 只补退市股) |
| `--delisted-only` | 配 `--tushare-quotes`:只补退市股 |
| `--clear {quotes,daily_basic,financials,all}` | 删除已同步数据,**默认预演(dry-run 只报行数),加 `--yes` 才真删**;可配 `--codes` / `--source` 限定 |
| `--yes` | 确认执行删除(配合 `--clear`;不加则只预演) |

示例:

```bash
# 冷启动:股票列表 + 交易日历
quanti sync --stocks
quanti sync --calendar

# 5 年全市场回填(含退市股,自动清异源旧数据 → 单源一致)
export TUSHARE_TOKEN=xxxx
quanti sync --backfill --years 5

# 财报(免费 akshare,无需 token,默认覆盖 5 年报告期)
quanti sync --financials
quanti sync --financials --years 8

# 同步指定股票(用显式源)
quanti sync --quotes --codes 000001,600519 --source tushare

# 切「原始价+复权因子」口径后重置(同源覆盖)
quanti sync --quotes --refetch

# 删除数据(先预演再 --yes)
quanti sync --clear all                              # 预演:报各表行数
quanti sync --clear all --yes                        # 实删行情+估值+财报
quanti sync --clear quotes --codes 000001 --yes      # 只删某票日线
quanti sync --clear quotes --source akshare --yes    # 只删某源日线
```

> **复权**:`daily_quotes` 存原始价 + 每日 `adj_factor`;tushare 路径由 `daily` 的 `pre_close` 重建因子(不调限频的 `adj_factor` 接口)。读时后复权(hfq),下单/图表用原始价。
> **一票一源**:`save_daily_quotes` 默认拒绝把异源 bar 拼到同一只票;`--backfill` 开跑前一次性清异源历史做干净迁移。

---

## `backtest` — 回测

事件驱动回测,模拟 A 股 T+1、涨跌停、佣金印花税、滑点对齐实盘。

| 参数 | 必填 | 默认 | 说明 |
|------|:--:|------|------|
| `--strategy` | ✓ | — | 策略名(`strategies/` 下自动发现) |
| `--codes` | | (无) | 逗号分隔代码;`--survivorship-free` 时忽略 |
| `--start` | ✓ | — | 起始日 `YYYY-MM-DD` |
| `--end` | ✓ | — | 截止日 `YYYY-MM-DD` |
| `--cash` | | `1000000` | 初始资金 |
| `--survivorship-free` | | off | 在按日期时点正确、含退市股的宇宙上回测(替代 `--codes`) |
| `--max-universe N` | | `300` | 无幸存者偏差宇宙规模上限 |

```bash
quanti backtest --strategy ma_cross --codes 000001,600519 \
  --start 2024-01-01 --end 2024-12-31 --cash 1000000

# 无幸存者偏差(需先回填含退市股的历史)
quanti backtest --strategy ma_cross --start 2021-01-01 --end 2022-12-31 \
  --survivorship-free --max-universe 300
```

---

## `optimize` — 走查式调参

对声明了 `param_space` 的策略做网格搜参 + 多折样本外(OOS)夏普验证,**跑赢默认才采纳**,自动防过拟合。采纳结果存各账户库的 `strategy_params`。

| 参数 | 默认 | 说明 |
|------|------|------|
| `--universe` | (无) | 股票池名;留空用 `goal.universe_pool` 或全市场 |
| `--end` | 今天 | 优化截止日 `YYYY-MM-DD` |
| `--cash` | `1000000` | 初始资金 |

```bash
quanti optimize
quanti optimize --universe my_pool --end 2024-12-31
```

---

## `serve` — 只起 Web

启动 FastAPI + Vue 仪表盘,**不自动启动 Agent**。

| 参数 | 默认 |
|------|------|
| `--host` | `127.0.0.1` |
| `--port` | `8000` |
| `--cash` | `1000000`(仅首次创建组合时生效) |

```bash
quanti serve                 # → http://127.0.0.1:8000
quanti serve --port 9000
```

---

## `up` — 一键启动

确保数据库/股票列表就绪 → 按需设目标 → 起 Web + Agent。

| 参数 | 默认 | 说明 |
|------|------|------|
| `--host` | `127.0.0.1` | |
| `--port` | `8000` | |
| `--cash` | `1000000` | 初始资金(仅首次创建组合时生效) |
| `--target` | (不改) | 目标年化收益(如 `0.20` = 20%) |
| `--max-drawdown` | (不改) | 可接受最大回撤,负数(如 `-0.20`) |
| `--risk {low,medium,high}` | (不改) | 风险偏好 |
| `--pool` | (无) | 候选股票池 |
| `--screener` | (无) | 选股器名 |
| `--strategy` | (自动) | 指定策略名,留空由 Agent 自动挑选 |
| `--no-agent` | off | 只起 Web,不启 Agent |

```bash
quanti up --target 0.20 --max-drawdown -0.20 --risk medium
quanti up --no-agent          # 等价于 serve,但会确保股票列表就绪
```

---

## `agent` — 命令行操作 Agent

无 server 模式下观察或触发 Agent(操作的是 `QUANTI_ACCOUNT` 对应的账户库)。

```bash
quanti agent <action> [参数]
```

| action | 作用 | 相关参数 |
|--------|------|---------|
| `tick` | 在本地强制跑一轮 Agent 周期(同步→选股→评估→风控→模拟下单→决策日志),打印 JSON | `--cash` |
| `status` | 打印 Agent 状态(启用/运行/上次 tick/总值/盈亏) | |
| `goal` | 打印当前目标(JSON) | |
| `set-goal` | 修改目标(只改传入的项) | `--target` / `--max-drawdown` / `--risk` |
| `decisions` | 打印最近决策日志 | `--limit`(默认 20) |
| `prune` | 删除超期决策日志 | `--older-than-days`(默认 90) |

其它参数:`--cash`(默认 `1000000`)、`--target`、`--max-drawdown`、`--risk {low,medium,high}`、`--limit`、`--older-than-days`。

```bash
quanti agent status
quanti agent tick
quanti agent goal
quanti agent set-goal --target 0.25 --risk high
quanti agent decisions --limit 50
quanti agent prune --older-than-days 90
```

---

## `mine-factors` — LLM 因子挖掘

让 LLM 提截面 alpha 表达式 → 安全解析 → 训练/OOS rank-IC 闸门去冗余 → 采纳入自演化因子库(默认不参与实盘排序,需账户开关 `use_generated_factors`)。复用 LLM 增强层的供应商配置(`DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY`)。

| 参数 | 默认 | 说明 |
|------|------|------|
| `--universe` | (无) | 股票池名;留空用全市场 |
| `--n` | `10` | 提多少个候选因子 |
| `--end` | 今天 | 截止日 `YYYY-MM-DD` |
| `--cash` | `1000000` | |

```bash
quanti mine-factors --n 10
quanti mine-factors --universe my_pool --n 20 --end 2024-12-31
```

---

## `mcp` — MCP server

以 stdio JSON-RPC 启动 MCP server,供 OpenClaw / Claude Desktop / Cursor 接入(暴露目标读写、Agent 控制、账户视图、试跑回测/选股、数据同步等工具)。

```bash
quanti mcp
```

OpenClaw / Claude Desktop 配置示例:

```json
{
  "mcpServers": {
    "quanti": { "command": "quanti", "args": ["mcp"] }
  }
}
```

---

## 典型工作流

**① 全新环境冷启动 + 5 年回填**
```bash
pip install -e ".[dev,data]"
export TUSHARE_TOKEN=xxxx
quanti sync --stocks                 # 名册(含退市股)
quanti sync --calendar               # 交易日历(节假日精度)
quanti sync --backfill --years 5     # 5 年全市场行情(含退市股)
quanti sync --financials             # 财报(免费 akshare)
```

**② 跑无幸存者偏差回测**
```bash
quanti backtest --strategy ma_cross --start 2021-01-01 --end 2023-12-31 \
  --survivorship-free --max-universe 300
```

**③ 目标驱动自治(Web + Agent)**
```bash
quanti up --target 0.20 --max-drawdown -0.20 --risk medium
# 浏览器打开 http://127.0.0.1:8000
```

**④ 调参 + 因子挖掘**
```bash
quanti optimize --universe my_pool
quanti mine-factors --universe my_pool --n 20
```

**⑤ 数据迁移 / 清理**
```bash
# 换源(akshare→tushare):无需手动删,回填自动清异源
quanti sync --backfill --years 5

# 彻底清某表(先预演)
quanti sync --clear financials
quanti sync --clear financials --yes

# 实盘账户(独立库)
QUANTI_ACCOUNT=live quanti agent status
```

---

> 更多设计细节见 [README](../README.md)(复权口径、一票一源、LLM 增强层、风控 protections、走查调参等)。
