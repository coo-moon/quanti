import axios from "axios";

const api = axios.create({
  baseURL: "/api",
});

export interface StockInfo {
  code: string;
  name: string;
  exchange: string;
  list_date: string;
  industry: string;
  latest_date: string | null;
}

export interface QuoteData {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  amount: number;
}

export interface BacktestRequest {
  strategy_name: string;
  codes: string[];
  start: string;
  end: string;
  initial_cash: number;
  params: Record<string, unknown>;
}

export interface TradeRecord {
  date: string;
  stock_code: string;
  direction: string;
  quantity: number;
  price: number;
  commission: number;
  strategy?: string;
  reason?: string; // exits: 止损 / 移动止盈 / 策略离场
}

export interface BacktestResult {
  metrics: Record<string, number>;
  trades: TradeRecord[];
  equity_curve: Record<string, number>;
  warning: string;
  // Portfolio circuit breaker: after halted_at the engine stopped trading —
  // the equity tail is cash/remnant drift, not the strategy.
  halted: boolean;
  halted_at: string | null;
  halted_reason: string;
}

export interface ServerMeta {
  account: string; // "paper" | "live"
  is_live: boolean;
}
export const fetchMeta = () => api.get<ServerMeta>("/meta");

export const fetchStocks = () => api.get<StockInfo[]>("/stocks");

export interface StockPoolStats {
  total: number;
  with_quotes: number;
  exchange_sh: number;
  exchange_sz: number;
  latest_quote_date: string | null;
}

export const fetchStockStats = () => api.get<StockPoolStats>("/stocks/stats");

export const fetchQuotes = (code: string, start: string, end: string) =>
  api.get<QuoteData[]>(`/stocks/${code}/quotes`, { params: { start, end } });

export interface SyncResult {
  synced: Record<string, number>;
  errors: Record<string, string>;
}

export interface SyncOpts {
  years?: number;
  with_basic?: boolean;
  with_financials?: boolean;
}

export const syncQuotes = (codes: string[], opts: SyncOpts = {}) =>
  api.post<SyncResult>("/sync/quotes", { codes, ...opts });

export const syncQuotesAsync = (opts: SyncOpts = {}) =>
  api.post<{ job_id: string }>("/sync/quotes/async", { codes: [], ...opts });

export const fetchQuotesSyncStatus = (jobId: string) =>
  api.get<SyncStatus>(`/sync/quotes/status`, { params: { job_id: jobId } });

export const syncStockList = () =>
  api.post<{ synced: number; message: string }>("/sync/stocks");

// --- Background quote syncer (continuous daemon, decoupled from agent tick) ---
export interface BackgroundSyncStatus {
  enabled: boolean;
  running: boolean;
  state: "stopped" | "active" | "idle" | "paused" | "disabled";
  started_at: string | null;
  last_loop_at: string | null;
  current_code: string | null;
  queue_remaining: number;
  synced_session: number;
  failed_session: number;
  last_full_scan_at: string | null;
  last_error: string | null;
  config: Record<string, number>;
}
export const fetchBackgroundSyncStatus = () =>
  api.get<BackgroundSyncStatus>("/sync/background/status");
export const pauseBackgroundSync = () =>
  api.post<{ ok: boolean; state: string }>("/sync/background/pause");
export const resumeBackgroundSync = () =>
  api.post<{ ok: boolean; state: string }>("/sync/background/resume");

export const runBacktest = (req: BacktestRequest) =>
  api.post<BacktestResult>("/backtest/run", req);

// --- Screener ---

export interface ScreenerInfo {
  name: string;
  name_zh?: string;
  description: string;
}

export interface ScreenRequest {
  screener_name: string;
  codes?: string[];
  end?: string;
  lookback_days?: number;
  top_n?: number;
  params?: Record<string, unknown>;
}

export interface ScreenResultItem {
  code: string;
  name: string;
  score: number;
  close: number;
  change_pct: number;
}

export interface ScreenResponse {
  screener: string;
  description: string;
  results: ScreenResultItem[];
  total_scanned: number;
}

export const fetchScreeners = () => api.get<ScreenerInfo[]>("/screeners");

export const runScreen = (req: ScreenRequest) =>
  api.post<ScreenResponse>("/screen/run", req);

// --- Stock Pools ---

export interface PoolInfo {
  name: string;
  created_at: string;
  description: string;
  stock_count: number;
}

export const fetchPools = () => api.get<PoolInfo[]>("/pools");

export const createPool = (data: { name: string; description: string }) =>
  api.post("/pools", data);

export const deletePool = (name: string) =>
  api.delete<{ name: string; message: string }>(`/pools/${encodeURIComponent(name)}`);

export const fetchPoolStocks = (name: string) =>
  api.get<StockInfo[]>(`/pools/${encodeURIComponent(name)}/stocks`);

export const addPoolStocks = (poolName: string, codes: string[]) =>
  api.post(`/pools/${encodeURIComponent(poolName)}/stocks`, { codes });

export const removePoolStocks = (poolName: string, codes: string[]) =>
  api.delete(`/pools/${encodeURIComponent(poolName)}/stocks`, { data: { codes } });

export const syncPoolStocks = (poolName: string) =>
  api.post<{ job_id: string }>(`/pools/${encodeURIComponent(poolName)}/sync`);

export interface SyncStatus {
  job_id: string;
  current: number;
  total: number;
  status: string;
  errors: Record<string, string>;
  message: string;
  eta_seconds: number | null;
}

export const fetchPoolSyncStatus = (poolName: string, jobId: string) =>
  api.get<SyncStatus>(`/pools/${encodeURIComponent(poolName)}/sync/status`, {
    params: { job_id: jobId },
  });

// --- Agent / Goal / Portfolio ---

export interface Goal {
  target_annual_return: number;
  max_drawdown: number;
  risk_tolerance: "low" | "medium" | "high";
  universe_pool: string;
  screener_name: string;
  strategy_name: string;
  params: Record<string, unknown>;
  rebalance_freq: string;
  enabled: boolean;
}

export interface AgentStatus {
  enabled: boolean;
  running: boolean;
  started_at: string | null;
  tick_interval_sec?: number;
  last_tick_at: string | null;
  last_tick_summary: string;
  last_strategy: string;
  last_evaluations: Array<{
    strategy_name: string;
    annual_return: number;
    max_drawdown: number;
    sharpe: number;
    total_trades: number;
    score: number;
    // Walk-forward (out-of-sample) metrics; n_folds === 0 means no WF data.
    oos_annual_return?: number;
    oos_max_drawdown?: number;
    oos_sharpe?: number;
    n_folds?: number;
    oos_trades?: number;
  }>;
  total_value: number;
  pnl_pct: number;
  pending_orders?: number;
  // Holdings whose entry_strategy is no longer loadable — strategy exit
  // silently degraded to stop-loss/TP only. Non-empty = needs operator action.
  degraded_exits?: Array<{
    code: string;
    entry_strategy: string;
    name: string;
  }>;
}

export interface PortfolioPosition {
  code: string;
  name: string;
  industry: string;
  quantity: number;
  avg_cost: number;
  current_price: number;
  price_date: string | null;
  market_value: number;
  pnl: number;
  pnl_pct: number;
  buy_date: string | null;
  score: number | null; // 当前融合候选分;null = 已掉出候选
}

export interface Portfolio {
  cash: number;
  initial_cash: number;
  market_value: number;
  total_value: number;
  pnl: number;
  pnl_pct: number;
  positions: PortfolioPosition[];
  snapshot_date: string;
}

export interface LiveStopPosition {
  code: string;
  name: string;
  quantity: number;
  avg_cost: number;
  current_price: number;
  pnl_pct: number;
  stop_price: number;
  stop_pct: number;
  atr_driven: boolean;
  entry_strategy: string;
  // llm_full 模式:止损价来自 LLM 落库点位,并附加仓价。
  llm_plan?: boolean;
  add_price?: number;
}

export interface LiveStatus {
  is_live: boolean;
  guard: {
    enabled: boolean;
    interval_sec: number;
    running: boolean;
    connected: boolean | null;
    in_session: boolean;
    llm_guard?: { mode: boolean; interval_sec: number; running: boolean };
  };
  positions: LiveStopPosition[];
}

export const fetchLiveStatus = () => api.get<LiveStatus>("/agent/live-status");

// --- llm_full 模式:每持仓 LLM 点位计划 ---
export interface PositionPlan {
  code: string;
  name: string;
  quantity: number;
  avg_cost: number;
  current_price: number;
  stop_price: number;
  add_price: number;
  add_size_pct: number;
  plan_reason: string;
  plan_updated_at: string;
}
export const fetchPositionPlans = () =>
  api.get<PositionPlan[]>("/agent/position-plans");
export const fetchLlmAudit = (limit = 10) =>
  api.get<DecisionRecord[]>("/agent/llm-audit", { params: { limit } });

export interface OrderRecord {
  order_id: string;
  code: string;
  direction: string;
  quantity: number;
  status: string;
  filled_price: number;
  filled_quantity: number;
  strategy_name: string;
  reason: string;
  created_at: string;
  filled_at: string | null;
}

export interface PendingOrderDetail {
  order_id: string;
  code: string;
  name: string;
  industry: string;
  direction: string;
  quantity: number;
  reason: string;
  created_at: string;
  expected_fill_date: string | null;
  fill_price_basis: string; // "open" → 次日开盘价
  bar_available: boolean;
  trading_days_pending: number | null;
  ttl_trading_days: number;
  entry_strategy?: string;
  score: number | null; // 下单时的融合候选分;null = 已掉出候选
}

export interface DecisionRecord {
  id: number;
  ts: string;
  kind: string;
  code: string;
  summary: string;
  details: Record<string, unknown>;
}

export interface StrategyInfo {
  name: string;
  name_zh?: string;
  description?: string;
}

export const fetchGoal = () => api.get<Goal>("/goal");
export const updateGoal = (g: Partial<Goal>) => api.post<{ ok: boolean; goal: Goal }>("/goal", g);
export const fetchPortfolio = () => api.get<Portfolio>("/portfolio");
export const resetPortfolio = (initial_cash: number) =>
  api.post<Portfolio>("/portfolio/reset", null, { params: { initial_cash } });
export const fetchPortfolioSnapshots = () =>
  api.get<Array<{ snapshot_date: string; cash: number; market_value: number; total_value: number }>>(
    "/portfolio/snapshots"
  );
export const fetchOrders = (limit = 100) =>
  api.get<OrderRecord[]>("/orders", { params: { limit } });
export const fetchPendingOrders = () =>
  api.get<PendingOrderDetail[]>("/orders/pending");
export const fetchTrades = (limit = 100) => api.get<unknown[]>("/trades", { params: { limit } });
export const manualOrder = (data: { code: string; direction: "buy" | "sell"; strength?: number; reason?: string }) =>
  api.post<{ filled: boolean; status: "filled" | "pending" | "rejected"; snapshot: Portfolio }>(
    "/orders/manual", data);

export const agentStart = () => api.post<{ status: string }>("/agent/start");
export const agentStop = () => api.post<{ status: string }>("/agent/stop");
export const agentRestart = () => api.post<{ status: string }>("/agent/restart");
export const agentTick = () => api.post<Record<string, unknown>>("/agent/tick");
export const fetchAgentStatus = () => api.get<AgentStatus>("/agent/status");
export const fetchAgentDecisions = (limit = 50, kind?: string) =>
  api.get<DecisionRecord[]>("/agent/decisions", { params: { limit, kind } });

export const fetchStrategies = () => api.get<StrategyInfo[]>("/strategies");

// --- Risk-control audit ---
export interface ChannelParity {
  channel: string;
  stop_loss: boolean;
  trailing_tp: boolean;
  strategy_exit: boolean;
  note: string;
}
export interface RiskExitEvent {
  ts: string;
  code: string;
  kind: string; // stop_loss | trailing_tp | strategy_exit | circuit_breaker | other
  reason: string;
  price: number | null;
  quantity: number | null;
}
export interface RiskAudit {
  account: string;
  is_live: boolean;
  // LLM 全权模式:经典个股离场与买入护栏被旁路,止损=LLM 落库点位。
  llm_full?: {
    enabled: boolean;
    disaster_floor_pct: number;
    n_positions?: number;
    n_with_stop?: number;
    n_with_add?: number;
  };
  exits: {
    stop_loss: { enabled: boolean; threshold: number };
    atr_stop: { enabled: boolean; k: number; n: number };
    trailing_take_profit: { enabled: boolean; activate: number; trail: number };
    strategy_exit: { enabled: boolean };
    portfolio_circuit_breaker: { threshold: number };
  };
  channel_parity: ChannelParity[];
  guard: {
    enabled: boolean;
    bypassed_by_llm_full?: boolean;
    locked: boolean;
    reason: string;
    recent_stop_losses: number;
    stoploss_guard: { enabled: boolean; lookback_days: number; trade_limit: number; lock_days: number };
    max_drawdown: { enabled: boolean; lookback_days: number; max_drawdown_pct: number; lock_days: number };
  };
  circuit_breaker: {
    total_value: number | null;
    peak_value: number | null;
    drawdown: number | null;
    threshold: number;
    tripped: boolean;
    headroom: number | null;
  };
  recent_exits: RiskExitEvent[];
  stock_pnl: StockPnlItem[];
}
export interface StockPnlItem {
  code: string;
  name: string;
  trips: number;
  total_pnl: number;
  avg_return: number;
  win_rate: number;
  last_sell_date: string | null;
  last_return: number;
}
export const fetchRiskAudit = (exitsLimit = 50) =>
  api.get<RiskAudit>("/risk/audit", { params: { exits_limit: exitsLimit } });

// --- Runtime risk-control config (P0-3) ---
export interface RiskControl {
  stop_loss_pct: number;
  portfolio_stop_loss_pct: number;
  take_profit_activate_pct: number;
  take_profit_trail_pct: number;
  strategy_exit_enabled: boolean;
  atr_stop_k: number;
  atr_stop_n: number;
  extreme_gap_up_block_pct: number;
  drift_trim_enabled: boolean;
  drift_trim_to_pct: number;
  drift_trim_band: number;
  rotation_enabled: boolean;
  rotation_margin: number;
  max_position_pct: number;
  max_industry_pct: number;
  // llm_full 模式的每标的灾难地板(0=关;仅兜底 LLM 点位缺失/被穿透)。
  llm_disaster_floor_pct: number;
}
export const fetchRiskControl = () =>
  api.get<RiskControl>("/config/risk-control");
export const saveRiskControl = (cfg: RiskControl) =>
  api.post<RiskControl & { ok: boolean }>("/config/risk-control", cfg);

// --- Live-order arm/disarm control (UI switch for 阶段 C→D) ---
export interface BridgeHealth {
  mode?: string;
  trader_connected?: boolean;
  datafeed_ok?: boolean;
  orders_allowed?: boolean;
}
export interface LiveControl {
  account: string;
  is_live: boolean;
  live_capable: boolean; // process started with QUANTI_LIVE_ACK
  orders_armed: boolean; // runtime arm switch (this endpoint toggles it)
  bridge: BridgeHealth | null;
}
export const fetchLiveControl = () => api.get<LiveControl>("/live/status");
export const setLiveOrdersArmed = (armed: boolean) =>
  api.post<{ ok: boolean; orders_armed: boolean }>("/live/orders-armed", { armed });

// --- Hyperopt / tuned params ---
export interface OptimizeResultItem {
  strategy_name: string;
  params: Record<string, unknown>;
  oos_sharpe: number;
  baseline_oos_sharpe: number;
  accepted: boolean;
  n_combos: number;
  universe_size: number;
  tuned_at: string;
}

export interface OptimizeStatus {
  job_id: string;
  current: number;
  total: number;
  status: string; // "running" | "done" | "error"
  current_strategy: string;
  results: OptimizeResultItem[];
}

export const runOptimizeAsync = () =>
  api.post<{ job_id: string }>("/agent/optimize/async");

export const fetchOptimizeStatus = (jobId: string) =>
  api.get<OptimizeStatus>("/agent/optimize/status", { params: { job_id: jobId } });

export const fetchTunedParams = () =>
  api.get<OptimizeResultItem[]>("/agent/tuned-params");

// --- LLM factor mining ---
export interface GeneratedFactor {
  name: string;
  expr_str: string;
  train_ic: number | null;
  oos_ic: number | null;
  accepted: boolean;
  enabled: boolean;
  created_at: string;
}

export interface MineStatus {
  job_id: string;
  current: number;
  total: number;
  status: string; // "running" | "done" | "error"
  results: GeneratedFactor[];
}

export const runMineAsync = () =>
  api.post<{ job_id: string }>("/agent/mine-factors/async");
export const runRescoreAsync = () =>
  api.post<{ job_id: string }>("/factors/rescore/async");
export const fetchMineStatus = (jobId: string) =>
  api.get<MineStatus>("/agent/mine-factors/status", { params: { job_id: jobId } });
export const fetchGeneratedFactors = () =>
  api.get<GeneratedFactor[]>("/factors/generated");
export const setFactorEnabled = (name: string, enabled: boolean) =>
  api.post<{ ok: boolean; name: string; enabled: boolean }>(
    `/factors/generated/${encodeURIComponent(name)}/enabled`, { enabled });

// --- Data source config ---
export interface DataSourceConfig {
  source: string;
  has_token: boolean;
  available_sources: string[];
}

export const fetchDataSource = () =>
  api.get<DataSourceConfig>("/config/data-source");
export const testDataSource = (source: string, token?: string | null) =>
  api.post<{ ok: boolean; message: string }>("/config/data-source/test", {
    source,
    token: token ?? null,
  });
export const saveDataSource = (source: string, token?: string | null) =>
  api.post<{ ok: boolean; message: string }>("/config/data-source", {
    source,
    token: token ?? null,
  });

// --- Alert channel config ---
export interface AlertConfig {
  has_webhook: boolean;
  source: string; // "env" | "db" | ""
  kinds: string[];
}

export const fetchAlertConfig = () => api.get<AlertConfig>("/config/alert");
export const testAlertConfig = (webhookUrl: string) =>
  api.post<{ ok: boolean; message: string }>("/config/alert/test", {
    webhook_url: webhookUrl,
  });
export const saveAlertConfig = (webhookUrl: string) =>
  api.post<{ ok: boolean }>("/config/alert", { webhook_url: webhookUrl });

// --- LLM API key config (never echoes the key back) ---
export interface LlmKeyConfig {
  env_var: string;
  env_set: boolean;
  db_set: boolean;
}

export const fetchLlmKeyConfig = () => api.get<LlmKeyConfig>("/config/llm-key");
export const saveLlmKeyConfig = (apiKey: string) =>
  api.post<{ ok: boolean }>("/config/llm-key", { api_key: apiKey });

export default api;


// --- ETF 网格挖掘器 ---
export interface EtfGridStatus {
  codes: number;
  start: string | null;
  end: string | null;
  rows: number;
  has_token: boolean;
  universe: number;
}
export interface EtfScreenRow {
  code: string;
  name: string;
  category: string;
  t0: boolean;
  price: number;
  grid_score: number;
  er: number;
  vol: number;
  amp: number;
  net: number;
  pos: number;
  rev: number;
  adv: number;
  days: number;
  grid_ret?: number;
  hold_ret?: number;
  grid_dd?: number;
  hold_dd?: number;
  bt_trades?: number;
  box_lo: number;
  box_hi: number;
  grids: number;
  step: number;
  step_pct: number;
  stop: number;
}
export interface EtfDeploy {
  box_lo: number;
  box_hi: number;
  price: number;
  grids: number;
  step: number;
  step_pct: number;
  stop: number;
}
export interface EtfBacktestResult {
  grid_ret: number;
  hold_ret: number;
  grid_dd: number;
  hold_dd: number;
  trades: number;
  start: string;
  end: string;
  grid_curve: Record<string, number>;
  hold_curve: Record<string, number>;
  deploy: EtfDeploy;
  name: string;
  category: string;
  t0: boolean;
  error?: string;
}
export interface EtfOptimizeRow {
  N: number;
  box: string;
  spacing: string;
  trim: string;
  mean: number;
  worst: number;
  dd: number;
  beat_hold: string;
  trades: number;
  per_quarter: Record<string, number>;
}
export interface EtfOptimizeResult {
  code: string;
  name: string;
  quarters: string[];
  holds: Record<string, number>;
  robust: EtfOptimizeRow[];
  overfit: EtfOptimizeRow[];
  best: EtfOptimizeRow;
  deploy: EtfDeploy;
  error?: string;
}

export const fetchEtfGridStatus = () =>
  api.get<EtfGridStatus>("/etf-grid/status");
export const runEtfSyncAsync = () =>
  api.post<{ job_id?: string; error?: string }>("/etf-grid/sync/async");
export const fetchEtfSyncStatus = (jobId: string) =>
  api.get<{ current: number; total: number; status: string; errors?: unknown }>(
    "/etf-grid/sync/status",
    { params: { job_id: jobId } },
  );
export const screenEtfGrid = (advMin = 1e8) =>
  api.post<{ results?: EtfScreenRow[]; count?: number; error?: string }>(
    "/etf-grid/screen",
    null,
    { params: { adv_min: advMin } },
  );
export const backtestEtfGrid = (req: {
  code: string;
  start?: string;
  N?: number;
  lookback?: number;
  rebal?: number;
  geom?: boolean;
  trim?: boolean;
}) => api.post<EtfBacktestResult>("/etf-grid/backtest", req);
export const optimizeEtfGrid = (code: string) =>
  api.post<EtfOptimizeResult>("/etf-grid/optimize", null, { params: { code } });

// --- 市场 regime 快照 ---

export interface RegimeMetrics {
  above20?: number;
  above50?: number;
  above200?: number;
  cap1?: number;
  eq1?: number;
  cap5?: number;
  eq5?: number;
  cap20?: number;
  eq20?: number;
  up?: number;
  dn?: number;
  fl?: number;
  ad_ratio?: number;
  nh?: number;
  nl?: number;
  amt_today?: number;
  amt5?: number;
  amt20?: number;
  amt_chg?: number;
  turn?: number;
  n_stocks?: number;
}
export interface RegimeSector {
  industry: string;
  ret: number;
  n: number;
}
export interface RegimeLLM {
  regime?: string;
  confidence?: number;
  headline?: string;
  drivers?: string[];
  sectors_favored?: string[];
  sectors_avoid?: string[];
  action?: string;
  risk_notes?: string[];
}
export interface RegimeSnapshot {
  exists?: boolean;
  date: string;
  rule_label: string;
  rule_score: number;
  llm_regime: string;
  llm_confidence: number;
  headline: string;
  action: string;
  metrics: RegimeMetrics;
  model: string;
  created_at: string;
  // 仅 latest / 单日详情返回
  sectors?: { top20?: RegimeSector[]; bottom20?: RegimeSector[]; top5d?: RegimeSector[] };
  llm?: RegimeLLM;
  report_md?: string;
  news?: { cctv?: unknown[]; flash?: unknown[] };
}

export const fetchRegimeLatest = () =>
  api.get<RegimeSnapshot & { exists: boolean }>("/regime/latest");
export const fetchRegimeHistory = (limit = 90) =>
  api.get<{ items: RegimeSnapshot[] }>("/regime/history", { params: { limit } });
export const fetchRegimeDay = (day: string) =>
  api.get<RegimeSnapshot>(`/regime/${day}`);
// 全市场扫描 + LLM 深度思考,实测 30-120s —— 前端要给足超时,否则手动
// 触发永远显示失败而后台其实成功了。
export const runRegimeSnapshot = () =>
  api.post<RegimeSnapshot & { ok: boolean }>("/regime/run", null, {
    timeout: 600_000,
  });
