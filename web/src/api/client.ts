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
}

export const fetchStocks = () => api.get<StockInfo[]>("/stocks");

export const fetchQuotes = (code: string, start: string, end: string) =>
  api.get<QuoteData[]>(`/stocks/${code}/quotes`, { params: { start, end } });

export interface SyncResult {
  synced: Record<string, number>;
}

export const syncQuotes = (codes: string[]) =>
  api.post<SyncResult>("/sync/quotes", { codes });

export const runBacktest = (req: BacktestRequest) =>
  api.post<BacktestResult>("/backtest/run", req);

export default api;
