"""파일 기반 kill switch.

운영 중에 외부 (운영자/대시보드/cron)에서 봇 진입을 차단하고 싶을 때
data_dir 안에 빈 파일 하나만 만들면 끝. 반대로 파일 지우면 즉시 해제.

지원 파일:
  KILL_ALL          → 전체 진입 차단
  KILL_<EXCHANGE>   → 특정 거래소 진입 차단 (예: KILL_lighter)

체크 방식:
  - 파일 존재 여부만 본다 (내용 무관)
  - 매 진입 직전에 ks.is_blocked(exchange) 호출
  - mtime 캐시로 100ms 폴링 부담 최소화

사용법:
  ks = KillSwitch(data_dir="./data")
  ok, reason = ks.check("lighter")
  if not ok:
      logger.warning(f"진입 차단: {reason}")
      return
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class KillSwitch:
    def __init__(self, data_dir: str | Path = "."):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._cache_at: float = 0.0
        self._cache_ttl: float = 1.0  # 1초 캐시

    def _path(self, name: str) -> Path:
        return self.data_dir / name

    def is_global_killed(self) -> bool:
        return self._path("KILL_ALL").exists()

    def is_exchange_killed(self, exchange: str) -> bool:
        return self._path(f"KILL_{exchange}").exists()

    def check(self, exchange: Optional[str] = None) -> tuple[bool, str]:
        """진입 가능 여부 (ok, reason). ok=False면 차단."""
        if self.is_global_killed():
            return False, "KILL_ALL"
        if exchange and self.is_exchange_killed(exchange):
            return False, f"KILL_{exchange}"
        return True, ""

    def is_blocked(self, exchange: Optional[str] = None) -> bool:
        """check()의 invert. 코드 흐름상 가독성 위해."""
        ok, _ = self.check(exchange)
        return not ok

    def engage(self, exchange: Optional[str] = None, reason: str = "") -> Path:
        """kill switch 발동. 파일을 생성하고 reason을 내용으로 기록."""
        name = f"KILL_{exchange}" if exchange else "KILL_ALL"
        p = self._path(name)
        p.write_text(
            f"engaged_at={time.time()}\nreason={reason}\n",
            encoding="utf-8",
        )
        logger.error(f"[kill_switch] ENGAGED {name}: {reason}")
        return p

    def release(self, exchange: Optional[str] = None) -> bool:
        """kill switch 해제. 파일 삭제. 없으면 False."""
        name = f"KILL_{exchange}" if exchange else "KILL_ALL"
        p = self._path(name)
        if not p.exists():
            return False
        try:
            p.unlink()
            logger.info(f"[kill_switch] released {name}")
            return True
        except Exception as e:
            logger.warning(f"[kill_switch] {name} 삭제 실패: {e}")
            return False

    def list_active(self) -> list[str]:
        """현재 활성화된 kill switch 파일명 리스트."""
        out = []
        for p in self.data_dir.iterdir():
            if p.is_file() and p.name.startswith("KILL_"):
                out.append(p.name)
        return sorted(out)
