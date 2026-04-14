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

export const runBacktest = (req: BacktestRequest) =>
  api.post<BacktestResult>("/backtest/run", req);

// --- Screener ---

export interface ScreenerInfo {
  name: string;
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
}

export const fetchPoolSyncStatus = (poolName: string, jobId: string) =>
  api.get<SyncStatus>(`/pools/${encodeURIComponent(poolName)}/sync/status`, {
    params: { job_id: jobId },
  });

export default api;
