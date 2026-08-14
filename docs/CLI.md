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
| [`doctor`](#doctor--系统体检) | 系统体检:退出覆盖/数据新鲜度/DB 完整性 |
| [`factor-watch`](#factor-watch--因子-ic-漂移体检) | 因子 IC 漂移体检:衰减/退役/无快照 |
| [`strategy-gate`](#strategy-gate--策略健康闸门) | 策略健康闸门:长窗回测剔除熔断/深亏策略 |
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
| `--financials` | 拉财务指标(ROE/同比,按公告日 PIT)。**默认 tushare `fina_indicator`**(真实 ann_date,需 2000 积分);`--source akshare` 用免费 `业绩报表`(全市场按报告期、额外含净利/营收绝对值、ann_date=法定截止日),按 `--years` 覆盖报告期 |
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

### Tushare 限频与积分(重要)

tushare 各接口按**积分**分档限频。门槛随积分变化(以报错信息为准):

| 接口 | 用于 | <2000 积分(实测低档) | 2000 积分 |
|------|------|----------|---------|
| `daily` | 日线行情(`--quotes`/`--backfill`) | 50 次/分钟 | 大幅提高(可调大 `--calls-per-min`) |
| `stock_basic` | 名册(`--stocks`) | 低至 1 次/小时(几乎不可用) | 可用(含退市股) |
| `daily_basic` | 估值(PE/PB/市值…) | ~1 次/分钟(5年不现实) | 可用 |
| `adj_factor` | 复权因子 | 1 次/小时 | 可用(但**仍不调用**——`pre_close` 重建更快省额度) |
| `fina_indicator` | 财报 | 无权限 | **可用**(`--financials` 默认即用) |

**默认全走 tushare**(2000 档够用):`--stocks` 名册、`--backfill` 行情+估值、`--financials` 财报。

**积分不足 / 想省 tushare 额度时**,以下都可改走**免费 akshare**:

- 名册:`quanti sync --stocks --source akshare`(无幸存者偏差:SH/SZ/BJ 在市 ~4900 + SH/SZ 退市 ~350,真实上市/退市日)。
- 财报:`quanti sync --financials --source akshare`(全市场按报告期,额外含净利/营收绝对值)。

**其它建议**:
- 回填期间别让其它进程抢同一 token(另一台机/notebook/同开的 Web 后台同步),否则 `daily` 配额被分食、频繁限频。必要时先 `/sync/background/pause` 或在 Web「数据源」面板暂停守护。
- `--calls-per-min` 按 token 的 `daily` 限额设(低档 ~50→设 80;2000+ 可设 400+)。
- 想要更高频次/分钟线:继续提升积分(见报错里的 doc_id=108 链接)。

> **2000 积分推荐(全 tushare)**:
> ```bash
> export TUSHARE_TOKEN=xxxx
> quanti sync --stocks                     # 名册(tushare,含退市股)
> quanti sync --backfill --years 5 --calls-per-min 400   # 行情 + 估值
> quanti sync --financials                 # 财报(tushare fina_indicator)
> ```
> **零成本组合(无积分)**:把 `--stocks` / `--financials` 加 `--source akshare`,行情仍用 tushare(`daily` 50/min)。

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

## `doctor` — 系统体检

三项只读检查:持仓策略离场覆盖(策略被移走 → 只剩止损/止盈)、数据新鲜度(每个代码的最新 bar vs 交易日历)、SQLite 完整性。发现问题退出码为 1(可接 cron 告警);后台同步守护每天 17:45 自动跑一次并把结果写进决策日志。

```bash
quanti doctor                      # 人类可读摘要
quanti doctor --json               # 机器可读
quanti doctor --codes 000001,600519  # 只体检指定代码的数据
```

---

## `factor-watch` — 因子 IC 漂移体检

把每日重评(`rescore_generated_factors`)沉淀的 IC 快照变成衰减决策:基线(历史 OOS IC 均值,不含最近窗口)vs 近期(最近 3 次快照),近期 < 基线一半 → 标记「衰减」;曾被闸通过、最新一次被拒 → 标记「退役」;已入库启用但尚无快照(本功能上线前的存量)→ 标记「无快照」。发现问题退出码 1;后台每日重评后若有问题,会以 `factor_watch` 决策条目落进审计流。

```bash
quanti factor-watch        # 人类可读
quanti factor-watch --json # 机器可读
```

---

## `strategy-gate` — 策略健康闸门

每个可加载策略跑 2 年长窗回测(默认风险:ATR 止损 + 组合熔断):触发 -30% 熔断 → 「breaker」剔除;无熔断但年化夏普 < 阈值(默认 -0.5)→ 「deep_loss」剔除;其余「pass」。判定写入 `strategy_gate` 表,**走查选股器将不再选用被剔除策略**(2026-08-14 勘误的机制化:短窗 OOS 夏普看不出组合级杀手,长窗熔断判定才看得见)。每日后台自动跑,剔除事件落决策日志;退出码 1 = 存在剔除。

```bash
quanti strategy-gate              # 人类可读
quanti strategy-gate --json       # 机器可读
quanti strategy-gate --lookback-days 365 --threshold -0.3
```

---

## 离线 LLM 评估(`scripts/llm_eval.py`)

把历史交易日重放给 LLM 决策层 vs 机械基线(生产融合口径:0.5×策略分 + 0.5×sigmoid(因子分))的**离线实验**——衡量「LLM 选的票」vs「机械排名的票」的未来收益,llm_full 模式上实盘前的信任前提。只读、不交易、不做任何账户写入(可选 `--log-decision` 落审计流)。

```bash
# 需要 DEEPSEEK_API_KEY(或 --provider anthropic)
python scripts/llm_eval.py --end 2026-07-31 --days 30 --k 5
```

关键参数:`--days` 评估日数(默认 30,每周一次采样)、`--stride` 采样间隔(默认 5 个交易日)、`--max-codes` 每日候选上限(默认 60,LLM 上下文友好)、`--horizon` 前视窗口(默认 5/10 个交易日)。LLM 输出经防御解析(限候选集、去重、≤K);解析失败有一次纠正重试,仍失败按当日错误记账——绝不静默回退。报告 JSON 落 `data/llm_eval_<date>.json`,摘要含各臂均值收益/胜率/重合度。

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
