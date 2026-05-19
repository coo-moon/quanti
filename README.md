# Quanti - A股量化交易系统

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Quanti 是一个面向中国 A 股市场的开源量化交易系统，提供从**数据采集、因子计算、策略编写、回测验证**到 **Web 可视化**的完整工作流。

![Dashboard](docs/screenshots/dashboard.png)

## 特性

| 模块 | 功能 |
|------|------|
| **数据采集** | AkShare 数据源，自动同步全A股行情，SQLite 本地存储 |
| **股票池管理** | 创建/删除股票池，批量添加/移除股票，一键同步K线数据 |
| **技术因子** | 内置 MA/EMA/RSI/MACD/布林带/ATR 等技术指标，支持自定义因子 |
| **策略框架** | 继承 `BaseStrategy` 即可编写策略，动态加载无需配置 |
| **回测引擎** | 事件驱动，模拟A股T+1规则、涨跌停、佣金印花税 |
| **Web 仪表盘** | FastAPI + Vue 3，K线图表、回测曲线、选股结果可视化 |
| **选股器** | 可扩展的选股插件框架，支持自定义选股逻辑 |
| **进度追踪** | 后台任务实时进度，同步/回测/选股均有 ETA 预估 |

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
| **Dashboard** | 全局股票池统计、一键下载K线（含进度条+ETA） |
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
│   ├── database.py           #   SQLite 存储 + sync_jobs 任务追踪
│   ├── provider.py           #   统一数据接口
│   └── akshare_adapter.py    #   AkShare 适配器（含回退逻辑）
│
├── factors/                  # 因子引擎
│   ├── registry.py           #   因子注册表
│   └── technical.py          #   技术指标实现
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
├── screener/                 # 选股器框架
│   ├── base.py               #   选股器基类
│   └── loader.py             #   动态加载器
│
├── risk/                     # 风控模块
│   └── manager.py            #   风险管理器
│
├── api/                      # Web API
│   ├── app.py                #   FastAPI 应用
│   └── routes.py             #   路由定义
│
└── cli.py                    # 命令行入口

web/src/views/                 # Vue 3 前端
├── Dashboard.vue             #   主页仪表盘
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
- 全部在 `data/quanti.db` 持久化：组合、持仓、订单、成交、目标、决策

在 Web → **AI Agent** 页面可以：编辑目标、启停 Agent、立即跑一轮、查看持仓与决策日志、手动下单覆盖。

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
- [x] **PaperBroker 模拟盘 + A 股 T+1/佣金/印花税完整模拟**
- [x] **MCP server（stdio）— OpenClaw / Claude Desktop 即插即用**
- [x] 决策日志自动保留（默认 90 天）+ 手动清理 (`quanti agent prune`)
- [x] 前端按路由懒加载（ECharts 进 Backtest 才下载，首屏 -72%）
- [ ] 接入真实券商 API（QMT / Easytrader） — 见 `quanti/execution/`，预留扩展点
- [ ] 实时分钟级行情
- [ ] PostgreSQL 后端

## 许可证

[MIT License](LICENSE)
