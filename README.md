# Quanti - A股量化交易系统

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Quanti 是一个面向中国 A 股市场的开源量化交易系统，提供从**数据采集、因子计算、策略编写、回测验证**到 **Web 可视化**的完整工作流。

![Dashboard](docs/screenshots/dashboard.png)

## 特性

| 模块 | 功能 |
|------|------|
| **数据采集** | 可切换数据源（默认 Tushare 含退市股，支持 AkShare/xtdata），按 显式>DB配置>env>默认 解析；无 token 时报错提示配置 token 或显式切到 AkShare（不静默换源）；自动同步全A股行情，SQLite 本地存储 |
| **后台同步守护** | 常驻增量同步：收盘后自动全市场扫一轮，盘中安静；停牌股/死数据源指数退避 |
| **股票池管理** | 创建/删除股票池，批量添加/移除股票，一键同步K线数据 |
| **技术因子** | 内置 MA/EMA/RSI/MACD/布林带/ATR/ADX 等技术指标；横截面因子覆盖全维度 —— 动量/反转/波动/流动性(量价) + 估值/股息/规模/质量/成长(PIT 基本面),支持自定义 |
| **因子 DSL（防前视）** | 声明式因子表达式（`Ref/Mean/Std/Sum/Max/Min/Log` + OHLCV/换手率，及按公告日 PIT 合并的基本面字段 pe/pe_ttm/pb/ps/ps_ttm/total_mv/circ_mv/dv_ratio/roe/netprofit_yoy/revenue_yoy）；结构上禁止引用未来数据，安全白名单解析（无 `eval`） |
| **策略框架** | 继承 `BaseStrategy` 即可编写策略，动态加载无需配置 |
| **回测引擎** | 事件驱动，模拟A股T+1规则、涨跌停、佣金印花税 |
| **走查式调参** | 网格搜参 + 多折样本外（OOS）夏普验证，跑赢默认才采纳，自动防过拟合 |
| **AI Agent** | 目标驱动自治循环：同步→选股→策略评估→风控→模拟下单→决策日志；支持每日定时运行 |
| **LLM 因子挖掘** | LLM 提因子表达式 → 安全解析 → 训练/OOS rank-IC 闸门去冗余 → 采纳入自演化因子库，可选注入实盘排序 |
| **LLM 增强层** | 新闻情绪 / 多空辩论 / 风控三角 / 反思记忆（DeepSeek 或 Anthropic，默认关闭，只做加法） |
| **行情状态检测** | 趋势/震荡/高波动分类（observe-only），写入决策日志供 LLM 与人参考 |
| **可组合风控** | 硬限（单票/行业/止损/组合熔断）之上叠加 StoplossGuard / MaxDrawdown 软锁：连续止损或回撤超阈则临时锁新仓 |
| **模拟盘** | PaperBroker：挂单 T+1 次日开盘成交、涨跌停/一字板不可成交闸门、TTL 过期、止损、A股费用模型 |
| **Web 仪表盘** | FastAPI + Vue 3，K线图表、回测曲线、选股结果、Agent 决策可视化 |
| **选股器** | 可扩展的选股插件框架，支持自定义选股逻辑 |
| **MCP server** | stdio JSON-RPC，Claude Desktop / OpenClaw / Cursor 即插即用 |

## 快速开始

### 环境要求

- Python 3.11+
- Node.js 18+（开发前端可选）

### 安装

```bash
git clone https://github.com/coo-moon/quanti.git
cd quanti
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,data]"   # data extra 含 tushare(默认历史源)
```

> **Windows**:`pip install` 后若提示 `quanti` 不是命令(`quanti.exe` 所在 `...\PythonXXX\Scripts` 不在 PATH),两种解决:
> - 直接用模块跑(免配 PATH):`python -m quanti.cli sync --stocks`
> - 或把那个 `Scripts` 目录加进用户 PATH(PowerShell,改完**重开终端**):
>   ```powershell
>   [Environment]::SetEnvironmentVariable("Path", $env:Path + ";<上面提示的 Scripts 路径>", "User")
>   ```
> 前端构建在 `web/` 目录:先 `npm install`(装 `npm-run-all2` 等,提供 `run-p`)再 `npm run build`。

### 一键同步全A股数据

```bash
# 同步股票列表（含名称、交易所、所属行业）
quanti sync --stocks

# 启动服务，Web界面下载K线
quanti serve
# 访问 http://127.0.0.1:8000
```

### 运行回测

```bash
quanti backtest \
  --strategy ma_cross \
  --codes 000001,600519 \
  --start 2024-01-01 \
  --end 2024-12-31 \
  --cash 1000000
```

## Web 界面

启动 `quanti serve` 后访问 http://127.0.0.1:8000：

| 页面 | 功能 |
|------|------|
| **Dashboard** | 全局股票池统计、真实数据最新日期、后台同步状态、一键下载K线（含进度条+ETA）、**数据源切换面板（选源 tushare/akshare/xtdata + 填 Token + 测试连接 + 保存校验）** |
| **AI Agent** | 目标设定（含每日定时运行）、启停/手动 tick、**参数优化**（走查调参）与 **因子挖掘 (LLM)** 面板、LLM 增强层开关、持仓与决策日志、手动下单 |
| **股票池** | 创建/管理股票池，池内股票同步K线，实时进度 |
| **选股器** | 选择选股策略，设定参数，运行选股得到评分排名 |
| **回测** | 选择策略与股票，设置参数，一键回测并查看绩效曲线 |
| **关于** | 项目介绍与依赖版本 |

## 系统架构

```
quanti/
├── models.py                 # 核心模型 (BarData, Signal, Order, Portfolio)
│
├── data/                     # 数据层
│   ├── database.py           #   SQLite 存储（线程安全）+ sync_jobs 任务追踪
│   ├── provider.py           #   统一数据接口
│   ├── source.py             #   数据源解析（显式>DB>env>默认tushare）+ 适配器工厂 + 连通性探测
│   ├── akshare_adapter.py    #   AkShare 适配器（多源回退 + 增量同步）
│   ├── tushare_adapter.py    #   Tushare 适配器（含退市股名册 + 原始价+adj_factor + 按日期回填 + daily_basic/财务）
│   ├── xtdata_adapter.py     #   xtdata（QMT）适配器（经 qmt-bridge）
│   ├── backfill.py           #   5 年逐日全市场回填（断点续 backfill_progress + 限速）
│   └── background_sync.py    #   后台同步守护（交易时段感知 + 指数退避）
│
├── agent/                    # AI Agent
│   ├── runtime.py            #   自治循环（tick 调度、universe、模式分发）
│   ├── goal.py               #   目标模型（年化/回撤/风险偏好/params）
│   ├── selector.py           #   StrategySelector：按目标回测挑最佳策略
│   ├── walk_forward.py       #   走查验证（多折 OOS 切分）
│   ├── hyperopt.py           #   走查式调参（网格搜参 + OOS 采纳闸门）
│   ├── factor_miner.py       #   LLM 因子挖掘（提因子→IC 闸门→落库）
│   ├── universe.py           #   可交易宇宙（元数据/流动性/ADV20，时点正确）
│   ├── signal_pipeline.py    #   策略集成 + 因子 + 情绪三方融合
│   ├── llm_runtime.py        #   LLM 判断层（交易员/多空辩论/风控三角）
│   ├── sentiment.py          #   ①新闻情绪 overlay（批量打分+缓存）
│   ├── reflection.py         #   ④反思记忆（已实现盈亏 FIFO 配对）
│   ├── regime.py             #   行情状态检测（ER/波动率分位/广度）
│   └── openai_compat.py      #   DeepSeek 等 OpenAI 兼容供应商适配
│
├── factors/                  # 因子引擎
│   ├── registry.py           #   因子注册表
│   ├── technical.py          #   技术指标实现（含 ADX）
│   ├── expr.py               #   防前视因子 DSL（Ref/Mean/Std/... 节点）
│   ├── parser.py             #   安全白名单解析器（字符串→Expr，无 eval）
│   ├── library.py            #   内置因子表达式 + FactorFn 适配
│   ├── evaluation.py         #   rank-IC（训练/OOS 信息系数）
│   └── cross_sectional.py    #   横截面因子与复合排名（可并入生成因子）
│
├── strategy/                 # 策略框架
│   ├── base.py               #   策略基类
│   └── loader.py             #   动态策略加载器
│
├── backtest/                 # 回测引擎
│   ├── engine.py             #   事件驱动核心
│   ├── commission.py         #   A股费用模型
│   └── metrics.py            #   夏普/最大回撤/年化收益
│
├── execution/                # 执行层
│   └── paper_broker.py       #   模拟盘（挂单 T+1 生命周期 + 止损 + 快照）
│
├── screener/                 # 选股器框架
│   ├── base.py               #   选股器基类
│   └── loader.py             #   动态加载器
│
├── risk/                     # 风控模块
│   ├── manager.py            #   硬限制：单票/行业/止损/组合熔断（LLM 不可逾越）
│   └── protections.py        #   可组合软锁：StoplossGuard + MaxDrawdown（仅锁新仓）
│
├── api/                      # Web API
│   ├── app.py                #   FastAPI 应用
│   └── routes.py             #   路由定义
│
├── mcp_server.py             # MCP server（stdio JSON-RPC）
└── cli.py                    # 命令行入口

web/src/views/                 # Vue 3 前端
├── Dashboard.vue             #   主页仪表盘（含后台同步状态）
├── Agent.vue                 #   AI Agent：目标、LLM 增强层开关、决策日志
├── Pool.vue                  #   股票池管理
├── Screener.vue              #   选股器
├── Backtest.vue              #   回测页面
└── AboutView.vue             #   关于页面
```

## 编写自定义策略

在 `strategies/` 目录下创建文件，继承 `BaseStrategy`：

```python
from quanti.strategy.base import BaseStrategy
from quanti.models import BarData, Direction, Signal


class MyStrategy(BaseStrategy):
    name = "my_strategy"
    params = {"fast_period": 5, "slow_period": 20}

    def init(self, params: dict) -> None:
        self.fast = params["fast_period"]
        self.slow = params["slow_period"]
        self.position = None

    def on_bar(self, bar: BarData) -> list[Signal]:
        # 示例：简单均线交叉策略
        if self.position is None and bar.close > bar.ma(self.fast):
            return [Signal(stock_code=bar.code, direction=Direction.BUY, strength=0.8)]
        elif self.position == Direction.BUY and bar.close < bar.ma(self.slow):
            return [Signal(stock_code=bar.code, direction=Direction.SELL, strength=1.0)]
        return []
```

策略文件自动被发现，无需任何配置。

## 编写自定义选股器

在 `screeners/` 目录下创建文件：

```python
from quanti.screener.base import BaseScreener
import pandas as pd


class MyScreener(BaseScreener):
    name = "my_screener"
    description = "我的选股器"

    def screen(self, code: str, bars: list) -> float:
        """返回评分（越高越好），返回 0 表示不入选"""
        if len(bars) < 20:
            return 0
        recent = pd.DataFrame([{"close": b.close, "volume": b.volume} for b in bars[-20:]])
        # 示例：成交量放大且股价上涨
        vol_ratio = recent["volume"].iloc[-1] / recent["volume"].mean()
        price_change = (recent["close"].iloc[-1] - recent["close"].iloc[0]) / recent["close"].iloc[0]
        if vol_ratio > 1.5 and price_change > 0.05:
            return vol_ratio * price_change * 100
        return 0
```

## 编写自定义因子（Factor DSL）

因子用一套**防前视** DSL 声明——所有算子只能向后看，结构上无法引用未来数据；字符串形式经白名单解析（绝不 `eval`），是 LLM 生成因子的安全边界。

- **数据字段**：`close / open / high / low / volume / turnover`；以及按公告日 (ann_date) point-in-time 合并的基本面字段 `pe / pe_ttm / pb / ps / ps_ttm / total_mv / circ_mv / dv_ratio / roe / netprofit_yoy / revenue_yoy`（无对应数据的股票求值为 NaN，安全降级，不会引发前视）
- **函数**：`Ref(x, n)`（滞后 n 根，n≥0）、`Mean/Std/Sum/Max/Min(x, n)`（n 根滚动窗口）、`Log(x)`；算子 `+ - * /` 与一元负号（除零→NaN）
- **防前视**：`Ref` 禁止负 shift、滚动窗口止于当前行 → 日期 t 的因子值只依赖 ≤t 的数据
- **基本面（防前视合并）**：基本面字段在截面面板生成时按 point-in-time 合并——估值类（daily_basic：pe/pb/总市值等）按同日 `date` 对齐；财报类（roe/netprofit_yoy/revenue_yoy）用 `merge_asof(direction="backward")` 仅匹配公告日 `ann_date ≤ 当前 bar 日期` 的最新一期，因子在 t 日只看见 ≤t 已公告的财报，结构上无前视。无基本面数据时整段合并自动跳过，引用基本面的因子降级为 NaN。

```python
from quanti.factors.expr import Close, Ref, Mean, Std, Log
from quanti.factors.parser import parse_expr

# 1) Python 对象写法
reversal_1w = -(Close() / Ref(Close(), 5) - 1)            # 一周反转
realized_vol = -Std(Log(Close() / Ref(Close(), 1)), 20)   # 已实现波动（取负）

# 2) 字符串写法（白名单安全解析，LLM 因子走这条）
expr = parse_expr("-Mean(turnover, 20)")                  # 低换手异象
```

内置因子见 `quanti/factors/library.py`；这套 DSL 同时支撑下文的 [LLM 因子挖掘](#llm-因子挖掘)。

## 数据同步命令

| 命令 | 说明 |
|------|------|
| `quanti sync --stocks` | 同步全A股列表（名称/行业/交易所） |
| `quanti sync --calendar` | 同步交易日历 |
| `quanti sync --quotes --codes 000001,600519` | 同步指定股票K线 |
| `quanti sync --quotes --refetch` | 全量重拉历史(覆盖旧数据)——从 qfq 切到「原始价+复权因子」后须跑一次 |
| `quanti sync --tushare-stocks` | 同步含退市股的全量名册（需 `TUSHARE_TOKEN`，可选依赖 `.[data]`） |
| `quanti sync --tushare-quotes --delisted-only` | 补拉退市股历史行情（用于无幸存者偏差回测） |
| `quanti sync --backfill --years 5` | 逐交易日全市场批量回填（含退市股，高效/可断点续）；需 `TUSHARE_TOKEN` |
| `quanti sync --financials` | 拉财务指标（ROE/同比，按公告日 PIT）。默认 **tushare `fina_indicator`**（真实 ann_date，需 2000 积分）；`--source akshare` 用免费 `业绩报表`（额外含净利/营收绝对值） |
| `quanti sync --source {tushare,akshare,xtdata}` | 指定历史源；默认按 DB app_config > env `QUANTI_DATA_SOURCE` > tushare，无 token 时报错（不静默回退，须显式 `--source akshare`） |
| `quanti sync --clear {quotes,daily_basic,financials,all}` | 删除已同步数据，**默认预演(dry-run 只报行数)，加 `--yes` 才真删**；可配 `--codes` 限定股票、`--source` 限定行情源；全量清 quotes 时同步重置回填断点 |
| `quanti serve` | 启动 API 服务（端口 8000） |

> **复权口径(Qlib 式)**:`daily_quotes` 存**原始价(不复权)+ 每日复权因子 `adj_factor`**(=后复权/原始);akshare/tushare/xtdata 三源同一口径。研究/回测/因子/策略由 `DataProvider` 读时**后复权(hfq,连续、跨除权无假跳变、可复现)**,实盘下单与图表展示用**原始价**。因子锚定上市首日 → 增量同步安全。旧库是 qfq,升级后跑一次 `quanti sync --quotes --refetch` 重置口径。

> **数据源配置**:数据源可经三处切换(优先级 显式 CLI `--source` > DB `app_config`(Web/UI 设置) > env `QUANTI_DATA_SOURCE` > 默认 `tushare`);Tushare token 经 DB `app_config.data_source_token` > env `TUSHARE_TOKEN` 解析;tushare 未装或无 token 时**不静默回退**,统一报错(`DataSourceUnavailable` / UI 错误提示),要用 akshare 须显式切源——避免不同口径/纵深的源污染 DB、破坏「一票一源」。Web 端在 **Dashboard → 数据源** 面板可选源 / 填 token / 测试连接 / 保存校验。

> **一票一源守卫**:`daily_quotes` 每行带 `source`,`save_daily_quotes` **默认拒绝把不同源的 bar 拼到已有序列上**(跳过+告警,不静默混源——不同源单位/复权约定不同,拼接会污染价序),除非显式 `allow_source_mix=True`。换源是**迁移**而非混用:`quanti sync --backfill` 开跑前会一次性 `purge_other_source_quotes` 清除异源历史,之后整库单源一致。日更/增量遇到异源股票会跳过并提示先跑 `--backfill` 迁移。

### 无幸存者偏差回测 (survivorship-free)

```bash
# 1) 含退市股的全量名册 + 5 年历史(需 TUSHARE_TOKEN;PowerShell: $env:TUSHARE_TOKEN="...")
export TUSHARE_TOKEN=...
quanti sync --stocks                   # 名册(tushare stock_basic,含退市股,需 2000 积分)
quanti sync --backfill --years 5       # 逐交易日全市场回填(含退市股,可断点续)
#   ↑ 积分不足?名册可免费走 akshare: quanti sync --stocks --source akshare

# 2) 在"按日期时点正确、含退市股"的宇宙上回测
quanti backtest --strategy my_strat --start 2021-01-01 --end 2022-12-31 --survivorship-free
```

> 退市股**历史行情**只能靠 tushare 逐日 `daily`(每天返回全市场,含当天在市后退市的票);akshare 的免费源(东财/新浪)退市即下架,只提供退市股**名册**,不提供其历史行情。

> **批量回填与财务同步**:`quanti sync --backfill [--years N]`(默认 5 年)按交易日逐日全市场回填(`quanti/data/backfill.py` `run_backfill`),每个交易日约 2 次调用(`pro.daily + pro.daily_basic`)。**复权因子不再调 `adj_factor` 接口**(低积分档限 1 次/分钟,是回填瓶颈),改由 `pro.daily` 自带的 `pre_close` 重建(`tushare_adapter.reconstruct_adj_factor`:`f[t]=f[t-1]·close[t-1]/pre_close[t]`,增量同步用上一根已存 bar 续接、接缝无跳变),只用限额宽松的 `daily`(500 次/分钟)。断点续传(`backfill_progress` 表记录已完成交易日,重跑自动跳过)、按 `calls_per_min` 限速、含退市股、需 Tushare token 且**不静默回退 akshare**——比 per-stock 串行的 `--tushare-quotes --delisted-only` 明显更快。后台同步守护对 tushare 改走**逐日 top-up**(整市场一天 ≈2 次调用,而非逐股两次×数千只),避免触发频率限制。另有 `quanti sync --financials`(默认 5 年报告期)从 **akshare 业绩报表**(免费、无需 token,按报告期全市场一次拉取)灌入 `financials` 表:ROE、净利润/营收**绝对值**及同比增速。`ann_date` 取该报告期的**法定披露截止日**(Q1→04-30、中报→08-31、Q3→10-31、年报→次年 04-30),而非 akshare 不可靠的「最新公告日期」字段——这样 `merge_asof(ann_date)` 严格防前视(财报必在截止日前公开,保守不漏未来)。财报与行情源正交(`financials` 独立表,不受「一票一源」约束),故 tushare 行情 + akshare 财报可干净共用。

手动同步通常不需要：服务启动后**后台同步守护**（`BackgroundQuoteSyncer`）会持续维护数据新鲜度——

- **交易时段感知**：交易日 15:30（收盘宽限）后期待当天的 bar，自动触发一轮全市场增量同步（每只通常 1-2 根）；盘中与周末保持安静，不做无意义重拉；
- **增量拉取**：已有数据的股票从最新 bar 续拉，只有空白股票付一次约一年的冷启动成本；
- **退避机制**：硬失败（数据源不覆盖、网络异常）按 30 分钟 → 1h → 2h → 4h 封顶指数退避；同步成功但无新数据（停牌股）平退避 30 分钟，不会无限循环；
- 优先级：挂单股 > 持仓股 > 无数据 > 数据过期；状态可在 Dashboard 实时查看（含退避中数量）。

## 配置

数据按账户分库：
- **交易库** `data/<账户>.db`（默认 `data/paper.db`）—— 组合/持仓/订单/成交/目标/决策/调优参数/生成因子，每个账户独立。
- **共享行情库** `data/market.db` —— 股票列表/K线/交易日历/股票池/情绪，全账户共用，同步一次即可。

账户名由环境变量 `QUANTI_ACCOUNT` 决定（默认 `paper`）；设 `QUANTI_ACCOUNT=live` 即切到 `data/live.db`。旧的单文件 `data/quanti.db` 可用 `python scripts/migrate_split_db.py` 一键拆分（自动备份为 `.bak`）。

## 技术栈

| 组件 | 技术 |
|------|------|
| 数据源 | [Tushare](https://tushare.pro)（默认，可选依赖 `.[data]`）/ [AkShare](https://github.com/akfamily/akshare)（需显式切换，不静默回退）/ xtdata（QMT 实时/历史） |
| 存储 | SQLite + WAL 模式 |
| 后端 | FastAPI + Uvicorn + Pydantic |
| 前端 | Vue 3 + TypeScript + ECharts + Axios |
| 策略/因子 | Python 3.11+ |
| LLM（可选） | DeepSeek（OpenAI 兼容，httpx 直连零依赖）/ Anthropic SDK |
| 测试 | pytest + pytest-asyncio |

## 运行测试

```bash
pytest tests/ -v
```

## AI Agent 模式 — 开箱即用

一行命令：设定目标，剩下交给 Agent。

```bash
quanti up --target 0.20 --max-drawdown -0.20 --risk medium
```

- 首次运行后台异步拉取股票列表，Web 立即可用 → http://127.0.0.1:8000
- Agent 自治循环：每个 tick 完成 *同步数据 → 选股 → 策略评估 → 信号生成 → 风控 → 模拟下单 → 写决策日志*
- 没指定 `--strategy` 时，Agent 用 `StrategySelector` 把所有策略按目标做近 1 年回测，挑得分最高的
- 运行模式可选：纯规则 / `ensemble`（Top-K 策略融合 + 横截面因子）/ `llm`（在 ensemble 产出上叠加 LLM 判断层，见下节）
- 全部持久化在按账户分库的 `data/paper.db`（交易状态）+ 共享 `data/market.db`（行情）：组合、持仓、订单、成交、目标、决策

在 Web → **AI Agent** 页面可以：编辑目标、启停 Agent、立即跑一轮、查看持仓与决策日志、手动下单覆盖。

### LLM 多智能体增强层（可选，默认关闭）

设计哲学：**规则优先，LLM 只做加法**。回测、因子打分、风控全部保持确定性可审计；LLM 的职责限定为对系统产出的候选做"判断"——倾斜排序、收缩仓位、补充上下文。硬风控（单票 10% / 行业 30% / 止损线 / 组合 -15% 熔断）LLM 永远无法绕过；全部开关默认关闭，关闭时行为与纯规则路径完全一致。

| 层 | goal params 开关 | 作用 |
|------|------|------|
| ① 新闻情绪 | `sentiment_enabled` + `sentiment_blend`(0~1) | 抓取个股近 7 天新闻标题，LLM 批量打分 [-1,1]，按权重融入买入信号；按（股票, 日期）缓存，不重复计费 |
| ② 多空辩论 | `llm_debate` + `llm_debate_rounds` | 多头/空头研究员就候选清单辩论 N 轮，辩论稿进入交易员上下文，由其作为"研究主管"裁决 |
| ③ 风控三角 | `llm_risk_debate` | 激进/中性/保守三视角对每笔提议投"保留比例"，按风险偏好聚合（low→最小 / medium→均值 / high→最大），**只能缩仓或否决，不能加仓** |
| ④ 反思记忆 | `llm_reflection` + `llm_max_reflections` | 已实现盈亏 FIFO 配对成历史回合，按相关度（同股 > 同行业）注入上下文，让 LLM 带着"上次的教训"决策 |
| 行情状态 | `regime_detect` | 等权合成指数 + Kaufman ER + 波动率分位 + 广度 → 趋势上行/下行/震荡/高波动，observe-only 写决策日志 |

**供应商**：`llm_provider` 支持 `deepseek`（默认模型 `deepseek-v4-pro`，OpenAI 兼容接口，零额外依赖，`export DEEPSEEK_API_KEY=...` 即可）和 `anthropic`（`pip install -e ".[llm]"` + `ANTHROPIC_API_KEY`）。`claude-*` 模型名在 DeepSeek 路径下自动重映射；v4 思考模式与强制工具调用的兼容性已在客户端处理（结构化输出自动关思考，辩论等自由文本保留思考）。

开关入口三选一：Web **AI Agent → LLM 增强层**面板 / MCP `set_goal` 的 `params` / `quanti agent set-goal`。每轮 LLM 决策的完整细节（真实模型名、推理、辩论稿、风控保留比例、token 用量）都落在 `llm_cycle` 决策日志里。

### 走查式调参（walk-forward hyperopt）

对声明了 `param_space` 的策略做网格搜参，并在**样本外**验证，跑赢默认才采纳——自动防过拟合。

- 在策略类里声明 `param_space`（如 `ma_cross`：`{"short_period": [3,5,8,10], "long_period": [20,30,60]}`），留空则不调参。
- 触发：Web **AI Agent → 参数优化** 面板「运行优化」，或 CLI `quanti optimize`（`--universe / --end / --cash`）。
- OOS 指标 = 多折走查的**池化夏普**（合并各折 OOS 日收益后整体估一次，池化观测不足则记 0，避免短折夏普噪声）。hyperopt 采纳门（全满足才 ✓）：有效折数 ≥2、OOS 成交 ≥5、调优 OOS 夏普 > 默认 + 0.1、且 > 0；selector 侧另设最小 OOS 成交置信门（OOS 成交 <10 的夏普视为噪声，不计分也不获资金权重）。默认 `train_days=365, n_folds=3, test_days=21, max_combos=64`。
- 采纳后参数经 `resolve_params` 自动叠加到下一轮策略初始化（`get_active_params` 仅在 `accepted` 时返回），未采纳则保持默认；结果存于各账户库的 `strategy_params` 表。

### LLM 因子挖掘

让 LLM 用上文的因子 DSL 提出截面 alpha 表达式，经安全解析与 rank-IC 闸门筛选后入库，**可选**注入实盘选股排序（默认不参与）。可用字段除 OHLCV/换手率外，还包含 PIT 基本面 `pe/pe_ttm/pb/ps/ps_ttm/total_mv/circ_mv/dv_ratio/roe/netprofit_yoy/revenue_yoy`；函数限 `Ref/Mean/Std/Sum/Max/Min/Log` + `+ - * /` 与一元负号，仅整数窗口，禁止 `**`。

- 触发：Web **AI Agent → 因子挖掘 (LLM)** 面板「运行挖掘」，或 CLI `quanti mine-factors`（`--universe / --n（默认 10）/ --end`）。复用 LLM 增强层的供应商配置（DeepSeek / Anthropic）。
- **IC 闸门**（采纳条件，全满足）：`|训练IC| ≥ 0.02`、`OOS IC ≥ 0.03`、且与已采纳因子不冗余（秩相关 < 0.7）；训练/OOS 窗口留间隔防标签泄漏。
- **基本面参与打分**：IC 评估时按 point-in-time 把 `daily_basic`/`financials`（估值 + ROE/同比）并入每只票的求值帧（financials 用 `merge_asof(ann_date)` 防前视），所以 LLM 提的**估值/质量因子（pe/pb/roe/…）能算出真实 IC 并被采纳**；DB 无基本面时自动跳过该合并（零额外开销，仅量价/换手维度）。
- **三态语义**：`采纳` = 挖掘时通过 IC 闸门（不可变）；`启用` = 逐因子开关（默认开）；`生效` = 采纳 ∧ 启用 ∧ 账户总开关 `use_generated_factors`（默认**关**）三者同时成立，才真正并入下单排序。
- 生成因子存于各账户库的 `generated_factors` 表；接口 `GET /factors/generated`、`POST /factors/generated/{name}/enabled`。

### 每日定时运行

Agent 默认按固定间隔跑（`tick_interval_sec`，默认 4 小时）。设 `goal.params.daily_run_time = "HH:MM"` 后改为**每天定点跑一次**：

- `daily_run_time`：`"HH:MM"`（24 小时制，如 `"17:30"`）；不设 = 间隔模式。
- `daily_trading_days_only`：默认 `true`，仅交易日触发，周末/节假日自动跳过（节假日精度需先 `quanti sync --calendar`，否则按周一~周五）。
- 入口：Web **AI Agent → 目标设定 → 运行计划** 控件（开关 + 时间 + 仅交易日）。保存时若调度变化且 Agent 在运行，会自动重启使新调度即时生效。

### 可组合风控（protections）

在确定性**硬限**（单票 10% / 行业 30% / 单票止损 -8% / 组合 -15% 熔断）之上，再叠加一层从历史事实推导的**软锁**——只临时禁止**新建仓**、从不强制卖出，回测与实盘共用同一套纯逻辑：

| 保护 | 触发 | 效果 |
|------|------|------|
| **StoplossGuard** | 近 5 个交易日内 ≥3 次止损离场 | 锁定新买入 5 个交易日 |
| **MaxDrawdown** | 近 10 个交易日窗口回撤 ≤ -8% | 锁定新买入 10 个交易日 |

锁定是无状态的「前向 K 日」模型（每日由事实重建，重启安全）。v1 为全局软锁，阈值目前为代码内默认（`ProtectionConfig`），随回测/实盘的 `apply_risk` 一同开关。

### MCP 接入（OpenClaw / Claude Desktop / Cursor 等）

`quanti mcp` 以 stdio JSON-RPC 启动 MCP server，暴露 19 个工具：

| 工具 | 用途 |
|------|------|
| `get_goal` / `set_goal` | 读写目标 |
| `agent_start` / `agent_stop` / `agent_status` / `agent_tick` | 控制循环 |
| `get_portfolio` / `list_orders` / `list_trades` | 账户视图 |
| `place_order` | 手动覆盖买卖 |
| `list_strategies` / `list_screeners` / `list_pools` | 资源清单 |
| `run_backtest` / `run_screener` | 试跑（不影响实盘账户） |
| `list_decisions` / `prune_decisions` | 决策日志（查看 / 清理 N 天前） |
| `sync_stocks` / `sync_quotes` | 数据同步 |

> 策略调参不是独立 MCP 工具，而是通过 `set_goal` 的 `params` 传入（见 `set_goal`），故不计入上面 19 个工具。

OpenClaw 配置示例（MCP client config）：
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

数据源切换也通过 Web API 暴露（挂载于 `/api`）：`GET /config/data-source`（读当前源 + `has_token`，绝不回明文 token）、`POST /config/data-source/test`（只探活不落库）、`POST /config/data-source`（先校验源 + 探活，通过才落库）。

### CLI 命令速查

> 完整命令与全部参数见 **[命令行使用大全 docs/CLI.md](docs/CLI.md)**。

| 命令 | 说明 |
|------|------|
| `quanti up [--target 0.20 --no-agent ...]` | 一键启动数据 + 目标 + Web + Agent |
| `quanti serve` | 只起 Web，不自动启 Agent |
| `quanti agent tick` | 在本地强制跑一轮 Agent 周期（不需 server） |
| `quanti agent status` / `decisions` / `goal` | 命令行观察 Agent |
| `quanti agent set-goal --target 0.25 --risk high` | 命令行改目标 |
| `quanti optimize [--universe POOL --end DATE]` | 走查式调参：各策略 OOS 验证 + 采纳 |
| `quanti mine-factors [--universe POOL --n 10 --end DATE]` | LLM 因子挖掘：提因子 → IC 闸门 → 落库 |
| `quanti mcp` | 起 MCP server 供 OpenClaw 接入 |
| `quanti sync --stocks` / `quanti backtest ...` | 原有命令 |

## 项目状态

- [x] 数据采集与存储 / 因子 / 策略 / 选股 / 回测 / 风控（回测链路完整）
- [x] Web 可视化（含 AI Agent 页）
- [x] 持久化组合：positions / orders / trades / snapshots
- [x] **AI Agent 自治循环 + 目标管理**
- [x] **StrategySelector：按目标自动挑最佳策略**
- [x] **PaperBroker 模拟盘 + A 股 T+1/佣金/印花税完整模拟**（挂单次日开盘成交 + TTL 过期）
- [x] **MCP server（stdio）— OpenClaw / Claude Desktop 即插即用**
- [x] **LLM 多智能体增强层**：新闻情绪 / 多空辩论 / 风控三角 / 反思记忆（DeepSeek `deepseek-v4-pro` 默认，Anthropic 可选）
- [x] **行情 regime 检测 v1**（趋势/震荡/高波动，observe-only）
- [x] **走查式调参（walk-forward hyperopt）**：网格搜参 + 多折 OOS 夏普验证，跑赢默认才采纳
- [x] **防前视因子 DSL + 安全解析**（声明式表达式，禁止未来引用，无 `eval`）
- [x] **LLM 因子挖掘**：提因子 → IC 闸门去冗余 → 自演化因子库，可选注入实盘排序（默认关）
- [x] **可组合风控 protections**：StoplossGuard + MaxDrawdown 软锁（在 -15% 硬熔断之上）
- [x] **回测≡实盘一致性加固（2026-06-22 审计修复）**：回测应用 -15% 组合回撤熔断（C1）、建仓复用实盘 sizing 读 signal.strength（C2）、回测滑点对齐实盘 FlatSlippage 10bps（C4）、单 bar 成交额参与率上限封顶巨单瞬时成交（B1）、PaperBroker T+1 按建仓批次冻结当日买入（F1）
- [x] **Agent 每日定时运行**（`daily_run_time` + 仅交易日，含 Web 运行计划 控件）
- [x] **无幸存者偏差回测数据源**（Tushare 退市股名册 + 历史行情）
- [x] **实盘/模拟盘分库**：每账户独立交易库 + 共享行情库
- [x] **后台同步守护**：交易时段感知（收盘后自动全市场增量）+ 停牌/死源指数退避
- [x] 决策日志自动保留（默认 90 天）+ 手动清理 (`quanti agent prune`)
- [x] 前端按路由懒加载（ECharts 进 Backtest 才下载，首屏 -72%）
- [ ] regime 检测 v1.1：observe-only 验证后按行情自动切换选股器/仓位
- [~] 接入真实券商 API（QMT / miniQMT）—— **脚手架就绪、mock 端到端可跑、真机未验证、暂无启用入口**（已过 2026-06-22 实盘安全审计加固：require_live 门控 / 限价 tick+涨跌停 clamp / 持仓现价驱动逐票止损，G1/G4/C5/C6）：`QmtBroker` + `bridge/qmt_bridge.py`（vnpy_xt 后端）+ `xtdata` 历史源；路线见 `docs/plans/2026-06-16-live-trading-qmt.md`
- [ ] 实时分钟级行情
- [ ] PostgreSQL 后端

## 许可证

[MIT License](LICENSE)
