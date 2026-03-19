<template>
  <div class="dashboard">
    <h1>Quanti 仪表盘</h1>

    <div class="stats-grid">
      <div class="stat-card">
        <div class="stat-label">股票数量</div>
        <div class="stat-value">{{ stocks.length }}</div>
      </div>
    </div>

    <div class="stock-list">
      <h2>股票列表</h2>
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
            <td>{{ stock.code }}</td>
            <td>{{ stock.name }}</td>
            <td>{{ stock.exchange }}</td>
            <td>{{ stock.industry }}</td>
            <td>{{ stock.list_date }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue";
import { fetchStocks, type StockInfo } from "../api/client";

const stocks = ref<StockInfo[]>([]);

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
  padding: 20px;
}
.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
  margin-bottom: 24px;
}
.stat-card {
  background: #f8f9fa;
  border-radius: 8px;
  padding: 16px;
}
.stat-label {
  color: #666;
  font-size: 14px;
}
.stat-value {
  font-size: 32px;
  font-weight: bold;
  color: #333;
}
table {
  width: 100%;
  border-collapse: collapse;
}
th,
td {
  padding: 8px 12px;
  border-bottom: 1px solid #eee;
  text-align: left;
}
th {
  background: #f5f5f5;
  font-weight: 600;
}
</style>
