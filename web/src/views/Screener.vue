<template>
  <div class="screener">
    <div class="page-header">
      <h1>选股中心</h1>
      <p class="page-desc">使用量化策略从股票池中筛选目标股票</p>
    </div>

    <div class="card form-card">
      <div class="form-grid">
        <div class="form-group">
          <label>选股策略</label>
          <div class="select-wrap">
            <select v-model="form.screener">
              <option v-for="s in screeners" :key="s.name" :value="s.name">
                {{ s.description || s.name }}
              </option>
            </select>
          </div>
        </div>
        <div class="form-group">
          <label>股票池</label>
          <input v-model="form.codes" placeholder="留空=全部已同步股票" />
        </div>
        <div class="form-group">
          <label>截止日期</label>
          <input v-model="form.end" type="date" />
        </div>
        <div class="form-group">
          <label>Top N</label>
          <input v-model.number="form.topN" type="number" min="1" max="100" />
        </div>
        <div class="form-group form-action">
          <button class="btn-primary" @click="runScreener" :disabled="loading">
            <span v-if="loading" class="spinner" />
            {{ loading ? "筛选中（首次可能需同步数据）..." : "运行选股" }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="error" class="alert alert-error">
      <svg width="18" height="18" viewBox="0 0 18 18" fill="none">
        <circle cx="9" cy="9" r="8" stroke="currentColor" stroke-width="1.5" />
        <path d="M9 5.5v4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
        <circle cx="9" cy="12.5" r="0.75" fill="currentColor" />
      </svg>
      <span>{{ error }}</span>
    </div>

    <template v-if="result">
      <div class="result-header">
        <h2>筛选结果</h2>
        <span class="scan-info">共扫描 {{ result.total_scanned }} 只，命中 {{ result.results.length }} 只</span>
      </div>

      <div v-if="result.results.length === 0" class="card empty-card">
        <p>未找到符合条件的股票</p>
      </div>

      <div v-else class="card">
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>排名</th>
                <th>代码</th>
                <th>名称</th>
                <th>评分</th>
                <th>最新价</th>
                <th>涨跌幅</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(item, i) in result.results" :key="item.code">
                <td>
                  <span class="rank" :class="rankClass(i)">{{ i + 1 }}</span>
                </td>
                <td><span class="code-badge">{{ item.code }}</span></td>
                <td class="td-name">{{ item.name }}</td>
                <td>
                  <div class="score-bar-wrap">
                    <div class="score-bar" :style="{ width: item.score * 100 + '%' }" />
                    <span class="score-text">{{ (item.score * 100).toFixed(0) }}</span>
                  </div>
                </td>
                <td class="td-mono">{{ item.close.toFixed(2) }}</td>
                <td :class="item.change_pct >= 0 ? 'text-red' : 'text-green'">
                  {{ item.change_pct >= 0 ? "+" : "" }}{{ item.change_pct.toFixed(2) }}%
                </td>
                <td>
                  <button class="btn-small" @click="goBacktest(item.code)">回测</button>
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
import { ref, onMounted } from "vue";
import { useRouter } from "vue-router";
import {
  fetchScreeners,
  runScreen,
  type ScreenerInfo,
  type ScreenResponse,
} from "../api/client";

const router = useRouter();

const screeners = ref<ScreenerInfo[]>([]);
const form = ref({
  screener: "",
  codes: "",
  end: new Date().toISOString().slice(0, 10),
  topN: 20,
});
const loading = ref(false);
const error = ref("");
const result = ref<ScreenResponse | null>(null);

onMounted(async () => {
  try {
    const res = await fetchScreeners();
    screeners.value = res.data;
    if (screeners.value.length > 0 && screeners.value[0]) {
      form.value.screener = screeners.value[0].name;
    }
  } catch (e) {
    console.error("Failed to fetch screeners:", e);
  }
});

function rankClass(index: number): string {
  if (index === 0) return "gold";
  if (index === 1) return "silver";
  if (index === 2) return "bronze";
  return "";
}

async function runScreener() {
  loading.value = true;
  error.value = "";
  result.value = null;
  try {
    const codes = form.value.codes
      ? form.value.codes.split(",").map((s) => s.trim())
      : [];
    const res = await runScreen({
      screener_name: form.value.screener,
      codes,
      end: form.value.end,
      top_n: form.value.topN,
    });
    if ("error" in res.data) {
      error.value = (res.data as unknown as { error: string }).error;
    } else {
      result.value = res.data;
    }
  } catch (e) {
    error.value = "选股请求失败，请检查服务是否正常运行";
    console.error("Screen failed:", e);
  } finally {
    loading.value = false;
  }
}

function goBacktest(code: string) {
  router.push({ path: "/backtest", query: { code } });
}
</script>

<style scoped>
.screener {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.page-header h1 {
  font-size: 32px;
  font-weight: 700;
  letter-spacing: -0.5px;
}

.page-desc {
  margin-top: 4px;
  font-size: 15px;
  color: var(--color-text-secondary);
}

.form-card {
  background: var(--color-surface);
  border-radius: var(--radius-lg);
  padding: 24px;
  box-shadow: var(--shadow-sm);
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
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
  border-left: 5px solid transparent;
  border-right: 5px solid transparent;
  border-top: 5px solid var(--color-text-tertiary);
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

.alert {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 18px;
  border-radius: var(--radius-md);
  font-size: 14px;
}

.alert-error {
  background: rgba(255, 59, 48, 0.08);
  color: var(--color-red);
}

.result-header {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-top: 8px;
}

.result-header h2 {
  font-size: 24px;
  font-weight: 700;
  letter-spacing: -0.3px;
}

.scan-info {
  font-size: 14px;
  color: var(--color-text-secondary);
}

.card {
  background: var(--color-surface);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  overflow: hidden;
}

.empty-card {
  padding: 40px;
  text-align: center;
  color: var(--color-text-secondary);
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
  padding: 12px 24px;
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

.rank {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  font-size: 13px;
  font-weight: 600;
  background: var(--color-bg);
  color: var(--color-text-secondary);
}

.rank.gold {
  background: linear-gradient(135deg, #ffd700, #ffb800);
  color: white;
}

.rank.silver {
  background: linear-gradient(135deg, #c0c0c0, #a8a8a8);
  color: white;
}

.rank.bronze {
  background: linear-gradient(135deg, #cd7f32, #b87333);
  color: white;
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

.td-mono {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
}

.text-red {
  color: var(--color-red);
  font-weight: 500;
}

.text-green {
  color: #34c759;
  font-weight: 500;
}

.score-bar-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 100px;
}

.score-bar {
  height: 6px;
  border-radius: 3px;
  background: linear-gradient(90deg, var(--color-accent), #40a9ff);
  transition: width 0.3s ease;
}

.score-text {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-accent);
  min-width: 24px;
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
</style>
