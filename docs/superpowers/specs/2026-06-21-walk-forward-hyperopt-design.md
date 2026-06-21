# 设计：走查式参数寻优（Walk-Forward Hyperopt, v1）

**日期**: 2026-06-21
**借鉴来源**: freqtrade hyperopt（见 `docs/2026-06-20-reference-mature-quant-systems.md` 第 ⑤ 项）
**状态**: 已 brainstorm 收敛，待用户复核 → writing-plans

## 1. 目标与非目标

**目标**：给策略加**系统化参数寻优**，并用样本外（OOS）验证防过拟合——在较早的训练期上 grid 搜索每个策略的参数，把选出的参数拿到**没参与挑选**的 OOS 窗口验证，**只有 OOS 上确实赢过默认参数才采纳**；采纳的参数落库，agent 选股/交易/回测时自动使用。

**非目标 / 明确不做（v1）**：
- 不每个 agent 周期自动调参（成本 + 不稳定）。调参是**按需**独立动作（CLI + 异步 API），结果持久化。
- 不引入 Bayesian/optuna 依赖。v1 用**无依赖的 grid 搜索**（+ 超额组合随机采样）。
- 不做调度器（定时自动调）——留 v2。
- 不改 walk_forward 的 OOS 评分语义；复用它做验证。

## 2. 触发模型（已定）：按需 + 持久化

独立 `HyperOptimizer`，由 **CLI `quanti optimize`**（同步，脚本用）和**异步 API job**（UI 用）触发；逐策略 grid 搜索 → OOS 验证 → 采纳参数落库。agent / selector / 回测在实例化策略时，有采纳的调优参就用、没有用默认。**与 per-cycle 选股解耦**，类似 freqtrade 的 hyperopt 独立命令。

## 3. 优化核心（已定）：A1 训练期挑参 → OOS 验证 → 赢过默认才用

对每个策略：

1. **网格**：从策略声明的 `param_space` 笛卡尔积出参数组合。**封顶** `max_combos`（默认 64）；超出用固定种子随机采样，并 **log 丢弃数量**（不静默截断）。
2. **训练期搜索**：在 OOS 折**之前**的训练窗口 `[train_start, train_end]`（默认 365 天）上，对每个组合跑一次回测，按训练期 sharpe 选出**训练最优组合**。
3. **OOS 验证**：对（a）训练最优组合 与（b）默认参数，各跑现有 `run_walk_forward`（在 OOS 折上），得各自 `oos_sharpe` 等。
4. **采纳门**：训练最优组合被采纳，当且仅当——
   - 过守卫：`n_folds ≥ min_folds`（默认 2）、`total_trades_oos ≥ min_trades_oos`（默认 5）、`oos_sharpe` 有限；
   - 且 `tuned.oos_sharpe > default.oos_sharpe + accept_margin`（默认 0.1）；
   - 且 `tuned.oos_sharpe > 0`。
   否则**保留默认**（记录为未采纳）。

**无泄漏的时间切分**（关键）：OOS 折由 `make_folds(end, n_folds, warmup_days, test_days)` 生成；令 `train_end = 最早折.warmup_start - 1 天`，`train_start = train_end - train_days`。这样训练期与验证期（**连同 warmup**）完全不重叠。

**为什么 A1 是干净的**：训练期负责*挑*参数，OOS 折只负责*验证*两个**事先确定**的组合（训练最优 + 默认）。没有"在要报告的同一份样本外上挑参"的选择偏差——选择发生在训练期，验证发生在它之后的、互不重叠的 OOS。

**"默认参数"基线的定义**：优化器是 goal-无关的独立工具，基线 = 策略 `init({})` 的内置默认（不依赖 goal.params）。建议每个 `param_space` 把内置默认值**也列为候选**（如 ma_cross 默认 5/20 落在 `{3,5,8,10}×{20,30,60}` 内），于是基线就是网格里的"默认组合"，采纳门即"训练最优组合 必须在 OOS 上比默认组合赢出余量"。消费侧 `resolve_params`（§6）用 tuned 覆盖 goal.params——若用户在 goal.params 里手动改了某键，该键仍按 goal.params 生效（调优只覆盖它产出的键），两者并存，v1 接受此口径。

## 4. 参数空间声明（策略侧）

`BaseStrategy` 加可选类属性：
```python
class BaseStrategy(ABC):
    param_space: dict[str, list] = {}   # 空 = 不调参（保持现状）
```
给 6 个策略各声明一个**小网格**（2–3 个参数、每个 ≤ ~4 个候选值，组合数 ≤ cap）。示例：
```python
class MACrossStrategy(BaseStrategy):
    param_space = {"short_period": [3, 5, 8, 10], "long_period": [20, 30, 60]}
```
其余 `macd_cross / bollinger_band / rsi_ob_os / turtle_breakout / ma_volume` 在实现时按各自 `init()` 读取的参数声明同等小网格；`param_space` 为空的策略自动跳过（不调参）。co-location：旋钮与策略同处，新策略自带可调性。

## 5. 持久化

新表 `strategy_params`（存 per-account 交易库，与 goal 同库）：
```sql
CREATE TABLE IF NOT EXISTS strategy_params (
    strategy_name TEXT PRIMARY KEY,
    params_json TEXT NOT NULL,
    oos_sharpe REAL,
    baseline_oos_sharpe REAL,
    accepted INTEGER NOT NULL,      -- 1=采纳调优参, 0=保留默认
    n_combos INTEGER,              -- 实际试的组合数
    universe_size INTEGER,
    tuned_at TEXT NOT NULL
);
```
DB 方法：
- `save_optimization(name, params, oos_sharpe, baseline, accepted, n_combos, universe_size)` — upsert。
- `get_active_params(name) -> dict | None` — `accepted=1` 时返回 `params_json`，否则 `None`（用默认）。
- `list_optimization_results() -> list[dict]` — 全部行（含未采纳），供 UI 表。

## 6. 消费接线（关键）

单一 helper：
```python
def resolve_params(db, strategy_name: str, goal) -> dict:
    base = dict(goal.params or {})
    tuned = db.get_active_params(strategy_name)
    if tuned:
        base.update(tuned)   # 调优参覆盖 goal 默认
    return base
```
在**每处** `strategy.init(cfg)` 用它替代当前的 `cfg = goal.params or {}`：
- `selector.evaluate` 的 walk-forward factory（`selector.py:153-157`）；
- runtime 部署所选策略时的 init；
- 经 selector 的回测路径。
没调过 / 未采纳的策略 = 现状不变。实现时需枚举并覆盖所有 init 点（教训同源于 protections 的"所有 BUY 入口统一过闸"）。

## 7. 前端（Vue）

**Agent 视图新增「参数优化」卡**（`web/src/views/Agent.vue`，置于"最近策略评估"附近）：
- **「运行优化」按钮** → 异步 job + 轮询，复用现有 `syncQuotesAsync`/`fetchQuotesSyncStatus` 模式：`POST /agent/optimize/async → {job_id}`；`GET /agent/optimize/status?job_id= → {current,total,current_strategy,results,status}`。运行中显示进度（第 X/Y 个策略）。
- **结果表**（每策略一行）：默认 OOS sharpe｜调优 OOS sharpe｜是否采纳｜调优参数｜试了几组｜上次优化时间。
- **「已调优」徽章**：在现有"最近策略评估"表给当前生效调优参的策略打标，区分 agent 用的是调优参还是默认。

**`web/src/api/client.ts` 增**：`OptimizeResultItem` / `OptimizeStatus` 接口 + `runOptimizeAsync()` / `fetchOptimizeStatus(jobId)` / `fetchTunedParams()`，沿用现有 axios 风格。

## 8. 后端接口

- **`HyperOptimizer`**（`quanti/agent/hyperopt.py`，纯编排，吃一个 `BacktestEngine`）：
  - `optimize(strategy_cls, codes, end) -> OptimizeResult`
  - `optimize_all(strategy_classes, codes, end, progress=None) -> list[OptimizeResult]`（`progress(i, total, name)` 回调供异步 job 更新状态）
  - `OptimizeResult(strategy_name, accepted, chosen_params, default_params, tuned_oos_sharpe, default_oos_sharpe, n_combos_tried, n_combos_total, reason)`
- **CLI** `quanti optimize [--universe POOL] [--end DATE]`：同步跑 `optimize_all`、打印结果表、落库。复用 backtest CLI 的 engine/provider 构造。
- **异步 API job**：`POST /agent/optimize/async`、`GET /agent/optimize/status`，复用现有异步 job 机制（如 quotes 同步 job 的 job 注册 + 状态轮询）；后台线程跑 `optimize_all`，`progress` 回调更新 job 状态，结束写库。
- **`GET /agent/tuned-params`**：返回 `list_optimization_results()` 给 UI。

## 9. 默认值（可调）

| 项 | 默认 |
|------|------|
| 训练期 `train_days` | 365 |
| OOS 折 | `n_folds=3, test_days=21, warmup_days=120`（沿用 selector 现值） |
| 网格上限 `max_combos` | 64（超出固定种子随机采样 + log） |
| universe 上限 | 复用 `selector_max_universe`（100） |
| 采纳余量 `accept_margin` | 0.1（且 tuned oos_sharpe > 0） |
| 守卫 | `min_folds=2`, `min_trades_oos=5` |
| 随机种子 | 42（采样可复现） |

## 10. 成本与诚实

- 凡封顶（网格、universe、折数）必 `log` 丢弃量，不静默截断。
- 优化是重计算：CLI 同步会跑几分钟～十几分钟；UI 走异步 job 避免 HTTP 超时。
- 结果表显式展示"试了几组 / 是否采纳"，让用户看清优化覆盖范围与决策。

## 11. 测试

**后端单测（合成 engine/strategy）**：
- 无泄漏切分：`train_end < 最早折.warmup_start`；构造数据断言训练期与验证期不重叠。
- 采纳门：调优明显赢过默认 → 采纳；调优更差 / 仅微弱 → 不采纳、保留默认；`param_space` 空 → 跳过（accepted=False, 用默认）；守卫不过（折/成交太少）→ 不采纳。
- 网格封顶：组合 > cap → 随机采样到 cap 且记录 `n_combos_total`，log 丢弃。
- DB：`save_optimization` → `get_active_params`（accepted=1 返回参数 / 0 返回 None）/ `list_optimization_results`。
- `resolve_params`：有采纳调优参 → 覆盖 goal.params；无 → goal.params 原样。
- 接线：selector / runtime 实例化策略时带上 `resolve_params` 的结果（spy/断言）。
- 异步 API job：start → status 轮询 → 结束带 results（集成测试）。

**前端**：若仓库有前端测试基建则加最小组件测试；否则经 dev server 手测（按现状记入 PR 说明），不为此引入新测试栈。

**回归**：现有全量测试保持绿；`ruff check` 干净。

## 12. 已知 v1 限制 / 后续

- 训练期打分用单次训练回测 sharpe（非训练期内再做一层 walk-forward）；可后续细化。
- v1.1：Bayesian/optuna 搜索替换 grid（应对大/连续空间）。
- v2：定时自动调参（调度器，方案 C）。
- 调优参存 per-account 交易库；若要跨账户共享可后续移到 market.db。
- 训练期打分目标可配（sharpe vs 复合分），v1 固定 sharpe。
