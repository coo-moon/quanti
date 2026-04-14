<template>
  <div class="dashboard">
    <div class="page-header">
      <h1>仪表盘</h1>
      <p class="page-desc">管理你的股票池和市场数据</p>
    </div>

    <div class="stats-row">
      <div class="stat-card">
        <div class="stat-icon blue">
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
            <rect x="2" y="10" width="4" height="8" rx="1" fill="currentColor" />
            <rect x="8" y="6" width="4" height="12" rx="1" fill="currentColor" />
            <rect x="14" y="2" width="4" height="16" rx="1" fill="currentColor" />
          </svg>
        </div>
        <div class="stat-info">
          <span class="stat-label">已同步股票</span>
          <span class="stat-value">{{ poolStats?.with_quotes ?? stocks.length }}</span>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon purple">
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
            <path d="M10 2v16M2 10h16" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
          </svg>
        </div>
        <div class="stat-info">
          <span class="stat-label">股票池总数</span>
          <span class="stat-value">{{ poolStats?.total ?? '-' }}</span>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon green">
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
            <circle cx="10" cy="10" r="7" stroke="currentColor" stroke-width="2" />
            <path d="M10 6v4l3 2" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
          </svg>
        </div>
        <div class="stat-info">
          <span class="stat-label">最近更新</span>
          <span class="stat-value stat-value-sm">{{ lastUpdate }}</span>
        </div>
      </div>
    </div>

    <!-- Add Stock -->
    <div class="card add-card">
      <div class="add-row">
        <div class="add-input-wrap">
          <svg class="add-icon" width="16" height="16" viewBox="0 0 16 16" fill="none">
            <circle cx="8" cy="8" r="7" stroke="currentColor" stroke-width="1.5" />
            <path d="M8 5v6M5 8h6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
          </svg>
          <input
            v-model="addInput"
            placeholder="输入股票代码，多个用逗号分隔，如 600519,000858,300750"
            @keyup.enter="addStocks"
          />
        </div>
        <button class="btn-primary" @click="addStocks" :disabled="syncing || !addInput.trim()">
          <span v-if="syncing" class="spinner" />
          {{ syncing ? "同步中..." : "添加并同步" }}
        </button>
        <button
          v-if="stocks.length > 0"
          class="btn-secondary"
          @click="syncAll"
          :disabled="syncing"
        >
          <span v-if="syncingAll" class="spinner dark" />
          {{ syncingAll ? "同步中..." : "下载K线" }}
        </button>
        <button class="btn-pool" @click="syncFullPool" :disabled="syncingPool">
          <span v-if="syncingPool" class="spinner dark" />
          {{ syncingPool ? "同步中..." : "同步全A股池" }}
        </button>
      </div>
      <div v-if="syncMsg" class="sync-msg" :class="syncError ? 'error' : 'success'">
        {{ syncMsg }}
      </div>
    </div>

    <!-- Download Progress Bar -->
    <div v-if="syncJobId && syncProgress.total > 0" class="progress-bar-wrap">
      <div class="progress-info">
        <span>已下载 {{ syncProgress.current }}/{{ syncProgress.total }}<span v-if="syncProgress.eta_seconds" class="progress-eta">，约剩 {{ Math.floor(syncProgress.eta_seconds / 60) }} 分钟</span></span>
        <span v-if="Object.keys(syncProgress.errors).length" class="progress-errors">
          {{ Object.keys(syncProgress.errors).length }} 只失败
        </span>
      </div>
      <div class="progress-bar">
        <div class="progress-fill" :style="{ width: (syncProgress.current / syncProgress.total * 100) + '%' }"></div>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <h2>股票列表</h2>
        <span class="card-header-hint" v-if="stocks.length">共 {{ stocks.length }} 只</span>
      </div>
      <div v-if="stocks.length === 0" class="empty-state">
        <p>暂无股票数据</p>
        <p class="empty-hint">在上方输入股票代码添加</p>
      </div>
      <div v-else class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>代码</th>
              <th>名称</th>
              <th>交易所</th>
              <th>行业</th>
              <th>上市日期</th>
              <th>最新数据</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="stock in stocks" :key="stock.code">
              <td><span class="code-badge">{{ stock.code }}</span></td>
              <td class="td-name">{{ stock.name }}</td>
              <td><span class="exchange-tag" :class="stock.exchange.toLowerCase()">{{ stock.exchange }}</span></td>
              <td>{{ stock.industry || '-' }}</td>
              <td class="td-muted">{{ stock.list_date }}</td>
              <td :class="stock.latest_date ? 'td-date' : 'td-muted'">{{ stock.latest_date || '-' }}</td>
              <td>
                <button
                  class="btn-small"
                  @click="syncOne(stock.code)"
                  :disabled="syncingCodes.has(stock.code)"
                >
                  {{ syncingCodes.has(stock.code) ? "同步中" : "同步" }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from "vue";
import { fetchStocks, fetchStockStats, syncQuotes, syncStockList, syncQuotesAsync, fetchQuotesSyncStatus, type StockInfo, type StockPoolStats, type SyncStatus } from "../api/client";

const stocks = ref<StockInfo[]>([]);
const poolStats = ref<StockPoolStats | null>(null);
const addInput = ref("");
const syncing = ref(false);
const syncingAll = ref(false);
const syncingPool = ref(false);
const syncMsg = ref("");
const syncError = ref(false);
const syncingCodes = reactive(new Set<string>());
const syncJobId = ref<string | null>(null);
const syncProgress = ref<SyncStatus>({ job_id: "", current: 0, total: 0, status: "", errors: {}, message: "", eta_seconds: null });
let pollTimer: ReturnType<typeof setInterval> | null = null;

const lastUpdate = computed(() => {
  return new Date().toLocaleDateString("zh-CN");
});

onMounted(async () => {
  await loadStocks();
});

async function loadStocks() {
  try {
    const [stocksRes, statsRes] = await Promise.all([fetchStocks(), fetchStockStats()]);
    stocks.value = stocksRes.data;
    poolStats.value = statsRes.data;
  } catch (e) {
    console.error("Failed to fetch stocks:", e);
  }
}

async function syncFullPool() {
  syncingPool.value = true;
  syncMsg.value = "";
  syncError.value = false;
  try {
    const res = await syncStockList();
    syncMsg.value = res.data.message;
    syncError.value = false;
    await loadStocks();
  } catch (e) {
    syncMsg.value = "同步全A股池失败，请检查网络";
    syncError.value = true;
  } finally {
    syncingPool.value = false;
  }
}

function parseCodes(input: string): string[] {
  return input
    .replace(/\s+/g, ",")
    .split(",")
    .map((s) => s.trim())
    .filter((s) => /^\d{6}$/.test(s));
}

async function addStocks() {
  const codes = parseCodes(addInput.value);
  if (codes.length === 0) {
    syncMsg.value = "请输入有效的6位股票代码";
    syncError.value = true;
    return;
  }
  syncing.value = true;
  syncMsg.value = "";
  syncError.value = false;
  try {
    const res = await syncQuotes(codes);
    const data = res.data;
    const okCount = Object.values(data.synced).filter((n) => n > 0).length;
    const errCount = Object.keys(data.errors).length;
    if (errCount > 0) {
      const errCodes = Object.keys(data.errors).join(", ");
      syncMsg.value = `成功同步 ${okCount} 只，${errCount} 只失败（${errCodes}）`;
      syncError.value = errCount > okCount;
    } else {
      syncMsg.value = `成功同步 ${okCount} 只股票`;
      syncError.value = false;
    }
    addInput.value = "";
    await loadStocks();
  } catch (e) {
    syncMsg.value = "同步请求失败，请检查服务是否正常运行";
    syncError.value = true;
  } finally {
    syncing.value = false;
  }
}

async function syncOne(code: string) {
  syncingCodes.add(code);
  try {
    await syncQuotes([code]);
    await loadStocks();
  } catch (e) {
    console.error(`Sync failed for ${code}:`, e);
  } finally {
    syncingCodes.delete(code);
  }
}

function startPolling(jobId: string) {
  syncJobId.value = jobId;
  pollTimer = setInterval(async () => {
    try {
      const res = await fetchQuotesSyncStatus(jobId);
      syncProgress.value = res.data;
      if (res.data.status !== "running") {
        stopPolling();
        await loadStocks();
        syncMsg.value = res.data.message;
        syncError.value = res.data.status === "error";
      }
    } catch (e) {
      console.error("Poll error:", e);
    }
  }, 1000);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  syncJobId.value = null;
}

async function syncAll() {
  if (stocks.value.length === 0) return;
  syncingAll.value = true;
  syncing.value = true;
  syncMsg.value = "";
  syncError.value = false;
  syncProgress.value = { job_id: "", current: 0, total: 0, status: "running", errors: {}, message: "启动中...", eta_seconds: null };
  try {
    const res = await syncQuotesAsync();
    if (res.data.job_id) {
      startPolling(res.data.job_id);
    }
  } catch (e) {
    syncMsg.value = "同步启动失败";
    syncError.value = true;
    stopPolling();
    syncingAll.value = false;
    syncing.value = false;
  }
}
</script>

<style scoped>
.dashboard {
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

.stats-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 16px;
}

.stat-card {
  background: var(--color-surface);
  border-radius: var(--radius-lg);
  padding: 20px;
  display: flex;
  align-items: center;
  gap: 16px;
  box-shadow: var(--shadow-sm);
  transition: box-shadow var(--transition);
}

.stat-card:hover {
  box-shadow: var(--shadow-md);
}

.stat-icon {
  width: 44px;
  height: 44px;
  border-radius: var(--radius-md);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.stat-icon.blue {
  background: rgba(0, 113, 227, 0.1);
  color: #0071e3;
}

.stat-icon.green {
  background: rgba(52, 199, 89, 0.1);
  color: #34c759;
}

.stat-icon.purple {
  background: rgba(175, 82, 222, 0.1);
  color: #af52de;
}

.stat-label {
  display: block;
  font-size: 13px;
  color: var(--color-text-secondary);
  margin-bottom: 2px;
}

.stat-value {
  font-size: 28px;
  font-weight: 700;
  letter-spacing: -0.5px;
  color: var(--color-text-primary);
}

.stat-value-sm {
  font-size: 17px;
  font-weight: 600;
}

.card {
  background: var(--color-surface);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  overflow: hidden;
}

.add-card {
  padding: 20px 24px;
}

.add-row {
  display: flex;
  gap: 10px;
  align-items: center;
}

.add-input-wrap {
  flex: 1;
  position: relative;
  display: flex;
  align-items: center;
}

.add-icon {
  position: absolute;
  left: 12px;
  color: var(--color-text-tertiary);
  pointer-events: none;
}

.add-input-wrap input {
  width: 100%;
  height: 40px;
  padding: 0 12px 0 36px;
  border: 1px solid rgba(0, 0, 0, 0.12);
  border-radius: var(--radius-sm);
  font-size: 14px;
  font-family: var(--font-sans);
  color: var(--color-text-primary);
  background: var(--color-surface);
  outline: none;
  transition: all var(--transition);
}

.add-input-wrap input:focus {
  border-color: var(--color-accent);
  box-shadow: 0 0 0 3px rgba(0, 113, 227, 0.15);
}

.btn-primary {
  height: 40px;
  padding: 0 20px;
  background: var(--color-accent);
  color: white;
  border: none;
  border-radius: 20px;
  font-size: 14px;
  font-weight: 500;
  font-family: var(--font-sans);
  cursor: pointer;
  transition: all var(--transition);
  display: flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}

.btn-primary:hover:not(:disabled) {
  background: var(--color-accent-hover);
  transform: scale(1.02);
}

.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-secondary {
  height: 40px;
  padding: 0 20px;
  background: var(--color-bg);
  color: var(--color-text-primary);
  border: 1px solid rgba(0, 0, 0, 0.12);
  border-radius: 20px;
  font-size: 14px;
  font-weight: 500;
  font-family: var(--font-sans);
  cursor: pointer;
  transition: all var(--transition);
  display: flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}

.btn-secondary:hover:not(:disabled) {
  background: rgba(0, 0, 0, 0.06);
}

.btn-secondary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-pool {
  height: 40px;
  padding: 0 16px;
  background: rgba(175, 82, 222, 0.1);
  color: #af52de;
  border: 1px solid rgba(175, 82, 222, 0.3);
  border-radius: 20px;
  font-size: 14px;
  font-weight: 500;
  font-family: var(--font-sans);
  cursor: pointer;
  transition: all var(--transition);
  display: flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}

.btn-pool:hover:not(:disabled) {
  background: rgba(175, 82, 222, 0.18);
}

.btn-pool:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.progress-bar-wrap {
  padding: 12px 20px;
  background: rgba(0, 113, 227, 0.06);
  border-radius: var(--radius-md);
}

.progress-info {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  color: var(--color-text-secondary);
  margin-bottom: 6px;
}

.progress-errors {
  color: var(--color-red);
}

.progress-eta {
  color: var(--color-muted);
  font-size: 12px;
}

.progress-bar {
  height: 6px;
  background: rgba(0, 113, 227, 0.15);
  border-radius: 3px;
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: var(--color-accent);
  border-radius: 3px;
  transition: width 0.3s ease;
}

.spinner {
  width: 14px;
  height: 14px;
  border: 2px solid rgba(255, 255, 255, 0.3);
  border-top-color: white;
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
}

.spinner.dark {
  border-color: rgba(0, 0, 0, 0.15);
  border-top-color: var(--color-text-primary);
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.sync-msg {
  margin-top: 12px;
  font-size: 13px;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
}

.sync-msg.success {
  background: rgba(52, 199, 89, 0.08);
  color: #34c759;
}

.sync-msg.error {
  background: rgba(255, 59, 48, 0.08);
  color: var(--color-red);
}

.card-header {
  padding: 20px 24px 16px;
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.card-header h2 {
  font-size: 20px;
  font-weight: 600;
  letter-spacing: -0.3px;
}

.card-header-hint {
  font-size: 13px;
  color: var(--color-text-tertiary);
}

.btn-small {
  padding: 4px 14px;
  font-size: 12px;
  font-weight: 500;
  font-family: var(--font-sans);
  color: var(--color-accent);
  background: var(--color-blue-bg);
  border: none;
  border-radius: 12px;
  cursor: pointer;
  transition: all var(--transition);
}

.btn-small:hover {
  background: rgba(0, 113, 227, 0.15);
}

.btn-small:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.empty-state {
  padding: 48px 24px;
  text-align: center;
  color: var(--color-text-secondary);
}

.empty-hint {
  font-size: 13px;
  color: var(--color-text-tertiary);
  margin-top: 4px;
}

.table-wrap {
  overflow-x: auto;
}

table {
  width: 100%;
  border-collapse: collapse;
}

th {
  padding: 10px 24px;
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
  padding: 12px 24px;
  font-size: 14px;
  border-bottom: 0.5px solid var(--color-border);
}

tbody tr {
  transition: background var(--transition);
}

tbody tr:hover {
  background: rgba(0, 0, 0, 0.02);
}

tbody tr:last-child td {
  border-bottom: none;
}

.code-badge {
  font-family: var(--font-mono);
  font-size: 13px;
  font-weight: 500;
  padding: 2px 8px;
  background: var(--color-bg);
  border-radius: 6px;
}

.td-name {
  font-weight: 500;
}

.td-muted {
  color: var(--color-text-secondary);
  font-size: 13px;
}

.td-date {
  color: #34c759;
  font-size: 13px;
  font-weight: 500;
}

.exchange-tag {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 4px;
  text-transform: uppercase;
}

.exchange-tag.sh {
  background: rgba(0, 113, 227, 0.08);
  color: #0071e3;
}

.exchange-tag.sz {
  background: rgba(175, 82, 222, 0.08);
  color: #af52de;
}
</style>
