"""
Oil Spread Trader — Brent/WTI 스프레드 페어트레이딩

전략:
- Brent(xyz:BRENTOIL) vs WTI(xyz:CL) 스프레드 추적
- 스프레드 Z-Score 기반 진입/청산
- Z > entry_z: 스프레드 확대 → Short Spread (Sell Brent, Buy WTI)
- Z < -entry_z: 스프레드 축소 → Long Spread (Buy Brent, Sell WTI)
- |Z| < exit_z: 평균 회귀 → 청산

실행:
  python -m strategies.oil_spread_trader --config config.yaml
  python -m strategies.oil_spread_trader --config config.yaml --dry-run
"""

import asyncio
import argparse
import logging
import time
import yaml
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ OIL │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


@dataclass
class OilSpreadConfig:
    coin1: str = "xyz:BRENTOIL"       # Long leg
    coin2: str = "xyz:CL"             # Short leg
    leverage: int = 10
    size: float = 1.0                  # 배럴 수
    lookback: int = 120                # Z-Score 계산 윈도우 (스냅샷 수)
    entry_z: float = 1.5              # 진입 Z-Score 임계값
    exit_z: float = 0.3               # 청산 Z-Score 임계값
    stop_loss_usd: float = 15.0       # 절대 손절 ($)
    scan_interval: int = 30            # 스캔 주기 (초)
    exchange_key: str = "hyperliquid_2"  # config.yaml 거래소 키


@dataclass
class SpreadPosition:
    direction: str = ""   # "long_spread" (Buy Brent, Sell WTI) or "short_spread"
    brent_entry: float = 0.0
    wti_entry: float = 0.0
    spread_entry: float = 0.0
    size: float = 0.0
    entry_time: float = 0.0


class OilSpreadTrader:
    def __init__(self, exchange, config: OilSpreadConfig, dry_run: bool = False):
        self.ex = exchange
        self.cfg = config
        self.dry_run = dry_run
        self.running = False
        self.pos = SpreadPosition()
        self.spread_history: deque[float] = deque(maxlen=config.lookback)

    def _calc_zscore(self, spread: float) -> float:
        if len(self.spread_history) < 20:
            return 0.0
        import statistics
        mean = statistics.mean(self.spread_history)
        stdev = statistics.stdev(self.spread_history)
        if stdev < 0.01:
            return 0.0
        return (spread - mean) / stdev

    async def _get_prices(self):
        brent = await self.ex.get_mark_price(self.cfg.coin1)
        wti = await self.ex.get_mark_price(self.cfg.coin2)
        return brent, wti

    async def _enter(self, direction: str, brent_px: float, wti_px: float):
        size = self.cfg.size
        if direction == "long_spread":
            # Buy Brent, Sell WTI (스프레드 확대 기대)
            if not self.dry_run:
                await self.ex.create_order(self.cfg.coin1, "buy", size, order_type="market", prefer_ws=False)
                await self.ex.create_order(self.cfg.coin2, "sell", size, order_type="market", prefer_ws=False)
            logger.info(f"진입 LONG SPREAD | Brent Buy {size}@{brent_px:.2f} + WTI Sell {size}@{wti_px:.2f} | spread={brent_px-wti_px:.2f}")
        else:
            # Sell Brent, Buy WTI (스프레드 축소 기대)
            if not self.dry_run:
                await self.ex.create_order(self.cfg.coin1, "sell", size, order_type="market", prefer_ws=False)
                await self.ex.create_order(self.cfg.coin2, "buy", size, order_type="market", prefer_ws=False)
            logger.info(f"진입 SHORT SPREAD | Brent Sell {size}@{brent_px:.2f} + WTI Buy {size}@{wti_px:.2f} | spread={brent_px-wti_px:.2f}")

        self.pos = SpreadPosition(
            direction=direction,
            brent_entry=brent_px,
            wti_entry=wti_px,
            spread_entry=brent_px - wti_px,
            size=size,
            entry_time=time.time(),
        )

    async def _close(self, reason: str, brent_px: float, wti_px: float):
        if not self.pos.direction:
            return
        size = self.pos.size
        if self.pos.direction == "long_spread":
            if not self.dry_run:
                await self.ex.create_order(self.cfg.coin1, "sell", size, order_type="market", prefer_ws=False)
                await self.ex.create_order(self.cfg.coin2, "buy", size, order_type="market", prefer_ws=False)
        else:
            if not self.dry_run:
                await self.ex.create_order(self.cfg.coin1, "buy", size, order_type="market", prefer_ws=False)
                await self.ex.create_order(self.cfg.coin2, "sell", size, order_type="market", prefer_ws=False)

        spread_now = brent_px - wti_px
        spread_pnl = (spread_now - self.pos.spread_entry) * size if self.pos.direction == "long_spread" else (self.pos.spread_entry - spread_now) * size
        hold_min = (time.time() - self.pos.entry_time) / 60

        logger.info(
            f"청산 | {reason} | PnL ~${spread_pnl:.2f} | "
            f"spread {self.pos.spread_entry:.2f}→{spread_now:.2f} | {hold_min:.0f}분 보유"
        )
        self.pos = SpreadPosition()

    def _calc_pnl(self, brent_px: float, wti_px: float) -> float:
        if not self.pos.direction:
            return 0.0
        spread_now = brent_px - wti_px
        if self.pos.direction == "long_spread":
            return (spread_now - self.pos.spread_entry) * self.pos.size
        else:
            return (self.pos.spread_entry - spread_now) * self.pos.size

    async def run(self):
        self.running = True
        cfg = self.cfg

        # 레버리지 설정
        for sym in [cfg.coin1, cfg.coin2]:
            try:
                await self.ex.update_leverage(sym, leverage=cfg.leverage, margin_mode="cross")
            except Exception:
                pass

        logger.info(
            f"Oil Spread Trader 시작 | {cfg.coin1} vs {cfg.coin2} | "
            f"size={cfg.size} lev={cfg.leverage}x | "
            f"entry_z={cfg.entry_z} exit_z={cfg.exit_z} | "
            f"SL=${cfg.stop_loss_usd} | {'DRY RUN' if self.dry_run else 'LIVE'}"
        )

        while self.running:
            try:
                brent, wti = await self._get_prices()
                spread = brent - wti
                self.spread_history.append(spread)
                z = self._calc_zscore(spread)

                if self.pos.direction:
                    pnl = self._calc_pnl(brent, wti)

                    # 손절
                    if pnl <= -cfg.stop_loss_usd:
                        await self._close(f"손절 ${pnl:.2f}", brent, wti)
                    # 평균 회귀 청산
                    elif self.pos.direction == "long_spread" and z <= cfg.exit_z:
                        await self._close(f"평균회귀 z={z:.2f}", brent, wti)
                    elif self.pos.direction == "short_spread" and z >= -cfg.exit_z:
                        await self._close(f"평균회귀 z={z:.2f}", brent, wti)
                    else:
                        logger.info(
                            f"보유 {self.pos.direction} | spread={spread:.2f} z={z:.2f} "
                            f"PnL=${pnl:.2f} | Brent={brent:.2f} WTI={wti:.2f}"
                        )
                else:
                    # 진입 체크
                    if len(self.spread_history) >= 20:
                        if z < -cfg.entry_z:
                            await self._enter("long_spread", brent, wti)
                        elif z > cfg.entry_z:
                            await self._enter("short_spread", brent, wti)
                        else:
                            logger.info(
                                f"대기 | spread={spread:.2f} z={z:.2f} "
                                f"({len(self.spread_history)}/{cfg.lookback}) | "
                                f"Brent={brent:.2f} WTI={wti:.2f}"
                            )
                    else:
                        logger.info(
                            f"수집중 | spread={spread:.2f} "
                            f"({len(self.spread_history)}/{cfg.lookback}) | "
                            f"Brent={brent:.2f} WTI={wti:.2f}"
                        )

                await asyncio.sleep(cfg.scan_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"에러: {e}")
                await asyncio.sleep(cfg.scan_interval)

        # 종료 시 포지션 청산
        if self.pos.direction:
            try:
                brent, wti = await self._get_prices()
                await self._close("shutdown", brent, wti)
            except Exception as e:
                logger.error(f"종료 청산 실패: {e}")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--size", type=float, default=1.0)
    parser.add_argument("--entry-z", type=float, default=1.5)
    parser.add_argument("--exit-z", type=float, default=0.3)
    parser.add_argument("--stop-loss", type=float, default=15.0)
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--exchange", default="hyperliquid_2")
    args = parser.parse_args()

    config_path = Path(__file__).resolve().parent.parent / args.config
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    from mpdex import create_exchange
    keys = config["exchanges"][args.exchange]["keys"]
    ex = await create_exchange("hyperliquid", SimpleNamespace(**keys))

    oil_config = OilSpreadConfig(
        size=args.size,
        entry_z=args.entry_z,
        exit_z=args.exit_z,
        stop_loss_usd=args.stop_loss,
        scan_interval=args.interval,
        exchange_key=args.exchange,
    )

    trader = OilSpreadTrader(ex, oil_config, dry_run=args.dry_run)

    try:
        await trader.run()
    except KeyboardInterrupt:
        trader.running = False
    finally:
        await ex.close()


if __name__ == "__main__":
    asyncio.run(main())
