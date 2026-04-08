<template>
  <div class="pool-page">
    <div class="page-header">
      <h1>股票池管理</h1>
      <p class="page-desc">创建和管理自定义股票池</p>
    </div>

    <div class="pool-layout">
      <!-- Left: Pool List -->
      <div class="pool-sidebar">
        <div class="sidebar-header">
          <span class="sidebar-title">我的股票池</span>
          <button class="btn-add-pool" @click="showCreateModal = true" title="创建新股票池">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
            </svg>
          </button>
        </div>

        <div v-if="pools.length === 0" class="sidebar-empty">
          <p>暂无股票池</p>
          <p class="empty-hint">点击 + 创建第一个股票池</p>
        </div>

        <div v-else class="pool-list">
          <div
            v-for="pool in pools"
            :key="pool.name"
            class="pool-item"
            :class="{ active: selectedPool === pool.name }"
            @click="selectPool(pool.name)"
          >
            <div class="pool-item-info">
              <span class="pool-name">{{ pool.name }}</span>
              <span class="pool-count">{{ pool.stock_count }} 只</span>
            </div>
            <button
              class="btn-delete-pool"
              @click.stop="confirmDeletePool(pool.name)"
              title="删除股票池"
            >
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <path d="M2 3.5h10M5.5 3.5V2.5a1 1 0 011-1h1a1 1 0 011 1v1M3.5 3.5l.5 8a1 1 0 001 1h4a1 1 0 001-1l.5-8" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" />
              </svg>
            </button>
          </div>
        </div>

        <!-- Sync All A-Stocks -->
        <div class="sidebar-footer">
          <button class="btn-sync-all" @click="syncAllAStocks" :disabled="syncingAll">
            <span v-if="syncingAll" class="spinner dark" />
            <svg v-else width="14" height="14" viewBox="0 0 14 14" fill="none">
              <path d="M1 7a6 6 0 0111.5-1.5M13 7a6 6 0 01-11.5 1.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
              <path d="M12 2v3h-3M2 12V9h3" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            {{ syncingAll ? "同步中..." : "同步全A股" }}
          </button>
        </div>
      </div>

      <!-- Right: Pool Detail -->
      <div class="pool-detail">
        <div v-if="!selectedPool" class="detail-empty">
          <p>选择一个股票池</p>
          <p class="empty-hint">或创建一个新的股票池开始</p>
        </div>

        <template v-else>
          <div class="detail-header">
            <div class="detail-title-row">
              <h2>{{ selectedPool }}</h2>
              <span class="detail-count">{{ poolStocks.length }} 只股票</span>
            </div>
            <div class="detail-actions">
              <button class="btn-sync-pool" @click="syncPool" :disabled="syncingPool || poolStocks.length === 0">
                <span v-if="syncingPool" class="spinner dark" />
                <svg v-else width="14" height="14" viewBox="0 0 14 14" fill="none">
                  <path d="M1 7a6 6 0 0111.5-1.5M13 7a6 6 0 01-11.5 1.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
                </svg>
                {{ syncingPool ? "同步中..." : "同步数据" }}
              </button>
            </div>
          </div>

          <!-- Add Stocks -->
          <div class="card add-card">
            <div class="add-row">
              <div class="add-input-wrap">
                <svg class="add-icon" width="16" height="16" viewBox="0 0 16 16" fill="none">
                  <circle cx="8" cy="8" r="7" stroke="currentColor" stroke-width="1.5" />
                  <path d="M8 5v6M5 8h6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
                </svg>
                <input
                  v-model="addInput"
                  placeholder="输入股票代码，多个用逗号分隔"
                  @keyup.enter="addStocksToPool"
                />
              </div>
              <button class="btn-primary" @click="addStocksToPool" :disabled="!addInput.trim()">
                添加
              </button>
            </div>
          </div>

          <!-- Sync Message -->
          <div v-if="syncMsg" class="sync-msg" :class="syncError ? 'error' : 'success'">
            {{ syncMsg }}
          </div>

          <!-- Stock Table -->
          <div class="card">
            <div v-if="poolStocks.length === 0" class="empty-state">
              <p>股票池为空</p>
              <p class="empty-hint">在上方添加股票</p>
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
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="stock in poolStocks" :key="stock.code">
                    <td><span class="code-badge">{{ stock.code }}</span></td>
                    <td class="td-name">{{ stock.name }}</td>
                    <td><span class="exchange-tag" :class="stock.exchange.toLowerCase()">{{ stock.exchange }}</span></td>
                    <td>{{ stock.industry || '-' }}</td>
                    <td class="td-muted">{{ stock.list_date }}</td>
                    <td>
                      <button class="btn-remove" @click="removeStock(stock.code)">移除</button>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </template>
      </div>
    </div>

    <!-- Create Pool Modal -->
    <div v-if="showCreateModal" class="modal-overlay" @click.self="showCreateModal = false">
      <div class="modal">
        <div class="modal-header">
          <h3>创建股票池</h3>
          <button class="btn-close" @click="showCreateModal = false">×</button>
        </div>
        <div class="modal-body">
          <div class="form-group">
            <label>股票池名称</label>
            <input v-model="newPoolName" placeholder="例如：我的自选股、消费行业" @keyup.enter="createPool" />
          </div>
        </div>
        <div class="modal-footer">
          <button class="btn-secondary" @click="showCreateModal = false">取消</button>
          <button class="btn-primary" @click="createPool" :disabled="!newPoolName.trim()">创建</button>
        </div>
      </div>
    </div>

    <!-- Delete Confirm Modal -->
    <div v-if="deleteTarget" class="modal-overlay" @click.self="deleteTarget = ''">
      <div class="modal">
        <div class="modal-header">
          <h3>确认删除</h3>
          <button class="btn-close" @click="deleteTarget = ''">×</button>
        </div>
        <div class="modal-body">
          <p>确定要删除股票池 <strong>{{ deleteTarget }}</strong> 吗？此操作不可恢复。</p>
        </div>
        <div class="modal-footer">
          <button class="btn-secondary" @click="deleteTarget = ''">取消</button>
          <button class="btn-danger" @click="doDeletePool">删除</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue";
import {
  fetchPools,
  createPool as apiCreatePool,
  deletePool as apiDeletePool,
  fetchPoolStocks,
  addPoolStocks,
  removePoolStocks,
  syncPoolStocks,
  syncStockList,
  type StockInfo,
} from "../api/client";

const pools = ref<{ name: string; created_at: string; description: string; stock_count: number }[]>([]);
const selectedPool = ref("");
const poolStocks = ref<StockInfo[]>([]);
const addInput = ref("");
const syncMsg = ref("");
const syncError = ref(false);
const syncingPool = ref(false);
const syncingAll = ref(false);

// Modals
const showCreateModal = ref(false);
const newPoolName = ref("");
const deleteTarget = ref("");

onMounted(async () => {
  await loadPools();
});

async function loadPools() {
  try {
    const res = await fetchPools();
    pools.value = res.data;
    if (pools.value.length > 0 && !selectedPool.value) {
      await selectPool(pools.value[0]!.name);
    }
  } catch (e) {
    console.error("Failed to fetch pools:", e);
  }
}

async function selectPool(name: string) {
  selectedPool.value = name;
  syncMsg.value = "";
  try {
    const res = await fetchPoolStocks(name);
    poolStocks.value = res.data;
  } catch (e) {
    console.error("Failed to fetch pool stocks:", e);
  }
}

async function createPool() {
  if (!newPoolName.value.trim()) return;
  try {
    await apiCreatePool({ name: newPoolName.value.trim(), description: "" });
    newPoolName.value = "";
    showCreateModal.value = false;
    await loadPools();
    await selectPool(pools.value[0]?.name || "");
  } catch (e) {
    console.error("Failed to create pool:", e);
  }
}

function confirmDeletePool(name: string) {
  deleteTarget.value = name;
}

async function doDeletePool() {
  if (!deleteTarget.value) return;
  try {
    await apiDeletePool(deleteTarget.value);
    deleteTarget.value = "";
    if (selectedPool.value) {
      selectedPool.value = "";
      poolStocks.value = [];
    }
    await loadPools();
  } catch (e) {
    console.error("Failed to delete pool:", e);
  }
}

async function addStocksToPool() {
  if (!selectedPool.value || !addInput.value.trim()) return;
  const codes = addInput.value
    .replace(/\s+/g, ",")
    .split(",")
    .map((s) => s.trim())
    .filter((s) => /^\d{6}$/.test(s));
  if (codes.length === 0) {
    syncMsg.value = "请输入有效的6位股票代码";
    syncError.value = true;
    return;
  }
  try {
    const res = await addPoolStocks(selectedPool.value, codes);
    syncMsg.value = res.data.message;
    syncError.value = false;
    addInput.value = "";
    await selectPool(selectedPool.value);
    await loadPools();
  } catch (e) {
    syncMsg.value = "添加失败";
    syncError.value = true;
  }
}

async function removeStock(code: string) {
  if (!selectedPool.value) return;
  try {
    await removePoolStocks(selectedPool.value, [code]);
    await selectPool(selectedPool.value);
    await loadPools();
  } catch (e) {
    console.error("Failed to remove stock:", e);
  }
}

async function syncPool() {
  if (!selectedPool.value || syncingPool.value) return;
  syncingPool.value = true;
  syncMsg.value = "";
  syncError.value = false;
  try {
    const res = await syncPoolStocks(selectedPool.value);
    const ok = Object.values(res.data.synced).filter((n: number) => n > 0).length;
    const err = Object.keys(res.data.errors || {}).length;
    if (err > 0) {
      syncMsg.value = `同步完成，${ok} 只成功，${err} 只失败`;
      syncError.value = true;
    } else {
      syncMsg.value = `同步完成，${ok} 只股票已更新`;
      syncError.value = false;
    }
  } catch (e) {
    syncMsg.value = "同步失败";
    syncError.value = true;
  } finally {
    syncingPool.value = false;
  }
}

async function syncAllAStocks() {
  syncingAll.value = true;
  syncMsg.value = "";
  try {
    const res = await syncStockList();
    syncMsg.value = res.data.message;
    syncError.value = false;
  } catch (e) {
    syncMsg.value = "同步全A股失败";
    syncError.value = true;
  } finally {
    syncingAll.value = false;
  }
}
</script>

<style scoped>
.pool-page {
  display: flex;
  flex-direction: column;
  gap: 20px;
  height: calc(100vh - 120px);
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

.pool-layout {
  display: grid;
  grid-template-columns: 260px 1fr;
  gap: 20px;
  flex: 1;
  min-height: 0;
}

/* Sidebar */
.pool-sidebar {
  background: var(--color-surface);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.sidebar-header {
  padding: 16px 16px 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 0.5px solid var(--color-border);
}

.sidebar-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.btn-add-pool {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: var(--color-accent);
  color: white;
  border: none;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all var(--transition);
}

.btn-add-pool:hover {
  background: var(--color-accent-hover);
  transform: scale(1.1);
}

.sidebar-empty {
  flex: 1;
  padding: 32px 16px;
  text-align: center;
  color: var(--color-text-secondary);
  font-size: 14px;
}

.empty-hint {
  font-size: 12px;
  color: var(--color-text-tertiary);
  margin-top: 4px;
}

.pool-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.pool-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition);
}

.pool-item:hover {
  background: rgba(0, 0, 0, 0.04);
}

.pool-item.active {
  background: var(--color-blue-bg);
}

.pool-item-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.pool-name {
  font-size: 14px;
  font-weight: 500;
  color: var(--color-text-primary);
}

.pool-count {
  font-size: 12px;
  color: var(--color-text-tertiary);
}

.btn-delete-pool {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: transparent;
  border: none;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-tertiary);
  opacity: 0;
  transition: all var(--transition);
}

.pool-item:hover .btn-delete-pool {
  opacity: 1;
}

.btn-delete-pool:hover {
  background: rgba(255, 59, 48, 0.1);
  color: var(--color-red);
}

.sidebar-footer {
  padding: 12px 16px;
  border-top: 0.5px solid var(--color-border);
}

.btn-sync-all {
  width: 100%;
  height: 36px;
  background: rgba(0, 113, 227, 0.08);
  color: var(--color-accent);
  border: none;
  border-radius: var(--radius-md);
  font-size: 13px;
  font-weight: 500;
  font-family: var(--font-sans);
  cursor: pointer;
  transition: all var(--transition);
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}

.btn-sync-all:hover:not(:disabled) {
  background: rgba(0, 113, 227, 0.15);
}

.btn-sync-all:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* Detail */
.pool-detail {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-height: 0;
}

.detail-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: var(--color-text-secondary);
  font-size: 15px;
}

.detail-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 4px;
}

.detail-title-row {
  display: flex;
  align-items: baseline;
  gap: 12px;
}

.detail-title-row h2 {
  font-size: 24px;
  font-weight: 700;
  letter-spacing: -0.3px;
}

.detail-count {
  font-size: 14px;
  color: var(--color-text-tertiary);
}

.detail-actions {
  display: flex;
  gap: 8px;
}

.btn-sync-pool {
  height: 36px;
  padding: 0 16px;
  background: var(--color-blue-bg);
  color: var(--color-accent);
  border: none;
  border-radius: 18px;
  font-size: 13px;
  font-weight: 500;
  font-family: var(--font-sans);
  cursor: pointer;
  transition: all var(--transition);
  display: flex;
  align-items: center;
  gap: 6px;
}

.btn-sync-pool:hover:not(:disabled) {
  background: rgba(0, 113, 227, 0.15);
}

.btn-sync-pool:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* Add Card */
.add-card {
  padding: 16px 20px;
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
  height: 38px;
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
  height: 38px;
  padding: 0 20px;
  background: var(--color-accent);
  color: white;
  border: none;
  border-radius: 19px;
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
}

.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-secondary {
  height: 38px;
  padding: 0 20px;
  background: var(--color-bg);
  color: var(--color-text-primary);
  border: 1px solid rgba(0, 0, 0, 0.12);
  border-radius: 19px;
  font-size: 14px;
  font-weight: 500;
  font-family: var(--font-sans);
  cursor: pointer;
  transition: all var(--transition);
}

.btn-secondary:hover:not(:disabled) {
  background: rgba(0, 0, 0, 0.06);
}

/* Sync Message */
.sync-msg {
  font-size: 13px;
  padding: 8px 16px;
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

/* Card & Table */
.card {
  background: var(--color-surface);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  overflow: hidden;
  flex: 1;
  display: flex;
  flex-direction: column;
}

.table-wrap {
  overflow-x: auto;
  flex: 1;
}

table {
  width: 100%;
  border-collapse: collapse;
}

th {
  padding: 10px 24px;
  font-size: 11px;
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

.btn-remove {
  font-size: 12px;
  font-weight: 500;
  font-family: var(--font-sans);
  color: var(--color-red);
  background: transparent;
  border: none;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 8px;
  transition: all var(--transition);
}

.btn-remove:hover {
  background: rgba(255, 59, 48, 0.08);
}

.empty-state {
  padding: 48px 24px;
  text-align: center;
  color: var(--color-text-secondary);
}

/* Spinner */
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

/* Modal */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.modal {
  background: var(--color-surface);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  width: 400px;
  max-width: 90vw;
  overflow: hidden;
}

.modal-header {
  padding: 20px 24px 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 0.5px solid var(--color-border);
}

.modal-header h3 {
  font-size: 18px;
  font-weight: 600;
}

.btn-close {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: transparent;
  border: none;
  cursor: pointer;
  font-size: 22px;
  color: var(--color-text-tertiary);
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all var(--transition);
}

.btn-close:hover {
  background: rgba(0, 0, 0, 0.06);
  color: var(--color-text-primary);
}

.modal-body {
  padding: 20px 24px;
}

.modal-body p {
  font-size: 14px;
  color: var(--color-text-secondary);
}

.modal-body strong {
  color: var(--color-text-primary);
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.form-group label {
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text-secondary);
}

.form-group input {
  height: 40px;
  padding: 0 12px;
  border: 1px solid rgba(0, 0, 0, 0.12);
  border-radius: var(--radius-sm);
  font-size: 14px;
  font-family: var(--font-sans);
  color: var(--color-text-primary);
  background: var(--color-surface);
  outline: none;
  transition: all var(--transition);
}

.form-group input:focus {
  border-color: var(--color-accent);
  box-shadow: 0 0 0 3px rgba(0, 113, 227, 0.15);
}

.modal-footer {
  padding: 16px 24px;
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  border-top: 0.5px solid var(--color-border);
}

.btn-danger {
  height: 38px;
  padding: 0 20px;
  background: var(--color-red);
  color: white;
  border: none;
  border-radius: 19px;
  font-size: 14px;
  font-weight: 500;
  font-family: var(--font-sans);
  cursor: pointer;
  transition: all var(--transition);
}

.btn-danger:hover {
  background: #e51c10;
}
</style>
