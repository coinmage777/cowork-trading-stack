"""Weather strategy backtest against resolved Polymarket temperature markets."""
import asyncio
import json
import math
import re
import time
from datetime import datetime, timezone, timedelta

import httpx

# Config
KELLY_FRACTION = 0.08
MAX_BET = 3.0
MIN_BET = 0.5
MIN_EDGE = 0.15
TAKER_FEE = 0.02
SIGMOID_SCALE = 3.0

CITY_COORDS = {
    "new-york": (40.7128, -74.0060), "chicago": (41.8781, -87.6298),
    "los-angeles": (34.0522, -118.2437), "miami": (25.7617, -80.1918),
    "dallas": (32.7767, -96.7970), "seattle": (47.6062, -122.3321),
    "denver": (39.7392, -104.9903), "atlanta": (33.7490, -84.3880),
    "austin": (30.2672, -97.7431), "houston": (29.7604, -95.3698),
    "san-francisco": (37.7749, -122.4194), "london": (51.5074, -0.1278),
    "seoul": (37.5665, 126.978), "hong-kong": (22.3193, 114.1694),
    "tokyo": (35.6762, 139.6503), "toronto": (43.6532, -79.3832),
    "singapore": (1.3521, 103.8198), "taipei": (25.0330, 121.5654),
    "beijing": (39.9042, 116.4074), "shanghai": (31.2304, 121.4737),
    "shenzhen": (22.5431, 114.0579), "chongqing": (29.4316, 106.9123),
    "wuhan": (30.5928, 114.3055), "chengdu": (30.5728, 104.0668),
    "paris": (48.8566, 2.3522), "madrid": (40.4168, -3.7038),
    "milan": (45.4642, 9.19), "munich": (48.1351, 11.582),
    "warsaw": (52.2297, 21.0122), "ankara": (39.9334, 32.8597),
    "buenos-aires": (-34.6037, -58.3816), "sao-paulo": (-23.5505, -46.6333),
    "lucknow": (26.8467, 80.9462), "wellington": (-41.2866, 174.7756),
    "tel-aviv": (32.0853, 34.7818), "sydney": (-33.8688, 151.2093),
}

CITY_PATTERNS = {
    "new york": "new-york", "nyc": "new-york", "chicago": "chicago",
    "london": "london", "seoul": "seoul", "hong kong": "hong-kong",
    "tokyo": "tokyo", "los angeles": "los-angeles", "miami": "miami",
    "dallas": "dallas", "seattle": "seattle", "denver": "denver",
    "atlanta": "atlanta", "sydney": "sydney", "toronto": "toronto",
    "austin": "austin", "houston": "houston", "san francisco": "san-francisco",
    "tel aviv": "tel-aviv", "singapore": "singapore", "taipei": "taipei",
    "beijing": "beijing", "shanghai": "shanghai", "shenzhen": "shenzhen",
    "chongqing": "chongqing", "wuhan": "wuhan", "chengdu": "chengdu",
    "paris": "paris", "madrid": "madrid", "milan": "milan", "munich": "munich",
    "warsaw": "warsaw", "ankara": "ankara", "buenos aires": "buenos-aires",
    "sao paulo": "sao-paulo", "lucknow": "lucknow", "wellington": "wellington",
    "new york city": "new-york",
}

_forecast_cache = {}


async def fetch_resolved_events(limit=200):
    async with httpx.AsyncClient(timeout=15) as client:
        all_events = []
        for offset in range(0, limit, 50):
            resp = await client.get(
                "https://gamma-api.polymarket.com/events",
                params={
                    "tag_slug": "temperature",
                    "closed": "true",
                    "limit": 50,
                    "offset": offset,
                    "order": "endDate",
                    "ascending": "false",
                },
            )
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not batch:
                break
            all_events.extend(batch)
            await asyncio.sleep(0.3)
        return all_events


async def fetch_forecast(city, target_date):
    cache_key = f"{city}:{target_date}"
    if cache_key in _forecast_cache:
        return _forecast_cache[cache_key]

    coords = CITY_COORDS.get(city)
    if not coords:
        return []

    lat, lon = coords
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "hourly": "temperature_2m",
                    "temperature_unit": "fahrenheit",
                    "start_date": target_date,
                    "end_date": target_date,
                },
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            temps = data.get("hourly", {}).get("temperature_2m", [])
            result = [float(t) for t in temps if t is not None]
            _forecast_cache[cache_key] = result
            return result
        except Exception:
            return []


def sigmoid(x, scale=SIGMOID_SCALE):
    try:
        return 1.0 / (1.0 + math.exp(-x / scale))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def extract_city(text):
    t = text.lower()
    for pattern, city_key in sorted(CITY_PATTERNS.items(), key=lambda x: -len(x[0])):
        if pattern in t:
            return city_key
    return ""


def extract_threshold_f(question):
    q = question.lower()

    # Celsius
    m = re.search(r'be\s+(-?\d+(?:\.\d+)?)\s*[^\w]*c', q)
    if m:
        tc = float(m.group(1))
        tf = tc * 9 / 5 + 32
        if "or below" in q or "or less" in q or "or lower" in q or "or cooler" in q:
            return tf, "below"
        elif "or higher" in q or "or above" in q or "or more" in q or "or warmer" in q:
            return tf, "above"
        return tf, "exact"

    # Fahrenheit
    m = re.search(r'be\s+(-?\d+(?:\.\d+)?)\s*[^\w]*f', q)
    if m:
        tf = float(m.group(1))
        if "or below" in q or "or less" in q or "or lower" in q or "or cooler" in q:
            return tf, "below"
        elif "or higher" in q or "or above" in q or "or more" in q or "or warmer" in q:
            return tf, "above"
        return tf, "exact"

    return None, None


async def backtest():
    print("=" * 80)
    print("  WEATHER STRATEGY BACKTEST")
    print("=" * 80)

    events = await fetch_resolved_events(200)
    print(f"\n  Loaded {len(events)} resolved temperature events\n")

    total_trades = 0
    total_pnl = 0.0
    wins = 0
    losses = 0
    skipped = 0
    no_forecast = 0
    city_stats = {}
    edge_buckets = {">50%": [0, 0, 0.0], "30-50%": [0, 0, 0.0], "15-30%": [0, 0, 0.0]}
    direction_stats = {"below": [0, 0, 0.0], "above": [0, 0, 0.0], "exact": [0, 0, 0.0]}
    date_pnl = {}

    for event in events:
        title = event.get("title", "")
        city = extract_city(title)
        if not city:
            continue

        end_date = (event.get("endDate", "") or "")[:10]
        if not end_date:
            continue

        markets = event.get("markets", [])

        # Get forecast temps for this city/date
        temps_f = await fetch_forecast(city, end_date)
        if not temps_f:
            no_forecast += 1
            continue

        forecast_high_f = max(temps_f)

        for m in markets:
            question = m.get("question", "")
            threshold_f, direction = extract_threshold_f(question)
            if threshold_f is None:
                continue

            # Our forecast probability for YES
            margin = forecast_high_f - threshold_f
            if direction == "below":
                forecast_prob_yes = 1.0 - sigmoid(margin, SIGMOID_SCALE)
            elif direction == "above":
                forecast_prob_yes = sigmoid(margin, SIGMOID_SCALE)
            else:
                forecast_prob_yes = math.exp(-0.5 * (margin / 2.0) ** 2)

            # Actual resolution
            prices = m.get("outcomePrices", "[]")
            if isinstance(prices, str):
                prices = json.loads(prices)
            if len(prices) < 2:
                continue
            actual_yes = float(prices[0]) > 0.5

            # Determine our trade side
            # Simulate: assume market was ~fair (entry ~ 0.5 for contested, lower for tails)
            if direction in ("below", "above"):
                if forecast_prob_yes > 0.5 + MIN_EDGE:
                    side = "YES"
                    entry_price = max(0.05, 1.0 - forecast_prob_yes + 0.05)
                    edge = forecast_prob_yes - entry_price
                elif (1.0 - forecast_prob_yes) > 0.5 + MIN_EDGE:
                    side = "NO"
                    entry_price = max(0.05, forecast_prob_yes + 0.05)
                    edge = (1.0 - forecast_prob_yes) - entry_price
                else:
                    skipped += 1
                    continue
            else:
                if forecast_prob_yes > 0.5 + MIN_EDGE:
                    side = "YES"
                    entry_price = max(0.05, 1.0 - forecast_prob_yes + 0.05)
                    edge = forecast_prob_yes - entry_price
                elif forecast_prob_yes < 0.5 - MIN_EDGE:
                    side = "NO"
                    entry_price = max(0.05, forecast_prob_yes + 0.05)
                    edge = (1.0 - forecast_prob_yes) - entry_price
                else:
                    skipped += 1
                    continue

            if edge < MIN_EDGE:
                skipped += 1
                continue

            # Kelly sizing
            kelly_raw = edge / (1.0 - entry_price) if entry_price < 1 else 0
            bet = max(MIN_BET, min(MAX_BET, kelly_raw * KELLY_FRACTION * 100))

            # PnL
            won = (side == "YES" and actual_yes) or (side == "NO" and not actual_yes)
            if won:
                pnl = bet * ((1.0 / entry_price) - 1) * (1 - TAKER_FEE)
                wins += 1
            else:
                pnl = -bet
                losses += 1

            total_trades += 1
            total_pnl += pnl

            # Edge bucket
            if edge > 0.50:
                b = edge_buckets[">50%"]
            elif edge > 0.30:
                b = edge_buckets["30-50%"]
            else:
                b = edge_buckets["15-30%"]
            b[0] += 1
            b[1] += 1 if won else 0
            b[2] += pnl

            # Direction stats
            d = direction_stats[direction]
            d[0] += 1
            d[1] += 1 if won else 0
            d[2] += pnl

            # City stats
            if city not in city_stats:
                city_stats[city] = {"trades": 0, "wins": 0, "pnl": 0.0}
            city_stats[city]["trades"] += 1
            city_stats[city]["wins"] += 1 if won else 0
            city_stats[city]["pnl"] += pnl

            # Date PnL
            if end_date not in date_pnl:
                date_pnl[end_date] = {"trades": 0, "wins": 0, "pnl": 0.0}
            date_pnl[end_date]["trades"] += 1
            date_pnl[end_date]["wins"] += 1 if won else 0
            date_pnl[end_date]["pnl"] += pnl

    # Results
    wr = wins / total_trades * 100 if total_trades else 0
    print(f"{'='*80}")
    print(f"  RESULTS")
    print(f"{'='*80}")
    print(f"  Total trades:    {total_trades}")
    print(f"  Wins/Losses:     {wins}/{losses} ({wr:.1f}%)")
    print(f"  Total PnL:       ${total_pnl:+.2f}")
    if total_trades:
        print(f"  Avg PnL/trade:   ${total_pnl/total_trades:+.3f}")
        print(f"  Profit Factor:   {abs(sum(1 for _ in range(wins))) * (total_pnl + sum(MAX_BET for _ in range(losses))) / max(1, losses * MAX_BET):.2f}" if losses else "  Profit Factor:   inf")
    print(f"  Skipped:         {skipped} (edge < {MIN_EDGE*100:.0f}%)")
    print(f"  No forecast:     {no_forecast}")

    print(f"\n  Edge Buckets:")
    for label, (cnt, w, p) in edge_buckets.items():
        wr2 = w / cnt * 100 if cnt else 0
        print(f"    {label:>8}: {cnt:3d} trades, {w}W/{cnt-w}L ({wr2:.0f}%), PnL=${p:+.2f}")

    print(f"\n  Market Type:")
    for label, (cnt, w, p) in direction_stats.items():
        wr2 = w / cnt * 100 if cnt else 0
        print(f"    {label:>8}: {cnt:3d} trades, {w}W/{cnt-w}L ({wr2:.0f}%), PnL=${p:+.2f}")

    print(f"\n  Daily PnL:")
    for d in sorted(date_pnl.keys(), reverse=True):
        s = date_pnl[d]
        dwr = s["wins"] / s["trades"] * 100 if s["trades"] else 0
        print(f"    {d}: {s['trades']:3d} trades, {s['wins']}W ({dwr:.0f}%), PnL=${s['pnl']:+.2f}")

    print(f"\n  City Performance (top 10 by PnL):")
    sorted_cities = sorted(city_stats.items(), key=lambda x: -x[1]["pnl"])
    for city, s in sorted_cities[:10]:
        cwr = s["wins"] / s["trades"] * 100 if s["trades"] else 0
        print(f"    {city:>15}: {s['trades']:3d} trades, {s['wins']}W ({cwr:.0f}%), PnL=${s['pnl']:+.2f}")

    if len(sorted_cities) > 3:
        print(f"\n  Worst 5 cities:")
        for city, s in sorted_cities[-5:]:
            cwr = s["wins"] / s["trades"] * 100 if s["trades"] else 0
            print(f"    {city:>15}: {s['trades']:3d} trades, {s['wins']}W ({cwr:.0f}%), PnL=${s['pnl']:+.2f}")

    print(f"\n{'='*80}")


if __name__ == "__main__":
    asyncio.run(backtest())
