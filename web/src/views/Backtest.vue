<template>
  <div class="backtest">
    <div class="page-header">
      <h1>回测中心</h1>
      <p class="page-desc">配置策略参数，运行历史回测分析</p>
    </div>

    <div class="card form-card">
      <div class="form-grid">
        <div class="form-group">
          <label>股票代码</label>
          <input v-model="form.codes" placeholder="000001, 600519" />
        </div>
        <div class="form-group">
          <label>策略</label>
          <div class="select-wrap">
            <select v-model="form.strategy">
              <option value="ma_cross">均线交叉</option>
              <option value="macd_cross">MACD交叉</option>
              <option value="rsi_ob_os">RSI超买超卖</option>
              <option value="bollinger_band">布林带突破</option>
              <option value="ma_volume">均线+放量</option>
              <option value="turtle_breakout">海龟突破</option>
            </select>
          </div>
        </div>
        <div class="form-group">
          <label>起始日期</label>
          <input v-model="form.start" type="date" />
        </div>
        <div class="form-group">
          <label>结束日期</label>
          <input v-model="form.end" type="date" />
        </div>
        <div class="form-group">
          <label>初始资金</label>
          <div class="input-suffix-wrap">
            <input v-model.number="form.cash" type="number" />
            <span class="input-suffix">CNY</span>
          </div>
        </div>
        <div class="form-group form-action">
          <button class="btn-primary" @click="runTest" :disabled="loading">
            <span v-if="loading" class="spinner" />
            {{ loading ? "运行中..." : "运行回测" }}
          </button>
        </div>
      </div>
    </div>

    <!-- Error -->
    <div v-if="error" class="alert alert-error">
      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
        <circle cx="9" cy="9" r="8" stroke="currentColor" stroke-width="1.5" />
        <path d="M9 5.5v4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
        <circle cx="9" cy="12.5" r="0.75" fill="currentColor" />
      </svg>
      <span>{{ error }}</span>
      <button v-if="showSync" class="btn-sync" @click="syncData" :disabled="syncing">
        {{ syncing ? "同步中..." : "同步数据" }}
      </button>
    </div>

    <!-- Success -->
    <div v-if="syncMsg" class="alert alert-success">
      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
        <circle cx="9" cy="9" r="8" stroke="currentColor" stroke-width="1.5" />
        <path d="M5.5 9.5l2 2 5-5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
      <span>{{ syncMsg }}</span>
    </div>

    <!-- Halted: the portfolio circuit breaker stopped strategy trading -->
    <div v-if="result && result.halted" class="alert alert-danger">
      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
        <path d="M9 2l7.5 13H1.5L9 2z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" />
        <path d="M9 7.5v3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
        <circle cx="9" cy="13" r="0.75" fill="currentColor" />
      </svg>
      <span>⚠️ {{ result.halted_reason || "组合回撤熔断" }}：{{ result.halted_at }} 起引擎停摆——熔断后段是现金/残仓漂移，不是策略在交易，OOS 指标请勿跨过熔断点解读。</span>
    </div>

    <!-- Warning -->
    <div v-if="result && result.warning" class="alert alert-warning">
      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
        <path d="M9 2l7.5 13H1.5L9 2z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" />
        <path d="M9 7.5v3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
        <circle cx="9" cy="13" r="0.75" fill="currentColor" />
      </svg>
      <span>{{ result.warning }}</span>
    </div>

    <!-- Results -->
    <template v-if="result && Object.keys(result.metrics).length > 0">
      <div class="section-title">
        <h2>回测结果</h2>
      </div>

      <div class="metrics-grid">
        <div
          class="metric-card"
          v-for="(value, key) in result.metrics"
          :key="key"
          :class="metricCardClass(key as string, value as number)"
        >
          <span class="metric-label">{{ metricLabel(key as string) }}</span>
          <span class="metric-value">
            {{ formatMetric(key as string, value as number) }}
          </span>
        </div>
      </div>

      <div class="card chart-card">
        <div class="card-header">
          <h3>净值曲线</h3>
        </div>
        <div class="chart-container">
          <v-chart :option="chartOption" autoresize style="height: 360px" />
        </div>
      </div>

      <div class="card trades-card">
        <div class="card-header">
          <h3>交易记录</h3>
          <span class="trade-count">{{ result.trades.length }} 笔</span>
        </div>
        <div v-if="result.trades.length === 0" class="empty-state">
          <p>该区间无交易记录</p>
        </div>
        <div v-else class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>日期</th>
                <th>股票</th>
                <th>方向</th>
                <th>数量</th>
                <th>价格</th>
                <th>手续费</th>
                <th>离场原因</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(trade, i) in result.trades" :key="i">
                <td class="td-muted">{{ trade.date }}</td>
                <td><span class="code-badge">{{ trade.stock_code }}</span></td>
                <td>
                  <span class="direction-tag" :class="trade.direction">
                    {{ trade.direction === "buy" ? "买入" : "卖出" }}
                  </span>
                </td>
                <td class="td-mono">{{ trade.quantity.toLocaleString() }}</td>
                <td class="td-mono">{{ trade.price.toFixed(2) }}</td>
                <td class="td-mono td-muted">{{ trade.commission.toFixed(2) }}</td>
                <td class="td-muted exit-reason" :title="trade.reason || ''">
                  {{ trade.direction === "sell" ? (trade.reason || "—") : "" }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from "vue";
import { useRoute } from "vue-router";
import VChart from "vue-echarts";
import { use } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { LineChart } from "echarts/charts";
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
} from "echarts/components";
import { runBacktest, syncQuotes, type BacktestResult } from "../api/client";

use([CanvasRenderer, LineChart, GridComponent, TooltipComponent, LegendComponent]);

const route = useRoute();

const form = ref({
  codes: (route.query.code as string) || "000001",
  strategy: "ma_cross",
  start: "2024-01-01",
  end: "2024-12-31",
  cash: 100000,
});

const loading = ref(false);
const result = ref<BacktestResult | null>(null);
const error = ref("");
const showSync = ref(false);
const syncing = ref(false);
const syncMsg = ref("");

const chartOption = computed(() => {
  if (!result.value) return {};
  const dates = Object.keys(result.value.equity_curve);
  const values = Object.values(result.value.equity_curve);
  const minVal = Math.min(...values);
  const maxVal = Math.max(...values);
  const padding = Math.max((maxVal - minVal) * 0.15, maxVal * 0.02);
  const yMin = Math.floor((minVal - padding) / 1000) * 1000;
  const yMax = Math.ceil((maxVal + padding) / 1000) * 1000;
  return {
    tooltip: {
      trigger: "axis",
      backgroundColor: "rgba(255,255,255,0.96)",
      borderColor: "rgba(0,0,0,0.08)",
      borderWidth: 1,
      textStyle: { color: "#1d1d1f", fontSize: 13 },
      shadowBlur: 12,
      shadowColor: "rgba(0,0,0,0.08)",
      formatter: (params: unknown) => {
        const arr = params as { axisValue: string; value: number }[];
        if (!arr || !arr[0]) return "";
        const p = arr[0];
        const val = Number(p.value);
        const base = values[0] ?? val;
        const ret = ((val - base) / base * 100).toFixed(2);
        const color = val >= base ? "#ff3b30" : "#34c759";
        return `<div style="font-size:12px;color:#86868b">${p.axisValue}</div>
          <div style="font-size:15px;font-weight:600;margin-top:4px">${val.toLocaleString("zh-CN", { minimumFractionDigits: 2 })}</div>
          <div style="font-size:13px;color:${color};margin-top:2px">${val >= base ? "+" : ""}${ret}%</div>`;
      },
    },
    grid: { left: 60, right: 24, top: 16, bottom: 32 },
    xAxis: {
      type: "category",
      data: dates,
      axisLine: { lineStyle: { color: "rgba(0,0,0,0.08)" } },
      axisTick: { show: false },
      axisLabel: { color: "#86868b", fontSize: 11 },
    },
    yAxis: {
      type: "value",
      min: yMin,
      max: yMax,
      splitLine: { lineStyle: { color: "rgba(0,0,0,0.04)" } },
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: "#86868b", fontSize: 11 },
    },
    series: [
      {
        name: "组合净值",
        type: "line",
        data: values,
        smooth: 0.3,
        symbol: "none",
        lineStyle: { width: 2.5, color: "#0071e3" },
        areaStyle: {
          color: {
            type: "linear",
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: "rgba(0,113,227,0.15)" },
              { offset: 1, color: "rgba(0,113,227,0.01)" },
            ],
          },
        },
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

function metricCardClass(key: string, value: number): string {
  if (["total_return", "annual_return"].includes(key)) {
    return value >= 0 ? "positive" : "negative";
  }
  if (key === "max_drawdown") return "negative";
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
      error.value = "该股票暂无行情数据";
      showSync.value = true;
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

async function syncData() {
  syncing.value = true;
  syncMsg.value = "";
  try {
    const codes = form.value.codes.split(",").map((s) => s.trim());
    const res = await syncQuotes(codes);
    const total = Object.values(res.data.synced).reduce((a, b) => a + b, 0);
    if (total > 0) {
      syncMsg.value = `同步完成，共获取 ${total} 条行情数据。正在自动运行回测...`;
      error.value = "";
      showSync.value = false;
      setTimeout(() => {
        syncMsg.value = "";
        runTest();
      }, 500);
    } else {
      const errMsgs = Object.entries(res.data.errors || {})
        .map(([code, msg]) => `${code}: ${msg}`)
        .join("；");
      error.value = errMsgs || "未获取到数据，请确认股票代码是否正确（可能是网络超时，请重试）";
    }
  } catch (e) {
    error.value = "数据同步失败，请检查网络连接";
    console.error("Sync failed:", e);
  } finally {
    syncing.value = false;
  }
}
</script>

<style scoped>
.backtest {
  display: flex;
  flex-direction: column;
  gap: 20px;
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

/* --- Form --- */
.form-card {
  background: var(--color-surface);
  border-radius: var(--radius-lg);
  padding: 24px;
  box-shadow: var(--shadow-sm);
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 16px;
  align-items: end;
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

.form-group input,
.form-group select {
  height: 40px;
  padding: 0 12px;
  border: 1px solid rgba(0, 0, 0, 0.12);
  border-radius: var(--radius-sm);
  font-size: 15px;
  font-family: var(--font-sans);
  color: var(--color-text-primary);
  background: var(--color-surface);
  outline: none;
  transition: all var(--transition);
  -webkit-appearance: none;
}

.form-group input:focus,
.form-group select:focus {
  border-color: var(--color-accent);
  box-shadow: 0 0 0 3px rgba(0, 113, 227, 0.15);
}

.form-group input::placeholder {
  color: var(--color-text-tertiary);
}

.select-wrap {
  position: relative;
}

.select-wrap select {
  width: 100%;
  padding-right: 32px;
  cursor: pointer;
}

.select-wrap::after {
  content: "";
  position: absolute;
  right: 12px;
  top: 50%;
  transform: translateY(-50%);
  width: 0;
  height: 0;
  border-left: 5px solid transparent;
  border-right: 5px solid transparent;
  border-top: 5px solid var(--color-text-tertiary);
  pointer-events: none;
}

.input-suffix-wrap {
  position: relative;
}

.input-suffix-wrap input {
  width: 100%;
  padding-right: 48px;
}

.input-suffix {
  position: absolute;
  right: 12px;
  top: 50%;
  transform: translateY(-50%);
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-tertiary);
  pointer-events: none;
}

.form-action {
  justify-content: flex-end;
}

.btn-primary {
  height: 40px;
  padding: 0 24px;
  background: var(--color-accent);
  color: white;
  border: none;
  border-radius: 20px;
  font-size: 15px;
  font-weight: 500;
  font-family: var(--font-sans);
  cursor: pointer;
  transition: all var(--transition);
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  white-space: nowrap;
}

.btn-primary:hover:not(:disabled) {
  background: var(--color-accent-hover);
  transform: scale(1.02);
}

.btn-primary:active:not(:disabled) {
  transform: scale(0.98);
}

.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.spinner {
  width: 16px;
  height: 16px;
  border: 2px solid rgba(255, 255, 255, 0.3);
  border-top-color: white;
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* --- Alerts --- */
.alert {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 18px;
  border-radius: var(--radius-md);
  font-size: 14px;
  font-weight: 450;
}

.alert-error {
  background: rgba(255, 59, 48, 0.08);
  color: var(--color-red);
}

.alert-success {
  background: rgba(52, 199, 89, 0.08);
  color: var(--color-green);
}

.alert-warning {
  background: rgba(255, 149, 0, 0.08);
  color: var(--color-orange);
}

.alert-danger {
  background: rgba(192, 57, 43, 0.08);
  color: #c0392b;
}

.alert svg {
  flex-shrink: 0;
}

.btn-sync {
  margin-left: auto;
  padding: 6px 16px;
  background: var(--color-red);
  color: white;
  border: none;
  border-radius: 16px;
  font-size: 13px;
  font-weight: 500;
  font-family: var(--font-sans);
  cursor: pointer;
  white-space: nowrap;
  transition: all var(--transition);
}

.btn-sync:hover:not(:disabled) {
  opacity: 0.85;
}

.btn-sync:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* --- Section Title --- */
.section-title {
  margin-top: 8px;
}

.section-title h2 {
  font-size: 24px;
  font-weight: 700;
  letter-spacing: -0.3px;
}

/* --- Metrics --- */
.metrics-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 12px;
}

.metric-card {
  background: var(--color-surface);
  border-radius: var(--radius-md);
  padding: 16px;
  box-shadow: var(--shadow-sm);
  transition: all var(--transition);
}

.metric-card:hover {
  box-shadow: var(--shadow-md);
  transform: translateY(-1px);
}

.metric-card.positive .metric-value {
  color: var(--color-red);
}

.metric-card.negative .metric-value {
  color: var(--color-green);
}

.metric-label {
  display: block;
  font-size: 12px;
  font-weight: 500;
  color: var(--color-text-tertiary);
  margin-bottom: 4px;
}

.metric-value {
  font-size: 22px;
  font-weight: 700;
  letter-spacing: -0.5px;
  color: var(--color-text-primary);
}

/* --- Chart --- */
.card {
  background: var(--color-surface);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  overflow: hidden;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 20px 24px 12px;
}

.card-header h3 {
  font-size: 17px;
  font-weight: 600;
  letter-spacing: -0.2px;
}

.chart-card .chart-container {
  padding: 0 12px 16px;
}

.trade-count {
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text-tertiary);
  background: var(--color-bg);
  padding: 4px 10px;
  border-radius: 12px;
}

/* --- Table --- */
.empty-state {
  padding: 40px 24px;
  text-align: center;
  color: var(--color-text-secondary);
  font-size: 14px;
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
  padding: 11px 24px;
  font-size: 14px;
  border-bottom: 0.5px solid var(--color-border);
}

tbody tr {
  transition: background var(--transition);
}

tbody tr:hover {
  background: rgba(0, 0, 0, 0.015);
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

.td-mono {
  font-family: var(--font-mono);
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}

.td-muted {
  color: var(--color-text-secondary);
}

.direction-tag {
  display: inline-block;
  font-size: 12px;
  font-weight: 600;
  padding: 2px 10px;
  border-radius: 10px;
}

.direction-tag.buy {
  background: rgba(255, 59, 48, 0.08);
  color: var(--color-red);
}

.direction-tag.sell {
  background: rgba(52, 199, 89, 0.08);
  color: var(--color-green);
}
</style>
