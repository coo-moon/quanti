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
          <span class="stat-label">股票数量</span>
          <span class="stat-value">{{ stocks.length }}</span>
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

    <div class="card">
      <div class="card-header">
        <h2>股票列表</h2>
      </div>
      <div v-if="stocks.length === 0" class="empty-state">
        <p>暂无股票数据</p>
        <p class="empty-hint">在回测中心同步股票数据后会自动添加</p>
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
            </tr>
          </thead>
          <tbody>
            <tr v-for="stock in stocks" :key="stock.code">
              <td><span class="code-badge">{{ stock.code }}</span></td>
              <td class="td-name">{{ stock.name }}</td>
              <td><span class="exchange-tag" :class="stock.exchange.toLowerCase()">{{ stock.exchange }}</span></td>
              <td>{{ stock.industry || '-' }}</td>
              <td class="td-muted">{{ stock.list_date }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { fetchStocks, type StockInfo } from "../api/client";

const stocks = ref<StockInfo[]>([]);

const lastUpdate = computed(() => {
  return new Date().toLocaleDateString("zh-CN");
});

onMounted(async () => {
  try {
    const res = await fetchStocks();
    stocks.value = res.data;
  } catch (e) {
    console.error("Failed to fetch stocks:", e);
  }
});
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

.card-header {
  padding: 20px 24px 16px;
}

.card-header h2 {
  font-size: 20px;
  font-weight: 600;
  letter-spacing: -0.3px;
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
