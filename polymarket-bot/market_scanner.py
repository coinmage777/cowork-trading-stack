"""Polymarket market scanning across BTC/ETH short-horizon Up/Down markets."""

import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import asyncio as _asyncio

import httpx

from config import Config

logger = logging.getLogger("polybot")


@dataclass(frozen=True)
class MarketSpec:
    group_id: str
    asset_symbol: str
    slug_asset: str
    slug_window: str
    duration_sec: int
    windows_to_scan: int

    @property
    def duration_minutes(self) -> float:
        return self.duration_sec / 60.0


@dataclass
class MarketInfo:
    market_id: str
    condition_id: str
    question: str
    up_token_id: str
    down_token_id: str
    up_price: float
    down_price: float
    implied_prob_up: float
    expiry_timestamp: float
    strike_price: Optional[float]
    liquidity: float
    minutes_to_expiry: float
    active: bool
    market_type: str = "updown"
    asset_symbol: str = "BTC"
    market_group: str = "btc_15m"
    duration_minutes: float = 15.0

    @property
    def yes_token_id(self):
        return self.up_token_id

    @property
    def no_token_id(self):
        return self.down_token_id

    @property
    def yes_price(self):
        return self.up_price

    @property
    def no_price(self):
        return self.down_price

    @property
    def implied_prob_yes(self):
        return self.implied_prob_up


class MarketScanner:
    def __init__(self, config: Config, mode: str = "paper"):
        self.config = config
        self.mode = mode
        self._gamma_client = httpx.AsyncClient(base_url="https://gamma-api.polymarket.com", timeout=15.0)
        self._client = httpx.AsyncClient(base_url=config.clob_api_url, timeout=15.0, headers={"Content-Type": "application/json"})
        self.market_specs = self._build_market_specs(config)
        self._cached_markets: dict[str, list[MarketInfo]] = {}
        self._last_fetch_ts: dict[str, float] = {}
        self._ref_prices: dict[str, float] = {"BTC": 69000.0, "ETH": 3500.0, "SOL": 140.0}
        self._clob_client = None
        self._log_throttle: dict[str, float] = {}
        self._orderbook_cache: dict[str, tuple[float, dict]] = {}  # token_id -> (ts, data)
        # Last place_order actual cost/shares (populated each place_order call).
        # Polymarket enforces a 5-share minimum, so when size/price < 5 the real USDC
        # cost diverges from the requested bet size. Caller must read this immediately
        # after a successful place_order to log the truth into the DB.
        self._last_order_cost: float = 0.0
        self._last_order_shares: float = 0.0

    def _build_market_specs(self, config: Config) -> list[MarketSpec]:
        mapping = {
            "btc_15m": MarketSpec("btc_15m", "BTC", "btc", "15m", 900, 6),
            "btc_5m": MarketSpec("btc_5m", "BTC", "btc", "5m", 300, 10),
            "btc_1h": MarketSpec("btc_1h", "BTC", "btc", "1h", 3600, 4),
            "eth_15m": MarketSpec("eth_15m", "ETH", "eth", "15m", 900, 6),
            "sol_15m": MarketSpec("sol_15m", "SOL", "sol", "15m", 900, 6),
        }
        specs = [mapping[group] for group in config.active_market_groups() if group in mapping]
        return specs or [mapping["btc_15m"]]

    async def close(self):
        await self._gamma_client.aclose()
        await self._client.aclose()

    def set_reference_price(self, asset_symbol: str, price: float):
        self._ref_prices[asset_symbol.upper()] = price

    def _throttled_log(self, key: str, message: str, *, level: int = logging.INFO, interval_sec: int | None = None):
        window = int(interval_sec or self.config.repeated_skip_log_interval_sec)
        now_ts = time.time()
        next_ts = float(self._log_throttle.get(key, 0.0))
        if now_ts < next_ts:
            return
        logger.log(level, message)
        self._log_throttle[key] = now_ts + max(5, window)

    def _decode_jsonish(self, value: Any, default: Any):
        if value in (None, ""):
            return default
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return default
        return value

    async def fetch_active_markets(self, spec: MarketSpec) -> list[MarketInfo]:
        now = time.time()
        cached = self._cached_markets.get(spec.group_id, [])
        last_fetch = float(self._last_fetch_ts.get(spec.group_id, 0.0))
        cooldown = max(1, int(getattr(self.config, "market_fetch_cooldown_sec", 5)))
        if cached and now - last_fetch < cooldown:
            return cached

        markets: list[MarketInfo] = []
        current_window_start = int(now // spec.duration_sec) * spec.duration_sec
        timestamps_to_check = [current_window_start + (i * spec.duration_sec) for i in range(-1, spec.windows_to_scan)]

        for ts in timestamps_to_check:
            slug = f"{spec.slug_asset}-updown-{spec.slug_window}-{ts}"
            for attempt in range(3):
                try:
                    response = await self._gamma_client.get("/events", params={"slug": slug})
                    response.raise_for_status()
                    data = response.json()
                    if not data:
                        break
                    events = data if isinstance(data, list) else [data]
                    for event in events:
                        for raw_market in event.get("markets", []):
                            if raw_market.get("closed"):
                                continue
                            market_info = self._parse_updown_market(raw_market, now, spec)
                            if market_info and market_info.active:
                                markets.append(market_info)
                    break  # success
                except (httpx.TimeoutException, httpx.ConnectError, OSError) as exc:
                    if attempt < 2:
                        await _asyncio.sleep(1.0 * (attempt + 1))
                        continue
                    logger.warning(f"Slug {slug} fetch failed after 3 attempts: {exc}")
                except Exception as exc:
                    logger.warning(f"Slug {slug} fetch error: {exc}")
                    break

        self._last_fetch_ts[spec.group_id] = now
        if markets:
            self._cached_markets[spec.group_id] = markets
            self._throttled_log(
                f"markets:{spec.group_id}:fetched",
                f"Fetched {len(markets)} active markets for {spec.group_id} from {len(timestamps_to_check)} windows",
                interval_sec=60,
            )
            return markets

        if self.mode == "paper" and not cached:
            self._throttled_log(
                f"markets:{spec.group_id}:simulated",
                f"No real markets found for {spec.group_id}, using simulated fallback",
                interval_sec=300,
            )
            simulated = self._generate_simulated_markets(spec, now)
            self._cached_markets[spec.group_id] = simulated
            return simulated

        if cached:
            self._throttled_log(
                f"markets:{spec.group_id}:stale",
                f"No fresh markets for {spec.group_id}; reusing {len(cached)} cached entries",
                interval_sec=300,
            )
        return cached

    def _parse_updown_market(self, raw: dict, now: float, spec: MarketSpec) -> Optional[MarketInfo]:
        try:
            clob_ids = self._decode_jsonish(raw.get("clobTokenIds", []), [])
            outcomes = self._decode_jsonish(raw.get("outcomes", []), [])
            prices = self._decode_jsonish(raw.get("outcomePrices", []), [0.5, 0.5])
            if len(clob_ids) < 2:
                return None
            if not prices or len(prices) < 2:
                prices = [0.5, 0.5]

            up_idx, down_idx = 0, 1
            for i, outcome in enumerate(outcomes or []):
                label = str(outcome).lower()
                if label == "up":
                    up_idx = i
                elif label == "down":
                    down_idx = i

            up_price = float(prices[up_idx])
            down_price = float(prices[down_idx])
            end_date = raw.get("endDate", "")
            expiry_ts = datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp() if end_date else now + spec.duration_sec
            minutes_to_expiry = (expiry_ts - now) / 60.0
            liquidity = float(raw.get("liquidity", 0) or 0)
            market_id = str(raw.get("conditionId") or raw.get("id") or "")
            if not market_id:
                return None
            return MarketInfo(
                market_id=market_id,
                condition_id=str(raw.get("conditionId") or market_id),
                question=str(raw.get("question") or f"{spec.asset_symbol} up/down"),
                up_token_id=str(clob_ids[up_idx]),
                down_token_id=str(clob_ids[down_idx]),
                up_price=up_price,
                down_price=down_price,
                implied_prob_up=up_price,
                expiry_timestamp=expiry_ts,
                strike_price=None,
                liquidity=liquidity,
                minutes_to_expiry=minutes_to_expiry,
                active=minutes_to_expiry > 0,
                market_type="updown",
                asset_symbol=spec.asset_symbol,
                market_group=spec.group_id,
                duration_minutes=spec.duration_minutes,
            )
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            logger.debug(f"Failed to parse market for {spec.group_id}: {exc}")
            return None

    def _generate_simulated_markets(self, spec: MarketSpec, now: float) -> list[MarketInfo]:
        base_price = self._ref_prices.get(spec.asset_symbol, 50000.0)
        markets: list[MarketInfo] = []
        for i in range(4):
            exp = now + (spec.duration_sec / 2) + (i * max(spec.duration_sec / 3, 300))
            noise = random.gauss(0, 0.12)
            up_price = max(0.05, min(0.95, 0.5 + noise))
            mid = f"sim_{spec.group_id}_{int(exp)}"
            markets.append(
                MarketInfo(
                    market_id=mid,
                    condition_id=mid,
                    question=f"{spec.asset_symbol} Up/Down sim ({int((exp - now) / 60)}min)",
                    up_token_id=f"up_{mid}",
                    down_token_id=f"down_{mid}",
                    up_price=round(up_price, 4),
                    down_price=round(1 - up_price, 4),
                    implied_prob_up=round(up_price, 4),
                    expiry_timestamp=exp,
                    strike_price=base_price,
                    liquidity=random.uniform(10000, 50000),
                    minutes_to_expiry=(exp - now) / 60,
                    active=True,
                    market_type="updown",
                    asset_symbol=spec.asset_symbol,
                    market_group=spec.group_id,
                    duration_minutes=spec.duration_minutes,
                )
            )
        return markets

    async def get_orderbook(self, token_id: str) -> dict:
        for attempt in range(3):
            try:
                response = await self._client.get("/book", params={"token_id": token_id})
                response.raise_for_status()
                return response.json()
            except (httpx.TimeoutException, httpx.ConnectError, OSError):
                if attempt < 2:
                    await _asyncio.sleep(0.5 * (attempt + 1))
                    continue
                logger.warning(f"Orderbook fetch failed for {token_id[:20]} after 3 attempts")
            except httpx.HTTPError as exc:
                logger.warning(f"Orderbook fetch error for {token_id[:20]}: {exc}")
                break
        return {"bids": [], "asks": []}

    async def get_orderbook_sentiment(self, token_id: str) -> dict:
        """Fetch orderbook and compute bid/ask imbalance as sentiment signal."""
        now = time.time()
        cached = self._orderbook_cache.get(token_id)
        if cached and now - cached[0] < 10:
            return cached[1]
        book = await self.get_orderbook(token_id)
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        bid_depth = sum(float(b.get("size") or b.get("s") or 0) for b in bids[:10])
        ask_depth = sum(float(a.get("size") or a.get("s") or 0) for a in asks[:10])
        total = bid_depth + ask_depth
        if total < self.config.orderbook_min_depth:
            result = {"sentiment": 0.0, "bid_depth": bid_depth, "ask_depth": ask_depth, "spread": 0.0, "valid": False}
        else:
            imbalance = (bid_depth - ask_depth) / total  # -1 to +1
            best_bid = float(bids[0].get("price") or bids[0].get("p") or 0) if bids else 0
            best_ask = float(asks[0].get("price") or asks[0].get("p") or 0) if asks else 0
            spread = best_ask - best_bid if best_ask > best_bid else 0
            result = {"sentiment": imbalance, "bid_depth": bid_depth, "ask_depth": ask_depth, "spread": spread, "valid": True}
        self._orderbook_cache[token_id] = (now, result)
        return result

    def _get_clob_client(self):
        if self._clob_client is None:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            creds = ApiCreds(
                api_key=self.config.clob_api_key,
                api_secret=self.config.clob_secret,
                api_passphrase=self.config.clob_passphrase,
            )
            self._clob_client = ClobClient(
                host=self.config.clob_api_url,
                chain_id=self.config.chain_id,
                key=self.config.private_key,
                creds=creds,
                signature_type=2,
                funder=self.config.proxy_wallet,
            )
        return self._clob_client

    async def check_balance(self) -> float:
        try:
            import asyncio

            client = self._get_clob_client()
            loop = asyncio.get_event_loop()

            def _check():
                from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=-1)
                bal = client.get_balance_allowance(params)
                balance_usdc = int(bal.get("balance", "0")) / 1_000_000
                logger.info(f"[LIVE] USDC balance: ${balance_usdc:.2f} | EOA={client.signer.address()} | Proxy={self.config.proxy_wallet}")
                return balance_usdc

            return await loop.run_in_executor(None, _check)
        except Exception as exc:
            logger.error(f"[LIVE] Balance check error: {exc}", exc_info=True)
            return 0.0

    async def place_order(self, token_id: str, side: str, size: float, price: float, mode: str = "paper") -> Optional[str]:
        # Reset last-order cost so callers can detect stale reads
        self._last_order_cost = 0.0
        self._last_order_shares = 0.0
        if mode == "paper":
            # Paper: actual cost == requested size (no 5-share floor applied, pnl math stays consistent)
            self._last_order_cost = float(size)
            self._last_order_shares = float(size) / float(price) if price > 0 else 0.0
            return f"paper_{int(time.time() * 1000)}"
        if price < 0.001 or price > 0.999:
            logger.warning(f"[LIVE] Invalid price {price}, must be 0.001-0.999, skipping")
            return None
        shares = round(max(5.0, size / price), 2)
        actual_cost = shares * price
        if actual_cost < 1.0:
            logger.info(f"[LIVE] Order cost ${actual_cost:.2f} below $1 minimum, skipping")
            return None
        try:
            import asyncio
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.order_builder.constants import BUY

            client = self._get_clob_client()
            order_args = OrderArgs(token_id=token_id, price=price, size=shares, side=BUY)
            logger.info(f"[LIVE] Placing: {side} {shares:.1f} shares @ {price:.4f} = ${actual_cost:.2f}")
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: client.create_and_post_order(order_args)),
                timeout=30.0,
            )
            order_id = None
            if isinstance(result, dict):
                order_id = result.get("orderID") or result.get("id")
                if not result.get("success", False):
                    logger.error(f"[LIVE] Order rejected: {result.get('errorMsg', 'unknown error')}")
                    return None
            elif hasattr(result, "order_id"):
                order_id = result.order_id
            if order_id:
                # Expose actual cost/shares to caller so DB size reflects what was really spent,
                # not the bet-size intent which diverges when the 5-share floor kicks in.
                self._last_order_cost = float(actual_cost)
                self._last_order_shares = float(shares)
                logger.info(f"[LIVE] Order placed: {side} {shares:.1f}sh @ {price:.4f} = ${actual_cost:.2f} -> {str(order_id)[:20]}...")
            else:
                logger.warning(f"[LIVE] Order response without id: {result}")
            return order_id
        except asyncio.TimeoutError:
            logger.error("[LIVE] Order timed out after 30s")
            return None
        except Exception as exc:
            logger.error(f"[LIVE] Order error: {exc}", exc_info=True)
            return None

    async def get_executable_price(self, token_id: str) -> float:
        """Get actual executable buy price from /price endpoint (not /book which shows 0.01/0.99)."""
        try:
            response = await self._client.get("/price", params={"token_id": token_id, "side": "buy"})
            response.raise_for_status()
            data = response.json()
            price = float(data.get("price", 0) or 0)
            return price if price > 0 else 0.0
        except Exception as exc:
            logger.debug(f"Executable price fetch failed for {token_id[:20]}: {exc}")
            return 0.0

    async def verify_order_fill(self, order_id: str, max_wait_sec: float = 5.0) -> dict:
        """Check if an order was filled. Returns {'filled': bool, 'size_filled': float, 'avg_price': float, 'status': str}."""
        try:
            import asyncio
            client = self._get_clob_client()
            loop = asyncio.get_event_loop()

            # Brief wait for matching engine
            await asyncio.sleep(min(2.0, max_wait_sec / 2))

            order = await loop.run_in_executor(None, lambda: client.get_order(order_id))
            if not order:
                return {"filled": False, "size_filled": 0.0, "avg_price": 0.0, "status": "unknown"}

            status = str(order.get("status", "")).upper()
            size_matched = float(order.get("size_matched", 0) or order.get("filled", 0) or 0)
            original_size = float(order.get("original_size", 0) or order.get("size", 0) or 0)
            price = float(order.get("price", 0) or 0)

            if status == "MATCHED" or (size_matched > 0 and size_matched >= original_size * 0.9):
                return {"filled": True, "size_filled": size_matched, "avg_price": price, "status": status}

            # Wait remaining time and recheck
            if max_wait_sec > 2.5:
                await asyncio.sleep(max_wait_sec - 2.0)
                order = await loop.run_in_executor(None, lambda: client.get_order(order_id))
                if order:
                    status = str(order.get("status", "")).upper()
                    size_matched = float(order.get("size_matched", 0) or order.get("filled", 0) or 0)
                    if status == "MATCHED" or (size_matched > 0 and size_matched >= original_size * 0.9):
                        return {"filled": True, "size_filled": size_matched, "avg_price": price, "status": status}

            # Not filled — cancel the order
            if status in ("LIVE", "OPEN", ""):
                try:
                    await loop.run_in_executor(None, lambda: client.cancel(order_id))
                    logger.info(f"[LIVE] Cancelled unfilled order {str(order_id)[:20]}... status={status}")
                except Exception as cancel_err:
                    logger.warning(f"[LIVE] Cancel failed for {str(order_id)[:20]}...: {cancel_err}")

            return {"filled": False, "size_filled": size_matched, "avg_price": price, "status": status}
        except Exception as exc:
            logger.warning(f"[LIVE] Order verify error: {exc}")
            # Assume filled on error to avoid losing track
            return {"filled": True, "size_filled": 0.0, "avg_price": 0.0, "status": "verify_error"}

    async def get_best_bid(self, token_id: str) -> float:
        """Get best bid price from orderbook."""
        book = await self.get_orderbook(token_id)
        bids = book.get("bids") or []
        if bids:
            return float(bids[0].get("price") or bids[0].get("p") or 0)
        return 0.0

    async def sell_order(self, token_id: str, shares: float, price: float, mode: str = "paper") -> Optional[str]:
        """Place a SELL order to exit a position."""
        if mode == "paper":
            return f"paper_sell_{int(time.time() * 1000)}"
        if price < 0.001 or price > 0.999:
            logger.warning(f"[LIVE] Invalid sell price {price}, skipping")
            return None
        if shares < 1.0:
            logger.info(f"[LIVE] Sell shares {shares:.1f} too small, skipping")
            return None
        try:
            import asyncio
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.order_builder.constants import SELL

            client = self._get_clob_client()
            order_args = OrderArgs(token_id=token_id, price=price, size=round(shares, 2), side=SELL)
            logger.info(f"[LIVE] Selling: {shares:.1f} shares @ {price:.4f}")
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: client.create_and_post_order(order_args)),
                timeout=30.0,
            )
            order_id = None
            if isinstance(result, dict):
                order_id = result.get("orderID") or result.get("id")
                if not result.get("success", False):
                    logger.error(f"[LIVE] Sell rejected: {result.get('errorMsg', 'unknown error')}")
                    return None
            elif hasattr(result, "order_id"):
                order_id = result.order_id
            if order_id:
                logger.info(f"[LIVE] Sell placed: {shares:.1f}sh @ {price:.4f} -> {str(order_id)[:20]}...")
            else:
                logger.warning(f"[LIVE] Sell response without id: {result}")
            return order_id
        except asyncio.TimeoutError:
            logger.error("[LIVE] Sell order timed out after 30s")
            return None
        except Exception as exc:
            logger.error(f"[LIVE] Sell order error: {exc}", exc_info=True)
            return None

    def _append_cheap_opportunity(self, opportunities: list[dict], market: MarketInfo, side: str, token_id: str, model_prob: float, market_prob: float):
        edge = model_prob - market_prob
        opportunities.append(
            {
                "market": market,
                "side": side,
                "token_id": token_id,
                "model_prob": model_prob,
                "market_prob": market_prob,
                "edge": edge,
                "entry_price": market_prob,
                "strategy": "ultra_cheap" if market_prob < 0.15 else "cheap",
            }
        )

    def find_edge_opportunities(self, markets: list[MarketInfo], model_prob_up: float, current_price: float, config: Config, *, strike_prob_up: float | None = None) -> list[dict]:
        opportunities: list[dict] = []

        # Blend model_prob with strike-based probability if available
        effective_prob_up = model_prob_up
        if strike_prob_up is not None and config.strike_price_enabled:
            w = config.strike_prob_weight
            effective_prob_up = model_prob_up * (1 - w) + strike_prob_up * w

        for market in markets:
            # --- Feature 6: Both-sides hedge arbitrage ---
            if config.hedge_enabled:
                combined = market.up_price + market.down_price
                if combined < config.hedge_max_combined_price and combined > 0:
                    profit_per_share = 1.0 - combined
                    profit_pct = profit_per_share / combined
                    if profit_pct >= config.hedge_min_profit_pct:
                        # Buy the cheaper side (more profit on that side)
                        cheaper_side = "UP" if market.up_price <= market.down_price else "DOWN"
                        cheaper_price = min(market.up_price, market.down_price)
                        token_id = market.up_token_id if cheaper_side == "UP" else market.down_token_id
                        opportunities.append({
                            "market": market,
                            "side": cheaper_side,
                            "token_id": token_id,
                            "model_prob": 1.0 / combined,  # guaranteed payout
                            "market_prob": cheaper_price,
                            "edge": profit_pct,
                            "entry_price": cheaper_price,
                            "strategy": "hedge_arb",
                            "hedge_combined": combined,
                        })
                        continue

            # --- Feature 3: Expiry snipe (near-expiry with clear direction) ---
            if config.expiry_snipe_enabled and market.strike_price and market.strike_price > 0:
                if 0.5 < market.minutes_to_expiry <= config.expiry_snipe_max_minutes:
                    dist_pct = (current_price - market.strike_price) / market.strike_price
                    fee_rate = getattr(config, "taker_fee_rate", 0.10)
                    if abs(dist_pct) >= config.expiry_snipe_min_strike_dist_pct:
                        if dist_pct > 0 and market.up_price < config.expiry_snipe_max_entry_price:
                            snipe_prob = min(0.95, 0.70 + abs(dist_pct) * 100)
                            gross_edge = snipe_prob - market.up_price
                            net_edge = gross_edge * (1.0 - fee_rate)
                            if net_edge > 0.04:
                                opportunities.append({
                                    "market": market, "side": "UP", "token_id": market.up_token_id,
                                    "model_prob": snipe_prob, "market_prob": market.up_price,
                                    "edge": net_edge, "entry_price": market.up_price, "strategy": "expiry_snipe",
                                })
                        elif dist_pct < 0 and market.down_price < config.expiry_snipe_max_entry_price:
                            snipe_prob = min(0.95, 0.70 + abs(dist_pct) * 100)
                            gross_edge = snipe_prob - market.down_price
                            net_edge = gross_edge * (1.0 - fee_rate)
                            if net_edge > 0.04:
                                opportunities.append({
                                    "market": market, "side": "DOWN", "token_id": market.down_token_id,
                                    "model_prob": snipe_prob, "market_prob": market.down_price,
                                    "edge": net_edge, "entry_price": market.down_price, "strategy": "expiry_snipe",
                                })
                    continue  # near-expiry markets handled, skip normal strategies

            if market.minutes_to_expiry < config.min_time_to_expiry:
                continue
            if market.liquidity < config.min_market_liquidity:
                continue

            # --- Cheap opportunity detection (using blended probability) ---
            fee_rate = getattr(config, "taker_fee_rate", 0.10)
            if market.up_price < config.cheap_up_price_cap and effective_prob_up >= config.cheap_up_min_model_prob:
                edge_up = (effective_prob_up - market.implied_prob_up) * (1.0 - fee_rate)
                if edge_up > config.min_edge_threshold:
                    self._append_cheap_opportunity(opportunities, market, "UP", market.up_token_id, effective_prob_up, market.implied_prob_up)

            mp_down = 1 - effective_prob_up
            if market.down_price < config.cheap_down_price_cap and mp_down >= (1 - config.cheap_down_max_model_prob):
                edge_down = (mp_down - market.down_price) * (1.0 - fee_rate)
                if edge_down > config.min_edge_threshold:
                    self._append_cheap_opportunity(opportunities, market, "DOWN", market.down_token_id, mp_down, market.down_price)

        if not opportunities:
            best_edge = 0.0
            best_opp = None
            fee_rate = getattr(config, "taker_fee_rate", 0.10)
            for market in markets:
                if market.minutes_to_expiry < config.min_time_to_expiry or market.minutes_to_expiry > max(45, market.duration_minutes * 2):
                    continue
                if market.liquidity < config.min_market_liquidity:
                    continue
                edge_up = (effective_prob_up - market.implied_prob_up) * (1.0 - fee_rate)
                edge_down = ((1 - effective_prob_up) - market.down_price) * (1.0 - fee_rate)
                if market.up_price < market.down_price and edge_up > best_edge:
                    best_edge = edge_up
                    best_opp = {
                        "market": market, "side": "UP", "token_id": market.up_token_id,
                        "model_prob": effective_prob_up, "market_prob": market.implied_prob_up,
                        "edge": edge_up, "entry_price": market.up_price, "strategy": "deviation",
                    }
                elif market.down_price < market.up_price and edge_down > best_edge:
                    best_edge = edge_down
                    best_opp = {
                        "market": market, "side": "DOWN", "token_id": market.down_token_id,
                        "model_prob": 1 - effective_prob_up, "market_prob": market.down_price,
                        "edge": edge_down, "entry_price": market.down_price, "strategy": "deviation",
                    }
            if best_opp and best_edge > config.min_edge_threshold:
                # Filter: entry_price 0.48+ is -EV (live WR 48%, fee-negative)
                # Filter: entry_price <0.15 is adverse selection (extreme prices)
                ep = best_opp["entry_price"]
                if 0.15 <= ep <= 0.47:
                    opportunities.append(best_opp)
        opportunities.sort(key=lambda item: (item["entry_price"], -abs(item["edge"])))
        return opportunities
