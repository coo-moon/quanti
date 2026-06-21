# 设计：声明式、防前视的因子 Pipeline（Factor DSL, v1）

**日期**: 2026-06-21
**借鉴来源**: zipline Pipeline + Qlib 因子表达式引擎（见 `docs/2026-06-20-reference-mature-quant-systems.md` 第 ② 项）
**状态**: 已 brainstorm 收敛，待用户复核 → writing-plans

## 1. 目标与非目标

**目标**：把手写的因子函数（`factors/cross_sectional.py` 里 5 个 `factor_*(bars)`）换成一套**声明式 Python 对象 DSL**——因子由 `Ref / Mean / Std / Log / 四则` 等原语组合而成，**结构上不可能前视**（没有任何引用未来的原语），可组合、可单测、可批量回测。外层横截面处理与消费方**保持不变**（drop-in）。

**非目标 / 明确不做（v1）**：
- 不做字符串表达式 parser（Qlib 风格 `"Ref($close,126)"`）——留给将来 ⑥（LLM 生成因子）。
- 不做全 panel 向量化重写；不把横截面算子（Rank/CSZScore/行业中性）放进 DSL——pipeline 已做跨票 zscore/winsorize/行业中性。
- 不新增因子；只**等价移植**现有 5 个。
- 不改 `compute_factor_panel` 的横截面管线、不改 `fuse_buy_signals` 消费方。

## 2. DSL 模块 `quanti/factors/expr.py`

表达式树：`Expr` 抽象基类，核心方法 `evaluate(ctx: EvalContext) -> pd.Series`（按日期索引、单只票的因子时间序列）。

```python
class EvalContext:
    """持有单只票的 bars：列 open/high/low/close/volume/turnover，按日期升序。
    构造时若有 `date` 列则 set_index('date')，使求值产出的 Series 按日期索引
    （便于 as-of 取值与不变量测试）；缺列则用默认 RangeIndex（shift/rolling
    是位置运算，行序不变即可）。求值器只拿到 ≤ as_of 的数据（见 §3）。"""
    def __init__(self, bars: pd.DataFrame): ...
    def field(self, name: str) -> pd.Series: ...   # 返回某列，索引=日期/行序

class Expr(ABC):
    def evaluate(self, ctx: EvalContext) -> pd.Series: ...
    # 运算符重载 → BinaryOp / UnaryOp，标量自动包成 Constant
    def __add__/__sub__/__mul__/__truediv__/__neg__(...)
    def __radd__/__rsub__/__rmul__/__rtruediv__(...)   # 标量在左
```

**数据项**（`Field` 的便捷子类/工厂）：`Close() Open() High() Low() Volume() Turnover()` → `ctx.field(name)`。

**时序原语（一律只回看）**：
| 原语 | 语义 | 实现 |
|------|------|------|
| `Ref(e, n)` | 滞后 n 个 bar | `e.evaluate(ctx).shift(n)`；**n≥0**，负数 `raise ValueError` |
| `Mean(e, n)` | n 窗滚动均值 | `.rolling(n).mean()` |
| `Std(e, n)` | n 窗滚动标准差 | `.rolling(n).std()`（ddof=1，pandas 默认） |
| `Sum(e, n)` | n 窗滚动和 | `.rolling(n).sum()` |
| `Max(e, n)` / `Min(e, n)` | n 窗滚动极值 | `.rolling(n).max()/.min()` |
| `Log(e)` | 自然对数 | `np.log(e.evaluate(ctx))` |
| `Constant(c)` | 标量常量 | 广播为同索引常量序列 |
| `BinaryOp` | `+ - * /` | 两子表达式逐元素运算 |
| `UnaryOp` | 一元负 | `-e` |

窗口不足时（前 n-1 行）`rolling`/`shift` 自然产出 `NaN`——与现有手写因子"历史不足返回 NaN"一致。

## 3. 结构防前视（核心保证）

- **没有任何"前移"原语**：`Ref` 只接受 `n≥0`（负数报错）；`rolling` 窗口始终在当前行**结束**；`shift(n≥0)` 只把过去搬到现在。因此任意 DSL 表达式在日期 t 的值，**只可能依赖 ≤ t 的数据**。
- 求值器拿到的数据本就 ≤ as_of（`compute_factor_panel` 用 `provider.get_daily_df(code, start, as_of)`）。
- 配合**次日开盘执行**（与回测一致），无 look-ahead。
- **不变量测试（防前视的证明）**：对一组 bars 计算因子序列；在尾部**追加任意未来 bar** 后重算；断言**日期 t 处的因子值不变**。手写 Python 给不了这个保证，DSL 结构上给得了。

## 4. 移植现有 5 因子（等价、行为不变）

`quanti/factors/library.py` 用 DSL 定义（`close = Close()` 等）：

| 因子 | DSL 表达式 | 对照旧实现 |
|------|------|------|
| `momentum_3m` | `Ref(close,21) / Ref(close,63) - 1` | `_cum_return(close, 63, 21)` |
| `momentum_6m` | `Ref(close,21) / Ref(close,126) - 1` | `_cum_return(close, 126, 21)` |
| `reversal_1w` | `-(close / Ref(close,5) - 1)` | `-_cum_return(close, 5, 0)` |
| `turnover_20d` | `-Mean(Turnover(), 20)` | `-mean(turnover[-20:])` |
| `realized_vol_20d` | `-Std(Log(close / Ref(close,1)), 20) * (252 ** 0.5)` | `-std(logret[-20:])*sqrt(252)` |

口径核对：`Ref(x,n)` 在末行 = `x.iloc[-1-n]`，故 `Ref(close,21)/Ref(close,63)` 末行 = `close[-22]/close[-64]`，与 `_cum_return(start=63,end=21)` 的 `p_end/p_start = close[-22]/close[-64]` 一致。realized_vol 的 `Std(...,20)` 对 20 个对数收益求 ddof=1 标准差，与旧 `rets.std()` 一致。

**等价测试**：对样本 bars，每个因子 `as_factor_fn(expr)(bars)` 的值 ≈ 旧 `factor_*(bars)`（移植前先固化旧值或临时保留旧函数对比）。

## 5. drop-in 适配器（外层不变）

`library.py` 提供：
```python
def as_factor_fn(expr: Expr) -> FactorFn:
    """把 Expr 包成现有 FactorFn = Callable[[pd.DataFrame], float]：
    取 as_of（最后一行）的值。NaN/异常按现状返回 NaN。"""
    def fn(bars: pd.DataFrame) -> float:
        s = expr.evaluate(EvalContext(bars))
        return float(s.iloc[-1]) if len(s) else float("nan")
    return fn
```
`cross_sectional.py` 的 `DEFAULT_FACTORS` 改为 `{name: as_factor_fn(expr)}`（引 library）。**`compute_factor_panel`、`_winsorize/_zscore/_industry_demean`、`rank_by_composite`、`fuse_buy_signals` 全部零改动**——因为 `FactorFn` 契约不变。删除旧 `factor_*` 手写函数（`_cum_return` 若仅被它们使用则一并删；若他处仍用则保留）。

## 6. 批量求值

`.evaluate()` 本就返回整条按日期的序列——天然"为每个历史日算出因子"。加一个便捷函数：
```python
def evaluate_series(expr: Expr, bars: pd.DataFrame) -> pd.Series:
    return expr.evaluate(EvalContext(bars))   # date → factor value
```
v1 主消费仍走 `as_factor_fn`（as_of 末行口径）；`evaluate_series` 供批量回测/单测/将来因子研究用。

## 7. 模块布局

- **新增** `quanti/factors/expr.py`：`Expr / EvalContext / Field 与数据项 / Ref/Mean/Std/Sum/Max/Min/Log/Constant / BinaryOp/UnaryOp / 运算符重载`。
- **新增** `quanti/factors/library.py`：5 个因子表达式 + `as_factor_fn` + `evaluate_series` + `DEFAULT_FACTOR_EXPRS` 映射。
- **改** `quanti/factors/cross_sectional.py`：`DEFAULT_FACTORS` 改引 `library`；移除手写 `factor_*`（按引用情况处理 `_cum_return`）。pipeline 其余不动。
- `registry.py`（独立的通用注册表）不在本次范围，保持不动。

## 8. 测试

`tests/test_factor_expr.py`（新）：
- 原语单测：`Ref/Mean/Std/Sum/Max/Min/Log/Constant` + 四则 + 标量左乘，在小合成序列上对拍手算值。
- **防前视不变量**：追加未来 bar 后，过去日期的因子值不变。
- `Ref(e, -1)`（负滞后）`raise ValueError`。
- 窗口不足 → NaN。

`tests/test_factor_library.py`（新）或并入现有 `tests/test_factors_cross.py`：
- 5 因子**等价测试**：DSL 值 ≈ 旧手写值（样本 bars）。
- `compute_factor_panel` 小 universe **回归**：DSL 接入后 panel 的 composite 排名/数值与移植前一致（用固定合成数据）。
- 现有 `tests/test_factors_cross.py` 保持绿（必要时按新 DEFAULT_FACTORS 调整 import）。

全量 pytest 绿；`ruff check` 干净。

## 9. 已知 v1 限制 / 后续

- v1.1：横截面 DSL 原语（`Rank / CSZScore / IndNeutralize` 进 DSL），让横截面因子也声明式表达。
- ⑥：字符串表达式 parser（在对象 DSL 之上套一层，LLM 生成因子直接吐字符串 → 解析为 Expr），含防注入。
- 规模化：全 panel 向量化求值（日期×代码），应对数千票宇宙。
- 因子级回测/IC 评估工具（用 `evaluate_series` 批量算因子 + 计算 IC/分层收益）。
