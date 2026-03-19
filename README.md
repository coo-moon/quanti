# Quanti - A股量化交易系统

Quanti 是一个面向中国 A 股市场的开源量化交易系统，提供从数据获取、因子计算、策略编写、回测验证到 Web 可视化的完整工作流。

## 特性

- **数据模块** - 基于 AkShare 的 A 股行情数据自动采集，SQLite 本地存储
- **因子引擎** - 可扩展的因子注册表，内置 MA/EMA/RSI/MACD/布林带/ATR 等技术指标
- **策略框架** - 面向对象的策略基类，支持从目录动态加载自定义策略
- **回测引擎** - 事件驱动回测，模拟 A 股 T+1 规则、涨跌停、佣金印花税
- **风控模块** - 独立风控层，支持个股仓位限制、止损、每日交易限额
- **Web 仪表盘** - FastAPI 后端 + Vue 3 前端，ECharts 图表展示回测结果
- **CLI 工具** - 命令行一键同步数据、运行回测、启动服务

## 系统架构

```
quanti/
├── models.py          # 核心领域模型 (BarData, Signal, Order, Portfolio)
├── data/              # 数据层
│   ├── database.py    #   SQLite 存储
│   ├── provider.py    #   统一数据接口
│   └── akshare_adapter.py  # AkShare 数据源适配器
├── factors/           # 因子引擎
│   ├── registry.py    #   因子注册表
│   └── technical.py   #   技术指标因子
├── strategy/          # 策略引擎
│   ├── base.py        #   策略基类
│   └── loader.py      #   动态策略加载器
├── backtest/          # 回测引擎
│   ├── engine.py      #   事件驱动回测核心
│   ├── commission.py  #   A 股费用模型
│   └── metrics.py     #   绩效指标 (夏普/最大回撤/年化)
├── risk/              # 风控模块
│   └── manager.py     #   风险管理器
├── api/               # Web API
│   ├── app.py         #   FastAPI 应用
│   └── routes.py      #   路由定义
└── cli.py             # 命令行入口
```

## 快速开始

### 环境要求

- Python 3.11+
- Node.js 18+（前端开发可选）

### 安装

```bash
git clone https://github.com/your-username/quanti.git
cd quanti

# 创建虚拟环境并安装
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 同步数据

```bash
# 同步交易日历
quanti sync --calendar

# 同步股票列表
quanti sync --stocks

# 同步指定股票行情
quanti sync --quotes --codes 000001,600519
```

### 运行回测

```bash
quanti backtest \
  --strategy ma_cross \
  --codes 000001 \
  --start 2024-01-01 \
  --end 2024-12-31 \
  --cash 100000
```

### 启动 Web 服务

```bash
# 启动后端 API
quanti serve

# 前端开发模式（可选）
cd web && npm install && npm run dev
```

访问 http://127.0.0.1:8000 查看仪表盘。

## 编写自定义策略

在 `strategies/` 目录下创建 Python 文件，继承 `BaseStrategy`：

```python
from quanti.strategy.base import BaseStrategy
from quanti.models import BarData, Direction, Signal


class MyStrategy(BaseStrategy):
    name = "my_strategy"

    def init(self, config: dict) -> None:
        self.threshold = config.get("threshold", 10.0)

    def on_bar(self, bar: BarData) -> list[Signal]:
        if bar.close > self.threshold:
            return [Signal(
                stock_code=bar.code,
                direction=Direction.BUY,
                strength=0.8,
                reason="price above threshold",
            )]
        return []
```

策略文件会被自动发现和加载，无需修改任何配置。

## 注册自定义因子

```python
from quanti.factors.registry import register_factor
import pandas as pd

@register_factor("my_momentum")
def my_momentum(df: pd.DataFrame) -> pd.Series:
    return df["close"].pct_change(20)
```

## 运行测试

```bash
pytest tests/ -v
```

## 技术栈

| 组件 | 技术 |
|------|------|
| 语言 | Python 3.11+ |
| 数据源 | AkShare |
| 存储 | SQLite (可升级 PostgreSQL) |
| 后端 | FastAPI + Uvicorn |
| 前端 | Vue 3 + TypeScript + ECharts |
| 测试 | pytest |

## 项目状态

当前为 v0.1.0，核心功能已实现：

- [x] 数据采集与存储
- [x] 技术因子计算
- [x] 策略框架与动态加载
- [x] 事件驱动回测引擎
- [x] A 股费用与 T+1 规则
- [x] 风险管理
- [x] REST API
- [x] Web 可视化仪表盘
- [x] CLI 工具

后续计划：

- [ ] 实盘模拟交易对接
- [ ] 更多内置策略（动量、均值回归、多因子选股）
- [ ] PostgreSQL 支持
- [ ] 策略参数优化
- [ ] 实时行情推送

## 许可证

[MIT License](LICENSE)

## 贡献

欢迎提交 Issue 和 Pull Request。
