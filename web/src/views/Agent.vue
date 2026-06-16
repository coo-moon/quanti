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
      <div class="stat-card" :class="modeClass">
        <div class="stat-info">
          <span class="stat-label">运行模式</span>
          <span class="stat-value stat-value-sm">{{ modeLabel }}</span>
        </div>
      </div>
      <div class="stat-card" :class="pendingCardClass" v-if="(agent?.pending_orders ?? 0) > 0">
        <div class="stat-info">
          <span class="stat-label">待成交订单</span>
          <span class="stat-value">{{ agent?.pending_orders ?? 0 }}</span>
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
            <option v-for="s in screeners" :key="s.name" :value="s.name" :title="s.description">
              {{ s.name_zh || s.name }}
            </option>
          </select>
        </label>
        <label>
          <span>策略（留空 = Agent 自动挑选）</span>
          <select v-model="goalDraft.strategy_name">
            <option value="">由 Agent 自动挑选</option>
            <option v-for="s in strategies" :key="s.name" :value="s.name" :title="s.description || ''">
              {{ s.name_zh || s.name }}
            </option>
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

    <!-- Agent mode + upgrades (P1-P4) -->
    <div class="card">
      <div class="card-header">
        <h2>Agent 模式</h2>
        <span class="muted">钉死策略时模式无效 — 钉选优先</span>
      </div>
      <div class="mode-presets">
        <button
          type="button"
          class="mode-pill"
          :class="{ 'mode-active mode-rule-active': advParams.agent_mode === 'rule' }"
          @click="applyPreset('rule')"
        >
          <div class="mode-title">经典 (rule)</div>
          <div class="mode-desc">单策略 · Selector 24h cache · 不开因子/LLM</div>
        </button>
        <button
          type="button"
          class="mode-pill"
          :class="{ 'mode-active mode-ensemble-active': advParams.agent_mode === 'ensemble' }"
          @click="applyPreset('ensemble')"
        >
          <div class="mode-title">集成 (ensemble)</div>
          <div class="mode-desc">Top-K 策略加权 + 截面因子 + 流动性清洗 + 行业中性</div>
        </button>
        <button
          type="button"
          class="mode-pill"
          :class="{ 'mode-active mode-llm-active': advParams.agent_mode === 'llm' }"
          @click="applyPreset('llm')"
        >
          <div class="mode-title">LLM 决策</div>
          <div class="mode-desc">ensemble 候选 → LLM 拍板 · 默认 DeepSeek，可切 Anthropic</div>
        </button>
      </div>
      <details class="advanced">
        <summary>高级开关(单独细调,会覆盖预设)</summary>
        <div class="adv-grid">
          <label class="adv-check">
            <input type="checkbox" v-model="advParams.ensemble_enabled" />
            <span>ensemble_enabled</span>
            <em>Top-K 策略融合;关闭则走单策略</em>
          </label>
          <label class="adv-check">
            <input type="checkbox" v-model="advParams.industry_neutral" />
            <span>industry_neutral</span>
            <em>每行业最多 2 个候选,避免行业过度集中</em>
          </label>
          <label class="adv-check">
            <input type="checkbox" v-model="advParams.liquidity_filter" />
            <span>liquidity_filter</span>
            <em>排除停牌/新股/低流动性,从全市场筛到 ~1000 只</em>
          </label>
          <label class="adv-check">
            <input type="checkbox" v-model="advParams.wf_enabled" />
            <span>wf_enabled</span>
            <em>walk-forward 滚动验证,杜绝单窗 IS 过拟合</em>
          </label>
        </div>
        <div class="adv-note">
          预设按钮会重置这四项;手动改完后下方保存目标按钮才会落库。LLM 模式的供应商与
          多智能体增强开关见下方「LLM 增强层」。
        </div>
      </details>

      <details class="advanced">
        <summary>LLM 增强层(情绪 / 多空辩论 / 风控三角 / 反思)</summary>
        <div class="adv-llm-provider">
          <label>
            <span>供应商 llm_provider</span>
            <select v-model="advParams.llm_provider">
              <option value="anthropic">Anthropic claude(需 ANTHROPIC_API_KEY + pip install .[llm]）</option>
              <option value="deepseek">DeepSeek deepseek-v4-pro(需 DEEPSEEK_API_KEY，无需额外安装）</option>
            </select>
          </label>
        </div>
        <div class="adv-grid">
          <label class="adv-check">
            <input type="checkbox" v-model="advParams.sentiment_enabled" />
            <span>sentiment_enabled</span>
            <em>① 新闻情绪 overlay(ensemble 与 LLM 模式都生效)</em>
          </label>
          <label class="adv-num">
            <span>sentiment_blend</span>
            <input type="number" step="0.05" min="0" max="1"
                   v-model.number="advParams.sentiment_blend" />
            <em>情绪在候选融合中的权重 0~1</em>
          </label>
          <label class="adv-check">
            <input type="checkbox" v-model="advParams.llm_debate" />
            <span>llm_debate</span>
            <em>② 多空辩论(仅 LLM 模式)</em>
          </label>
          <label class="adv-num">
            <span>llm_debate_rounds</span>
            <input type="number" step="1" min="1" max="3"
                   v-model.number="advParams.llm_debate_rounds" />
            <em>Bull→Bear 轮数</em>
          </label>
          <label class="adv-check">
            <input type="checkbox" v-model="advParams.llm_risk_debate" />
            <span>llm_risk_debate</span>
            <em>③ 风控三角(激进/中性/保守,只能缩仓或否决;仅 LLM 模式)</em>
          </label>
          <label class="adv-check">
            <input type="checkbox" v-model="advParams.llm_reflection" />
            <span>llm_reflection</span>
            <em>④ 历史经验(按相关度 + 已实现盈亏;仅 LLM 模式)</em>
          </label>
        </div>
        <div class="adv-note">
          ②③④ 仅在 <b>LLM 决策</b>模式生效;① 情绪在 ensemble 也生效。需配置对应供应商
          API key;缺 key/SDK 时这些层自动 no-op,不影响其余流程。
        </div>
      </details>
    </div>

    <!-- Portfolio + positions -->
    <div class="card">
      <div class="card-header">
        <h2>当前持仓</h2>
        <div class="muted">
          现金 ¥{{ formatMoney(portfolio?.cash ?? 0) }} / 市值 ¥{{
            formatMoney(portfolio?.market_value ?? 0)
          }}
          <span v-if="portfolio?.snapshot_date" class="asof">· 净值截至 {{ portfolio.snapshot_date }}</span>
        </div>
      </div>
      <div class="table-wrap" v-if="portfolio && portfolio.positions.length > 0">
        <table class="data-table">
        <thead>
          <tr>
            <th>代码</th>
            <th>名称</th>
            <th>数量</th>
            <th>成本</th>
            <th>买入时间</th>
            <th>现价</th>
            <th>最近更新</th>
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
            <td class="nowrap">{{ p.buy_date ?? "—" }}</td>
            <td>{{ p.current_price.toFixed(2) }}</td>
            <td :class="priceDateClass(p.price_date)">{{ p.price_date ?? "—" }}</td>
            <td>{{ formatMoney(p.market_value) }}</td>
            <td :class="p.pnl >= 0 ? 'up' : 'down'">{{ formatMoney(p.pnl) }}</td>
            <td :class="p.pnl_pct >= 0 ? 'up' : 'down'">{{ formatPct(p.pnl_pct) }}</td>
            <td>
              <button class="btn-link" @click="sellOne(p.code)">卖出</button>
            </td>
          </tr>
        </tbody>
        </table>
      </div>
      <div v-else class="empty">暂无持仓</div>
    </div>

    <!-- Pending orders detail -->
    <div class="card" v-if="pendingOrders.length > 0">
      <div class="card-header">
        <h2>待成交订单 <span class="count-badge">{{ pendingOrders.length }}</span></h2>
        <div class="muted">挂单按 T+1 规则，于预计成交日的{{ fillBasisLabel }}成交</div>
      </div>
      <div class="table-wrap">
        <table class="data-table">
        <thead>
          <tr>
            <th>代码</th>
            <th>名称</th>
            <th>方向</th>
            <th>数量</th>
            <th>加入队列</th>
            <th>预计成交日</th>
            <th>状态</th>
            <th>理由</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="o in pendingOrders" :key="o.order_id">
            <td>{{ o.code }}</td>
            <td>{{ o.name }}</td>
            <td :class="o.direction === 'buy' ? 'up' : 'down'">
              {{ o.direction === "buy" ? "买入" : "卖出" }}
            </td>
            <td>{{ o.quantity || "—" }}</td>
            <td class="nowrap">{{ formatDateTime(o.created_at) }}</td>
            <td class="nowrap">{{ o.expected_fill_date ?? "—" }}</td>
            <td>
              <span :class="o.bar_available ? 'tag tag-ready' : 'tag tag-wait'">
                {{ o.bar_available ? "待下轮成交" : "等待行情" }}
              </span>
              <span class="ttl-hint">
                已等 {{ o.trading_days_pending ?? 0 }}/{{ o.ttl_trading_days }} 交易日
              </span>
            </td>
            <td class="reason-cell" :title="o.reason">{{ o.reason || "—" }}</td>
          </tr>
        </tbody>
        </table>
      </div>
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
        <div class="muted">选定:<b>{{ displayStrategy(agent.last_strategy) }}</b></div>
      </div>
      <div class="table-wrap">
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
            <td>
              <b v-if="e.strategy_name === agent.last_strategy">{{ displayStrategy(e.strategy_name) }}</b>
              <span v-else>{{ displayStrategy(e.strategy_name) }}</span>
            </td>
            <td :class="e.annual_return >= 0 ? 'up' : 'down'">{{ formatPct(e.annual_return) }}</td>
            <td class="down">{{ formatPct(e.max_drawdown) }}</td>
            <td>{{ e.sharpe.toFixed(2) }}</td>
            <td>{{ e.total_trades }}</td>
            <td>{{ e.score.toFixed(3) }}</td>
          </tr>
        </tbody>
        </table>
      </div>
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
  fetchPendingOrders,
  fetchPortfolio,
  fetchScreeners,
  fetchStrategies,
  manualOrder,
  resetPortfolio,
  updateGoal,
  type AgentStatus,
  type DecisionRecord,
  type Goal,
  type PendingOrderDetail,
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

// Agent mode + P1-P4 upgrade switches. Mirrors a subset of goalDraft.params
// for direct UI binding. Synced both directions: loadAll() pulls server →
// advParams via syncAdvFromParams; saveGoal() pushes advParams → goalDraft.params
// via syncParamsFromAdv right before sending.
type AgentMode = "rule" | "ensemble" | "llm";
const advParams = reactive({
  agent_mode: "rule" as AgentMode,
  ensemble_enabled: false,
  industry_neutral: false,
  liquidity_filter: false,
  wf_enabled: true, // default-on
  // LLM enhancement layer (all default-off; ②③④ only apply in LLM mode,
  // ① sentiment also applies in ensemble mode).
  llm_provider: "anthropic" as "anthropic" | "deepseek",
  sentiment_enabled: false,
  sentiment_blend: 0.2,
  llm_debate: false,
  llm_debate_rounds: 1,
  llm_risk_debate: false,
  llm_reflection: false,
});

function syncAdvFromParams() {
  const p = (goalDraft.params || {}) as Record<string, unknown>;
  const ensemble = !!p.ensemble_enabled;
  const isLLM = p.agent_mode === "llm";
  advParams.agent_mode = isLLM ? "llm" : ensemble ? "ensemble" : "rule";
  advParams.ensemble_enabled = ensemble || isLLM;
  advParams.industry_neutral = !!p.industry_neutral;
  advParams.liquidity_filter = !!p.liquidity_filter;
  advParams.wf_enabled = p.wf_enabled !== false; // default true if absent
  advParams.llm_provider = p.llm_provider === "deepseek" ? "deepseek" : "anthropic";
  advParams.sentiment_enabled = !!p.sentiment_enabled;
  advParams.sentiment_blend =
    typeof p.sentiment_blend === "number" ? p.sentiment_blend : 0.2;
  advParams.llm_debate = !!p.llm_debate;
  advParams.llm_debate_rounds =
    typeof p.llm_debate_rounds === "number" ? p.llm_debate_rounds : 1;
  advParams.llm_risk_debate = !!p.llm_risk_debate;
  advParams.llm_reflection = !!p.llm_reflection;
}

function syncParamsFromAdv() {
  // Preserve any unknown keys the user may have set via API/MCP.
  const existing = (goalDraft.params || {}) as Record<string, unknown>;
  goalDraft.params = {
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
}

function applyPreset(mode: AgentMode) {
  advParams.agent_mode = mode;
  if (mode === "rule") {
    advParams.ensemble_enabled = false;
    advParams.industry_neutral = false;
    advParams.liquidity_filter = false;
    advParams.wf_enabled = true;
  } else {
    // ensemble and llm share the same selection-side configuration; LLM
    // just adds the Claude decision layer on top of those candidates.
    advParams.ensemble_enabled = true;
    advParams.industry_neutral = true;
    advParams.liquidity_filter = true;
    advParams.wf_enabled = true;
  }
}

const portfolio = ref<Portfolio | null>(null);
const agent = ref<AgentStatus | null>(null);
const decisions = ref<DecisionRecord[]>([]);
const strategies = ref<StrategyInfo[]>([]);
const screeners = ref<ScreenerInfo[]>([]);
const pendingOrders = ref<PendingOrderDetail[]>([]);

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
// Queued time — date + HH:MM is enough; seconds are noise here.
function formatDateTime(ts: string) {
  if (!ts) return "—";
  return ts.replace("T", " ").slice(0, 16);
}
// Flag a stale mark price: if the position's price date trails the
// portfolio snapshot date, the row hasn't been re-marked to the latest bar.
function priceDateClass(d: string | null) {
  if (!d) return "td-muted";
  if (portfolio.value?.snapshot_date && d < portfolio.value.snapshot_date)
    return "stale-date";
  return "";
}
const fillBasisLabel = computed(() =>
  pendingOrders.value[0]?.fill_price_basis === "close" ? "收盘价" : "开盘价");

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

// Lookup table: stable name → display label. Built reactively from the
// loaded strategy/screener lists. Falls back to the stable name if the
// list hasn't loaded yet or the lookup misses (e.g. user-removed plugin).
const strategyNameMap = computed(() => {
  const m = new Map<string, string>();
  for (const s of strategies.value) {
    if (s.name_zh) m.set(s.name, s.name_zh);
  }
  return m;
});
function displayStrategy(name: string): string {
  if (!name) return "";
  if (name === "ensemble") return "ensemble";  // synthetic, not in the list
  if (name === "llm") return "LLM";
  return strategyNameMap.value.get(name) || name;
}

// Mode badge derived from the live (post-save) advParams state.
// If the user pinned a strategy, all params are inert — show "钉死策略"
// so they're not confused by a mode badge that does nothing.
const modeLabel = computed(() => {
  if (goalDraft.strategy_name) return `钉死: ${displayStrategy(goalDraft.strategy_name)}`;
  if (advParams.agent_mode === "llm") return "LLM 决策";
  if (advParams.agent_mode === "ensemble") return "集成 (ensemble)";
  return "经典 (rule)";
});

const modeClass = computed(() => {
  if (goalDraft.strategy_name) return "mode-pinned";
  if (advParams.agent_mode === "llm") return "mode-llm";
  if (advParams.agent_mode === "ensemble") return "mode-ensemble";
  return "mode-rule";
});

const pendingCardClass = computed(() => {
  const n = agent.value?.pending_orders ?? 0;
  if (n > 10) return "pending-heavy";
  if (n > 0) return "pending-active";
  return "";
});

function kindClass(kind: string) {
  if (kind === "trade") return "kind-trade";
  if (kind === "risk_reject") return "kind-warn";
  if (kind === "cycle") return "kind-info";
  if (kind === "agent_start" || kind === "agent_stop") return "kind-meta";
  if (kind === "strategy_pick" || kind === "strategy_ensemble") return "kind-info";
  if (kind === "llm_cycle") return "kind-llm";
  if (kind === "agent_error") return "kind-error";
  if (kind === "order_queued") return "kind-pending";
  if (kind === "order_filled_pending") return "kind-trade";  // = real fill
  if (kind === "order_expired_pending") return "kind-meta";
  return "";
}

async function loadAll() {
  const [g, p, a, d, str, scr, pend] = await Promise.all([
    fetchGoal(),
    fetchPortfolio(),
    fetchAgentStatus(),
    fetchAgentDecisions(50),
    fetchStrategies(),
    fetchScreeners(),
    fetchPendingOrders(),
  ]);
  Object.assign(goalDraft, g.data);
  syncAdvFromParams();
  portfolio.value = p.data;
  agent.value = a.data;
  decisions.value = d.data;
  strategies.value = str.data;
  screeners.value = scr.data;
  pendingOrders.value = pend.data;
}

async function loadDecisions() {
  const d = await fetchAgentDecisions(50);
  decisions.value = d.data;
}

async function saveGoal() {
  saving.value = true;
  try {
    // Push the mode/upgrade switches into goalDraft.params right before sending.
    syncParamsFromAdv();
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
/* Horizontal scroll on narrow screens — matches the .table-wrap convention
   already used by Dashboard/Backtest/Screener/Pool. Without it the wide
   tables (10-col holdings, pending orders) wrap Chinese text char-by-char
   on mobile. Desktop is unaffected (table fits, nothing to scroll). */
.table-wrap {
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
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
  /* Keep each cell on one line so the table grows to its natural width and
     scrolls inside .table-wrap on mobile, instead of wrapping Chinese names
     char-by-char. .reason-cell overrides with its own ellipsis clamp. */
  white-space: nowrap;
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
.asof {
  margin-left: 4px;
  color: var(--color-text-secondary);
}
.nowrap {
  white-space: nowrap;
}
.td-muted {
  color: var(--color-text-secondary);
}
.stale-date {
  color: #c2870b;
}
.count-badge {
  display: inline-block;
  min-width: 18px;
  padding: 0 6px;
  margin-left: 4px;
  font-size: 12px;
  line-height: 18px;
  text-align: center;
  border-radius: 9px;
  background: rgba(0, 0, 0, 0.06);
  color: var(--color-text-secondary);
}
.tag {
  display: inline-block;
  padding: 1px 7px;
  border-radius: 4px;
  font-size: 12px;
  white-space: nowrap;
}
.tag-ready {
  background: rgba(52, 168, 83, 0.12);
  color: #1e7e34;
}
.tag-wait {
  background: rgba(234, 179, 8, 0.14);
  color: #a16207;
}
.ttl-hint {
  margin-left: 6px;
  font-size: 12px;
  color: var(--color-text-secondary);
}
.reason-cell {
  /* Show the full LLM rationale: wrap to a few lines instead of clipping
     with an ellipsis. Overrides the table-wide `white-space: nowrap`. */
  max-width: 360px;
  white-space: normal;
  word-break: break-word;
  line-height: 1.4;
  color: var(--color-text-secondary);
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

/* Mode badge: surfaces which agent path will run on the next tick. */
.stat-card.mode-rule {
  background: rgba(107, 114, 128, 0.08);
}
.stat-card.mode-ensemble {
  background: rgba(22, 163, 74, 0.08);
}
.stat-card.mode-llm {
  background: rgba(139, 92, 246, 0.10);
  border: 1px solid rgba(139, 92, 246, 0.25);
}
.stat-card.mode-pinned {
  background: rgba(245, 158, 11, 0.10);
  border: 1px solid rgba(245, 158, 11, 0.25);
}
.stat-card.pending-active {
  background: rgba(245, 158, 11, 0.10);
  border: 1px solid rgba(245, 158, 11, 0.25);
}
.stat-card.pending-heavy {
  background: rgba(192, 57, 43, 0.10);
  border: 1px solid rgba(192, 57, 43, 0.30);
}
.decision.kind-pending {
  background: rgba(245, 158, 11, 0.06);
  border-left: 3px solid rgba(245, 158, 11, 0.5);
  padding-left: 10px;
}

/* Mode picker pills: three radio-like clickable cards. */
.mode-presets {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
  margin-bottom: 8px;
}
.mode-pill {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  padding: 12px 14px;
  background: rgba(0, 0, 0, 0.02);
  border: 1.5px solid transparent;
  border-radius: 10px;
  cursor: pointer;
  transition: all 0.15s ease;
  text-align: left;
}
.mode-pill:hover {
  background: rgba(0, 0, 0, 0.04);
}
.mode-pill .mode-title {
  font-weight: 600;
  font-size: 14px;
}
.mode-pill .mode-desc {
  font-size: 11.5px;
  color: var(--color-text-secondary);
  line-height: 1.45;
}
.mode-pill.mode-active {
  background: rgba(0, 113, 227, 0.05);
}
.mode-pill.mode-rule-active {
  border-color: rgba(107, 114, 128, 0.5);
  background: rgba(107, 114, 128, 0.08);
}
.mode-pill.mode-ensemble-active {
  border-color: rgba(22, 163, 74, 0.5);
  background: rgba(22, 163, 74, 0.08);
}
.mode-pill.mode-llm-active {
  border-color: rgba(139, 92, 246, 0.5);
  background: rgba(139, 92, 246, 0.10);
}

/* Advanced switches (folded by default). */
.advanced {
  margin-top: 14px;
  padding: 10px 12px;
  background: rgba(0, 0, 0, 0.02);
  border-radius: 8px;
  font-size: 13px;
}
.advanced summary {
  cursor: pointer;
  font-weight: 600;
  user-select: none;
}
.adv-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 8px 16px;
  margin-top: 10px;
}
.adv-check {
  display: grid;
  grid-template-columns: auto 1fr;
  align-items: baseline;
  gap: 6px 8px;
  padding: 4px 0;
}
.adv-check span {
  font-family: ui-monospace, "SF Mono", monospace;
  font-size: 12.5px;
  color: #1e3a8a;
}
.adv-check em {
  grid-column: 2;
  font-style: normal;
  color: var(--color-text-secondary);
  font-size: 11.5px;
}
.adv-note {
  margin-top: 10px;
  font-size: 11.5px;
  color: var(--color-text-secondary);
  line-height: 1.5;
}
.adv-note code {
  background: rgba(0, 0, 0, 0.06);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 11px;
}
.adv-llm-provider {
  margin-top: 10px;
}
.adv-llm-provider label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  max-width: 520px;
}
.adv-num {
  display: grid;
  grid-template-columns: 1fr 90px;
  align-items: center;
  gap: 4px 8px;
  padding: 4px 0;
}
.adv-num span {
  font-family: ui-monospace, "SF Mono", monospace;
  font-size: 12.5px;
  color: #1e3a8a;
}
.adv-num em {
  grid-column: 1 / -1;
  font-style: normal;
  color: var(--color-text-secondary);
  font-size: 11.5px;
}
.advanced select,
.advanced input[type="number"] {
  padding: 6px 8px;
  border: 1px solid rgba(0, 0, 0, 0.12);
  border-radius: 6px;
  font-size: 13px;
}
</style>
