"""Stoikov-Avellaneda market-making for Polymarket Up/Down binary markets.

Design notes
------------
- expiry_snipe went $249→$99 because directional limit-BUY on Up/Down gets
  adversarial-selected: the order only fills when price has moved against
  the bettor. The answer isn't "don't trade Up/Down" — it is "provide
  BOTH sides of the book and earn the spread". This module is the MM side.

- Stoikov-Avellaneda quote:
    reservation  r = s - q*gamma*sigma^2*(T-t)
    half_spread  d = (gamma*sigma^2*(T-t) + (2/gamma)*ln(1+gamma/k)) / 2
    bid          = r - d
    ask          = r + d
  where s=mid, q=net inventory (positive=long), gamma=risk aversion,
  sigma=volatility of 1-min returns, (T-t)=seconds to expiry (scaled), k=liquidity.

- Polymarket specifics that bite:
    * min order cost >= $1 (enforced in MarketScanner.place_order)
    * min price 0.001, max 0.999 (reject 0 or 1)
    * SELL requires shares in inventory → we start in 'accumulation' mode
      (BUY-only quotes) and switch to 'two_sided' after we own shares.
    * post-only is honored by ClobClient.post_order(order, post_only=True).
    * mid-price limits historically had ~86% non-fill in directional snipe
      — that's FINE here: we WANT to rest on the book as a maker.

- Three safety locks:
    MM_ENABLED     — master switch (default False)
    MM_LIVE_CONFIRM— must be True OR we stay in shadow/dry-run even when enabled
    MM_KILL_SWITCH_FILE — file existence pauses quoting within one tick

- Zero modifications required to market_scanner.py or data_logger.py.
  We access `scanner._get_clob_client()` for the direct post_order path
  since MarketScanner.place_order uses create_and_post_order which does
  not expose post_only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    from bayesian_prior import BayesianDirection
    BAYES_AVAILABLE = True
except ImportError:
    BayesianDirection = None  # type: ignore
    BAYES_AVAILABLE = False

logger = logging.getLogger("polybot")


# Polymarket CLOB constraints (duplicated from market_scanner for explicitness)
_MIN_PRICE = 0.001
_MAX_PRICE = 0.999
_MIN_SHARES = 5.0
_MIN_NOTIONAL = 1.0
_TICK = 0.001  # CLOB price tick


def _clamp_price(p: float) -> float:
    if p != p or p <= 0:  # NaN/neg guard
        return _MIN_PRICE
    return max(_MIN_PRICE, min(_MAX_PRICE, p))


def _round_tick(p: float) -> float:
    return round(round(p / _TICK) * _TICK, 4)


@dataclass
class QuoteState:
    """Per-market live MM state."""
    market_id: str
    token_id: str  # UP token we quote
    mid_samples: deque = field(default_factory=lambda: deque(maxlen=64))
    inventory_shares: float = 0.0
    inventory_cost_usd: float = 0.0
    open_bid_order_id: Optional[str] = None
    open_ask_order_id: Optional[str] = None
    last_bid_price: float = 0.0
    last_ask_price: float = 0.0
    last_quote_ts: float = 0.0
    mode: str = "accumulation"  # 'accumulation' | 'two_sided' | 'wind_down'
    db_trade_id: int = -1
    expiry_ts: float = 0.0
    # Bayesian directional-bias state (used only when MM_BAYES_ENABLED=true)
    hold_mode: bool = False             # True → cancel all quotes, hold inventory to resolution
    target_side: str = ""               # "UP" or "DOWN" once prior commits; "" = undecided
    last_prob_up: float = 0.5           # last computed P(UP) for shadow telemetry
    last_bayes_action: str = "both_sides"  # 'buy_only' | 'sell_only' | 'both_sides'


class StoikovMM:
    """Avellaneda–Stoikov market maker on Polymarket UP/DOWN tokens."""

    def __init__(self, scanner: Any, data_logger: Any, config: Any):
        self.scanner = scanner
        self.db = data_logger
        self.config = config
        # Switches
        self.enabled = bool(getattr(config, "mm_enabled", False))
        self.dry_run = bool(getattr(config, "mm_dry_run", True))
        self.live_confirm = bool(getattr(config, "mm_live_confirm", False))
        # Parameters
        self.gamma = float(getattr(config, "mm_gamma", 0.1))
        self.k = float(getattr(config, "mm_k", 1.5))
        self.min_spread_bps = int(getattr(config, "mm_min_spread_bps", 200))
        self.max_inventory_usd = float(getattr(config, "mm_max_inventory_usd", 10.0))
        self.max_open_markets = int(getattr(config, "mm_max_open_markets", 3))
        self.daily_cap_usd = float(getattr(config, "mm_daily_cap_usd", 50.0))
        self.poll_sec = int(getattr(config, "mm_poll_interval_sec", 10))
        self.min_ttc = int(getattr(config, "mm_min_time_to_close_sec", 300))
        self.max_ttc = int(getattr(config, "mm_max_time_to_close_sec", 3600))
        # --- σ estimation envelope (calibrated for Polymarket binary mids) ---
        # Observed Polymarket mids stay flat at 0.500–0.505 for long windows →
        # previous default σ=0.05 yielded half-spread ≈ 8% and zero fills.
        # Cap σ from the *measured* buffer and enforce a floor/ceiling.
        self.sigma_floor = float(getattr(config, "mm_sigma_floor", 0.001))
        self.sigma_cap = float(getattr(config, "mm_sigma_cap", 0.05))
        self.sigma_default = float(getattr(config, "mm_sigma_default", 0.01))
        self.sigma_lookback = int(getattr(config, "mm_sigma_lookback", 30))
        self.sigma_min_samples = int(getattr(config, "mm_sigma_min_samples", 5))
        # Hard cap on half-spread regardless of Stoikov formula (binary [0,1] overshoot guard)
        self.half_spread_cap = float(getattr(config, "mm_half_spread_cap", 0.03))
        # Kill switch path
        kill_file = getattr(config, "mm_kill_switch_file", "data/KILL_MM")
        self.kill_switch = Path(kill_file)
        # Per-quote sizing (shares notional target ≈ inventory cap / 4)
        self.quote_notional_usd = max(_MIN_NOTIONAL, self.max_inventory_usd / 4.0)
        # State
        self._states: dict[str, QuoteState] = {}
        self._day_key: str = ""
        self._day_spent_usd: float = 0.0  # gross USD BUYs attempted today
        # JSONL decision log
        self._log_dir = Path(getattr(config, "base_dir", ".")) / "data"
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._jsonl_path = self._log_dir / "mm_quotes.jsonl"
        # --- Bayesian directional prior (shadow-by-default) ---
        self.bayes_enabled = bool(getattr(config, "mm_bayes_enabled", False)) and BAYES_AVAILABLE
        self.bayes_threshold_up = float(getattr(config, "mm_bayes_threshold_up", 0.55))
        self.bayes_threshold_down = float(getattr(config, "mm_bayes_threshold_down", 0.45))
        self.hold_min_shares = float(getattr(config, "mm_hold_min_shares", 10.0))
        self.bayes: Optional[Any] = None
        if self.bayes_enabled:
            try:
                self.bayes = BayesianDirection(
                    lookback_min=float(getattr(config, "mm_bayes_lookback_min", 5.0)),
                    momentum_weight=float(getattr(config, "mm_bayes_momentum_weight", 0.3)),
                    ttc_weight=float(getattr(config, "mm_bayes_ttc_weight", 0.1)),
                    threshold_up=self.bayes_threshold_up,
                    threshold_down=self.bayes_threshold_down,
                    log_path=str(self._log_dir / "mm_bayes.jsonl"),
                )
                logger.info(
                    f"[MM-BAYES] enabled lookback={getattr(config,'mm_bayes_lookback_min',5.0)}min "
                    f"mom_w={getattr(config,'mm_bayes_momentum_weight',0.3)} "
                    f"ttc_w={getattr(config,'mm_bayes_ttc_weight',0.1)} "
                    f"thr_up={self.bayes_threshold_up} thr_dn={self.bayes_threshold_down} "
                    f"hold_min_shares={self.hold_min_shares}"
                )
            except Exception as exc:
                logger.error(f"[MM-BAYES] init failed: {exc}", exc_info=True)
                self.bayes = None
                self.bayes_enabled = False

    # -------------------------- PUBLIC LOOP --------------------------

    async def run_loop(self):
        if not self.enabled:
            logger.info("[MM] disabled (MM_ENABLED=false). run_loop exiting.")
            return
        mode_label = "DRY_RUN" if (self.dry_run or not self.live_confirm) else "LIVE"
        logger.info(
            f"[MM] StoikovMM starting mode={mode_label} gamma={self.gamma} k={self.k} "
            f"min_spread_bps={self.min_spread_bps} max_inv=${self.max_inventory_usd} "
            f"max_markets={self.max_open_markets} daily_cap=${self.daily_cap_usd}"
        )
        # Warm-up: let scanner/price engines breathe
        await asyncio.sleep(8)
        while True:
            try:
                await self._mm_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[MM] tick error: {exc}", exc_info=True)
            await asyncio.sleep(self.poll_sec)

    # -------------------------- CORE TICK --------------------------

    async def _mm_tick(self):
        # Kill switch: cancel everything + pause
        if self.kill_switch.exists():
            if self._states:
                logger.warning(f"[MM] KILL switch present at {self.kill_switch}. Cancelling all quotes.")
                await self._cancel_all()
            return

        # Daily cap rollover
        day_now = time.strftime("%Y-%m-%d", time.gmtime())
        if day_now != self._day_key:
            self._day_key = day_now
            self._day_spent_usd = 0.0

        # 1) Update existing quoted markets (re-quote / exit / close)
        await self._manage_open_quotes()

        # 2) If we still have headroom, scan for new candidates
        if len([s for s in self._states.values() if s.mode != "wind_down"]) >= self.max_open_markets:
            return
        if self._day_spent_usd >= self.daily_cap_usd:
            self._throttle_log("mm:daily_cap", f"[MM] daily cap ${self.daily_cap_usd} reached; no new quotes.")
            return

        candidates = await self._scan_mm_candidates()
        for cand in candidates:
            if len([s for s in self._states.values() if s.mode != "wind_down"]) >= self.max_open_markets:
                break
            if self._day_spent_usd >= self.daily_cap_usd:
                break
            await self._open_quote_for_market(cand)

    # -------------------------- CANDIDATE SCAN --------------------------

    async def _scan_mm_candidates(self) -> list[dict]:
        now = time.time()
        specs = getattr(self.scanner, "market_specs", [])
        candidates: list[dict] = []

        # Fetch markets across specs in parallel
        async def _fetch(spec):
            try:
                return await self.scanner.fetch_active_markets(spec)
            except Exception as exc:
                logger.debug(f"[MM] fetch {getattr(spec,'group_id','?')} failed: {exc}")
                return []

        results = await asyncio.gather(*[_fetch(s) for s in specs], return_exceptions=False)

        for markets in results:
            for m in markets:
                try:
                    ttc = float(m.expiry_timestamp) - now
                    if ttc < self.min_ttc or ttc > self.max_ttc:
                        continue
                    if float(getattr(m, "liquidity", 0.0) or 0.0) < 500.0:
                        continue
                    mid = self._mid_from_prices(m.up_price, m.down_price)
                    if mid <= _MIN_PRICE or mid >= _MAX_PRICE:
                        continue
                    if m.market_id in self._states:
                        continue  # already quoted
                    # Orderbook check: spread must be >= min_spread
                    book = await self.scanner.get_orderbook(m.up_token_id)
                    bids = book.get("bids") or []
                    asks = book.get("asks") or []
                    if not bids or not asks:
                        continue
                    best_bid = float(bids[0].get("price") or bids[0].get("p") or 0)
                    best_ask = float(asks[0].get("price") or asks[0].get("p") or 0)
                    if best_ask <= best_bid or best_bid <= 0:
                        continue
                    spread_bps = int(round((best_ask - best_bid) * 10000))
                    if spread_bps < self.min_spread_bps:
                        continue
                    candidates.append({
                        "market": m,
                        "mid": mid,
                        "ttc": ttc,
                        "best_bid": best_bid,
                        "best_ask": best_ask,
                        "spread_bps": spread_bps,
                    })
                except Exception as exc:
                    logger.debug(f"[MM] candidate parse error: {exc}")
        # Prefer wider spread + longer TTC (more time to earn)
        candidates.sort(key=lambda c: (-c["spread_bps"], -c["ttc"]))
        return candidates[: self.max_open_markets * 2]

    # -------------------------- QUOTE MATH --------------------------

    def _mid_from_prices(self, up_price: float, down_price: float) -> float:
        # Prefer complementary mid if both valid
        if 0 < up_price < 1 and 0 < down_price < 1:
            total = up_price + down_price
            if 0.6 < total < 1.4:  # sanity band
                return up_price / total  # normalize to sum to 1
        return up_price if 0 < up_price < 1 else 0.5

    def _estimate_vol(self, state: QuoteState) -> tuple[float, dict]:
        """Rolling std of mid returns, calibrated for Polymarket binary mids.

        Returns (sigma, meta) where meta carries telemetry:
          - sigma_raw: uncapped std × √N (for diagnostics)
          - sigma_floor / sigma_cap: active bounds
          - n_samples / n_returns: buffer fill
          - source: 'default' | 'measured'

        Prior implementation returned a hard-coded 0.05 when buffer was sparse
        AND when logs-returns were noisy. On flat 0.500–0.505 mids that 0.05
        produced bid=0.42 / ask=0.58, never crossed → 0% fill rate on 2500+ quotes.

        New approach: take last `sigma_lookback` mid samples, compute stdev of
        step-to-step *linear* returns (safer than log on binary [0,1] where
        a 0.001 tick near 0 explodes), multiply by √N to scale to the window,
        then clamp to [sigma_floor, sigma_cap].
        """
        samples = list(state.mid_samples)
        n = len(samples)
        meta = {
            "sigma_raw": 0.0,
            "sigma_floor": self.sigma_floor,
            "sigma_cap": self.sigma_cap,
            "n_samples": n,
            "n_returns": 0,
            "source": "default",
        }
        if n < self.sigma_min_samples:
            return self.sigma_default, meta
        # Use only the tail `sigma_lookback` samples
        tail = samples[-self.sigma_lookback:]
        mids = [m for (_, m) in tail if m is not None and m > 0.0]
        if len(mids) < self.sigma_min_samples:
            return self.sigma_default, meta
        rets: list[float] = []
        for a, b in zip(mids[:-1], mids[1:]):
            if a > 0:
                rets.append((b - a) / a)
        if len(rets) < 2:
            return self.sigma_default, meta
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
        std = math.sqrt(max(var, 0.0))
        # Scale by √N to roughly approximate a window-aggregate σ (Stoikov
        # expects per-unit-time volatility; since our poll is ~10s and we
        # want a window-level sigma, √N gives a mild upscale. Caller keeps
        # T-t in minutes so the overall units still balance out safely under
        # the hard half_spread_cap added downstream.)
        sigma_raw = std * math.sqrt(float(len(rets)))
        if sigma_raw != sigma_raw:  # NaN guard
            sigma_raw = self.sigma_default
        sigma = max(self.sigma_floor, min(self.sigma_cap, sigma_raw))
        meta["sigma_raw"] = sigma_raw
        meta["n_returns"] = len(rets)
        meta["source"] = "measured"
        return sigma, meta

    def _compute_quotes(self, mid: float, inventory_shares: float, t_minus_t_sec: float, sigma: float) -> tuple[float, float, float]:
        """Stoikov-Avellaneda reservation price + optimal spread.

        Returns (bid, ask, reservation_price) clamped to CLOB ticks.
        """
        # Normalize T-t to "minutes remaining" so sigma (per-minute stdev) has matching units.
        tt_min = max(0.0, t_minus_t_sec / 60.0)
        # inventory in "bet units" — 1 bet unit = quote_notional_usd shares at mid
        unit_size = max(1.0, self.quote_notional_usd / max(mid, 0.05))
        q = inventory_shares / unit_size  # dimensionless inventory
        gamma = max(1e-3, self.gamma)
        k = max(1e-3, self.k)
        sigma_sq = sigma * sigma
        # reservation price
        r = mid - q * gamma * sigma_sq * tt_min
        # optimal spread (total)
        try:
            log_term = math.log(1.0 + gamma / k)
        except ValueError:
            log_term = 0.1
        total_spread = gamma * sigma_sq * tt_min + (2.0 / gamma) * log_term
        # Stoikov's closed form is derived for continuous assets where the
        # liquidity term (2/γ)·ln(1+γ/k) is small relative to price. On binary
        # prices ∈ [0,1], it dominates and produces insane quotes (±0.5). We
        # therefore hard-cap the half-spread via `mm_half_spread_cap` (default
        # 3%). Combined with the rolling-σ estimator (σ ∈ [0.001, 0.05]), this
        # keeps half-spread realistic for flat Polymarket mids (0.500–0.505).
        # 2026-04-24 post-mortem: prior hard cap of 8% + σ fallback of 0.05
        # produced bid=0.42 / ask=0.58 on 0.50-ish mids → 0% fill rate across
        # 2500+ shadow quotes. New cap default 3% targets 5–15% fill rate.
        half = total_spread / 2.0
        half = max(half, self.min_spread_bps / 20000.0)
        half = min(half, max(_TICK, self.half_spread_cap))
        bid = r - half
        ask = r + half
        # Clamp and tick
        bid = _round_tick(_clamp_price(bid))
        ask = _round_tick(_clamp_price(ask))
        r = _round_tick(_clamp_price(r))
        # Ensure strict ordering bid<r<ask
        if ask <= bid:
            ask = _round_tick(_clamp_price(bid + _TICK))
        return bid, ask, r

    # -------------------------- OPEN / MANAGE QUOTES --------------------------

    async def _open_quote_for_market(self, cand: dict):
        market = cand["market"]
        state = QuoteState(
            market_id=market.market_id,
            token_id=market.up_token_id,
            expiry_ts=float(market.expiry_timestamp),
        )
        state.mid_samples.append((time.time(), cand["mid"]))
        self._states[market.market_id] = state
        await self._requote(state, market_question=getattr(market, "question", ""))

    async def _manage_open_quotes(self):
        to_drop: list[str] = []
        for mid_key, state in list(self._states.items()):
            now = time.time()
            ttc = state.expiry_ts - now
            if ttc < 60.0:
                logger.info(f"[MM] market={mid_key[:10]} TTC={ttc:.0f}s < 60s, winding down.")
                await self._exit_on_close(state)
                to_drop.append(mid_key)
                continue
            # Refresh mid
            book = await self.scanner.get_orderbook(state.token_id)
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            if not bids or not asks:
                self._throttle_log(f"mm:empty:{mid_key}", f"[MM] market={mid_key[:10]} empty book, skipping requote.")
                continue
            best_bid = float(bids[0].get("price") or 0)
            best_ask = float(asks[0].get("price") or 0)
            if best_ask <= 0 or best_bid <= 0 or best_ask <= best_bid:
                continue
            mid = (best_bid + best_ask) / 2.0
            state.mid_samples.append((now, mid))
            # Check fills (informational — we rely on post_only, so fills happen when matched)
            await self._reconcile_fills(state)
            await self._requote(state)

        for k in to_drop:
            self._states.pop(k, None)

    async def _requote(self, state: QuoteState, market_question: str = ""):
        now = time.time()
        ttc = state.expiry_ts - now
        if ttc < 60.0:
            return
        if not state.mid_samples:
            return
        mid = state.mid_samples[-1][1]
        sigma, sigma_meta = self._estimate_vol(state)
        bid, ask, r = self._compute_quotes(mid, state.inventory_shares, ttc, sigma)
        # quote-to-mid distance in bps for fill-rate diagnostics
        bid_bps_below_mid = ((mid - bid) / mid * 10000.0) if mid > 0 else 0.0
        ask_bps_above_mid = ((ask - mid) / mid * 10000.0) if mid > 0 else 0.0
        half_spread_actual = (ask - bid) / 2.0

        # -------------------- Bayesian directional prior --------------------
        # When MM_BAYES_ENABLED=false the entire block is a no-op and we retain
        # the original symmetric behaviour (both legs always considered).
        bayes_action = "both_sides"
        prob_up = 0.5
        prob_down = 0.5
        bayes_confidence = 0.0
        prior_payload: Optional[dict] = None
        if self.bayes_enabled and self.bayes is not None:
            try:
                # Feed latest mid into the prior's rolling buffer
                self.bayes.ingest(state.market_id, now, mid)
                prior_payload = await self.bayes.compute_prior(
                    market_id=state.market_id,
                    symbol=state.token_id[:10],
                    close_price=mid,
                    ttc_sec=ttc,
                )
                prob_up = float(prior_payload.get("prob_up", 0.5))
                prob_down = float(prior_payload.get("prob_down", 0.5))
                bayes_action = str(prior_payload.get("action", "both_sides"))
                bayes_confidence = float(prior_payload.get("confidence", 0.0))
                state.last_prob_up = prob_up
                state.last_bayes_action = bayes_action
                # Adaptive skew: nudge quotes toward the favoured side.
                # Magnitude = (prob_up-0.5) mapped to ±50% of the half-spread.
                half = max(_TICK, (ask - bid) / 2.0)
                skew = max(-0.5, min(0.5, (prob_up - 0.5) * 2.0)) * half * 0.5
                if bayes_action == "buy_only":
                    # Accumulate UP side: bid more aggressively (higher bid).
                    # We do NOT move the ask because we will block it below.
                    bid = _round_tick(_clamp_price(bid + abs(skew)))
                elif bayes_action == "sell_only":
                    # Offload UP side: ask more aggressively (lower ask).
                    ask = _round_tick(_clamp_price(ask - abs(skew)))
                # 'both_sides' → leave bid/ask untouched (symmetric fallback)
                # Final ordering guard after skew
                if ask <= bid:
                    ask = _round_tick(_clamp_price(bid + _TICK))
            except Exception as exc:
                logger.debug(f"[MM-BAYES] compute_prior failed for {state.market_id[:10]}: {exc}")

        # Inventory skew / mode switching
        inv_usd = state.inventory_shares * mid
        if state.mode == "accumulation" and inv_usd >= self.max_inventory_usd * 0.5:
            state.mode = "two_sided"
        if state.mode == "two_sided" and inv_usd <= 0:
            state.mode = "accumulation"

        # -------------------- Hold-to-resolution logic ---------------------
        # If we have meaningful directional inventory AND the prior still
        # confirms UP → stop quoting; just hold until expiry pays $1/share.
        # Set `hold_mode` once; re-evaluate each tick so a prior flip re-enables
        # quoting. In shadow mode we only log — no order cancels.
        want_hold = (
            self.bayes_enabled
            and state.inventory_shares >= self.hold_min_shares
            and prob_up >= self.bayes_threshold_up
            and bayes_confidence >= 0.5
        )
        if want_hold and not state.hold_mode:
            state.hold_mode = True
            state.target_side = "UP"
            logger.info(
                f"[MM-HOLD] market={state.market_id[:10]} target=UP "
                f"shares={state.inventory_shares:.1f} prob={prob_up:.3f} "
                f"conf={bayes_confidence:.2f} ttc={ttc:.0f}s live={self._should_execute_live()}"
            )
            if self._should_execute_live():
                # Live: actually cancel resting quotes so we don't keep accumulating
                if state.open_bid_order_id:
                    await self._cancel_order(state.open_bid_order_id)
                    state.open_bid_order_id = None
                if state.open_ask_order_id:
                    await self._cancel_order(state.open_ask_order_id)
                    state.open_ask_order_id = None
        elif not want_hold and state.hold_mode:
            # Prior flipped or inventory drained — resume quoting
            state.hold_mode = False
            state.target_side = ""
            logger.info(
                f"[MM-HOLD] market={state.market_id[:10]} exiting hold_mode "
                f"prob={prob_up:.3f} shares={state.inventory_shares:.1f}"
            )

        # Hard inventory cap: stop buying when at cap
        buy_blocked = inv_usd >= self.max_inventory_usd
        # If we'd exceed daily cap, also block buys
        if self._day_spent_usd + self.quote_notional_usd > self.daily_cap_usd:
            buy_blocked = True
        # Directional blocks from Bayesian prior
        #   buy_only  → block ASK leg
        #   sell_only → block BID leg
        bayes_block_bid = self.bayes_enabled and bayes_action == "sell_only"
        bayes_block_ask = self.bayes_enabled and bayes_action == "buy_only"
        if bayes_block_bid:
            buy_blocked = True
        # Hold-mode: block both legs (we're accumulating nothing, selling nothing)
        if state.hold_mode:
            buy_blocked = True
            bayes_block_ask = True

        # Cancel stale quotes that differ by >= 1 tick from target
        if state.open_bid_order_id and abs(state.last_bid_price - bid) >= _TICK:
            await self._cancel_order(state.open_bid_order_id)
            state.open_bid_order_id = None
        if state.open_ask_order_id and abs(state.last_ask_price - ask) >= _TICK:
            await self._cancel_order(state.open_ask_order_id)
            state.open_ask_order_id = None

        placed_bid = False
        placed_ask = False

        # --- BID leg ---
        if not state.open_bid_order_id and not buy_blocked and bid >= _MIN_PRICE:
            shares = max(_MIN_SHARES, self.quote_notional_usd / max(bid, 0.01))
            if self._should_execute_live():
                oid = await self._place_maker("BUY", state.token_id, bid, shares)
            else:
                oid = f"shadow_bid_{int(time.time()*1000)}"
            if oid:
                state.open_bid_order_id = oid
                state.last_bid_price = bid
                self._day_spent_usd += bid * shares
                placed_bid = True
                # Open DB trade on first successful bid (mm_stoikov strategy_name)
                if state.db_trade_id < 0 and self._should_execute_live():
                    try:
                        state.db_trade_id = self.db.log_trade(
                            market_id=state.market_id,
                            side="UP",
                            size=float(bid * shares),
                            entry_price=float(bid),
                            signal_values={"sigma": sigma, "r": r, "half_spread": (ask - bid) / 2, "mode": state.mode},
                            model_prob=float(r),
                            market_prob=float(mid),
                            edge=float((r - mid)),
                            kelly_fraction=0.0,
                            expiry_time=str(int(state.expiry_ts)),
                            market_question=market_question,
                            order_id=str(oid),
                            mode="live",
                            strategy_name="mm_stoikov",
                            market_liquidity=0.0,
                            minutes_to_expiry=ttc / 60.0,
                            asset_symbol=str(getattr(market_question, "asset_symbol", "")) or "",
                            market_group="",
                            market_duration_min=0.0,
                            token_id=state.token_id,
                        )
                    except Exception as exc:
                        logger.warning(f"[MM] log_trade failed: {exc}")

        # --- ASK leg --- (only if we have inventory to sell OR two_sided mode)
        can_ask = (state.inventory_shares >= _MIN_SHARES) and (state.mode == "two_sided")
        # Bayesian prior may veto the ask leg entirely (heavy-buy regime)
        if bayes_block_ask:
            can_ask = False
        if not state.open_ask_order_id and can_ask and ask <= _MAX_PRICE:
            shares = min(state.inventory_shares, max(_MIN_SHARES, self.quote_notional_usd / max(ask, 0.01)))
            if self._should_execute_live():
                oid = await self._place_maker("SELL", state.token_id, ask, shares)
            else:
                oid = f"shadow_ask_{int(time.time()*1000)}"
            if oid:
                state.open_ask_order_id = oid
                state.last_ask_price = ask
                placed_ask = True

        state.last_quote_ts = now

        self._log_decision({
            "ts": now,
            "market_id": state.market_id,
            "token_id": state.token_id,
            "mid": mid,
            "sigma": sigma,
            "sigma_raw": sigma_meta.get("sigma_raw"),
            "sigma_floor": sigma_meta.get("sigma_floor"),
            "sigma_cap": sigma_meta.get("sigma_cap"),
            "sigma_n_samples": sigma_meta.get("n_samples"),
            "sigma_n_returns": sigma_meta.get("n_returns"),
            "sigma_source": sigma_meta.get("source"),
            "half_spread_actual": half_spread_actual,
            "half_spread_cap": self.half_spread_cap,
            "bid_bps_below_mid": bid_bps_below_mid,
            "ask_bps_above_mid": ask_bps_above_mid,
            "ttc_sec": ttc,
            "inventory_shares": state.inventory_shares,
            "inventory_usd": inv_usd,
            "bid": bid,
            "ask": ask,
            "r": r,
            "mode": state.mode,
            "buy_blocked": buy_blocked,
            "action_bid": "placed" if placed_bid else ("held" if state.open_bid_order_id else "skipped"),
            "action_ask": "placed" if placed_ask else ("held" if state.open_ask_order_id else "skipped"),
            "live": self._should_execute_live(),
            # Bayesian prior telemetry (null when disabled)
            "bayes_enabled": self.bayes_enabled,
            "bayes_action": bayes_action,
            "prob_up": prob_up,
            "prob_down": prob_down,
            "bayes_confidence": bayes_confidence,
            "hold_mode": state.hold_mode,
            "target_side": state.target_side,
            "momentum_score": (prior_payload or {}).get("momentum_score"),
            "ttc_score": (prior_payload or {}).get("ttc_score"),
        })
        logger.info(
            f"[MM] market={state.market_id[:10]} mid={mid:.3f} "
            f"sigma_est={sigma:.4f} sigma_cap={self.sigma_cap} sigma_floor={self.sigma_floor} "
            f"src={sigma_meta.get('source')} n={sigma_meta.get('n_returns')} "
            f"TTC={ttc:.0f}s inv={state.inventory_shares:.1f}sh (${inv_usd:.2f}) "
            f"bid={bid:.3f} ask={ask:.3f} bid_bps_below_mid={bid_bps_below_mid:.0f} "
            f"half={half_spread_actual:.4f} mode={state.mode} p_up={prob_up:.3f} "
            f"act={bayes_action} hold={state.hold_mode} live={self._should_execute_live()}"
        )

    # -------------------------- ORDER PLUMBING --------------------------

    def _should_execute_live(self) -> bool:
        if not self.enabled:
            return False
        if self.dry_run:
            return False
        if not self.live_confirm:
            return False
        return True

    async def _place_maker(self, side: str, token_id: str, price: float, shares: float) -> Optional[str]:
        """Post an order with post_only=True so it rests as a maker.

        This bypasses MarketScanner.place_order because that helper calls
        create_and_post_order which doesn't expose post_only. We use the
        underlying ClobClient directly via scanner._get_clob_client().
        """
        try:
            price = _round_tick(_clamp_price(price))
            shares = round(max(_MIN_SHARES, shares), 2)
            if price * shares < _MIN_NOTIONAL:
                logger.info(f"[MM] notional ${price*shares:.2f} below ${_MIN_NOTIONAL}, skipping.")
                return None
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.order_builder.constants import BUY, SELL
            client = self.scanner._get_clob_client()
            side_const = BUY if side == "BUY" else SELL
            # Use GTD expiration: now + 2 minutes. Post-only ensures maker-only semantics.
            expiration = int(time.time()) + 120
            order_args = OrderArgs(
                token_id=token_id,
                price=float(price),
                size=float(shares),
                side=side_const,
                expiration=expiration,
            )
            loop = asyncio.get_event_loop()

            def _build_and_post():
                ord_obj = client.create_order(order_args)
                # post_only=True; orderType defaults GTC but expiration field enforces GTD lifetime.
                return client.post_order(ord_obj, post_only=True)

            result = await asyncio.wait_for(loop.run_in_executor(None, _build_and_post), timeout=30.0)
            oid = None
            if isinstance(result, dict):
                oid = result.get("orderID") or result.get("id")
                if not result.get("success", True):
                    logger.warning(f"[MM] order rejected: {result.get('errorMsg','?')}")
                    return None
            elif hasattr(result, "order_id"):
                oid = result.order_id
            if oid:
                logger.info(f"[MM] posted {side} {shares}@{price:.3f} post_only -> {str(oid)[:20]}...")
            return oid
        except asyncio.TimeoutError:
            logger.error("[MM] order post timed out after 30s")
            return None
        except Exception as exc:
            logger.error(f"[MM] place_maker error: {exc}", exc_info=True)
            return None

    async def _cancel_order(self, order_id: str):
        if not order_id or str(order_id).startswith(("paper_", "shadow_")):
            return
        try:
            client = self.scanner._get_clob_client()
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: client.cancel(order_id))
        except Exception as exc:
            logger.debug(f"[MM] cancel {str(order_id)[:20]}... failed: {exc}")

    async def _reconcile_fills(self, state: QuoteState):
        """Poll get_order for known open orders. Update inventory when filled."""
        if not self._should_execute_live():
            return
        try:
            client = self.scanner._get_clob_client()
            loop = asyncio.get_event_loop()
            for leg in ("bid", "ask"):
                oid = state.open_bid_order_id if leg == "bid" else state.open_ask_order_id
                if not oid:
                    continue
                try:
                    order = await loop.run_in_executor(None, lambda o=oid: client.get_order(o))
                except Exception as exc:
                    logger.debug(f"[MM] get_order {str(oid)[:20]}... err: {exc}")
                    continue
                if not order:
                    continue
                status = str(order.get("status", "")).upper()
                size_matched = float(order.get("size_matched", 0) or 0)
                price = float(order.get("price", 0) or 0)
                original = float(order.get("original_size", 0) or order.get("size", 0) or 0)
                fully_filled = status == "MATCHED" or (size_matched > 0 and size_matched >= original * 0.99)
                if fully_filled:
                    if leg == "bid":
                        state.inventory_shares += size_matched
                        state.inventory_cost_usd += size_matched * price
                        state.open_bid_order_id = None
                        logger.info(f"[MM] BID filled {size_matched}@{price:.3f} inv={state.inventory_shares:.1f}")
                    else:
                        realized_pnl = size_matched * price - (
                            (state.inventory_cost_usd / max(state.inventory_shares, 1e-6)) * size_matched
                            if state.inventory_shares > 0 else 0.0
                        )
                        state.inventory_shares = max(0.0, state.inventory_shares - size_matched)
                        # reduce cost proportionally
                        if state.inventory_shares > 0:
                            state.inventory_cost_usd *= state.inventory_shares / (state.inventory_shares + size_matched)
                        else:
                            state.inventory_cost_usd = 0.0
                        state.open_ask_order_id = None
                        logger.info(f"[MM] ASK filled {size_matched}@{price:.3f} pnl=${realized_pnl:+.2f}")
                elif status in ("CANCELED", "CANCELLED", "EXPIRED"):
                    if leg == "bid":
                        state.open_bid_order_id = None
                    else:
                        state.open_ask_order_id = None
        except Exception as exc:
            logger.debug(f"[MM] reconcile_fills error: {exc}")

    async def _exit_on_close(self, state: QuoteState):
        """TTC<60s: cancel all open quotes. Leave inventory to auto-redeem on resolution."""
        if state.open_bid_order_id:
            await self._cancel_order(state.open_bid_order_id)
            state.open_bid_order_id = None
        if state.open_ask_order_id:
            await self._cancel_order(state.open_ask_order_id)
            state.open_ask_order_id = None
        state.mode = "wind_down"
        # Close DB trade if we have one (resolution-based pnl will be reconciled
        # by existing _close_trade_if_resolved pipeline; we just mark our own
        # state wound down so we don't keep quoting).
        logger.info(
            f"[MM] wound down market={state.market_id[:10]} "
            f"remaining_inv={state.inventory_shares:.1f}sh cost=${state.inventory_cost_usd:.2f} "
            f"hold_mode={state.hold_mode} last_prob_up={state.last_prob_up:.3f}"
        )
        # Release Bayesian prior buffer to free memory
        if self.bayes is not None:
            try:
                self.bayes.clear(state.market_id)
            except Exception:
                pass

    async def _cancel_all(self):
        for state in list(self._states.values()):
            await self._exit_on_close(state)

    # -------------------------- UTIL --------------------------

    def _log_decision(self, payload: dict):
        try:
            with self._jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, default=str) + "\n")
        except Exception as exc:
            logger.debug(f"[MM] jsonl write failed: {exc}")

    _log_throttle_state: dict[str, float] = {}

    def _throttle_log(self, key: str, message: str, interval_sec: int = 300):
        now = time.time()
        nxt = self._log_throttle_state.get(key, 0.0)
        if now < nxt:
            return
        logger.info(message)
        self._log_throttle_state[key] = now + interval_sec
