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
}

export interface BacktestResult {
  metrics: Record<string, number>;
  trades: TradeRecord[];
  equity_curve: Record<string, number>;
  warning: string;
}

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

export const syncQuotes = (codes: string[]) =>
  api.post<SyncResult>("/sync/quotes", { codes });

export const syncQuotesAsync = () =>
  api.post<{ job_id: string }>("/sync/quotes/async", { codes: [] });

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
  }>;
  total_value: number;
  pnl_pct: number;
  pending_orders?: number;
}

export interface PortfolioPosition {
  code: string;
  name: string;
  quantity: number;
  avg_cost: number;
  current_price: number;
  price_date: string | null;
  market_value: number;
  pnl: number;
  pnl_pct: number;
  buy_date: string | null;
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
  direction: string;
  quantity: number;
  reason: string;
  created_at: string;
  expected_fill_date: string | null;
  fill_price_basis: string; // "open" → 次日开盘价
  bar_available: boolean;
  trading_days_pending: number | null;
  ttl_trading_days: number;
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
  api.post<{ filled: boolean; snapshot: Portfolio }>("/orders/manual", data);

export const agentStart = () => api.post<{ status: string }>("/agent/start");
export const agentStop = () => api.post<{ status: string }>("/agent/stop");
export const agentTick = () => api.post<Record<string, unknown>>("/agent/tick");
export const fetchAgentStatus = () => api.get<AgentStatus>("/agent/status");
export const fetchAgentDecisions = (limit = 50, kind?: string) =>
  api.get<DecisionRecord[]>("/agent/decisions", { params: { limit, kind } });

export const fetchStrategies = () => api.get<StrategyInfo[]>("/strategies");

export default api;
