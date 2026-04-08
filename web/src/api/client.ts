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

export default api;
