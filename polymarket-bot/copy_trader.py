"""Copy-trading signal source driven by Polymarket leaderboard wallets."""

import logging
import time
from dataclasses import dataclass

import httpx

from config import Config
from market_scanner import MarketInfo

logger = logging.getLogger("polybot")


@dataclass(frozen=True)
class LeaderboardWallet:
    wallet: str
    user_name: str
    rank: int
    pnl: float
    volume: float


class CopyTradeTracker:
    def __init__(self, config: Config):
        self.config = config
        self._client = httpx.AsyncClient(base_url="https://data-api.polymarket.com", timeout=15.0)
        self._wallet_cache: list[LeaderboardWallet] = []
        self._wallet_cache_ts = 0.0
        self._trade_cache: dict[str, tuple[float, list[dict]]] = {}
        self._seen_trade_keys: dict[str, float] = {}
        self._last_label = "copy:off"

    async def close(self):
        await self._client.aclose()

    async def build_opportunities(self, markets_by_id: dict[str, MarketInfo]) -> tuple[list[dict], str]:
        if not self.config.copy_trading_enabled:
            return [], "copy:off"

        self._prune_seen_trade_keys()
        wallets = await self._fetch_wallets()
        if not wallets:
            self._last_label = "copy:0w/0s"
            return [], self._last_label

        market_lookup = self._build_market_lookup(markets_by_id)
        aggregated: dict[tuple[str, str], dict] = {}
        for wallet in wallets:
            for trade in await self._fetch_wallet_trades(wallet.wallet):
                market = self._resolve_market(trade, market_lookup)
                if market is None:
                    continue
                side = self._map_trade_to_bot_side(trade)
                if side is None:
                    continue
                trade_key = self._trade_key(trade, side)
                if not trade_key or trade_key in self._seen_trade_keys:
                    continue
                trade_cash = float(trade.get("price") or 0.0) * float(trade.get("size") or 0.0)
                if trade_cash < self.config.copy_min_trade_cash:
                    continue
                key = (market.market_id, side)
                bucket = aggregated.setdefault(
                    key,
                    {
                        "market": market,
                        "side": side,
                        "wallets": set(),
                        "wallet_names": [],
                        "notional": 0.0,
                        "latest_ts": 0.0,
                        "trade_keys": [],
                    },
                )
                if wallet.wallet not in bucket["wallets"]:
                    bucket["wallets"].add(wallet.wallet)
                    bucket["wallet_names"].append(wallet.user_name or wallet.wallet[:8])
                bucket["notional"] += trade_cash
                bucket["latest_ts"] = max(bucket["latest_ts"], float(trade.get("timestamp") or 0.0))
                bucket["trade_keys"].append(trade_key)

        opportunities: list[dict] = []
        for bucket in aggregated.values():
            distinct_wallets = len(bucket["wallets"])
            if distinct_wallets < self.config.copy_min_distinct_wallets:
                continue
            market = bucket["market"]
            side = bucket["side"]
            market_prob = market.up_price if side == "UP" else market.down_price
            token_id = market.up_token_id if side == "UP" else market.down_token_id
            synthetic_edge = self._synthetic_edge(distinct_wallets, float(bucket["notional"]))
            opportunities.append(
                {
                    "market": market,
                    "side": side,
                    "token_id": token_id,
                    "model_prob": min(0.99, market_prob + synthetic_edge),
                    "market_prob": market_prob,
                    "edge": synthetic_edge,
                    "entry_price": market_prob,
                    "strategy": "copy_wallet",
                    "copy_wallet_count": distinct_wallets,
                    "copy_wallets": list(bucket["wallet_names"])[:5],
                    "copy_notional": round(float(bucket["notional"]), 2),
                    "copy_latest_ts": bucket["latest_ts"],
                    "copy_note": f"{distinct_wallets} wallets / ${float(bucket['notional']):,.0f}",
                    "_trade_keys": list(bucket["trade_keys"]),
                }
            )

        opportunities.sort(
            key=lambda item: (
                -int(item.get("copy_wallet_count") or 0),
                -float(item.get("copy_notional") or 0.0),
                item["entry_price"],
            )
        )
        limited = opportunities[: max(0, int(self.config.copy_top_markets_per_scan))]
        for opp in limited:
            for trade_key in opp.pop("_trade_keys", []):
                self._seen_trade_keys[trade_key] = time.time()
        if limited:
            logger.info(
                f"[COPY] Built {len(limited)} copy opportunities: "
                + ", ".join(
                    f"{opp['market'].market_group}/{opp['side']} {opp['copy_note']}"
                    for opp in limited[:3]
                )
            )
        self._last_label = f"copy:{len(wallets)}w/{len(limited)}s"
        return limited, self._last_label

    async def _fetch_wallets(self) -> list[LeaderboardWallet]:
        now_ts = time.time()
        if self._wallet_cache and now_ts - self._wallet_cache_ts < max(30, self.config.copy_wallet_refresh_sec):
            return self._wallet_cache

        try:
            response = await self._client.get(
                "/v1/leaderboard",
                params={
                    "category": self.config.copy_leaderboard_category,
                    "timePeriod": self.config.copy_leaderboard_time_period,
                    "orderBy": "PNL",
                    "limit": min(50, max(1, self.config.copy_leaderboard_limit)),
                    "offset": 0,
                },
            )
            response.raise_for_status()
            wallets: list[LeaderboardWallet] = []
            for row in self._rows(response.json()):
                pnl = float(row.get("pnl") or 0.0)
                volume = float(row.get("vol") or 0.0)
                if pnl < self.config.copy_min_wallet_pnl or volume < self.config.copy_min_wallet_volume:
                    continue
                wallets.append(
                    LeaderboardWallet(
                        wallet=str(row.get("proxyWallet") or ""),
                        user_name=str(row.get("userName") or ""),
                        rank=int(row.get("rank") or 0),
                        pnl=pnl,
                        volume=volume,
                    )
                )
            self._wallet_cache = [wallet for wallet in wallets if wallet.wallet][: self.config.copy_leaderboard_limit]
            self._wallet_cache_ts = now_ts
            logger.info(
                f"[COPY] Loaded {len(self._wallet_cache)} leaderboard wallets "
                f"({self.config.copy_leaderboard_category}/{self.config.copy_leaderboard_time_period})"
            )
        except Exception as exc:
            logger.error(f"[COPY] Leaderboard fetch failed: {exc}", exc_info=True)
        return self._wallet_cache

    async def _fetch_wallet_trades(self, wallet: str) -> list[dict]:
        now_ts = time.time()
        cached = self._trade_cache.get(wallet)
        if cached and now_ts - cached[0] < max(10, self.config.copy_trade_refresh_sec):
            return cached[1]

        trades: list[dict] = []
        try:
            response = await self._client.get(
                "/trades",
                params={
                    "user": wallet,
                    "limit": max(1, self.config.copy_wallet_trade_limit),
                    "offset": 0,
                    "takerOnly": "false",
                },
            )
            response.raise_for_status()
            cutoff_ms = int((time.time() - max(60, self.config.copy_trade_lookback_sec)) * 1000)
            for row in self._rows(response.json()):
                timestamp = int(row.get("timestamp") or 0)
                if timestamp < cutoff_ms:
                    continue
                if str(row.get("side") or "").upper() != "BUY":
                    continue
                trades.append(row)
        except Exception as exc:
            logger.error(f"[COPY] Trade fetch failed for {wallet[:10]}...: {exc}", exc_info=True)

        self._trade_cache[wallet] = (now_ts, trades)
        return trades

    def _map_trade_to_bot_side(self, trade: dict) -> str | None:
        outcome = str(trade.get("outcome") or trade.get("sideLabel") or "").strip().lower()
        if outcome in {"up", "yes"}:
            return "UP"
        if outcome in {"down", "no"}:
            return "DOWN"
        return None

    def _synthetic_edge(self, distinct_wallets: int, notional: float) -> float:
        base = float(self.config.copy_signal_base_edge)
        wallet_bonus = max(0, distinct_wallets - 1) * float(self.config.copy_signal_wallet_bonus)
        cash_scale = max(1.0, float(self.config.copy_signal_cash_scale))
        cash_bonus_cap = max(0.0, float(self.config.copy_signal_cash_bonus_cap))
        cash_bonus = min(cash_bonus_cap, (max(0.0, notional) / cash_scale) * cash_bonus_cap)
        return min(float(self.config.copy_signal_max_edge), base + wallet_bonus + cash_bonus)

    def _trade_key(self, trade: dict, side: str) -> str:
        tx_hash = str(trade.get("transactionHash") or "").strip()
        condition_id = str(trade.get("conditionId") or "").strip()
        wallet = str(trade.get("proxyWallet") or "").strip()
        outcome = str(trade.get("outcome") or "").strip().lower()
        timestamp = int(trade.get("timestamp") or 0)
        if tx_hash:
            return f"{tx_hash}:{condition_id}:{side}"
        if not condition_id or not wallet or timestamp <= 0 or not outcome:
            return ""
        return f"{wallet}:{condition_id}:{outcome}:{timestamp}:{side}"

    def _prune_seen_trade_keys(self):
        cutoff = time.time() - max(600, self.config.copy_trade_lookback_sec * 2)
        stale_keys = [key for key, seen_ts in self._seen_trade_keys.items() if seen_ts < cutoff]
        for key in stale_keys:
            self._seen_trade_keys.pop(key, None)

    def _rows(self, payload) -> list[dict]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("data", "rows", "items"):
                rows = payload.get(key)
                if isinstance(rows, list):
                    return rows
        return []

    def _build_market_lookup(self, markets_by_id: dict[str, MarketInfo]) -> dict[str, MarketInfo]:
        lookup: dict[str, MarketInfo] = {}
        for market_id, market in markets_by_id.items():
            for key in {
                str(market_id),
                str(market.condition_id),
                str(market.up_token_id),
                str(market.down_token_id),
            }:
                if key:
                    lookup[key] = market
        return lookup

    def _resolve_market(self, trade: dict, market_lookup: dict[str, MarketInfo]) -> MarketInfo | None:
        candidate_keys = [
            str(trade.get("conditionId") or ""),
            str(trade.get("market") or ""),
            str(trade.get("marketId") or ""),
            str(trade.get("asset") or ""),
            str(trade.get("tokenID") or ""),
            str(trade.get("tokenId") or ""),
            str(trade.get("outcomeIndex") or ""),
        ]
        for key in candidate_keys:
            if key and key in market_lookup:
                return market_lookup[key]
        return None
