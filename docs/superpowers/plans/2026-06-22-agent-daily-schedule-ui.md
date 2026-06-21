# Agent 每日定时运行 UI 控件 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Agent 面板暴露每日定时调度（`daily_run_time` + `daily_trading_days_only`），保存时自动重启运行中的 agent 使新调度即时生效。

**Architecture:** 后端 `AgentRuntime` 已能消费这两个 `goal.params` 键，只需新增一个安全的 `restart()`（复用 `shutdown()`+`start()`，靠线程 `join` 避开竞态）+ `POST /agent/restart` 路由。前端把三个新键接入现有 `advParams ↔ goal.params` 同步机制，加一个「运行计划」控件，并在调度变化且运行中时调用 restart。顺带修正每日模式下「下次」运行时刻的显示。

**Tech Stack:** Python 3 + FastAPI + pytest（后端）；Vue 3 `<script setup>` + TypeScript + Vite（前端，无单测框架，以 `vue-tsc` 类型检查 + 手动验证为门禁）。

## Global Constraints

- 后端测试用 venv 解释器运行：`.venv/Scripts/python.exe -m pytest ...`（Windows）。
- 后端 lint：`.venv/Scripts/python.exe -m ruff check <files>` 须通过。
- 前端类型检查门禁：在 `web/` 目录运行 `npm run type-check`（即 `vue-tsc --build`）须无错误。本仓库前端**无单元测试框架**，UI 行为以手动验证步骤确认。
- 参数键约定：`daily_run_time` 为字符串 `"HH:MM"`（24 小时制）；`daily_trading_days_only` 为布尔，缺省视为 `true`。开关关闭时必须从 `params` 中**删除** `daily_run_time`（和 `daily_trading_days_only`），以回到间隔模式。
- 接口路径：goal 在 `/api/goal`；agent 控制在 `/api/agent/{start,stop,restart,tick,status}`（router 以 `prefix="/api"` 挂载于 `quanti/api/app.py:108`）。
- `restart()` 不得改动持久化的 `goal.enabled`；前端仅在 `agent.running` 为真时调用它。
- 提交信息结尾附：`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。

---

## File Structure

| 文件 | 职责 | 改动 |
|---|---|---|
| `quanti/agent/runtime.py` | Agent 循环与生命周期 | 新增 `AgentRuntime.restart()` |
| `quanti/api/routes.py` | HTTP 路由 | 新增 `POST /agent/restart` |
| `tests/test_agent_schedule.py` | 调度相关后端测试 | 追加 `restart()` 用例 |
| `tests/test_api_restart.py` | restart 路由测试 | 新建 |
| `web/src/api/client.ts` | 前端 API 客户端 | 新增 `agentRestart()` |
| `web/src/views/Agent.vue` | Agent 页面 | `advParams` 三键、sync 函数、「运行计划」控件、`saveGoal` 自动重启、`nextTickStr`/模式副标 |

---

## Task 1: 后端 `AgentRuntime.restart()`

**Files:**
- Modify: `quanti/agent/runtime.py`（在 `shutdown()` 之后，约 `runtime.py:171` 后插入）
- Test: `tests/test_agent_schedule.py`（追加用例，复用文件内现有 `_runtime(tmp_path)` 夹具）

**Interfaces:**
- Consumes: 现有 `AgentRuntime.start()`（`runtime.py:127-141`，置 `goal.enabled=True` 并起线程）、`AgentRuntime.shutdown()`（`runtime.py:159-171`，设 `stop_flag`、`join` 线程、**不动** `goal.enabled`）、`AgentRuntime.status()`（返回 `AgentStatus`，其 `running = self._thread.is_alive()`）。
- Produces: `AgentRuntime.restart() -> None`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_agent_schedule.py` 末尾追加（该文件已 import `load_goal, save_goal, AgentRuntime` 及 `_runtime`）：

```python
def test_restart_spawns_new_thread_and_keeps_enabled(tmp_path):
    db, rt = _runtime(tmp_path)
    # Daily mode → start() 不会立即跑一轮 cycle（避免触网/耗时）。
    goal = load_goal(db)
    goal.params = {**(goal.params or {}), "daily_run_time": "23:59"}
    save_goal(db, goal)
    rt.start()
    try:
        assert rt.status().running is True
        first_thread = rt._thread
        rt.restart()
        assert rt.status().running is True
        assert rt._thread is not first_thread        # 换了新线程
        assert load_goal(db).enabled is True         # enabled 被保留
    finally:
        rt.stop()


def test_restart_when_not_running_behaves_like_start(tmp_path):
    db, rt = _runtime(tmp_path)
    goal = load_goal(db)
    goal.params = {**(goal.params or {}), "daily_run_time": "23:59"}
    save_goal(db, goal)
    rt.restart()  # 从未 start 过 → 等价于 start()
    try:
        assert rt.status().running is True
    finally:
        rt.stop()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_agent_schedule.py::test_restart_spawns_new_thread_and_keeps_enabled -v`
Expected: FAIL，`AttributeError: 'AgentRuntime' object has no attribute 'restart'`。

- [ ] **Step 3: 实现 `restart()`**

在 `quanti/agent/runtime.py` 的 `shutdown()` 方法之后（`runtime.py:171` 之后）插入：

```python
    def restart(self) -> None:
        """Reschedule cleanly: stop the running loop thread (joining it) then
        start a fresh one, WITHOUT flipping persisted goal.enabled. Used when
        the daily schedule changes so a new daily_run_time takes effect
        immediately. Safe to call when not running (acts like start())."""
        self.shutdown()
        self.start()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_agent_schedule.py -v`
Expected: PASS（含两个新用例与所有原有调度用例）。

- [ ] **Step 5: lint 并提交**

```bash
.venv/Scripts/python.exe -m ruff check quanti/agent/runtime.py tests/test_agent_schedule.py
git add quanti/agent/runtime.py tests/test_agent_schedule.py
git commit -m "$(cat <<'EOF'
feat(agent): AgentRuntime.restart() — 干净重启循环线程，保留 enabled

复用 shutdown()(join 线程)+ start()，用于调度变更后即时生效，无竞态。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: 后端 `POST /agent/restart` 路由

**Files:**
- Modify: `quanti/api/routes.py`（在 `/agent/stop` 路由后、`/agent/tick` 前插入，约 `routes.py:831` 与 `833` 之间）
- Test: `tests/test_api_restart.py`（新建）

**Interfaces:**
- Consumes: `request.app.state.agent.restart()`（Task 1）；`quanti.api.app.create_app(initial_cash, autostart_agent=False)`（测试构造 app，见 `tests/test_api_mine.py:13`）。
- Produces: HTTP `POST /api/agent/restart` → `{"status": "restarted"}`。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_api_restart.py`：

```python
"""Tests for the POST /agent/restart endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from quanti.api.app import create_app


def test_restart_endpoint_calls_agent_restart(monkeypatch):
    app = create_app(initial_cash=1_000_000, autostart_agent=False)
    calls = {"n": 0}
    # Spy 替换真实 restart，避免起线程/触网。
    monkeypatch.setattr(
        app.state.agent, "restart",
        lambda: calls.__setitem__("n", calls["n"] + 1))
    client = TestClient(app)

    r = client.post("/api/agent/restart")

    assert r.status_code == 200
    assert r.json() == {"status": "restarted"}
    assert calls["n"] == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api_restart.py -v`
Expected: FAIL，`404 Not Found`（路由未定义，断言 `status_code == 200` 失败）。

- [ ] **Step 3: 实现路由**

在 `quanti/api/routes.py` 的 `/agent/stop` 路由（`routes.py:828-831`）之后插入：

```python
@router.post("/agent/restart")
async def agent_restart(request: Request):
    """Stop the agent loop thread and start a fresh one so a changed daily
    schedule takes effect immediately. Persisted goal.enabled is untouched."""
    request.app.state.agent.restart()
    return {"status": "restarted"}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api_restart.py -v`
Expected: PASS。

- [ ] **Step 5: lint 并提交**

```bash
.venv/Scripts/python.exe -m ruff check quanti/api/routes.py tests/test_api_restart.py
git add quanti/api/routes.py tests/test_api_restart.py
git commit -m "$(cat <<'EOF'
feat(api): POST /agent/restart 路由

调 AgentRuntime.restart() 使调度变更即时生效。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 前端参数管道（client + advParams + sync 函数）

**Files:**
- Modify: `web/src/api/client.ts`（`agentStop` 后，约 `client.ts:318`）
- Modify: `web/src/views/Agent.vue`（import 块 `503-536`；`advParams` `555-569`；`syncAdvFromParams` `572-590`；`syncParamsFromAdv` `592-613`）

**Interfaces:**
- Consumes: 现有 `api`（axios，baseURL `/api`，`client.ts:3-5`）；现有 `advParams` reactive、`goalDraft.params`。
- Produces: `agentRestart()`（client）；`advParams.daily_schedule_enabled: boolean`、`advParams.daily_run_time: string`、`advParams.daily_trading_days_only: boolean`；`syncAdvFromParams`/`syncParamsFromAdv` 对这三键的读写。Task 4/5 依赖它们。

- [ ] **Step 1: 新增 client `agentRestart`**

在 `web/src/api/client.ts` 的 `agentStop`（`client.ts:318`）之后插入：

```ts
export const agentRestart = () => api.post<{ status: string }>("/agent/restart");
```

- [ ] **Step 2: 在 Agent.vue 导入 `agentRestart`**

在 import 块（`Agent.vue:504-536`）中，于 `agentStop,`（`506`）一行后加入：

```ts
  agentRestart,
```

- [ ] **Step 3: 给 `advParams` 加三个键**

在 `advParams` reactive 对象内，`llm_reflection: false,`（`Agent.vue:569`）之后、`});` 之前插入：

```ts
  // 每日定时调度（cron-lite）：开启后每天在 daily_run_time 跑一次，
  // 否则按 tick_interval_sec 间隔跑。daily_schedule_enabled 仅 UI 本地状态。
  daily_schedule_enabled: false,
  daily_run_time: "09:35",
  daily_trading_days_only: true,
```

- [ ] **Step 4: `syncAdvFromParams` 反推三键**

在 `syncAdvFromParams()`（`Agent.vue:572-590`）的 `advParams.llm_reflection = !!p.llm_reflection;`（`589`）之后、函数闭合 `}` 之前插入：

```ts
  const drt = typeof p.daily_run_time === "string" ? p.daily_run_time : "";
  advParams.daily_schedule_enabled = drt !== "";
  if (drt) advParams.daily_run_time = drt;
  advParams.daily_trading_days_only = p.daily_trading_days_only !== false;
```

- [ ] **Step 5: `syncParamsFromAdv` 写出三键**

将 `syncParamsFromAdv()` 函数体（`Agent.vue:592-613`）整体替换为：

```ts
function syncParamsFromAdv() {
  // Preserve any unknown keys the user may have set via API/MCP.
  const existing = (goalDraft.params || {}) as Record<string, unknown>;
  const params: Record<string, unknown> = {
    ...existing,
    agent_mode: advParams.agent_mode === "llm" ? "llm" : "",
    ensemble_enabled:
      advParams.ensemble_enabled ||
      advParams.agent_mode === "ensemble" ||
      advParams.agent_mode === "llm",
    industry_neutral: advParams.industry_neutral,
    liquidity_filter: advParams.liquidity_filter,
    wf_enabled: advParams.wf_enabled,
    llm_provider: advParams.llm_provider,
    sentiment_enabled: advParams.sentiment_enabled,
    sentiment_blend: advParams.sentiment_blend,
    llm_debate: advParams.llm_debate,
    llm_debate_rounds: advParams.llm_debate_rounds,
    llm_risk_debate: advParams.llm_risk_debate,
    llm_reflection: advParams.llm_reflection,
  };
  if (advParams.daily_schedule_enabled && advParams.daily_run_time) {
    params.daily_run_time = advParams.daily_run_time;
    params.daily_trading_days_only = advParams.daily_trading_days_only;
  } else {
    delete params.daily_run_time;
    delete params.daily_trading_days_only;
  }
  goalDraft.params = params;
}
```

- [ ] **Step 6: 类型检查通过**

Run（在 `web/` 目录）：`npm run type-check`
Expected: 无类型错误（exit 0）。

- [ ] **Step 7: 提交**

```bash
git add web/src/api/client.ts web/src/views/Agent.vue
git commit -m "$(cat <<'EOF'
feat(web): 调度参数管道 — agentRestart + advParams 三键 + sync 读写

daily_schedule_enabled/daily_run_time/daily_trading_days_only 接入
现有 advParams↔goal.params 同步；关闭开关时从 params 删除调度键。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: 「运行计划」控件 + 保存时自动重启

**Files:**
- Modify: `web/src/views/Agent.vue`（模板 `目标设定` 卡片 `goal-grid` 后约 `97`/`98` 之间；脚本 `advParams` 后加 baseline；`loadAll` `884-904`；`saveGoal` `911-923`；`<style scoped>` 末尾）

**Interfaces:**
- Consumes: Task 3 的 `advParams` 三键与 `syncParamsFromAdv()`；`agentRestart()`（Task 3）；现有 `agent`（ref，`agent.value?.running`）、`updateGoal`、`loadAll`、`syncAdvFromParams`、`setMessage`、`saving`。
- Produces: 模板「运行计划」区块（绑定 `advParams` 三键）；`scheduleBaseline` 快照 + `captureScheduleBaseline()`；`saveGoal()` 在调度变化且运行中时调用 `agentRestart()`。

- [ ] **Step 1: 加模板「运行计划」区块**

在 `web/src/views/Agent.vue` 的 `目标设定` 卡片内，`goal-grid` 的闭合 `</div>`（`Agent.vue:97`）之后、`<div class="actions">`（`98`）之前插入：

```html
      <div class="schedule-block">
        <label class="schedule-toggle">
          <input type="checkbox" v-model="advParams.daily_schedule_enabled" />
          <span>每日定时运行</span>
        </label>
        <div v-if="advParams.daily_schedule_enabled" class="schedule-fields">
          <label class="schedule-time">
            <span>运行时间</span>
            <input type="time" v-model="advParams.daily_run_time" />
          </label>
          <label class="schedule-toggle">
            <input type="checkbox" v-model="advParams.daily_trading_days_only" />
            <span>仅交易日运行（周末/节假日自动跳过）</span>
          </label>
          <p class="muted">
            节假日精度需先运行 <code>quanti sync --calendar</code>；否则按周一~周五判定。
          </p>
        </div>
      </div>
```

- [ ] **Step 2: 加 `scheduleBaseline` 快照与捕获函数**

在 `syncParamsFromAdv()` 函数（Task 3 改写后的版本）之后插入：

```ts
// 调度基线快照：用于 saveGoal 判断 daily_run_time/仅交易日是否变化，
// 只有变化且 agent 在运行时才触发重启。
let scheduleBaseline = { time: "", tradingOnly: true };
function captureScheduleBaseline() {
  const p = (goalDraft.params || {}) as Record<string, unknown>;
  scheduleBaseline = {
    time: typeof p.daily_run_time === "string" ? p.daily_run_time : "",
    tradingOnly: p.daily_trading_days_only !== false,
  };
}
```

- [ ] **Step 3: `loadAll` 中捕获基线**

在 `loadAll()`（`Agent.vue:884-904`）里，`syncAdvFromParams();`（`896`）之后插入一行：

```ts
  captureScheduleBaseline();
```

- [ ] **Step 4: 改写 `saveGoal` 加自动重启**

将 `saveGoal()`（`Agent.vue:911-923`）整体替换为：

```ts
async function saveGoal() {
  saving.value = true;
  try {
    // Push the mode/upgrade switches into goalDraft.params right before sending.
    syncParamsFromAdv();
    const p = goalDraft.params as Record<string, unknown>;
    const newTime = typeof p.daily_run_time === "string" ? p.daily_run_time : "";
    const newTradingOnly = p.daily_trading_days_only !== false;
    const scheduleChanged =
      newTime !== scheduleBaseline.time ||
      newTradingOnly !== scheduleBaseline.tradingOnly;
    await updateGoal(goalDraft);
    if (scheduleChanged && agent.value?.running) {
      await agentRestart();
      setMessage("目标已保存；调度已更新，Agent 已重启");
    } else {
      setMessage("目标已保存");
    }
    await loadAll(); // 刷新状态并通过 captureScheduleBaseline 重置基线
  } catch (e: any) {
    setMessage("保存失败: " + (e?.message ?? e), true);
  } finally {
    saving.value = false;
  }
}
```

- [ ] **Step 5: 加最小作用域样式**

在 `Agent.vue` 的 `<style scoped>` 块末尾（`</style>` 之前）追加：

```css
.schedule-block {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border, #e5e7eb);
}
.schedule-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
}
.schedule-fields {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.schedule-time {
  display: flex;
  align-items: center;
  gap: 8px;
}
.schedule-time input {
  width: 140px;
}
```

- [ ] **Step 6: 类型检查通过**

Run（在 `web/` 目录）：`npm run type-check`
Expected: 无类型错误（exit 0）。

- [ ] **Step 7: 手动验证**

启动后端 + 前端（参考本机 dev 流程）。在 Agent 页面：
1. 「目标设定」底部出现「运行计划」开关；勾选后出现时间选择器与「仅交易日」勾选框。
2. 设时间 `17:30` → 点「保存目标」→ 提示「目标已保存」(若 agent 未运行) 或「…Agent 已重启」(运行中)。
3. 浏览器开发者工具确认 `POST /api/goal` 请求体 `params.daily_run_time == "17:30"`；运行中时其后有一条 `POST /api/agent/restart`。
4. 取消勾选开关 → 保存 → 确认 `POST /api/goal` 请求体中**已无** `daily_run_time` 键。
5. 只改「目标年化收益」不动调度 → 保存 → 确认**没有** `POST /api/agent/restart`（调度未变不重启）。

- [ ] **Step 8: 提交**

```bash
git add web/src/views/Agent.vue
git commit -m "$(cat <<'EOF'
feat(web): 「运行计划」控件 + 保存时按需自动重启 Agent

目标设定加每日定时开关/时间/仅交易日；调度变化且运行中时调
/agent/restart 即时生效，未变化则不重启。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: 状态显示修正（下次运行时刻 + 模式副标）

**Files:**
- Modify: `web/src/views/Agent.vue`（`nextTickStr` `822-827`；新增 `scheduleSubStr` computed；模板「运行模式」卡 `41-46`）

**Interfaces:**
- Consumes: Task 3 的 `advParams.daily_schedule_enabled`/`daily_run_time`/`daily_trading_days_only`；现有 `agent`（ref）、`nowTs`（ref，每 20s 刷新，`Agent.vue:1011`）。
- Produces: 修正后的 `nextTickStr`（每日模式显示下一个 HH:MM）；`scheduleSubStr` computed；模式卡副标。

- [ ] **Step 1: 改写 `nextTickStr`**

将 `nextTickStr`（`Agent.vue:822-827`）整体替换为：

```ts
const nextTickStr = computed(() => {
  const a = agent.value;
  if (!a || !a.running) return "—";
  // 每日定时模式：显示下一个 daily_run_time 时刻。
  if (advParams.daily_schedule_enabled && advParams.daily_run_time) {
    const [h, m] = advParams.daily_run_time.split(":").map(Number);
    if (!Number.isNaN(h) && !Number.isNaN(m)) {
      const now = new Date(nowTs.value);
      const next = new Date(now);
      next.setHours(h, m, 0, 0);
      if (next <= now) next.setDate(next.getDate() + 1);
      const sameDay = next.getDate() === now.getDate();
      const hh = String(next.getHours()).padStart(2, "0");
      const mm = String(next.getMinutes()).padStart(2, "0");
      return `${hh}:${mm}${sameDay ? "" : "(明天)"}`;
    }
  }
  // 间隔模式（原逻辑不变）。
  if (!a.last_tick_at || !a.tick_interval_sec) return "—";
  const next = new Date(Date.parse(a.last_tick_at) + a.tick_interval_sec * 1000);
  return `${String(next.getHours()).padStart(2, "0")}:${String(next.getMinutes()).padStart(2, "0")}`;
});
```

- [ ] **Step 2: 新增 `scheduleSubStr` computed**

在 `nextTickStr` 之后插入：

```ts
const scheduleSubStr = computed(() => {
  if (!advParams.daily_schedule_enabled || !advParams.daily_run_time) return "";
  const days = advParams.daily_trading_days_only ? "仅交易日" : "含周末";
  return `每日 ${advParams.daily_run_time} · ${days}`;
});
```

- [ ] **Step 3: 模式卡加副标**

在「运行模式」`stat-card`（`Agent.vue:41-46`）内，`<span class="stat-value stat-value-sm">{{ modeLabel }}</span>`（`44`）之后插入：

```html
          <span v-if="scheduleSubStr" class="stat-sub">{{ scheduleSubStr }}</span>
```

- [ ] **Step 4: 类型检查通过**

Run（在 `web/` 目录）：`npm run type-check`
Expected: 无类型错误（exit 0）。

- [ ] **Step 5: 手动验证**

Agent 运行中：
1. 开启每日定时 `17:30` 并保存。
2. 「运行时间/运行模式」卡的「下次」显示 `17:30`（若今天已过 17:30 则显示 `17:30(明天)`），不再是「上次+4h」。
3. 「运行模式」卡出现副标 `每日 17:30 · 仅交易日`；取消「仅交易日」后副标变 `含周末`。
4. 关闭每日定时开关并保存 → 「下次」回到间隔模式显示，副标消失。

- [ ] **Step 6: 提交**

```bash
git add web/src/views/Agent.vue
git commit -m "$(cat <<'EOF'
feat(web): 每日模式下「下次」显示正确时刻 + 模式卡调度副标

nextTickStr 在每日定时模式显示下一个 HH:MM；运行模式卡加
「每日 17:30 · 仅交易日」副标。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## 收尾验证（全部任务完成后）

- [ ] 后端全量回归：`.venv/Scripts/python.exe -m pytest tests/test_agent_schedule.py tests/test_api_restart.py -v` 全绿。
- [ ] 前端：`web/` 下 `npm run type-check` 与 `npm run build` 通过。
- [ ] 端到端手动串联：开启每日定时→保存（运行中触发重启）→`GET /api/goal` 确认 `params.daily_run_time`/`daily_trading_days_only`→「下次」与模式副标正确→关闭开关保存→`daily_run_time` 消失、回到间隔模式。

---

## Self-Review

**1. Spec coverage**
- 控件范围（开关+时间+仅交易日）→ Task 4 Step 1。
- 数据流（advParams 三键 + sync 读写，关闭即删除键）→ Task 3 Step 3-5。
- 自动重启（后端 `restart()` + 路由 + 前端仅调度变化且运行中触发）→ Task 1 / Task 2 / Task 4 Step 2-4。
- 「下次」显示修正 + 模式副标 → Task 5。
- 测试（restart 方法 + 路由 + 前端 sync 往返/手动）→ Task 1 Step 1、Task 2 Step 1、Task 3 Step 6、Task 4/5 手动验证。
- 接口路径 `/api/goal` 与 `/api/agent/restart`、enabled 不被改动 → Global Constraints + Task 1/2 实现与断言。
- 全部 spec 小节均有对应任务，无遗漏。

**2. Placeholder scan**：无 TBD/TODO；每个改代码的步骤均含完整代码块与确切路径/行号；测试步骤含完整测试代码与期望输出。

**3. Type consistency**：跨任务名称一致 —— `restart()`（Task 1 定义，Task 2/4 调用）、`agentRestart()`（Task 3 定义，Task 4 调用）、`advParams.daily_schedule_enabled`/`daily_run_time`/`daily_trading_days_only`（Task 3 定义，Task 4/5 使用）、`scheduleBaseline`/`captureScheduleBaseline()`（Task 4 内自洽）、`scheduleSubStr`（Task 5 定义并在模板使用）。`{"status": "restarted"}` 返回体在 Task 2 实现与测试断言一致。
