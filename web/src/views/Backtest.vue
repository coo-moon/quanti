<template>
  <div class="backtest">
    <h1>回测中心</h1>

    <div class="backtest-form">
      <div class="form-row">
        <label>股票代码</label>
        <input v-model="form.codes" placeholder="000001,600519" />
      </div>
      <div class="form-row">
        <label>策略</label>
        <select v-model="form.strategy">
          <option value="ma_cross">均线交叉</option>
        </select>
      </div>
      <div class="form-row">
        <label>起始日期</label>
        <input v-model="form.start" type="date" />
      </div>
      <div class="form-row">
        <label>结束日期</label>
        <input v-model="form.end" type="date" />
      </div>
      <div class="form-row">
        <label>初始资金</label>
        <input v-model.number="form.cash" type="number" />
      </div>
      <button @click="runTest" :disabled="loading">
        {{ loading ? "运行中..." : "运行回测" }}
      </button>
    </div>

    <div v-if="error" class="error-msg">{{ error }}</div>

    <div v-if="result && Object.keys(result.metrics).length > 0" class="results">
      <h2>回测结果</h2>

      <div class="metrics-grid">
        <div class="metric" v-for="(value, key) in result.metrics" :key="key">
          <span class="metric-label">{{ metricLabel(key as string) }}</span>
          <span class="metric-value" :class="metricClass(key as string, value as number)">
            {{ formatMetric(key as string, value as number) }}
          </span>
        </div>
      </div>

      <div class="chart-container">
        <v-chart :option="chartOption" autoresize style="height: 400px" />
      </div>

      <h3>交易记录 ({{ result.trades.length }} 笔)</h3>
      <table>
        <thead>
          <tr>
            <th>日期</th>
            <th>股票</th>
            <th>方向</th>
            <th>数量</th>
            <th>价格</th>
            <th>手续费</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(trade, i) in result.trades" :key="i">
            <td>{{ trade.date }}</td>
            <td>{{ trade.stock_code }}</td>
            <td :class="trade.direction === 'buy' ? 'text-red' : 'text-green'">
              {{ trade.direction === "buy" ? "买入" : "卖出" }}
            </td>
            <td>{{ trade.quantity }}</td>
            <td>{{ trade.price.toFixed(2) }}</td>
            <td>{{ trade.commission.toFixed(2) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from "vue";
import VChart from "vue-echarts";
import { use } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { LineChart } from "echarts/charts";
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
} from "echarts/components";
import { runBacktest, type BacktestResult } from "../api/client";

use([CanvasRenderer, LineChart, GridComponent, TooltipComponent, LegendComponent]);

const form = ref({
  codes: "000001",
  strategy: "ma_cross",
  start: "2024-01-01",
  end: "2024-12-31",
  cash: 100000,
});

const loading = ref(false);
const result = ref<BacktestResult | null>(null);
const error = ref("");

const chartOption = computed(() => {
  if (!result.value) return {};
  const dates = Object.keys(result.value.equity_curve);
  const values = Object.values(result.value.equity_curve);
  return {
    tooltip: { trigger: "axis" },
    xAxis: { type: "category", data: dates },
    yAxis: { type: "value", name: "净值 (元)" },
    series: [
      {
        name: "组合净值",
        type: "line",
        data: values,
        smooth: true,
        lineStyle: { width: 2 },
        areaStyle: { opacity: 0.1 },
      },
    ],
  };
});

const metricLabels: Record<string, string> = {
  total_return: "总收益率",
  annual_return: "年化收益率",
  annual_volatility: "年化波动率",
  max_drawdown: "最大回撤",
  sharpe_ratio: "夏普比率",
  sortino_ratio: "索提诺比率",
  calmar_ratio: "卡尔马比率",
  win_rate: "胜率",
  trading_days: "交易天数",
};

function metricLabel(key: string): string {
  return metricLabels[key] || key;
}

function formatMetric(key: string, value: number): string {
  if (["total_return", "annual_return", "annual_volatility", "max_drawdown", "win_rate"].includes(key)) {
    return (value * 100).toFixed(2) + "%";
  }
  if (key === "trading_days") return String(Math.round(value));
  return value.toFixed(2);
}

function metricClass(key: string, value: number): string {
  if (["total_return", "annual_return"].includes(key)) {
    return value >= 0 ? "text-red" : "text-green";
  }
  if (key === "max_drawdown") return "text-green";
  return "";
}

async function runTest() {
  loading.value = true;
  error.value = "";
  result.value = null;
  try {
    const res = await runBacktest({
      strategy_name: form.value.strategy,
      codes: form.value.codes.split(",").map((s) => s.trim()),
      start: form.value.start,
      end: form.value.end,
      initial_cash: form.value.cash,
      params: {},
    });
    const data = res.data;
    if (!data.metrics || Object.keys(data.metrics).length === 0) {
      error.value = "该股票暂无行情数据，请先通过 CLI 同步数据：quanti sync --quotes --codes " + form.value.codes;
    } else {
      result.value = data;
    }
  } catch (e) {
    error.value = "回测请求失败，请检查服务是否正常运行";
    console.error("Backtest failed:", e);
  } finally {
    loading.value = false;
  }
}
</script>

<style scoped>
.backtest {
  padding: 20px;
}
.backtest-form {
  background: #f8f9fa;
  padding: 20px;
  border-radius: 8px;
  margin-bottom: 24px;
}
.form-row {
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  gap: 12px;
}
.form-row label {
  width: 80px;
  font-weight: 600;
}
.form-row input,
.form-row select {
  padding: 6px 10px;
  border: 1px solid #ddd;
  border-radius: 4px;
  font-size: 14px;
}
button {
  padding: 8px 24px;
  background: #1890ff;
  color: white;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  font-size: 14px;
}
button:disabled {
  background: #ccc;
}
.metrics-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
  margin-bottom: 24px;
}
.metric {
  background: #f8f9fa;
  padding: 12px;
  border-radius: 6px;
}
.metric-label {
  display: block;
  color: #666;
  font-size: 12px;
}
.metric-value {
  font-size: 20px;
  font-weight: bold;
}
.text-red {
  color: #cf1322;
}
.text-green {
  color: #389e0d;
}
.chart-container {
  margin-bottom: 24px;
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
}
.error-msg {
  background: #fff2f0;
  border: 1px solid #ffccc7;
  color: #cf1322;
  padding: 12px 16px;
  border-radius: 6px;
  margin-bottom: 20px;
}
</style>
