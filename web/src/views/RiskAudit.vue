<template>
  <div class="risk-audit">
    <div class="page-header">
      <h1>风控审计</h1>
      <p class="page-desc">
        止损 / 止盈 / 熔断的当前配置、三通道一致性、护栏实时状态与最近的风控退出。
        <span v-if="data" class="acct-tag" :class="data.is_live ? 'live' : 'paper'">
          {{ data.is_live ? "实盘 QMT" : "模拟盘 Paper" }}
        </span>
      </p>
    </div>

    <div v-if="err" class="err-banner">{{ err }}</div>

    <template v-if="data">
      <!-- 退出阈值 -->
      <div class="section-head">
        <h2>退出阈值</h2>
        <button class="btn-edit" @click="toggleEdit">{{ editing ? "取消" : "编辑" }}</button>
      </div>

      <div v-if="!editing" class="stats-row">
        <div class="stat-card">
          <span class="stat-label">单标的止损线</span>
          <span class="stat-value neg">{{ pct(data.exits.stop_loss.threshold) }}</span>
        </div>
        <div class="stat-card">
          <span class="stat-label">ATR 自适应止损</span>
          <span class="stat-value" v-if="data.exits.atr_stop.enabled">
            ×{{ data.exits.atr_stop.k }}
            <span class="stat-sub">ATR({{ data.exits.atr_stop.n }}) · 替代固定止损</span>
          </span>
          <span class="stat-value muted" v-else>关闭</span>
        </div>
        <div class="stat-card">
          <span class="stat-label">组合回撤熔断</span>
          <span class="stat-value neg">{{ pct(data.exits.portfolio_circuit_breaker.threshold) }}</span>
        </div>
        <div class="stat-card">
          <span class="stat-label">移动止盈</span>
          <span class="stat-value" v-if="data.exits.trailing_take_profit.enabled">
            +{{ pct(data.exits.trailing_take_profit.activate) }}
            <span class="stat-sub">武装 · 回撤 {{ pct(data.exits.trailing_take_profit.trail) }} 离场</span>
          </span>
          <span class="stat-value muted" v-else>已关闭</span>
        </div>
        <div class="stat-card">
          <span class="stat-label">策略离场</span>
          <span class="stat-value" :class="data.exits.strategy_exit.enabled ? 'pos' : 'muted'">
            {{ data.exits.strategy_exit.enabled ? "启用" : "关闭" }}
          </span>
        </div>
      </div>

      <form v-else class="card edit-form" @submit.prevent="save">
        <div class="form-grid">
          <label>单标的止损线 (%)
            <input type="number" step="0.5" v-model.number="form.stop_loss_pct" />
          </label>
          <label>组合回撤熔断 (%)
            <input type="number" step="0.5" v-model.number="form.portfolio_stop_loss_pct" />
          </label>
          <label>移动止盈激活 (%)
            <input type="number" step="0.5" min="0" v-model.number="form.take_profit_activate_pct" />
          </label>
          <label>移动止盈回撤 (%)
            <input type="number" step="0.5" min="0" v-model.number="form.take_profit_trail_pct" />
          </label>
          <label>ATR 乘子 k (0=关闭)
            <input type="number" step="0.5" min="0" v-model.number="form.atr_stop_k" />
          </label>
          <label>ATR 周期 n
            <input type="number" step="1" min="1" v-model.number="form.atr_stop_n" />
          </label>
          <label class="chk">
            <input type="checkbox" v-model="form.strategy_exit_enabled" />
            启用策略离场
          </label>
        </div>
        <p class="form-hint">
          止损 / 熔断为负百分比(如 -8)。ATR k&gt;0 时,单标的止损改用 -k×(ATR/价),替代固定止损线;改动即时生效,无需重启。
        </p>
        <div class="form-actions">
          <span v-if="saveMsg" class="save-msg" :class="saveErr ? 'err' : 'ok'">{{ saveMsg }}</span>
          <button type="submit" class="btn-save" :disabled="saving">{{ saving ? "保存中…" : "保存" }}</button>
        </div>
      </form>

      <!-- 护栏 + 熔断状态 -->
      <div class="two-col">
        <div class="card status-card">
          <div class="card-header"><h2>买入护栏(只锁新买,不强卖)</h2></div>
          <div class="status-body">
            <div class="status-line">
              <span>当前状态</span>
              <span class="badge" :class="data.guard.locked ? 'bad' : 'ok'">
                {{ data.guard.locked ? "锁定中" : "正常" }}
              </span>
            </div>
            <div v-if="data.guard.locked" class="status-reason">{{ data.guard.reason }}</div>
            <div class="status-line">
              <span>连损护栏 StoplossGuard</span>
              <span class="muted">
                近 {{ data.guard.stoploss_guard.lookback_days }} 日止损 ≥
                {{ data.guard.stoploss_guard.trade_limit }} 次 → 锁 {{ data.guard.stoploss_guard.lock_days }} 日
                <template v-if="!data.guard.stoploss_guard.enabled">(关闭)</template>
              </span>
            </div>
            <div class="status-line">
              <span>近期止损次数</span>
              <span :class="data.guard.recent_stop_losses >= data.guard.stoploss_guard.trade_limit ? 'neg' : ''">
                {{ data.guard.recent_stop_losses }}
              </span>
            </div>
            <div class="status-line">
              <span>回撤软锁 MaxDrawdown</span>
              <span class="muted">
                近 {{ data.guard.max_drawdown.lookback_days }} 日回撤 ≤
                {{ pct(data.guard.max_drawdown.max_drawdown_pct) }} → 锁 {{ data.guard.max_drawdown.lock_days }} 日
                <template v-if="!data.guard.max_drawdown.enabled">(关闭)</template>
              </span>
            </div>
          </div>
        </div>

        <div class="card status-card">
          <div class="card-header"><h2>组合熔断距离</h2></div>
          <div class="status-body">
            <template v-if="data.circuit_breaker.drawdown !== null">
              <div class="status-line">
                <span>当前回撤</span>
                <span :class="data.circuit_breaker.drawdown < 0 ? 'neg' : 'pos'">
                  {{ pct(data.circuit_breaker.drawdown) }}
                </span>
              </div>
              <div class="cb-bar">
                <div class="cb-fill" :class="data.circuit_breaker.tripped ? 'tripped' : ''"
                     :style="{ width: cbPct + '%' }"></div>
                <div class="cb-threshold"></div>
              </div>
              <div class="status-line cb-foot">
                <span class="muted">净值 {{ fmtMoney(data.circuit_breaker.total_value) }}
                  / 峰值 {{ fmtMoney(data.circuit_breaker.peak_value) }}</span>
                <span class="badge" :class="data.circuit_breaker.tripped ? 'bad' : 'ok'">
                  {{ data.circuit_breaker.tripped ? "已触发(清仓)"
                     : "距熔断 " + pct(data.circuit_breaker.headroom) }}
                </span>
              </div>
            </template>
            <p v-else class="muted">暂无净值快照,无法计算回撤。</p>
          </div>
        </div>
      </div>

      <!-- 三通道一致性 -->
      <div class="card">
        <div class="card-header">
          <h2>三通道退出一致性</h2>
          <span class="card-header-hint">回测 / 模拟盘 / 实盘是否启用同样的退出档</span>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr><th>通道</th><th>固定止损</th><th>移动止盈</th><th>策略离场</th><th>说明</th></tr>
            </thead>
            <tbody>
              <tr v-for="c in data.channel_parity" :key="c.channel">
                <td class="td-name">{{ c.channel }}</td>
                <td><span class="dot" :class="c.stop_loss ? 'on' : 'off'"></span></td>
                <td><span class="dot" :class="c.trailing_tp ? 'on' : 'off'"></span></td>
                <td><span class="dot" :class="c.strategy_exit ? 'on' : 'off'"></span></td>
                <td class="td-muted">{{ c.note || "—" }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- 最近风控退出 -->
      <div class="card">
        <div class="card-header">
          <h2>最近风控退出</h2>
          <span class="card-header-hint" v-if="data.recent_exits.length">
            共 {{ data.recent_exits.length }} 条
          </span>
        </div>
        <div v-if="!data.recent_exits.length" class="empty-state">
          <p>暂无风控退出记录</p>
          <p class="empty-hint">止损 / 移动止盈 / 策略离场 / 组合熔断成交后会出现在这里</p>
        </div>
        <div v-else class="table-wrap">
          <table>
            <thead>
              <tr><th>时间</th><th>类型</th><th>代码</th><th>成交价</th><th>数量</th><th>原因</th></tr>
            </thead>
            <tbody>
              <tr v-for="(e, i) in data.recent_exits" :key="i">
                <td class="td-muted">{{ fmtTime(e.ts) }}</td>
                <td><span class="badge" :class="kindClass(e.kind)">{{ kindLabel(e.kind) }}</span></td>
                <td><span class="code-badge">{{ e.code }}</span></td>
                <td>{{ e.price != null ? e.price.toFixed(2) : "—" }}</td>
                <td>{{ e.quantity ?? "—" }}</td>
                <td class="td-muted">{{ e.reason }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>

    <div v-else-if="!err" class="empty-state"><p>加载中…</p></div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";
import {
  fetchRiskAudit, fetchRiskControl, saveRiskControl, type RiskAudit,
} from "../api/client";

const data = ref<RiskAudit | null>(null);
const err = ref("");
let timer: ReturnType<typeof setInterval> | null = null;

const pct = (x: number | null) => (x == null ? "—" : (x * 100).toFixed(1) + "%");
const fmtMoney = (x: number | null) =>
  x == null ? "—" : "¥" + Math.round(x).toLocaleString("zh-CN");
const fmtTime = (ts: string) => (ts ? new Date(ts).toLocaleString("zh-CN") : "—");

// How far drawdown has eaten into the breaker budget, 0–100%.
const cbPct = computed(() => {
  const cb = data.value?.circuit_breaker;
  if (!cb || cb.drawdown == null || !cb.threshold) return 0;
  return Math.max(0, Math.min(100, (cb.drawdown / cb.threshold) * 100));
});

const KIND: Record<string, string> = {
  stop_loss: "止损",
  trailing_tp: "移动止盈",
  strategy_exit: "策略离场",
  circuit_breaker: "组合熔断",
  other: "其他",
};
const kindLabel = (k: string) => KIND[k] ?? k;
const kindClass = (k: string) =>
  k === "stop_loss" || k === "circuit_breaker" ? "bad" : k === "trailing_tp" ? "ok" : "neutral";

async function load() {
  try {
    const res = await fetchRiskAudit();
    data.value = res.data;
    err.value = "";
  } catch (e: any) {
    err.value = e?.message || "加载风控审计失败";
  }
}

// --- edit runtime risk config (P0-3) ---
// form holds the % fields as human percentages (-8, 15…); converted on save.
const editing = ref(false);
const saving = ref(false);
const saveMsg = ref("");
const saveErr = ref(false);
const form = reactive({
  stop_loss_pct: -8, portfolio_stop_loss_pct: -15,
  take_profit_activate_pct: 15, take_profit_trail_pct: 10,
  strategy_exit_enabled: true, atr_stop_k: 0, atr_stop_n: 14,
});

async function toggleEdit() {
  if (editing.value) {
    editing.value = false;
    return;
  }
  saveMsg.value = "";
  try {
    const { data: c } = await fetchRiskControl();
    form.stop_loss_pct = +(c.stop_loss_pct * 100).toFixed(2);
    form.portfolio_stop_loss_pct = +(c.portfolio_stop_loss_pct * 100).toFixed(2);
    form.take_profit_activate_pct = +(c.take_profit_activate_pct * 100).toFixed(2);
    form.take_profit_trail_pct = +(c.take_profit_trail_pct * 100).toFixed(2);
    form.strategy_exit_enabled = c.strategy_exit_enabled;
    form.atr_stop_k = c.atr_stop_k;
    form.atr_stop_n = c.atr_stop_n;
    editing.value = true;
  } catch (e: any) {
    err.value = e?.message || "读取风控配置失败";
  }
}

async function save() {
  saving.value = true;
  saveMsg.value = "";
  saveErr.value = false;
  try {
    await saveRiskControl({
      stop_loss_pct: form.stop_loss_pct / 100,
      portfolio_stop_loss_pct: form.portfolio_stop_loss_pct / 100,
      take_profit_activate_pct: form.take_profit_activate_pct / 100,
      take_profit_trail_pct: form.take_profit_trail_pct / 100,
      strategy_exit_enabled: form.strategy_exit_enabled,
      atr_stop_k: form.atr_stop_k,
      atr_stop_n: form.atr_stop_n,
    });
    editing.value = false;
    await load();
  } catch (e: any) {
    saveErr.value = true;
    saveMsg.value = e?.response?.data?.detail || e?.message || "保存失败";
  } finally {
    saving.value = false;
  }
}

onMounted(() => {
  load();
  timer = setInterval(load, 10_000);
});
onUnmounted(() => {
  if (timer) clearInterval(timer);
});
</script>

<style scoped>
.risk-audit {
  display: flex;
  flex-direction: column;
  gap: 24px;
}
.page-header h1 {
  font-size: 32px;
  font-weight: 700;
  letter-spacing: -0.5px;
  color: var(--color-text-primary);
}
.page-desc {
  margin-top: 4px;
  font-size: 15px;
  color: var(--color-text-secondary);
}
.acct-tag {
  margin-left: 8px;
  font-size: 12px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 10px;
}
.acct-tag.live { background: rgba(255, 59, 48, 0.1); color: #ff3b30; }
.acct-tag.paper { background: rgba(0, 113, 227, 0.1); color: #0071e3; }

.err-banner {
  padding: 10px 14px;
  border-radius: var(--radius-md);
  background: rgba(255, 59, 48, 0.08);
  color: var(--color-red, #dc2626);
  font-size: 13px;
}

.stats-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
}
.stat-card {
  background: var(--color-surface);
  border-radius: var(--radius-lg);
  padding: 20px;
  box-shadow: var(--shadow-sm);
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.stat-label { font-size: 13px; color: var(--color-text-secondary); }
.stat-value {
  font-size: 26px;
  font-weight: 700;
  letter-spacing: -0.5px;
  color: var(--color-text-primary);
}
.stat-sub {
  display: block;
  font-size: 12px;
  font-weight: 400;
  color: var(--color-text-tertiary);
}
.stat-value.neg, .neg { color: #ff3b30; }
.stat-value.pos, .pos { color: #34c759; }
.stat-value.muted, .muted { color: var(--color-text-tertiary); }

.two-col {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 16px;
}
.card {
  background: var(--color-surface);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  overflow: hidden;
}
.card-header {
  padding: 18px 22px 12px;
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.card-header h2 { font-size: 18px; font-weight: 600; letter-spacing: -0.3px; }
.card-header-hint { font-size: 13px; color: var(--color-text-tertiary); }

.status-body { padding: 4px 22px 20px; }
.status-line {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 8px 0;
  font-size: 14px;
  border-bottom: 0.5px solid var(--color-border);
}
.status-line:last-child { border-bottom: none; }
.status-reason {
  margin: 6px 0;
  padding: 8px 12px;
  font-size: 13px;
  background: rgba(255, 59, 48, 0.06);
  color: #b91c1c;
  border-radius: var(--radius-sm);
}
.cb-foot { border-bottom: none; }

.badge {
  font-size: 12px;
  font-weight: 600;
  padding: 2px 10px;
  border-radius: 10px;
}
.badge.ok { background: rgba(52, 199, 89, 0.12); color: #2e9e4f; }
.badge.bad { background: rgba(255, 59, 48, 0.12); color: #ff3b30; }
.badge.neutral { background: rgba(0, 113, 227, 0.1); color: #0071e3; }

.cb-bar {
  position: relative;
  height: 10px;
  margin: 12px 0;
  background: rgba(52, 199, 89, 0.15);
  border-radius: 5px;
  overflow: hidden;
}
.cb-fill {
  height: 100%;
  background: #f59e0b;
  border-radius: 5px;
  transition: width 0.4s ease;
}
.cb-fill.tripped { background: #ff3b30; }

.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th {
  padding: 10px 22px;
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--color-text-tertiary);
  text-align: left;
  background: rgba(0, 0, 0, 0.02);
  border-top: 0.5px solid var(--color-border);
  border-bottom: 0.5px solid var(--color-border);
}
td {
  padding: 11px 22px;
  font-size: 14px;
  border-bottom: 0.5px solid var(--color-border);
}
tbody tr:last-child td { border-bottom: none; }
.td-name { font-weight: 500; }
.td-muted { color: var(--color-text-secondary); font-size: 13px; }
.code-badge {
  font-family: var(--font-mono);
  font-size: 13px;
  font-weight: 500;
  padding: 2px 8px;
  background: var(--color-bg);
  border-radius: 6px;
}
.dot {
  display: inline-block;
  width: 9px;
  height: 9px;
  border-radius: 50%;
}
.dot.on { background: #34c759; }
.dot.off { background: rgba(0, 0, 0, 0.18); }

.empty-state { padding: 40px 22px; text-align: center; color: var(--color-text-secondary); }
.empty-hint { font-size: 13px; color: var(--color-text-tertiary); margin-top: 4px; }

/* --- edit form (P0-3) --- */
.section-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
}
.section-head h2 { font-size: 18px; font-weight: 600; letter-spacing: -0.3px; }
.btn-edit {
  font-size: 13px;
  font-weight: 500;
  color: var(--color-accent);
  background: var(--color-blue-bg, rgba(0, 113, 227, 0.1));
  border: none;
  border-radius: 12px;
  padding: 4px 14px;
  cursor: pointer;
}
.btn-edit:hover { background: rgba(0, 113, 227, 0.15); }
.edit-form { padding: 20px 22px; }
.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 14px 20px;
}
.form-grid label {
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 13px;
  color: var(--color-text-secondary);
}
.form-grid input[type="number"] {
  height: 34px;
  padding: 0 10px;
  border: 1px solid rgba(0, 0, 0, 0.12);
  border-radius: var(--radius-sm, 8px);
  font-size: 14px;
  font-family: var(--font-mono);
  color: var(--color-text-primary);
  background: var(--color-surface);
}
.form-grid label.chk {
  flex-direction: row;
  align-items: center;
  gap: 8px;
  font-size: 14px;
}
.form-hint {
  margin: 14px 0 0;
  font-size: 12px;
  color: var(--color-text-tertiary);
  line-height: 1.5;
}
.form-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 14px;
}
.save-msg { font-size: 13px; }
.save-msg.ok { color: #2e9e4f; }
.save-msg.err { color: #ff3b30; }
.btn-save {
  height: 36px;
  padding: 0 22px;
  background: var(--color-accent);
  color: #fff;
  border: none;
  border-radius: 18px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
}
.btn-save:disabled { opacity: 0.5; cursor: not-allowed; }
</style>
