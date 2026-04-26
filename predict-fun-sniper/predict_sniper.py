"""
Predict.fun Expiry Sniper — crypto up/down 마켓 만료 직전 스나이핑

Polymarket expiry_snipe 전략을 predict.fun에 적용.
마켓 만료 8분 이내에 가격 방향이 확실할 때 진입.

사용법:
  python predict_sniper.py                    # 1회 스캔
  python predict_sniper.py --live             # 라이브 루프
  python predict_sniper.py --live --paper     # 페이퍼 모드 (주문 안 넣음)

필요 패키지: predict-sdk, eth-account, aiohttp, httpx
"""

import asyncio
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import httpx

from predict_client import PredictClient, Market

from dotenv import load_dotenv

logger = logging.getLogger("predict_sniper")

# ──────────────────────────────────────────
# Config
# ──────────────────────────────────────────

load_dotenv(Path(__file__).parent / ".env")

SNIPE_MAX_MINUTES = float(os.getenv("PREDICT_SNIPE_MAX_MINUTES", "8.0"))
SNIPE_MIN_STRIKE_DIST = float(os.getenv("PREDICT_SNIPE_MIN_STRIKE_DIST", "0.0008"))
SNIPE_MAX_ENTRY_PRICE = float(os.getenv("PREDICT_SNIPE_MAX_ENTRY_PRICE", "0.65"))
SNIPE_MIN_EDGE = float(os.getenv("PREDICT_SNIPE_MIN_EDGE", "0.04"))
SNIPE_BET_SIZE = int(os.getenv("PREDICT_SNIPE_BET_SIZE", "3"))
TAKER_FEE_RATE = float(os.getenv("PREDICT_TAKER_FEE_RATE", "0.02"))
SCAN_INTERVAL = int(os.getenv("PREDICT_SCAN_INTERVAL_SEC", "20"))
ASSETS = os.getenv("PREDICT_ASSETS", "BTC,ETH,BNB,SOL").split(",")
CLAIM_INTERVAL = int(os.getenv("PREDICT_CLAIM_INTERVAL_SEC", "300"))
BINANCE_REST = "https://api.binance.com"


# ──────────────────────────────────────────
# Market Fetcher
# ──────────────────────────────────────────

class PredictMarketFetcher:
    """predict.fun에서 crypto up/down 마켓 조회"""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.headers = {"x-api-key": api_key}

    async def fetch_open_crypto_markets(self) -> list[dict]:
        """OPEN 상태인 crypto up/down 마켓 조회.

        2026-04-25: categories listing이 가까운 만기 hourly 마켓을 누락 →
        listing + slug-direct GET 합집합으로 보완. listing은 fallback으로 유지하되,
        slug-direct로 ±N시간 hourly 마켓을 직접 조회해 LIVE 진입 윈도우 확보.
        """
        listing_markets: list[dict] = []
        slug_markets: list[dict] = []
        seen: set = set()

        # ---------- 1) categories listing (기존 fallback) ----------
        cursor = ""
        for page_idx in range(5):  # max 5 pages
            url = "https://api.predict.fun/v1/categories?limit=50"
            if cursor:
                url += f"&cursor={cursor}"

            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(url, headers=self.headers)
                    if resp.status_code != 200:
                        break

                    data = resp.json()
                    cats = data.get("data", [])
                    cursor = data.get("cursor", "")

                    if not cats:
                        break

                    for cat in cats:
                        slug = cat.get("slug", "")
                        status = cat.get("status", "")

                        if status != "OPEN":
                            continue

                        # crypto up/down 마켓만 필터
                        is_crypto = any(k in slug.lower() for k in [
                            "btc", "eth", "bnb", "sol", "up-down", "ethereum", "bitcoin"
                        ])
                        if not is_crypto:
                            continue

                        for m in cat.get("markets", []):
                            mid = m.get("id")
                            if mid and mid not in seen:
                                seen.add(mid)
                                parsed = self._parse_market(m, cat)
                                if parsed:
                                    listing_markets.append(parsed)

                    if not cursor:
                        break
            except httpx.TimeoutException:
                logger.warning(f"[PREDICT] API timeout fetching markets (page {page_idx + 1})")
                await asyncio.sleep(5)
                continue
            except Exception as e:
                logger.warning(f"[PREDICT] API listing error: {e}")
                break

        # ---------- 2) slug-direct GET (가까운 만기 보완) ----------
        try:
            lookahead = int(os.getenv("PREDICT_SLUG_LOOKAHEAD_HOURS", "2"))
        except (TypeError, ValueError):
            lookahead = 2
        try:
            lookback = int(os.getenv("PREDICT_SLUG_LOOKBACK_HOURS", "0"))
        except (TypeError, ValueError):
            lookback = 0
        # SOL/BNB는 현재 슬러그 패턴 미확인 → BTC/ETH만 우선. .env로 확장 가능.
        assets_csv = os.getenv("PREDICT_SLUG_ASSETS", "bitcoin,ethereum")
        slug_assets = [a.strip().lower() for a in assets_csv.split(",") if a.strip()]

        candidates = self._generate_slug_candidates(
            datetime.now(timezone.utc),
            lookahead_hours=lookahead,
            lookback_hours=lookback,
            assets=slug_assets,
        )

        if candidates:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    tasks = [self._fetch_one_slug(client, slug) for slug in candidates]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                for slug, res in zip(candidates, results):
                    if isinstance(res, Exception):
                        # silent — 일시적 네트워크 오류
                        continue
                    if not res:
                        continue
                    cat = res
                    status = cat.get("status", "")
                    if status != "OPEN":
                        continue
                    for m in cat.get("markets", []) or []:
                        mid = m.get("id")
                        if mid and mid not in seen:
                            seen.add(mid)
                            parsed = self._parse_market(m, cat)
                            if parsed:
                                slug_markets.append(parsed)
            except Exception as e:
                logger.warning(f"[PREDICT] slug-direct fetch error: {e}")

        merged = listing_markets + slug_markets
        logger.info(
            f"[PREDICT] fetched listing={len(listing_markets)} "
            f"slug_direct={len(slug_markets)} unique={len(merged)} "
            f"(slug_candidates={len(candidates)})"
        )

        return merged

    @staticmethod
    def _generate_slug_candidates(
        now_dt: datetime,
        lookahead_hours: int = 2,
        lookback_hours: int = 0,
        assets: Optional[list[str]] = None,
    ) -> list[str]:
        """현재 시각 ±N시간 범위의 hourly market 슬러그 후보 생성.

        Predict.fun 슬러그 포맷: `{asset}-up-or-down-{month}-{day}-{year}-{hour12}{ampm}-et`
        DST 무시 — 4월(april)은 EDT (UTC-4) 고정. 11월~3월은 EST(-5) 처리 필요하면 여기서 분기.
        """
        if assets is None:
            assets = ["bitcoin", "ethereum"]
        slugs: list[str] = []
        # EDT (UTC-4) — april ~ october. EST (UTC-5) — november ~ march.
        # 보수적으로 month 기준 분기. 정확하게는 미국 DST rule이지만 hourly cadence엔 무관.
        month_for_offset = now_dt.month
        if 4 <= month_for_offset <= 10:
            et_offset_hours = -4  # EDT
        else:
            et_offset_hours = -5  # EST
        et_tz = timezone(timedelta(hours=et_offset_hours))
        month_names = [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ]

        seen_slugs: set = set()
        for asset in assets:
            for hour_offset in range(-lookback_hours, lookahead_hours + 1):
                t = now_dt + timedelta(hours=hour_offset)
                et_time = t.astimezone(et_tz)
                month = month_names[et_time.month - 1]
                day = et_time.day
                year = et_time.year
                hour_12 = et_time.hour % 12
                if hour_12 == 0:
                    hour_12 = 12
                ampm = "am" if et_time.hour < 12 else "pm"
                slug = f"{asset}-up-or-down-{month}-{day}-{year}-{hour_12}{ampm}-et"
                if slug not in seen_slugs:
                    seen_slugs.add(slug)
                    slugs.append(slug)
        return slugs

    async def _fetch_one_slug(self, client: httpx.AsyncClient, slug: str) -> Optional[dict]:
        """단일 슬러그 직접 GET. 성공 시 category dict 반환, 실패 시 None.

        404 (slug 미존재) → silent skip. 5xx → warn. timeout → silent skip.
        """
        url = f"https://api.predict.fun/v1/categories/{slug}"
        try:
            resp = await client.get(url, headers=self.headers)
            if resp.status_code == 404:
                return None
            if resp.status_code >= 500:
                logger.warning(f"[PREDICT] slug GET 5xx [{resp.status_code}] {slug}")
                return None
            if resp.status_code != 200:
                return None
            data = resp.json()
            cat = data.get("data")
            if isinstance(cat, dict):
                return cat
            return None
        except httpx.TimeoutException:
            return None
        except Exception as e:
            logger.debug(f"[PREDICT] slug GET error {slug}: {e}")
            return None

    def _parse_market(self, m: dict, cat: dict) -> Optional[dict]:
        """마켓 데이터를 파싱하여 snipe에 필요한 정보 추출"""
        outcomes = m.get("outcomes", [])
        up_token = ""
        down_token = ""
        for o in outcomes:
            name = (o.get("name", "") or "").upper()
            token = str(o.get("onChainId", "") or "")
            if name == "UP" or name == "YES":
                up_token = token
            elif name == "DOWN" or name == "NO":
                down_token = token

        if not up_token or not down_token:
            return None

        # Extract asset from slug
        slug = m.get("categorySlug", cat.get("slug", ""))
        asset = self._extract_asset(slug)
        if not asset:
            return None

        # Extract strike price.
        # 2026-04-24 fix: Predict.fun의 현재 hourly 마켓 description에는 strike(=candle open)이
        # 들어있지 않다. 실제 open price는 `variantData.startPrice` 에 들어온다 (candle이
        # 열리는 순간부터 non-null). description regex는 구형 포맷 호환용으로만 남긴다.
        variant = m.get("variantData") or {}
        strike = 0.0
        try:
            vstart = variant.get("startPrice")
            if vstart is not None:
                strike = float(vstart)
        except (TypeError, ValueError):
            strike = 0.0
        if strike <= 0:
            desc = cat.get("description", m.get("description", ""))
            strike = self._extract_strike(desc)

        # Extract expiry time from boost_ends or slug
        expiry = self._extract_expiry(m, cat, slug)

        return {
            "id": m.get("id"),
            "question": m.get("question", m.get("title", "")),
            "slug": slug,
            "asset": asset,
            "binance_symbol": f"{asset}USDT",
            "up_token": up_token,
            "down_token": down_token,
            "strike_price": strike,
            "expiry_time": expiry,
            "neg_risk": m.get("isNegRisk", False),
            "yield_bearing": m.get("isYieldBearing", False),
            "fee_rate_bps": int(m.get("feeRateBps", 200) or 200),
            "is_boosted": m.get("isBoosted", False),
        }

    @staticmethod
    def _extract_asset(slug: str) -> str:
        slug_lower = slug.lower()
        if "btc" in slug_lower or "bitcoin" in slug_lower:
            return "BTC"
        if "eth" in slug_lower or "ethereum" in slug_lower:
            return "ETH"
        if "bnb" in slug_lower:
            return "BNB"
        if "sol" in slug_lower or "solana" in slug_lower:
            return "SOL"
        return ""

    @staticmethod
    def _extract_strike(desc: str) -> float:
        """Description에서 starting price 추출.

        2026-04-24 fix: 기존 regex `open price.*?\$?([\d,]+\.?\d*)` 는
        "open price for the BNB/USDT 1 hour candle" 에서 `1` 을 strike로
        오인식해 strike=1.0 반환 → 전 마켓이 dist_small 또는 edge_low 스킵됨.
        (1) `$` prefix를 반드시 요구하고 (2) 매칭값이 현실적 가격 범위 (>10) 일
        때만 허용한다.
        """
        match = re.search(r"starting price of \$([\d,]+\.?\d*)", desc)
        if match:
            try:
                val = float(match.group(1).replace(",", ""))
                if val > 10:
                    return val
            except ValueError:
                pass

        # "open price" 패턴 — 반드시 $ 기호가 prefix 되어 있어야 함
        match2 = re.search(r"open price[^$]{0,40}\$([\d,]+\.?\d*)", desc)
        if match2:
            try:
                val = float(match2.group(1).replace(",", ""))
                if val > 10:
                    return val
            except ValueError:
                pass

        return 0.0

    @staticmethod
    def _extract_expiry(m: dict, cat: dict, slug: str) -> float:
        """만료 시각을 unix timestamp로 추출"""
        # boost_ends_at이 있으면 사용 (정확한 만료 시각)
        boost_end = m.get("boostEndsAt") or cat.get("boostEndsAt")
        if boost_end:
            try:
                dt = datetime.fromisoformat(boost_end.replace("Z", "+00:00"))
                return dt.timestamp()
            except Exception:
                pass

        # slug에서 시간 추출 시도
        # 구형 15분 슬러그: btc-usd-up-down-2026-03-23-21-45-15-minutes
        time_match = re.search(r"(\d{4}-\d{2}-\d{2})-(\d{2})-(\d{2})-(\d+)-minut", slug)
        if time_match:
            date_str = time_match.group(1)
            hour = int(time_match.group(2))
            minute = int(time_match.group(3))
            duration = int(time_match.group(4))
            try:
                dt = datetime.strptime(f"{date_str} {hour}:{minute}", "%Y-%m-%d %H:%M")
                et_offset = timedelta(hours=4)  # EDT (UTC-4)
                dt_utc = dt + et_offset + timedelta(minutes=duration)
                return dt_utc.replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                pass

        # 신형 hourly 슬러그: bitcoin-up-or-down-april-23-2026-7pm-et
        #  → 해당 ET 시각의 1시간봉 종료(다음 정각)
        month_map = {
            "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
            "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
        }
        hourly_match = re.search(
            r"(?:bitcoin|ethereum|solana|bnb|btc|eth|sol)-up-or-down-([a-z]+)-(\d{1,2})-(\d{4})-(\d{1,2})(am|pm)-et",
            slug.lower(),
        )
        if hourly_match:
            mon_str, day_str, year_str, hour_str, ampm = hourly_match.groups()
            mon = month_map.get(mon_str)
            if mon:
                try:
                    day = int(day_str)
                    year = int(year_str)
                    hour12 = int(hour_str) % 12
                    hour_et = hour12 + (12 if ampm == "pm" else 0)
                    # "7pm ET market" = 1-hour candle starting 7pm, ending 8pm ET
                    dt_et = datetime(year, mon, day, hour_et, 0, 0)
                    et_offset = timedelta(hours=4)  # EDT (UTC-4, 4-10월)
                    dt_utc_end = dt_et + et_offset + timedelta(hours=1)
                    return dt_utc_end.replace(tzinfo=timezone.utc).timestamp()
                except Exception:
                    pass

        # 신형 daily 슬러그: bitcoin-up-or-down-on-april-23-2026
        #  → 해당 날짜 12:00 ET noon (다음 12:00 noon 마감)
        daily_match = re.search(
            r"(?:bitcoin|ethereum|solana|bnb|btc|eth|sol)-up-or-down-on-([a-z]+)-(\d{1,2})-(\d{4})",
            slug.lower(),
        )
        if daily_match:
            mon_str, day_str, year_str = daily_match.groups()
            mon = month_map.get(mon_str)
            if mon:
                try:
                    day = int(day_str)
                    year = int(year_str)
                    # 마감: 다음 날 12:00 ET noon
                    dt_et_end = datetime(year, mon, day, 12, 0, 0) + timedelta(days=1)
                    et_offset = timedelta(hours=4)
                    dt_utc_end = dt_et_end + et_offset
                    return dt_utc_end.replace(tzinfo=timezone.utc).timestamp()
                except Exception:
                    pass

        return 0.0


# ──────────────────────────────────────────
# Price Feed
# ──────────────────────────────────────────

async def get_binance_price(symbol: str) -> float:
    """Binance에서 현재가 조회"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{BINANCE_REST}/api/v3/ticker/price",
                params={"symbol": symbol},
            )
            if resp.status_code == 200:
                return float(resp.json()["price"])
    except Exception:
        pass
    return 0.0


# ──────────────────────────────────────────
# Sniper Engine
# ──────────────────────────────────────────

class PredictSniper:
    """predict.fun expiry snipe 엔진"""

    def __init__(self, predict_client: PredictClient, fetcher: PredictMarketFetcher, paper: bool = True):
        self.client = predict_client
        self.fetcher = fetcher
        self.paper = paper

        self._active_markets: set[int] = set()  # 이미 진입한 마켓
        self._trade_count = 0
        self._total_pnl = 0.0
        self._wins = 0
        self._gas_insufficient = False  # 가스비 부족 플래그

        # 자산별 edge buffer (ETH/SOL은 BTC보다 변동성 크므로 더 높은 edge 요구)
        self._asset_edge_buffers = {"BTC": 0.0, "ETH": 0.03, "SOL": 0.03, "BNB": 0.02}
        # .env에서 커스텀 버퍼 로드
        raw = os.getenv("PREDICT_ASSET_EDGE_BUFFERS", "")
        if raw:
            for pair in raw.split(","):
                parts = pair.strip().split(":")
                if len(parts) == 2:
                    self._asset_edge_buffers[parts[0].strip()] = float(parts[1].strip())

        # DB logging callback: (trade_data: dict) -> int (trade_id)
        self._on_trade_open: callable = None
        # DB close callback: (market_id: int, won: bool, entry_price: float, size: float) -> None
        self._on_trade_close: callable = None
        # Predict.fun 전용 JSON 로그 (DB 외 보조 기록)
        self._log_path = Path(__file__).parent / "predict_trades.json"

    def _calc_snipe_prob(self, abs_dist_pct: float, opp_minutes_left: float, asset: str) -> float:
        """변동성 기반 스나이프 확률 계산.

        정규분포 CDF로 "현재 가격이 만기까지 strike를 넘지 않을 확률" 추정.
        자산별 평균 1분 변동성을 하드코딩 (Binance 1분봉 기준).
        """
        # 자산별 1분 평균 변동성 (σ, 퍼센트 기준)
        vol_per_min = {
            "BTC": 0.0012,   # ~0.12% per minute
            "ETH": 0.0018,   # ~0.18%
            "SOL": 0.0025,   # ~0.25%
            "BNB": 0.0015,   # ~0.15%
        }.get(asset, 0.0015)

        remaining_vol = vol_per_min * math.sqrt(max(opp_minutes_left, 0.5))

        if remaining_vol <= 0:
            return 0.95

        z = abs_dist_pct / remaining_vol

        # Normal CDF approximation (no scipy dependency)
        # Abramowitz & Stegun approximation
        def norm_cdf(x):
            if x < 0:
                return 1.0 - norm_cdf(-x)
            t = 1.0 / (1.0 + 0.2316419 * x)
            d = 0.3989422804014327  # 1/sqrt(2*pi)
            poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
            return 1.0 - d * math.exp(-0.5 * x * x) * poly

        prob = norm_cdf(z)
        return min(0.95, max(0.50, prob))

    def set_db_callbacks(self, on_open: callable, on_close: callable):
        """main.py에서 DB 로깅 콜백 주입"""
        self._on_trade_open = on_open
        self._on_trade_close = on_close

    async def scan_and_snipe(self):
        """한 사이클: 마켓 스캔 → 기회 탐색 → 진입"""
        markets = await self.fetcher.fetch_open_crypto_markets()
        now = time.time()

        # 2026-04-24: env-based hot tuning — .env의 값을 매 스캔마다 재조회하여
        # restart 없이 MAX_MINUTES/MIN_EDGE/MAX_ENTRY_PRICE/BET_SIZE 조정 가능.
        try:
            max_minutes = float(os.getenv("PREDICT_SNIPE_MAX_MINUTES", str(SNIPE_MAX_MINUTES)))
        except (TypeError, ValueError):
            max_minutes = SNIPE_MAX_MINUTES
        try:
            max_entry_price = float(os.getenv("PREDICT_SNIPE_MAX_ENTRY_PRICE", str(SNIPE_MAX_ENTRY_PRICE)))
        except (TypeError, ValueError):
            max_entry_price = SNIPE_MAX_ENTRY_PRICE
        try:
            min_edge = float(os.getenv("PREDICT_SNIPE_MIN_EDGE", str(SNIPE_MIN_EDGE)))
        except (TypeError, ValueError):
            min_edge = SNIPE_MIN_EDGE
        try:
            min_strike_dist = float(os.getenv("PREDICT_SNIPE_MIN_STRIKE_DIST", str(SNIPE_MIN_STRIKE_DIST)))
        except (TypeError, ValueError):
            min_strike_dist = SNIPE_MIN_STRIKE_DIST

        opportunities = []
        skip_counts = {"active": 0, "no_expiry": 0, "window": 0, "no_price": 0,
                       "no_strike": 0, "dist_small": 0, "entry_cap": 0, "edge_low": 0}

        for m in markets:
            mid = m["id"]
            if mid in self._active_markets:
                skip_counts["active"] += 1
                continue

            expiry = m["expiry_time"]
            if expiry <= 0:
                skip_counts["no_expiry"] += 1
                continue

            minutes_left = (expiry - now) / 60
            if minutes_left <= 0 or minutes_left > max_minutes:
                skip_counts["window"] += 1
                continue

            # Get current price from Binance
            price = await get_binance_price(m["binance_symbol"])
            if price <= 0:
                skip_counts["no_price"] += 1
                continue

            # Get strike price
            strike = m["strike_price"]
            if strike <= 0:
                # ETH/BNB 1hr 마켓은 strike가 description에 없을 수 있음
                # candle open price = 마켓 생성 시점의 가격으로 추정
                skip_counts["no_strike"] += 1
                continue

            # Calculate direction probability
            dist_pct = (price - strike) / strike
            if abs(dist_pct) < min_strike_dist:
                skip_counts["dist_small"] += 1
                continue  # 방향 불확실

            # Volatility-based probability model
            # Use recent price movement to estimate remaining uncertainty
            snipe_prob = self._calc_snipe_prob(abs(dist_pct), opp_minutes_left=minutes_left, asset=m["asset"])

            # Get orderbook prices
            bid, ask, bid_sz, ask_sz = await self.client.get_orderbook(mid)

            if dist_pct > 0:
                # Price above strike → UP 유리
                side = "UP"
                token_id = m["up_token"]
                # UP side의 ask price가 entry price
                entry_price = ask if ask > 0 else (1.0 - bid if bid > 0 else 0.5)
            else:
                # Price below strike → DOWN 유리
                side = "DOWN"
                token_id = m["down_token"]
                entry_price = ask if ask > 0 else (1.0 - bid if bid > 0 else 0.5)

            # 실제로는 orderbook에서 반대쪽 bid를 이용해 진입가 계산
            # predict.fun의 orderbook은 YES side 기준
            if side == "UP":
                # YES 토큰을 사야 함 → ask price
                entry_price = ask if ask > 0 else 0.5
            else:
                # NO 토큰을 사야 함 → 1 - YES bid (또는 NO ask)
                # predict.fun에서 NO 토큰의 가격 = 1 - YES 가격
                entry_price = 1.0 - bid if bid > 0 else 0.5

            if entry_price > max_entry_price:
                skip_counts["entry_cap"] += 1
                continue

            gross_edge = snipe_prob - entry_price
            edge = gross_edge * (1.0 - TAKER_FEE_RATE)
            # 자산별 추가 edge buffer
            asset_buffer = self._asset_edge_buffers.get(m["asset"], 0.0)
            required_edge = min_edge + asset_buffer
            if edge < required_edge:
                skip_counts["edge_low"] += 1
                continue

            opportunities.append({
                "market": m,
                "side": side,
                "token_id": token_id,
                "entry_price": entry_price,
                "strike_price": strike,
                "current_price": price,
                "dist_pct": dist_pct,
                "snipe_prob": snipe_prob,
                "edge": edge,
                "minutes_left": minutes_left,
            })

        # Sort by edge (best first)
        opportunities.sort(key=lambda x: -x["edge"])

        # 요약 로그: 매 스캔마다 마켓 총수 + 필터 탈락 분해 (5분마다만 INFO, 그 외 DEBUG)
        total_markets = len(markets)
        total_opps = len(opportunities)
        summary_line = (
            f"[PREDICT] scan: {total_markets} markets, {total_opps} opps | "
            f"skip: active={skip_counts['active']} expiry={skip_counts['no_expiry']} "
            f"window={skip_counts['window']} price={skip_counts['no_price']} "
            f"strike={skip_counts['no_strike']} dist={skip_counts['dist_small']} "
            f"cap={skip_counts['entry_cap']} edge={skip_counts['edge_low']}"
        )
        # 5분마다 INFO, 그 외 DEBUG
        if int(now) % 300 < 10:
            logger.info(summary_line)
        else:
            logger.debug(summary_line)

        for opp in opportunities[:3]:  # max 3 per scan
            await self._execute_snipe(opp)

    async def _execute_snipe(self, opp: dict):
        """스나이프 주문 실행"""
        m = opp["market"]
        mid = m["id"]
        side = opp["side"]
        entry_price = opp["entry_price"]
        shares = SNIPE_BET_SIZE
        cost = shares * entry_price

        mode_label = "PAPER" if self.paper else "LIVE"
        logger.info(
            f"[{mode_label}] SNIPE {m['asset']} {side} | "
            f"#{mid} {m['question'][:40]}... | "
            f"${cost:.2f} ({shares}sh@{entry_price:.3f}) | "
            f"edge={opp['edge']*100:.1f}% prob={opp['snipe_prob']*100:.0f}% | "
            f"strike={opp['strike_price']:.2f} now={opp['current_price']:.2f} "
            f"dist={opp['dist_pct']*100:.2f}% | "
            f"{opp['minutes_left']:.1f}m left"
        )

        if not self.paper:
            # 실제 주문
            result = await self.client.place_order(
                market_id=mid,
                token_id=opp["token_id"],
                side="BUY",
                price=round(entry_price, 2),
                shares=shares,
                neg_risk=m.get("neg_risk", False),
                yield_bearing=m.get("yield_bearing", False),
                fee_rate_bps=m.get("fee_rate_bps", 200),
            )
            if result:
                logger.info(f"  Order placed: {result}")
            else:
                logger.error(f"  Order FAILED")
                return

        self._active_markets.add(mid)
        self._trade_count += 1

        # DB 로깅 (main.py에서 콜백 주입된 경우)
        if self._on_trade_open:
            try:
                cost = shares * entry_price
                self._on_trade_open({
                    "market_id": str(mid),
                    "market_question": m.get("question", "")[:100],
                    "side": side,
                    "entry_price": round(entry_price, 4),
                    "size": round(cost, 2),
                    "signal_values": {
                        "strike_price": round(opp["strike_price"], 2),
                        "current_price": round(opp["current_price"], 2),
                        "dist_pct": round(opp["dist_pct"], 4),
                        "snipe_prob": round(opp["snipe_prob"], 4),
                        "source": "predict.fun",
                        "asset": m["asset"],
                    },
                    "model_prob": round(opp["snipe_prob"], 4),
                    "market_prob": round(entry_price, 4),
                    "edge": round(opp["edge"], 4),
                    "kelly_fraction": 0,
                    "expiry_time": str(int(m.get("expiry_time", 0))),
                    "strategy_name": "predict_snipe",
                    "mode": "paper" if self.paper else "live",
                    "market_group": f"predict_{m['asset'].lower()}",
                    "asset_symbol": m["asset"],
                    "token_id": str(opp["token_id"]),
                })
            except Exception as e:
                # exc_info captures full traceback so kwarg mismatches (TypeError),
                # DB schema drift, or DataLogger internal errors are visible.
                logger.error(f"[PREDICT-DB] log_trade callback failed: {e}", exc_info=True)

        # JSON 보조 로그 (항상 기록)
        self._log_trade_json(opp)

        # Signal bridge에도 발행 (perp-dex 연동)
        self._publish_signal(opp)

    def _publish_signal(self, opp: dict):
        """signal_bridge.json에 시그널 발행 (polymarket 봇과 공유)"""
        try:
            bridge_path = Path(__file__).parent / "signal_bridge.json"
            existing = {}
            if bridge_path.exists():
                existing = json.loads(bridge_path.read_text(encoding="utf-8"))

            active = existing.get("active_signals", [])
            now = time.time()

            # 만료된 시그널 제거
            active = [s for s in active if now - s["timestamp"] < s.get("window_duration", 15) * 60]

            # 새 시그널 추가
            m = opp["market"]
            direction = "long" if opp["side"] == "UP" else "short"
            signal = {
                "timestamp": now,
                "asset": m["asset"],
                "direction": direction,
                "blended_prob": round(opp["snipe_prob"], 4),
                "rule_prob": round(opp["snipe_prob"], 4),
                "ml_prob": 0.0,
                "market_price": round(opp["entry_price"], 4),
                "edge": round(opp["edge"], 4),
                "entry_price_binance": round(opp["current_price"], 2),
                "minutes_to_expiry": round(opp["minutes_left"], 1),
                "window_duration": 15.0,
                "confidence_tier": "high" if opp["snipe_prob"] >= 0.75 else "medium",
                "rsi": 0,
                "bb_position": 0,
                "trend_strength": 0,
                "vol_regime": "medium",
                "signal_id": f"predict_{m['asset']}_{int(now)}",
                "mode": "paper" if self.paper else "live",
                "consumed": False,
                "consumed_at": 0.0,
                "source": "predict.fun",
            }

            # 같은 asset의 이전 시그널 제거
            active = [s for s in active if s.get("asset") != m["asset"] or s.get("source") != "predict.fun"]
            active.append(signal)

            data = {
                "last_updated": now,
                "publisher": "predict-sniper",
                "active_signals": active,
            }

            tmp = bridge_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(bridge_path)

        except Exception as e:
            logger.warning(f"Signal publish failed: {e}")

    def _log_trade_json(self, opp: dict):
        """predict_trades.json에 거래 기록 (DB 백업용)"""
        try:
            trades = []
            if self._log_path.exists():
                try:
                    trades = json.loads(self._log_path.read_text(encoding="utf-8"))
                except Exception:
                    trades = []
            m = opp["market"]
            trades.append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "market_id": m["id"],
                "asset": m["asset"],
                "side": opp["side"],
                "entry_price": round(opp["entry_price"], 4),
                "cost": round(SNIPE_BET_SIZE * opp["entry_price"], 2),
                "edge": round(opp["edge"], 4),
                "snipe_prob": round(opp["snipe_prob"], 4),
                "strike": round(opp["strike_price"], 2),
                "current_price": round(opp["current_price"], 2),
                "minutes_left": round(opp["minutes_left"], 1),
                "paper": self.paper,
                "result": "pending",
            })
            # Keep last 500 entries
            if len(trades) > 500:
                trades = trades[-500:]
            self._log_path.write_text(json.dumps(trades, indent=2), encoding="utf-8")
        except Exception as e:
            logger.debug(f"JSON trade log failed: {e}")

    def _update_trade_result(self, market_id: int, won: bool):
        """predict_trades.json에서 해당 마켓의 결과를 업데이트"""
        try:
            if not self._log_path.exists():
                return
            trades = json.loads(self._log_path.read_text(encoding="utf-8"))
            updated = False
            for t in reversed(trades):
                if t.get("market_id") == market_id and t.get("result") == "pending":
                    t["result"] = "win" if won else "loss"
                    t["resolved_at"] = datetime.now(timezone.utc).isoformat()
                    updated = True
                    break
            if updated:
                self._log_path.write_text(json.dumps(trades, indent=2), encoding="utf-8")
        except Exception as e:
            logger.debug(f"JSON result update failed: {e}")

    async def claim_resolved(self):
        """resolved된 winning 포지션 자동 claim"""
        try:
            # 가스비 부족 시 BNB 잔고 재확인 후 스킵
            if self._gas_insufficient:
                try:
                    from web3 import Web3
                    w3 = Web3(Web3.HTTPProvider("https://bsc-dataseed.binance.org/"))
                    bal = w3.eth.get_balance(Web3.to_checksum_address(self.client.signer_address))
                    bnb = bal / 1e18
                    if bnb < 0.001:
                        logger.warning(f"[CLAIM] Skipping — signer BNB too low ({bnb:.6f}). Need gas top-up.")
                        return 0
                    logger.info(f"[CLAIM] Gas available again ({bnb:.4f} BNB), resuming claims")
                    self._gas_insufficient = False
                except Exception:
                    logger.warning("[CLAIM] Cannot check BNB balance, skipping claim cycle")
                    return 0

            data = await self.client._request("GET", "/positions", require_jwt=True)
            if not data or not data.get("success"):
                return 0

            claimed = 0
            for p in data.get("data", []):
                market_obj = p.get("market", {}) or {}
                outcome_obj = p.get("outcome", {}) or {}

                market_id = market_obj.get("id")
                market_status = market_obj.get("status", "")
                condition_id = market_obj.get("conditionId", "")
                outcome_status = outcome_obj.get("status", "")
                outcome_name = outcome_obj.get("name", "")

                amount_wei = p.get("amount", "0")
                try:
                    shares = float(amount_wei) / 1e18
                except Exception:
                    shares = 0

                if shares <= 0 or market_status != "RESOLVED" or outcome_status != "WON":
                    continue
                if not condition_id:
                    continue

                # Determine index_set
                upper = outcome_name.upper()
                if upper in ("YES", "Y", "UP"):
                    index_set = 1
                elif upper in ("NO", "N", "DOWN"):
                    index_set = 2
                else:
                    index_set = 1

                neg_risk = market_obj.get("negRisk", market_obj.get("isNegRisk", False))
                yield_bearing = market_obj.get("yieldBearing", market_obj.get("isYieldBearing", True))
                amount = int(amount_wei) if neg_risk else None

                logger.info(f"[CLAIM] Claiming #{market_id} {outcome_name} {shares:.2f}sh | {market_obj.get('question','')[:40]}")

                try:
                    result = await self.client.order_builder.redeem_positions_async(
                        condition_id=condition_id,
                        index_set=index_set,
                        amount=amount,
                        is_neg_risk=neg_risk,
                        is_yield_bearing=yield_bearing,
                    )
                    if result.success:
                        tx_hash = "unknown"
                        if result.receipt:
                            raw = result.receipt.get("transactionHash", result.receipt.get("hash"))
                            if raw:
                                tx_hash = ("0x" + raw.hex()) if isinstance(raw, bytes) else str(raw)
                            status = result.receipt.get("status")
                            if status is not None and int(status) == 1:
                                logger.info(f"[CLAIM] SUCCESS ${shares:.2f} TX={tx_hash}")
                                self._update_trade_result(market_id, won=True)
                                # DB callback for close (don't swallow — predict_snipe DB
                                # row would silently stay 'open' forever if this fails)
                                if self._on_trade_close:
                                    try:
                                        self._on_trade_close(market_id, True, 0.0, shares)
                                    except Exception as cb_exc:
                                        logger.error(
                                            f"[CLAIM] DB close callback failed for market={market_id}: {cb_exc}",
                                            exc_info=True,
                                        )
                                claimed += 1
                            else:
                                logger.error(f"[CLAIM] TX reverted: {tx_hash}")
                    else:
                        cause = str(getattr(result, 'cause', result))
                        if "insufficient funds for gas" in cause:
                            logger.warning(f"[CLAIM] Insufficient gas — pausing claims until BNB refilled")
                            self._gas_insufficient = True
                            break
                        logger.error(f"[CLAIM] Failed: {cause}")
                except Exception as e:
                    err_str = str(e)
                    if "insufficient funds for gas" in err_str:
                        logger.warning(f"[CLAIM] Insufficient gas — pausing claims until BNB refilled")
                        self._gas_insufficient = True
                        break
                    logger.error(f"[CLAIM] Error: {e}")

                await asyncio.sleep(2)

            if claimed > 0:
                logger.info(f"[CLAIM] Total claimed: {claimed} positions")
            return claimed

        except Exception as e:
            logger.error(f"[CLAIM] Check error: {e}")
            return 0

    def get_stats(self) -> dict:
        return {
            "trades": self._trade_count,
            "wins": self._wins,
            "pnl": self._total_pnl,
            "active_markets": len(self._active_markets),
        }


# ──────────────────────────────────────────
# Main
# ──────────────────────────────────────────

async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Predict.fun Expiry Sniper")
    parser.add_argument("--live", action="store_true", help="Live loop mode")
    parser.add_argument("--paper", action="store_true", help="Paper mode (no real orders)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    load_dotenv(Path(__file__).parent / ".env")

    # Initialize predict client
    predict = PredictClient(
        api_key=os.getenv("PREDICT_API_KEY", ""),
        private_key=os.getenv("PREDICT_PRIVATE_KEY", ""),
        predict_account=os.getenv("PREDICT_ACCOUNT", ""),
    )
    await predict.connect()

    fetcher = PredictMarketFetcher(api_key=os.getenv("PREDICT_API_KEY", ""))
    paper = args.paper or not args.live
    sniper = PredictSniper(predict, fetcher, paper=paper)

    mode_label = "PAPER" if paper else "LIVE"
    logger.info(f"Predict.fun Sniper started in {mode_label} mode")

    if args.live and not args.paper:
        logger.info("*** LIVE MODE — Real money orders will be placed ***")

    last_claim_time = 0

    try:
        while True:
            try:
                await sniper.scan_and_snipe()
                stats = sniper.get_stats()
                if stats["trades"] > 0:
                    logger.info(f"Stats: {stats['trades']} trades, active={stats['active_markets']}")

                # Auto-claim every CLAIM_INTERVAL
                now = time.time()
                if not paper and now - last_claim_time >= CLAIM_INTERVAL:
                    last_claim_time = now
                    await sniper.claim_resolved()

            except Exception as e:
                logger.error(f"Scan error: {e}", exc_info=True)

            if not args.live:
                break

            await asyncio.sleep(SCAN_INTERVAL)
    finally:
        await predict.close()


if __name__ == "__main__":
    asyncio.run(main())
