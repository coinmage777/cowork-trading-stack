"""BNB 가스비 모니터 (Predict.fun Signer EOA).

Predict.fun 클레임은 Signer EOA에서 트랜잭션을 서명 + 가스비(BNB) 지불한다.
BNB 잔고가 0이면 수익금이 있어도 클레임 실패 → 이 모듈이 주기적으로 감시.

사용:
    from bnb_monitor import BnbGasMonitor
    monitor = BnbGasMonitor(signer_address=client.signer_address, notifier=notifier)
    asyncio.create_task(monitor.run())

    # 외부에서 상태 확인 (예: claim loop 전에)
    if monitor.is_low():
        logger.warning("BNB 부족 — claim 스킵")
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# BSC 공용 RPC (실패 시 다음 RPC로 rotate)
BSC_RPCS = [
    "https://bsc-dataseed.binance.org",
    "https://bsc-dataseed1.defibit.io",
    "https://bsc-dataseed1.ninicoin.io",
    "https://bsc.publicnode.com",
]


class BnbGasMonitor:
    def __init__(
        self,
        *,
        signer_address: str,
        low_balance_bnb: float = 0.002,
        critical_bnb: float = 0.0005,
        check_interval_seconds: int = 600,
        notifier=None,
    ):
        self.signer_address = signer_address
        self.low_balance_bnb = low_balance_bnb
        self.critical_bnb = critical_bnb
        self.check_interval_seconds = check_interval_seconds
        self.notifier = notifier

        self._last_balance_wei: Optional[int] = None
        self._last_checked: float = 0.0
        self._running = False

    async def get_balance_wei(self) -> Optional[int]:
        if not self.signer_address:
            return None
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getBalance",
            "params": [self.signer_address, "latest"],
            "id": 1,
        }
        timeout = aiohttp.ClientTimeout(total=10)
        for rpc in BSC_RPCS:
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(rpc, json=payload) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()
                        hex_bal = data.get("result")
                        if not hex_bal:
                            continue
                        return int(hex_bal, 16)
            except Exception as e:
                logger.debug(f"[bnb] RPC {rpc} 실패: {e}")
                continue
        return None

    async def get_balance_bnb(self) -> Optional[float]:
        wei = await self.get_balance_wei()
        if wei is None:
            return None
        return wei / 1e18

    def is_low(self) -> bool:
        if self._last_balance_wei is None:
            return False
        return self._last_balance_wei / 1e18 < self.low_balance_bnb

    def is_critical(self) -> bool:
        if self._last_balance_wei is None:
            return False
        return self._last_balance_wei / 1e18 < self.critical_bnb

    async def run(self) -> None:
        self._running = True
        was_critical = False  # 이전 사이클 critical 상태
        logger.info(
            f"[bnb] 모니터 시작: signer={self.signer_address} "
            f"low={self.low_balance_bnb} critical={self.critical_bnb}"
        )
        while self._running:
            try:
                bal = await self.get_balance_bnb()
                if bal is not None:
                    self._last_balance_wei = int(bal * 1e18)
                    self._last_checked = time.time()
                    if bal < self.critical_bnb:
                        was_critical = True
                        msg = (
                            f"<b>[BNB CRITICAL]</b> Signer <code>{self.signer_address}</code> "
                            f"balance={bal:.6f} BNB (below {self.critical_bnb}). "
                            f"Predict.fun claims will fail."
                        )
                        logger.error(f"[bnb] {msg}")
                        await self._safe_notify(msg, dedup_key="bnb_critical", dedup_seconds=3600)
                    elif bal < self.low_balance_bnb:
                        msg = (
                            f"<b>[BNB LOW]</b> Signer <code>{self.signer_address}</code> "
                            f"balance={bal:.6f} BNB (below {self.low_balance_bnb})."
                        )
                        logger.warning(f"[bnb] {msg}")
                        # 2026-04-23: dedup widened 6h → 24h to reduce alert spam.
                        # Override via BNB_LOW_DEDUP_SECONDS env.
                        try:
                            low_dedup = int(os.environ.get("BNB_LOW_DEDUP_SECONDS", "86400"))
                        except ValueError:
                            low_dedup = 86400
                        await self._safe_notify(msg, dedup_key="bnb_low", dedup_seconds=low_dedup)
                    else:
                        # 이전이 critical이었는데 회복 → 자동 claim 재개 알림
                        if was_critical:
                            was_critical = False
                            # 2026-04-23: recovered alert disabled by default.
                            # Enable via BNB_RECOVERED_ALERT=true.
                            recovered_enabled = os.environ.get("BNB_RECOVERED_ALERT", "false").strip().lower() in ("1", "true", "yes", "on")
                            if recovered_enabled:
                                msg = (
                                    f"<b>[BNB RECOVERED]</b> balance={bal:.6f} BNB ≥ {self.low_balance_bnb}\n"
                                    f"Predict.fun 자동 claim 재개. 대기 중이던 포지션 클레임 시도."
                                )
                                logger.info(f"[bnb] {msg}")
                                await self._safe_notify(msg, dedup_key="bnb_recovered", dedup_seconds=1800)
                            else:
                                logger.info(f"[bnb] recovered to {bal:.6f} BNB (alert silenced)")
                        logger.info(f"[bnb] balance={bal:.6f} BNB (ok)")
                else:
                    logger.warning("[bnb] 잔고 조회 실패 (모든 RPC 실패)")
            except Exception as e:
                logger.warning(f"[bnb] check 에러: {e}")
            await asyncio.sleep(self.check_interval_seconds)

    def stop(self) -> None:
        self._running = False

    async def _safe_notify(self, msg: str, **kwargs) -> None:
        if not self.notifier:
            return
        try:
            await self.notifier.notify(msg, **kwargs)
        except Exception as e:
            logger.debug(f"[bnb] notify failed: {e}")
