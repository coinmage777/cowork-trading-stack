"""Bayesian directional prior for Polymarket UP/DOWN market making.

Background
----------
The vanilla Avellaneda-Stoikov MM in `mm_strategy.py` quotes *symmetrically*
around the reservation price — it's a pure spread-capture model. But when we
studied @Dan1ro0 / @nihiiism (the ~$100K/mo Polymarket operator) we observed
their flow is **directional**, not symmetric:

  2,641 trades logged → 75% BUY / 25% SELL = asymmetric quoting.
  Round-trip WR on spread capture alone = 43% (losing).
  Net edge comes from holding winning shares to $1 resolution.

So a symmetric MM structurally **cannot** reproduce their alpha. We need a
directional prior that skews quotes toward the expected winning side.

Design
------
`BayesianDirection.compute_prior(...)` returns `{prob_up, prob_down, confidence}`
blended from two weak signals:

  1. Short-term price momentum (last `lookback_min` minutes)
       momentum = (mid_now - mid_then) / max(mid_then, 1e-6)
       momentum_score in [-1, +1] via tanh compression
  2. Time-to-close (TTC) effect
       As TTC → 0, UP token with mid > 0.5 gets closer to a $1 payout,
       DOWN token with mid < 0.5 gets closer to $0. So near expiry, the
       current mid itself is a stronger prior (the market has converged).
       ttc_score = (mid - 0.5) * (1 - ttc_sec / ttc_anchor)   // stronger near expiry

  P(UP) = 0.5 + momentum_weight * momentum_score + ttc_weight * ttc_score
       clamped to [0.05, 0.95] to avoid over-confidence from noisy signals.

  confidence = min(1.0, num_samples / min_samples_for_confidence)
       — low confidence means the prior is effectively 0.5 / 0.5.

Shadow logging
--------------
Every call logs to `data/mm_bayes.jsonl` so we can post-hoc backtest:
  * Does P(UP) > 0.55 actually correlate with UP resolution?
  * What's the calibration curve?

This module does NOT place orders. It's a pure decision-helper consumed by
`mm_strategy.StoikovMM`.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("polybot")


# TTC anchor = 1 hour. Beyond 1hr the ttc_score is dampened (mid hasn't converged).
_TTC_ANCHOR_SEC = 3600.0
_MIN_SAMPLES_FOR_CONFIDENCE = 6  # ~1 minute at poll_sec=10s


@dataclass
class PriorState:
    """Per-market rolling buffer for price samples used by the prior."""
    market_id: str
    samples: deque = field(default_factory=lambda: deque(maxlen=128))  # (ts, mid)

    def add(self, ts: float, mid: float) -> None:
        if mid <= 0 or mid >= 1:
            return
        self.samples.append((ts, mid))

    def lookup_price_at(self, target_ts: float) -> Optional[float]:
        """Return the mid closest to target_ts (±30s tolerance).

        Returns None if no sample is within tolerance — caller treats as
        "not enough data, confidence=0".
        """
        if not self.samples:
            return None
        best_mid: Optional[float] = None
        best_gap = 1e18
        for ts, mid in self.samples:
            gap = abs(ts - target_ts)
            if gap < best_gap:
                best_gap = gap
                best_mid = mid
        # Must be within ±30s of the target moment to count as "lookback_min ago"
        if best_gap > 30.0:
            return None
        return best_mid


class BayesianDirection:
    """Blend momentum + TTC signals into a directional prior P(UP)."""

    def __init__(
        self,
        lookback_min: float = 5.0,
        momentum_weight: float = 0.3,
        ttc_weight: float = 0.1,
        threshold_up: float = 0.55,
        threshold_down: float = 0.45,
        log_path: Optional[str] = None,
    ):
        self.lookback_min = float(lookback_min)
        self.momentum_weight = float(momentum_weight)
        self.ttc_weight = float(ttc_weight)
        self.threshold_up = float(threshold_up)
        self.threshold_down = float(threshold_down)
        self._states: dict[str, PriorState] = {}
        # JSONL decision log (shadow analysis)
        if log_path:
            self._log_path: Optional[Path] = Path(log_path)
            try:
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                self._log_path = None
        else:
            self._log_path = None

    # --------------------------------------------------------------
    # Public API
    # --------------------------------------------------------------

    def ingest(self, market_id: str, ts: float, mid: float) -> None:
        """Feed a fresh mid observation. Call from StoikovMM each tick."""
        st = self._states.get(market_id)
        if st is None:
            st = PriorState(market_id=market_id)
            self._states[market_id] = st
        st.add(ts, mid)

    async def compute_prior(
        self,
        market_id: str,
        symbol: str,
        close_price: float,
        ttc_sec: float,
    ) -> dict[str, Any]:
        """Return {prob_up, prob_down, confidence, action, momentum_score, ttc_score}.

        - close_price: current mid in [0, 1].
        - ttc_sec: seconds until market resolution.
        - symbol: informational (e.g. 'BTC-UP'), used only for logging.
        """
        now = time.time()
        st = self._states.get(market_id)
        momentum_score = 0.0
        lookback_price: Optional[float] = None
        n_samples = 0
        if st is not None:
            n_samples = len(st.samples)
            lookback_price = st.lookup_price_at(now - self.lookback_min * 60.0)

        # --- momentum score in [-1, +1] ---
        if lookback_price and lookback_price > 0 and close_price > 0:
            raw_ret = (close_price - lookback_price) / lookback_price
            # tanh compresses extreme moves. A 10% binary-price move in 5 min is
            # already strong directional info; tanh(0.10*5)=tanh(0.5)=~0.46.
            momentum_score = math.tanh(raw_ret * 5.0)
            # Clamp defensively
            if momentum_score != momentum_score:  # NaN guard
                momentum_score = 0.0

        # --- TTC score ---
        # Near expiry, the current mid IS the market's consensus prior.
        # 60s to close, mid=0.7 → ttc_score ≈ +0.2 * (1 - 60/3600) ≈ +0.197.
        # 50min to close, mid=0.7 → ttc_score ≈ +0.2 * (1 - 3000/3600) ≈ +0.033.
        ttc_ratio = max(0.0, min(1.0, float(ttc_sec) / _TTC_ANCHOR_SEC))
        ttc_score = (close_price - 0.5) * (1.0 - ttc_ratio)

        prob_up_raw = 0.5 + self.momentum_weight * momentum_score + self.ttc_weight * ttc_score
        prob_up = max(0.05, min(0.95, prob_up_raw))
        prob_down = 1.0 - prob_up
        confidence = min(1.0, n_samples / float(_MIN_SAMPLES_FOR_CONFIDENCE))

        # Action recommendation
        if confidence < 0.5:
            action = "both_sides"  # not enough data, stay symmetric
        elif prob_up > self.threshold_up:
            action = "buy_only"
        elif prob_up < self.threshold_down:
            action = "sell_only"
        else:
            action = "both_sides"

        payload = {
            "ts": now,
            "market_id": market_id,
            "symbol": symbol,
            "mid": float(close_price),
            "ttc_sec": float(ttc_sec),
            "lookback_min": self.lookback_min,
            "lookback_price": lookback_price,
            "momentum_score": float(momentum_score),
            "ttc_score": float(ttc_score),
            "prob_up": float(prob_up),
            "prob_down": float(prob_down),
            "confidence": float(confidence),
            "n_samples": n_samples,
            "action": action,
        }
        self._log(payload)
        return payload

    def clear(self, market_id: str) -> None:
        """Drop state for a closed market."""
        self._states.pop(market_id, None)

    # --------------------------------------------------------------
    # Internal
    # --------------------------------------------------------------

    def _log(self, payload: dict) -> None:
        if not self._log_path:
            return
        try:
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, default=str) + "\n")
        except Exception as exc:
            logger.debug(f"[MM-BAYES] jsonl write failed: {exc}")


# --------------------------------------------------------------------------
# Self-tests (run via `python bayesian_prior.py`)
# --------------------------------------------------------------------------

def _selftest() -> None:
    """Lightweight asserts. Not a full pytest suite — enough to catch regressions."""
    import asyncio

    async def run() -> None:
        bd = BayesianDirection(lookback_min=5.0, momentum_weight=0.3, ttc_weight=0.1)
        mkt = "test_mkt"
        base_ts = time.time() - 310.0  # seed 5+ min ago so lookback lands inside buffer

        # Insufficient samples → confidence low → both_sides regardless of mid
        p = await bd.compute_prior(mkt, "TEST-UP", 0.8, 600.0)
        assert p["action"] == "both_sides", f"cold-start must be symmetric: {p}"
        assert 0.05 <= p["prob_up"] <= 0.95

        # Feed rising mids across time to simulate positive momentum
        for i in range(_MIN_SAMPLES_FOR_CONFIDENCE + 4):
            # Spread samples across lookback_min*60 so 5-min-ago price is found
            bd.ingest(mkt, base_ts + i * 40.0, 0.40 + i * 0.01)

        p_up = await bd.compute_prior(mkt, "TEST-UP", 0.70, 1800.0)
        assert p_up["prob_up"] > 0.5, f"rising momentum should push prob_up above 0.5: {p_up}"
        assert p_up["momentum_score"] > 0

        # Feed falling mids for a different market
        mkt2 = "test_mkt_down"
        for i in range(_MIN_SAMPLES_FOR_CONFIDENCE + 4):
            bd.ingest(mkt2, base_ts + i * 40.0, 0.70 - i * 0.01)
        p_dn = await bd.compute_prior(mkt2, "TEST-UP", 0.40, 1800.0)
        assert p_dn["prob_up"] < 0.5, f"falling momentum should push prob_up below 0.5: {p_dn}"

        # TTC effect: same mid (0.7) near expiry → stronger UP lean
        mkt3 = "test_ttc"
        for i in range(_MIN_SAMPLES_FOR_CONFIDENCE + 4):
            bd.ingest(mkt3, base_ts + i * 40.0, 0.70)  # flat
        p_near = await bd.compute_prior(mkt3, "TEST-UP", 0.70, 60.0)
        p_far = await bd.compute_prior(mkt3, "TEST-UP", 0.70, 3500.0)
        assert p_near["ttc_score"] > p_far["ttc_score"], (
            f"near-expiry ttc_score should exceed far: near={p_near} far={p_far}"
        )

        # Clamp behavior: absurd momentum still bounded
        mkt4 = "test_clamp"
        for i in range(_MIN_SAMPLES_FOR_CONFIDENCE + 4):
            bd.ingest(mkt4, base_ts + i * 40.0, 0.01 + i * 0.001)
        p_extreme = await bd.compute_prior(mkt4, "TEST-UP", 0.99, 60.0)
        assert 0.05 <= p_extreme["prob_up"] <= 0.95

        print("OK bayesian_prior self-test")

    asyncio.run(run())


if __name__ == "__main__":
    _selftest()
