"""Weather market strategy for Polymarket.

Fetches NOAA/NWS forecasts and compares them against Polymarket weather market
implied probabilities.  When forecast probability diverges from market price by
more than a configurable threshold the module produces edge-opportunities in
the same dict format the rest of the bot already consumes.

Designed to be non-invasive: import and call from the main signal loop, or run
standalone via ``python weather_strategy.py`` for paper-testing.
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import httpx

logger = logging.getLogger("polybot.weather")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class WeatherForecast:
    """Parsed hourly forecast from NWS."""
    city: str
    date: str            # YYYY-MM-DD
    hour: int            # 0-23 local
    temp_f: float        # Fahrenheit
    temp_c: float        # Celsius
    precip_pct: float    # 0-100
    wind_speed_mph: float
    short_desc: str
    forecast_time: float  # epoch when forecast was fetched
    source: str = "NWS"


@dataclass
class WeatherMarket:
    """Single Polymarket weather market."""
    market_id: str
    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    end_date: str
    expiry_ts: float
    city: str
    metric: str          # "high_temp", "low_temp", "precip"
    threshold: float     # e.g. 75 (°F)
    direction: str       # "above", "below", "yes", "no"
    liquidity: float
    active: bool


@dataclass
class WeatherConfig:
    """Weather-specific config (injected from main Config)."""
    enabled: bool = True
    scan_interval_sec: int = 120
    forecast_refresh_sec: int = 600
    min_edge_threshold: float = 0.15
    min_liquidity: float = 50.0
    max_bet_size: float = 3.0
    min_bet_size: float = 0.5
    kelly_fraction: float = 0.08
    max_open_weather_positions: int = 5
    target_cities: list[str] = field(default_factory=lambda: [
        "new-york", "london", "chicago", "seoul", "hong-kong",
    ])
    # NWS grid-point mapping  (city -> (office, gridX, gridY))
    nws_grid_points: dict[str, tuple[str, int, int]] = field(default_factory=lambda: {
        "new-york":      ("OKX", 33, 37),
        "chicago":       ("LOT", 65, 76),
        "los-angeles":   ("LOX", 154, 44),
        "miami":         ("MFL", 110, 50),
        "dallas":        ("FWD", 87, 108),
        "seattle":       ("SEW", 124, 67),
        "denver":        ("BOU", 62, 60),
        "atlanta":       ("FFC", 51, 87),
        "austin":        ("EWX", 156, 91),
        "houston":       ("HGX", 65, 97),
        "san-francisco": ("MTR", 85, 105),
    })
    # OpenMeteo fallback for non-US cities
    openmeteo_coords: dict[str, tuple[float, float]] = field(default_factory=lambda: {
        "london":       (51.5074, -0.1278),
        "seoul":        (37.5665, 126.978),
        "hong-kong":    (22.3193, 114.1694),
        "tokyo":        (35.6762, 139.6503),
        "sydney":       (-33.8688, 151.2093),
        "toronto":      (43.6532, -79.3832),
        "singapore":    (1.3521, 103.8198),
        "taipei":       (25.0330, 121.5654),
        "beijing":      (39.9042, 116.4074),
        "shanghai":     (31.2304, 121.4737),
        "shenzhen":     (22.5431, 114.0579),
        "chongqing":    (29.4316, 106.9123),
        "wuhan":        (30.5928, 114.3055),
        "chengdu":      (30.5728, 104.0668),
        "paris":        (48.8566, 2.3522),
        "madrid":       (40.4168, -3.7038),
        "milan":        (45.4642, 9.1900),
        "munich":       (48.1351, 11.5820),
        "warsaw":       (52.2297, 21.0122),
        "ankara":       (39.9334, 32.8597),
        "buenos-aires": (-34.6037, -58.3816),
        "sao-paulo":    (-23.5505, -46.6333),
        "lucknow":      (26.8467, 80.9462),
        "wellington":   (-41.2866, 174.7756),
        "tel-aviv":     (32.0853, 34.7818),
    })


# ---------------------------------------------------------------------------
# Forecast fetcher
# ---------------------------------------------------------------------------

class ForecastFetcher:
    """Fetches hourly forecasts from NWS (US) and Open-Meteo (international)."""

    def __init__(self, weather_cfg: WeatherConfig):
        self.cfg = weather_cfg
        self._client = httpx.AsyncClient(timeout=15.0, headers={
            "User-Agent": "PolymarketWeatherBot/1.0 (contact: bot@example.com)",
        })
        self._cache: dict[str, tuple[float, list[WeatherForecast]]] = {}

    async def close(self):
        await self._client.aclose()

    async def get_forecasts(self, city: str) -> list[WeatherForecast]:
        """Return cached or fresh hourly forecasts for *city*."""
        now = time.time()
        cached = self._cache.get(city)
        if cached and now - cached[0] < self.cfg.forecast_refresh_sec:
            return cached[1]

        forecasts: list[WeatherForecast] = []
        if city in self.cfg.nws_grid_points:
            forecasts = await self._fetch_nws(city)
        elif city in self.cfg.openmeteo_coords:
            forecasts = await self._fetch_openmeteo(city)

        if forecasts:
            self._cache[city] = (now, forecasts)
        return forecasts

    # -- NWS (US cities) ---------------------------------------------------

    async def _fetch_nws(self, city: str) -> list[WeatherForecast]:
        office, gx, gy = self.cfg.nws_grid_points[city]
        url = f"https://api.weather.gov/gridpoints/{office}/{gx},{gy}/forecast/hourly"
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
            data = resp.json()
            periods = data.get("properties", {}).get("periods", [])
            forecasts = []
            for p in periods[:48]:  # next 48 hours
                dt = datetime.fromisoformat(p["startTime"])
                temp_f = float(p.get("temperature", 0))
                temp_c = (temp_f - 32) * 5 / 9
                precip_raw = p.get("probabilityOfPrecipitation", {})
                precip_pct = float(precip_raw.get("value", 0) or 0)
                wind_raw = p.get("windSpeed", "0 mph")
                wind_mph = float(re.search(r"(\d+)", str(wind_raw)).group(1)) if re.search(r"(\d+)", str(wind_raw)) else 0.0
                forecasts.append(WeatherForecast(
                    city=city,
                    date=dt.strftime("%Y-%m-%d"),
                    hour=dt.hour,
                    temp_f=temp_f,
                    temp_c=temp_c,
                    precip_pct=precip_pct,
                    wind_speed_mph=wind_mph,
                    short_desc=p.get("shortForecast", ""),
                    forecast_time=time.time(),
                    source="NWS",
                ))
            logger.info(f"[WEATHER] NWS fetched {len(forecasts)} hours for {city}")
            return forecasts
        except Exception as exc:
            logger.warning(f"[WEATHER] NWS fetch failed for {city}: {exc}")
            return []

    # -- Open-Meteo (international) -----------------------------------------

    async def _fetch_openmeteo(self, city: str) -> list[WeatherForecast]:
        lat, lon = self.cfg.openmeteo_coords[city]
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&hourly=temperature_2m,precipitation_probability,wind_speed_10m"
            f"&temperature_unit=fahrenheit&wind_speed_unit=mph"
            f"&forecast_days=2"
        )
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
            data = resp.json()
            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            temps = hourly.get("temperature_2m", [])
            precips = hourly.get("precipitation_probability", [])
            winds = hourly.get("wind_speed_10m", [])
            forecasts = []
            for i, t in enumerate(times):
                dt = datetime.fromisoformat(t)
                temp_f = float(temps[i]) if i < len(temps) else 0
                temp_c = (temp_f - 32) * 5 / 9
                forecasts.append(WeatherForecast(
                    city=city,
                    date=dt.strftime("%Y-%m-%d"),
                    hour=dt.hour,
                    temp_f=temp_f,
                    temp_c=temp_c,
                    precip_pct=float(precips[i]) if i < len(precips) else 0,
                    wind_speed_mph=float(winds[i]) if i < len(winds) else 0,
                    short_desc="",
                    forecast_time=time.time(),
                    source="OpenMeteo",
                ))
            logger.info(f"[WEATHER] OpenMeteo fetched {len(forecasts)} hours for {city}")
            return forecasts
        except Exception as exc:
            logger.warning(f"[WEATHER] OpenMeteo fetch failed for {city}: {exc}")
            return []


# ---------------------------------------------------------------------------
# Market scanner (weather-specific)
# ---------------------------------------------------------------------------

class WeatherMarketScanner:
    """Discovers weather prediction markets on Polymarket via gamma-api."""

    def __init__(self, weather_cfg: WeatherConfig):
        self.cfg = weather_cfg
        self._client = httpx.AsyncClient(
            base_url="https://gamma-api.polymarket.com",
            timeout=15.0,
        )
        self._cache: list[WeatherMarket] = []
        self._last_fetch: float = 0

    async def close(self):
        await self._client.aclose()

    async def fetch_weather_markets(self) -> list[WeatherMarket]:
        now = time.time()
        if self._cache and now - self._last_fetch < 60:
            return self._cache

        markets: list[WeatherMarket] = []
        try:
            # gamma-api: tag_slug=temperature returns daily temperature events
            resp = await self._client.get("/events", params={
                "tag_slug": "temperature",
                "closed": "false",
                "limit": 50,
            })
            if resp.status_code == 200:
                events = resp.json() if isinstance(resp.json(), list) else [resp.json()]
                for event in events:
                    # Each event = one city+date, with ~11 temperature range sub-markets
                    event_title = event.get("title", "")
                    event_end = event.get("endDate", "")
                    event_liq = float(event.get("liquidity", 0) or 0)

                    for raw in event.get("markets", []):
                        # Skip closed sub-markets
                        if raw.get("closed", False):
                            continue
                        # Inject event-level info for parsing
                        raw["_event_title"] = event_title
                        raw["_event_end"] = event_end
                        raw["_event_liq"] = event_liq
                        parsed = self._parse_weather_market(raw)
                        if parsed:
                            markets.append(parsed)
        except Exception as exc:
            logger.warning(f"[WEATHER] Market fetch error: {exc}")

        if markets:
            self._cache = markets
            self._last_fetch = now
            logger.info(f"[WEATHER] Found {len(markets)} active weather markets across {len({m.city for m in markets})} cities")
        else:
            self._last_fetch = now
        return markets

    def _parse_weather_market(self, raw: dict) -> Optional[WeatherMarket]:
        """Parse a raw market dict into WeatherMarket, extracting city/threshold.

        Question format: "Will the highest temperature in Seoul be 13°C on March 25?"
        or "Will the highest temperature in Toronto be -4°C or below on March 24?"
        """
        try:
            question = str(raw.get("question", "")).strip()
            if not question:
                return None

            # Extract city — try event title first, then question
            event_title = raw.get("_event_title", "")
            city = self._extract_city(event_title) if event_title else None
            if not city:
                city = self._extract_city(question)
            if not city:
                return None

            # Extract metric and threshold
            metric, threshold, direction = self._extract_metric(question)
            if metric is None:
                return None

            clob_ids = raw.get("clobTokenIds", [])
            if isinstance(clob_ids, str):
                clob_ids = json.loads(clob_ids)
            if len(clob_ids) < 2:
                return None

            prices = raw.get("outcomePrices", [0.5, 0.5])
            if isinstance(prices, str):
                prices = json.loads(prices)

            outcomes = raw.get("outcomes", ["Yes", "No"])
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)

            yes_idx, no_idx = 0, 1
            for i, o in enumerate(outcomes):
                label = str(o).lower()
                if label == "yes":
                    yes_idx = i
                elif label == "no":
                    no_idx = i

            end_date = raw.get("endDate", "") or raw.get("_event_end", "")
            expiry_ts = 0
            if end_date:
                try:
                    expiry_ts = datetime.fromisoformat(
                        end_date.replace("Z", "+00:00")
                    ).timestamp()
                except Exception:
                    pass

            market_id = str(raw.get("conditionId") or raw.get("id") or "")
            if not market_id:
                return None

            # Use event-level liquidity if sub-market liquidity is 0
            liq = float(raw.get("liquidity", 0) or 0)
            if liq == 0:
                liq = float(raw.get("_event_liq", 0) or 0)

            return WeatherMarket(
                market_id=market_id,
                condition_id=str(raw.get("conditionId") or market_id),
                question=question,
                yes_token_id=str(clob_ids[yes_idx]),
                no_token_id=str(clob_ids[no_idx]),
                yes_price=float(prices[yes_idx]),
                no_price=float(prices[no_idx]),
                end_date=end_date,
                expiry_ts=expiry_ts,
                city=city,
                metric=metric,
                threshold=threshold,
                direction=direction,
                liquidity=liq,
                active=not raw.get("closed", False),
            )
        except Exception as exc:
            logger.debug(f"[WEATHER] Parse error: {exc}")
            return None

    # -- Question parsing helpers -------------------------------------------

    _CITY_PATTERNS = {
        "new york":      "new-york",
        "nyc":           "new-york",
        "chicago":       "chicago",
        "london":        "london",
        "seoul":         "seoul",
        "hong kong":     "hong-kong",
        "tokyo":         "tokyo",
        "los angeles":   "los-angeles",
        "miami":         "miami",
        "dallas":        "dallas",
        "seattle":       "seattle",
        "denver":        "denver",
        "atlanta":       "atlanta",
        "sydney":        "sydney",
        "toronto":       "toronto",
        "austin":        "austin",
        "houston":       "houston",
        "san francisco": "san-francisco",
        "tel aviv":      "tel-aviv",
        "singapore":     "singapore",
        "taipei":        "taipei",
        "beijing":       "beijing",
        "shanghai":      "shanghai",
        "shenzhen":      "shenzhen",
        "chongqing":     "chongqing",
        "wuhan":         "wuhan",
        "chengdu":       "chengdu",
        "paris":         "paris",
        "madrid":        "madrid",
        "milan":         "milan",
        "munich":        "munich",
        "warsaw":        "warsaw",
        "ankara":        "ankara",
        "buenos aires":  "buenos-aires",
        "sao paulo":     "sao-paulo",
        "lucknow":       "lucknow",
        "wellington":    "wellington",
    }

    def _extract_city(self, question: str) -> Optional[str]:
        q = question.lower()
        for pattern, city_key in self._CITY_PATTERNS.items():
            if pattern in q:
                return city_key
        return None

    def _extract_metric(self, question: str) -> tuple[Optional[str], float, str]:
        """Return (metric, threshold_C_as_F, direction) or (None, 0, '').

        Polymarket weather market question formats:
          "Will the highest temperature in Seoul be 13°C on March 25?"
          "Will the highest temperature in Toronto be -4°C or below on March 24?"
          "Will the highest temperature in NYC be 5°C or higher on March 25?"
          "Will the high temperature in NYC exceed 75°F?"
        """
        q = question.lower()

        # Determine metric type
        if any(w in q for w in ["highest", "high", "max"]):
            metric = "high_temp"
        elif any(w in q for w in ["lowest", "low", "min"]):
            metric = "low_temp"
        else:
            metric = "high_temp"  # default

        # Celsius pattern (handles negative temps): "be 13°C" or "be -4°C or below"
        celsius_match = re.search(r'be\s+(-?\d+(?:\.\d+)?)\s*[°º˚]?\s*c', q)
        if celsius_match:
            threshold_c = float(celsius_match.group(1))
            threshold_f = threshold_c * 9 / 5 + 32
            # "or below" = this is the upper bound; "or higher" = lower bound; exact = "above"
            if "or below" in q or "or less" in q or "or lower" in q:
                direction = "below"
            elif "or higher" in q or "or above" in q or "or more" in q:
                direction = "above"
            else:
                # Exact temp like "be 13°C" → treat as "above" (resolves YES if high == threshold)
                direction = "above"
            return metric, threshold_f, direction

        # Fahrenheit pattern (handles negative temps)
        fahr_match = re.search(r'be\s+(-?\d+(?:\.\d+)?)\s*[°º˚]?\s*f', q)
        if fahr_match:
            threshold_f = float(fahr_match.group(1))
            if "or below" in q or "or less" in q or "or lower" in q:
                direction = "below"
            elif "or higher" in q or "or above" in q or "or more" in q:
                direction = "above"
            else:
                direction = "above"
            return metric, threshold_f, direction

        # Legacy patterns: "exceed 75°F", "above 80°F"
        legacy_match = re.search(
            r'(above|below|exceed|over|under|at least|less than|greater than|more than)\s+'
            r'(-?\d+(?:\.\d+)?)\s*[°º˚]?\s*([fFcC])',
            q,
        )
        if legacy_match:
            direction_word = legacy_match.group(1)
            threshold = float(legacy_match.group(2))
            unit = legacy_match.group(3).lower()
            if unit == 'c':
                threshold = threshold * 9 / 5 + 32
            direction = "above" if direction_word in ("above", "exceed", "over", "at least", "greater than", "more than") else "below"
            return metric, threshold, direction

        return None, 0, ""


# ---------------------------------------------------------------------------
# Edge calculator
# ---------------------------------------------------------------------------

class WeatherEdgeCalculator:
    """Compares weather forecasts against market prices to find mispricings."""

    def __init__(self, weather_cfg: WeatherConfig):
        self.cfg = weather_cfg

    def calculate_forecast_probability(
        self, forecasts: list[WeatherForecast], market: WeatherMarket
    ) -> Optional[float]:
        """
        Estimate the probability that the weather outcome resolves YES.

        For temperature markets: compare forecast temp distribution against
        the market threshold.
        """
        if not forecasts:
            return None

        # Filter forecasts to the market's resolution date
        target_date = market.end_date[:10] if market.end_date else None
        if not target_date:
            return None

        day_forecasts = [f for f in forecasts if f.date == target_date]
        if not day_forecasts:
            # Try tomorrow if no match
            tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
            day_forecasts = [f for f in forecasts if f.date == tomorrow]

        if not day_forecasts:
            return None

        if market.metric == "high_temp":
            # Predicted daily high = max of hourly temps
            predicted_high = max(f.temp_f for f in day_forecasts)
            # Also compute ensemble-like spread using all hourly values
            temps = sorted([f.temp_f for f in day_forecasts], reverse=True)
            # Use top-3 average as "likely high"
            avg_top3 = sum(temps[:3]) / min(3, len(temps))
            # Margin: how far is the predicted high from threshold
            margin = avg_top3 - market.threshold
            # Convert margin to probability using a sigmoid-like function
            # ±5°F maps roughly to 10%-90% probability range
            prob_yes = self._sigmoid(margin, scale=3.0)
            if market.direction == "below":
                prob_yes = 1.0 - prob_yes
            return prob_yes

        elif market.metric == "low_temp":
            predicted_low = min(f.temp_f for f in day_forecasts)
            temps = sorted([f.temp_f for f in day_forecasts])
            avg_bot3 = sum(temps[:3]) / min(3, len(temps))
            margin = avg_bot3 - market.threshold
            prob_yes = self._sigmoid(margin, scale=3.0)
            if market.direction == "below":
                prob_yes = 1.0 - prob_yes
            return prob_yes

        return None

    @staticmethod
    def _sigmoid(x: float, scale: float = 3.0) -> float:
        """Map a margin (degrees diff) to a 0-1 probability."""
        import math
        return 1.0 / (1.0 + math.exp(-x / scale))

    def find_weather_opportunities(
        self,
        markets: list[WeatherMarket],
        forecasts_by_city: dict[str, list[WeatherForecast]],
    ) -> list[dict[str, Any]]:
        """
        Return a list of opportunity dicts compatible with the main bot's format.
        """
        opportunities: list[dict[str, Any]] = []

        for market in markets:
            if not market.active:
                continue
            if market.liquidity < self.cfg.min_liquidity:
                continue
            # Skip already-resolved markets (price near 0 or 1 = no liquidity)
            if market.yes_price < 0.03 or market.yes_price > 0.97:
                continue

            forecasts = forecasts_by_city.get(market.city, [])
            if not forecasts:
                continue

            forecast_prob = self.calculate_forecast_probability(forecasts, market)
            if forecast_prob is None:
                continue

            market_prob_yes = market.yes_price
            edge_yes = forecast_prob - market_prob_yes
            edge_no = (1.0 - forecast_prob) - market.no_price

            # Skip if entry price would be below CLOB minimum (0.001)
            if market.yes_price < 0.001 and market.no_price < 0.001:
                continue

            # Pick the side with more edge
            if edge_yes > edge_no and edge_yes > self.cfg.min_edge_threshold and market.yes_price >= 0.001:
                kelly_raw = edge_yes / (1.0 - market_prob_yes) if market_prob_yes < 1 else 0
                kelly_frac = kelly_raw * self.cfg.kelly_fraction
                bet_size = max(self.cfg.min_bet_size, min(self.cfg.max_bet_size, kelly_frac * 100))
                opportunities.append({
                    "market_id": market.market_id,
                    "condition_id": market.condition_id,
                    "question": market.question,
                    "side": "YES",
                    "token_id": market.yes_token_id,
                    "model_prob": round(forecast_prob, 4),
                    "market_prob": round(market_prob_yes, 4),
                    "edge": round(edge_yes, 4),
                    "entry_price": market.yes_price,
                    "strategy": "weather_forecast",
                    "bet_size": round(bet_size, 2),
                    "city": market.city,
                    "metric": market.metric,
                    "threshold": market.threshold,
                    "forecast_source": forecasts[0].source if forecasts else "unknown",
                    "market_type": "weather",
                    "expiry_ts": market.expiry_ts,
                })
            elif edge_no > edge_yes and edge_no > self.cfg.min_edge_threshold and market.no_price >= 0.001:
                kelly_raw = edge_no / (1.0 - market.no_price) if market.no_price < 1 else 0
                kelly_frac = kelly_raw * self.cfg.kelly_fraction
                bet_size = max(self.cfg.min_bet_size, min(self.cfg.max_bet_size, kelly_frac * 100))
                opportunities.append({
                    "market_id": market.market_id,
                    "condition_id": market.condition_id,
                    "question": market.question,
                    "side": "NO",
                    "token_id": market.no_token_id,
                    "model_prob": round(1.0 - forecast_prob, 4),
                    "market_prob": round(market.no_price, 4),
                    "edge": round(edge_no, 4),
                    "entry_price": market.no_price,
                    "strategy": "weather_forecast",
                    "bet_size": round(bet_size, 2),
                    "city": market.city,
                    "metric": market.metric,
                    "threshold": market.threshold,
                    "forecast_source": forecasts[0].source if forecasts else "unknown",
                    "market_type": "weather",
                    "expiry_ts": market.expiry_ts,
                })

        # Sort by edge descending
        opportunities.sort(key=lambda x: -x["edge"])
        return opportunities


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

class WeatherStrategy:
    """
    Top-level orchestrator.  Use from main.py:

        weather = WeatherStrategy(weather_cfg)
        opps = await weather.scan()       # returns list[dict]
        await weather.close()
    """

    def __init__(self, weather_cfg: Optional[WeatherConfig] = None):
        self.cfg = weather_cfg or WeatherConfig()
        self.fetcher = ForecastFetcher(self.cfg)
        self.market_scanner = WeatherMarketScanner(self.cfg)
        self.edge_calc = WeatherEdgeCalculator(self.cfg)
        self._last_scan_ts: float = 0
        self._last_opportunities: list[dict] = []

    async def close(self):
        await self.fetcher.close()
        await self.market_scanner.close()

    async def scan(self) -> list[dict[str, Any]]:
        """Full scan cycle: fetch forecasts + markets → find edge → return opps."""
        now = time.time()
        if now - self._last_scan_ts < self.cfg.scan_interval_sec:
            return self._last_opportunities

        # 1) Fetch weather markets
        markets = await self.market_scanner.fetch_weather_markets()
        if not markets:
            logger.info("[WEATHER] No active weather markets found")
            self._last_scan_ts = now
            return []

        # 2) Determine which cities need forecasts
        needed_cities = {m.city for m in markets}

        # 3) Fetch forecasts for those cities
        forecasts_by_city: dict[str, list[WeatherForecast]] = {}
        for city in needed_cities:
            forecasts = await self.fetcher.get_forecasts(city)
            if forecasts:
                forecasts_by_city[city] = forecasts

        # 4) Calculate edge
        opportunities = self.edge_calc.find_weather_opportunities(
            markets, forecasts_by_city
        )

        self._last_scan_ts = now
        self._last_opportunities = opportunities

        if opportunities:
            logger.info(
                f"[WEATHER] {len(opportunities)} opportunities found | "
                f"top edge: {opportunities[0]['edge']:.1%} on {opportunities[0]['question'][:60]}"
            )
        else:
            logger.info(f"[WEATHER] Scanned {len(markets)} markets, 0 edge opportunities")

        return opportunities

    async def evaluate_position(
        self,
        city: str,
        metric: str,
        threshold: float,
        direction: str,
        end_date: str,
        entry_price: float,
        entry_forecast_prob: float,
        expiry_ts: float = 0,
        edge_buffer: float = 0.05,
        forecast_drop_pct: float = 0.30,
        urgent_hours: float = 2.0,
    ) -> dict:
        """Re-evaluate a held weather position against latest forecast.

        Returns dict with:
            action: "hold" or "sell"
            reason: str
            current_forecast_prob: float
        """
        result = {"action": "hold", "reason": "", "current_forecast_prob": 0.0}

        # Fetch latest forecast
        forecasts = await self.fetcher.get_forecasts(city)
        if not forecasts:
            result["reason"] = "no_forecast_data"
            return result

        # Build a minimal WeatherMarket stub for the edge calculator
        stub = WeatherMarket(
            market_id="", condition_id="", question="",
            yes_token_id="", no_token_id="",
            yes_price=0.0, no_price=0.0,
            end_date=end_date, expiry_ts=expiry_ts,
            city=city, metric=metric, threshold=threshold,
            direction=direction, liquidity=0.0, active=True,
        )
        forecast_prob = self.edge_calc.calculate_forecast_probability(forecasts, stub)
        if forecast_prob is None:
            result["reason"] = "forecast_calc_failed"
            return result

        result["current_forecast_prob"] = forecast_prob

        # Condition A: Edge evaporation — forecast no longer supports the position
        if forecast_prob < entry_price + edge_buffer:
            result["action"] = "sell"
            result["reason"] = f"edge_gone: forecast={forecast_prob:.1%} < entry+buffer={entry_price + edge_buffer:.1%}"
            return result

        # Condition B: Take profit on deterioration — forecast dropped significantly but still in profit
        if entry_forecast_prob > 0 and forecast_prob < entry_forecast_prob * (1.0 - forecast_drop_pct):
            result["action"] = "sell"
            result["reason"] = f"forecast_drop: {entry_forecast_prob:.1%}->{forecast_prob:.1%} ({(1 - forecast_prob / entry_forecast_prob) * 100:.0f}% drop)"
            return result

        # Condition C: Time urgency — close to expiry and uncertain
        if expiry_ts > 0:
            hours_left = (expiry_ts - time.time()) / 3600
            if 0 < hours_left < urgent_hours and forecast_prob < 0.50:
                result["action"] = "sell"
                result["reason"] = f"urgent: {hours_left:.1f}h left, forecast={forecast_prob:.1%}"
                return result

        result["reason"] = f"hold: forecast={forecast_prob:.1%} still strong"
        return result

    def format_status(self) -> str:
        """One-line status string for dashboard."""
        if not self._last_opportunities:
            return "weather: idle"
        top = self._last_opportunities[0]
        return (
            f"weather: {len(self._last_opportunities)} opps | "
            f"top={top['edge']:.1%} edge {top['city']} {top['question'][:40]}"
        )


# ---------------------------------------------------------------------------
# Standalone paper test
# ---------------------------------------------------------------------------

async def _paper_test():
    """Run a single scan cycle and print results."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = WeatherConfig()
    strategy = WeatherStrategy(cfg)
    try:
        print("=" * 70)
        print("POLYMARKET WEATHER STRATEGY — PAPER TEST")
        print("=" * 70)

        # Fetch forecasts for all configured cities
        print("\n[1/3] Fetching weather forecasts...")
        for city in cfg.target_cities:
            forecasts = await strategy.fetcher.get_forecasts(city)
            if forecasts:
                # Show daily high/low summary
                by_date: dict[str, list[float]] = {}
                for f in forecasts:
                    by_date.setdefault(f.date, []).append(f.temp_f)
                for date, temps in sorted(by_date.items()):
                    print(f"  {city:>12} {date}  high={max(temps):.0f}°F  low={min(temps):.0f}°F  (src={forecasts[0].source})")
            else:
                print(f"  {city:>12}  ❌ no forecast data")

        # Fetch weather markets
        print("\n[2/3] Scanning Polymarket weather markets...")
        markets = await strategy.market_scanner.fetch_weather_markets()
        print(f"  Found {len(markets)} active weather markets")
        for m in markets[:10]:
            print(f"  • {m.question[:60]}  yes={m.yes_price:.2f}  no={m.no_price:.2f}  liq=${m.liquidity:.0f}")

        # Find opportunities
        print("\n[3/3] Calculating edge opportunities...")
        opps = await strategy.scan()
        if opps:
            print(f"\n  🎯 {len(opps)} opportunities found:\n")
            for i, opp in enumerate(opps[:10], 1):
                print(f"  #{i}  {opp['question'][:55]}")
                print(f"      side={opp['side']}  forecast={opp['model_prob']:.1%}  market={opp['market_prob']:.1%}  edge={opp['edge']:.1%}")
                print(f"      city={opp['city']}  bet=${opp['bet_size']:.2f}  source={opp['forecast_source']}")
                print()
        else:
            print("\n  No edge opportunities at this time.")
            print("  This is normal — check back when markets have wider mispricings.")

        print("=" * 70)
        print(f"Status: {strategy.format_status()}")
        print("=" * 70)

    finally:
        await strategy.close()


if __name__ == "__main__":
    asyncio.run(_paper_test())
