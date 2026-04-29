"""파일 트리거 감시 (Windows-friendly SIGHUP alternative).

Windows 환경에서 SIGHUP/SIGTERM 핸들러가 작동하지 않는 문제를 우회하기 위해
`triggers/` 디렉토리의 파일 존재 여부로 봇 제어 명령을 전달.

지원 트리거 (파일 존재하면 발동, 처리 후 자동 삭제):
  triggers/restart.trigger  -> graceful restart (포지션 유지)
  triggers/reload.trigger   -> hot reload (config만 리로드)
  triggers/close.trigger    -> 전체 청산 + 종료
  triggers/clear_cb.trigger -> circuit breaker 해제
  triggers/status.trigger   -> 현재 상태를 triggers/status.out 으로 dump

사용 예:
  cd multi-perp-dex
  touch triggers/restart.trigger      # macOS/Linux
  type nul > triggers\\restart.trigger # Windows
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

TRIGGER_DIR_NAME = "triggers"


class TriggerWatcher:
    def __init__(
        self,
        base_dir: str | Path,
        *,
        on_restart: Optional[Callable[[], Awaitable[None]]] = None,
        on_reload: Optional[Callable[[], Awaitable[None]]] = None,
        on_close: Optional[Callable[[], Awaitable[None]]] = None,
        on_clear_cb: Optional[Callable[[], Awaitable[None]]] = None,
        status_dumper: Optional[Callable[[], dict]] = None,
        poll_interval: float = 2.0,
    ):
        self.trigger_dir = Path(base_dir) / TRIGGER_DIR_NAME
        self.trigger_dir.mkdir(parents=True, exist_ok=True)
        self.on_restart = on_restart
        self.on_reload = on_reload
        self.on_close = on_close
        self.on_clear_cb = on_clear_cb
        self.status_dumper = status_dumper
        self.poll_interval = poll_interval
        self._running = False

    def _path(self, name: str) -> Path:
        return self.trigger_dir / name

    def _consume(self, name: str) -> bool:
        """파일이 있으면 삭제하고 True. 없으면 False."""
        p = self._path(name)
        if not p.exists():
            return False
        try:
            p.unlink()
        except Exception as e:
            logger.warning(f"[trigger] {name} 삭제 실패: {e}")
        return True

    async def run(self) -> None:
        self._running = True
        logger.info(f"[trigger] 감시 시작: {self.trigger_dir}")
        while self._running:
            try:
                if self._consume("restart.trigger") and self.on_restart:
                    logger.info("[trigger] restart.trigger -> graceful restart")
                    await self.on_restart()
                    break  # 봇 종료 예정이므로 loop 나감
                if self._consume("reload.trigger") and self.on_reload:
                    logger.info("[trigger] reload.trigger -> hot reload")
                    await self.on_reload()
                if self._consume("close.trigger") and self.on_close:
                    logger.info("[trigger] close.trigger -> 전체 청산 + 종료")
                    await self.on_close()
                    break
                if self._consume("clear_cb.trigger") and self.on_clear_cb:
                    logger.info("[trigger] clear_cb.trigger -> CB 해제")
                    await self.on_clear_cb()
                if self._consume("status.trigger"):
                    if self.status_dumper:
                        try:
                            state = self.status_dumper()
                            out = self._path("status.out")
                            out.write_text(
                                json.dumps(state, indent=2, ensure_ascii=False, default=str),
                                encoding="utf-8",
                            )
                            logger.info(f"[trigger] status -> {out.name}")
                        except Exception as e:
                            logger.warning(f"[trigger] status dump 실패: {e}")
            except Exception as e:
                logger.warning(f"[trigger] loop 에러: {e}")
            await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        self._running = False
