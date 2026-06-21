# 成熟量化系统借鉴清单（for quanti）

**日期**: 2026-06-20
**背景**: quanti = A 股、规则优先 + LLM 增强、正在接 QMT 实盘；刚完成交易核心审计修复（前视偏差/回测≡实盘、行业·单票·总仓硬限、组合 -15% 熔断、走查指标）。
**目的**: 调研 GitHub 上相对成熟的量化系统，提炼可落地到 quanti 各模块的借鉴点。

## 一、参考系统与各自成熟点

| 系统 | 定位 / 成熟度 | 最值得看的地方 |
|------|------|------|
| **vnpy / VeighNa** | 国内最成熟的实盘框架（2015 至今，v4.0） | 事件引擎 + **gateway 抽象**（CTP / **QMT** / 币安…）+ 订单/成交回调状态机；v4 新增 `vnpy.alpha` ML 因子引擎；DuckDB 存储 |
| **Microsoft Qlib** | AI 量化研究平台 | **point-in-time 数据层** + **因子表达式引擎**（Alpha158/360）+ model zoo；**RD-Agent**（LLM 自动挖因子 / 联合优化） |
| **zipline-reloaded** | Quantopian 引擎维护版 | **Pipeline API**：横截面因子研究，**结构上杜绝前视**；配 Norgate 等数据做幸存者偏差 / point-in-time |
| **freqtrade** | 最成熟的开源交易机器人（加密） | **dry-run**（同代码路径模拟）、**hyperopt**（参数寻优）、**protections**（冷却 / 连损熔断 / 回撤锁） |
| **rqalpha** | 国内 A 股全链路 | **mod 插件架构**（数据 / 经纪 / 风控全可替换） |
| backtrader | 经典学习型回测库 | 事件驱动 API 友好，但维护风险高，不建议作 A 股生产基座 |

## 二、对 quanti 的借鉴点（按优先级，映射到模块 / 审计）

### ① 点对点数据 + 幸存者偏差（Qlib / zipline+Norgate）— 最高杠杆
- **问题**: 审计里最深的研究正确性缺口——xtdata/akshare 只给"当前在市"标的，没有退市股历史、没有 point-in-time 指数成分，回测系统性高估。
- **借鉴**: 给 `data/` 加一层 **point-in-time 标的池 + 退市股历史**（专业源 Tushare/聚宽补），仿 Qlib 数据层。
- **对应**: 审计 look-ahead / survivorship。

### ② 声明式、防前视的因子 Pipeline（zipline Pipeline + Qlib 表达式引擎）
- **问题**: 同-bar 前视已用命令式修掉，但 `factors/cross_sectional.py` 仍手写、易再引入前视。
- **借鉴**: zipline Pipeline 模式（因子在"截至 t-1 窗口"算、t 执行，前视结构上不可能）+ Qlib 因子表达式 DSL（`Ref/Mean/Std` 组合，因子可组合 / 可单测 / 可批量回测）。
- **对应**: `factors/`、`agent/signal_pipeline.py`。

### ③ vnpy 的 gateway / 回调模型 — 直接降低 phase ②/③ 风险
- **问题**: 正在手搓 `qmt-bridge` + `QmtBroker`，异步成交靠轮询对账。
- **借鉴**: vnpy **已有成熟 QMT gateway** + `on_order/on_trade/on_position` 回调状态机 + 持仓对账。**要么直接复用 vnpy 的 QMT gateway**（省自建 bridge 的坑），要么照搬其订单状态机 / 异步成交回调设计。
- **对应**: `bridge/qmt_bridge.py`、`execution/qmt_broker.py`。

### ④ freqtrade 式 "protections"（可组合风控）
- **现状**: 已有硬上限 + 组合 -15% 熔断。
- **借鉴**: 可插拔保护层——连续止损 N 次冷却、单票/全局回撤锁、低胜率锁仓，比单一熔断更细腻。
- **对应**: `risk/manager.py`。

### ⑤ 走查式参数寻优（freqtrade hyperopt）✅ 已实现
- **现状**: 有 `StrategySelector`（选策略）+ walk_forward，但无调参。
- **借鉴**: 在已有 walk-forward 框架上加参数搜索（stoploss/窗口/阈值），**用 OOS 验证防过拟合**（刚加的 min-fold / 短窗口不外推年化正好兜底）。
- **对应**: `agent/selector.py`、`agent/walk_forward.py`。
- **实现**: 见设计文档 `docs/superpowers/specs/2026-06-21-walk-forward-hyperopt-design.md` 及实现计划；`HyperOptimizer`（网格搜索 + OOS 验证门控）+ `resolve_params` 接线 + CLI `quanti optimize` + 异步 API + 前端优化卡均已落地（feat/strategy-hyperopt）。

### ⑥ LLM 因子/策略挖掘闭环（Qlib RD-Agent / QuantaAlpha）— 差异化方向
- **契合**: quanti "规则验证、LLM 只做加法"的哲学。
- **借鉴**: 让 LLM **提因子 / 选股器（代码生成）→ 自动回测 + walk-forward 闸门 → 跑赢基线才采纳**，形成自进化因子库。
- **对应**: `agent/llm_runtime.py` + `factors/` + `screeners/`。

### ⑦ dry-run 纪律：paper↔live 同一条代码路径（freqtrade）
- **现状**: `Broker` 协议（PaperBroker/QmtBroker 同接口）已在做。
- **借鉴**: 继续保证 paper 与 live **只有"成交来源"不同、其余完全一致**，补 live 端遥测 / 告警。
- **对应**: `execution/`。

### ⑧ 插件化（rqalpha mod / vnpy）
- **现状**: 已有策略 / 选股器动态加载 + Broker 协议。
- **借鉴**: 把"数据源 / 风控规则"也做成可替换插件。

## 三、落地排序（务实）

1. **③ vnpy QMT gateway** — 正接实盘，省坑最多、最快见效。
2. **④ + ⑤ protections + 走查调参** — 在刚硬化的风控/选股上加细度，低风险高价值。
3. **① + ② point-in-time 数据 + 因子 Pipeline** — 根治回测可信度，规模化研究地基。
4. **⑥ LLM 因子挖掘闭环** — 把 LLM 优势变成自进化因子库，quanti 最具差异化的方向。

## Sources

- vnpy/vnpy — https://github.com/vnpy/vnpy （README_ENG: https://github.com/vnpy/vnpy/blob/master/README_ENG.md ）
- microsoft/qlib — https://github.com/microsoft/qlib ; RD-Agent — https://github.com/microsoft/RD-Agent ; QuantaAlpha — https://github.com/QuantaAlpha/QuantaAlpha
- stefan-jansen/zipline-reloaded — https://github.com/stefan-jansen/zipline-reloaded
- Freqtrade docs — https://www.freqtrade.io/en/stable/ （backtesting/protections、hyperopt）
- 对比文章: Backtrader vs vnpy vs Qlib (2026) — https://dev.to/linou518/backtrader-vs-vnpy-vs-qlib-a-deep-comparison-of-python-quant-backtesting-frameworks-2026-3gjl ; Python Backtesting Landscape 2026 — https://python.financial/
