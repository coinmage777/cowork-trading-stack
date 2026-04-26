export interface NetworkInfo {
  network: string;
  deposit: boolean;
  withdraw: boolean;
  fee: number | null;
}

export interface MarketData {
  supported: boolean;
  bid: number | null;
  ask: number | null;
  gap: number | null;
}

export interface ExchangeInfo {
  spot: MarketData;
  futures: MarketData;
  networks: NetworkInfo[];
  margin: {
    supported: boolean | null;
  };
  loan: {
    supported: boolean | null;
  };
}

export interface WithdrawalLimit {
  onetime_coin: number | null;
  onetime_krw: number | null;
  daily_coin: number | null;
  daily_krw: number | null;
  remaining_daily_coin: number | null;
  remaining_daily_krw: number | null;
}

export interface BithumbInfo {
  ask: number | null;
  usdt_krw_last: number | null;
  withdrawal_limit: WithdrawalLimit | null;
  networks: NetworkInfo[];
}

export interface GapUpdate {
  type: 'gap_update';
  ticker: string;
  timestamp: number;
  bithumb: BithumbInfo;
  exchanges: Record<string, ExchangeInfo>;
}

export interface NetworkWatchItem {
  exchange: string;
  ticker: string;
  network: string;
}

export interface PriceMuteItem {
  exchange: string;
  ticker: string;
}

export interface HedgeOrderLeg {
  exchange: string;
  market: 'spot' | 'futures' | string;
  symbol: string;
  side: 'buy' | 'sell' | string;
  requested_qty: number;
  status: string;
  filled_qty: number;
  avg_price: number | null;
  cost: number | null;
  order_id: string | null;
  error: string | null;
}

export interface HedgeJob {
  job_id: string;
  created_at: number;
  updated_at: number;
  ticker: string;
  status: string;
  message?: string;
  futures_exchange: string;
  nominal_usd: number;
  leverage: number;
  requested_qty: number;
  entry_usdt_krw: number | null;
  entry_qty_spot?: number;
  entry_qty_futures?: number;
  residual_qty?: number;
  residual_ratio?: number;
  residual_notional_usd?: number | null;
  hedge_ratio_tolerance?: number;
  hedge_notional_tolerance_usd?: number;
  entry_avg_spot_krw?: number | null;
  entry_avg_futures_usdt?: number | null;
  entry_gap?: number | null;
  entry_spread_usdt?: number | null;
  finalized_at?: number;
  close_qty_spot?: number;
  close_qty_futures?: number;
  closed_qty?: number;
  closed_at?: number | null;
  close_avg_spot_price?: number | null;
  close_avg_spot_quote?: string | null;
  close_avg_spot_usdt?: number | null;
  close_avg_futures_usdt?: number | null;
  close_usdt_krw?: number | null;
  close_gap?: number | null;
  close_spread_usdt?: number | null;
  exit_spot_exchange?: string;
  exit_futures_exchange?: string;
  exit_usdt_krw?: number | null;
  exit_avg_spot_krw?: number | null;
  exit_avg_spot_usdt?: number | null;
  exit_avg_futures_usdt?: number | null;
  exit_gap?: number | null;
  exit_spread_usdt?: number | null;
  final_pnl_usdt?: number | null;
  final_pnl_krw?: number | null;
  warnings?: string[];
  events?: Array<Record<string, unknown>>;
  legs?: {
    spot: HedgeOrderLeg[];
    futures: HedgeOrderLeg[];
  };
}
