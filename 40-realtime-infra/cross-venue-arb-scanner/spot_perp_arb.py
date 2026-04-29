"""Spot-Perp 현선갭 아비트라지 (데이터맥시+ 6강 "현선갭").

원리:
- 같은 거래소의 spot과 perp 사이 가격 차이를 이용
- spot long + perp short (delta neutral) → 펀딩 수취 + 스프레드 수렴 이익
- 전송 시간 없어 즉시성 최고 (같은 거래소 내)

현재 상태: 2026-04-22 scaffold.
HL은 spot (BTC/USDC) + perp (BTC) 둘 다 지원. 활성화 전 마크 데이터 수집 단계.

스캐폴드 — 실제 주문 로직 실행 전에:
1. HL spot + perp 양쪽 mark 수집 (실시간)
2. basis 분포 DB 저장
3. 수익성 검증 후 라이브 활성화 (별도 결정)
"""
import asyncio
import time
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("strategies.multi_runner")

DB_PATH = Path("<INSTALL_DIR>/multi-perp-dex/spot_perp_basis.db")


@dataclass
class SpotPerpArbConfig:
    enabled: bool = False
    mode: str = "shadow"  # shadow | paper | live
    coins: list = field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    scan_interval: int = 60
    # shadow 수집 후 활성화 검토
    min_basis_pct: float = 0.15  # 0.15% 이상 spread
    size_usd: float = 20.0
    leverage: int = 3


class SpotPerpArbTrader:
    """Spot (BTC/USDC) + Perp (BTC) 현선갭 기회 탐색.

    현 단계: shadow 수집만. 실제 주문 로직은 이후 검증 후 추가.
    """

    def __init__(self, wrapper_provider, config: SpotPerpArbConfig):
        self.wrapper_provider = wrapper_provider
        self.config = config
        self.wrappers = {}
        self._running = False
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS spot_perp_basis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                symbol TEXT NOT NULL,
                venue TEXT NOT NULL,
                spot_price REAL,
                perp_price REAL,
                basis_pct REAL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sp_ts ON spot_perp_basis(ts, symbol)")
        conn.commit()
        conn.close()

    async def run_once(self):
        try:
            self.wrappers = self.wrapper_provider() or {}
        except Exception as e:
            logger.error(f"[spot_perp] wrapper_provider 실패: {e}")
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        rows = []
        for coin in self.config.coins:
            # HL 계열 거래소에서 spot + perp 동시 조회
            for venue in ("hyperliquid_2",):
                w = self.wrappers.get(venue)
                if not w:
                    continue
                try:
                    # perp mark
                    perp_price = await asyncio.wait_for(w.get_mark_price(coin), timeout=4.0)
                    # spot mark (심볼 포맷 거래소별 다름)
                    spot_symbol = f"{coin}/USDC"
                    spot_price = None
                    try:
                        if hasattr(w, "get_mark_price"):
                            spot_price = await asyncio.wait_for(
                                w.get_mark_price(spot_symbol, is_spot=True), timeout=4.0
                            )
                    except Exception:
                        pass
                    if not perp_price or not spot_price or spot_price <= 0:
                        continue
                    basis_pct = (float(perp_price) - float(spot_price)) / float(spot_price) * 100.0
                    rows.append((now_iso, coin, venue, float(spot_price), float(perp_price), basis_pct))
                    if abs(basis_pct) >= self.config.min_basis_pct:
                        logger.info(
                            f"[spot_perp] {coin} {venue} spot={spot_price:.4f} perp={perp_price:.4f} "
                            f"basis={basis_pct:+.3f}% (기회 포착 — shadow 기록)"
                        )
                except Exception as e:
                    logger.debug(f"[spot_perp] {coin} {venue} err: {e}")

        if rows:
            conn = sqlite3.connect(DB_PATH)
            conn.executemany(
                "INSERT INTO spot_perp_basis (ts, symbol, venue, spot_price, perp_price, basis_pct) VALUES (?,?,?,?,?,?)",
                rows,
            )
            conn.commit()
            conn.close()

    async def run(self):
        logger.info(
            f"[spot_perp] 시작 | enabled={self.config.enabled} mode={self.config.mode} "
            f"coins={self.config.coins} min_basis={self.config.min_basis_pct}%"
        )
        self._running = True
        while self._running:
            try:
                await self.run_once()
            except Exception as e:
                logger.error(f"[spot_perp] loop err: {e}")
            await asyncio.sleep(self.config.scan_interval)

    async def shutdown(self, close_positions: bool = True):
        self._running = False
        logger.info("[spot_perp] shutdown")
