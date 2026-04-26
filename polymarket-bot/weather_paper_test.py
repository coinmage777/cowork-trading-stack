#!/usr/bin/env python3
"""
날씨 전략 페이퍼 테스트 — 독립 실행 스크립트.

사용법:
    python weather_paper_test.py                   # 단일 스캔
    python weather_paper_test.py --loop             # 2분 간격 반복 스캔
    python weather_paper_test.py --loop --interval 60  # 1분 간격
"""

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone

from weather_strategy import WeatherStrategy, WeatherConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("weather_test")


class PaperTracker:
    """간단한 페이퍼 트레이드 추적기."""

    def __init__(self):
        self.trades: list[dict] = []
        self.total_invested: float = 0.0
        self.total_pnl: float = 0.0  # resolved 된 것만

    def record(self, opp: dict):
        trade = {
            **opp,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "open",
            "result_pnl": None,
        }
        self.trades.append(trade)
        self.total_invested += opp["bet_size"]
        logger.info(
            f"📝 PAPER TRADE #{len(self.trades)}: "
            f"{opp['side']} ${opp['bet_size']:.2f} @ {opp['entry_price']:.2f} | "
            f"edge={opp['edge']:.1%} | {opp['city']} | {opp['question'][:50]}"
        )

    def summary(self):
        print("\n" + "=" * 70)
        print("PAPER TRADING SUMMARY")
        print("=" * 70)
        print(f"  Total trades:    {len(self.trades)}")
        print(f"  Total invested:  ${self.total_invested:.2f}")
        print(f"  Avg edge:        {sum(t['edge'] for t in self.trades) / max(1, len(self.trades)):.1%}")
        print(f"  Cities traded:   {', '.join(sorted(set(t['city'] for t in self.trades)))}")
        if self.trades:
            best = max(self.trades, key=lambda t: t["edge"])
            print(f"  Best edge:       {best['edge']:.1%} on {best['question'][:50]}")
        print("=" * 70)


async def run_single_scan(strategy: WeatherStrategy, tracker: PaperTracker):
    """단일 스캔 + 페이퍼 트레이드."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n--- Scan @ {ts} ---")

    opps = await strategy.scan()

    if not opps:
        print("  No opportunities found.")
        return

    print(f"  Found {len(opps)} opportunities:")
    for i, opp in enumerate(opps[:5], 1):
        star = " ★" if opp["edge"] >= 0.25 else ""
        print(
            f"  {i}. [{opp['side']:3s}] {opp['city']:>12} | "
            f"forecast={opp['model_prob']:.1%} vs market={opp['market_prob']:.1%} | "
            f"edge={opp['edge']:.1%}{star} | ${opp['bet_size']:.2f}"
        )
        print(f"       {opp['question'][:60]}")

    # Record top opportunity as paper trade
    tracker.record(opps[0])


async def main():
    parser = argparse.ArgumentParser(description="Weather Strategy Paper Test")
    parser.add_argument("--loop", action="store_true", help="반복 스캔 모드")
    parser.add_argument("--interval", type=int, default=120, help="스캔 간격 (초)")
    parser.add_argument("--cities", type=str, default="new-york,london,chicago,seoul,hong-kong",
                        help="대상 도시 (콤마 구분)")
    parser.add_argument("--min-edge", type=float, default=0.15, help="최소 엣지 임계값")
    parser.add_argument("--max-bet", type=float, default=3.0, help="최대 베팅액")
    args = parser.parse_args()

    cfg = WeatherConfig(
        target_cities=[c.strip() for c in args.cities.split(",") if c.strip()],
        min_edge_threshold=args.min_edge,
        max_bet_size=args.max_bet,
        scan_interval_sec=0,  # 테스트 시 쿨다운 비활성화
    )
    strategy = WeatherStrategy(cfg)
    tracker = PaperTracker()

    print("=" * 70)
    print("POLYMARKET WEATHER STRATEGY — PAPER TEST")
    print(f"  Cities:    {', '.join(cfg.target_cities)}")
    print(f"  Min edge:  {cfg.min_edge_threshold:.0%}")
    print(f"  Max bet:   ${cfg.max_bet_size:.2f}")
    print(f"  Mode:      {'loop (' + str(args.interval) + 's)' if args.loop else 'single scan'}")
    print("=" * 70)

    try:
        if args.loop:
            scan_count = 0
            while True:
                scan_count += 1
                await run_single_scan(strategy, tracker)
                if scan_count % 5 == 0:
                    tracker.summary()
                print(f"\n  Next scan in {args.interval}s... (Ctrl+C to stop)")
                await asyncio.sleep(args.interval)
        else:
            # Single scan with full details
            print("\n[1/3] Fetching forecasts...")
            for city in cfg.target_cities:
                forecasts = await strategy.fetcher.get_forecasts(city)
                if forecasts:
                    by_date: dict[str, list[float]] = {}
                    for f in forecasts:
                        by_date.setdefault(f.date, []).append(f.temp_f)
                    for date, temps in sorted(by_date.items()):
                        print(f"  {city:>12} {date}  high={max(temps):.0f}°F  low={min(temps):.0f}°F  ({forecasts[0].source})")
                else:
                    print(f"  {city:>12}  -- no data")

            print("\n[2/3] Scanning weather markets...")
            markets = await strategy.market_scanner.fetch_weather_markets()
            print(f"  {len(markets)} active markets found")
            for m in markets[:8]:
                print(f"  • {m.question[:55]}  yes={m.yes_price:.2f}  no={m.no_price:.2f}  liq=${m.liquidity:.0f}")

            print("\n[3/3] Calculating edge...")
            await run_single_scan(strategy, tracker)
            tracker.summary()

    except KeyboardInterrupt:
        print("\n\nStopped by user.")
        tracker.summary()
    finally:
        await strategy.close()


if __name__ == "__main__":
    asyncio.run(main())
