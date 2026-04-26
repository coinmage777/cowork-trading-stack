"""
Funding Rate Collector (v2)
===========================
16+ Perp DEX 거래소의 BTC/ETH/SOL 펀딩레이트를 주기적으로 수집 → SQLite DB 저장.
거래소 간 스프레드 자동 계산 + funding_simulator / funding_arb 의 데이터 소스.

단독 실행:
  python -m strategies.funding_collector --config config.yaml
  python -m strategies.funding_collector --config config.yaml --once

봇 내 병렬 태스크:
  collector = FundingCollector(config)
  asyncio.create_task(collector.run())
"""

import asyncio
import aiohttp
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── mpdex_gap_recorder (Rust) bridge ──
# Modes (env GAP_RECORDER_MODE):
#   "shadow"      — default: write to BOTH Python sqlite3 INSERT and Rust recorder.
#                   Used to validate row counts match before flipping to rust_only.
#   "rust_only"   — write only via Rust (batched). Python INSERT skipped.
#   "python_only" — legacy path; no Rust calls. Fallback if Rust import fails.
#
# Env GAP_RECORDER_RETENTION_HOURS (float, default 0 = disabled):
#   If > 0, after each collect_price_gaps cycle we (a) count rows older than N
#   hours (dry-run) and (b) only actually prune when GAP_RECORDER_PRUNE_APPLY=1.
try:
    from mpdex_gap_recorder_bridge import GapRecorderClient, is_available as _gap_is_available
    _GAP_RECORDER_AVAILABLE = bool(_gap_is_available())
except Exception as _gap_exc:  # pragma: no cover
    GapRecorderClient = None  # type: ignore
    _GAP_RECORDER_AVAILABLE = False
    logger.warning(f"  PGAP  | mpdex_gap_recorder bridge unavailable: {_gap_exc!r} (falling back to Python INSERT)")

_GAP_RECORDER_MODE = (os.environ.get("GAP_RECORDER_MODE") or "shadow").strip().lower()
if _GAP_RECORDER_MODE not in ("shadow", "rust_only", "python_only"):
    logger.warning(f"  PGAP  | Unknown GAP_RECORDER_MODE={_GAP_RECORDER_MODE!r}, defaulting to shadow")
    _GAP_RECORDER_MODE = "shadow"
if not _GAP_RECORDER_AVAILABLE and _GAP_RECORDER_MODE != "python_only":
    logger.warning(f"  PGAP  | Rust recorder unavailable; forcing python_only mode (was {_GAP_RECORDER_MODE!r})")
    _GAP_RECORDER_MODE = "python_only"

try:
    _GAP_RETENTION_HOURS = float(os.environ.get("GAP_RECORDER_RETENTION_HOURS", "0") or 0)
except ValueError:
    _GAP_RETENTION_HOURS = 0.0
_GAP_PRUNE_APPLY = (os.environ.get("GAP_RECORDER_PRUNE_APPLY", "0").strip() == "1")

# HL 기반 거래소 (동일 펀딩레이트 — HL API 한 번만 조회)
HL_BASED_EXCHANGES = {
    "hyperliquid", "hyperliquid_2", "miracle", "dreamcash", "based",
    "supercexy", "bullpen", "dexari", "liquid", "hyena", "hl_wallet_b",
    "hl_wallet_c", "katana", "decibel", "ethereal", "treadfi",
    "hl_wallet_c", "hyena_2",
}


# ──────────────────────────────────────────
# DB
# ──────────────────────────────────────────

class FundingDB:
    """펀딩레이트 SQLite DB"""

    def __init__(self, db_path: str = "funding_rates.db"):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS funding_rates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                exchange TEXT NOT NULL,
                symbol TEXT NOT NULL,
                funding_rate REAL NOT NULL,
                funding_rate_8h REAL,
                funding_interval TEXT,
                next_funding_time TEXT,
                source TEXT DEFAULT 'api'
            );
            CREATE INDEX IF NOT EXISTS idx_fr_ts ON funding_rates(timestamp, exchange, symbol);
            CREATE INDEX IF NOT EXISTS idx_fr_exchange ON funding_rates(exchange, symbol, timestamp);

            CREATE TABLE IF NOT EXISTS funding_spreads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                max_exchange TEXT NOT NULL,
                min_exchange TEXT NOT NULL,
                max_rate_8h REAL NOT NULL,
                min_rate_8h REAL NOT NULL,
                spread_8h REAL NOT NULL,
                spread_pct REAL NOT NULL,
                all_rates TEXT,
                actionable BOOLEAN DEFAULT 0,
                estimated_profit_pct REAL
            );
            CREATE INDEX IF NOT EXISTS idx_fs_ts ON funding_spreads(timestamp, symbol);
            CREATE INDEX IF NOT EXISTS idx_fs_actionable ON funding_spreads(actionable, symbol);

            CREATE TABLE IF NOT EXISTS price_gaps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                max_exchange TEXT NOT NULL,
                min_exchange TEXT NOT NULL,
                max_price REAL NOT NULL,
                min_price REAL NOT NULL,
                gap_usd REAL NOT NULL,
                gap_pct REAL NOT NULL,
                all_prices TEXT,
                actionable BOOLEAN DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_pg_ts ON price_gaps(timestamp, symbol);

            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                open_time TEXT NOT NULL,
                close_time TEXT,
                symbol TEXT NOT NULL,
                long_exchange TEXT NOT NULL,
                short_exchange TEXT NOT NULL,
                entry_spread_8h REAL NOT NULL,
                exit_spread_8h REAL,
                margin_per_leg REAL NOT NULL,
                leverage INTEGER NOT NULL,
                est_funding_collected REAL DEFAULT 0,
                est_fee_cost REAL DEFAULT 0,
                est_pnl REAL,
                status TEXT DEFAULT 'open',
                close_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS live_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                open_time TEXT NOT NULL,
                close_time TEXT,
                symbol TEXT NOT NULL,
                long_exchange TEXT NOT NULL,
                short_exchange TEXT NOT NULL,
                long_order_id TEXT,
                short_order_id TEXT,
                entry_spread_8h REAL NOT NULL,
                exit_spread_8h REAL,
                entry_price REAL,
                margin_per_leg REAL NOT NULL,
                leverage INTEGER NOT NULL,
                size REAL,
                funding_collected REAL DEFAULT 0,
                fee_cost REAL DEFAULT 0,
                realized_pnl REAL,
                status TEXT DEFAULT 'open',
                close_reason TEXT,
                notes TEXT
            );
        """)
        self._conn.commit()

    def insert_rate(self, ts: str, exchange: str, symbol: str,
                    rate: float, interval: str = "1h",
                    next_time: str = None, source: str = "api"):
        rate_8h = normalize_to_8h(rate, interval)
        self._conn.execute(
            """INSERT INTO funding_rates
               (timestamp, exchange, symbol, funding_rate, funding_rate_8h,
                funding_interval, next_funding_time, source)
               VALUES (?,?,?,?,?,?,?,?)""",
            (ts, exchange, symbol, rate, rate_8h, interval, next_time, source),
        )

    def insert_spread(self, ts: str, symbol: str, max_ex: str, min_ex: str,
                      max_rate_8h: float, min_rate_8h: float,
                      all_rates: dict, fee_threshold: float = 0.0007):
        spread = max_rate_8h - min_rate_8h
        spread_pct = spread * 100
        profit = spread - fee_threshold
        actionable = profit > 0
        self._conn.execute(
            """INSERT INTO funding_spreads
               (timestamp, symbol, max_exchange, min_exchange, max_rate_8h, min_rate_8h,
                spread_8h, spread_pct, all_rates, actionable, estimated_profit_pct)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (ts, symbol, max_ex, min_ex, max_rate_8h, min_rate_8h,
             spread, spread_pct, json.dumps(all_rates), int(actionable), profit * 100),
        )

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    @property
    def conn(self):
        return self._conn


def normalize_to_8h(rate: float, interval: str) -> float:
    """펀딩레이트를 8h 기준으로 정규화"""
    multiplier = {"1h": 8, "4h": 2, "8h": 1, "24h": 1/3}
    return rate * multiplier.get(interval, 8)


# ──────────────────────────────────────────
# Collector
# ──────────────────────────────────────────

class FundingCollector:

    def __init__(self, config: dict):
        fc = config.get("funding_collector", config)
        self.symbols = fc.get("symbols", ["BTC", "ETH"])
        self.interval_minutes = fc.get("interval_minutes", 10)
        self.db_path = fc.get("db_path", "funding_rates.db")
        self.fee_threshold = fc.get("fee_threshold", 0.0007)
        self.external_cfg = fc.get("external_dexes", {})
        self.running = False
        self.db = FundingDB(self.db_path)
        self._session: Optional[aiohttp.ClientSession] = None

        # mpdex_gap_recorder (Rust) client — one per collector instance.
        # flush_threshold: 100 rows. With 2-3 symbols per cycle the Python
        # shadow INSERT path still commits every cycle, so the Rust buffer
        # is flushed explicitly at end of each collect_price_gaps() call.
        self._gap_mode: str = _GAP_RECORDER_MODE
        self._gap_rec: Optional[Any] = None
        self._gap_shadow_mismatches: int = 0
        if self._gap_mode != "python_only" and _GAP_RECORDER_AVAILABLE:
            try:
                self._gap_rec = GapRecorderClient(self.db_path, flush_threshold=100)
                logger.info(f"  PGAP  | Rust gap_recorder active | mode={self._gap_mode} db={self.db_path}")
            except Exception as e:
                logger.error(f"  PGAP  | Failed to init GapRecorderClient: {e!r} — falling back to python_only")
                self._gap_rec = None
                self._gap_mode = "python_only"
        else:
            logger.info(f"  PGAP  | Rust gap_recorder disabled | mode={self._gap_mode}")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self._session

    # ── HL ──

    async def _fetch_hl(self) -> dict[str, dict]:
        s = await self._get_session()
        try:
            async with s.post("https://api.hyperliquid.xyz/info",
                              json={"type": "metaAndAssetCtxs"}) as r:
                if r.status != 200:
                    return {}
                data = await r.json()
            universe = data[0].get("universe", [])
            ctxs = data[1]
            out = {}
            for i, asset in enumerate(universe):
                name = asset.get("name", "")
                if name in self.symbols and i < len(ctxs):
                    rate = ctxs[i].get("funding")
                    if rate is not None:
                        out[name] = {"rate": float(rate), "interval": "1h",
                                     "source": "hl_api"}
            return out
        except Exception as e:
            logger.debug(f"[funding] HL: {e}")
            return {}

    # ── Katana ──

    async def _fetch_katana(self) -> dict[str, dict]:
        s = await self._get_session()
        try:
            async with s.get("https://api-perps.katana.network/v1/markets") as r:
                if r.status != 200:
                    return {}
                markets = await r.json()
            out = {}
            for m in markets:
                sym = m.get("baseAsset", "")
                if sym in self.symbols:
                    rate = m.get("currentFundingRate") or m.get("lastFundingRate")
                    nft = m.get("nextFundingTime")
                    if rate is not None:
                        out[sym] = {"rate": float(rate), "interval": "1h",
                                    "next_time": str(nft) if nft else None,
                                    "source": "katana_api"}
            return out
        except Exception as e:
            logger.debug(f"[funding] Katana: {e}")
            return {}

    # ── dYdX v4 ──

    async def _fetch_dydx(self) -> dict[str, dict]:
        if not self.external_cfg.get("dydx", {}).get("enabled", True):
            return {}
        s = await self._get_session()
        base = self.external_cfg.get("dydx", {}).get("api_url", "https://indexer.dydx.trade/v4")
        out = {}
        for sym in self.symbols:
            ticker = f"{sym}-USD"
            try:
                async with s.get(f"{base}/perpetualMarkets", params={"ticker": ticker}) as r:
                    if r.status != 200:
                        continue
                    data = await r.json()
                    mkt = data.get("markets", {}).get(ticker, {})
                    rate = mkt.get("nextFundingRate")
                    if rate is not None:
                        out[sym] = {"rate": float(rate), "interval": "1h",
                                    "source": "dydx_api"}
            except Exception as e:
                logger.debug(f"[funding] dYdX {sym}: {e}")
        return out

    # ── Vertex ──

    async def _fetch_vertex(self) -> dict[str, dict]:
        if not self.external_cfg.get("vertex", {}).get("enabled", False):
            return {}
        s = await self._get_session()
        try:
            url = "https://archive.prod.vertexprotocol.com/v1"
            payload = {
                "funding_rates": {
                    "product_ids": [2, 4, 6]  # BTC=2, ETH=4, SOL=6
                }
            }
            async with s.post(url, json=payload) as r:
                if r.status != 200:
                    return {}
                data = await r.json()
            sym_map = {2: "BTC", 4: "ETH", 6: "SOL"}
            out = {}
            for pid, sym in sym_map.items():
                if sym not in self.symbols:
                    continue
                rates = data.get("funding_rates", {}).get(str(pid), [])
                if rates:
                    latest = rates[-1] if isinstance(rates, list) else rates
                    rate = latest.get("funding_rate")
                    if rate is not None:
                        out[sym] = {"rate": float(rate), "interval": "1h",
                                    "source": "vertex_api"}
            return out
        except Exception as e:
            logger.debug(f"[funding] Vertex: {e}")
            return {}

    # ── EdgeX ──

    async def _fetch_edgex(self) -> dict[str, dict]:
        s = await self._get_session()
        id_map = {"BTC": "10000001", "ETH": "10000002", "SOL": "10000003"}
        out = {}
        for sym, cid in id_map.items():
            if sym not in self.symbols:
                continue
            try:
                url = f"https://pro.edgex.exchange/api/v1/public/quote/getTicker?contractId={cid}"
                async with s.get(url) as r:
                    if r.status != 200:
                        continue
                    data = await r.json()
                    tickers = data.get("data", [])
                    if not tickers:
                        continue
                    t = tickers[0]
                    rate = t.get("fundingRate")
                    if rate is not None:
                        out[sym] = {"rate": float(rate), "interval": "8h",
                                    "source": "edgex_api"}
            except Exception as e:
                logger.debug(f"[funding] EdgeX {sym}: {e}")
        return out

    # ── StandX ──

    async def _fetch_standx(self) -> dict[str, dict]:
        s = await self._get_session()
        out = {}
        for sym in self.symbols:
            try:
                url = f"https://perps.standx.com/api/query_symbol_market?symbol={sym}-USD"
                async with s.get(url) as r:
                    if r.status != 200:
                        continue
                    data = await r.json()
                    rate = data.get("funding_rate")
                    nft = data.get("next_funding_time")
                    if rate is not None:
                        out[sym] = {"rate": float(rate), "interval": "1h",
                                    "next_time": nft, "source": "standx_api"}
            except Exception as e:
                logger.debug(f"[funding] StandX {sym}: {e}")
        return out

    # ── Backpack ──

    async def _fetch_backpack(self) -> dict[str, dict]:
        s = await self._get_session()
        out = {}
        try:
            async with s.get("https://api.backpack.exchange/api/v1/markPrices") as r:
                if r.status != 200:
                    return {}
                data = await r.json()
            sym_map = {"BTC_USDC_PERP": "BTC", "ETH_USDC_PERP": "ETH", "SOL_USDC_PERP": "SOL"}
            for m in data:
                sym = sym_map.get(m.get("symbol"))
                if sym and sym in self.symbols:
                    rate = m.get("fundingRate")
                    nft = m.get("nextFundingTimestamp")
                    if rate is not None:
                        out[sym] = {"rate": float(rate), "interval": "1h",
                                    "next_time": str(nft) if nft else None,
                                    "source": "backpack_api"}
        except Exception as e:
            logger.debug(f"[funding] Backpack: {e}")
        return out

    # ── Aster ──

    async def _fetch_aster(self) -> dict[str, dict]:
        s = await self._get_session()
        out = {}
        for sym in self.symbols:
            try:
                async with s.get(f"https://fapi.asterdex.com/fapi/v3/premiumIndex",
                                 params={"symbol": f"{sym}USDT"}) as r:
                    if r.status != 200:
                        continue
                    data = await r.json()
                    rate = data.get("lastFundingRate")
                    nft = data.get("nextFundingTime")
                    if rate is not None:
                        out[sym] = {"rate": float(rate), "interval": "8h",
                                    "next_time": str(nft) if nft else None,
                                    "source": "aster_api"}
            except Exception as e:
                logger.debug(f"[funding] Aster {sym}: {e}")
        return out

    # ── Pacifica ──

    async def _fetch_pacifica(self) -> dict[str, dict]:
        """
        Pacifica (SVM Perp DEX) 펀딩레이트 — 공개 REST, 인증 불필요.

        Endpoint: GET https://api.pacifica.fi/api/v1/info
        Payload  : {"success": true, "data": [{"symbol": "BTC", "funding_rate": "0.000015",
                   "next_funding_rate": "...", "base_asset": "BTC", ...}, ...]}
        Interval : 1시간 (Pacifica는 hourly funding)

        반환: {symbol: {rate, interval, next_rate, mark_price?, source}}
        """
        if not self.external_cfg.get("pacifica", {"enabled": True}).get("enabled", True):
            return {}
        s = await self._get_session()
        out = {}
        try:
            async with s.get("https://api.pacifica.fi/api/v1/info") as r:
                if r.status != 200:
                    return {}
                payload = await r.json()
            if not isinstance(payload, dict) or not payload.get("success"):
                return {}
            data = payload.get("data", [])
            if not isinstance(data, list):
                return {}
            for m in data:
                sym = (m.get("base_asset") or m.get("symbol") or "").upper()
                if sym not in self.symbols:
                    continue
                rate_str = m.get("funding_rate")
                if rate_str is None:
                    continue
                try:
                    rate = float(rate_str)
                except (TypeError, ValueError):
                    continue
                entry: dict[str, Any] = {
                    "rate": rate,
                    "interval": "1h",
                    "source": "pacifica_api",
                }
                next_rate = m.get("next_funding_rate")
                if next_rate is not None:
                    try:
                        entry["next_rate"] = float(next_rate)
                    except (TypeError, ValueError):
                        pass
                out[sym] = entry
        except Exception as e:
            logger.debug(f"[funding] Pacifica: {e}")
        return out

    # ── Ostium ──

    async def _fetch_ostium(self) -> dict[str, dict]:
        s = await self._get_session()
        out = {}
        try:
            async with s.get("https://metadata-backend.ostium.io/PricePublish/latest-prices") as r:
                if r.status != 200:
                    return {}
                data = await r.json()
                # Ostium 가격만 수집 (펀딩은 SDK 필요 — 추후)
                # 가격 갭 분석에는 유용
        except Exception as e:
            logger.debug(f"[funding] Ostium: {e}")
        return out

    # ── PerpDexList Metrics (OI/Volume 기반 필터링) ──

    async def fetch_exchange_metrics(self) -> dict[str, dict]:
        """perpdexlist.com에서 거래소별 볼륨/OI 가져오기"""
        s = await self._get_session()
        try:
            async with s.get("https://perpdexlist.com/api/exchanges/metrics",
                             headers={"User-Agent": "Mozilla/5.0"}) as r:
                if r.status != 200:
                    return {}
                data = await r.json()
                metrics = data.get("metrics", [])
                result = {}
                for m in metrics:
                    ex = m.get("exchange", "")
                    result[ex] = {
                        "volume_24h": m.get("volume_24h", 0) or 0,
                        "open_interest": m.get("open_interest", 0) or 0,
                        "status": m.get("status", "unknown"),
                    }
                return result
        except Exception as e:
            logger.debug(f"[funding] Metrics fetch: {e}")
            return {}

    # ── PerpDexList (32개 거래소 한 번에) ──

    async def _fetch_perpdexlist(self) -> dict[str, dict]:
        """perpdexlist.com 공개 API — 32개 거래소 펀딩레이트 한 번에 수집"""
        s = await self._get_session()
        out = {}
        try:
            async with s.get("https://perpdexlist.com/api/exchanges/funding",
                             headers={"User-Agent": "Mozilla/5.0"}) as r:
                if r.status != 200:
                    return {}
                data = await r.json()
                funding = data.get("funding", data)
                if not isinstance(funding, list):
                    return {}

                for item in funding:
                    ex = item.get("exchange", "")
                    sym = (item.get("base_asset") or item.get("symbol", "")).upper()
                    if sym not in self.symbols:
                        continue
                    rate = item.get("funding_rate")
                    if rate is None:
                        continue
                    interval_h = item.get("funding_interval_hours", 1)
                    interval_str = f"{interval_h}h"

                    ex_key = f"pdl_{ex}"  # prefix to avoid collision with direct API
                    out.setdefault(ex_key, {})[sym] = {
                        "rate": float(rate),
                        "interval": interval_str,
                        "source": "perpdexlist",
                    }
        except Exception as e:
            logger.debug(f"[funding] PerpDexList: {e}")
        return out

    # ── Collect All ──

    async def collect_all(self) -> dict[str, dict[str, float]]:
        now = datetime.now(timezone.utc).isoformat()

        fetchers = {
            "hyperliquid": self._fetch_hl(),
            "katana_ind": self._fetch_katana(),
            "dydx": self._fetch_dydx(),
            "edgex": self._fetch_edgex(),
            "standx": self._fetch_standx(),
            "backpack": self._fetch_backpack(),
            "aster": self._fetch_aster(),
            "vertex": self._fetch_vertex(),
            "pacifica": self._fetch_pacifica(),
            "perpdexlist": self._fetch_perpdexlist(),
        }

        results_raw = await asyncio.gather(*fetchers.values(), return_exceptions=True)
        fetcher_names = list(fetchers.keys())

        all_rates: dict[str, dict[str, float]] = {}  # {exchange: {symbol: rate_8h}}
        collected = 0

        for i, result in enumerate(results_raw):
            name = fetcher_names[i]
            if isinstance(result, Exception) or not result:
                continue

            # perpdexlist returns {ex_key: {sym: info}}, others return {sym: info}
            if name == "perpdexlist":
                for ex_key, symbols_data in result.items():
                    for symbol, info in symbols_data.items():
                        rate = info["rate"]
                        interval = info.get("interval", "1h")
                        rate_8h = normalize_to_8h(rate, interval)
                        source = info.get("source", "perpdexlist")
                        self.db.insert_rate(now, ex_key, symbol, rate, interval, None, source)
                        all_rates.setdefault(ex_key, {})[symbol] = rate_8h
                        collected += 1
            else:
                for symbol, info in result.items():
                    rate = info["rate"]
                    interval = info.get("interval", "1h")
                    rate_8h = normalize_to_8h(rate, interval)
                    source = info.get("source", "api")
                    next_time = info.get("next_time")
                    ex_name = name if name != "katana_ind" else "katana_independent"
                    self.db.insert_rate(now, ex_name, symbol, rate, interval, next_time, source)
                    all_rates.setdefault(ex_name, {})[symbol] = rate_8h
                    collected += 1

        # Spread calculation per symbol
        for symbol in self.symbols:
            sym_rates = {ex: rates[symbol] for ex, rates in all_rates.items() if symbol in rates}
            if len(sym_rates) >= 2:
                max_ex = max(sym_rates, key=sym_rates.get)
                min_ex = min(sym_rates, key=sym_rates.get)
                self.db.insert_spread(
                    now, symbol, max_ex, min_ex,
                    sym_rates[max_ex], sym_rates[min_ex],
                    {k: round(v, 8) for k, v in sym_rates.items()},
                    self.fee_threshold,
                )

        self.db.commit()

        ex_count = len(all_rates)
        logger.info(f"  FUND  | 수집 완료: {collected}건 ({ex_count}개 소스)")
        return all_rates

    # ── Price Gap Collection ──

    async def collect_price_gaps(self):
        """모든 거래소의 BTC/ETH mark price 비교 → 갭 저장"""
        now = datetime.now(timezone.utc).isoformat()
        s = await self._get_session()
        fee_pct = 0.0007  # 양쪽 taker 합계

        prices: dict[str, dict[str, float]] = {}  # {exchange: {symbol: price}}

        async def _hl_prices():
            try:
                async with s.post("https://api.hyperliquid.xyz/info",
                                  json={"type": "metaAndAssetCtxs"}) as r:
                    data = await r.json()
                    universe = data[0]["universe"]
                    ctxs = data[1]
                    for i, asset in enumerate(universe):
                        if asset["name"] in self.symbols:
                            prices.setdefault("hyperliquid", {})[asset["name"]] = float(ctxs[i]["markPx"])
            except Exception:
                pass

        async def _dydx_prices():
            for sym in self.symbols:
                try:
                    async with s.get(f"https://indexer.dydx.trade/v4/perpetualMarkets?ticker={sym}-USD") as r:
                        data = await r.json()
                        p = data.get("markets", {}).get(f"{sym}-USD", {}).get("oraclePrice")
                        if p:
                            prices.setdefault("dydx", {})[sym] = float(p)
                except Exception:
                    pass

        async def _katana_prices():
            try:
                async with s.get("https://api-perps.katana.network/v1/markets") as r:
                    for m in await r.json():
                        sym = m.get("baseAsset", "")
                        if sym in self.symbols:
                            prices.setdefault("katana", {})[sym] = float(m["indexPrice"])
            except Exception:
                pass

        async def _edgex_prices():
            id_map = {"BTC": "10000001", "ETH": "10000002", "SOL": "10000003"}
            for sym, cid in id_map.items():
                if sym not in self.symbols:
                    continue
                try:
                    async with s.get(f"https://pro.edgex.exchange/api/v1/public/quote/getTicker?contractId={cid}") as r:
                        data = await r.json()
                        t = (data.get("data") or [{}])[0]
                        p = t.get("lastPrice") or t.get("oraclePrice")
                        if p:
                            prices.setdefault("edgex", {})[sym] = float(p)
                except Exception:
                    pass

        async def _standx_prices():
            for sym in self.symbols:
                try:
                    async with s.get(f"https://perps.standx.com/api/query_symbol_price?symbol={sym}-USD") as r:
                        data = await r.json()
                        p = data.get("mark_price") or data.get("index_price")
                        if p:
                            prices.setdefault("standx", {})[sym] = float(p)
                except Exception:
                    pass

        async def _backpack_prices():
            try:
                async with s.get("https://api.backpack.exchange/api/v1/markPrices") as r:
                    sym_map = {"BTC_USDC_PERP": "BTC", "ETH_USDC_PERP": "ETH", "SOL_USDC_PERP": "SOL"}
                    for m in await r.json():
                        sym = sym_map.get(m.get("symbol"))
                        if sym and sym in self.symbols:
                            prices.setdefault("backpack", {})[sym] = float(m["markPrice"])
            except Exception:
                pass

        async def _aster_prices():
            for sym in self.symbols:
                try:
                    async with s.get(f"https://fapi.asterdex.com/fapi/v3/premiumIndex",
                                     params={"symbol": f"{sym}USDT"}) as r:
                        data = await r.json()
                        p = data.get("markPrice")
                        if p:
                            prices.setdefault("aster", {})[sym] = float(p)
                except Exception:
                    pass

        async def _ostium_prices():
            try:
                async with s.get("https://metadata-backend.ostium.io/PricePublish/latest-prices") as r:
                    for item in await r.json():
                        sym = item.get("from", "").upper()
                        if sym in self.symbols and item.get("to", "").upper() == "USD":
                            prices.setdefault("ostium", {})[sym] = float(item.get("mid", 0))
            except Exception:
                pass

        await asyncio.gather(
            _hl_prices(), _dydx_prices(), _katana_prices(),
            _edgex_prices(), _standx_prices(), _backpack_prices(),
            _aster_prices(), _ostium_prices(),
            return_exceptions=True,
        )

        # Calculate gaps per symbol
        gap_count = 0
        rust_writes = 0
        python_writes = 0
        for sym in self.symbols:
            sym_prices = {ex: p[sym] for ex, p in prices.items() if sym in p}
            if len(sym_prices) < 2:
                continue
            max_ex = max(sym_prices, key=sym_prices.get)
            min_ex = min(sym_prices, key=sym_prices.get)
            gap_usd = sym_prices[max_ex] - sym_prices[min_ex]
            gap_pct = gap_usd / sym_prices[min_ex] if sym_prices[min_ex] else 0
            actionable = gap_pct > fee_pct
            all_prices_rounded = {k: round(v, 2) for k, v in sym_prices.items()}

            # Python INSERT path (legacy). Runs in shadow + python_only modes.
            if self._gap_mode in ("shadow", "python_only"):
                try:
                    self.db._conn.execute(
                        """INSERT INTO price_gaps
                           (timestamp, symbol, max_exchange, min_exchange, max_price, min_price,
                            gap_usd, gap_pct, all_prices, actionable)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (now, sym, max_ex, min_ex, sym_prices[max_ex], sym_prices[min_ex],
                         gap_usd, gap_pct, json.dumps(all_prices_rounded),
                         int(actionable)),
                    )
                    python_writes += 1
                except Exception as e:
                    logger.error(f"  PGAP  | python INSERT failed for {sym}: {e!r}")

            # Rust recorder path. Runs in shadow + rust_only modes.
            if self._gap_mode in ("shadow", "rust_only") and self._gap_rec is not None:
                try:
                    self._gap_rec.record_price_gap(
                        ts=now,
                        symbol=sym,
                        max_exchange=max_ex,
                        min_exchange=min_ex,
                        max_price=float(sym_prices[max_ex]),
                        min_price=float(sym_prices[min_ex]),
                        gap_usd=float(gap_usd),
                        gap_pct=float(gap_pct),
                        all_prices=all_prices_rounded,
                        actionable=bool(actionable),
                    )
                    rust_writes += 1
                except Exception as e:
                    logger.error(f"  PGAP  | rust record_price_gap failed for {sym}: {e!r}")

            gap_count += 1

        # Commit Python writes (no-op if rust_only mode — nothing was executed).
        if python_writes:
            self.db.commit()

        # Flush Rust buffer to SQLite.
        flushed = 0
        if self._gap_rec is not None and rust_writes > 0:
            try:
                flushed = self._gap_rec.flush()
            except Exception as e:
                logger.error(f"  PGAP  | rust flush failed: {e!r}")

        # Shadow mode: verify both paths wrote the same number of rows per cycle.
        if self._gap_mode == "shadow" and rust_writes != python_writes:
            self._gap_shadow_mismatches += 1
            logger.warning(
                f"  PGAP  | shadow mismatch #{self._gap_shadow_mismatches}: "
                f"py={python_writes} rust={rust_writes}"
            )

        if gap_count:
            mode_tag = self._gap_mode
            if mode_tag == "shadow":
                detail = f"py={python_writes} rust={rust_writes} flushed={flushed}"
            elif mode_tag == "rust_only":
                detail = f"rust={rust_writes} flushed={flushed}"
            else:
                detail = f"py={python_writes}"
            logger.info(f"  PGAP  | 가격갭 수집: {gap_count}건 ({len(prices)}개 거래소) [{mode_tag}: {detail}]")

        # Retention: dry-run count or actually prune (gated by env).
        if _GAP_RETENTION_HOURS > 0:
            await self._gap_retention_check(_GAP_RETENTION_HOURS, _GAP_PRUNE_APPLY)

    async def _gap_retention_check(self, hours: float, apply: bool) -> None:
        """Count price_gaps rows older than `hours`. If apply=True, delete them.

        Dry-run default (apply=False) only logs the count — safe during shadow
        validation. Set GAP_RECORDER_PRUNE_APPLY=1 to actually prune.
        """
        try:
            cur = self.db._conn.execute(
                "SELECT COUNT(*) FROM price_gaps WHERE datetime(timestamp) < datetime('now', ?)",
                (f"-{hours} hours",),
            )
            old_rows = int(cur.fetchone()[0])
        except Exception as e:
            logger.error(f"  PGAP  | retention count failed: {e!r}")
            return

        if old_rows <= 0:
            logger.debug(f"  PGAP  | retention {hours}h: nothing to prune")
            return

        if not apply:
            logger.info(
                f"  PGAP  | retention {hours}h [dry-run]: "
                f"{old_rows} rows older than cutoff (set GAP_RECORDER_PRUNE_APPLY=1 to delete)"
            )
            return

        # Apply prune via Rust path when available (it uses the same SQLite DB,
        # but the Rust implementation is authoritative for cleanup semantics).
        deleted = 0
        if self._gap_rec is not None:
            try:
                deleted = self._gap_rec.prune_older_than(hours)
            except Exception as e:
                logger.error(f"  PGAP  | rust prune_older_than failed: {e!r}")
                deleted = -1
        if deleted < 0 or self._gap_rec is None:
            # Python fallback prune.
            try:
                cur = self.db._conn.execute(
                    "DELETE FROM price_gaps WHERE datetime(timestamp) < datetime('now', ?)",
                    (f"-{hours} hours",),
                )
                deleted = cur.rowcount or 0
                self.db.commit()
            except Exception as e:
                logger.error(f"  PGAP  | python prune failed: {e!r}")
                return
        logger.warning(f"  PGAP  | retention {hours}h [applied]: deleted {deleted} rows")

    # ── Run Loop ──

    async def run(self):
        self.running = True
        logger.info(f"  FUND  | Funding Collector 시작 | 주기={self.interval_minutes}분 심볼={self.symbols}")

        # Initial collection
        try:
            await self.collect_all()
            await self.collect_price_gaps()
        except Exception as e:
            logger.error(f"  FUND  | 초기 수집 실패: {e}")

        while self.running:
            await asyncio.sleep(self.interval_minutes * 60)
            try:
                await self.collect_all()
                await self.collect_price_gaps()
            except Exception as e:
                logger.error(f"  FUND  | 수집 에러: {e}")

    def stop(self):
        self.running = False
        try:
            if self._session and not self._session.closed:
                asyncio.get_event_loop().create_task(self._session.close())
        except Exception:
            pass
        # Flush and close the Rust recorder before closing the shared SQLite
        # connection. Order matters: flush first (writes pending rows), then
        # close (releases the Rust-side handle), then close the Python conn.
        if self._gap_rec is not None:
            try:
                self._gap_rec.flush()
            except Exception as e:
                logger.error(f"  PGAP  | rust flush on stop failed: {e!r}")
            try:
                self._gap_rec.close()
            except Exception as e:
                logger.error(f"  PGAP  | rust close on stop failed: {e!r}")
        self.db.close()


# ── CLI ──

def main():
    import argparse
    import yaml

    parser = argparse.ArgumentParser(description="Funding Rate Collector")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true", help="1회 수집 후 종료")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    with open(args.config, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    collector = FundingCollector(config)

    async def _run():
        rates = await collector.collect_all()
        await collector.collect_price_gaps()
        if args.once:
            print(f"\n{'='*60}")
            print(f"  펀딩레이트 수집 결과 ({len(rates)}개 소스)")
            print(f"{'='*60}")
            for ex, symbols in sorted(rates.items()):
                for sym, rate in sorted(symbols.items()):
                    print(f"  {ex:20s} {sym:4s} {rate*100:+.4f}% (8h)")
            print()

            # Show spreads
            for sym in collector.symbols:
                sym_rates = {ex: rates[sym] for ex, rates in rates.items() if sym in rates}
                if len(sym_rates) >= 2:
                    max_ex = max(sym_rates, key=sym_rates.get)
                    min_ex = min(sym_rates, key=sym_rates.get)
                    spread = sym_rates[max_ex] - sym_rates[min_ex]
                    print(f"  {sym} 스프레드: {spread*100:.4f}% ({max_ex} ↔ {min_ex})")
            collector.stop()
        else:
            await collector.run()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        collector.stop()


if __name__ == "__main__":
    main()
