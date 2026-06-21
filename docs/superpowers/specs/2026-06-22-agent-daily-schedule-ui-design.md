# Agent 每日定时运行 — UI 控件设计

- 日期：2026-06-22
- 状态：设计已认可，待写实现计划
- 范围：在 Web 界面（`web/src/views/Agent.vue`）暴露 agent 的每日定时调度（`goal.params.daily_run_time` + `daily_trading_days_only`），并在保存后自动重启正在运行的 agent 使新调度即时生效。

## 1. 背景与动机

`AgentRuntime` 已经支持两种运行模式（`quanti/agent/runtime.py`）：

- **间隔模式（默认）**：`goal.params["daily_run_time"]` 未设置时，启动即跑一次，之后每 `tick_interval_sec`（默认 4 小时）跑一轮，不区分交易日。
- **每日定时模式**：`goal.params["daily_run_time"] = "HH:MM"` 时，等到每天该时刻跑一次；默认仅交易日（`daily_trading_days_only`，缺省 `True`），周末/节假日自动跳过（`runtime.py:200-247`、`_daily_runs_today` at `216-231`、`_next_wait_seconds` at `233-240`）。

后端逻辑已完整且有测试（`tests/test_agent_schedule.py`），但这两个参数**没有任何界面入口**：`目标设定` 卡片只暴露收益/回撤/风险/票池/选股器/策略，`Agent 模式` 卡片只暴露 `advParams` 那组开关。用户只能手动调 `POST /api/goal` 写 `params`。本设计补上 UI 入口。

参数读写已有通道无需新增表：`POST /api/goal`（`routes.py:666-686`，整体替换 `params`）；前端 `saveGoal()` → `syncParamsFromAdv()` → `updateGoal(goalDraft)`（`Agent.vue:911-923`、`592-613`、`client.ts:301`）。`syncParamsFromAdv()` 用 `{...existing, ...}` 展开，已保留它不认识的 `params` 键（`Agent.vue:593` 注释明确）。

## 2. 目标与非目标

**目标**
1. 在「目标设定」卡片底部加「运行计划」区块：`每日定时运行` 开关 + `运行时间`（HH:MM）+ `仅交易日运行` 复选框。
2. 保存时若调度变化且 agent 在运行，自动重启 agent 使其即时、干净地按新调度生效。
3. 修正状态区「下次」显示，在每日定时模式下显示下一个调度时刻，而非 `上次 + 间隔`。

**非目标**
- 不改动 `runtime.py` 的调度算法本身（已可用）。
- 不引入秒级/cron 表达式级调度；只支持单一每日时刻。
- 不为因子挖掘/调参加定时（与本特性无关）。

## 3. 设计

### 3.1 UI 控件（`web/src/views/Agent.vue` 模板）

在「目标设定」卡片（`<div class="card">` 内 `goal-grid` 之后、`actions` 之前，约 `Agent.vue:97` 与 `98` 之间）插入一个「运行计划」小区块，沿用现有控件风格：

```
运行计划
[☑] 每日定时运行
  ── 仅在开关为 true 时显示（v-if="advParams.daily_schedule_enabled"）──
  运行时间   [<input type="time" v-model="advParams.daily_run_time">]
  [☑] 仅交易日运行（周末/节假日自动跳过）
```

- 用 `<input type="time">`：浏览器原生 HH:MM 选择器，天然只产出合法 `"HH:MM"`，免格式校验。
- 开关关闭 = 间隔模式；时间/仅交易日两项隐藏。
- 文案下方补一行 `muted` 提示：节假日精度需 `quanti sync --calendar`，否则按周一~周五判定。

### 3.2 数据流（前端状态 ↔ goal.params）

给 `advParams`（`Agent.vue:555-569`）新增三个键：

| 键 | 类型 | 含义 |
|---|---|---|
| `daily_schedule_enabled` | bool | 仅 UI 本地状态；是否每日定时模式 |
| `daily_run_time` | string | `"HH:MM"`；写入 `goal.params.daily_run_time` |
| `daily_trading_days_only` | bool | 写入 `goal.params.daily_trading_days_only`；默认 `true` |

**`syncAdvFromParams()`（`Agent.vue:572-590`）新增反推：**
```
const drt = typeof p.daily_run_time === "string" ? p.daily_run_time : "";
advParams.daily_schedule_enabled = drt !== "";
advParams.daily_run_time = drt || "09:35";   // 关闭时给个合理默认，便于打开即用
advParams.daily_trading_days_only = p.daily_trading_days_only !== false; // 缺省 true
```

**`syncParamsFromAdv()`（`Agent.vue:592-613`）新增写出：**
```
const params = { ...existing, /* 现有键不变 */ };
if (advParams.daily_schedule_enabled && advParams.daily_run_time) {
  params.daily_run_time = advParams.daily_run_time;
  params.daily_trading_days_only = advParams.daily_trading_days_only;
} else {
  delete params.daily_run_time;            // 关闭 = 回到间隔模式
  // daily_trading_days_only 可保留亦可删除；删除更干净，本设计选择删除
  delete params.daily_trading_days_only;
}
goalDraft.params = params;
```

之后照旧 `updateGoal(goalDraft)` → `POST /api/goal`。后端无需改动即可消费。

### 3.3 自动重启（仅当调度变化）

**问题**：agent 运行时改 `daily_run_time`，循环正阻塞在 `self._stop_flag.wait(旧间隔)`，要等这一轮结束才 re-read 调度，且可能在错误点位触发。前端连续两次 `stop`→`start` 有竞态：旧线程可能尚未退出（`is_alive()` 仍真），`start()` 会因 `if self._thread and self._thread.is_alive(): return`（`runtime.py:128-129`）提前返回，导致没真正重启。

**方案（A）：后端提供安全的 restart。** 复用已有的 `shutdown()`（`runtime.py:159-171`，它会 `self._thread.join(timeout=2)` 等线程退出；`stop_flag.set()` 会让阻塞的 `wait()` 立即返回，故 join 很快），再 `start()`：

- 新增 `AgentRuntime.restart()`：
  ```
  def restart(self) -> None:
      """Reschedule cleanly: stop the loop thread (join) then start a fresh
      one, WITHOUT flipping persisted goal.enabled. Used when the schedule
      changes so the new daily_run_time takes effect immediately."""
      self.shutdown()   # sets stop_flag, joins thread; leaves goal.enabled untouched
      self.start()      # clears stop_flag, spawns new thread, re-affirms enabled
  ```
  - `shutdown()` 不动 `goal.enabled`，`start()` 又把 enabled 置 true（`runtime.py:130-141`），净效果 enabled 不变、线程换新。无竞态（join 保证旧线程已退出）。
  - 若当前未运行（无线程），`shutdown()` 直接 return（`163-164`），`start()` 正常拉起——也可安全调用，但前端只在 `running` 时才调。
- 新增路由 `POST /agent/restart`（`routes.py`，与 `/agent/start`、`/agent/stop` 同组，约 `822` 行附近）：
  ```
  @router.post("/agent/restart")
  async def restart_agent(request: Request):
      request.app.state.agent.restart()
      return {"status": "restarted"}
  ```
- 前端 client：`export const agentRestart = () => api.post("/agent/restart");`（`client.ts`，紧邻 `agentStart`/`agentStop` at `317-318`）。

**前端触发逻辑（`saveGoal()`，`Agent.vue:911-923`）：**
- 页面加载目标后，把当时的调度值存为基线：`scheduleBaseline = { time: params.daily_run_time ?? "", tradingOnly: params.daily_trading_days_only !== false }`（在 `loadGoal`/`syncAdvFromParams` 之处记录）。
- `saveGoal()` 在 `updateGoal(goalDraft)` 成功后：
  ```
  const changed = (现 daily_run_time/daily_trading_days_only) !== scheduleBaseline;
  if (changed && agent.value?.running) {
    await agentRestart();
    setMessage("目标已保存；调度已更新，Agent 已重启");
  } else {
    setMessage("目标已保存");
  }
  // 重启或保存后刷新 agent 状态，并更新 scheduleBaseline。
  ```
- 调度未变化（用户只改了别的字段）→ 不重启，避免无关编辑触发重启。

### 3.4 状态显示修正

- **`nextTickStr`（`Agent.vue:822-827`）**：当前 `next = last_tick_at + tick_interval_sec`，在每日模式下错误。改为：
  - 若 `advParams.daily_schedule_enabled && advParams.daily_run_time` 为真：显示下一个 `HH:MM`（今天该时刻还没到→今天，否则→明天），文案如 `17:30(明天)`。
  - 否则保持现有 `上次 + 间隔` 逻辑。
  - 仅依赖前端已有的 `goalDraft.params`/`advParams`，不需要后端在 `AgentStatus` 里新增字段。
- **「运行模式」卡（`modeLabel`，`Agent.vue:849-`）**：在每日模式下追加一行副标 `每日 17:30 · 仅交易日`（或 `每日 17:30 · 含周末`），让当前调度一眼可见。具体用副标签 `stat-sub` 呈现，不挤占主 `modeLabel`。

## 4. 涉及文件

| 文件 | 改动 |
|---|---|
| `quanti/agent/runtime.py` | 新增 `restart()` 方法（复用 `shutdown()`+`start()`） |
| `quanti/api/routes.py` | 新增 `POST /agent/restart` 路由 |
| `web/src/api/client.ts` | 新增 `agentRestart()` |
| `web/src/views/Agent.vue` | 模板加「运行计划」区块；`advParams` 加 3 键；`syncAdvFromParams`/`syncParamsFromAdv` 读写；`saveGoal` 自动重启；`nextTickStr` 修正；模式卡副标 |

## 5. 测试

**后端（pytest）**
- `tests/test_agent.py` 或新增用例：`restart()` 在运行中调用后——旧线程不再 alive、有新线程在跑（`status().running == True`）、`load_goal().enabled` 仍为 `True`、`AgentStatus.started_at` 已更新。
- `restart()` 在未运行时调用安全（不抛异常，等价于 `start()`）。
- 路由 `POST /agent/restart` 返回 200 且调用了 `agent.restart()`（用 TestClient + monkeypatch/spy）。
- 现有 `tests/test_agent_schedule.py` 不回归。

**前端**
- `syncParamsFromAdv`：开关开 → `params.daily_run_time`/`daily_trading_days_only` 正确写入；开关关 → 两键被删除；其它 `params` 键不受影响。
- `syncAdvFromParams`：从含/不含 `daily_run_time` 的 `params` 正确反推开关与时间。
- 若仓库前端无单测框架，则以手动验证替代：设 17:30→保存→`GET /api/goal` 确认 `params.daily_run_time=="17:30"`；运行中保存触发重启；关开关→`daily_run_time` 消失、回到间隔模式；「下次」显示正确。

## 6. 边界与注意

- **时间格式**：`<input type="time">` 产出 `"HH:MM"`（24h），与 `_parse_hhmm`（`runtime.py:51-63`）兼容。空值（未选）按未启用处理。
- **重启副作用**：`restart()` 会写一次 `agent_start` 决策日志（`start()` 内，`runtime.py:141`）；`shutdown()` 不写日志（`159-171`）。可接受。
- **enabled 一致性**：`restart()` 经 `shutdown()`（不动 enabled）+ `start()`（置 true）后，enabled 恒为 true，符合"运行中重启"语义；不会把用户手动停用的 agent 意外拉起（前端仅在 `running` 时调）。
- **正确的接口路径**：goal 在 `/api/goal`（非 `/api/agent/goal`）；agent 控制在 `/api/agent/{start,stop,restart}`（router 以 `prefix="/api"` 挂载，`app.py:108`）。
- **节假日精度**：仅交易日依赖 `is_trading_day`，无日历时退化为按周判定（`runtime.py:227-231`）；UI 文案需提示。
