<template>
  <div class="agent-page">
    <div class="page-header">
      <h1>AI Agent</h1>
      <p class="page-desc">设定目标，剩下交给 Agent 自动维护</p>
    </div>

    <!-- Top stats -->
    <div class="stats-row">
      <div class="stat-card">
        <div class="stat-info">
          <span class="stat-label">组合净值</span>
          <span class="stat-value">¥{{ formatMoney(portfolio?.total_value ?? 0) }}</span>
        </div>
      </div>
      <div class="stat-card" :class="pnlClass">
        <div class="stat-info">
          <span class="stat-label">累计收益</span>
          <span class="stat-value">{{ formatPct(portfolio?.pnl_pct ?? 0) }}</span>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-info">
          <span class="stat-label">距目标</span>
          <span class="stat-value">{{ targetGapStr }}</span>
        </div>
      </div>
      <div class="stat-card" :class="agentStatusClass">
        <div class="stat-info">
          <span class="stat-label">Agent 状态</span>
          <span class="stat-value stat-value-sm">{{ agentStatusStr }}</span>
        </div>
      </div>
      <div class="stat-card" v-if="agent?.running">
        <div class="stat-info">
          <span class="stat-label">运行时间</span>
          <span class="stat-value stat-value-sm">{{ uptimeStr }}</span>
          <span class="stat-sub">上次 {{ lastTickStr }} · 下次 {{ nextTickStr }}</span>
        </div>
      </div>
      <div class="stat-card" :class="modeClass">
        <div class="stat-info">
          <span class="stat-label">运行模式</span>
          <span class="stat-value stat-value-sm">{{ modeLabel }}</span>
          <span v-if="scheduleSubStr" class="stat-sub">{{ scheduleSubStr }}</span>
        </div>
      </div>
      <div class="stat-card" :class="pendingCardClass" v-if="(agent?.pending_orders ?? 0) > 0">
        <div class="stat-info">
          <span class="stat-label">待成交订单</span>
          <span class="stat-value">{{ agent?.pending_orders ?? 0 }}</span>
        </div>
      </div>
    </div>

    <!-- Goal editor -->
    <div class="card">
      <div class="card-header"><h2>目标设定</h2></div>
      <div class="goal-grid">
        <label>
          <span>目标年化收益</span>
          <input type="number" step="0.01" v-model.number="goalDraft.target_annual_return" />
        </label>
        <label>
          <span>可接受最大回撤（负数）</span>
          <input type="number" step="0.01" v-model.number="goalDraft.max_drawdown" />
        </label>
        <label>
          <span>风险偏好</span>
          <select v-model="goalDraft.risk_tolerance">
            <option value="low">保守 (low)</option>
            <option value="medium">平衡 (medium)</option>
            <option value="high">激进 (high)</option>
          </select>
        </label>
        <label>
          <span>股票池（留空 = 全部）</span>
          <input type="text" v-model="goalDraft.universe_pool" placeholder="例如 my_pool" />
        </label>
        <label>
          <span>选股器（可选）</span>
          <select v-model="goalDraft.screener_name">
            <option value="">不使用</option>
            <option v-for="s in screeners" :key="s.name" :value="s.name" :title="s.description">
              {{ s.name_zh || s.name }}
            </option>
          </select>
        </label>
        <label>
          <span>策略（留空 = Agent 自动挑选）</span>
          <select v-model="goalDraft.strategy_name">
            <option value="">由 Agent 自动挑选</option>
            <option v-for="s in strategies" :key="s.name" :value="s.name" :title="s.description || ''">
              {{ s.name_zh || s.name }}
            </option>
          </select>
        </label>
      </div>
      <div class="schedule-block">
        <label class="schedule-toggle">
          <input type="checkbox" v-model="advParams.daily_schedule_enabled" />
          <span>每日定时运行</span>
        </label>
        <div v-if="advParams.daily_schedule_enabled" class="schedule-fields">
          <label class="schedule-time">
            <span>运行时间</span>
            <input type="time" v-model="advParams.daily_run_time" />
          </label>
          <label class="schedule-toggle">
            <input type="checkbox" v-model="advParams.daily_trading_days_only" />
            <span>仅交易日运行（周末/节假日自动跳过）</span>
          </label>
          <p class="muted">
            节假日精度需先运行 <code>quanti sync --calendar</code>；否则按周一~周五判定。
          </p>
        </div>
      </div>
      <div class="actions">
        <button class="btn-primary" :disabled="saving" @click="saveGoal">
          {{ saving ? "保存中..." : "保存目标" }}
        </button>
        <button class="btn-secondary" :disabled="ticking" @click="forceTick">
          {{ ticking ? "执行中..." : "立即跑一轮" }}
        </button>
        <button v-if="!agent?.running" class="btn-success" :disabled="busy" @click="startAgent">
          启动 Agent
        </button>
        <button v-else class="btn-danger" :disabled="busy" @click="stopAgent">停止 Agent</button>
        <button class="btn-secondary" :disabled="busy" @click="reset">重置组合</button>
      </div>
      <div v-if="message" class="sync-msg" :class="messageError ? 'error' : 'success'">
        {{ message }}
      </div>
    </div>

    <!-- Agent mode + upgrades (P1-P4) -->
    <div class="card">
      <div class="card-header">
        <h2>Agent 模式</h2>
        <span class="muted">钉死策略时模式无效 — 钉选优先</span>
      </div>
      <div class="mode-presets">
        <button
          type="button"
          class="mode-pill"
          :class="{ 'mode-active mode-rule-active': advParams.agent_mode === 'rule' }"
          @click="applyPreset('rule')"
        >
          <div class="mode-title">经典 (rule)</div>
          <div class="mode-desc">单策略 · Selector 24h cache · 不开因子/LLM</div>
        </button>
        <button
          type="button"
          class="mode-pill"
          :class="{ 'mode-active mode-ensemble-active': advParams.agent_mode === 'ensemble' }"
          @click="applyPreset('ensemble')"
        >
          <div class="mode-title">集成 (ensemble)</div>
          <div class="mode-desc">Top-K 策略加权 + 截面因子 + 流动性清洗 + 行业中性</div>
        </button>
        <button
          type="button"
          class="mode-pill"
          :class="{ 'mode-active mode-llm-active': advParams.agent_mode === 'llm' }"
          @click="applyPreset('llm')"
        >
          <div class="mode-title">LLM 决策</div>
          <div class="mode-desc">ensemble 候选 → LLM 拍板 · 默认 DeepSeek，可切 Anthropic</div>
        </button>
        <button
          type="button"
          class="mode-pill"
          :class="{ 'mode-active mode-llm-active': advParams.agent_mode === 'llm_full' }"
          @click="applyPreset('llm_full')"
        >
          <div class="mode-title">LLM 全权</div>
          <div class="mode-desc">100 候选直达 LLM · 买卖/止损/加仓点位全归 LLM · 盘中 LLM 守护 · 仅模拟盘</div>
        </button>
      </div>
      <div class="adv-note" v-if="advParams.agent_mode === 'llm_full'">
        全权模式:买入护栏只剩单票上限/日内开仓数等 sanity 闸;止损=LLM 落库点位
        (本地机械守护 5 秒比价执行,LLM 失联沿用最后点位)+ 灾难地板(风控审计页可调);
        盘中每 {{ advParams.llm_guard_interval_sec }}s 由 LLM 复核持仓并即刻执行。
        实盘账户拒绝此模式。
      </div>
      <details class="advanced">
        <summary>高级开关(单独细调,会覆盖预设)</summary>
        <div class="adv-grid">
          <label class="adv-check">
            <input type="checkbox" v-model="advParams.ensemble_enabled" />
            <span>ensemble_enabled</span>
            <em>Top-K 策略融合;关闭则走单策略</em>
          </label>
          <label class="adv-check">
            <input type="checkbox" v-model="advParams.industry_neutral" />
            <span>industry_neutral</span>
            <em>每行业最多 2 个候选,避免行业过度集中</em>
          </label>
          <label class="adv-check">
            <input type="checkbox" v-model="advParams.liquidity_filter" />
            <span>liquidity_filter</span>
            <em>排除停牌/新股/低流动性,从全市场筛到 ~1000 只</em>
          </label>
          <label class="adv-check">
            <input type="checkbox" v-model="advParams.wf_enabled" />
            <span>wf_enabled</span>
            <em>walk-forward 滚动验证,杜绝单窗 IS 过拟合</em>
          </label>
          <label class="adv-check">
            <input type="checkbox" v-model="advParams.dsr_gate" />
            <span>dsr_gate</span>
            <em>DSR 过拟合门:赢家 OOS 夏普按候选数做多重检验紧缩,低于 dsr_min 退等权(默认关,先看日志验校准)</em>
          </label>
          <label class="adv-num">
            <span>dsr_min</span>
            <input type="number" step="0.01" min="0" max="1"
                   v-model.number="advParams.dsr_min" />
            <em>DSR 门阈值 0~1,越高越严;默认 0.85(校准回测最优,平台 0.70~0.95)</em>
          </label>
          <label class="adv-check">
            <input type="checkbox" v-model="advParams.regime_detect" />
            <span>regime_detect</span>
            <em>默认开。tick 第一步读当日/上一交易日的全市场宽度快照写决策日志
              (只读 ~1ms,盘中守护链路不接它);不影响选股与仓位</em>
          </label>
          <label class="adv-check">
            <input type="checkbox" v-model="advParams.regime_in_prompt" />
            <span>regime_in_prompt</span>
            <em>默认开(仅 LLM 决策模式)。把快照里的客观指标(MA20/50/200 上方占比、
              涨跌家数、大盘 vs 等权、成交额 5v20、规则层标签)拼进裁判 LLM 上下文,
              附「不得据此调仓」禁令;快照里 LLM 写的 action/板块推荐一律剔除。
              immediate 成交模式与陈旧快照自动不注入</em>
          </label>
        </div>
        <div class="adv-note">
          预设按钮会重置上面四项模式开关(DSR 门与 regime 两项独立,不受预设影响);手动改完后
          下方保存目标按钮才会落库。regime 两项默认开:取消勾选并保存才会显式写
          <code>false</code> 关掉。LLM 模式的供应商与多智能体增强开关见下方「LLM 增强层」。
        </div>
      </details>

      <details class="advanced">
        <summary>LLM 增强层(情绪 / 多空辩论 / 风控三角 / 反思)</summary>
        <div class="adv-llm-provider">
          <label>
            <span>供应商 llm_provider</span>
            <select v-model="advParams.llm_provider" @change="onProviderChange">
              <option value="anthropic">Anthropic claude(需 ANTHROPIC_API_KEY + pip install .[llm]）</option>
              <option value="deepseek">DeepSeek deepseek-v4-flash(需 DEEPSEEK_API_KEY，无需额外安装）</option>
            </select>
          </label>
          <label>
            <span>模型 llm_model</span>
            <select v-model="advParams.llm_model">
              <option value="">默认({{ advParams.llm_provider === "deepseek" ? "deepseek-v4-flash" : "claude-sonnet-4-6" }})</option>
              <option v-for="m in llmModelOptions" :key="m.id" :value="m.id">
                {{ m.id }} — {{ m.desc }}
              </option>
              <option value="__custom__">自定义…</option>
            </select>
            <input
              v-if="llmModelCustom"
              type="text"
              v-model.trim="advParams.llm_model_custom"
              placeholder="输入模型 id,如 deepseek-v4-pro"
            />
            <em class="muted">决策/守护/情绪打分共用此模型。留默认最稳;DeepSeek 侧
              v4-pro 思考更深但更慢更贵,盘中守护(每 5 分钟)用 flash 性价比更高。
              选了对侧供应商的模型 id 时 DeepSeek 会自动回落自家默认。</em>
          </label>
        </div>
        <div class="adv-grid">
          <label class="adv-check">
            <input type="checkbox" v-model="advParams.sentiment_enabled" />
            <span>sentiment_enabled</span>
            <em>① 新闻情绪 overlay(ensemble 与 LLM 模式都生效)</em>
          </label>
          <label class="adv-num">
            <span>sentiment_blend</span>
            <input type="number" step="0.05" min="0" max="1"
                   v-model.number="advParams.sentiment_blend" />
            <em>情绪在候选融合中的权重 0~1</em>
          </label>
          <label class="adv-check">
            <input type="checkbox" v-model="advParams.llm_debate" />
            <span>llm_debate</span>
            <em>② 多空辩论(仅 LLM 模式)</em>
          </label>
          <label class="adv-num">
            <span>llm_debate_rounds</span>
            <input type="number" step="1" min="1" max="3"
                   v-model.number="advParams.llm_debate_rounds" />
            <em>Bull→Bear 轮数</em>
          </label>
          <label class="adv-check">
            <input type="checkbox" v-model="advParams.llm_risk_debate" />
            <span>llm_risk_debate</span>
            <em>③ 风控三角(激进/中性/保守,只能缩仓或否决;仅 LLM 模式)</em>
          </label>
          <label class="adv-check">
            <input type="checkbox" v-model="advParams.llm_reflection" />
            <span>llm_reflection</span>
            <em>④ 历史经验(按相关度 + 已实现盈亏;仅 LLM 模式)</em>
          </label>
          <label class="adv-num">
            <span>llm_guard_interval_sec</span>
            <input type="number" step="60" min="60" max="3600"
                   v-model.number="advParams.llm_guard_interval_sec" />
            <em>盘中 LLM 守护间隔秒数(仅 LLM 全权模式;默认 300 = 5 分钟)</em>
          </label>
          <label class="adv-num">
            <span>llm_max_orders</span>
            <input type="number" step="1" min="1" max="30"
                   v-model.number="advParams.llm_max_orders" />
            <em>LLM 单轮订单数上限(仅 LLM 全权模式;默认 10)</em>
          </label>
        </div>
        <div class="adv-note">
          ②③④ 仅在 <b>LLM 决策</b>模式生效;① 情绪在 ensemble 也生效。需配置对应供应商
          API key;缺 key/SDK 时这些层自动 no-op,不影响其余流程。
        </div>
      </details>
    </div>

    <!-- Live order control: the LAST safety gate before real money.
         Only shown on a live account. A single arm/disarm switch flips
         阶段C(观察) ↔ 阶段D(下单); it gates BUYs only — exits always pass. -->
    <div class="card live-control" v-if="liveControl && liveControl.is_live">
      <div class="card-header">
        <h2>
          实盘控制
          <span class="live-badge">● 实盘 · {{ liveControl.account }}</span>
        </h2>
        <div class="muted">
          下单闸
          <span :class="liveControl.orders_armed ? 'bad' : 'ok'">{{
            liveControl.orders_armed ? "已布防 · 放行真钱买入" : "已撤防 · 观察模式"
          }}</span>
        </div>
      </div>

      <!-- Bridge health (read-only): mirrors the broker→bridge→券商 chain. -->
      <div class="live-gate-row">
        <span class="gate-label">券商桥接</span>
        <span v-if="liveControl.bridge" class="muted">
          {{ liveControl.bridge.mode || "—" }}
          · 交易
          <span :class="liveControl.bridge.trader_connected ? 'ok' : 'bad'">{{
            liveControl.bridge.trader_connected ? "已连通" : "未连通"
          }}</span>
          · 行情
          <span :class="liveControl.bridge.datafeed_ok ? 'ok' : 'bad'">{{
            liveControl.bridge.datafeed_ok ? "正常" : "异常"
          }}</span>
          · 部署闸
          <span :class="liveControl.bridge.orders_allowed ? 'ok' : 'bad'">{{
            liveControl.bridge.orders_allowed ? "已放行" : "已锁定"
          }}</span>
        </span>
        <span v-else class="bad">桥接不可达（券商网关未运行 / 未连上）</span>
      </div>

      <div class="live-arm-row">
        <button
          :class="liveControl.orders_armed ? 'btn-danger' : 'btn-success'"
          :disabled="busy || !liveControl.live_capable"
          @click="toggleArm">
          {{ liveControl.orders_armed ? "撤防下单（回到观察）" : "布防实盘下单" }}
        </button>
        <span v-if="!liveControl.live_capable" class="bad asof">
          进程未带 <code>QUANTI_LIVE_ACK</code>：即便布防也不会真的下单，按此环境变量重启后端才可布防。
        </span>
      </div>

      <p class="muted live-control-note">
        此开关只拦<b>买入</b>：撤防时 Agent / 手动买入按「观察模式」拒单，
        卖出 / 止损 / 清仓<b>始终放行</b>。真实买入还需券商侧<b>部署闸</b>
        （bridge <code>orders_allowed</code>）与 <code>QUANTI_LIVE_ACK</code> 同时满足——
        本开关是这几道闸里唯一能在 UI 里实时切换的一道。
      </p>
    </div>

    <!-- Live status: intraday guard + per-holding stop price -->
    <div class="card" v-if="liveStatus">
      <div class="card-header">
        <h2>实盘状态</h2>
        <div class="muted">
          守护
          <span :class="liveStatus.guard.running ? 'ok' : 'bad'">{{
            liveStatus.guard.running ? "运行中"
              : (liveStatus.guard.enabled ? "已启用·未运行" : "未启用")
          }}</span>
          <span v-if="liveStatus.guard.enabled"> · 每 {{ liveStatus.guard.interval_sec }}s</span>
          · 数据源
          <span :class="liveStatus.guard.connected ? 'ok' : 'bad'">{{
            liveStatus.guard.connected === null ? "—"
              : (liveStatus.guard.connected ? "已连通" : "未连通")
          }}</span>
          · 交易时段 {{ liveStatus.guard.in_session ? "是" : "否" }}
          <span v-if="liveStatus.guard.llm_guard?.mode">
            · LLM 守护
            <span :class="liveStatus.guard.llm_guard.running ? 'ok' : 'bad'">{{
              liveStatus.guard.llm_guard.running ? "运行中" : "未运行"
            }}</span>
            · 每 {{ liveStatus.guard.llm_guard.interval_sec }}s
          </span>
          <span v-if="!liveStatus.is_live" class="asof">· 模拟盘(paper)</span>
        </div>
      </div>
      <div class="table-wrap" v-if="liveStatus.positions.length > 0">
        <table class="data-table">
          <thead>
            <tr>
              <th>代码</th><th>名称</th><th>数量</th>
              <th>买入价</th><th>当前价</th><th>止损价</th><th>加仓价</th><th>距止损</th><th>盈亏 %</th><th>进场策略</th><th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in liveStatus.positions" :key="p.code">
              <td>{{ p.code }}</td>
              <td>{{ p.name }}</td>
              <td>{{ p.quantity }}</td>
              <td>{{ p.avg_cost.toFixed(2) }}</td>
              <td>{{ p.current_price.toFixed(2) }}</td>
              <td>
                <template v-if="p.llm_plan && !p.stop_price">
                  <span class="bad">未设</span>
                  <span class="asof">LLM</span>
                </template>
                <template v-else>
                  {{ p.stop_price.toFixed(2) }}
                  <span class="asof">{{ p.llm_plan ? "LLM" : (p.atr_driven ? "ATR" : "地板") }}</span>
                </template>
              </td>
              <td>{{ p.llm_plan && p.add_price ? p.add_price.toFixed(2) : "—" }}</td>
              <td :class="stopDistance(p) <= 0.03 ? 'bad' : ''">{{ formatPct(stopDistance(p)) }}</td>
              <td :class="p.pnl_pct >= 0 ? 'up' : 'down'">{{ formatPct(p.pnl_pct) }}</td>
              <td>{{ p.entry_strategy || "—" }}</td>
              <td>
                <button class="btn-link" :disabled="busy"
                        :title="liveStatus.guard.in_session ? '交易时段:按当前实时价立即成交' : '非交易时段:挂单,次日开盘成交'"
                        @click="sellOne(p.code)">卖出</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-else class="empty">暂无持仓</div>
    </div>

    <!-- Portfolio + positions -->
    <div class="card">
      <div class="card-header">
        <h2>当前持仓</h2>
        <div class="muted">
          现金 ¥{{ formatMoney(portfolio?.cash ?? 0) }} / 市值 ¥{{
            formatMoney(portfolio?.market_value ?? 0)
          }}
          <span v-if="portfolio?.snapshot_date" class="asof">· 净值截至 {{ portfolio.snapshot_date }}</span>
        </div>
      </div>
      <div class="table-wrap" v-if="portfolio && portfolio.positions.length > 0">
        <table class="data-table">
        <thead>
          <tr>
            <th>代码</th>
            <th>名称</th>
            <th>行业</th>
            <th>数量</th>
            <th>成本</th>
            <th>买入时间</th>
            <th>现价</th>
            <th>最近更新</th>
            <th>市值</th>
            <th>盈亏</th>
            <th>盈亏 %</th>
            <th>分数</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in portfolio.positions" :key="p.code">
            <td>{{ p.code }}</td>
            <td>{{ p.name }}</td>
            <td>{{ p.industry || "—" }}</td>
            <td>{{ p.quantity }}</td>
            <td>{{ p.avg_cost.toFixed(2) }}</td>
            <td class="nowrap">{{ p.buy_date ?? "—" }}</td>
            <td>{{ p.current_price.toFixed(2) }}</td>
            <td :class="priceDateClass(p.price_date)">{{ p.price_date ?? "—" }}</td>
            <td>{{ formatMoney(p.market_value) }}</td>
            <td :class="p.pnl >= 0 ? 'up' : 'down'">{{ formatMoney(p.pnl) }}</td>
            <td :class="p.pnl_pct >= 0 ? 'up' : 'down'">{{ formatPct(p.pnl_pct) }}</td>
            <td :title="p.score == null ? '已掉出候选池(换仓视作最弱)' : ''">
              {{ p.score == null ? "—" : p.score.toFixed(2) }}
            </td>
            <td>
              <button class="btn-link" :disabled="busy"
                      :title="liveStatus?.guard.in_session ? '交易时段:按当前实时价立即成交' : '非交易时段:挂单,次日开盘成交'"
                      @click="sellOne(p.code)">卖出</button>
            </td>
          </tr>
        </tbody>
        </table>
      </div>
      <div v-else class="empty">暂无持仓</div>
    </div>

    <!-- Pending orders detail -->
    <div class="card" v-if="pendingOrders.length > 0">
      <div class="card-header">
        <h2>待成交订单 <span class="count-badge">{{ pendingOrders.length }}</span></h2>
        <div class="muted">挂单按 T+1 规则，于预计成交日的{{ fillBasisLabel }}成交</div>
      </div>
      <div class="table-wrap">
        <table class="data-table">
        <thead>
          <tr>
            <th>代码</th>
            <th>名称</th>
            <th>行业</th>
            <th>方向</th>
            <th>数量</th>
            <th>加入队列</th>
            <th>预计成交日</th>
            <th>状态</th>
            <th>进场策略</th>
            <th>分数</th>
            <th>理由</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="o in pendingOrders" :key="o.order_id">
            <td>{{ o.code }}</td>
            <td>{{ o.name }}</td>
            <td>{{ o.industry || "—" }}</td>
            <td :class="o.direction === 'buy' ? 'up' : 'down'">
              {{ o.direction === "buy" ? "买入" : "卖出" }}
            </td>
            <td>{{ o.quantity || "—" }}</td>
            <td class="nowrap">{{ formatDateTime(o.created_at) }}</td>
            <td class="nowrap">{{ o.expected_fill_date ?? "—" }}</td>
            <td>
              <span :class="o.bar_available ? 'tag tag-ready' : 'tag tag-wait'">
                {{ o.bar_available ? "待下轮成交" : "等待行情" }}
              </span>
              <span class="ttl-hint">
                已等 {{ o.trading_days_pending ?? 0 }}/{{ o.ttl_trading_days }} 交易日
              </span>
            </td>
            <td>{{ o.entry_strategy || "—" }}</td>
            <td :title="o.score == null ? '已掉出候选池' : ''">
              {{ o.score == null ? "—" : o.score.toFixed(2) }}
            </td>
            <td class="reason-cell" :title="o.reason">{{ o.reason || "—" }}</td>
          </tr>
        </tbody>
        </table>
      </div>
    </div>

    <!-- Recent exits -->
    <div class="card" v-if="recentExits.length > 0">
      <div class="card-header">
        <h2>最近离场</h2>
        <div class="muted">止损 / 移动止盈 / 策略离场 触发的卖出</div>
      </div>
      <div class="table-wrap">
        <table class="data-table">
        <thead>
          <tr>
            <th>代码</th>
            <th>类型</th>
            <th>状态</th>
            <th>成交价</th>
            <th>时间</th>
            <th>原因</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="o in recentExits" :key="o.order_id">
            <td>{{ o.code }}</td>
            <td><span :class="exitTag(o)">{{ exitLabel(o) }}</span></td>
            <td>{{ o.status === "filled" ? "已成交" : (o.status === "pending" ? "待成交" : o.status) }}</td>
            <td>{{ o.filled_price ? o.filled_price.toFixed(2) : "—" }}</td>
            <td class="nowrap">{{ formatDateTime(o.filled_at || o.created_at) }}</td>
            <td class="reason-cell" :title="o.reason">{{ o.reason || "—" }}</td>
          </tr>
        </tbody>
        </table>
      </div>
    </div>

    <!-- Manual order -->
    <div class="card">
      <div class="card-header"><h2>手动下单</h2></div>
      <div class="manual-row">
        <input v-model="manualCode" placeholder="股票代码 如 600519" />
        <select v-model="manualDirection">
          <option value="buy">买入</option>
          <option value="sell">卖出</option>
        </select>
        <input type="number" step="0.05" min="0.05" max="1" v-model.number="manualStrength" />
        <button class="btn-primary" :disabled="busy" @click="placeManual">下单</button>
      </div>
      <div class="muted">strength（0~1）：仅买入有效，作为现金的目标占比</div>
    </div>

    <!-- Hyperopt: parameter optimize card -->
    <div class="card">
      <div class="card-header">
        <h2>参数优化</h2>
        <button class="btn-secondary" :disabled="optimizing" @click="startOptimize">
          {{ optimizing ? `优化中 ${optProgress.current}/${optProgress.total} ${optProgress.strategy}` : "运行优化" }}
        </button>
      </div>
      <table v-if="tuned.length">
        <thead><tr><th>策略</th><th>默认 OOS</th><th>调优 OOS</th><th>采纳</th><th>参数</th><th>组合</th><th>时间</th></tr></thead>
        <tbody>
          <tr v-for="t in tuned" :key="t.strategy_name">
            <td>{{ t.strategy_name }}</td>
            <td>{{ t.baseline_oos_sharpe?.toFixed(2) }}</td>
            <td>{{ t.oos_sharpe?.toFixed(2) }}</td>
            <td>{{ t.accepted ? "✓" : "—" }}</td>
            <td>{{ t.accepted ? JSON.stringify(t.params) : "默认" }}</td>
            <td>{{ t.n_combos }}</td>
            <td>{{ t.tuned_at?.slice(0, 16).replace("T", " ") }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">尚未优化。点击"运行优化"在样本外验证各策略参数。</p>
    </div>

    <!-- Last evaluation -->
    <div class="card" v-if="agent && agent.last_evaluations.length > 0">
      <div class="card-header">
        <h2>最近策略评估</h2>
        <div class="muted">选定:<b>{{ displayStrategy(agent.last_strategy) }}</b></div>
      </div>
      <p class="muted" style="margin-top:0">
        数字为<b>样本外</b>(walk-forward,真实可信);标
        <span class="count-badge">样本内</span>
        的行无样本外数据,回退到样本内/已调优值,会显著偏乐观。
      </p>
      <div class="table-wrap">
        <table class="data-table">
        <thead>
          <tr>
            <th>策略</th>
            <th>年化</th>
            <th>最大回撤</th>
            <th>夏普</th>
            <th>成交</th>
            <th>得分</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="e in agent.last_evaluations" :key="e.strategy_name">
            <td>
              <b v-if="e.strategy_name === agent.last_strategy">{{ displayStrategy(e.strategy_name) }}</b>
              <span v-else>{{ displayStrategy(e.strategy_name) }}</span>
              <span v-if="tunedNames.has(e.strategy_name)" class="count-badge">已调优</span>
              <span v-if="!e.n_folds" class="count-badge">样本内</span>
            </td>
            <td :class="oosOr(e, 'annual_return') >= 0 ? 'up' : 'down'">{{ formatPct(oosOr(e, 'annual_return')) }}</td>
            <td class="down">{{ formatPct(oosOr(e, 'max_drawdown')) }}</td>
            <td>{{ oosOr(e, 'sharpe').toFixed(2) }}</td>
            <td>{{ e.n_folds ? e.oos_trades : e.total_trades }}</td>
            <td>{{ e.score.toFixed(3) }}</td>
          </tr>
        </tbody>
        </table>
      </div>
    </div>

    <!-- Factor Mining (LLM) -->
    <div class="card">
      <div class="card-header">
        <h2>因子挖掘 (LLM)</h2>
        <button class="btn-secondary" :disabled="mining" @click="startMine">
          {{ mining && !rescoring ? `挖掘中 ${mineProgress.current}/${mineProgress.total}` : "运行挖掘" }}
        </button>
        <button class="btn-secondary" :disabled="mining" @click="startRescore" title="不调 LLM，按当前数据重算已有因子的 IC 并刷新采纳">
          {{ rescoring ? `重评中 ${mineProgress.current}/${mineProgress.total}` : "重评已有" }}
        </button>
      </div>
      <label class="master-toggle">
        <input type="checkbox" v-model="useGenerated" @change="toggleMaster" />
        本账户实盘启用生成因子（默认关；开启后已采纳且启用的因子参与下单排名）
      </label>
      <table v-if="generated.length">
        <thead>
          <tr>
            <th>因子</th>
            <th>表达式</th>
            <th>训练IC</th>
            <th>OOS IC</th>
            <th>采纳</th>
            <th>启用</th>
            <th>生效中</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="f in generated" :key="f.name">
            <td>{{ f.name }}</td>
            <td><code>{{ f.expr_str }}</code></td>
            <td>{{ f.train_ic?.toFixed(3) }}</td>
            <td>{{ f.oos_ic?.toFixed(3) }}</td>
            <td>{{ f.accepted ? "✓" : "—" }}</td>
            <td><input type="checkbox" v-model="f.enabled" @change="toggleFactor(f)" /></td>
            <td>{{ f.accepted && f.enabled && useGenerated ? "● 生效" : "—" }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">尚未挖掘。点击"运行挖掘"让 LLM 提因子，IC 闸门筛选后入库。</p>
    </div>

    <!-- LLM 全权模式:tick 全流程时间线(候选→LLM→校验→执行→点位落库) -->
    <div class="card" v-if="tickFlow.length">
      <div class="card-header">
        <h2>LLM tick 流程</h2>
        <div class="muted">每日决策与盘中守护的分阶段执行流(最近 {{ tickFlow.length }} 轮)</div>
      </div>
      <div class="tick-flow">
        <div class="tick-group" v-for="g in tickFlow" :key="g.ts">
          <div class="tick-group-head">
            <span class="kind">{{ g.phase === "guard" ? "盘中守护" : "每日决策" }}</span>
            <span class="ts">{{ formatTs(g.ts) }}</span>
          </div>
          <div class="tick-step" v-for="s in g.steps" :key="s.id">
            <span class="tick-stage">{{ s.stage }}</span>
            <span class="tick-summary">{{ s.summary }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- Decision log -->
    <div class="card">
      <div class="card-header">
        <h2>决策日志</h2>
        <button class="btn-link" @click="loadDecisions">刷新</button>
      </div>
      <div class="decision-list">
        <div class="decision" v-for="d in decisions" :key="d.id" :class="kindClass(d.kind)">
          <div class="decision-meta">
            <span class="kind">{{ d.kind }}</span>
            <span class="ts">{{ formatTs(d.ts) }}</span>
          </div>
          <div class="decision-body">{{ d.summary }}</div>
          <!-- LLM cycle: surface Claude's reasoning so users can see WHY it chose what it did. -->
          <div
            v-if="d.kind === 'llm_cycle' && (d.details as any)?.reasoning"
            class="decision-reasoning"
          >
            <span class="reasoning-label">理由</span>
            {{ (d.details as any).reasoning }}
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";
import {
  agentStart,
  agentStop,
  agentRestart,
  agentTick,
  fetchAgentDecisions,
  fetchAgentStatus,
  fetchGoal,
  fetchOrders,
  fetchPendingOrders,
  fetchPortfolio,
  fetchLiveStatus,
  fetchScreeners,
  fetchStrategies,
  manualOrder,
  resetPortfolio,
  updateGoal,
  runOptimizeAsync,
  fetchOptimizeStatus,
  fetchTunedParams,
  runMineAsync,
  runRescoreAsync,
  fetchMineStatus,
  fetchGeneratedFactors,
  setFactorEnabled,
  fetchLiveControl,
  setLiveOrdersArmed,
  type AgentStatus,
  type DecisionRecord,
  type GeneratedFactor,
  type Goal,
  type OptimizeResultItem,
  type OrderRecord,
  type PendingOrderDetail,
  type Portfolio,
  type LiveStatus,
  type LiveControl,
  type ScreenerInfo,
  type StrategyInfo,
} from "../api/client";

const goalDraft = reactive<Goal>({
  target_annual_return: 0.2,
  max_drawdown: -0.2,
  risk_tolerance: "medium",
  universe_pool: "",
  screener_name: "",
  strategy_name: "",
  params: {},
  rebalance_freq: "daily",
  enabled: false,
});

// Agent mode + P1-P4 upgrade switches. Mirrors a subset of goalDraft.params
// for direct UI binding. Synced both directions: loadAll() pulls server →
// advParams via syncAdvFromParams; saveGoal() pushes advParams → goalDraft.params
// via syncParamsFromAdv right before sending.
type AgentMode = "rule" | "ensemble" | "llm" | "llm_full";

// 各供应商的模型预设(id 会原样写入 goal.params.llm_model)。DeepSeek 对
// claude-* id 自动回落自家默认,选错供应商侧的 id 不会炸;Anthropic 侧则
// 必须是有效 claude id。
const LLM_MODEL_PRESETS: Record<
  "anthropic" | "deepseek",
  { id: string; desc: string }[]
> = {
  deepseek: [
    { id: "deepseek-v4-flash", desc: "默认 · 快,function-calling 全支持" },
    { id: "deepseek-v4-pro", desc: "思考最深 · 慢且贵,适合每日决策" },
    { id: "deepseek-chat", desc: "legacy 别名(当前由 v4-flash 服务)" },
  ],
  anthropic: [
    { id: "claude-sonnet-4-6", desc: "后端默认 · 均衡" },
    { id: "claude-sonnet-5", desc: "新一代 Sonnet · 编码/agentic 更强" },
    { id: "claude-opus-5", desc: "最强推理 · 最贵" },
    { id: "claude-haiku-4-5", desc: "最快最便宜 · 简单任务" },
  ],
};
const advParams = reactive({
  agent_mode: "rule" as AgentMode,
  ensemble_enabled: false,
  industry_neutral: false,
  liquidity_filter: false,
  wf_enabled: true, // default-on
  // DSR 过拟合门(独立于预设,默认关):赢家夏普做多重检验紧缩,低于 dsr_min 退等权。
  dsr_gate: false,
  dsr_min: 0.85, // 校准回测最优(54 月 OOS,见 scripts/dsr_calibration.py)
  // 市场 regime(default-on,口径同 wf_enabled:键缺失=开,显式 false 才关)。
  // detect = tick 第一步只读观测;in_prompt = 客观指标进裁判 LLM 上下文。
  regime_detect: true,
  regime_in_prompt: true,
  // LLM enhancement layer (all default-off; ②③④ only apply in LLM mode,
  // ① sentiment also applies in ensemble mode).
  llm_provider: "anthropic" as "anthropic" | "deepseek",
  // 模型选择:"" = 用后端默认;"__custom__" = 手输(实际值在 llm_model_custom)。
  llm_model: "",
  llm_model_custom: "",
  sentiment_enabled: false,
  sentiment_blend: 0.2,
  llm_debate: false,
  llm_debate_rounds: 1,
  llm_risk_debate: false,
  llm_reflection: false,
  // LLM 全权模式:盘中 LLM 守护间隔 + 单轮订单上限。
  llm_guard_interval_sec: 300,
  llm_max_orders: 10,
  // 每日定时调度（cron-lite）：开启后每天在 daily_run_time 跑一次，
  // 否则按 tick_interval_sec 间隔跑。daily_schedule_enabled 仅 UI 本地状态。
  daily_schedule_enabled: false,
  daily_run_time: "09:35",
  daily_trading_days_only: true,
});

function syncAdvFromParams() {
  const p = (goalDraft.params || {}) as Record<string, unknown>;
  const ensemble = !!p.ensemble_enabled;
  const isLLM = p.agent_mode === "llm";
  const isLLMFull = p.agent_mode === "llm_full";
  advParams.agent_mode = isLLMFull ? "llm_full"
    : isLLM ? "llm" : ensemble ? "ensemble" : "rule";
  advParams.ensemble_enabled = ensemble || isLLM || isLLMFull;
  advParams.industry_neutral = !!p.industry_neutral;
  advParams.liquidity_filter = !!p.liquidity_filter;
  advParams.wf_enabled = p.wf_enabled !== false; // default true if absent
  advParams.dsr_gate = !!p.dsr_gate;
  advParams.dsr_min = typeof p.dsr_min === "number" ? p.dsr_min : 0.85;
  advParams.regime_detect = p.regime_detect !== false; // default true if absent
  advParams.regime_in_prompt = p.regime_in_prompt !== false;
  advParams.llm_provider = p.llm_provider === "deepseek" ? "deepseek" : "anthropic";
  const model = typeof p.llm_model === "string" ? p.llm_model : "";
  if (!model) {
    advParams.llm_model = "";
    advParams.llm_model_custom = "";
  } else if (LLM_MODEL_PRESETS[advParams.llm_provider].some((m) => m.id === model)) {
    advParams.llm_model = model;
    advParams.llm_model_custom = "";
  } else {
    advParams.llm_model = "__custom__";
    advParams.llm_model_custom = model;
  }
  advParams.sentiment_enabled = !!p.sentiment_enabled;
  advParams.sentiment_blend =
    typeof p.sentiment_blend === "number" ? p.sentiment_blend : 0.2;
  advParams.llm_debate = !!p.llm_debate;
  advParams.llm_debate_rounds =
    typeof p.llm_debate_rounds === "number" ? p.llm_debate_rounds : 1;
  advParams.llm_risk_debate = !!p.llm_risk_debate;
  advParams.llm_reflection = !!p.llm_reflection;
  advParams.llm_guard_interval_sec =
    typeof p.llm_guard_interval_sec === "number" ? p.llm_guard_interval_sec : 300;
  advParams.llm_max_orders =
    typeof p.llm_max_orders === "number" ? p.llm_max_orders : 10;
  const drt = typeof p.daily_run_time === "string" ? p.daily_run_time : "";
  advParams.daily_schedule_enabled = drt !== "";
  if (drt) advParams.daily_run_time = drt;
  advParams.daily_trading_days_only = p.daily_trading_days_only !== false;
}

function syncParamsFromAdv() {
  // Preserve any unknown keys the user may have set via API/MCP.
  const existing = (goalDraft.params || {}) as Record<string, unknown>;
  const params: Record<string, unknown> = {
    ...existing,
    agent_mode:
      advParams.agent_mode === "llm" || advParams.agent_mode === "llm_full"
        ? advParams.agent_mode
        : "",
    ensemble_enabled:
      advParams.ensemble_enabled ||
      advParams.agent_mode === "ensemble" ||
      advParams.agent_mode === "llm" ||
      advParams.agent_mode === "llm_full",
    industry_neutral: advParams.industry_neutral,
    liquidity_filter: advParams.liquidity_filter,
    wf_enabled: advParams.wf_enabled,
    dsr_gate: advParams.dsr_gate,
    dsr_min: advParams.dsr_min,
    regime_detect: advParams.regime_detect,
    regime_in_prompt: advParams.regime_in_prompt,
    llm_provider: advParams.llm_provider,
    sentiment_enabled: advParams.sentiment_enabled,
    sentiment_blend: advParams.sentiment_blend,
    llm_debate: advParams.llm_debate,
    llm_debate_rounds: advParams.llm_debate_rounds,
    llm_risk_debate: advParams.llm_risk_debate,
    llm_reflection: advParams.llm_reflection,
    llm_guard_interval_sec: advParams.llm_guard_interval_sec,
    llm_max_orders: advParams.llm_max_orders,
  };
  if (advParams.daily_schedule_enabled && advParams.daily_run_time) {
    params.daily_run_time = advParams.daily_run_time;
    params.daily_trading_days_only = advParams.daily_trading_days_only;
  } else {
    delete params.daily_run_time;
    delete params.daily_trading_days_only;
  }
  // 模型选择:空 = 用后端默认。必须删键而非写空串——空串会原样传给
  // Anthropic client 直接 400(DeepSeek client 对 falsy 会回落默认)。
  const effModel =
    advParams.llm_model === "__custom__"
      ? advParams.llm_model_custom
      : advParams.llm_model;
  if (effModel) {
    params.llm_model = effModel;
  } else {
    delete params.llm_model;
  }
  goalDraft.params = params;
}

// 服务端确认的调度快照（只在 loadAll 里从已拉取的 goalDraft.params 写入）。
// 顶部状态卡（下次运行时刻 / 模式副标）读它 = 反映正在运行的 agent；
// 编辑表单读 advParams = 草稿。saveGoal 也用它做「调度是否变化」判断。
const activeSchedule = ref({ enabled: false, time: "", tradingOnly: true });
function captureActiveSchedule() {
  const p = (goalDraft.params || {}) as Record<string, unknown>;
  const time = typeof p.daily_run_time === "string" ? p.daily_run_time : "";
  activeSchedule.value = {
    enabled: time !== "",
    time,
    tradingOnly: p.daily_trading_days_only !== false,
  };
}

const llmModelOptions = computed(() => LLM_MODEL_PRESETS[advParams.llm_provider]);
const llmModelCustom = computed(() => advParams.llm_model === "__custom__");

function onProviderChange() {
  // 切供应商时,若选中的是旧供应商的预设 id,重置回「默认」;
  // 自定义手输值保留(用户显式写的,不背着改)。
  if (
    advParams.llm_model &&
    advParams.llm_model !== "__custom__" &&
    !LLM_MODEL_PRESETS[advParams.llm_provider].some(
      (m) => m.id === advParams.llm_model,
    )
  ) {
    advParams.llm_model = "";
  }
}

function applyPreset(mode: AgentMode) {
  advParams.agent_mode = mode;
  if (mode === "rule") {
    advParams.ensemble_enabled = false;
    advParams.industry_neutral = false;
    advParams.liquidity_filter = false;
    advParams.wf_enabled = true;
  } else if (mode === "llm_full") {
    // 全权模式:候选管线只做流动性清洗 + 策略跑分,alpha 过滤(行业中性等)
    // 由 runtime 在该模式下自动旁路;行业集中度交给 LLM 判断。
    advParams.ensemble_enabled = true;
    advParams.industry_neutral = false;
    advParams.liquidity_filter = true;
    advParams.wf_enabled = true;
  } else {
    // ensemble and llm share the same selection-side configuration; LLM
    // just adds the Claude decision layer on top of those candidates.
    advParams.ensemble_enabled = true;
    advParams.industry_neutral = true;
    advParams.liquidity_filter = true;
    advParams.wf_enabled = true;
  }
}

const portfolio = ref<Portfolio | null>(null);
const liveStatus = ref<LiveStatus | null>(null);
const liveControl = ref<LiveControl | null>(null);
function stopDistance(p: { current_price: number; stop_price: number }): number {
  if (!p.current_price) return 0;
  return (p.current_price - p.stop_price) / p.current_price;  // headroom to stop
}
const agent = ref<AgentStatus | null>(null);
const decisions = ref<DecisionRecord[]>([]);

// LLM tick 流程时间线:tick_stage / llm_guard 事件按 tick_ts 聚组,倒序前 6 轮。
interface TickStep { id: number; stage: string; summary: string }
interface TickGroup { ts: string; phase: string; steps: TickStep[] }
const tickFlow = computed<TickGroup[]>(() => {
  const kinds = new Set(["tick_stage", "llm_guard", "llm_guard_skip",
                         "llm_add_triggered"]);
  const groups = new Map<string, TickGroup>();
  for (const d of decisions.value) {
    if (!kinds.has(d.kind)) continue;
    const det = (d.details || {}) as Record<string, unknown>;
    const ts = (det.tick_ts as string) || d.ts;
    const phase = d.kind === "tick_stage"
      ? ((det.phase as string) || "tick")
      : "guard";
    const key = `${phase}:${ts}`;
    if (!groups.has(key)) groups.set(key, { ts, phase, steps: [] });
    groups.get(key)!.steps.push({
      id: d.id,
      stage: d.kind === "tick_stage" ? ((det.stage as string) || "?") : d.kind,
      summary: d.summary,
    });
  }
  const out = [...groups.values()];
  out.sort((a, b) => (a.ts < b.ts ? 1 : -1));
  // 组内按 id 升序 = 阶段实际发生顺序。
  for (const g of out) g.steps.sort((a, b) => a.id - b.id);
  return out.slice(0, 6);
});
const strategies = ref<StrategyInfo[]>([]);
const screeners = ref<ScreenerInfo[]>([]);
const pendingOrders = ref<PendingOrderDetail[]>([]);
const orders = ref<OrderRecord[]>([]);

// Optimize (walk-forward hyperopt) state
const tuned = ref<OptimizeResultItem[]>([]);
const optimizing = ref(false);
const optProgress = ref<{ current: number; total: number; strategy: string }>(
  { current: 0, total: 0, strategy: "" });
let optTimer: number | undefined;

const tunedNames = computed(() =>
  new Set(tuned.value.filter(t => t.accepted).map(t => t.strategy_name)));

const loadTuned = async () => { tuned.value = (await fetchTunedParams()).data; };
const startOptimize = async () => {
  optimizing.value = true;
  try {
    const { data } = await runOptimizeAsync();
    const jobId = data.job_id;
    optTimer = window.setInterval(async () => {
      const s = (await fetchOptimizeStatus(jobId)).data;
      optProgress.value = { current: s.current, total: s.total, strategy: s.current_strategy };
      tuned.value = s.results;
      if (s.status === "done" || s.status === "error") {
        window.clearInterval(optTimer);
        optimizing.value = false;
      }
    }, 1500);
  } catch (e) {
    console.error("optimize launch failed", e);
    optimizing.value = false;
  }
};

// ── Factor Mining ──────────────────────────────────────────────────────────
const generated = ref<GeneratedFactor[]>([]);
const mining = ref(false);
const rescoring = ref(false); // re-score reuses the mining busy flag + progress
const mineProgress = ref<{ current: number; total: number }>({ current: 0, total: 0 });
let mineTimer: number | undefined;
const useGenerated = ref(false); // master switch: goal.params.use_generated_factors

const loadGenerated = async () => {
  generated.value = (await fetchGeneratedFactors()).data;
};
const loadMaster = async () => {
  const g = (await fetchGoal()).data;
  useGenerated.value = Boolean((g.params || {})["use_generated_factors"]);
};
const toggleMaster = async () => {
  const g = (await fetchGoal()).data;
  const params = { ...(g.params || {}), use_generated_factors: useGenerated.value };
  await updateGoal({ params });
};
const toggleFactor = async (f: GeneratedFactor) => {
  await setFactorEnabled(f.name, f.enabled);
  await loadGenerated();
};
const startMine = async () => {
  mining.value = true;
  try {
    const jid = (await runMineAsync()).data.job_id;
    if (mineTimer) window.clearInterval(mineTimer); // never stack pollers
    mineTimer = window.setInterval(async () => {
      const s = (await fetchMineStatus(jid)).data;
      mineProgress.value = { current: s.current, total: s.total };
      generated.value = s.results;
      if (s.status === "done" || s.status === "error") {
        window.clearInterval(mineTimer);
        mining.value = false;
        if (s.status === "done") await loadGenerated();
      }
    }, 2000);
  } catch (e) {
    console.error(e);
    mining.value = false;
  }
};
const startRescore = async () => {
  mining.value = true;
  rescoring.value = true;
  try {
    const jid = (await runRescoreAsync()).data.job_id;
    if (mineTimer) window.clearInterval(mineTimer); // never stack pollers
    mineTimer = window.setInterval(async () => {
      const s = (await fetchMineStatus(jid)).data;
      mineProgress.value = { current: s.current, total: s.total };
      generated.value = s.results;
      if (s.status === "done" || s.status === "error") {
        window.clearInterval(mineTimer);
        mining.value = false;
        rescoring.value = false;
        if (s.status === "done") await loadGenerated();
      }
    }, 2000);
  } catch (e) {
    console.error(e);
    mining.value = false;
    rescoring.value = false;
  }
};

// Exits = sells. risk_exit is the stop-loss/take-profit/strategy-exit path;
// other sells (manual, etc.) still count as an exit worth showing.
const recentExits = computed(() =>
  orders.value.filter((o) => o.direction === "sell").slice(0, 12));

function exitLabel(o: OrderRecord): string {
  const r = o.reason || "";
  if (r.includes("止损")) return "止损";
  if (r.includes("移动止盈") || r.includes("止盈")) return "止盈";
  if (r.includes("策略离场") || o.strategy_name !== "risk_exit") return o.strategy_name === "risk_exit" ? "策略离场" : "卖出";
  return "离场";
}
function exitTag(o: OrderRecord): string {
  const label = exitLabel(o);
  if (label === "止损") return "tag tag-wait";   // amber
  if (label === "止盈") return "tag tag-ready";  // green
  return "tag";
}

const saving = ref(false);
const ticking = ref(false);
const busy = ref(false);
const message = ref("");
const messageError = ref(false);

const manualCode = ref("");
const manualDirection = ref<"buy" | "sell">("buy");
const manualStrength = ref(0.2);

let timer: number | null = null;

function setMessage(msg: string, err = false) {
  message.value = msg;
  messageError.value = err;
  setTimeout(() => (message.value = ""), 6000);
}

function formatMoney(n: number) {
  return Number(n || 0).toLocaleString("zh-CN", { maximumFractionDigits: 0 });
}
function formatPct(n: number) {
  if (!isFinite(n)) return "-";
  return (n * 100).toFixed(2) + "%";
}
// Show the out-of-sample metric (real, walk-forward) when available, else fall
// back to the in-sample one. The selector scores on OOS, so the card should
// too — IS numbers are tuned/single-window and read far too optimistic.
function oosOr(e: any, base: "annual_return" | "max_drawdown" | "sharpe"): number {
  const v = e?.n_folds ? e["oos_" + base] : e?.[base];
  return typeof v === "number" ? v : 0;
}
function formatTs(ts: string) {
  return ts.replace("T", " ").slice(0, 19);
}
// Queued time — date + HH:MM is enough; seconds are noise here.
function formatDateTime(ts: string) {
  if (!ts) return "—";
  return ts.replace("T", " ").slice(0, 16);
}
// Flag a stale mark price: if the position's price date trails the
// portfolio snapshot date, the row hasn't been re-marked to the latest bar.
function priceDateClass(d: string | null) {
  if (!d) return "td-muted";
  if (portfolio.value?.snapshot_date && d < portfolio.value.snapshot_date)
    return "stale-date";
  return "";
}
const fillBasisLabel = computed(() =>
  pendingOrders.value[0]?.fill_price_basis === "close" ? "收盘价" : "开盘价");

const pnlClass = computed(() => {
  if (!portfolio.value) return "";
  return portfolio.value.pnl_pct >= 0 ? "up" : "down";
});

const targetGapStr = computed(() => {
  if (!portfolio.value || !goalDraft.target_annual_return) return "-";
  const gap = portfolio.value.pnl_pct - goalDraft.target_annual_return;
  return `${(gap * 100).toFixed(2)}%`;
});

const agentStatusStr = computed(() => {
  if (!agent.value) return "未启动";
  if (agent.value.running) return "运行中";
  if (agent.value.enabled) return "启用但未运行";
  return "已停止";
});

const agentStatusClass = computed(() => {
  if (!agent.value) return "";
  return agent.value.running ? "up" : "muted-card";
});

// Live clock for the "运行时间" card — ticked every 20s (minute-granularity
// display, so finer updates would just churn the DOM for nothing).
const nowTs = ref(Date.now());
let clockTimer: number | null = null;

function _fmtDuration(ms: number): string {
  if (ms < 60_000) return "刚启动";
  const totalMin = Math.floor(ms / 60_000);
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return h === 0 ? `${m} 分钟` : `${h} 小时 ${m} 分`;
}
function _hhmm(ts: string | null): string {
  return ts ? ts.slice(11, 16) : "—"; // HH:MM from naive ISO
}

const uptimeStr = computed(() => {
  const a = agent.value;
  if (!a || !a.running || !a.started_at) return "—";
  return _fmtDuration(nowTs.value - Date.parse(a.started_at));
});
const lastTickStr = computed(() => _hhmm(agent.value?.last_tick_at ?? null));
const nextTickStr = computed(() => {
  const a = agent.value;
  if (!a || !a.running) return "—";
  // 每日定时模式：显示下一个 daily_run_time 时刻（服务端确认的调度，非草稿）。
  if (activeSchedule.value.enabled && activeSchedule.value.time) {
    const parts = activeSchedule.value.time.split(":");
    const h = Number(parts[0]);
    const m = Number(parts[1]);
    if (parts[1] !== undefined && !Number.isNaN(h) && !Number.isNaN(m)) {
      const now = new Date(nowTs.value);
      const next = new Date(now);
      next.setHours(h, m, 0, 0);
      if (next <= now) next.setDate(next.getDate() + 1); // 已过则滚到明天
      const sameDay = next.getDate() === now.getDate();  // 滚动后日期不同 => 明天
      const hh = String(next.getHours()).padStart(2, "0");
      const mm = String(next.getMinutes()).padStart(2, "0");
      return `${hh}:${mm}${sameDay ? "" : "(明天)"}`;
    }
  }
  // 间隔模式（原逻辑不变）。
  if (!a.last_tick_at || !a.tick_interval_sec) return "—";
  const next = new Date(Date.parse(a.last_tick_at) + a.tick_interval_sec * 1000);
  return `${String(next.getHours()).padStart(2, "0")}:${String(next.getMinutes()).padStart(2, "0")}`;
});
const scheduleSubStr = computed(() => {
  if (!activeSchedule.value.enabled || !activeSchedule.value.time) return "";
  const days = activeSchedule.value.tradingOnly ? "仅交易日" : "含周末";
  return `每日 ${activeSchedule.value.time} · ${days}`;
});

// Lookup table: stable name → display label. Built reactively from the
// loaded strategy/screener lists. Falls back to the stable name if the
// list hasn't loaded yet or the lookup misses (e.g. user-removed plugin).
const strategyNameMap = computed(() => {
  const m = new Map<string, string>();
  for (const s of strategies.value) {
    if (s.name_zh) m.set(s.name, s.name_zh);
  }
  return m;
});
function displayStrategy(name: string): string {
  if (!name) return "";
  if (name === "ensemble") return "ensemble";  // synthetic, not in the list
  if (name === "llm") return "LLM";
  return strategyNameMap.value.get(name) || name;
}

// Mode badge derived from the live (post-save) advParams state.
// If the user pinned a strategy, all params are inert — show "钉死策略"
// so they're not confused by a mode badge that does nothing.
const modeLabel = computed(() => {
  if (goalDraft.strategy_name) return `钉死: ${displayStrategy(goalDraft.strategy_name)}`;
  if (advParams.agent_mode === "llm") return "LLM 决策";
  if (advParams.agent_mode === "ensemble") return "集成 (ensemble)";
  return "经典 (rule)";
});

const modeClass = computed(() => {
  if (goalDraft.strategy_name) return "mode-pinned";
  if (advParams.agent_mode === "llm") return "mode-llm";
  if (advParams.agent_mode === "ensemble") return "mode-ensemble";
  return "mode-rule";
});

const pendingCardClass = computed(() => {
  const n = agent.value?.pending_orders ?? 0;
  if (n > 10) return "pending-heavy";
  if (n > 0) return "pending-active";
  return "";
});

function kindClass(kind: string) {
  if (kind === "trade") return "kind-trade";
  if (kind === "risk_reject") return "kind-warn";
  if (kind === "cycle") return "kind-info";
  if (kind === "agent_start" || kind === "agent_stop") return "kind-meta";
  if (kind === "strategy_pick" || kind === "strategy_ensemble") return "kind-info";
  if (kind === "llm_cycle") return "kind-llm";
  if (kind === "agent_error") return "kind-error";
  if (kind === "order_queued") return "kind-pending";
  if (kind === "order_filled_pending") return "kind-trade";  // = real fill
  if (kind === "order_expired_pending") return "kind-meta";
  return "";
}

// Status / portfolio / decisions / option lists — safe to refresh on a poll.
// Does NOT touch goalDraft/advParams, so the background timer never overwrites
// the parameters the user is editing.
async function loadStatus() {
  const [p, a, d, str, scr, pend, ord, live, lc] = await Promise.all([
    fetchPortfolio(),
    fetchAgentStatus(),
    fetchAgentDecisions(50),
    fetchStrategies(),
    fetchScreeners(),
    fetchPendingOrders(),
    fetchOrders(200),
    fetchLiveStatus().catch(() => null),
    fetchLiveControl().catch(() => null),
  ]);
  portfolio.value = p.data;
  agent.value = a.data;
  decisions.value = d.data;
  strategies.value = str.data;
  screeners.value = scr.data;
  pendingOrders.value = pend.data;
  orders.value = ord.data;
  liveStatus.value = live?.data ?? null;
  liveControl.value = lc?.data ?? null;
}

// Loads the editable goal form FROM the server. Call only on mount and right
// after an explicit save — never on the poll (else it clobbers unsaved edits).
async function loadAll() {
  const g = await fetchGoal();
  Object.assign(goalDraft, g.data);
  syncAdvFromParams();
  captureActiveSchedule();
  await loadStatus();
}

async function loadDecisions() {
  const d = await fetchAgentDecisions(50);
  decisions.value = d.data;
}

async function saveGoal() {
  saving.value = true;
  try {
    // Push the mode/upgrade switches into goalDraft.params right before sending.
    syncParamsFromAdv();
    const p = goalDraft.params as Record<string, unknown>;
    const newTime = typeof p.daily_run_time === "string" ? p.daily_run_time : "";
    const newTradingOnly = p.daily_trading_days_only !== false;
    const scheduleChanged =
      newTime !== activeSchedule.value.time ||
      newTradingOnly !== activeSchedule.value.tradingOnly;
    await updateGoal(goalDraft);
    if (scheduleChanged && agent.value?.running) {
      await agentRestart();
      setMessage("目标已保存；调度已更新，Agent 已重启");
    } else {
      setMessage("目标已保存");
    }
    await loadAll(); // 刷新状态并通过 captureActiveSchedule 更新服务端确认调度快照
  } catch (e: any) {
    setMessage("保存失败: " + (e?.message ?? e), true);
  } finally {
    saving.value = false;
  }
}

async function startAgent() {
  busy.value = true;
  try {
    await agentStart();
    setMessage("Agent 已启动");
    await loadStatus();
  } finally {
    busy.value = false;
  }
}

async function stopAgent() {
  busy.value = true;
  try {
    await agentStop();
    setMessage("Agent 已停止");
    await loadStatus();
  } finally {
    busy.value = false;
  }
}

async function forceTick() {
  ticking.value = true;
  try {
    const r = await agentTick();
    setMessage("执行完成: " + JSON.stringify(r.data).slice(0, 200));
    await loadStatus();
  } catch (e: any) {
    setMessage("执行失败: " + (e?.message ?? e), true);
  } finally {
    ticking.value = false;
  }
}

async function reset() {
  if (!confirm("确认重置组合？所有持仓与交易将被清空。")) return;
  busy.value = true;
  try {
    const cash = Number(prompt("新的初始资金?", "1000000") || "1000000");
    await resetPortfolio(cash);
    await loadStatus();
    setMessage("组合已重置");
  } finally {
    busy.value = false;
  }
}

// 实盘下单 arm/disarm switch (阶段 C 观察 ↔ 阶段 D 下单). Arming is real money —
// confirm first; disarming is always allowed (never blocks exits).
async function toggleArm() {
  const lc = liveControl.value;
  if (!lc) return;
  const next = !lc.orders_armed;
  if (next && !confirm(
    "确认【布防实盘下单】？之后 Agent / 手动买入会向券商提交真钱订单。\n" +
    "（卖出 / 止损 / 清仓不受此开关影响，始终放行。）")) return;
  busy.value = true;
  try {
    const r = await setLiveOrdersArmed(next);
    liveControl.value = { ...lc, orders_armed: r.data.orders_armed };
    setMessage(r.data.orders_armed
      ? "已布防：放行真钱买入" : "已撤防：观察模式，拒绝买入", !r.data.orders_armed);
  } catch (e: any) {
    setMessage("切换失败: " + (e?.message ?? e), true);
  } finally {
    busy.value = false;
  }
}

async function placeManual() {
  if (!manualCode.value.trim()) return;
  busy.value = true;
  try {
    const r = await manualOrder({
      code: manualCode.value.trim(),
      direction: manualDirection.value,
      strength: manualStrength.value,
      reason: "manual via UI",
    });
    setMessage(r.data.filled ? "下单成交" : "下单被拒（可能是风控/无数据/资金不足）",
      !r.data.filled);
    portfolio.value = r.data.snapshot;
  } catch (e: any) {
    setMessage("下单失败: " + (e?.message ?? e), true);
  } finally {
    busy.value = false;
  }
}

async function sellOne(code: string) {
  const inSession = liveStatus.value?.guard.in_session;
  const how = inSession ? "交易时段内将按当前实时价立即成交"
    : "非交易时段,挂单次日开盘成交";
  if (!confirm(`确认全部卖出 ${code} ?(${how})`)) return;
  busy.value = true;
  try {
    const r = await manualOrder({ code, direction: "sell", reason: "manual sell" });
    setMessage(
      r.data.status === "filled" ? `已按当前实时价卖出 ${code}`
        : r.data.status === "pending" ? `卖出已提交 ${code},尚未成交(挂单)`
          : "卖出被拒(T+1 冻结/无持仓/风控)",
      r.data.status === "rejected");
    portfolio.value = r.data.snapshot;
    await loadStatus();
  } catch (e: any) {
    setMessage("卖出失败: " + (e?.response?.data?.detail ?? e?.message ?? e), true);
  } finally {
    busy.value = false;
  }
}

onMounted(() => {
  loadAll();
  loadTuned();
  loadGenerated();
  loadMaster();
  timer = window.setInterval(loadStatus, 15000);
  clockTimer = window.setInterval(() => (nowTs.value = Date.now()), 20000);
});

onUnmounted(() => {
  if (timer !== null) window.clearInterval(timer);
  if (clockTimer !== null) window.clearInterval(clockTimer);
  if (optTimer !== undefined) window.clearInterval(optTimer);
  if (mineTimer !== undefined) window.clearInterval(mineTimer);
});
</script>

<style scoped>
.agent-page {
  display: flex;
  flex-direction: column;
  gap: 24px;
}
.page-header h1 {
  margin: 0;
  font-size: 28px;
  font-weight: 600;
  letter-spacing: -0.5px;
}
.page-desc {
  color: var(--color-text-secondary);
  margin-top: 6px;
}
.stats-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 16px;
}
.stat-card {
  background: var(--color-surface, #fff);
  border: 0.5px solid rgba(0, 0, 0, 0.08);
  border-radius: 12px;
  padding: 16px 18px;
}
.stat-label {
  font-size: 13px;
  color: var(--color-text-secondary);
}
.stat-value {
  display: block;
  font-size: 22px;
  font-weight: 600;
  margin-top: 4px;
}
.stat-value-sm {
  font-size: 16px;
}
.stat-sub {
  display: block;
  margin-top: 2px;
  font-size: 11px;
  color: var(--color-text-secondary);
}
.up {
  color: #c0392b;
}
.down {
  color: #16a34a;
}
/* status / health (traffic-light: green=ok, red=bad) — distinct from the
   price 涨红跌绿 above. Used for guard状态 / 数据源连通 / 距止损危险. */
.ok {
  color: #16a34a;
}
.bad {
  color: #c0392b;
}
.muted-card {
  opacity: 0.7;
}
.card {
  background: var(--color-surface, #fff);
  border: 0.5px solid rgba(0, 0, 0, 0.08);
  border-radius: 12px;
  padding: 20px;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
}
.card-header h2 {
  margin: 0;
  font-size: 18px;
}
.muted {
  color: var(--color-text-secondary);
  font-size: 13px;
}
.goal-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 14px;
}
.goal-grid label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
}
.goal-grid input,
.goal-grid select,
.manual-row input,
.manual-row select {
  padding: 8px 10px;
  border: 1px solid rgba(0, 0, 0, 0.12);
  border-radius: 8px;
  font-size: 14px;
}
.actions {
  display: flex;
  gap: 10px;
  margin-top: 16px;
  flex-wrap: wrap;
}
.btn-primary,
.btn-secondary,
.btn-success,
.btn-danger,
.btn-link {
  padding: 8px 16px;
  border-radius: 8px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  border: 0;
}
.btn-primary {
  background: #0071e3;
  color: white;
}
.btn-secondary {
  background: rgba(0, 0, 0, 0.06);
  color: #333;
}
.btn-success {
  background: #16a34a;
  color: white;
}
.btn-danger {
  background: #c0392b;
  color: white;
}
.btn-link {
  background: transparent;
  color: #0071e3;
  padding: 4px 8px;
}
.btn-primary:disabled,
.btn-success:disabled,
.btn-danger:disabled,
.btn-secondary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.sync-msg {
  margin-top: 12px;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 13px;
}
.sync-msg.success {
  background: rgba(22, 163, 74, 0.1);
  color: #16a34a;
}
.sync-msg.error {
  background: rgba(192, 57, 43, 0.1);
  color: #c0392b;
}
/* Horizontal scroll on narrow screens — matches the .table-wrap convention
   already used by Dashboard/Backtest/Screener/Pool. Without it the wide
   tables (10-col holdings, pending orders) wrap Chinese text char-by-char
   on mobile. Desktop is unaffected (table fits, nothing to scroll). */
.table-wrap {
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}
.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.data-table th,
.data-table td {
  text-align: left;
  padding: 8px 10px;
  border-bottom: 0.5px solid rgba(0, 0, 0, 0.06);
  /* Keep each cell on one line so the table grows to its natural width and
     scrolls inside .table-wrap on mobile, instead of wrapping Chinese names
     char-by-char. .reason-cell overrides with its own ellipsis clamp. */
  white-space: nowrap;
}
.data-table th {
  background: rgba(0, 0, 0, 0.02);
  font-weight: 500;
}
.empty {
  color: var(--color-text-secondary);
  padding: 16px;
  text-align: center;
}
.asof {
  margin-left: 4px;
  color: var(--color-text-secondary);
}
.nowrap {
  white-space: nowrap;
}
.td-muted {
  color: var(--color-text-secondary);
}
.stale-date {
  color: #c2870b;
}
.count-badge {
  display: inline-block;
  min-width: 18px;
  padding: 0 6px;
  margin-left: 4px;
  font-size: 12px;
  line-height: 18px;
  text-align: center;
  border-radius: 9px;
  background: rgba(0, 0, 0, 0.06);
  color: var(--color-text-secondary);
}
.tag {
  display: inline-block;
  padding: 1px 7px;
  border-radius: 4px;
  font-size: 12px;
  white-space: nowrap;
}
.tag-ready {
  background: rgba(52, 168, 83, 0.12);
  color: #1e7e34;
}
.tag-wait {
  background: rgba(234, 179, 8, 0.14);
  color: #a16207;
}
.ttl-hint {
  margin-left: 6px;
  font-size: 12px;
  color: var(--color-text-secondary);
}
.reason-cell {
  /* Show the full LLM rationale: wrap to a few lines instead of clipping
     with an ellipsis. Overrides the table-wide `white-space: nowrap`. */
  max-width: 360px;
  white-space: normal;
  word-break: break-word;
  line-height: 1.4;
  color: var(--color-text-secondary);
}
.manual-row {
  display: grid;
  grid-template-columns: 1fr 100px 90px 110px;
  gap: 10px;
  align-items: center;
}
.tick-flow {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.tick-group {
  border-left: 3px solid var(--accent, #4a7cf7);
  padding: 4px 0 4px 12px;
}
.tick-group-head {
  font-size: 13px;
  margin-bottom: 4px;
}
.tick-group-head .kind {
  font-weight: 600;
  margin-right: 8px;
}
.tick-group-head .ts {
  color: var(--muted, #8a8f98);
  font-size: 12px;
}
.tick-step {
  display: flex;
  gap: 8px;
  font-size: 13px;
  padding: 2px 0;
}
.tick-stage {
  flex: 0 0 110px;
  color: var(--muted, #8a8f98);
  font-family: monospace;
  font-size: 12px;
}
.tick-summary {
  flex: 1;
  word-break: break-all;
}

.decision-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 420px;
  overflow-y: auto;
}
.decision {
  border: 0.5px solid rgba(0, 0, 0, 0.08);
  border-radius: 8px;
  padding: 8px 12px;
  font-size: 13px;
}
.decision-meta {
  display: flex;
  justify-content: space-between;
  color: var(--color-text-secondary);
  font-size: 12px;
}
.decision-meta .kind {
  font-weight: 600;
}
.decision.kind-trade {
  background: rgba(0, 113, 227, 0.06);
}
.decision.kind-warn {
  background: rgba(245, 158, 11, 0.08);
}
.decision.kind-info {
  background: rgba(22, 163, 74, 0.06);
}
.decision.kind-error {
  background: rgba(192, 57, 43, 0.1);
}
.decision.kind-meta {
  background: rgba(0, 0, 0, 0.03);
}
.decision.kind-llm {
  background: rgba(139, 92, 246, 0.07);  /* purple tint distinguishes LLM-driven cycles */
  border-left: 3px solid rgba(139, 92, 246, 0.6);
  padding-left: 10px;
}
.decision-reasoning {
  margin-top: 6px;
  padding: 8px 10px;
  background: rgba(139, 92, 246, 0.04);
  border-radius: 6px;
  font-size: 13px;
  color: #4c1d95;
  line-height: 1.5;
}
.reasoning-label {
  font-weight: 600;
  margin-right: 6px;
  color: #6d28d9;
}

/* Mode badge: surfaces which agent path will run on the next tick. */
.stat-card.mode-rule {
  background: rgba(107, 114, 128, 0.08);
}
.stat-card.mode-ensemble {
  background: rgba(22, 163, 74, 0.08);
}
.stat-card.mode-llm {
  background: rgba(139, 92, 246, 0.10);
  border: 1px solid rgba(139, 92, 246, 0.25);
}
.stat-card.mode-pinned {
  background: rgba(245, 158, 11, 0.10);
  border: 1px solid rgba(245, 158, 11, 0.25);
}
.stat-card.pending-active {
  background: rgba(245, 158, 11, 0.10);
  border: 1px solid rgba(245, 158, 11, 0.25);
}
.stat-card.pending-heavy {
  background: rgba(192, 57, 43, 0.10);
  border: 1px solid rgba(192, 57, 43, 0.30);
}
.decision.kind-pending {
  background: rgba(245, 158, 11, 0.06);
  border-left: 3px solid rgba(245, 158, 11, 0.5);
  padding-left: 10px;
}

/* Live control card: the real-money arm/disarm surface. Red-tinted frame so
   it reads as "danger zone" and never blends into the ordinary cards. */
.card.live-control {
  border: 1px solid rgba(192, 57, 43, 0.28);
  background: rgba(192, 57, 43, 0.03);
}
.live-badge {
  display: inline-block;
  margin-left: 8px;
  padding: 1px 9px;
  font-size: 12px;
  font-weight: 600;
  border-radius: 9px;
  color: #c0392b;
  background: rgba(192, 57, 43, 0.10);
  vertical-align: middle;
}
.live-gate-row {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 8px 0;
  border-top: 0.5px solid rgba(0, 0, 0, 0.06);
  font-size: 13px;
}
.gate-label {
  font-weight: 600;
  color: var(--color-text-secondary);
  min-width: 64px;
}
.live-arm-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin: 12px 0 6px;
}
.live-control-note {
  margin: 6px 0 0;
  line-height: 1.6;
}
.live-control-note code {
  background: rgba(0, 0, 0, 0.06);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 11px;
}

/* Mode picker pills: three radio-like clickable cards. */
.mode-presets {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
  margin-bottom: 8px;
}
.mode-pill {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  padding: 12px 14px;
  background: rgba(0, 0, 0, 0.02);
  border: 1.5px solid transparent;
  border-radius: 10px;
  cursor: pointer;
  transition: all 0.15s ease;
  text-align: left;
}
.mode-pill:hover {
  background: rgba(0, 0, 0, 0.04);
}
.mode-pill .mode-title {
  font-weight: 600;
  font-size: 14px;
}
.mode-pill .mode-desc {
  font-size: 11.5px;
  color: var(--color-text-secondary);
  line-height: 1.45;
}
.mode-pill.mode-active {
  background: rgba(0, 113, 227, 0.05);
}
.mode-pill.mode-rule-active {
  border-color: rgba(107, 114, 128, 0.5);
  background: rgba(107, 114, 128, 0.08);
}
.mode-pill.mode-ensemble-active {
  border-color: rgba(22, 163, 74, 0.5);
  background: rgba(22, 163, 74, 0.08);
}
.mode-pill.mode-llm-active {
  border-color: rgba(139, 92, 246, 0.5);
  background: rgba(139, 92, 246, 0.10);
}

/* Advanced switches (folded by default). */
.advanced {
  margin-top: 14px;
  padding: 10px 12px;
  background: rgba(0, 0, 0, 0.02);
  border-radius: 8px;
  font-size: 13px;
}
.advanced summary {
  cursor: pointer;
  font-weight: 600;
  user-select: none;
}
.adv-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 8px 16px;
  margin-top: 10px;
}
.adv-check {
  display: grid;
  grid-template-columns: auto 1fr;
  align-items: baseline;
  gap: 6px 8px;
  padding: 4px 0;
}
.adv-check span {
  font-family: ui-monospace, "SF Mono", monospace;
  font-size: 12.5px;
  color: #1e3a8a;
}
.adv-check em {
  grid-column: 2;
  font-style: normal;
  color: var(--color-text-secondary);
  font-size: 11.5px;
}
.adv-note {
  margin-top: 10px;
  font-size: 11.5px;
  color: var(--color-text-secondary);
  line-height: 1.5;
}
.adv-note code {
  background: rgba(0, 0, 0, 0.06);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 11px;
}
.adv-llm-provider {
  margin-top: 10px;
}
.adv-llm-provider label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  max-width: 520px;
}
.adv-num {
  display: grid;
  grid-template-columns: 1fr 90px;
  align-items: center;
  gap: 4px 8px;
  padding: 4px 0;
}
.adv-num span {
  font-family: ui-monospace, "SF Mono", monospace;
  font-size: 12.5px;
  color: #1e3a8a;
}
.adv-num em {
  grid-column: 1 / -1;
  font-style: normal;
  color: var(--color-text-secondary);
  font-size: 11.5px;
}
.advanced select,
.advanced input[type="number"] {
  padding: 6px 8px;
  border: 1px solid rgba(0, 0, 0, 0.12);
  border-radius: 6px;
  font-size: 13px;
}

/* Factor mining card */
.master-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  margin-bottom: 14px;
  cursor: pointer;
}
.schedule-block {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border, #e5e7eb);
}
.schedule-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
}
.schedule-fields {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.schedule-time {
  display: flex;
  align-items: center;
  gap: 8px;
}
.schedule-time input {
  width: 140px;
}
</style>
