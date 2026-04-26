"""Configuration and constants loaded from environment variables."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


@dataclass
class Config:
    base_dir: Path = field(default_factory=lambda: BASE_DIR)

    clob_api_key: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_KEY", ""))
    clob_secret: str = field(default_factory=lambda: os.getenv("POLYMARKET_SECRET", ""))
    clob_passphrase: str = field(default_factory=lambda: os.getenv("POLYMARKET_PASSPHRASE", ""))
    clob_api_url: str = field(default_factory=lambda: os.getenv("POLYMARKET_API_URL", "https://clob.polymarket.com"))
    chain_id: int = 137
    private_key: str = field(default_factory=lambda: os.getenv("PRIVATE_KEY", ""))
    proxy_wallet: str = field(default_factory=lambda: os.getenv("PROXY_WALLET", ""))

    binance_ws_url: str = "wss://stream.binance.com:9443/ws/btcusdt@trade"
    binance_rest_url: str = "https://api.binance.com"

    rsi_period: int = 14
    bb_period: int = 20
    bb_std: float = 2.0
    vwap_reset_interval: int = 60
    signal_recalc_interval: int = 30
    tick_cache_size: int = 1000
    candle_interval: str = "1m"
    candle_history: int = 100

    min_edge_threshold: float = 0.12
    min_entry_edge: float = 0.06

    # Fee rate (Polymarket charges ~10% on profit for taker orders)
    taker_fee_rate: float = 0.10

    cheap_up_price_cap: float = 0.30
    cheap_down_price_cap: float = 0.30
    cheap_up_min_model_prob: float = 0.48
    cheap_down_max_model_prob: float = 0.52
    late_market_window_min: float = 3.0
    late_market_window_max: float = 8.0
    late_market_price_range: float = 200.0
    late_market_extreme_low: float = 0.35
    late_market_extreme_high: float = 0.65
    late_model_uncertain_low: float = 0.40
    late_model_uncertain_high: float = 0.60

    kelly_fraction: float = 0.10
    min_bet_size: float = 0.50
    max_single_bet: float = 5.0
    max_portfolio_exposure: float = 30.0
    min_time_to_expiry: float = 5.0

    daily_stop_loss: float = field(default_factory=lambda: float(os.getenv("DAILY_STOP_LOSS", "-30.0")))
    max_open_positions: int = 3
    min_market_liquidity: float = 200.0
    enforce_paper_after_live_loss: bool = True
    live_to_paper_loss_threshold: float = -100.0
    live_resume_profit_target: float = 0.0
    recovery_mode_enabled: bool = field(default_factory=lambda: os.getenv("RECOVERY_MODE_ENABLED", "false").lower() == "true")
    recovery_start_pnl: float = field(default_factory=lambda: float(os.getenv("RECOVERY_START_PNL", "0")))

    paper_optimizer_enabled: bool = field(default_factory=lambda: os.getenv("PAPER_OPTIMIZER_ENABLED", "true").lower() == "true")
    paper_optimizer_block_reference_only: bool = field(default_factory=lambda: os.getenv("PAPER_OPTIMIZER_BLOCK_REFERENCE_ONLY", "false").lower() == "true")
    optimizer_eval_interval_sec: int = field(default_factory=lambda: int(os.getenv("OPTIMIZER_EVAL_INTERVAL_SEC", "300")))
    optimizer_min_closed_trades: int = field(default_factory=lambda: int(os.getenv("OPTIMIZER_MIN_CLOSED_TRADES", "8")))
    optimizer_recent_trade_window: int = field(default_factory=lambda: int(os.getenv("OPTIMIZER_RECENT_TRADE_WINDOW", "20")))
    optimizer_profit_target: float = field(default_factory=lambda: float(os.getenv("OPTIMIZER_PROFIT_TARGET", "5")))
    optimizer_day_profit_min_trades: int = field(default_factory=lambda: int(os.getenv("OPTIMIZER_DAY_PROFIT_MIN_TRADES", "12")))
    optimizer_inactivity_block_reset_minutes: int = field(default_factory=lambda: int(os.getenv("OPTIMIZER_INACTIVITY_BLOCK_RESET_MINUTES", "180")))

    paper_live_gate_enabled: bool = field(default_factory=lambda: os.getenv("PAPER_LIVE_GATE_ENABLED", "true").lower() == "true")
    paper_live_gate_min_trades: int = field(default_factory=lambda: int(os.getenv("PAPER_LIVE_GATE_MIN_TRADES", "20")))
    paper_live_gate_min_pnl: float = field(default_factory=lambda: float(os.getenv("PAPER_LIVE_GATE_MIN_PNL", "5")))
    paper_live_gate_min_win_rate: float = field(default_factory=lambda: float(os.getenv("PAPER_LIVE_GATE_MIN_WIN_RATE", "0.55")))
    paper_live_gate_min_profit_factor: float = field(default_factory=lambda: float(os.getenv("PAPER_LIVE_GATE_MIN_PROFIT_FACTOR", "1.2")))

    shadow_trading_enabled: bool = field(default_factory=lambda: os.getenv("SHADOW_TRADING_ENABLED", "true").lower() == "true")
    shadow_max_candidates_per_scan: int = field(default_factory=lambda: int(os.getenv("SHADOW_MAX_CANDIDATES_PER_SCAN", "2")))
    shadow_bet_size: float = field(default_factory=lambda: float(os.getenv("SHADOW_BET_SIZE", "1.0")))
    shadow_max_open_total: int = field(default_factory=lambda: int(os.getenv("SHADOW_MAX_OPEN_TOTAL", "8")))
    shadow_max_open_per_group: int = field(default_factory=lambda: int(os.getenv("SHADOW_MAX_OPEN_PER_GROUP", "2")))
    shadow_allow_simulated: bool = field(default_factory=lambda: os.getenv("SHADOW_ALLOW_SIMULATED", "true").lower() == "true")
    shadow_simulated_cooldown_sec: int = field(default_factory=lambda: int(os.getenv("SHADOW_SIMULATED_COOLDOWN_SEC", "900")))
    allow_simulated_paper_trades: bool = field(default_factory=lambda: os.getenv("ALLOW_SIMULATED_PAPER_TRADES", "false").lower() == "true")
    non_reference_trade_edge_buffer: float = field(default_factory=lambda: float(os.getenv("NON_REFERENCE_TRADE_EDGE_BUFFER", "0.01")))
    eth_trade_edge_buffer: float = field(default_factory=lambda: float(os.getenv("ETH_TRADE_EDGE_BUFFER", "0.01")))
    sol_up_blocked: bool = field(default_factory=lambda: os.getenv("SOL_UP_BLOCKED", "false").lower() == "true")
    # Comma-separated "ASSET:SIDE" pairs to block from live (e.g. "BTC:DOWN,SOL:UP")
    blocked_asset_sides: str = field(default_factory=lambda: os.getenv("BLOCKED_ASSET_SIDES", ""))
    non_reference_bet_scale: float = field(default_factory=lambda: float(os.getenv("NON_REFERENCE_BET_SCALE", "0.75")))
    shadow_guard_enabled: bool = field(default_factory=lambda: os.getenv("SHADOW_GUARD_ENABLED", "true").lower() == "true")
    shadow_guard_reference_only: bool = field(default_factory=lambda: os.getenv("SHADOW_GUARD_REFERENCE_ONLY", "true").lower() == "true")
    shadow_guard_min_group_trades: int = field(default_factory=lambda: int(os.getenv("SHADOW_GUARD_MIN_GROUP_TRADES", "30")))
    shadow_guard_group_max_pnl: float = field(default_factory=lambda: float(os.getenv("SHADOW_GUARD_GROUP_MAX_PNL", "-5.0")))
    shadow_guard_group_max_win_rate: float = field(default_factory=lambda: float(os.getenv("SHADOW_GUARD_GROUP_MAX_WIN_RATE", "0.38")))
    shadow_guard_min_side_trades: int = field(default_factory=lambda: int(os.getenv("SHADOW_GUARD_MIN_SIDE_TRADES", "15")))
    shadow_guard_side_max_pnl: float = field(default_factory=lambda: float(os.getenv("SHADOW_GUARD_SIDE_MAX_PNL", "-3.0")))
    shadow_guard_side_max_win_rate: float = field(default_factory=lambda: float(os.getenv("SHADOW_GUARD_SIDE_MAX_WIN_RATE", "0.35")))
    copy_trading_enabled: bool = field(default_factory=lambda: os.getenv("COPY_TRADING_ENABLED", "true").lower() == "true")
    copy_leaderboard_category: str = field(default_factory=lambda: os.getenv("COPY_LEADERBOARD_CATEGORY", "CRYPTO").upper())
    copy_leaderboard_time_period: str = field(default_factory=lambda: os.getenv("COPY_LEADERBOARD_TIME_PERIOD", "MONTH").upper())
    copy_leaderboard_limit: int = field(default_factory=lambda: int(os.getenv("COPY_LEADERBOARD_LIMIT", "20")))
    copy_min_wallet_pnl: float = field(default_factory=lambda: float(os.getenv("COPY_MIN_WALLET_PNL", "5000")))
    copy_min_wallet_volume: float = field(default_factory=lambda: float(os.getenv("COPY_MIN_WALLET_VOLUME", "25000")))
    copy_wallet_trade_limit: int = field(default_factory=lambda: int(os.getenv("COPY_WALLET_TRADE_LIMIT", "40")))
    copy_trade_lookback_sec: int = field(default_factory=lambda: int(os.getenv("COPY_TRADE_LOOKBACK_SEC", "900")))
    copy_min_trade_cash: float = field(default_factory=lambda: float(os.getenv("COPY_MIN_TRADE_CASH", "100")))
    copy_min_distinct_wallets: int = field(default_factory=lambda: int(os.getenv("COPY_MIN_DISTINCT_WALLETS", "1")))
    copy_top_markets_per_scan: int = field(default_factory=lambda: int(os.getenv("COPY_TOP_MARKETS_PER_SCAN", "5")))
    copy_wallet_refresh_sec: int = field(default_factory=lambda: int(os.getenv("COPY_WALLET_REFRESH_SEC", "600")))
    copy_trade_refresh_sec: int = field(default_factory=lambda: int(os.getenv("COPY_TRADE_REFRESH_SEC", "45")))
    copy_signal_base_edge: float = field(default_factory=lambda: float(os.getenv("COPY_SIGNAL_BASE_EDGE", "0.02")))
    copy_signal_wallet_bonus: float = field(default_factory=lambda: float(os.getenv("COPY_SIGNAL_WALLET_BONUS", "0.01")))
    copy_signal_cash_bonus_cap: float = field(default_factory=lambda: float(os.getenv("COPY_SIGNAL_CASH_BONUS_CAP", "0.02")))
    copy_signal_cash_scale: float = field(default_factory=lambda: float(os.getenv("COPY_SIGNAL_CASH_SCALE", "5000")))
    copy_signal_max_edge: float = field(default_factory=lambda: float(os.getenv("COPY_SIGNAL_MAX_EDGE", "0.08")))
    market_fetch_cooldown_sec: int = field(default_factory=lambda: int(os.getenv("MARKET_FETCH_COOLDOWN_SEC", "5")))
    repeated_skip_log_interval_sec: int = field(default_factory=lambda: int(os.getenv("REPEATED_SKIP_LOG_INTERVAL_SEC", "300")))
    db_busy_timeout_ms: int = field(default_factory=lambda: int(os.getenv("DB_BUSY_TIMEOUT_MS", "5000")))
    db_snapshot_log_every: int = field(default_factory=lambda: int(os.getenv("DB_SNAPSHOT_LOG_EVERY", "5")))
    stale_trade_reconcile_grace_sec: int = field(default_factory=lambda: int(os.getenv("STALE_TRADE_RECONCILE_GRACE_SEC", "900")))

    enabled_market_groups: str = field(default_factory=lambda: os.getenv("ENABLED_MARKET_GROUPS", "btc_15m,btc_1h,eth_15m"))
    performance_reference_group: str = field(default_factory=lambda: os.getenv("PERFORMANCE_REFERENCE_GROUP", "btc_15m"))
    live_only_strategies: str = field(default_factory=lambda: os.getenv("LIVE_ONLY_STRATEGIES", "expiry_snipe,hedge_arb"))

    max_trades_per_scan: int = 1
    db_path: str = field(default_factory=lambda: str(BASE_DIR / os.getenv("DB_PATH", "trades_v2.db")))
    log_path: str = field(default_factory=lambda: str(BASE_DIR / "bot.log"))
    scan_interval: int = 20
    log_level: str = "INFO"

    btc_market_slug: str = "btc"
    market_category: str = "crypto"

    # Feature 1: Strike price based probability
    strike_price_enabled: bool = True
    strike_prob_weight: float = 0.40

    # Feature 2: Orderbook sentiment
    orderbook_enabled: bool = True
    orderbook_sentiment_weight: float = 0.30
    orderbook_min_depth: float = 50.0

    # Feature 3: Expiry snipe (near-expiry trades when direction is clear)
    expiry_snipe_enabled: bool = True
    expiry_snipe_max_minutes: float = 8.0
    expiry_snipe_min_strike_dist_pct: float = 0.0008
    expiry_snipe_max_entry_price: float = 0.47
    expiry_snipe_bet_multiplier: float = 2.5

    # Feature 4: Multi-timeframe confirmation
    htf_enabled: bool = True
    htf_candle_interval: str = "5m"
    htf_candle_count: int = 30
    htf_trend_weight: float = 0.15

    # Feature 5: Volatility regime
    vol_regime_enabled: bool = True
    vol_regime_low_threshold: float = 0.003
    vol_regime_high_threshold: float = 0.008

    # Feature 6: Both-sides hedge arbitrage
    hedge_enabled: bool = True
    hedge_max_combined_price: float = field(default_factory=lambda: float(os.getenv("HEDGE_MAX_COMBINED_PRICE", "0.93")))
    hedge_min_profit_pct: float = field(default_factory=lambda: float(os.getenv("HEDGE_MIN_PROFIT_PCT", "0.03")))

    # Feature 7: ML model (Random Forest)
    ml_enabled: bool = field(default_factory=lambda: os.getenv("ML_ENABLED", "true").lower() == "true")
    ml_min_train_trades: int = field(default_factory=lambda: int(os.getenv("ML_MIN_TRAIN_TRADES", "30")))
    ml_retrain_interval_sec: int = field(default_factory=lambda: int(os.getenv("ML_RETRAIN_INTERVAL_SEC", "3600")))
    ml_blend_weight: float = field(default_factory=lambda: float(os.getenv("ML_BLEND_WEIGHT", "0.4")))
    ml_safety_margin_discount: float = field(default_factory=lambda: float(os.getenv("ML_SAFETY_MARGIN_DISCOUNT", "0.65")))
    ml_exit_factor: float = field(default_factory=lambda: float(os.getenv("ML_EXIT_FACTOR", "0.90")))
    ml_min_confidence: float = field(default_factory=lambda: float(os.getenv("ML_MIN_CONFIDENCE", "0.20")))
    ml_safety_margin_required: bool = field(default_factory=lambda: os.getenv("ML_SAFETY_MARGIN_REQUIRED", "false").lower() == "true")

    # Feature 8: Predict.fun sniper (crypto up/down expiry snipe)
    predict_enabled: bool = field(default_factory=lambda: os.getenv("PREDICT_ENABLED", "true").lower() == "true")
    predict_api_key: str = field(default_factory=lambda: os.getenv("PREDICT_API_KEY", ""))
    predict_private_key: str = field(default_factory=lambda: os.getenv("PREDICT_PRIVATE_KEY", ""))
    predict_account: str = field(default_factory=lambda: os.getenv("PREDICT_ACCOUNT", ""))
    predict_scan_interval_sec: int = field(default_factory=lambda: int(os.getenv("PREDICT_SCAN_INTERVAL_SEC", "20")))
    predict_snipe_max_minutes: float = field(default_factory=lambda: float(os.getenv("PREDICT_SNIPE_MAX_MINUTES", "8.0")))
    predict_snipe_min_strike_dist: float = field(default_factory=lambda: float(os.getenv("PREDICT_SNIPE_MIN_STRIKE_DIST", "0.0008")))
    predict_snipe_max_entry_price: float = field(default_factory=lambda: float(os.getenv("PREDICT_SNIPE_MAX_ENTRY_PRICE", "0.75")))
    predict_snipe_min_edge: float = field(default_factory=lambda: float(os.getenv("PREDICT_SNIPE_MIN_EDGE", "0.02")))
    predict_snipe_bet_size: int = field(default_factory=lambda: int(os.getenv("PREDICT_SNIPE_BET_SIZE", "5")))
    predict_taker_fee_rate: float = field(default_factory=lambda: float(os.getenv("PREDICT_TAKER_FEE_RATE", "0.02")))
    predict_claim_interval_sec: int = field(default_factory=lambda: int(os.getenv("PREDICT_CLAIM_INTERVAL_SEC", "300")))
    predict_max_open_positions: int = field(default_factory=lambda: int(os.getenv("PREDICT_MAX_OPEN_POSITIONS", "10")))
    predict_assets: str = field(default_factory=lambda: os.getenv("PREDICT_ASSETS", "BTC,ETH,BNB,SOL"))
    predict_asset_edge_buffers: str = field(default_factory=lambda: os.getenv("PREDICT_ASSET_EDGE_BUFFERS", "BTC:0.0,ETH:0.03,SOL:0.03,BNB:0.02"))

    # Feature 10: Stoikov-Avellaneda market making
    mm_enabled: bool = field(default_factory=lambda: os.getenv("MM_ENABLED", "false").lower() == "true")
    mm_dry_run: bool = field(default_factory=lambda: os.getenv("MM_DRY_RUN", "true").lower() == "true")
    mm_live_confirm: bool = field(default_factory=lambda: os.getenv("MM_LIVE_CONFIRM", "false").lower() == "true")
    mm_gamma: float = field(default_factory=lambda: float(os.getenv("MM_GAMMA", "0.1")))
    mm_k: float = field(default_factory=lambda: float(os.getenv("MM_K", "1.5")))
    mm_min_spread_bps: int = field(default_factory=lambda: int(os.getenv("MM_MIN_SPREAD_BPS", "200")))
    mm_max_inventory_usd: float = field(default_factory=lambda: float(os.getenv("MM_MAX_INVENTORY_USD", "10")))
    mm_max_open_markets: int = field(default_factory=lambda: int(os.getenv("MM_MAX_OPEN_MARKETS", "3")))
    mm_daily_cap_usd: float = field(default_factory=lambda: float(os.getenv("MM_DAILY_CAP_USD", "50")))
    mm_poll_interval_sec: int = field(default_factory=lambda: int(os.getenv("MM_POLL_INTERVAL_SEC", "10")))
    mm_min_time_to_close_sec: int = field(default_factory=lambda: int(os.getenv("MM_MIN_TIME_TO_CLOSE_SEC", "300")))
    mm_max_time_to_close_sec: int = field(default_factory=lambda: int(os.getenv("MM_MAX_TIME_TO_CLOSE_SEC", "3600")))
    mm_kill_switch_file: str = field(default_factory=lambda: os.getenv("MM_KILL_SWITCH_FILE", "data/KILL_MM"))
    # σ (volatility) estimation envelope — fixes 0% fill rate on flat Polymarket mids.
    # Prior default σ=0.05 fallback + 8% half-spread cap produced bid 0.42 / ask 0.58
    # on 0.500-ish mids → never crossed. New defaults target 5-15% fill rate.
    mm_sigma_floor: float = field(default_factory=lambda: float(os.getenv("MM_SIGMA_FLOOR", "0.001")))
    mm_sigma_cap: float = field(default_factory=lambda: float(os.getenv("MM_SIGMA_CAP", "0.05")))
    mm_sigma_default: float = field(default_factory=lambda: float(os.getenv("MM_SIGMA_DEFAULT", "0.01")))
    mm_sigma_lookback: int = field(default_factory=lambda: int(os.getenv("MM_SIGMA_LOOKBACK", "30")))
    mm_sigma_min_samples: int = field(default_factory=lambda: int(os.getenv("MM_SIGMA_MIN_SAMPLES", "5")))
    mm_half_spread_cap: float = field(default_factory=lambda: float(os.getenv("MM_HALF_SPREAD_CAP", "0.03")))
    # Bayesian directional prior augmenting the Avellaneda-Stoikov MM.
    # Shadow-by-default: when MM_DRY_RUN=true the prior only logs to data/mm_bayes.jsonl.
    mm_bayes_enabled: bool = field(default_factory=lambda: os.getenv("MM_BAYES_ENABLED", "false").lower() == "true")
    mm_bayes_lookback_min: float = field(default_factory=lambda: float(os.getenv("MM_BAYES_LOOKBACK_MIN", "5")))
    mm_bayes_momentum_weight: float = field(default_factory=lambda: float(os.getenv("MM_BAYES_MOMENTUM_WEIGHT", "0.3")))
    mm_bayes_ttc_weight: float = field(default_factory=lambda: float(os.getenv("MM_BAYES_TTC_WEIGHT", "0.1")))
    mm_bayes_threshold_up: float = field(default_factory=lambda: float(os.getenv("MM_BAYES_THRESHOLD_UP", "0.55")))
    mm_bayes_threshold_down: float = field(default_factory=lambda: float(os.getenv("MM_BAYES_THRESHOLD_DOWN", "0.45")))
    mm_hold_min_shares: float = field(default_factory=lambda: float(os.getenv("MM_HOLD_MIN_SHARES", "10")))

    # Feature 9: Weather market strategy
    weather_enabled: bool = field(default_factory=lambda: os.getenv("WEATHER_ENABLED", "true").lower() == "true")
    weather_scan_interval_sec: int = field(default_factory=lambda: int(os.getenv("WEATHER_SCAN_INTERVAL_SEC", "120")))
    weather_forecast_refresh_sec: int = field(default_factory=lambda: int(os.getenv("WEATHER_FORECAST_REFRESH_SEC", "600")))
    weather_min_edge: float = field(default_factory=lambda: float(os.getenv("WEATHER_MIN_EDGE", "0.15")))
    weather_min_liquidity: float = field(default_factory=lambda: float(os.getenv("WEATHER_MIN_LIQUIDITY", "50")))
    weather_max_bet: float = field(default_factory=lambda: float(os.getenv("WEATHER_MAX_BET", "3.0")))
    weather_min_bet: float = field(default_factory=lambda: float(os.getenv("WEATHER_MIN_BET", "0.5")))
    weather_kelly_fraction: float = field(default_factory=lambda: float(os.getenv("WEATHER_KELLY_FRACTION", "0.08")))
    weather_max_positions: int = field(default_factory=lambda: int(os.getenv("WEATHER_MAX_POSITIONS", "5")))
    weather_target_cities: str = field(default_factory=lambda: os.getenv("WEATHER_TARGET_CITIES", "new-york,london,chicago,seoul,hong-kong"))
    # Weather exit monitor
    weather_exit_enabled: bool = field(default_factory=lambda: os.getenv("WEATHER_EXIT_ENABLED", "true").lower() == "true")
    weather_exit_check_interval_sec: int = field(default_factory=lambda: int(os.getenv("WEATHER_EXIT_CHECK_SEC", "60")))
    weather_exit_edge_buffer: float = field(default_factory=lambda: float(os.getenv("WEATHER_EXIT_EDGE_BUFFER", "0.05")))
    weather_exit_forecast_drop_pct: float = field(default_factory=lambda: float(os.getenv("WEATHER_EXIT_FORECAST_DROP_PCT", "0.30")))
    weather_exit_min_sell_price_ratio: float = field(default_factory=lambda: float(os.getenv("WEATHER_EXIT_MIN_SELL_RATIO", "0.3")))
    weather_exit_urgent_hours: float = field(default_factory=lambda: float(os.getenv("WEATHER_EXIT_URGENT_HOURS", "2.0")))

    def active_market_groups(self) -> list[str]:
        raw = [item.strip().lower() for item in self.enabled_market_groups.split(",")]
        groups = [item for item in raw if item]
        return groups or ["btc_15m"]


