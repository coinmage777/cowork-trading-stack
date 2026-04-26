"""
Funding Rate Arbitrage — 거래소 간 펀딩레이트 차이를 이용한 시장중립 수익 전략.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from typing import Any, Dict, List

import aiohttp

from .venue_execution import (
    COLLECTOR_EXCHANGE_CANDIDATES,
    FUNDING_INTERVAL_HOURS,
    classify_execution_error,
    maybe_await,
    normalize_to_8h,
    observed_min_qty,
    resolve_data_path,
    round_order_size,
    wrapper_symbol_name,
)

logger = logging.getLogger(__name__)

try:
    from .shared_state import POSITION_REGISTRY, VENUE_CAP, REGIME_GATE, EVENT_BUS
    _SYNERGY = True
except Exception:
    _SYNERGY = False


@dataclass
class FundingArbConfig:
    enabled: bool = False
    coins: list = field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    leverage: int = 5
    margin_per_leg: float = 50.0
    min_rate_diff: float = 0.0003
    close_rate_diff: float = 0.0001
    assumed_roundtrip_fee_pct: float = 0.0016
    profit_buffer_pct: float = 0.0004
    scan_interval: int = 300
    max_positions: int = 2
    max_candidate_pairs: int = 5
    hl_api_url: str = "https://api.hyperliquid.xyz/info"
    entry_before_funding_minutes: int = 30
    max_hold_hours: float = 24.0
    db_path: str = "funding_rates.db"
    use_collector_fallback: bool = True
    collector_max_age_seconds: int = 1800
    live_fetch_timeout_seconds: float = 6.0
    max_retry_size_multiplier: float = 2.5


@dataclass
class FundingPosition:
    coin: str
    long_exchange: str
    short_exchange: str
    entry_rate_diff: float
    entry_time: float
    margin: float
    collected_funding: float = 0.0
    entry_count: int = 0


HL_BASED_EXCHANGES = {
    "hyperliquid",
    "hyperliquid_2",
    "miracle",
    "dreamcash",
    "hl_wallet_b",
    "hl_wallet_c",
}

EXCHANGE_GROUPS = {
    "hyperliquid": "hl",
    "hyperliquid_2": "hl",
    "miracle": "hl",
    "dreamcash": "hl",
    "hl_wallet_b": "hl",
    "hl_wallet_c": "hl",
    "ethereal": "ethereal",
    "ethereal_2": "ethereal",
    "nado": "nado",
    "nado_2": "nado",
    "standx": "standx",
    "standx_2": "standx",
    "hyena": "hyena",
    "hyena_2": "hyena",
    "hotstuff": "hotstuff",
    "hotstuff_2": "hotstuff",
    "variational_2": "variational",
    "katana_2": "katana",
    "treadfi.pacifica": "treadfi.pacifica",
}


def _parse_interval_hours(value: Any) -> float | None:
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        value_f = float(value)
        return value_f if value_f > 0 else None
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(value))
    if not match:
        return None
    try:
        parsed = float(match.group(1))
    except ValueError:
        return None
    return parsed if parsed > 0 else None


class FundingArbTrader:
    def __init__(
        self,
        config: FundingArbConfig,
        exchange_wrappers: Dict[str, object],
        exchange_names: List[str],
    ):
        self.config = config
        self.wrappers = exchange_wrappers
        self.exchange_names = exchange_names
        self.positions: List[FundingPosition] = []
        self.running = False
        self._shutdown_close_positions = True
        self._funding_cache: Dict[str, Dict[str, float]] = {}
        self._funding_meta: Dict[str, Dict[str, dict[str, Any]]] = {}
        self._last_fetch_time: float = 0
        self._observed_min_qty: dict[tuple[str, str], float] = {}
        self._collector_db_path = resolve_data_path(self.config.db_path)
        self._hl_exchanges = [e for e in exchange_names if self._exchange_group(e) == "hl"]
        self._non_hl_exchanges = [e for e in exchange_names if self._exchange_group(e) != "hl"]

        logger.info(
            f"  FUND │ 초기화 │ coins={config.coins} "
            f"│ HL계열={self._hl_exchanges} │ 독립={self._non_hl_exchanges} "
            f"│ min_diff_8h={self._effective_min_rate_diff()*100:.2f}% "
            f"│ close_diff_8h={self._effective_close_rate_diff()*100:.2f}% "
            f"│ collector_db={self._collector_db_path}"
        )

    def _effective_min_rate_diff(self) -> float:
        fee_aware_floor = self.config.assumed_roundtrip_fee_pct + self.config.profit_buffer_pct
        return max(self.config.min_rate_diff, fee_aware_floor)

    def _effective_close_rate_diff(self) -> float:
        fee_aware_floor = self.config.assumed_roundtrip_fee_pct * 0.5
        return max(self.config.close_rate_diff, fee_aware_floor)

    def _exchange_group(self, exchange_name: str) -> str:
        return EXCHANGE_GROUPS.get(exchange_name, exchange_name)

    def _exchange_interval_hours(self, exchange_name: str, wrapper: object | None = None) -> float | None:
        raw = getattr(wrapper, "_wrapper", wrapper)
        for attr in ("funding_interval_hours", "funding_interval", "funding_period_hours"):
            value = getattr(raw, attr, None)
            parsed = _parse_interval_hours(value)
            if parsed:
                return parsed
        group_name = self._exchange_group(exchange_name)
        return FUNDING_INTERVAL_HOURS.get(exchange_name) or FUNDING_INTERVAL_HOURS.get(group_name)

    def _collector_candidates(self, exchange_name: str) -> list[str]:
        candidates = COLLECTOR_EXCHANGE_CANDIDATES.get(exchange_name, [exchange_name])
        group_name = self._exchange_group(exchange_name)
        if group_name not in candidates:
            candidates = [group_name, *candidates]
        if exchange_name not in candidates:
            candidates = [exchange_name, *candidates]
        deduped = []
        seen = set()
        for item in candidates:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return deduped

    def _store_funding_rate(
        self,
        exchange_name: str,
        coin: str,
        rate_8h: float,
        *,
        source: str,
        raw_rate: float | None = None,
        interval_hours: float | None = None,
        observed_at: float | None = None,
        source_exchange: str | None = None,
    ) -> None:
        coin_upper = coin.upper()
        ts = observed_at or time.time()
        self._funding_cache.setdefault(exchange_name, {})[coin_upper] = rate_8h
        self._funding_meta.setdefault(exchange_name, {})[coin_upper] = {
            "rate_8h": rate_8h,
            "raw_rate": raw_rate,
            "interval_hours": interval_hours,
            "source": source,
            "source_exchange": source_exchange or exchange_name,
            "observed_at": ts,
        }

    def _prune_stale_cache(self) -> None:
        now = time.time()
        max_age = max(self.config.collector_max_age_seconds, self.config.scan_interval * 3)
        for exchange_name, per_coin in list(self._funding_meta.items()):
            for coin, meta in list(per_coin.items()):
                observed_at = float(meta.get("observed_at") or 0.0)
                if observed_at and (now - observed_at) <= max_age:
                    continue
                self._funding_cache.get(exchange_name, {}).pop(coin, None)
                self._funding_meta.get(exchange_name, {}).pop(coin, None)
            if not self._funding_meta.get(exchange_name):
                self._funding_meta.pop(exchange_name, None)
                self._funding_cache.pop(exchange_name, None)

    def _collector_rate_sync(self, exchange_name: str, coin: str) -> dict[str, Any] | None:
        if not self.config.use_collector_fallback:
            return None
        if not self._collector_db_path.exists():
            return None

        candidates = self._collector_candidates(exchange_name)
        placeholders = ",".join("?" for _ in candidates)
        query = (
            "SELECT exchange, funding_rate, funding_rate_8h, funding_interval, timestamp, source "
            "FROM funding_rates "
            f"WHERE symbol = ? AND exchange IN ({placeholders}) "
            "ORDER BY timestamp DESC LIMIT 1"
        )
        params = [coin.upper(), *candidates]

        conn = sqlite3.connect(self._collector_db_path)
        try:
            row = conn.execute(query, params).fetchone()
        finally:
            conn.close()

        if not row:
            return None

        source_exchange, funding_rate, funding_rate_8h, funding_interval, ts_text, source = row
        interval_hours = _parse_interval_hours(funding_interval)
        observed_at = 0.0
        if ts_text:
            try:
                observed_at = datetime.fromisoformat(ts_text).timestamp()
            except ValueError:
                observed_at = 0.0
        age_seconds = time.time() - observed_at if observed_at else None
        if age_seconds is not None and age_seconds > self.config.collector_max_age_seconds:
            return None

        if funding_rate_8h is None and funding_rate is not None:
            funding_rate_8h = normalize_to_8h(float(funding_rate), interval_hours)
        if funding_rate_8h is None:
            return None

        return {
            "rate_8h": float(funding_rate_8h),
            "raw_rate": float(funding_rate) if funding_rate is not None else None,
            "interval_hours": interval_hours,
            "observed_at": observed_at or time.time(),
            "source": f"collector:{source or 'db'}",
            "source_exchange": source_exchange,
            "age_seconds": age_seconds,
        }

    async def _collector_rate(self, exchange_name: str, coin: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._collector_rate_sync, exchange_name, coin)

    def _capture_execution_error(self, exchange_name: str, coin: str, error: Any) -> dict[str, Any]:
        info = classify_execution_error(error)
        min_qty = info.get("min_qty")
        if min_qty:
            key = (exchange_name, coin.upper())
            prev = self._observed_min_qty.get(key, 0.0)
            if min_qty > prev:
                self._observed_min_qty[key] = float(min_qty)
        return info

    def _required_notional(self, exchange_name: str, coin: str, price: float, base_notional: float) -> float:
        required = base_notional
        min_qty = observed_min_qty(exchange_name, coin, self._observed_min_qty)
        if min_qty and price > 0:
            required = max(required, min_qty * price)
        return required

    def _compute_leg_size(self, exchange_name: str, coin: str, price: float, notional: float) -> float:
        size = notional / price
        min_qty = observed_min_qty(exchange_name, coin, self._observed_min_qty)
        if min_qty and size < min_qty:
            size = min_qty
        size = round_order_size(coin, size)
        if min_qty and size < min_qty:
            size = min_qty
        return float(size)

    async def run(self):
        self.running = True
        logger.info("  FUND │ 펀딩레이트 아비트라지 시작")

        while self.running:
            try:
                await self._fetch_all_funding_rates()
                await self._manage_positions()
                if len(self.positions) < self.config.max_positions:
                    await self._scan_opportunities()
                await asyncio.sleep(self.config.scan_interval)
            except asyncio.CancelledError:
                logger.info("  FUND │ 종료 시그널 수신")
                break
            except Exception as exc:
                logger.error(f"  FUND │ 에러: {exc}", exc_info=True)
                await asyncio.sleep(60)

        await self.shutdown(close_positions=self._shutdown_close_positions)

    async def shutdown(self, close_positions: bool = True):
        self.running = False
        self._shutdown_close_positions = close_positions
        if close_positions and self.positions:
            logger.info(f"  FUND │ 종료 │ {len(self.positions)}개 포지션 청산 중")
            for pos in list(self.positions):
                await self._close_position(pos, reason="shutdown")
        logger.info("  FUND │ 종료 완료")

    def stop(self):
        self.running = False

    async def _fetch_all_funding_rates(self):
        now = time.time()
        if now - self._last_fetch_time < 60:
            return

        tasks = []
        if self._hl_exchanges:
            tasks.append(self._fetch_hl_funding())
        for exchange_name in self._non_hl_exchanges:
            tasks.append(self._fetch_exchange_funding(exchange_name))

        await asyncio.gather(*tasks, return_exceptions=True)
        self._last_fetch_time = now
        self._prune_stale_cache()

        total_rates = sum(len(v) for v in self._funding_cache.values())
        logger.debug(f"  FUND │ 펀딩레이트 수집 완료 │ {len(self._funding_cache)} 거래소, {total_rates} 코인")

    async def _fetch_hl_funding(self):
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                payload = {"type": "metaAndAssetCtxs"}
                async with session.post(
                    self.config.hl_api_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"  FUND │ HL API 응답 에러: {resp.status}")
                        return

                    data = await resp.json()
                    if not isinstance(data, list) or len(data) < 2:
                        return

                    universe = data[0].get("universe", [])
                    asset_ctxs = data[1]
                    coins = {c.upper() for c in self.config.coins}
                    rates: dict[str, tuple[float, float]] = {}
                    for idx, asset in enumerate(universe):
                        name = str(asset.get("name", "")).upper()
                        if name not in coins:
                            continue
                        ctx = asset_ctxs[idx] if idx < len(asset_ctxs) else {}
                        funding = ctx.get("funding")
                        if funding is None:
                            continue
                        raw_rate = float(funding)
                        rates[name] = (raw_rate, normalize_to_8h(raw_rate, 1.0))

                    observed_at = time.time()
                    for exchange_name in self._hl_exchanges:
                        for coin, (raw_rate, rate_8h) in rates.items():
                            self._store_funding_rate(
                                exchange_name,
                                coin,
                                rate_8h,
                                source="hl_api",
                                raw_rate=raw_rate,
                                interval_hours=1.0,
                                observed_at=observed_at,
                                source_exchange="hyperliquid",
                            )

                    if rates:
                        rate_str = " | ".join(
                            f"{coin}={rate_8h*100:+.4f}%/8h" for coin, (_, rate_8h) in sorted(rates.items())
                        )
                        logger.debug(f"  FUND │ HL 펀딩(8h): {rate_str}")
        except Exception as exc:
            logger.warning(f"  FUND │ HL 펀딩레이트 조회 실패: {exc}")

    async def _fetch_live_funding_rate(self, exchange_name: str, coin: str) -> dict[str, Any] | None:
        wrapper = self.wrappers.get(exchange_name)
        if not wrapper:
            return None

        coin_upper = coin.upper()
        converted_symbol = wrapper_symbol_name(wrapper, exchange_name, coin_upper)
        raw = getattr(wrapper, "_wrapper", wrapper)
        interval_hours = self._exchange_interval_hours(exchange_name, wrapper)

        get_funding_rate = getattr(wrapper, "get_funding_rate", None)
        if callable(get_funding_rate):
            try:
                raw_rate = await asyncio.wait_for(
                    maybe_await(get_funding_rate(coin_upper)),
                    timeout=self.config.live_fetch_timeout_seconds,
                )
                if raw_rate is not None:
                    raw_value = float(raw_rate)
                    return {
                        "rate_8h": normalize_to_8h(raw_value, interval_hours),
                        "raw_rate": raw_value,
                        "interval_hours": interval_hours,
                        "observed_at": time.time(),
                        "source": "live:get_funding_rate",
                        "source_exchange": exchange_name,
                    }
            except Exception as exc:
                logger.debug(f"  FUND │ {exchange_name} {coin_upper} live get_funding_rate 실패: {exc}")

        for target_name, target in (("wrapper", wrapper), ("raw", raw)):
            get_price_data = getattr(target, "get_price_data", None)
            if not callable(get_price_data):
                continue
            try:
                payload = await asyncio.wait_for(
                    maybe_await(get_price_data(converted_symbol)),
                    timeout=self.config.live_fetch_timeout_seconds,
                )
                if isinstance(payload, dict) and payload.get("funding_rate") is not None:
                    raw_value = float(payload["funding_rate"])
                    return {
                        "rate_8h": normalize_to_8h(raw_value, interval_hours),
                        "raw_rate": raw_value,
                        "interval_hours": interval_hours,
                        "observed_at": time.time(),
                        "source": f"live:{target_name}.get_price_data",
                        "source_exchange": exchange_name,
                    }
            except Exception as exc:
                logger.debug(f"  FUND │ {exchange_name} {coin_upper} {target_name}.get_price_data 실패: {exc}")

        exchange_obj = getattr(raw, "exchange", None)
        fetch_funding_rate = getattr(exchange_obj, "fetch_funding_rate", None)
        if callable(fetch_funding_rate):
            try:
                payload = await asyncio.wait_for(
                    maybe_await(fetch_funding_rate(converted_symbol)),
                    timeout=self.config.live_fetch_timeout_seconds,
                )
                if isinstance(payload, dict) and payload.get("fundingRate") is not None:
                    raw_value = float(payload["fundingRate"])
                    return {
                        "rate_8h": normalize_to_8h(raw_value, interval_hours),
                        "raw_rate": raw_value,
                        "interval_hours": interval_hours,
                        "observed_at": time.time(),
                        "source": "live:ccxt.fetch_funding_rate",
                        "source_exchange": exchange_name,
                    }
            except Exception as exc:
                logger.debug(f"  FUND │ {exchange_name} {coin_upper} ccxt funding 실패: {exc}")

        return None

    async def _fetch_exchange_funding(self, exchange_name: str):
        rates_found = 0
        for coin in self.config.coins:
            coin_upper = coin.upper()
            live = await self._fetch_live_funding_rate(exchange_name, coin_upper)
            if live:
                self._store_funding_rate(
                    exchange_name,
                    coin_upper,
                    live["rate_8h"],
                    source=live["source"],
                    raw_rate=live.get("raw_rate"),
                    interval_hours=live.get("interval_hours"),
                    observed_at=live.get("observed_at"),
                    source_exchange=live.get("source_exchange"),
                )
                rates_found += 1
                continue

            collector = await self._collector_rate(exchange_name, coin_upper)
            if collector:
                self._store_funding_rate(
                    exchange_name,
                    coin_upper,
                    collector["rate_8h"],
                    source=collector["source"],
                    raw_rate=collector.get("raw_rate"),
                    interval_hours=collector.get("interval_hours"),
                    observed_at=collector.get("observed_at"),
                    source_exchange=collector.get("source_exchange"),
                )
                rates_found += 1
                continue

            logger.debug(f"  FUND │ {exchange_name} {coin_upper} funding unavailable")

        if rates_found:
            logger.debug(f"  FUND │ {exchange_name} funding loaded │ {rates_found} symbols")

    async def _scan_opportunities(self):
        min_rate_diff = self._effective_min_rate_diff()
        for coin in self.config.coins:
            coin_upper = coin.upper()
            if any(pos.coin == coin_upper for pos in self.positions):
                continue

            exchange_rates: List[tuple[str, float]] = []
            for exchange_name, rates in self._funding_cache.items():
                if coin_upper in rates:
                    exchange_rates.append((exchange_name, rates[coin_upper]))

            if len(exchange_rates) < 2:
                continue

            candidates = []
            for (ex_a, rate_a), (ex_b, rate_b) in combinations(exchange_rates, 2):
                if self._exchange_group(ex_a) == self._exchange_group(ex_b):
                    continue
                if rate_a <= rate_b:
                    long_exchange, long_rate = ex_a, rate_a
                    short_exchange, short_rate = ex_b, rate_b
                else:
                    long_exchange, long_rate = ex_b, rate_b
                    short_exchange, short_rate = ex_a, rate_a
                rate_diff = short_rate - long_rate
                net_edge = rate_diff - min_rate_diff
                if net_edge <= 0:
                    continue
                candidates.append(
                    (
                        net_edge,
                        rate_diff,
                        long_exchange,
                        short_exchange,
                        long_rate,
                        short_rate,
                    )
                )

            candidates.sort(reverse=True)
            for rank, (net_edge, rate_diff, long_exchange, short_exchange, long_rate, short_rate) in enumerate(
                candidates[:self.config.max_candidate_pairs],
                start=1,
            ):
                logger.info(
                    f"  FUND │ ★ 기회 cand#{rank} │ {coin_upper} "
                    f"│ Long {long_exchange}({long_rate*100:+.4f}%/8h) "
                    f"← diff={rate_diff*100:.4f}%/8h net={net_edge*100:.4f}%/8h → "
                    f"Short {short_exchange}({short_rate*100:+.4f}%/8h)"
                )
                opened = await self._open_position(
                    coin=coin_upper,
                    long_exchange=long_exchange,
                    short_exchange=short_exchange,
                    rate_diff=rate_diff,
                )
                if opened:
                    break

    async def _open_position(self, coin: str, long_exchange: str, short_exchange: str, rate_diff: float):
        long_wrapper = self.wrappers.get(long_exchange)
        short_wrapper = self.wrappers.get(short_exchange)
        if not long_wrapper or not short_wrapper:
            logger.warning(f"  FUND │ 래퍼 없음: long={long_exchange}, short={short_exchange}")
            return False

        try:
            long_price, short_price = await asyncio.gather(
                long_wrapper.get_mark_price(coin),
                short_wrapper.get_mark_price(coin),
            )
            long_price = float(long_price or 0.0)
            short_price = float(short_price or 0.0)
            if long_price <= 0 or short_price <= 0:
                logger.warning(
                    f"  FUND │ 가격 조회 실패 │ {coin} "
                    f"long={long_exchange}:{long_price} short={short_exchange}:{short_price}"
                )
                return False

            notional = self.config.margin_per_leg * self.config.leverage
            if _SYNERGY:
                notional *= REGIME_GATE.scale("funding_arb")

            required_notional = max(
                self._required_notional(long_exchange, coin, long_price, notional),
                self._required_notional(short_exchange, coin, short_price, notional),
            )
            if required_notional > (notional * self.config.max_retry_size_multiplier):
                logger.info(
                    f"  FUND │ skip {coin} │ min_qty blocker "
                    f"required_notional=${required_notional:.2f} > "
                    f"${notional * self.config.max_retry_size_multiplier:.2f}"
                )
                return False

            if _SYNERGY:
                ok_l, r_l = VENUE_CAP.check_entry(long_exchange, "long", required_notional)
                ok_s, r_s = VENUE_CAP.check_entry(short_exchange, "short", required_notional)
                if not ok_l or not ok_s:
                    logger.info(f"  FUND │ skip {coin} — venue_cap {r_l or r_s}")
                    return False

            long_size = self._compute_leg_size(long_exchange, coin, long_price, required_notional)
            short_size = self._compute_leg_size(short_exchange, coin, short_price, required_notional)
            if long_size <= 0 or short_size <= 0:
                return False

            try:
                await long_wrapper.update_leverage(coin, self.config.leverage, "cross")
                await short_wrapper.update_leverage(coin, self.config.leverage, "cross")
            except Exception as exc:
                logger.debug(f"  FUND │ 레버리지 설정 에러 (무시): {exc}")

            results = await asyncio.gather(
                long_wrapper.create_order(coin, "buy", long_size, order_type="market"),
                short_wrapper.create_order(coin, "sell", short_size, order_type="market"),
                return_exceptions=True,
            )

            long_ok = not isinstance(results[0], Exception)
            short_ok = not isinstance(results[1], Exception)

            if long_ok and short_ok:
                pos = FundingPosition(
                    coin=coin,
                    long_exchange=long_exchange,
                    short_exchange=short_exchange,
                    entry_rate_diff=rate_diff,
                    entry_time=time.time(),
                    margin=required_notional / max(self.config.leverage, 1),
                )
                self.positions.append(pos)
                if _SYNERGY:
                    tid = f"{coin}_{int(time.time())}"
                    POSITION_REGISTRY.register("funding_arb", long_exchange, coin, "long", required_notional, trade_id=tid)
                    POSITION_REGISTRY.register("funding_arb", short_exchange, coin, "short", required_notional, trade_id=tid)
                    pos.synergy_tid = tid
                logger.info(
                    f"  FUND │ ✓ 진입 완료 │ {coin} "
                    f"Long@{long_exchange}({long_size}) Short@{short_exchange}({short_size}) "
                    f"│ notional=${required_notional:.2f} diff={rate_diff*100:.4f}%/8h"
                )
                return True

            if long_ok and not short_ok:
                short_info = self._capture_execution_error(short_exchange, coin, results[1])
                logger.warning(f"  FUND │ Short 실패, Long 롤백: {short_info}")
                try:
                    await long_wrapper.create_order(coin, "sell", long_size, order_type="market")
                except Exception as rollback_exc:
                    logger.error(f"  FUND │ Long 롤백 실패! 수동 확인 필요: {rollback_exc}")
            elif short_ok and not long_ok:
                long_info = self._capture_execution_error(long_exchange, coin, results[0])
                logger.warning(f"  FUND │ Long 실패, Short 롤백: {long_info}")
                try:
                    await short_wrapper.create_order(coin, "buy", short_size, order_type="market")
                except Exception as rollback_exc:
                    logger.error(f"  FUND │ Short 롤백 실패! 수동 확인 필요: {rollback_exc}")
            else:
                long_info = self._capture_execution_error(long_exchange, coin, results[0])
                short_info = self._capture_execution_error(short_exchange, coin, results[1])
                logger.error(f"  FUND │ 양쪽 모두 실패: L={long_info} S={short_info}")
            return False

        except Exception as exc:
            logger.error(f"  FUND │ 진입 에러: {exc}", exc_info=True)
            return False

    async def _manage_positions(self):
        for pos in list(self.positions):
            should_close = False
            reason = ""

            long_rate = self._funding_cache.get(pos.long_exchange, {}).get(pos.coin)
            short_rate = self._funding_cache.get(pos.short_exchange, {}).get(pos.coin)
            close_rate_diff = self._effective_close_rate_diff()

            if long_rate is not None and short_rate is not None:
                current_diff = short_rate - long_rate
                if current_diff <= close_rate_diff:
                    should_close = True
                    reason = f"rate_narrowed (diff={current_diff*100:.4f}%/8h)"
                if current_diff < 0:
                    should_close = True
                    reason = f"rate_reversed (diff={current_diff*100:.4f}%/8h)"

            hold_hours = (time.time() - pos.entry_time) / 3600
            if hold_hours > self.config.max_hold_hours:
                should_close = True
                reason = f"max_hold_exceeded ({hold_hours:.1f}h)"

            if should_close:
                await self._close_position(pos, reason=reason)

    async def _close_position(self, pos: FundingPosition, reason: str = ""):
        long_wrapper = self.wrappers.get(pos.long_exchange)
        short_wrapper = self.wrappers.get(pos.short_exchange)

        try:
            close_tasks = []
            if long_wrapper:
                close_tasks.append(long_wrapper.close_position(pos.coin))
            if short_wrapper:
                close_tasks.append(short_wrapper.close_position(pos.coin))

            close_errors = []
            if close_tasks:
                close_results = await asyncio.gather(*close_tasks, return_exceptions=True)
                close_errors = [result for result in close_results if isinstance(result, Exception)]
                if close_errors:
                    logger.error(f"  FUND │ 청산 에러 {len(close_errors)}건: {close_errors}")

            hold_hours = (time.time() - pos.entry_time) / 3600
            logger.info(
                f"  FUND │ ✓ 청산 │ {pos.coin} "
                f"Long@{pos.long_exchange} Short@{pos.short_exchange} "
                f"│ {hold_hours:.1f}h │ reason={reason}"
            )

            if close_errors:
                logger.warning(f"  FUND │ 포지션 {pos.coin} remove 보류 — repair_required")
                return

            self.positions.remove(pos)
            if _SYNERGY and hasattr(pos, "synergy_tid"):
                for key in list(POSITION_REGISTRY._positions.keys()):
                    if key[0] == "funding_arb" and key[3] == pos.synergy_tid:
                        POSITION_REGISTRY._positions.pop(key, None)

        except Exception as exc:
            logger.error(f"  FUND │ 청산 에러: {exc}", exc_info=True)

    def get_state(self) -> dict:
        return {
            "positions": [
                {
                    "coin": pos.coin,
                    "long": pos.long_exchange,
                    "short": pos.short_exchange,
                    "entry_diff_8h": f"{pos.entry_rate_diff*100:.4f}%",
                    "hold_hours": round((time.time() - pos.entry_time) / 3600, 1),
                }
                for pos in self.positions
            ],
            "funding_rates": {
                exchange_name: {
                    coin: f"{rate*100:+.4f}%/8h" for coin, rate in rates.items()
                }
                for exchange_name, rates in self._funding_cache.items()
            },
            "funding_meta": self._funding_meta,
            "active_count": len(self.positions),
        }

    def get_status_line(self) -> str:
        if not self.positions:
            rates_count = sum(len(v) for v in self._funding_cache.values())
            return f"FUND │ 대기 │ {rates_count} rates cached"

        lines = []
        for pos in self.positions:
            hold_hours = (time.time() - pos.entry_time) / 3600
            lines.append(f"{pos.coin} L@{pos.long_exchange[:4]} S@{pos.short_exchange[:4]} {hold_hours:.1f}h")
        return f"FUND │ {len(self.positions)} pos │ {' | '.join(lines)}"
