<script setup lang="ts">
/**
 * 市场 regime 快照卡片 —— 仪表盘的第一屏。
 *
 * 每天 17:30 由后台守护进程生成(全A股宽度 + 板块轮动 + 资金 + 时事面,
 * 经 DeepSeek v4-pro 深度思考),这里只做展示 + 手动补跑。
 *
 * 判定分两层且都显示:规则层(多因子投票,确定性可复现)和 LLM 层(叙事与
 * 建议)。两者背离时不藏着——那本身就是信号,所以徽章旁边永远并排列出。
 *
 * 纯观测:不下单、不产生信号。
 */
import { computed, onMounted, ref } from "vue";
import {
  fetchRegimeLatest,
  fetchRegimeHistory,
  fetchRegimeDay,
  runRegimeSnapshot,
  type RegimeSnapshot,
} from "../api/client";

const snap = ref<RegimeSnapshot | null>(null);
const history = ref<RegimeSnapshot[]>([]);
const loading = ref(true);
const running = ref(false);
const err = ref("");
const showReport = ref(false);
const viewingDay = ref<string>("");

/** regime → 主题色。A股口径:涨=红、跌=绿;震荡取琥珀紫,和涨跌都区分得开。 */
// dot 是时间轴小条用的纯色:11px 宽的条上,渐变会把三种 regime 糊成一片
// 橙色系,纯色才一眼分得清。
interface Theme { grad: string; glow: string; icon: string; dot: string }
const UNKNOWN: Theme = {
  grad: "linear-gradient(135deg,#8e8e93 0%,#c7c7cc 100%)", glow: "rgba(0,0,0,.15)",
  icon: "•", dot: "#c7c7cc",
};
const THEME: Record<string, Theme> = {
  上涨: { grad: "linear-gradient(135deg,#ff3b30 0%,#ff9500 100%)", glow: "rgba(255,59,48,.35)", icon: "▲", dot: "#ff3b30" },
  震荡: { grad: "linear-gradient(135deg,#f59e0b 0%,#a855f7 100%)", glow: "rgba(168,85,247,.32)", icon: "◆", dot: "#a855f7" },
  下跌: { grad: "linear-gradient(135deg,#10b981 0%,#0ea5e9 100%)", glow: "rgba(16,185,129,.32)", icon: "▼", dot: "#10b981" },
};
const themeOf = (r: string): Theme => THEME[r] ?? UNKNOWN;

/** LLM 判定优先(它吃了消息面);没有就退回规则层标签的首字。 */
function regimeOf(s: RegimeSnapshot | null): string {
  if (!s) return "未知";
  if (s.llm_regime && THEME[s.llm_regime]) return s.llm_regime;
  for (const k of ["上涨", "震荡", "下跌"]) if (s.rule_label?.includes(k)) return k;
  return "未知";
}

const regime = computed(() => regimeOf(snap.value));
const theme = computed(() => themeOf(regime.value));
const m = computed(() => snap.value?.metrics ?? {});
const llm = computed(() => snap.value?.llm ?? {});

/** 规则层 vs LLM 层是否背离 —— 背离要显眼地讲出来。 */
const divergent = computed(() => {
  const s = snap.value;
  if (!s?.llm_regime) return false;
  return !s.rule_label?.includes(s.llm_regime);
});

const ACTION_COLOR: Record<string, string> = {
  加仓: "#ff3b30", 持仓: "#0071e3", 减仓: "#ff9500", 观望: "#8e8e93",
};

function pct(v: number | undefined, digits = 1): string {
  return v === undefined || v === null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(digits)}%`;
}
function num(v: number | undefined, digits = 0): string {
  return v === undefined || v === null ? "—" : v.toFixed(digits);
}
/** 涨跌上色:红涨绿跌(A股口径)。 */
function upDown(v: number | undefined): string {
  if (v === undefined || v === null) return "";
  return v > 0 ? "v-up" : v < 0 ? "v-down" : "";
}

/**
 * 极简 markdown → HTML。只认 LLM 实际会用的两种标记(段落、**加粗**),
 * 不为此装一个 markdown 依赖。**先转义再替换**:正文是模型生成的不可信
 * 文本,直接 v-html 就是一个 XSS 口子。
 */
function renderMd(md: string): string {
  const esc = (t: string) =>
    t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return md
    .split(/\n\s*\n/)
    .map((block) => {
      const b = esc(block.trim());
      if (!b) return "";
      const bold = b.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
      // 整段只有一个加粗短语 → 当小标题
      if (/^<strong>[^<]*<\/strong>$/.test(bold)) return `<h4>${bold}</h4>`;
      return `<p>${bold.replace(/\n/g, "<br/>")}</p>`;
    })
    .join("");
}

async function load() {
  loading.value = true;
  err.value = "";
  try {
    const [latest, hist] = await Promise.all([
      fetchRegimeLatest(),
      fetchRegimeHistory(90),
    ]);
    snap.value = latest.data?.exists ? latest.data : null;
    history.value = hist.data?.items ?? [];
    viewingDay.value = snap.value?.date ?? "";
  } catch (e: unknown) {
    err.value = e instanceof Error ? e.message : String(e);
  } finally {
    loading.value = false;
  }
}

async function openDay(day: string) {
  if (day === viewingDay.value) return;
  try {
    const { data } = await fetchRegimeDay(day);
    snap.value = data;
    viewingDay.value = day;
  } catch (e: unknown) {
    err.value = e instanceof Error ? e.message : String(e);
  }
}

async function runNow() {
  running.value = true;
  err.value = "";
  try {
    await runRegimeSnapshot();
    await load();
  } catch (e: unknown) {
    err.value = `生成失败: ${e instanceof Error ? e.message : String(e)}`;
  } finally {
    running.value = false;
  }
}

/** 历史条按时间正序(左旧右新),读起来才像时间轴。 */
const timeline = computed(() => [...history.value].reverse());

onMounted(load);
</script>

<template>
  <div class="regime-wrap">
    <!-- 空态:还没跑过 -->
    <div v-if="!loading && !snap" class="regime-card empty">
      <div class="empty-body">
        <h2>市场 regime 快照</h2>
        <p>还没有快照。后台每天 17:30 自动生成,也可以现在就跑一次(约 1-2 分钟)。</p>
        <button class="run-btn" :disabled="running" @click="runNow">
          {{ running ? "生成中…" : "立即生成" }}
        </button>
        <p v-if="err" class="err">{{ err }}</p>
      </div>
    </div>

    <div v-else-if="loading" class="regime-card empty">
      <div class="empty-body"><p>加载中…</p></div>
    </div>

    <div v-else class="regime-card" :style="{ background: theme.grad, boxShadow: `0 12px 40px ${theme.glow}` }">
      <!-- 头部 -->
      <div class="rc-head">
        <div class="rc-verdict">
          <span class="rc-icon">{{ theme.icon }}</span>
          <div>
            <div class="rc-label">{{ regime }}</div>
            <div class="rc-sub">
              {{ snap!.date }} · 规则层 {{ snap!.rule_label }}({{ snap!.rule_score > 0 ? "+" : "" }}{{ snap!.rule_score }})
              <span v-if="divergent" class="rc-diverge">规则与 AI 判定背离</span>
            </div>
          </div>
        </div>
        <div class="rc-right">
          <div v-if="snap!.action" class="rc-action" :style="{ background: ACTION_COLOR[snap!.action] ?? '#8e8e93' }">
            {{ snap!.action }}
          </div>
          <div v-if="snap!.llm_confidence" class="rc-conf">
            把握 {{ snap!.llm_confidence }}%
            <div class="rc-conf-bar"><i :style="{ width: snap!.llm_confidence + '%' }" /></div>
          </div>
        </div>
      </div>

      <h2 v-if="snap!.headline" class="rc-headline">{{ snap!.headline }}</h2>

      <!-- 关键指标 -->
      <div class="rc-metrics">
        <div class="rc-metric">
          <span class="k">站上 MA50</span>
          <span class="v">{{ num(m.above50) }}%</span>
          <span class="h">MA20 {{ num(m.above20) }}% · MA200 {{ num(m.above200) }}%</span>
        </div>
        <div class="rc-metric">
          <span class="k">涨跌家数</span>
          <span class="v">{{ m.up ?? "—" }}/{{ m.dn ?? "—" }}</span>
          <span class="h">涨跌比 {{ m.ad_ratio?.toFixed(2) ?? "—" }}</span>
        </div>
        <div class="rc-metric">
          <span class="k">成交额</span>
          <span class="v">{{ num(m.amt_today) }}<small>亿</small></span>
          <span class="h">5v20 {{ pct(m.amt_chg) }}</span>
        </div>
        <div class="rc-metric">
          <span class="k">大盘 / 等权(20日)</span>
          <span class="v">{{ pct(m.cap20) }} <small>/</small> {{ pct(m.eq20) }}</span>
          <span class="h">今日 {{ pct(m.cap1) }} / {{ pct(m.eq1) }}</span>
        </div>
        <div class="rc-metric">
          <span class="k">20日新高/新低</span>
          <span class="v">{{ m.nh ?? "—" }}/{{ m.nl ?? "—" }}</span>
          <span class="h">换手中位 {{ num(m.turn, 2) }}%</span>
        </div>
      </div>

      <!-- 板块 -->
      <div class="rc-sectors" v-if="llm.sectors_favored?.length || llm.sectors_avoid?.length">
        <div v-if="llm.sectors_favored?.length" class="rc-sec-row">
          <span class="rc-sec-tag strong">占优</span>
          <span v-for="s in llm.sectors_favored" :key="s" class="rc-chip">{{ s }}</span>
        </div>
        <div v-if="llm.sectors_avoid?.length" class="rc-sec-row">
          <span class="rc-sec-tag weak">回避</span>
          <span v-for="s in llm.sectors_avoid" :key="s" class="rc-chip">{{ s }}</span>
        </div>
      </div>

      <!-- 依据 -->
      <ul v-if="llm.drivers?.length" class="rc-drivers">
        <li v-for="(d, i) in llm.drivers" :key="i">{{ d }}</li>
      </ul>

      <!-- 历史时间轴 -->
      <!-- 一期也显示:历史区从第一天就在那儿,随每日快照自己长出来 -->
      <div v-if="timeline.length" class="rc-history">
        <div class="rc-hist-title">历史 regime · 最近 {{ timeline.length }} 期(点击查看)</div>
        <div class="rc-hist-bars">
          <button
            v-for="h in timeline"
            :key="h.date"
            class="rc-bar"
            :class="{ active: h.date === viewingDay }"
            :style="{ background: themeOf(regimeOf(h)).dot }"
            :title="`${h.date} ${regimeOf(h)}${h.headline ? ' — ' + h.headline : ''}`"
            @click="openDay(h.date)"
          />
        </div>
      </div>

      <!-- 展开正文 -->
      <div class="rc-foot">
        <button class="rc-link" @click="showReport = !showReport">
          {{ showReport ? "收起完整报告" : "展开完整报告" }}
        </button>
        <button class="rc-link" :disabled="running" @click="runNow">
          {{ running ? "重新生成中…" : "重新生成" }}
        </button>
        <span class="rc-model">{{ snap!.model || "无 LLM" }} · {{ snap!.created_at?.slice(0, 16).replace("T", " ") }}</span>
      </div>
      <p v-if="err" class="err in-card">{{ err }}</p>

      <div v-if="showReport" class="rc-report">
        <div class="rc-md" v-html="renderMd(snap!.report_md || '')" />
        <div v-if="llm.risk_notes?.length" class="rc-risks">
          <div class="rc-risk-title">证伪 / 风险点</div>
          <ul><li v-for="(r, i) in llm.risk_notes" :key="i">{{ r }}</li></ul>
        </div>
        <div v-if="snap!.sectors?.top20?.length" class="rc-sector-tables">
          <div>
            <div class="rc-risk-title">20日最强</div>
            <div v-for="s in snap!.sectors!.top20!.slice(0, 8)" :key="s.industry" class="rc-sec-line">
              <span>{{ s.industry }}</span><span :class="upDown(s.ret)">{{ pct(s.ret) }}</span>
            </div>
          </div>
          <div>
            <div class="rc-risk-title">20日最弱</div>
            <div v-for="s in snap!.sectors!.bottom20!.slice(0, 8)" :key="s.industry" class="rc-sec-line">
              <span>{{ s.industry }}</span><span :class="upDown(s.ret)">{{ pct(s.ret) }}</span>
            </div>
          </div>
          <div>
            <div class="rc-risk-title">近5日领涨</div>
            <div v-for="s in snap!.sectors!.top5d!.slice(0, 8)" :key="s.industry" class="rc-sec-line">
              <span>{{ s.industry }}</span><span :class="upDown(s.ret)">{{ pct(s.ret) }}</span>
            </div>
          </div>
        </div>
        <p class="rc-disclaimer">
          纯观测报告,非投资建议。本系统的实证结论是择时无 alpha,该快照用于解释持仓与控制暴露,不作方向性下注依据。
        </p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.regime-wrap { margin-bottom: 20px; }

.regime-card {
  border-radius: var(--radius-xl);
  padding: 22px 24px;
  color: #fff;
  position: relative;
  overflow: hidden;
}
.regime-card.empty {
  background: var(--color-surface);
  color: var(--color-text-primary);
  box-shadow: var(--shadow-sm);
  border: 1px dashed var(--color-border);
}
.empty-body { text-align: center; padding: 18px 0; }
.empty-body h2 { margin: 0 0 6px; font-size: 17px; }
.empty-body p { color: var(--color-text-secondary); font-size: 13px; margin: 0 0 12px; }
.run-btn {
  background: var(--color-accent); color: #fff; border: none;
  padding: 8px 18px; border-radius: var(--radius-sm); font-size: 13px; cursor: pointer;
}
.run-btn:disabled { opacity: .6; cursor: default; }

.rc-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.rc-verdict { display: flex; align-items: center; gap: 14px; }
.rc-icon { font-size: 30px; line-height: 1; opacity: .95; }
.rc-label { font-size: 30px; font-weight: 700; letter-spacing: 1px; line-height: 1.1; }
.rc-sub { font-size: 12px; opacity: .85; margin-top: 3px; }
.rc-diverge {
  margin-left: 8px; background: rgba(255,255,255,.25);
  padding: 1px 7px; border-radius: 20px; font-weight: 600;
}
.rc-right { display: flex; align-items: center; gap: 14px; }
.rc-action {
  font-size: 16px; font-weight: 700; padding: 7px 16px; border-radius: var(--radius-sm);
  box-shadow: 0 2px 10px rgba(0,0,0,.18);
}
.rc-conf { font-size: 11px; opacity: .9; min-width: 84px; }
.rc-conf-bar { height: 4px; background: rgba(255,255,255,.3); border-radius: 3px; margin-top: 4px; overflow: hidden; }
.rc-conf-bar i { display: block; height: 100%; background: #fff; border-radius: 3px; }

.rc-headline { font-size: 19px; font-weight: 600; margin: 14px 0 4px; line-height: 1.4; }

.rc-metrics {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px; margin-top: 16px;
}
.rc-metric {
  background: rgba(255,255,255,.16); border-radius: var(--radius-md);
  padding: 10px 12px; display: flex; flex-direction: column; gap: 2px;
  backdrop-filter: blur(6px);
}
.rc-metric .k { font-size: 11px; opacity: .85; }
.rc-metric .v { font-size: 19px; font-weight: 700; }
.rc-metric .v small { font-size: 11px; font-weight: 500; opacity: .8; }
.rc-metric .h { font-size: 10px; opacity: .75; }

.rc-sectors { margin-top: 14px; display: flex; flex-direction: column; gap: 6px; }
.rc-sec-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.rc-sec-tag { font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 20px; }
.rc-sec-tag.strong { background: rgba(255,255,255,.9); color: #d70015; }
.rc-sec-tag.weak { background: rgba(255,255,255,.9); color: #0a7d3c; }
.rc-chip {
  font-size: 12px; background: rgba(255,255,255,.2);
  padding: 2px 9px; border-radius: 20px;
}

.rc-drivers { margin: 14px 0 0; padding-left: 18px; font-size: 12.5px; line-height: 1.7; opacity: .95; }

.rc-history { margin-top: 16px; }
.rc-hist-title { font-size: 11px; opacity: .8; margin-bottom: 6px; }
/* 深色底槽:不然「上涨」的红条落在红橙卡片背景上就隐形了。 */
.rc-hist-bars {
  display: inline-flex; gap: 3px; align-items: flex-end; flex-wrap: wrap;
  background: rgba(0, 0, 0, .22); border-radius: var(--radius-sm);
  padding: 6px 8px; max-width: 100%;
}
.rc-bar {
  width: 11px; height: 26px; border: none; border-radius: 3px;
  cursor: pointer; opacity: .85; transition: var(--transition); padding: 0;
}
.rc-bar:hover { opacity: 1; transform: translateY(-2px); }
.rc-bar.active { opacity: 1; box-shadow: 0 0 0 2px #fff; }

.rc-foot { margin-top: 16px; display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.rc-link {
  background: rgba(255,255,255,.22); border: none; color: #fff;
  font-size: 12px; padding: 6px 14px; border-radius: var(--radius-sm); cursor: pointer;
}
.rc-link:hover { background: rgba(255,255,255,.32); }
.rc-link:disabled { opacity: .5; cursor: default; }
.rc-model { font-size: 11px; opacity: .7; margin-left: auto; }

.rc-report {
  margin-top: 16px; background: rgba(255,255,255,.94); color: var(--color-text-primary);
  border-radius: var(--radius-md); padding: 18px 20px;
}
.rc-md :deep(h4) { font-size: 14px; margin: 16px 0 6px; }
.rc-md :deep(h4:first-child) { margin-top: 0; }
.rc-md :deep(p) { font-size: 13px; line-height: 1.8; margin: 0 0 10px; color: #333; }
.rc-risks { margin-top: 14px; border-top: 1px solid var(--color-border); padding-top: 12px; }
.rc-risk-title { font-size: 12px; font-weight: 700; color: var(--color-text-secondary); margin-bottom: 6px; }
.rc-risks ul { margin: 0; padding-left: 18px; font-size: 12.5px; line-height: 1.7; color: #333; }
.rc-sector-tables {
  margin-top: 14px; display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 16px; border-top: 1px solid var(--color-border); padding-top: 12px;
}
.rc-sec-line { display: flex; justify-content: space-between; font-size: 12px; padding: 2px 0; }
.v-up { color: var(--color-red); font-weight: 600; }
.v-down { color: var(--color-green); font-weight: 600; }
.rc-disclaimer {
  margin: 14px 0 0; font-size: 11px; color: var(--color-text-tertiary); line-height: 1.6;
}

.err { color: #fff3f0; font-size: 12px; margin-top: 8px; }
.err.in-card { background: rgba(0,0,0,.25); padding: 6px 10px; border-radius: 6px; }
.regime-card.empty .err { color: var(--color-red); }
</style>
