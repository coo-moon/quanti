# 设计：可组合 Protections 风控层（v1）

**日期**: 2026-06-21
**借鉴来源**: freqtrade protections（见 `docs/2026-06-20-reference-mature-quant-systems.md` 第 ④ 项）
**状态**: 已对抗式评审（4 视角，见会话），待用户复核 → writing-plans

## 1. 目标与非目标

**目标**：在已硬化的风控核心之上，加一层**可组合、可插拔**的"保护"——在连续止损或近期回撤过深时**软锁新买入**，给市场/策略喘息。两条 v1 保护：

- **StoplossGuard（连损冷却）**：近窗口内止损离场过于频繁 → 锁新买。
- **MaxDrawdown（回撤软锁）**：近窗口净值回撤过深 → 全局锁新买，比 -15% 硬熔断更早更浅。

**非目标 / 明确不做（v1）**：
- 不强制卖出。protections **只锁 BUY**；止损/止盈/策略离场永远照跑。
- 不持久化"锁对象"、不加新表。锁状态每周期由 DB/内存**事实重建**。
- v1 不做 CooldownPeriod（单票冷却）、LowProfit（低胜率锁）——留 v2。
- v1 不加结构化 `exit_kind` DB 列（用共享常量 + 双重匹配兜底，见 §6）；列为 v1.1 加固项。

## 2. 锁语义：触发后 K 交易日前向锁（核心决策）

放弃"纯滑动窗口判据"（评审一致否决：丢失 freqtrade `stop_duration` 的确定性冷却与滞回，靠事件老化而非净值恢复解锁，且 MaxDrawdown 用"今值 vs 窗口峰"会在反弹时漏锁、在旧峰老化当天"坑底松手"）。

采用**统一的无状态前向锁判据**——仍 100% 由事实推导、无锁对象、重启不失忆：

> **今天被某保护锁住 ⟺ 最近 K 个交易日内存在任一"触发日"。**
> 用 `count_trading_days_between(trigger_day, today) <= K` 判定（含现成交易日历）。

- **StoplossGuard 触发日 `d`**：以 `d` 结尾的 `W_sg` 交易日窗口内，止损离场次数 `>= N`。
- **MaxDrawdown 触发日 `d`**：以 `d` 结尾的 `W_md` 交易日净值窗口内，**真实峰谷最大回撤**（running peak-to-trough，非"今值 vs 峰"）`<= 阈值`。

判定"今天是否锁"时，对最近 `K` 个交易日的每一天 `d` 评估其是否为触发日；任一成立即锁。所需事实只覆盖最近约 `K + W` 个交易日（≈15–20 天），开销可忽略。

**有意的取舍（写进文档与代码注释）**：锁定期内若出现**新的**触发日（持续止损 / 持续创新低），锁会顺延——这比 freqtrade"自首次触发固定时长"**更保护**（持续承压时持续停手）。不构成无限续期：只有在事实上反复触发时才延长，承压消失后 `K` 个交易日内自然解除，且不在阈值边界抖动（一旦触发，`K` 天内稳定锁定）。

**确定解锁日**：`最近触发日 + K 个交易日`，可在 status/UI 显示"因 X 协议锁定买入，预计 Y 解锁"。

### 默认参数

| 保护 | 窗口 W | 触发阈值 | 锁期 K | 范围 |
|------|------|------|------|------|
| StoplossGuard | `W_sg = 5` 交易日 | `N = 3` 次止损 | `K_sg = 5` 交易日 | 全局 |
| MaxDrawdown | `W_md = 10` 交易日 | `-8%` 窗口峰谷回撤 | `K_md = 10` 交易日 | 全局 |

约束：软锁阈值 `-8%` **必须严格浅于** -15% 硬熔断，否则软锁形同虚设（见 §7）。全部可经 `ProtectionConfig` 调。

## 3. 架构：纯逻辑 + 抽象事实接口（方案 A）

`ProtectionManager` 是**纯逻辑**，吃一个与 broker/DB 无关的**事实接口**，逻辑层不关心事实来自 DB 还是内存。这样 live/paper 从 DB 喂、回测从内存喂，**同一份逻辑、同一份单测**——这正是守住 backtest≡live 的本体。

```
ProtectionContext (Protocol / 抽象事实供给):
    today: date
    stop_loss_exit_dates(self) -> list[date]      # 近 (K_sg+W_sg) 交易日内止损离场的成交日
    equity_series(self) -> list[tuple[date, float]]  # 近 (K_md+W_md) 交易日的 (日期, total_value)，按日期升序
    trading_days_between(self, start: date, end: date) -> int  # 注入交易日历，保持纯逻辑

DBProtectionContext(ProtectionContext)      # live/paper：从 SQLite 读
MemoryProtectionContext(ProtectionContext)  # 回测：从引擎内存结构读
```

```
risk/protections.py:
    @dataclass ProtectionConfig            # 开关 + 各阈值（见 §2 默认表）
    class Protection(Protocol):
        def lock_reason(self, ctx) -> str | None   # None=放行；非空=被锁原因（含解锁日）
    class StoplossGuard(Protection)
    class MaxDrawdown(Protection)
    class ProtectionManager:
        def check_entry(self, ctx, code: str | None = None) -> tuple[bool, str]
        # 聚合所有启用保护，任一锁住即 (False, reason)；reason 标明哪个协议 + 预计解锁日
```

`check_entry` v1 两条都是全局锁，`code` 形参默认 `None`、暂不使用，为 v2 的单票冷却（CooldownPeriod）预留。

## 4. 集成点

### 4.1 live / paper（PaperBroker、QmtBroker）

把两个 broker 里散落的 `self._risk.check(signal, portfolio)`（PaperBroker `paper_broker.py:197/236/331`、QmtBroker `qmt_broker.py:174`）**收口**进一个私有 `_entry_allowed(signal, portfolio) -> (ok, reason)`：

```
def _entry_allowed(self, signal, portfolio):
    ok, reason = self._risk.check(signal, portfolio)   # 既有硬限
    if not ok:
        return ok, reason
    if signal.direction == Direction.BUY and self._protections.config.enabled:
        ctx = self._build_protection_context()         # 从 self._db 构造
        plocked, preason = self._protections.check_entry(ctx)
        if plocked:
            return False, preason
    return True, ""
```

- 只对 BUY 生效（`check` 对 SELL 永远放行，故包裹安全）。
- **所有 BUY 入口**（含加仓 add-on）必须走 `_entry_allowed`，不得有绕过路径（教训同源于审计"单票/总仓事后口径要在所有入口生效"）。
- 被锁 → 拒买并写 `agent_decisions`，`kind="protection_block"`，summary 含协议名 + 预计解锁日，使"今天没买"有可见、可审计的原因，非静默。

### 4.2 回测（BacktestEngine）

- 给 `BacktestEngine.__init__` 加可选 `protection_manager: ProtectionManager | None = None`（默认 None，向后兼容）。
- 引擎内存已有：`equity_values: dict[date, float]`（`engine.py:223`）、`trades: list[TradeRecord]`（`strategy=='risk_exit'` 且 `reason` 带止损前缀即止损离场，`engine.py:385/432`、`manager.py:154`）。用一个 `MemoryProtectionContext` 把这两者切窗口喂给**同一个** `ProtectionManager`。
- 在引擎 BUY 闸（`self._risk.check` 旁，`engine.py:216`）对 BUY 多加一道 protection 闸；被锁则计入 `skipped_signals` 并 `continue`。
- protections 自身**无状态、只读事实流**，不依赖引擎每 bar 调的 `reset_daily()`（`engine.py:166`）的日历语义。

### 4.3 配置接线

- 各 broker / 引擎构造处与 `risk_config` 并列加 `protection_config: ProtectionConfig | None`（`cli.py`、`api/routes.py`、`agent/selector.py`、`agent/runtime.py`、`mcp_server.py`）。
- **回测开关 `apply_protections_in_backtest`（默认开）**：selector/walk-forward 评估路径默认带 protections，使策略排名与实盘口径一致，堵住"高估在回撤里加仓类策略 → 选出实盘不会执行的策略"的缝（与审计 metric-selection HIGH 项同源）。用户手动单策略回测可关。

## 5. 数据流（DBProtectionContext 的查询）

- **止损离场日期**：新增 `db.stop_loss_exit_dates(since: date) -> list[date]`，查
  `orders WHERE direction='sell' AND status='filled' AND strategy_name='risk_exit'
   AND reason LIKE '{prefix}%' AND filled_at >= since`，取 `filled_at` 的日期。
  口径：用**成交离场日 `filled_at`**（仓位实际亏损离场之日），非信号生成日；未成交不计。pending 模式下止损卖单次日开盘成交，`filled_at` 即那天。
- **净值序列**：复用 `db.get_portfolio_snapshots(limit)`（`snapshot_date, total_value`），切近 `K_md+W_md` 交易日窗口、升序。
- 交易日距离：`count_trading_days_between`（`utils/market.py:127`，计 `(start, end]`）。

## 6. 止损识别契约（脆弱字符串 → 代码契约）

当前止损 reason 在 `RiskManager.check_exits`（`manager.py:154`）产出，形如 `f"止损 {pnl} ≤ ..."`；同一 `strategy_name="risk_exit"` 下还有"移动止盈""策略离场信号"两类，**不可误计入连损**。

- 在 `manager.py` 提模块常量 `STOP_LOSS_REASON_PREFIX = "止损"`，`check_exits` 产出与 protections 识别**共用**，消除散落字面量。
- 识别用**双重匹配**：`strategy_name == "risk_exit"` 且 `reason.startswith(STOP_LOSS_REASON_PREFIX)`。
- **不变量单测**：断言 `check_exits` 触发止损时 reason 命中常量；断言移动止盈/策略离场**不**命中。
- 处理遗留：`RiskManager.check_stop_loss`（`manager.py:114`）用英文 `"Stop loss triggered: ..."`。实现时确认它在 live/回测路径已不产出落库订单（brokers 与引擎均走 `check_exits`）；若仍有产出方，统一到常量或在识别中一并覆盖。
- v1.1 加固方向：升级为结构化 `orders.exit_kind` 枚举列（彻底摆脱文案漂移），届时迁移识别逻辑。

## 7. 两层回撤风控的关系（文档须讲清）

| 层 | 口径 | 触发动作 | 峰来源 |
|------|------|------|------|
| MaxDrawdown 软锁（本设计） | 近 `W_md` 交易日**窗口**峰谷回撤 ≤ -8% | 仅锁新买，持仓不动 | 窗口内 running peak |
| -15% 硬熔断（已存在） | 全历史高水位回撤 ≤ -15% | 清仓 + 暂停 agent | `get_peak_total_value`=全历史 MAX |

- 软锁更早更浅、可自愈（`K_md` 内退出）；硬熔断更深、清仓停机。
- 硬熔断清仓 + agent 暂停后恢复时：净值窗口仍含熔断前深跌，MaxDrawdown 软锁可能在恢复后再锁至多 `K_md` 个交易日——视为"刚经历 -15% 后谨慎建仓"，可接受，且会自然退出。文档写明。

## 8. 边界与稳健性

- **快照点数不足**：净值窗口内快照点数 `< max_drawdown_min_points`（`ProtectionConfig` 字段，默认 `5`）→ MaxDrawdown **fail-open 放行**，不在薄数据上凭空锁仓（仿选股 min-OOS-fold 守卫）。StoplossGuard 无此问题（无止损事件即不触发）。
- **快照缺日**：某交易日漏写快照（停机/异常）→ 窗口按"现有点"算，配合 fail-open；MaxDrawdown 用窗口内现有点的 running peak-to-trough，不补插。
- **交易日历**：所有窗口/锁期用交易日（非自然日）。`is_trading_day` 在 `trade_calendar` 为空时退化为"工作日"，会令窗口偏移——**上实盘前确保 `trade_calendar` 已 sync**（写进 QMT 路线图待办）。
- **MaxDrawdown 触发日定义**：以 `d` 结尾窗口的真实峰谷回撤 ≤ 阈值即 `d` 为触发日；"今天锁"= 最近 `K_md` 交易日内存在触发日。无需单独维护"首破日"状态，天然处理"锁期内创新低"= 出现新触发日 → 顺延（见 §2）。
- **回测 vs live 触发日口径对齐**：回测止损事件日 = `TradeRecord.date`（次日开盘成交日），live = `orders.filled_at` 之日，二者都是**实际成交离场日**，语义一致。

## 9. 测试计划

**纯逻辑单测 `tests/test_protections.py`（用 fake ProtectionContext，免 DB）**：
- StoplossGuard：窗口内 `< N` → 放行；`>= N` 触发日后 `K` 日内锁、第 `K+1` 日解；锁期内新触发日顺延；按全局。
- MaxDrawdown：窗口峰谷回撤优于阈值 → 放行；`<= 阈值` 触发后 `K` 日内锁、之后解；**"先深跌 -12% 后反弹 -7%"仍锁**（真实峰谷，非今值）；**孤立旧高点不卡死、其老化不致净值未恢复时解锁**。
- 聚合：任一锁住即拒，reason 标明协议 + 解锁日；`enabled=False` 恒放行；薄数据 fail-open。
- 识别：任意移动止盈 / 策略离场**不**被计入连损。

**集成测试**：
- `tests/test_paper_broker.py`：种入近窗止损离场 / 回撤后，BUY 被拦且写 `protection_block`；SELL 仍通过；加仓入口同样被拦。
- `tests/test_backtest.py`：注入 ProtectionManager，构造连损 / 深回撤场景，断言其后的 BUY 被 skip 且计入 `skipped_signals`；不注入时行为不变（向后兼容）。
- 全量回归：现有 337 测试保持绿（尤其 `test_backtest` 的 `risk_exit`、`test_qmt_broker`、`test_risk`）。

## 10. 落地顺序（writing-plans 细化）

1. `risk/protections.py`：`ProtectionConfig` + `ProtectionContext` 协议 + `StoplossGuard`/`MaxDrawdown`/`ProtectionManager`（纯逻辑）+ 单测。
2. `manager.py`：`STOP_LOSS_REASON_PREFIX` 常量，`check_exits` 改用之 + 不变量单测。
3. `database.py`：`stop_loss_exit_dates(since)` 查询；`DBProtectionContext`。
4. PaperBroker / QmtBroker：`_entry_allowed` 收口 + `protection_config` 接线 + `protection_block` 决策日志 + 集成测试。
5. BacktestEngine：`MemoryProtectionContext` + 可选 `protection_manager` + BUY 闸 + `apply_protections_in_backtest` 开关 + selector/runtime 接线（默认开）。
6. 各构造处（cli/api/selector/runtime/mcp）接线；全量回归 + ruff。
7. 文档：两层风控关系、`# VERIFY`（trade_calendar sync）写进 QMT 路线图待办。

## 11. 已知 v1 限制 / 后续

- v1.1：结构化 `orders.exit_kind` 列替代 reason 前缀识别。
- v2：CooldownPeriod（单票冷却，`check_entry(code,...)` 已预留）、LowProfit（低胜率锁，需足够已平仓样本 + 可靠实现收益）。
- 可选：MaxDrawdown"回撤收复过半提前解锁"（v1 用纯前向 `K` 日锁，简单确定；如嫌偶发卡太久再加）。
