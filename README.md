# Quanti - A股量化交易系统

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Quanti 是一个面向中国 A 股市场的开源量化交易系统，提供从**数据采集、因子计算、策略编写、回测验证**到 **Web 可视化**的完整工作流。

![Dashboard](docs/screenshots/dashboard.png)

## 特性

| 模块 | 功能 |
|------|------|
| **数据采集** | AkShare 数据源，自动同步全A股行情，SQLite 本地存储 |
| **后台同步守护** | 常驻增量同步：收盘后自动全市场扫一轮，盘中安静；停牌股/死数据源指数退避 |
| **股票池管理** | 创建/删除股票池，批量添加/移除股票，一键同步K线数据 |
| **技术因子** | 内置 MA/EMA/RSI/MACD/布林带/ATR/ADX 等技术指标 + 横截面因子，支持自定义 |
| **策略框架** | 继承 `BaseStrategy` 即可编写策略，动态加载无需配置 |
| **回测引擎** | 事件驱动，模拟A股T+1规则、涨跌停、佣金印花税 |
| **AI Agent** | 目标驱动自治循环：同步→选股→策略评估→风控→模拟下单→决策日志 |
| **LLM 增强层** | 新闻情绪 / 多空辩论 / 风控三角 / 反思记忆（DeepSeek 或 Anthropic，默认关闭，只做加法） |
| **行情状态检测** | 趋势/震荡/高波动分类（observe-only），写入决策日志供 LLM 与人参考 |
| **模拟盘** | PaperBroker：挂单 T+1 次日开盘成交、TTL 过期、止损、A股费用模型 |
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
pip install -e ".[dev]"
```

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
| **Dashboard** | 全局股票池统计、真实数据最新日期、后台同步状态、一键下载K线（含进度条+ETA） |
| **AI Agent** | 目标设定、启停/手动 tick、LLM 增强层开关面板、持仓与决策日志、手动下单 |
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
│   ├── akshare_adapter.py    #   AkShare 适配器（多源回退 + 增量同步）
│   └── background_sync.py    #   后台同步守护（交易时段感知 + 指数退避）
│
├── agent/                    # AI Agent
│   ├── runtime.py            #   自治循环（tick 调度、universe、模式分发）
│   ├── goal.py               #   目标模型（年化/回撤/风险偏好/params）
│   ├── selector.py           #   StrategySelector：按目标回测挑最佳策略
│   ├── walk_forward.py       #   走查验证
│   ├── universe.py           #   全市场过滤（元数据/流动性/ADV20）
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
│   └── cross_sectional.py    #   横截面因子与复合排名
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
│   └── manager.py            #   硬限制：单票/行业/总仓位/止损（LLM 不可逾越）
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

## 数据同步命令

| 命令 | 说明 |
|------|------|
| `quanti sync --stocks` | 同步全A股列表（名称/行业/交易所） |
| `quanti sync --calendar` | 同步交易日历 |
| `quanti sync --quotes --codes 000001,600519` | 同步指定股票K线 |
| `quanti serve` | 启动 API 服务（端口 8000） |

手动同步通常不需要：服务启动后**后台同步守护**（`BackgroundQuoteSyncer`）会持续维护数据新鲜度——

- **交易时段感知**：交易日 15:30（收盘宽限）后期待当天的 bar，自动触发一轮全市场增量同步（每只通常 1-2 根）；盘中与周末保持安静，不做无意义重拉；
- **增量拉取**：已有数据的股票从最新 bar 续拉，只有空白股票付一次约一年的冷启动成本；
- **退避机制**：硬失败（数据源不覆盖、网络异常）按 30 分钟 → 1h → 2h → 4h 封顶指数退避；同步成功但无新数据（停牌股）平退避 30 分钟，不会无限循环；
- 优先级：持仓股 > 无数据 > 数据过期；状态可在 Dashboard 实时查看（含退避中数量）。

## 配置

数据存储路径可在 `quanti/cli.py` 中配置，默认 `data/quanti.db`。

## 技术栈

| 组件 | 技术 |
|------|------|
| 数据源 | [AkShare](https://github.com/akfamily/akshare) |
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
- 全部在 `data/quanti.db` 持久化：组合、持仓、订单、成交、目标、决策

在 Web → **AI Agent** 页面可以：编辑目标、启停 Agent、立即跑一轮、查看持仓与决策日志、手动下单覆盖。

### LLM 多智能体增强层（可选，默认关闭）

设计哲学：**规则优先，LLM 只做加法**。回测、因子打分、风控全部保持确定性可审计；LLM 的职责限定为对系统产出的候选做"判断"——倾斜排序、收缩仓位、补充上下文。硬风控（单票 10% / 行业 30% / 总仓位 80% / 止损线）LLM 永远无法绕过；全部开关默认关闭，关闭时行为与纯规则路径完全一致。

| 层 | goal params 开关 | 作用 |
|------|------|------|
| ① 新闻情绪 | `sentiment_enabled` + `sentiment_blend`(0~1) | 抓取个股近 7 天新闻标题，LLM 批量打分 [-1,1]，按权重融入买入信号；按（股票, 日期）缓存，不重复计费 |
| ② 多空辩论 | `llm_debate` + `llm_debate_rounds` | 多头/空头研究员就候选清单辩论 N 轮，辩论稿进入交易员上下文，由其作为"研究主管"裁决 |
| ③ 风控三角 | `llm_risk_debate` | 激进/中性/保守三视角对每笔提议投"保留比例"，按风险偏好聚合（low→最小 / medium→均值 / high→最大），**只能缩仓或否决，不能加仓** |
| ④ 反思记忆 | `llm_reflection` + `llm_max_reflections` | 已实现盈亏 FIFO 配对成历史回合，按相关度（同股 > 同行业）注入上下文，让 LLM 带着"上次的教训"决策 |
| 行情状态 | `regime_detect` | 等权合成指数 + Kaufman ER + 波动率分位 + 广度 → 趋势上行/下行/震荡/高波动，observe-only 写决策日志 |

**供应商**：`llm_provider` 支持 `deepseek`（默认模型 `deepseek-v4-pro`，OpenAI 兼容接口，零额外依赖，`export DEEPSEEK_API_KEY=...` 即可）和 `anthropic`（`pip install -e ".[llm]"` + `ANTHROPIC_API_KEY`）。`claude-*` 模型名在 DeepSeek 路径下自动重映射；v4 思考模式与强制工具调用的兼容性已在客户端处理（结构化输出自动关思考，辩论等自由文本保留思考）。

开关入口三选一：Web **AI Agent → LLM 增强层**面板 / MCP `set_goal` 的 `params` / `quanti agent set-goal`。每轮 LLM 决策的完整细节（真实模型名、推理、辩论稿、风控保留比例、token 用量）都落在 `llm_cycle` 决策日志里。

### MCP 接入（OpenClaw / Claude Desktop / Cursor 等）

`quanti mcp` 以 stdio JSON-RPC 启动 MCP server，暴露 18 个工具：

| 工具 | 用途 |
|------|------|
| `get_goal` / `set_goal` | 读写目标 |
| `agent_start` / `agent_stop` / `agent_status` / `agent_tick` | 控制循环 |
| `get_portfolio` / `list_positions` / `list_orders` / `list_trades` | 账户视图 |
| `place_order` | 手动覆盖买卖 |
| `list_strategies` / `list_screeners` / `list_pools` | 资源清单 |
| `run_backtest` / `run_screener` | 试跑（不影响实盘账户） |
| `list_decisions` | 决策日志 |
| `sync_stocks` / `sync_quotes` | 数据同步 |
| `tune_strategy`（通过 `set_goal` 的 `params`） | 调策略参数 |

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

### CLI 命令速查

| 命令 | 说明 |
|------|------|
| `quanti up [--target 0.20 --no-agent ...]` | 一键启动数据 + 目标 + Web + Agent |
| `quanti serve` | 只起 Web，不自动启 Agent |
| `quanti agent tick` | 在本地强制跑一轮 Agent 周期（不需 server） |
| `quanti agent status` / `decisions` / `goal` | 命令行观察 Agent |
| `quanti agent set-goal --target 0.25 --risk high` | 命令行改目标 |
| `quanti mcp` | 起 MCP server 供 OpenClaw 接入 |
| `quanti sync --stocks` / `quanti backtest ...` | 原有命令 |

## 项目状态

- [x] 数据采集与存储 / 因子 / 策略 / 选股 / 回测 / 风控（已接入实盘+回测链路）
- [x] Web 可视化（含 AI Agent 页）
- [x] 持久化组合：positions / orders / trades / snapshots
- [x] **AI Agent 自治循环 + 目标管理**
- [x] **StrategySelector：按目标自动挑最佳策略**
- [x] **PaperBroker 模拟盘 + A 股 T+1/佣金/印花税完整模拟**（挂单次日开盘成交 + TTL 过期）
- [x] **MCP server（stdio）— OpenClaw / Claude Desktop 即插即用**
- [x] **LLM 多智能体增强层**：新闻情绪 / 多空辩论 / 风控三角 / 反思记忆（DeepSeek `deepseek-v4-pro` 默认，Anthropic 可选）
- [x] **行情 regime 检测 v1**（趋势/震荡/高波动，observe-only）
- [x] **后台同步守护**：交易时段感知（收盘后自动全市场增量）+ 停牌/死源指数退避
- [x] 决策日志自动保留（默认 90 天）+ 手动清理 (`quanti agent prune`)
- [x] 前端按路由懒加载（ECharts 进 Backtest 才下载，首屏 -72%）
- [ ] regime 检测 v1.1：observe-only 验证后按行情自动切换选股器/仓位
- [ ] 接入真实券商 API（QMT / Easytrader） — 见 `quanti/execution/`，预留扩展点
- [ ] 实时分钟级行情
- [ ] PostgreSQL 后端

## 许可证

[MIT License](LICENSE)
