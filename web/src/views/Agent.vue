<template>
  <div class="agent-page">
    <div class="page-header">
      <h1>AI Agent</h1>
      <p class="page-desc">设定目标，剩下交给 Agent 自动维护</p>
    </div>

    <!-- Top stats -->
    <div class="stats-row">
      <div class="stat-card">
        <div class="stat-info">
          <span class="stat-label">组合净值</span>
          <span class="stat-value">¥{{ formatMoney(portfolio?.total_value ?? 0) }}</span>
        </div>
      </div>
      <div class="stat-card" :class="pnlClass">
        <div class="stat-info">
          <span class="stat-label">累计收益</span>
          <span class="stat-value">{{ formatPct(portfolio?.pnl_pct ?? 0) }}</span>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-info">
          <span class="stat-label">距目标</span>
          <span class="stat-value">{{ targetGapStr }}</span>
        </div>
      </div>
      <div class="stat-card" :class="agentStatusClass">
        <div class="stat-info">
          <span class="stat-label">Agent 状态</span>
          <span class="stat-value stat-value-sm">{{ agentStatusStr }}</span>
        </div>
      </div>
    </div>

    <!-- Goal editor -->
    <div class="card">
      <div class="card-header"><h2>目标设定</h2></div>
      <div class="goal-grid">
        <label>
          <span>目标年化收益</span>
          <input type="number" step="0.01" v-model.number="goalDraft.target_annual_return" />
        </label>
        <label>
          <span>可接受最大回撤（负数）</span>
          <input type="number" step="0.01" v-model.number="goalDraft.max_drawdown" />
        </label>
        <label>
          <span>风险偏好</span>
          <select v-model="goalDraft.risk_tolerance">
            <option value="low">保守 (low)</option>
            <option value="medium">平衡 (medium)</option>
            <option value="high">激进 (high)</option>
          </select>
        </label>
        <label>
          <span>股票池（留空 = 全部）</span>
          <input type="text" v-model="goalDraft.universe_pool" placeholder="例如 my_pool" />
        </label>
        <label>
          <span>选股器（可选）</span>
          <select v-model="goalDraft.screener_name">
            <option value="">不使用</option>
            <option v-for="s in screeners" :key="s.name" :value="s.name">{{ s.name }}</option>
          </select>
        </label>
        <label>
          <span>策略（留空 = Agent 自动挑选）</span>
          <select v-model="goalDraft.strategy_name">
            <option value="">由 Agent 自动挑选</option>
            <option v-for="s in strategies" :key="s.name" :value="s.name">{{ s.name }}</option>
          </select>
        </label>
      </div>
      <div class="actions">
        <button class="btn-primary" :disabled="saving" @click="saveGoal">
          {{ saving ? "保存中..." : "保存目标" }}
        </button>
        <button class="btn-secondary" :disabled="ticking" @click="forceTick">
          {{ ticking ? "执行中..." : "立即跑一轮" }}
        </button>
        <button v-if="!agent?.running" class="btn-success" :disabled="busy" @click="startAgent">
          启动 Agent
        </button>
        <button v-else class="btn-danger" :disabled="busy" @click="stopAgent">停止 Agent</button>
        <button class="btn-secondary" :disabled="busy" @click="reset">重置组合</button>
      </div>
      <div v-if="message" class="sync-msg" :class="messageError ? 'error' : 'success'">
        {{ message }}
      </div>
    </div>

    <!-- Portfolio + positions -->
    <div class="card">
      <div class="card-header">
        <h2>当前持仓</h2>
        <div class="muted">
          现金 ¥{{ formatMoney(portfolio?.cash ?? 0) }} / 市值 ¥{{
            formatMoney(portfolio?.market_value ?? 0)
          }}
        </div>
      </div>
      <table class="data-table" v-if="portfolio && portfolio.positions.length > 0">
        <thead>
          <tr>
            <th>代码</th>
            <th>名称</th>
            <th>数量</th>
            <th>成本</th>
            <th>现价</th>
            <th>市值</th>
            <th>盈亏</th>
            <th>盈亏 %</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in portfolio.positions" :key="p.code">
            <td>{{ p.code }}</td>
            <td>{{ p.name }}</td>
            <td>{{ p.quantity }}</td>
            <td>{{ p.avg_cost.toFixed(2) }}</td>
            <td>{{ p.current_price.toFixed(2) }}</td>
            <td>{{ formatMoney(p.market_value) }}</td>
            <td :class="p.pnl >= 0 ? 'up' : 'down'">{{ formatMoney(p.pnl) }}</td>
            <td :class="p.pnl_pct >= 0 ? 'up' : 'down'">{{ formatPct(p.pnl_pct) }}</td>
            <td>
              <button class="btn-link" @click="sellOne(p.code)">卖出</button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">暂无持仓</div>
    </div>

    <!-- Manual order -->
    <div class="card">
      <div class="card-header"><h2>手动下单</h2></div>
      <div class="manual-row">
        <input v-model="manualCode" placeholder="股票代码 如 600519" />
        <select v-model="manualDirection">
          <option value="buy">买入</option>
          <option value="sell">卖出</option>
        </select>
        <input type="number" step="0.05" min="0.05" max="1" v-model.number="manualStrength" />
        <button class="btn-primary" :disabled="busy" @click="placeManual">下单</button>
      </div>
      <div class="muted">strength（0~1）：仅买入有效，作为现金的目标占比</div>
    </div>

    <!-- Last evaluation -->
    <div class="card" v-if="agent && agent.last_evaluations.length > 0">
      <div class="card-header">
        <h2>最近策略评估</h2>
        <div class="muted">选定：<b>{{ agent.last_strategy }}</b></div>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>策略</th>
            <th>年化</th>
            <th>最大回撤</th>
            <th>夏普</th>
            <th>成交</th>
            <th>得分</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="e in agent.last_evaluations" :key="e.strategy_name">
            <td><b v-if="e.strategy_name === agent.last_strategy">{{ e.strategy_name }}</b><span v-else>{{ e.strategy_name }}</span></td>
            <td :class="e.annual_return >= 0 ? 'up' : 'down'">{{ formatPct(e.annual_return) }}</td>
            <td class="down">{{ formatPct(e.max_drawdown) }}</td>
            <td>{{ e.sharpe.toFixed(2) }}</td>
            <td>{{ e.total_trades }}</td>
            <td>{{ e.score.toFixed(3) }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Decision log -->
    <div class="card">
      <div class="card-header">
        <h2>决策日志</h2>
        <button class="btn-link" @click="loadDecisions">刷新</button>
      </div>
      <div class="decision-list">
        <div class="decision" v-for="d in decisions" :key="d.id" :class="kindClass(d.kind)">
          <div class="decision-meta">
            <span class="kind">{{ d.kind }}</span>
            <span class="ts">{{ formatTs(d.ts) }}</span>
          </div>
          <div class="decision-body">{{ d.summary }}</div>
          <!-- LLM cycle: surface Claude's reasoning so users can see WHY it chose what it did. -->
          <div
            v-if="d.kind === 'llm_cycle' && (d.details as any)?.reasoning"
            class="decision-reasoning"
          >
            <span class="reasoning-label">理由</span>
            {{ (d.details as any).reasoning }}
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";
import {
  agentStart,
  agentStop,
  agentTick,
  fetchAgentDecisions,
  fetchAgentStatus,
  fetchGoal,
  fetchPortfolio,
  fetchScreeners,
  fetchStrategies,
  manualOrder,
  resetPortfolio,
  updateGoal,
  type AgentStatus,
  type DecisionRecord,
  type Goal,
  type Portfolio,
  type ScreenerInfo,
  type StrategyInfo,
} from "../api/client";

const goalDraft = reactive<Goal>({
  target_annual_return: 0.2,
  max_drawdown: -0.2,
  risk_tolerance: "medium",
  universe_pool: "",
  screener_name: "",
  strategy_name: "",
  params: {},
  rebalance_freq: "daily",
  enabled: false,
});

const portfolio = ref<Portfolio | null>(null);
const agent = ref<AgentStatus | null>(null);
const decisions = ref<DecisionRecord[]>([]);
const strategies = ref<StrategyInfo[]>([]);
const screeners = ref<ScreenerInfo[]>([]);

const saving = ref(false);
const ticking = ref(false);
const busy = ref(false);
const message = ref("");
const messageError = ref(false);

const manualCode = ref("");
const manualDirection = ref<"buy" | "sell">("buy");
const manualStrength = ref(0.2);

let timer: number | null = null;

function setMessage(msg: string, err = false) {
  message.value = msg;
  messageError.value = err;
  setTimeout(() => (message.value = ""), 6000);
}

function formatMoney(n: number) {
  return Number(n || 0).toLocaleString("zh-CN", { maximumFractionDigits: 0 });
}
function formatPct(n: number) {
  if (!isFinite(n)) return "-";
  return (n * 100).toFixed(2) + "%";
}
function formatTs(ts: string) {
  return ts.replace("T", " ").slice(0, 19);
}

const pnlClass = computed(() => {
  if (!portfolio.value) return "";
  return portfolio.value.pnl_pct >= 0 ? "up" : "down";
});

const targetGapStr = computed(() => {
  if (!portfolio.value || !goalDraft.target_annual_return) return "-";
  const gap = portfolio.value.pnl_pct - goalDraft.target_annual_return;
  return `${(gap * 100).toFixed(2)}%`;
});

const agentStatusStr = computed(() => {
  if (!agent.value) return "未启动";
  if (agent.value.running) return "运行中";
  if (agent.value.enabled) return "启用但未运行";
  return "已停止";
});

const agentStatusClass = computed(() => {
  if (!agent.value) return "";
  return agent.value.running ? "up" : "muted-card";
});

function kindClass(kind: string) {
  if (kind === "trade") return "kind-trade";
  if (kind === "risk_reject") return "kind-warn";
  if (kind === "cycle") return "kind-info";
  if (kind === "agent_start" || kind === "agent_stop") return "kind-meta";
  if (kind === "strategy_pick" || kind === "strategy_ensemble") return "kind-info";
  if (kind === "llm_cycle") return "kind-llm";
  if (kind === "agent_error") return "kind-error";
  return "";
}

async function loadAll() {
  const [g, p, a, d, str, scr] = await Promise.all([
    fetchGoal(),
    fetchPortfolio(),
    fetchAgentStatus(),
    fetchAgentDecisions(50),
    fetchStrategies(),
    fetchScreeners(),
  ]);
  Object.assign(goalDraft, g.data);
  portfolio.value = p.data;
  agent.value = a.data;
  decisions.value = d.data;
  strategies.value = str.data;
  screeners.value = scr.data;
}

async function loadDecisions() {
  const d = await fetchAgentDecisions(50);
  decisions.value = d.data;
}

async function saveGoal() {
  saving.value = true;
  try {
    await updateGoal(goalDraft);
    setMessage("目标已保存");
  } catch (e: any) {
    setMessage("保存失败: " + (e?.message ?? e), true);
  } finally {
    saving.value = false;
  }
}

async function startAgent() {
  busy.value = true;
  try {
    await agentStart();
    setMessage("Agent 已启动");
    await loadAll();
  } finally {
    busy.value = false;
  }
}

async function stopAgent() {
  busy.value = true;
  try {
    await agentStop();
    setMessage("Agent 已停止");
    await loadAll();
  } finally {
    busy.value = false;
  }
}

async function forceTick() {
  ticking.value = true;
  try {
    const r = await agentTick();
    setMessage("执行完成: " + JSON.stringify(r.data).slice(0, 200));
    await loadAll();
  } catch (e: any) {
    setMessage("执行失败: " + (e?.message ?? e), true);
  } finally {
    ticking.value = false;
  }
}

async function reset() {
  if (!confirm("确认重置组合？所有持仓与交易将被清空。")) return;
  busy.value = true;
  try {
    const cash = Number(prompt("新的初始资金?", "1000000") || "1000000");
    await resetPortfolio(cash);
    await loadAll();
    setMessage("组合已重置");
  } finally {
    busy.value = false;
  }
}

async function placeManual() {
  if (!manualCode.value.trim()) return;
  busy.value = true;
  try {
    const r = await manualOrder({
      code: manualCode.value.trim(),
      direction: manualDirection.value,
      strength: manualStrength.value,
      reason: "manual via UI",
    });
    setMessage(r.data.filled ? "下单成交" : "下单被拒（可能是风控/无数据/资金不足）",
      !r.data.filled);
    portfolio.value = r.data.snapshot;
  } catch (e: any) {
    setMessage("下单失败: " + (e?.message ?? e), true);
  } finally {
    busy.value = false;
  }
}

async function sellOne(code: string) {
  if (!confirm(`确认全部卖出 ${code} ?`)) return;
  busy.value = true;
  try {
    const r = await manualOrder({ code, direction: "sell", reason: "manual sell" });
    setMessage(r.data.filled ? "已卖出" : "卖出失败", !r.data.filled);
    portfolio.value = r.data.snapshot;
  } finally {
    busy.value = false;
  }
}

onMounted(() => {
  loadAll();
  timer = window.setInterval(loadAll, 15000);
});

onUnmounted(() => {
  if (timer !== null) window.clearInterval(timer);
});
</script>

<style scoped>
.agent-page {
  display: flex;
  flex-direction: column;
  gap: 24px;
}
.page-header h1 {
  margin: 0;
  font-size: 28px;
  font-weight: 600;
  letter-spacing: -0.5px;
}
.page-desc {
  color: var(--color-text-secondary);
  margin-top: 6px;
}
.stats-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 16px;
}
.stat-card {
  background: var(--color-surface, #fff);
  border: 0.5px solid rgba(0, 0, 0, 0.08);
  border-radius: 12px;
  padding: 16px 18px;
}
.stat-label {
  font-size: 13px;
  color: var(--color-text-secondary);
}
.stat-value {
  display: block;
  font-size: 22px;
  font-weight: 600;
  margin-top: 4px;
}
.stat-value-sm {
  font-size: 16px;
}
.up {
  color: #c0392b;
}
.down {
  color: #16a34a;
}
.muted-card {
  opacity: 0.7;
}
.card {
  background: var(--color-surface, #fff);
  border: 0.5px solid rgba(0, 0, 0, 0.08);
  border-radius: 12px;
  padding: 20px;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
}
.card-header h2 {
  margin: 0;
  font-size: 18px;
}
.muted {
  color: var(--color-text-secondary);
  font-size: 13px;
}
.goal-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 14px;
}
.goal-grid label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
}
.goal-grid input,
.goal-grid select,
.manual-row input,
.manual-row select {
  padding: 8px 10px;
  border: 1px solid rgba(0, 0, 0, 0.12);
  border-radius: 8px;
  font-size: 14px;
}
.actions {
  display: flex;
  gap: 10px;
  margin-top: 16px;
  flex-wrap: wrap;
}
.btn-primary,
.btn-secondary,
.btn-success,
.btn-danger,
.btn-link {
  padding: 8px 16px;
  border-radius: 8px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  border: 0;
}
.btn-primary {
  background: #0071e3;
  color: white;
}
.btn-secondary {
  background: rgba(0, 0, 0, 0.06);
  color: #333;
}
.btn-success {
  background: #16a34a;
  color: white;
}
.btn-danger {
  background: #c0392b;
  color: white;
}
.btn-link {
  background: transparent;
  color: #0071e3;
  padding: 4px 8px;
}
.btn-primary:disabled,
.btn-success:disabled,
.btn-danger:disabled,
.btn-secondary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.sync-msg {
  margin-top: 12px;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 13px;
}
.sync-msg.success {
  background: rgba(22, 163, 74, 0.1);
  color: #16a34a;
}
.sync-msg.error {
  background: rgba(192, 57, 43, 0.1);
  color: #c0392b;
}
.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.data-table th,
.data-table td {
  text-align: left;
  padding: 8px 10px;
  border-bottom: 0.5px solid rgba(0, 0, 0, 0.06);
}
.data-table th {
  background: rgba(0, 0, 0, 0.02);
  font-weight: 500;
}
.empty {
  color: var(--color-text-secondary);
  padding: 16px;
  text-align: center;
}
.manual-row {
  display: grid;
  grid-template-columns: 1fr 100px 90px 110px;
  gap: 10px;
  align-items: center;
}
.decision-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 420px;
  overflow-y: auto;
}
.decision {
  border: 0.5px solid rgba(0, 0, 0, 0.08);
  border-radius: 8px;
  padding: 8px 12px;
  font-size: 13px;
}
.decision-meta {
  display: flex;
  justify-content: space-between;
  color: var(--color-text-secondary);
  font-size: 12px;
}
.decision-meta .kind {
  font-weight: 600;
}
.decision.kind-trade {
  background: rgba(0, 113, 227, 0.06);
}
.decision.kind-warn {
  background: rgba(245, 158, 11, 0.08);
}
.decision.kind-info {
  background: rgba(22, 163, 74, 0.06);
}
.decision.kind-error {
  background: rgba(192, 57, 43, 0.1);
}
.decision.kind-meta {
  background: rgba(0, 0, 0, 0.03);
}
.decision.kind-llm {
  background: rgba(139, 92, 246, 0.07);  /* purple tint distinguishes LLM-driven cycles */
  border-left: 3px solid rgba(139, 92, 246, 0.6);
  padding-left: 10px;
}
.decision-reasoning {
  margin-top: 6px;
  padding: 8px 10px;
  background: rgba(139, 92, 246, 0.04);
  border-radius: 6px;
  font-size: 13px;
  color: #4c1d95;
  line-height: 1.5;
}
.reasoning-label {
  font-weight: 600;
  margin-right: 6px;
  color: #6d28d9;
}
</style>
