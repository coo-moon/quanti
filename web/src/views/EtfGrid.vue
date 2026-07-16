<template>
  <div class="etf-grid">
    <div class="page-header">
      <h1>ETF 网格挖掘器</h1>
      <p class="page-desc">挖掘近期适合网格「稳定套现」的 ETF，回测并计算最佳箱体 / 格数。</p>
    </div>

    <!-- 诚实风险横幅 -->
    <div class="alert alert-warning">
      <svg width="18" height="18" viewBox="0 0 16 16" fill="none">
        <path d="M8 1L15 14H1L8 1z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" />
        <path d="M8 6v4M8 11.5v.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
      </svg>
      <span>
        网格交易<b>不产生 alpha</b>：稳定成立的价值只有<b>削减回撤</b>。能否赚钱取决于标的未来是否继续横盘——不可预知。
        下方指标与回测<b>描述过去、非预测未来</b>；实盘务必带破位止损 + 每月重新挖掘。
      </span>
    </div>

    <!-- 数据状态 / 同步 -->
    <div class="card">
      <div class="status-row">
        <div class="status-items">
          <div class="status-item"><span class="k">缓存 ETF</span><span class="v">{{ status?.codes ?? 0 }}</span></div>
          <div class="status-item"><span class="k">数据区间</span><span class="v">{{ status?.start || "—" }} ~ {{ status?.end || "—" }}</span></div>
          <div class="status-item"><span class="k">全集</span><span class="v">{{ status?.universe ?? 0 }} 只</span></div>
        </div>
        <button class="btn-primary" :disabled="syncing || !status?.has_token" @click="doSync">
          <span v-if="syncing" class="spinner" />
          {{ syncing ? `同步中 ${syncCur}/${syncTot}` : "同步ETF数据" }}
        </button>
      </div>
      <div v-if="status && !status.has_token" class="alert alert-error mt">
        <span>未配置 tushare token（基金接口需 2000 积分权限）。请先到「数据源配置」填入 token。</span>
      </div>
    </div>

    <!-- 挖掘 -->
    <div class="card">
      <div class="screen-head">
        <div class="section-title">挖掘（稳定套现候选）</div>
        <div class="screen-ctrl">
          <label class="mini">最低日均额
            <select v-model.number="advMin" class="mini-select">
              <option :value="50000000">0.5亿</option>
              <option :value="100000000">1亿</option>
              <option :value="300000000">3亿</option>
              <option :value="500000000">5亿</option>
            </select>
          </label>
          <button class="btn-primary" :disabled="screening || !status?.codes" @click="doScreen">
            <span v-if="screening" class="spinner" />开始挖掘
          </button>
        </div>
      </div>
      <div v-if="screenError" class="alert alert-error mt">{{ screenError }}</div>
      <div v-if="rows.length" class="table-wrap mt">
        <table>
          <thead>
            <tr>
              <th>#</th><th>代码</th><th>名称</th><th>类别</th>
              <th>ER</th><th>波动</th><th>振幅</th><th>净半年</th><th>穿越</th><th>日均额</th>
              <th>近半年网格</th><th>近半年持有</th><th>网格回撤</th><th>箱体(60日)</th><th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(r, i) in rows" :key="r.code" :class="{ sel: r.code === selected }">
              <td><span class="rank" :class="rankCls(i)">{{ i + 1 }}</span></td>
              <td><span class="code-badge">{{ r.code }}</span></td>
              <td>{{ r.name }}<span v-if="r.t0" class="t0-tag">T+0</span></td>
              <td class="td-muted">{{ r.category }}</td>
              <td class="td-mono">{{ r.er.toFixed(3) }}</td>
              <td class="td-mono">{{ pct(r.vol) }}</td>
              <td class="td-mono">{{ pct(r.amp) }}</td>
              <td class="td-mono" :class="sign(r.net)">{{ pct(r.net) }}</td>
              <td class="td-mono">{{ r.rev.toFixed(1) }}</td>
              <td class="td-mono">{{ (r.adv / 1e8).toFixed(1) }}亿</td>
              <td class="td-mono" :class="sign(r.grid_ret)">{{ pct(r.grid_ret) }}</td>
              <td class="td-mono" :class="sign(r.hold_ret)">{{ pct(r.hold_ret) }}</td>
              <td class="td-mono">{{ pct(r.grid_dd) }}</td>
              <td class="td-mono td-muted">{{ r.box_lo }}~{{ r.box_hi }}</td>
              <td>
                <button class="btn-small" @click="pick(r.code)">回测/优化</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else-if="screened && !screenError" class="td-muted mt">无达标标的（放宽日均额或先同步数据）。</p>
    </div>

    <!-- 回测 + 优化 -->
    <template v-if="selected">
      <div class="card">
        <div class="section-title">回测 · {{ selectedName }} <span class="code-badge">{{ selected }}</span></div>
        <div class="form-grid mt">
          <div class="form-group"><label>格数 N</label><input type="number" v-model.number="bt.N" min="4" max="30" /></div>
          <div class="form-group"><label>箱体回看(日)</label><input type="number" v-model.number="bt.lookback" min="20" max="120" /></div>
          <div class="form-group">
            <label>箱体重设</label>
            <div class="select-wrap"><select v-model.number="bt.rebal">
              <option :value="0">固定(更稳健)</option><option :value="20">每20日</option><option :value="40">每40日</option>
            </select></div>
          </div>
          <div class="form-group">
            <label>间距</label>
            <div class="select-wrap"><select v-model="bt.geom">
              <option :value="false">等差</option><option :value="true">等比</option>
            </select></div>
          </div>
          <div class="form-group">
            <label>裁极端影线</label>
            <div class="select-wrap"><select v-model="bt.trim">
              <option :value="true">是</option><option :value="false">否</option>
            </select></div>
          </div>
          <div class="form-group form-actions">
            <button class="btn-primary" :disabled="btLoading" @click="doBacktest">
              <span v-if="btLoading" class="spinner" />回测
            </button>
            <button class="btn-primary alt" :disabled="optLoading" @click="doOptimize">
              <span v-if="optLoading" class="spinner" />参数优化
            </button>
          </div>
        </div>

        <template v-if="btResult && !btResult.error">
          <div class="metrics-grid mt">
            <div class="metric-card" :class="signCard(btResult.grid_ret)">
              <div class="metric-label">网格收益</div><div class="metric-value">{{ pct(btResult.grid_ret) }}</div></div>
            <div class="metric-card" :class="signCard(btResult.hold_ret)">
              <div class="metric-label">买入持有</div><div class="metric-value">{{ pct(btResult.hold_ret) }}</div></div>
            <div class="metric-card"><div class="metric-label">网格最大回撤</div><div class="metric-value">{{ pct(btResult.grid_dd) }}</div></div>
            <div class="metric-card"><div class="metric-label">持有最大回撤</div><div class="metric-value">{{ pct(btResult.hold_dd) }}</div></div>
            <div class="metric-card"><div class="metric-label">交易次数</div><div class="metric-value">{{ btResult.trades }}</div></div>
          </div>
          <div class="card chart-card mt">
            <v-chart :option="chartOption" autoresize style="height: 340px" />
          </div>
          <div class="deploy mt">
            <div class="deploy-title">可落地网格（原始价）</div>
            <div class="deploy-grid">
              <div><span class="k">箱体</span><span class="v">{{ btResult.deploy.box_lo }} ~ {{ btResult.deploy.box_hi }}</span></div>
              <div><span class="k">现价</span><span class="v">{{ btResult.deploy.price }}</span></div>
              <div><span class="k">格数</span><span class="v">{{ btResult.deploy.grids }}</span></div>
              <div><span class="k">步长</span><span class="v">{{ btResult.deploy.step }} 元 ({{ btResult.deploy.step_pct }}%/格)</span></div>
              <div><span class="k">破位止损</span><span class="v text-green">{{ btResult.deploy.stop }}</span></div>
            </div>
          </div>
        </template>
        <div v-else-if="btResult?.error" class="alert alert-error mt">{{ btResult.error }}</div>
      </div>

      <!-- 优化结果 -->
      <div v-if="optResult && !optResult.error" class="card">
        <div class="section-title">参数优化 · 多季度样本外验证</div>
        <p class="td-muted mt-s">各季<b>买入持有</b>基准：
          <span v-for="q in optResult.quarters" :key="q" class="qhold">{{ q }}: {{ optResult.holds[q] }}%</span>
        </p>
        <div class="opt-best mt">
          <div class="opt-best-label">推荐稳健配置</div>
          <div class="opt-best-body">
            <b>{{ optResult.best.N }} 格 · {{ optResult.best.box }} · {{ optResult.best.spacing }} · 裁影线{{ optResult.best.trim }}</b>
            <span>6季均值 <em class="text-red">{{ optResult.best.mean }}%</em> · 最差季 {{ optResult.best.worst }}% · 均回撤 {{ optResult.best.dd }}% · 胜持有 {{ optResult.best.beat_hold }}</span>
          </div>
          <div class="deploy-grid">
            <div><span class="k">箱体</span><span class="v">{{ optResult.deploy.box_lo }} ~ {{ optResult.deploy.box_hi }}</span></div>
            <div><span class="k">格数</span><span class="v">{{ optResult.deploy.grids }}</span></div>
            <div><span class="k">步长</span><span class="v">{{ optResult.deploy.step }} 元 ({{ optResult.deploy.step_pct }}%)</span></div>
            <div><span class="k">止损</span><span class="v text-green">{{ optResult.deploy.stop }}</span></div>
          </div>
        </div>
        <div class="section-sub mt">稳健排序（按最差季→均值）</div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>格数</th><th>箱体</th><th>间距</th><th>裁</th><th>均值</th><th>最差季</th><th>均回撤</th><th>胜持有</th>
              <th v-for="q in optResult.quarters" :key="q">{{ q }}</th></tr></thead>
            <tbody>
              <tr v-for="(r, i) in optResult.robust" :key="i" :class="{ sel: i === 0 }">
                <td class="td-mono">{{ r.N }}</td><td>{{ r.box }}</td><td>{{ r.spacing }}</td><td>{{ r.trim }}</td>
                <td class="td-mono text-red">{{ r.mean }}%</td><td class="td-mono">{{ r.worst }}%</td>
                <td class="td-mono">{{ r.dd }}%</td><td class="td-mono">{{ r.beat_hold }}</td>
                <td v-for="q in optResult.quarters" :key="q" class="td-mono" :class="sign(r.per_quarter[q])">{{ r.per_quarter[q] }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="section-sub mt">⚠️ 过拟合反例（只按最近一季挑最优——最差季往往更糟，勿采用）</div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>格数</th><th>箱体</th><th>间距</th><th>均值</th><th>最差季</th>
              <th v-for="q in optResult.quarters" :key="q">{{ q }}</th></tr></thead>
            <tbody>
              <tr v-for="(r, i) in optResult.overfit" :key="i">
                <td class="td-mono">{{ r.N }}</td><td>{{ r.box }}</td><td>{{ r.spacing }}</td>
                <td class="td-mono">{{ r.mean }}%</td><td class="td-mono text-green">{{ r.worst }}%</td>
                <td v-for="q in optResult.quarters" :key="q" class="td-mono" :class="sign(r.per_quarter[q])">{{ r.per_quarter[q] }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
      <div v-else-if="optResult?.error" class="alert alert-error">{{ optResult.error }}</div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import VChart from "vue-echarts";
import { use } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { LineChart } from "echarts/charts";
import { GridComponent, TooltipComponent, LegendComponent } from "echarts/components";
import {
  fetchEtfGridStatus,
  runEtfSyncAsync,
  fetchEtfSyncStatus,
  screenEtfGrid,
  backtestEtfGrid,
  optimizeEtfGrid,
  type EtfGridStatus,
  type EtfScreenRow,
  type EtfBacktestResult,
  type EtfOptimizeResult,
} from "../api/client";

use([CanvasRenderer, LineChart, GridComponent, TooltipComponent, LegendComponent]);

const status = ref<EtfGridStatus | null>(null);
const syncing = ref(false);
const syncCur = ref(0);
const syncTot = ref(0);
let syncTimer: ReturnType<typeof setInterval> | null = null;

const advMin = ref(100000000);
const screening = ref(false);
const screened = ref(false);
const screenError = ref("");
const rows = ref<EtfScreenRow[]>([]);

const selected = ref("");
const selectedName = ref("");
const bt = ref({ N: 10, lookback: 60, rebal: 0, geom: false, trim: true });
const btLoading = ref(false);
const btResult = ref<EtfBacktestResult | null>(null);
const optLoading = ref(false);
const optResult = ref<EtfOptimizeResult | null>(null);

const pct = (v?: number) => (v == null ? "—" : `${(v * 100).toFixed(1)}%`);
const sign = (v?: number) => (v == null ? "" : v >= 0 ? "text-red" : "text-green");
const signCard = (v?: number) => (v == null ? "" : v >= 0 ? "positive" : "negative");
const rankCls = (i: number) => (i === 0 ? "gold" : i === 1 ? "silver" : i === 2 ? "bronze" : "");

async function refreshStatus() {
  try {
    status.value = (await fetchEtfGridStatus()).data;
  } catch (e) {
    console.error(e);
  }
}

function stopSyncPoll() {
  if (syncTimer) {
    clearInterval(syncTimer);
    syncTimer = null;
  }
}

async function doSync() {
  syncing.value = true;
  syncCur.value = 0;
  syncTot.value = status.value?.universe ?? 0;
  try {
    const res = await runEtfSyncAsync();
    if (res.data.error || !res.data.job_id) {
      screenError.value = res.data.error || "同步启动失败";
      syncing.value = false;
      return;
    }
    const jobId = res.data.job_id;
    syncTimer = setInterval(async () => {
      try {
        const s = (await fetchEtfSyncStatus(jobId)).data;
        syncCur.value = s.current;
        syncTot.value = s.total;
        if (s.status !== "running") {
          stopSyncPoll();
          syncing.value = false;
          await refreshStatus();
        }
      } catch (e) {
        console.error(e);
        stopSyncPoll();
        syncing.value = false;
      }
    }, 1000);
  } catch (e) {
    console.error(e);
    syncing.value = false;
  }
}

async function doScreen() {
  screening.value = true;
  screenError.value = "";
  try {
    const res = await screenEtfGrid(advMin.value);
    if (res.data.error) {
      screenError.value = res.data.error;
      rows.value = [];
    } else {
      rows.value = res.data.results ?? [];
    }
    screened.value = true;
  } catch (e) {
    console.error(e);
    screenError.value = "挖掘失败";
  } finally {
    screening.value = false;
  }
}

function pick(code: string) {
  selected.value = code;
  selectedName.value = rows.value.find((r) => r.code === code)?.name ?? "";
  btResult.value = null;
  optResult.value = null;
  doBacktest();
}

async function doBacktest() {
  if (!selected.value) return;
  btLoading.value = true;
  try {
    const res = await backtestEtfGrid({ code: selected.value, ...bt.value });
    btResult.value = res.data;
  } catch (e) {
    console.error(e);
    btResult.value = { error: "回测失败" } as EtfBacktestResult;
  } finally {
    btLoading.value = false;
  }
}

async function doOptimize() {
  if (!selected.value) return;
  optLoading.value = true;
  try {
    const res = await optimizeEtfGrid(selected.value);
    optResult.value = res.data;
  } catch (e) {
    console.error(e);
    optResult.value = { error: "优化失败" } as EtfOptimizeResult;
  } finally {
    optLoading.value = false;
  }
}

const chartOption = computed(() => {
  const r = btResult.value;
  if (!r || r.error) return {};
  const dates = Object.keys(r.grid_curve);
  const g = Object.values(r.grid_curve);
  const h = dates.map((d) => r.hold_curve[d]);
  return {
    tooltip: { trigger: "axis", valueFormatter: (v: number) => `${((v - 1) * 100).toFixed(1)}%` },
    legend: { data: ["网格", "买入持有"], top: 0 },
    grid: { left: 48, right: 16, top: 34, bottom: 28 },
    xAxis: { type: "category", data: dates, axisLabel: { fontSize: 10 } },
    yAxis: { type: "value", scale: true, axisLabel: { formatter: (v: number) => `${((v - 1) * 100).toFixed(0)}%` } },
    series: [
      { name: "网格", type: "line", data: g, showSymbol: false, smooth: true,
        lineStyle: { color: "#0071e3", width: 2 } },
      { name: "买入持有", type: "line", data: h, showSymbol: false, smooth: true,
        lineStyle: { color: "#8e8e93", width: 1.5, type: "dashed" } },
    ],
  };
});

onMounted(refreshStatus);
onUnmounted(stopSyncPoll);
</script>

<style scoped>
.etf-grid { display: flex; flex-direction: column; gap: 20px; }
.page-header h1 { font-size: 26px; font-weight: 600; }
.page-desc { color: var(--color-text-secondary); margin-top: 4px; }
.card { background: var(--color-surface); border-radius: var(--radius-lg); padding: 20px; box-shadow: var(--shadow-sm); }
.mt { margin-top: 14px; }
.mt-s { margin-top: 6px; }
.section-title { font-size: 16px; font-weight: 600; }
.section-sub { font-size: 13px; font-weight: 600; color: var(--color-text-secondary); }
.status-row { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }
.status-items { display: flex; gap: 28px; flex-wrap: wrap; }
.status-item { display: flex; flex-direction: column; }
.status-item .k { font-size: 12px; color: var(--color-text-tertiary); }
.status-item .v { font-size: 15px; font-weight: 600; font-variant-numeric: tabular-nums; }
.screen-head { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }
.screen-ctrl { display: flex; align-items: center; gap: 12px; }
.mini { font-size: 12px; color: var(--color-text-secondary); display: flex; align-items: center; gap: 6px; }
.mini-select { padding: 4px 8px; border-radius: var(--radius-sm); border: 1px solid var(--color-border, #d2d2d7); }
.btn-primary { background: var(--color-accent); color: #fff; border: none; border-radius: 980px; padding: 9px 18px;
  font-size: 14px; font-weight: 500; cursor: pointer; display: inline-flex; align-items: center; gap: 8px; transition: var(--transition); }
.btn-primary:hover:not(:disabled) { opacity: 0.9; }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-primary.alt { background: #5856d6; }
.btn-small { background: var(--color-blue-bg, #e8f0fe); color: var(--color-accent); border: none; border-radius: var(--radius-sm);
  padding: 4px 10px; font-size: 12px; cursor: pointer; }
.btn-small:hover { opacity: 0.85; }
.spinner { width: 13px; height: 13px; border: 2px solid rgba(255,255,255,0.4); border-top-color: #fff; border-radius: 50%; animation: spin 0.7s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px; align-items: end; }
.form-group { display: flex; flex-direction: column; gap: 6px; }
.form-group label { font-size: 12px; color: var(--color-text-secondary); }
.form-group input, .select-wrap select { padding: 8px 10px; border-radius: var(--radius-sm); border: 1px solid var(--color-border, #d2d2d7); font-size: 14px; width: 100%; }
.form-actions { flex-direction: row; gap: 10px; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; padding: 8px 10px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.03em; color: var(--color-text-tertiary); border-bottom: 1px solid var(--color-border, #e5e5ea); white-space: nowrap; }
td { padding: 8px 10px; border-bottom: 1px solid var(--color-border, #f0f0f2); white-space: nowrap; }
tr.sel { background: var(--color-blue-bg, #e8f0fe); }
.code-badge { font-family: var(--font-mono); font-size: 12px; background: var(--color-bg); padding: 2px 6px; border-radius: 4px; }
.td-mono { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
.td-muted { color: var(--color-text-tertiary); }
.text-red { color: var(--color-red); }
.text-green { color: var(--color-green); }
.t0-tag { font-size: 10px; color: #5856d6; border: 1px solid #5856d6; border-radius: 4px; padding: 0 4px; margin-left: 5px; }
.rank { display: inline-flex; align-items: center; justify-content: center; width: 20px; height: 20px; border-radius: 50%; font-size: 11px; background: var(--color-bg); }
.rank.gold { background: #ffd60a; color: #000; }
.rank.silver { background: #d1d1d6; color: #000; }
.rank.bronze { background: #e0a458; color: #fff; }
.metrics-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 12px; }
.metric-card { background: var(--color-bg); border-radius: var(--radius-md); padding: 12px 14px; }
.metric-card.positive .metric-value { color: var(--color-red); }
.metric-card.negative .metric-value { color: var(--color-green); }
.metric-label { font-size: 12px; color: var(--color-text-tertiary); }
.metric-value { font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums; margin-top: 2px; }
.chart-card { padding: 12px; }
.deploy { background: var(--color-bg); border-radius: var(--radius-md); padding: 14px 16px; }
.deploy-title, .deploy-grid { }
.deploy-title { font-size: 13px; font-weight: 600; margin-bottom: 10px; }
.deploy-grid { display: flex; flex-wrap: wrap; gap: 24px; }
.deploy-grid > div { display: flex; flex-direction: column; }
.deploy-grid .k { font-size: 12px; color: var(--color-text-tertiary); }
.deploy-grid .v { font-size: 15px; font-weight: 600; font-variant-numeric: tabular-nums; }
.alert { display: flex; align-items: flex-start; gap: 10px; padding: 12px 14px; border-radius: var(--radius-md); font-size: 13px; line-height: 1.5; }
.alert svg { flex-shrink: 0; margin-top: 1px; }
.alert-warning { background: #fff8e6; color: #7a5b00; }
.alert-error { background: #ffecec; color: var(--color-red); }
.opt-best { background: var(--color-blue-bg, #eef4ff); border-radius: var(--radius-md); padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; }
.opt-best-label { font-size: 12px; color: var(--color-accent); font-weight: 600; }
.opt-best-body { display: flex; flex-direction: column; gap: 4px; font-size: 14px; }
.opt-best-body span { color: var(--color-text-secondary); font-size: 13px; }
.opt-best-body em { font-style: normal; font-weight: 600; }
.qhold { margin-right: 12px; font-variant-numeric: tabular-nums; }
</style>
