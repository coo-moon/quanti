# Smart Quant Upgrade — 三阶段升级计划

**Date**: 2026-05-25
**Owner**: wenbo
**Status**: in progress
**Target**: 实盘年化 30-50%、最大回撤 < 25%(顶级私募水平,需真实 alpha + 严风控)

## 北极星 & 红线

**北极星**: 把这套系统从"6 个手工技术策略 + 规则 agent",升级为
"截面因子 + 滚动验证 + LLM 决策"的可观测、可解释、可回归的智能交易系统。

**红线 (不可触碰)**:
1. 任何回测/选股环节都不能用未来信息。
2. RiskManager 是硬性边界:LLM 也无权突破单股 10% / 行业 30% / 总仓 80% / 回撤 15% 上限。
3. 不能为了"看起来好看"就关闭手续费/滑点/印花税。
4. 任何"年化 100%+"的回测结果必须有 OOS 验证,否则视为过拟合。
5. 不删任何与生产路径相关的诚实校验(T+1、ST 屏蔽、停牌、连板)。

---

## Phase 1 — Backtest 可信度 (Foundation)

**为什么先做这一步**: 后面所有的 alpha 工作都要靠回测来评估。如果回测本身骗自己,
后面跑出来的 "30-50% 年化" 都是幻觉。这个阶段不增加 alpha,只增加"我们到底
赚不赚钱"这个问题的可信度。

### P1.1 — Walk-forward 验证

**问题**: `quanti/agent/selector.py` 当前在过去 365 天上 in-sample 回测打分。
6 个策略里选 IS Sharpe 最高的 → 几乎必然过拟合,因为 IS 表现本身被用来选模型。

**方案**:
- `quanti/agent/walk_forward.py` — 新模块。提供 `run_walk_forward(strategy, codes, end, train_days=180, test_days=21, n_folds=6)`。
- 每个 fold: train 段不做回测(策略本来就是无参的;train 段只是"如果有参就调优"的占位),test 段做回测,聚合各 fold 的 OOS 指标。
- `StrategyEvaluation` 新增 `oos_annual_return`, `oos_max_drawdown`, `oos_sharpe`, `oos_consistency` (各 fold 收益的 1 - CoV)。
- `_score()` 改为以 OOS 指标为主体,IS 仅作 tie-break。
- `goal.params` 新增 `wf_enabled: bool = True`,允许关闭走老路径(便于对照)。

**验收**:
- `tests/test_walk_forward.py`: 用一个明显过拟合的合成策略(随机 + 训练段强信号),验证 IS Sharpe 高但 OOS Sharpe 低,新评分函数把它排到后面。
- 跑一遍 `pytest -q` 全绿。

### P1.2 — 成交量加权滑点

**问题**: `quanti/backtest/engine.py` 当前固定 0.1% 滑点。大单/小盘股根本不止这个滑点。

**方案**:
- `quanti/backtest/slippage.py` — 新模块。两种模型:
  - `FlatSlippage(bps=10)` — 现有行为。
  - `VolumeImpactSlippage(base_bps=5, impact_coef=10, alpha=0.5)`:
    `slip_bps = base_bps + impact_coef * sqrt(qty * price / adv20_amount) * 100`
    其中 `adv20_amount` = 该标的过去 20 日平均成交额。
- `BacktestEngine` 构造函数接受 `slippage` 实例;默认 `VolumeImpactSlippage`。
- 缺数据(成交额 0 或缺失)时降级到 FlatSlippage,记一条 warning。

**验收**:
- `tests/test_slippage.py`: 小单/大单/缺数据三种情况的滑点都符合预期数量级。
- 现有 `tests/test_backtest.py` 中的回归数字会变,需要更新预期值。

### P1.3 — 波动率目标仓位

**问题**: 当前每股固定 10% 仓位。低波股票"用不掉"风险预算,高波股票把风险预算用爆。

**方案**:
- `quanti/risk/sizer.py` — 新模块。`VolTargetSizer(target_portfolio_vol=0.18, lookback_days=60)`:
  - 对每个候选股算近 60 日年化波动率 σ_i。
  - 等权目标下,单股权重 ∝ 1/σ_i,归一化到总仓预算。
  - 仍受 RiskManager 单股 10%、行业 30% 上限约束。
- `PaperBroker.execute_signals()` 在生成订单数量时调用 sizer。
- 配置开关 `goal.params["sizer"] = "fixed" | "vol_target"`,默认 `vol_target`。

**验收**:
- `tests/test_sizer.py`: 合成两个标的(σ=10% 和 σ=40%),验证低波分配权重大致是高波的 4 倍,且总和不超总仓上限。

---

## Phase 2 — 截面 Alpha (Smarter Signals)

### P2.1 — 截面因子模块

**问题**: 6 个手工策略本质都是时序技术信号,缺少横向对比维度。
A 股最稳定的 alpha 来源是截面因子。

**方案**:
- `quanti/factors/cross_sectional.py` — 新模块。
- 实现 5 个基础因子:
  - `momentum_3m`, `momentum_6m` — 反转扣 1 个月,即 t-126 到 t-21 的累计收益。
  - `reversal_1w` — 短期反转,t-5 到 t 的累计收益取负。
  - `turnover_20d` — 20 日平均换手率(负向因子,高换手低收益)。
  - `realized_vol_20d` — 20 日已实现波动率(负向)。
- 每个因子: 横截面 z-score → 99% 缩尾 → 行业内 demean → 等权或 IC 加权合成。
- 输出 DataFrame: `index=code, columns=[factor_name..., composite]`,值域 [-3, 3]。

**验收**:
- `tests/test_factors_cross.py`: 合成 30 支股票 200 天数据(其中 5 支真有动量),验证 composite 因子能把它们排到前面。

### P2.2 — Selector Top-K ensemble

**问题**: Top-1 winner-take-all 在策略风格切换的市场会被"上个月有效的"策略坑。

**方案**:
- `StrategySelector.pick_topk(goal, codes, k=3)` 返回 List[(strategy, weight)],
  权重 ∝ OOS Sharpe(剪到 0,做 softmax 归一)。
- `runtime.py` 改造: 对 Top-K 都跑信号生成,信号融合规则:
  - 同一标的多策略同方向 → 累加权重,记为 strategy_score ∈ [0, 1]。
  - 与 P2.1 的 composite factor 相乘 → final_score。
  - final_score 通过阈值(默认 0.3)的进 buy 池;按 final_score 降序 + VolTargetSizer 分配资金。

**验收**:
- `tests/test_selector_ensemble.py`: 3 个虚假策略(在不同样本期分别表现好),验证 ensemble 在跨期上比 Top-1 更稳。

### P2.3 — 主动行业中性化

**问题**: 当前 RiskManager 的 30% per industry 是被动上限,会出现"打满某行业才停"的行为。

**方案**:
- 信号融合后、下单前,做一次行业归一化:
  - 按行业分组,行业内 final_score 重排序 → 每个行业最多取 top-N (N=2)。
  - 配置开关 `goal.params["industry_neutral"] = True`。
- 历史信号选最近的处理逻辑(`latest[]` dict)中加入行业约束。

**验收**:
- `tests/test_industry_neutral.py`: 同行业 10 支股票打分都很高,开关 ON 时下单数 ≤ 2,OFF 时 ≤ 10。

---

## Phase 3 — LLM Agent (Visible Intelligence)

### P3.1 — Claude 接入 Agent runtime

**问题**: 当前 `agent/runtime.py` 是规则驱动,不是 LLM 驱动。要让"agent"名副其实。

**方案**:
- `quanti/agent/llm_runtime.py` — 新建,与 `runtime.py` 并存。
- 配置开关 `goal.params["agent_mode"] = "rule" | "llm"`,默认 `"rule"`(向后兼容)。
- 每个 tick:
  1. 准备 context (压缩后) — goal、当前 portfolio 概览(总仓、现金、持仓 top 5)、
     Top-K 候选信号(每个含 strategy_score, factor_score, industry, recent 60d return)、
     最近 5 条 decision_log。
  2. 用 anthropic SDK 调用 Claude(默认 `claude-sonnet-4-6` 以控本,
     `goal.params["llm_model"]` 可改)。Prompt 含 system prompt(限制其只能在
     RiskManager 范围内做调整)+ tool 定义。
  3. 暴露给 LLM 的 tools(均为 dry-run 安全版本):
     - `inspect_position(code)` — 该标的最新状态。
     - `inspect_decision_history(limit)` — 最近若干次决策回看。
     - `propose_orders(orders: list[{code, direction, size_pct, reason}])` — 这是 LLM 最终落子的唯一接口。
  4. LLM 通过 propose_orders 返回的提案再过一遍 RiskManager,通过的下单。
- 使用 prompt caching(system prompt + 工具定义 24h 缓存)节省 token。

**安全 invariant**:
- LLM 提案中超 RiskManager 限的订单 → 拒绝并记 `reason: blocked_by_risk_manager`。
- LLM 单 tick token 上限 200K(避免失控)。
- 任何工具 schema 校验失败 → 回退到规则路径(degrade gracefully)。

**验收**:
- `tests/test_llm_runtime.py`: 用 stub Anthropic client(响应从 JSON fixture 加载),验证 LLM 提议被 RiskManager 拦截、被 dispatch、被记录。

### P3.2 — 决策理由日志 + 前端

**方案**:
- 每次 LLM tick 后,`db.log_decision("llm_cycle", summary, details={prompt_digest, response_digest, reasoning, ...})`。
- 前端 `web/` AI Agent 页面 — 新增"决策理由"展开区,展示中文 reasoning + 各候选股得分明细。
- MCP 工具 `list_decisions` 现成可用,前端复用即可。

**验收**:
- 跑一次 LLM tick,前端能看到 reasoning;再跑一次规则 tick,前端看不到 reasoning 但有 summary,二者并存。

---

## 阶段依赖

```
P1.1 (walk-forward) ─┐
P1.2 (slippage)      ├──→ P2.2 (ensemble 需要 OOS Sharpe 做权重)
P1.3 (sizer)         │           │
                     │           ↓
P2.1 (factors) ──────┴──→ P2.3 (industry neutral)
                                 │
                                 ↓
                          P3.1 (LLM runtime)
                                 │
                                 ↓
                          P3.2 (decision log + 前端)
```

可以并行: P1.1 ‖ P1.2 ‖ P1.3 ‖ P2.1。

---

## 不在范围内 (明确不做)

- ❌ 高频/分钟级数据接入(当前 daily-only,改一项要重建数据层)。
- ❌ 期权/期货扩展(出 A 股股票就是另一个项目)。
- ❌ 真实券商对接(等 3 个月模拟盘门槛过了再说,见 [TODO-live-trading.md](../TODO-live-trading.md))。
- ❌ 任何承诺超过 50% 年化的方案 — 那是过拟合或赌博,与本项目宗旨不符。
- ❌ 加杠杆 / 融券做空(A 股散户实操路径上不现实,合规风险高)。

---

## 验收 Gate (跨阶段)

每阶段完成必须满足:
- `pytest -q` 100% 通过(含新增测试)。
- 跑一次 `quanti agent_tick` 不报错。
- 关键改动加 docstring 说明"为什么这样做",不只 "what"。
- 旧路径(`agent_mode="rule"`, `sizer="fixed"`, `wf_enabled=False`)仍可工作。

最终 Gate (Phase 3 后):
- 在最近 1 年数据上跑一次完整回测(LLM mode + ensemble + factors + walk-forward selector):
  - 如果回测年化 < 15%、最大回撤 > 25% → 项目失败,回滚 LLM 改动,保留 Phase 1+2。
  - 如果回测年化 > 100% → **不要相信**,继续做 OOS 验证 + 调小 alpha 系数排查泄露。
  - 30-50% 区间是健康目标。
