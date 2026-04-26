"""ML-enhanced probability model: Random Forest + safety margin entry filter.

Inspired by the 100+ signals / Random Forest approach, adapted for
Polymarket BTC/ETH Up/Down binary markets.

Key ideas:
- Random Forest ensemble predicts contract resolution probability (0-1)
- Safety margin: only enter when market_price <= model_prob * discount_factor
- Exit target: sell when market_price >= model_prob * exit_factor
- Sharpe Ratio evaluation using log returns
- MAE/MFE tracking for exit optimization
"""

import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger("polybot")

FEATURE_NAMES = [
    "rsi",
    "bb_position",          # (price - bb_mid) / (bb_width/2), normalized
    "bb_width_pct",
    "vwap_distance",        # (price - vwap) / vwap
    "momentum",
    "trend_short",          # 5-candle trend
    "trend_mid",            # 10-candle trend
    "trend_long",           # 20-candle trend
    "htf_trend",
    "vol_regime_code",      # low=0, medium=1, high=2
    "market_price",         # up_price or down_price (entry price)
    "implied_prob_up",
    "minutes_to_expiry",
    "liquidity_log",        # log(liquidity)
    "duration_minutes",
    "strike_distance_pct",  # (current_price - strike) / strike
    "orderbook_sentiment",
    "hour_of_day",          # 0-23 UTC
    "day_of_week",          # 0-6
    "price_momentum_7d",    # proxy: trend_long * bb_width
    "volume_regime",        # recent_vol / avg_vol ratio proxy
    "direction_bias",       # raw direction bias from PriceEngine
    "model_prob_rule",      # rule-based model_prob_up from PriceEngine
]


@dataclass
class MLPrediction:
    ml_prob: float              # ML model probability (0-1)
    blended_prob: float         # blended with rule-based
    confidence: float           # model confidence (0-1)
    safety_margin_ok: bool      # market_price <= blended_prob * discount
    entry_value_ratio: float    # market_price / blended_prob (lower = better)
    exit_target: float          # blended_prob * exit_factor
    features_used: int


@dataclass
class TradeMetrics:
    """MAE/MFE tracker for a single trade."""
    trade_id: int
    entry_price: float
    model_prob: float
    mae: float = 0.0           # max adverse excursion (worst unrealized loss)
    mfe: float = 0.0           # max favorable excursion (best unrealized gain)
    peak_price: float = 0.0
    trough_price: float = 1.0

    def update(self, current_price: float, side: str):
        if side == "UP":
            self.peak_price = max(self.peak_price, current_price)
            self.trough_price = min(self.trough_price, current_price)
            self.mfe = max(self.mfe, current_price - self.entry_price)
            self.mae = min(self.mae, current_price - self.entry_price)
        else:  # DOWN
            self.peak_price = max(self.peak_price, current_price)
            self.trough_price = min(self.trough_price, current_price)
            # For DOWN side, price going down is favorable
            self.mfe = max(self.mfe, self.entry_price - current_price)
            self.mae = min(self.mae, self.entry_price - current_price)


class MLModel:
    """Random Forest model for Polymarket probability prediction."""

    def __init__(self, config):
        self.config = config
        self._model = None
        self._scaler = None
        self._is_trained = False
        self._last_train_ts = 0.0
        self._train_count = 0
        self._trade_metrics: dict[int, TradeMetrics] = {}
        self._closed_returns: list[float] = []  # log returns for Sharpe
        self._model_path = Path(config.base_dir) / "ml_model_state.json"

    @property
    def is_trained(self) -> bool:
        return self._is_trained

    def extract_features(
        self,
        signal_dict: dict,
        market_price: float,
        implied_prob_up: float,
        minutes_to_expiry: float,
        liquidity: float,
        duration_minutes: float,
        strike_distance_pct: float = 0.0,
        orderbook_sentiment: float = 0.0,
        direction_bias: float = 0.0,
        model_prob_rule: float = 0.5,
    ) -> np.ndarray:
        """Extract feature vector from signal + market data."""
        price = signal_dict.get("price", 0.0)
        bb_mid = signal_dict.get("bb_mid", price)
        bb_upper = signal_dict.get("bb_upper", price)
        bb_lower = signal_dict.get("bb_lower", price)
        bb_width = bb_upper - bb_lower

        bb_position = 0.0
        if bb_width > 0:
            bb_position = np.clip((price - bb_mid) / (bb_width / 2.0), -2.0, 2.0)

        vwap = signal_dict.get("vwap", price)
        vwap_distance = ((price - vwap) / vwap) if vwap > 0 else 0.0

        vol_regime_map = {"low": 0, "medium": 1, "high": 2}
        vol_regime_code = vol_regime_map.get(signal_dict.get("vol_regime", "medium"), 1)

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

        features = np.array([
            signal_dict.get("rsi", 50.0),
            bb_position,
            signal_dict.get("bb_width_pct", 0.005),
            vwap_distance,
            signal_dict.get("momentum", 0.0),
            signal_dict.get("trend_strength", 0.0),  # short trend proxy
            signal_dict.get("trend_strength", 0.0) * 0.8,  # mid trend proxy
            signal_dict.get("trend_strength", 0.0) * 0.5,  # long trend proxy
            signal_dict.get("htf_trend", 0.0),
            float(vol_regime_code),
            market_price,
            implied_prob_up,
            minutes_to_expiry,
            math.log1p(max(0, liquidity)),
            duration_minutes,
            strike_distance_pct,
            orderbook_sentiment,
            float(now.hour),
            float(now.weekday()),
            signal_dict.get("trend_strength", 0.0) * signal_dict.get("bb_width_pct", 0.005),
            1.0,  # volume regime placeholder
            direction_bias,
            model_prob_rule,
        ], dtype=np.float64)

        return features

    def train(self, db_logger, min_trades: int = 30):
        """Train RF model on historical closed trades."""
        try:
            from sklearn.ensemble import RandomForestClassifier
            from sklearn.preprocessing import StandardScaler
        except ImportError:
            logger.warning("[ML] scikit-learn not installed. ML model disabled.")
            return False

        closed = db_logger.get_closed_trades(limit=2000, include_shadow=False)
        if len(closed) < min_trades:
            logger.info(f"[ML] Not enough trades to train: {len(closed)}/{min_trades}")
            return False

        X_list = []
        y_list = []
        for trade in closed:
            signal_json = trade.get("signal_values", "{}")
            try:
                sig = json.loads(signal_json) if isinstance(signal_json, str) else (signal_json or {})
            except (json.JSONDecodeError, TypeError):
                sig = {}

            if not sig:
                continue

            pnl = float(trade.get("pnl", 0) or 0)
            label = 1 if pnl > 0 else 0

            entry_price = float(trade.get("entry_price", 0.5) or 0.5)
            model_prob = float(trade.get("model_prob", 0.5) or 0.5)
            market_prob = float(trade.get("market_prob", 0.5) or 0.5)
            minutes = float(trade.get("minutes_to_expiry", 10) or 10)
            liquidity = float(trade.get("market_liquidity", 1000) or 1000)
            duration = float(trade.get("market_duration_min", 15) or 15)

            features = self.extract_features(
                signal_dict=sig,
                market_price=entry_price,
                implied_prob_up=market_prob,
                minutes_to_expiry=minutes,
                liquidity=liquidity,
                duration_minutes=duration,
                direction_bias=sig.get("direction_bias", 0.0),
                model_prob_rule=model_prob,
            )
            X_list.append(features)
            y_list.append(label)

        if len(X_list) < min_trades:
            logger.info(f"[ML] Not enough valid feature vectors: {len(X_list)}/{min_trades}")
            return False

        X = np.array(X_list)
        y = np.array(y_list)

        # Handle NaN/Inf
        X = np.nan_to_num(X, nan=0.0, posinf=1e6, neginf=-1e6)

        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)

        # Random Forest: sqrt(n_features) features per tree (as per tweet)
        n_features = X.shape[1]
        max_features_per_tree = max(1, int(math.sqrt(n_features)))

        model = RandomForestClassifier(
            n_estimators=150,
            max_features=max_features_per_tree,
            max_depth=8,
            min_samples_leaf=5,
            class_weight="balanced",
            random_state=42,
            n_jobs=-1,
        )
        model.fit(X_scaled, y)

        self._model = model
        self._scaler = scaler
        self._is_trained = True
        self._last_train_ts = time.time()
        self._train_count = len(X_list)

        # Feature importances for logging
        importances = model.feature_importances_
        top_idx = np.argsort(importances)[-5:][::-1]
        top_features = [(FEATURE_NAMES[i] if i < len(FEATURE_NAMES) else f"f{i}", importances[i]) for i in top_idx]
        top_str = ", ".join(f"{name}={imp:.3f}" for name, imp in top_features)
        logger.info(f"[ML] Trained on {len(X_list)} trades. Top features: {top_str}")

        # OOB or simple train accuracy for sanity check
        train_acc = model.score(X_scaled, y)
        logger.info(f"[ML] Train accuracy: {train_acc:.1%} (n_trees={model.n_estimators}, max_features={max_features_per_tree})")

        return True

    def predict(
        self,
        signal_dict: dict,
        market_price: float,
        implied_prob_up: float,
        minutes_to_expiry: float,
        liquidity: float,
        duration_minutes: float,
        side: str,
        strike_distance_pct: float = 0.0,
        orderbook_sentiment: float = 0.0,
        direction_bias: float = 0.0,
        model_prob_rule: float = 0.5,
    ) -> MLPrediction:
        """Predict probability and check safety margin."""
        ml_prob = model_prob_rule  # fallback to rule-based
        confidence = 0.0
        features_used = 0

        if self._is_trained and self._model is not None and self._scaler is not None:
            try:
                features = self.extract_features(
                    signal_dict=signal_dict,
                    market_price=market_price,
                    implied_prob_up=implied_prob_up,
                    minutes_to_expiry=minutes_to_expiry,
                    liquidity=liquidity,
                    duration_minutes=duration_minutes,
                    strike_distance_pct=strike_distance_pct,
                    orderbook_sentiment=orderbook_sentiment,
                    direction_bias=direction_bias,
                    model_prob_rule=model_prob_rule,
                )
                features = np.nan_to_num(features, nan=0.0, posinf=1e6, neginf=-1e6)
                X = self._scaler.transform(features.reshape(1, -1))
                proba = self._model.predict_proba(X)[0]

                # proba[1] = probability of class 1 (winning trade)
                ml_prob_win = float(proba[1]) if len(proba) > 1 else 0.5
                features_used = len(features)

                # Convert "win probability" to directional probability
                # If predicting UP side: ml_prob = win_prob (higher = more likely UP wins)
                # If predicting DOWN side: ml_prob = win_prob (higher = more likely DOWN wins)
                ml_prob = ml_prob_win

                # Confidence: how far from 0.5 (uncertain)
                confidence = abs(ml_prob - 0.5) * 2.0

            except Exception as e:
                logger.debug(f"[ML] Prediction failed: {e}")
                ml_prob = model_prob_rule

        # Blend ML with rule-based
        ml_weight = self.config.ml_blend_weight if self._is_trained else 0.0
        blended_prob = ml_prob * ml_weight + model_prob_rule * (1.0 - ml_weight)
        blended_prob = np.clip(blended_prob, 0.05, 0.95)

        # Safety margin check: market_price <= blended_prob * discount_factor
        discount = self.config.ml_safety_margin_discount
        safety_margin_ok = market_price <= blended_prob * discount

        entry_value_ratio = market_price / blended_prob if blended_prob > 0 else 999.0

        # Exit target
        exit_target = blended_prob * self.config.ml_exit_factor

        return MLPrediction(
            ml_prob=ml_prob,
            blended_prob=blended_prob,
            confidence=confidence,
            safety_margin_ok=safety_margin_ok,
            entry_value_ratio=entry_value_ratio,
            exit_target=exit_target,
            features_used=features_used,
        )

    # --- MAE/MFE Tracking ---

    def start_tracking(self, trade_id: int, entry_price: float, model_prob: float):
        self._trade_metrics[trade_id] = TradeMetrics(
            trade_id=trade_id,
            entry_price=entry_price,
            model_prob=model_prob,
            peak_price=entry_price,
            trough_price=entry_price,
        )

    def update_tracking(self, trade_id: int, current_price: float, side: str):
        metrics = self._trade_metrics.get(trade_id)
        if metrics:
            metrics.update(current_price, side)

    def finish_tracking(self, trade_id: int, exit_price: float, pnl: float) -> Optional[dict]:
        metrics = self._trade_metrics.pop(trade_id, None)
        if metrics is None:
            return None

        # Log return for Sharpe calculation
        if metrics.entry_price > 0:
            if pnl >= 0:
                # Won: bought at entry_price, resolved to ~1.0
                log_ret = math.log(max(0.01, exit_price) / max(0.01, metrics.entry_price))
            else:
                log_ret = math.log(max(0.01, exit_price) / max(0.01, metrics.entry_price))
            self._closed_returns.append(log_ret)

        return {
            "mae": round(metrics.mae, 4),
            "mfe": round(metrics.mfe, 4),
            "peak_price": round(metrics.peak_price, 4),
            "trough_price": round(metrics.trough_price, 4),
            "left_on_table": round(metrics.mfe - (exit_price - metrics.entry_price), 4),
        }

    # --- Sharpe Ratio ---

    def compute_sharpe_ratio(self, risk_free_rate: float = 0.0) -> float:
        """Compute Sharpe Ratio from log returns of closed trades."""
        if len(self._closed_returns) < 5:
            return 0.0
        returns = np.array(self._closed_returns)
        mean_ret = np.mean(returns)
        std_ret = np.std(returns)
        if std_ret == 0:
            return 0.0
        return float((mean_ret - risk_free_rate) / std_ret)

    def get_performance_summary(self) -> dict:
        """Summary stats for dashboard/logging."""
        sharpe = self.compute_sharpe_ratio()
        n_returns = len(self._closed_returns)
        avg_mae = 0.0
        avg_mfe = 0.0
        if self._trade_metrics:
            maes = [m.mae for m in self._trade_metrics.values()]
            mfes = [m.mfe for m in self._trade_metrics.values()]
            avg_mae = np.mean(maes) if maes else 0.0
            avg_mfe = np.mean(mfes) if mfes else 0.0

        sharpe_label = "bad" if sharpe < 1 else ("good" if sharpe < 2 else "excellent")

        return {
            "ml_trained": self._is_trained,
            "train_count": self._train_count,
            "sharpe_ratio": round(sharpe, 3),
            "sharpe_label": sharpe_label,
            "n_closed_returns": n_returns,
            "avg_return": round(float(np.mean(self._closed_returns)), 4) if self._closed_returns else 0.0,
            "active_tracks": len(self._trade_metrics),
            "avg_mae": round(float(avg_mae), 4),
            "avg_mfe": round(float(avg_mfe), 4),
        }

    def should_retrain(self, interval_sec: int = 3600) -> bool:
        """Check if model should be retrained."""
        if not self._is_trained:
            return True
        return (time.time() - self._last_train_ts) > interval_sec
