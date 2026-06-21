# 设计：LLM 因子挖掘闭环（v1）

**日期**: 2026-06-21
**借鉴来源**: Qlib RD-Agent / QuantaAlpha（见 `docs/2026-06-20-reference-mature-quant-systems.md` 第 ⑥ 项）
**状态**: 已 brainstorm 收敛，待用户复核 → writing-plans
**依赖**: ② 因子 DSL（`quanti/factors/expr.py` + `library.py`，PR #25）。本分支基于 `feat/factor-pipeline`；#25 合入 main 后 rebase。

## 1. 目标与非目标

**目标**：让 LLM **自动挖因子**，但用规则严格把关——契合 quanti "规则验证、LLM 只做加法"：LLM 提候选因子（创意），rank-IC 走查闸门裁决（只收真有效的），采纳的进**自进化因子库**。闭环：LLM 生成因子表达式 → 安全解析 → 训练/OOS IC 闸门 → 落库 → 经开关并入 composite。

**非目标 / 明确不做（v1）**：
- **不生成策略/选股器 Python 代码**（需沙箱执行任意代码——安全爆炸）。只生成**因子表达式**（走 ② DSL 白名单解析，不执行任意代码）。
- 不做组合增量评估（v1 用单因子 rank-IC）；不做多轮 LLM 反思迭代；不做 IC 衰减自动下架。
- 解析器仅覆盖 ② 现有原语（`Ref/Mean/Std/Sum/Max/Min/Log` + 数据项 + 四则）。
- 采纳因子**默认不进实盘** composite（见 §5）。

## 2. 安全解析器 `quanti/factors/parser.py`

`parse_expr(s: str) -> Expr`：把 LLM 文本转成 ② 的 `Expr`。**绝不 `eval`/`exec`**。基于 `ast.parse(s, mode="eval")` + 白名单递归下降：

| AST 节点 | 允许 | 映射 |
|------|------|------|
| `ast.Name` | 仅 `close/open/high/low/volume/turnover` | → `Field`（`Close()` 等） |
| `ast.Call` | 仅函数名 `Ref/Mean/Std/Sum/Max/Min/Log` | → 对应 Expr 节点；参数递归解析（窗口须 int 常量） |
| `ast.BinOp` | `+ - * /` | → `BinaryOp` |
| `ast.UnaryOp` | `USub`（一元负） | → `UnaryOp("neg", …)` |
| `ast.Constant` | int/float | → `Constant` |
| 其它 | **全拒** | `raise FactorParseError` |

约束：表达式长度上限、AST 深度上限（防 DoS）；`Ref/Mean/Std/Sum/Max/Min` 的窗口参数必须是正整数常量；未知名字/属性/下标/lambda/推导式一律拒。失败抛 `FactorParseError`，矿工捕获并丢弃该候选。

## 3. IC 评估器 `quanti/factors/evaluation.py`

`factor_ic(expr, provider, db, codes, eval_dates, fwd_days=5) -> float`：**rank-IC**（信息系数）。
- 每个 `eval_date`：横截面取每只票的因子值（用 ② `evaluate_series`，as-of 该日末值）+ 未来 `fwd_days` 收益 `close(t+fwd)/close(t)-1`；对该横截面算 Spearman 相关。
- 跨所有 eval_dates 取均值 → 单一 IC。样本不足（票数/日数太少）→ 返回 `nan`（仿选股 min-fold 守卫）。
- **IC 用未来收益是研究/评估口径**（在历史上算，未来已知，合法）；因子本身仍由 ② 结构防前视。

训练/OOS 切分复用 walk_forward 口径：训练期 eval_dates 算 train_ic（过滤），OOS 期 eval_dates 算 oos_ic（采纳门）。

## 4. 因子矿工 `quanti/agent/factor_miner.py`

`mine_factors(llm, db, provider, codes, *, n_candidates, ...) -> list[MineResult]`：
1. 建 prompt：DSL 语法说明 + 现有因子（`DEFAULT_FACTORS` 名+表达式 + 已采纳的 generated）作范例 + "提 K 个**新且彼此多样**的因子，每行 `name: expression`"。
2. `_complete_text(llm, system, user, cfg)`（复用 ⑤ `_build_llm_client` 选 provider）→ 解析回 `(name, expr_str)` 列表。
3. 逐候选：`parse_expr`（失败跳过、记原因）→ `factor_ic` 训练期过滤 → OOS IC → **采纳门（精确）**：
   `accepted = (|train_ic| ≥ min_train_ic) AND (oos_ic ≥ oos_ic_threshold) AND (与每个已采纳因子的横截面因子值 |corr| < redundancy_max)`。
   即 `oos_ic_threshold` 就是"跑赢基线"的门槛（一个固定 IC 地板，而非与现有因子比中位数——v1 保持口径简单确定）；去冗余防收进与已有因子高度重复的因子。任一条件不过 → 不采纳（仍落库记原因）。
4. 每个候选（采纳与否）落 `generated_factors`。
5. 返回 `MineResult(name, expr_str, train_ic, oos_ic, accepted, reason)` 列表。

`MineResult` dataclass 字段同上。LLM 不可用（无 key / 未装）→ 返回空 + 记 `llm_unavailable`，优雅降级。

## 5. 落库 + 接入

**新表 `generated_factors`**（per-account 交易库）：
```sql
CREATE TABLE IF NOT EXISTS generated_factors (
    name TEXT PRIMARY KEY,
    expr_str TEXT NOT NULL,
    train_ic REAL,
    oos_ic REAL,
    accepted INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,   -- 人工开关（UI 可切）
    created_at TEXT NOT NULL
);
```
DB 方法：`save_generated_factor(...)`（upsert）、`list_generated_factors() -> list[dict]`（UI 全量）、`set_factor_enabled(name, enabled)`（UI 开关）、`load_active_factor_fns(db) -> dict[str, FactorFn]`（`accepted=1 且 enabled=1` → `{name: as_factor_fn(parse_expr(expr_str))}`；解析失败的跳过并 log）。

**接入点（仅一处）**：`compute_factor_panel` 加参数 `include_generated: bool = False`。为 True 时因子集 = `DEFAULT_FACTORS + load_active_factor_fns(db)`。
- **回测 / selector / 研究路径**：传 `include_generated=True`（立刻能评估、进策略排名）。
- **实盘 runtime**：默认 `False`；经 `goal.params["use_generated_factors"]=True` 显式开启（人工复核后）。
下游 `_winsorize/_zscore/_industry_demean/composite → fuse_buy_signals → 选股 → 风控 → 下单` 全部不变。

## 6. 触发 + 前端

- **CLI** `quanti mine-factors [--universe POOL] [--n N] [--end DATE]`：同步跑 `mine_factors`、落库、打印结果表。
- **异步 API job**：`POST /agent/mine-factors/async`、`GET /agent/mine-factors/status`，复用现有 `sync_jobs` + executor 模式（同 ⑤ optimize job）；`GET /factors/generated` 返回 `list_generated_factors`；`POST /factors/generated/{name}/enabled` 切开关。
- **前端**（`web/src/views/Agent.vue`）新增「**因子挖掘**」卡：
  - **运行按钮**（异步 + 进度轮询）。
  - **实盘总开关「本账户实盘启用生成因子」**（醒目、默认关、带风险说明文案）——绑定 `goal.params.use_generated_factors`，经现有 `updateGoal` 或专用端点写回。这是"LLM 因子是否影响该账户真金白银"的总闸，必须 UI 可见可切。
  - **因子库表**（name｜表达式｜train IC｜OOS IC｜采纳｜**每因子启用开关**｜**生效中?**）。"生效中" = `accepted ∧ enabled ∧ 总开关开`，让你一眼看出此刻哪些因子真在影响排名。
  `client.ts` 加对应类型与调用（含读写总开关 + 每因子开关）。

## 7. 安全 / 防过拟合

- **解析白名单**是安全底线：LLM 输出只走 ② 原语解析，**不执行任意代码**；未知节点/属性/调用一律拒。
- **OOS IC 闸门 + 跑赢基线 + 去冗余**：防 LLM "编"出过拟合或与现有重复的因子。训练期挑、OOS 期验，与 ⑤ 同源的样本外纪律。
- 每次挖掘 cap 候选数（默认 K=10）+ universe 上限（复用 `selector_max_universe`=100）；LLM 调用按需（CLI/UI 手动），不进 agent 周期。
- 采纳因子实盘默认不启用（§5），人工复核 + 开关后才影响真金白银。

## 8. 默认值（可调）

| 项 | 默认 |
|------|------|
| `fwd_days`（IC 前瞻） | 5 |
| `oos_ic_threshold`（采纳门） | 0.03 |
| `min_train_ic`（过滤） | 0.02 |
| `redundancy_max`（\|corr\| 上限） | 0.7 |
| `n_candidates`（每次 K） | 10 |
| universe 上限 | `selector_max_universe`（100） |
| train/OOS 窗 | 沿用 walk_forward（n_folds=3, test=21, warmup=120 → train 取更早窗） |
| 实盘启用生成因子 | 关（goal.params 显式开） |

## 9. 测试

**后端单测**：
- `parser`：解析合法表达式 == 等价手搭 Expr；拒非法（`__import__`、属性、下标、未知函数、负窗口）抛 `FactorParseError`；不 eval（注入 `os.system(...)` 被拒）。
- `evaluation`：合成数据上 rank-IC——构造一个"因子值与未来收益强相关"的票池，断言 IC 高;无相关 → IC≈0;样本不足 → nan。
- `factor_miner`：注入 fake LLM（返回固定候选字符串）+ fake/真实 IC，断言采纳门（OOS 过/不过、冗余、解析失败跳过）、落库。
- DB：`save/list/set_enabled/load_active_factor_fns`（accepted&enabled 才返回；解析失败跳过）。
- `compute_factor_panel(include_generated=True)`：合成一个已采纳因子，断言它进了 panel 的因子列。
- 异步 API job 生命周期（注入 fake miner）。

**前端**：`cd web && npm run build` 通过（无 FE 测试基建则手测 + build 为门）。

**回归**：全量 pytest 绿；`ruff check` 干净。

## 10. 已知 v1 限制 / 后续

- 不生成策略/选股器代码（需沙箱）。
- 单因子 IC 闸（非"加进 composite 看组合 OOS 增量"）。
- 解析器只覆盖 ② 现有原语；新增原语需同步白名单。
- 后续：多轮 LLM 反思迭代、因子去相关组合优化、IC 衰减监控自动下架、字符串 parser 反哺 ② 让所有因子都可配置化。
