"""
XYZ Volume Farmer
HIP-3 xyz:XYZ100 마켓에서 볼륨 파밍

사용법:
    cd multi-perp-dex
    python -m strategies.xyz_farmer --config config.yaml
    python -m strategies.xyz_farmer --config config.yaml --size 0.01 --interval 45
"""

import asyncio
import argparse
import logging
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mpdex.factory import create_exchange, symbol_create

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────
# 기본 설정
# ──────────────────────────────────────────
DEFAULT_COIN = "xyz:XYZ100"
DEFAULT_SIZE = 0.005        # 0.005 lot ≈ $123 notional
DEFAULT_INTERVAL = 60       # 60초마다 사이클
DEFAULT_MAX_DAILY_COST = 50 # 일일 최대 비용 $50
FEE_RATE = 0.00009          # HIP-3 Growth Mode 0.009% (taker)
MAKER_FEE_RATE = 0.0        # HIP-3 Maker = 0%
ALO_OFFSET = 0.0001         # 0.01% BBO 오프셋
ALO_TIMEOUT = 30            # 지정가 체결 대기 (초)


class XyzVolumeFarmer:
    """
    핑퐁 볼륨 파머: market buy → close → repeat
    각 사이클마다 2x notional 볼륨 생성
    """

    def __init__(self, exchange, coin: str, size: float,
                 interval: int, max_daily_cost: float, display_name: str = "HL"):
        self.ex = exchange
        self.coin = coin
        self.symbol = symbol_create("hyperliquid", coin)
        self.size = size
        self.interval = interval
        self.max_daily_cost = max_daily_cost
        self.display = display_name

        # 통계
        self.total_volume = 0.0
        self.total_cost = 0.0
        self.cycles = 0
        self._maker_cycles = 0
        self._taker_fallbacks = 0
        self.start_time = None
        self._running = False

    async def run(self):
        self._running = True
        self.start_time = time.time()

        price = await self.ex.get_mark_price(self.symbol)
        notional = self.size * price
        cycle_cost_taker = notional * 2 * FEE_RATE
        est_daily_taker = cycle_cost_taker * (86400 / self.interval)
        est_daily = est_daily_taker  # ALO 사용 시 대부분 0, 폴백 시에만 비용

        logger.info(f"  {self.display} │ XYZ Farmer 시작")
        logger.info(f"  {self.display} │ 코인={self.coin} 사이즈={self.size} 간격={self.interval}초")
        logger.info(f"  {self.display} │ 1회 notional=${notional:,.0f} 예상 일일볼륨=${notional * 2 * 86400 / self.interval:,.0f}")
        logger.info(f"  {self.display} │ 예상 일일비용=${est_daily:.2f}")

        # 기존 포지션 정리
        await self._cleanup_position()

        while self._running:
            try:
                await self._cycle()
            except Exception as e:
                logger.error(f"  {self.display} │ 사이클 에러: {e}")
                await asyncio.sleep(5)
                continue

            if self.total_cost >= self.max_daily_cost:
                logger.warning(f"  {self.display} │ 일일 비용 한도 도달 (${self.total_cost:.2f}), 대기")
                # 자정까지 대기 후 리셋
                await self._wait_until_reset()
                self.total_cost = 0.0
                continue

            await asyncio.sleep(self.interval)

    async def _cycle(self):
        """한 사이클: ALO buy → ALO sell (maker 수수료 0%)"""
        price = await self.ex.get_mark_price(self.symbol)
        if not price:
            return

        notional = self.size * price
        offset = price * ALO_OFFSET
        cycle_cost = 0.0

        # 1) ALO Buy (bid side)
        buy_price = round(price - offset, 4)
        result = await self.ex.create_order(
            symbol=self.symbol, side="buy", amount=self.size,
            price=buy_price, order_type="limit", tif="Alo",
        )
        if not result:
            logger.warning(f"  {self.display} │ ALO 매수 실패")
            return

        # 체결 대기
        buy_filled = await self._wait_fill(ALO_TIMEOUT)
        if not buy_filled:
            await self.ex.cancel_orders(self.symbol)
            return  # 체결 안 됨 → 비용 0

        self._maker_cycles += 1

        # 2) ALO Sell (ask side)
        sell_price = round(price + offset, 4)
        result = await self.ex.create_order(
            symbol=self.symbol, side="sell", amount=self.size,
            price=sell_price, order_type="limit", tif="Alo",
        )

        sell_filled = await self._wait_fill(ALO_TIMEOUT)
        if not sell_filled:
            # 폴백: 시장가 청산 (포지션 보유 방지)
            await self.ex.cancel_orders(self.symbol)
            pos = await self.ex.get_position(self.symbol)
            if pos:
                await self.ex.close_position(self.symbol, pos)
            cycle_cost = notional * FEE_RATE  # taker 1회
            self._taker_fallbacks += 1

        # 통계
        self.total_volume += notional * 2
        self.total_cost += cycle_cost
        self.cycles += 1

        elapsed = time.time() - self.start_time
        hours = max(elapsed / 3600, 0.001)

        if self.cycles <= 3 or self.cycles % 5 == 0:
            logger.info(
                f"  {self.display} │ "
                f"#{self.cycles} "
                f"vol=${self.total_volume:,.0f} "
                f"cost=${self.total_cost:.2f} "
                f"maker={self._maker_cycles} fb={self._taker_fallbacks} "
                f"rate=${self.total_volume / hours:,.0f}/hr"
            )

    async def _wait_fill(self, timeout: int = 30) -> bool:
        """지정가 체결 대기 (open orders 폴링)"""
        for _ in range(timeout // 2):
            await asyncio.sleep(2)
            try:
                orders = await self.ex.get_open_orders(self.symbol)
                if not orders:
                    return True
            except Exception:
                pass
        return False

    async def _cleanup_position(self):
        """시작/종료 시 기존 포지션 정리"""
        try:
            pos = await self.ex.get_position(self.symbol)
            if pos:
                logger.info(f"  {self.display} │ 기존 {self.coin} 포지션 청산")
                await self.ex.close_position(self.symbol, pos)
                await asyncio.sleep(3)
        except Exception as e:
            logger.debug(f"  {self.display} │ 포지션 정리 스킵: {e}")

    async def _wait_until_reset(self):
        """다음 UTC 자정까지 대기"""
        now = datetime.now(timezone.utc)
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if tomorrow <= now:
            tomorrow = tomorrow.replace(day=tomorrow.day + 1)
        wait_secs = (tomorrow - now).total_seconds()
        logger.info(f"  {self.display} │ {wait_secs / 3600:.1f}시간 후 리셋")
        # 10분 단위로 체크하면서 대기
        while wait_secs > 0 and self._running:
            await asyncio.sleep(min(600, wait_secs))
            wait_secs -= 600

    async def stop(self):
        """정상 종료"""
        self._running = False
        await self._cleanup_position()
        elapsed = time.time() - (self.start_time or time.time())
        logger.info(
            f"  {self.display} │ Farmer 종료 — "
            f"{self.cycles}사이클, "
            f"vol=${self.total_volume:,.0f}, "
            f"cost=${self.total_cost:.2f}, "
            f"{elapsed / 3600:.1f}시간"
        )


# ──────────────────────────────────────────
# 메인 런처
# ──────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="XYZ Volume Farmer")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--coin", default=DEFAULT_COIN, help="HIP-3 코인 (default: xyz:XYZ100)")
    parser.add_argument("--size", type=float, default=None, help="주문 사이즈 (lot)")
    parser.add_argument("--interval", type=int, default=None, help="사이클 간격 (초)")
    parser.add_argument("--max-cost", type=float, default=None, help="일일 최대 비용 ($)")
    parser.add_argument("--exchange", default="hyperliquid", help="사용할 거래소 (config.yaml 키)")
    args = parser.parse_args()

    # 로깅 설정
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(message)s",
        datefmt="%H:%M:%S",
    )

    # config 로드
    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # farmer 설정 (config.yaml 또는 CLI 인자)
    farmer_cfg = cfg.get("xyz_farmer", {})
    coin = args.coin
    size = args.size or farmer_cfg.get("size", DEFAULT_SIZE)
    interval = args.interval or farmer_cfg.get("interval", DEFAULT_INTERVAL)
    max_cost = args.max_cost or farmer_cfg.get("max_daily_cost", DEFAULT_MAX_DAILY_COST)

    # 거래소 별칭 처리
    exc_key = args.exchange
    ALIASES = {"hyperliquid_2": "hyperliquid", "hyperliquid_3": "hyperliquid"}
    platform = ALIASES.get(exc_key, exc_key)
    display = {"hyperliquid_2": "miracle"}.get(exc_key, exc_key)

    # 거래소 연결
    exc_cfg = cfg["exchanges"].get(exc_key)
    if not exc_cfg or not exc_cfg.get("enabled"):
        logger.error(f"거래소 '{exc_key}'이 config.yaml에 없거나 비활성화됨")
        return

    kp = SimpleNamespace(**exc_cfg.get("keys", {}))
    logger.info(f"━━━ XYZ Volume Farmer ━━━")
    logger.info(f"  거래소: {display} | 코인: {coin} | 사이즈: {size}")

    try:
        exchange = await create_exchange(platform, kp)
    except Exception as e:
        logger.error(f"거래소 연결 실패: {e}")
        return

    farmer = XyzVolumeFarmer(
        exchange=exchange,
        coin=coin,
        size=size,
        interval=interval,
        max_daily_cost=max_cost,
        display_name=display.upper()[:6],
    )

    # 시그널 핸들러
    stop_event = asyncio.Event()

    def handle_signal():
        logger.info("종료 신호 수신...")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    # 실행
    farmer_task = asyncio.create_task(farmer.run())

    await stop_event.wait()
    await farmer.stop()
    farmer_task.cancel()

    try:
        await exchange.close()
    except Exception:
        pass

    logger.info("━━━ Farmer 종료 완료 ━━━")


if __name__ == "__main__":
    asyncio.run(main())
