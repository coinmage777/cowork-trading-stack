"""
State Manager — JSON 파일 기반 상태 영속화 (atomic write)
==========================================================
종료 시 트레이더 상태를 JSON으로 저장, 재시작 시 복원.
포지션 청산 없이 graceful restart 가능.

atomic write: tmp 파일에 쓴 뒤 os.replace로 한 번에 swap.
중간에 봇이 죽어도 기존 파일은 손상되지 않음.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


class StateManager:
    """
    Trader-agnostic state persister.

    스키마는 호출자 책임 — 본 모듈은 dict<str, dict>를 통째로 read/write.
    """

    def __init__(self, path: str | Path = "trader_state.json"):
        self.path = str(path)

    def save(self, state: dict[str, Any]) -> None:
        """
        atomic write. tmp에 쓰고 os.replace로 한 번에 swap.

        Args:
            state: 저장할 상태 dict. 비어있으면 파일 삭제 (clear()와 동일).
        """
        if not state:
            self.clear()
            return

        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp, self.path)
            logger.info(f"  STATE | {len(state)}개 항목 저장 -> {self.path}")
        except Exception as e:
            logger.error(f"  STATE | 저장 실패: {e}")
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def save_all(self, traders: Iterable) -> None:
        """
        편의 메서드 — trader 객체 리스트를 받아 각 객체의 get_state() 호출.

        trader는 다음 메서드를 가져야 함:
          get_state() -> dict | None  # None이면 저장 안 함
                                      # dict는 반드시 'exchange_name' 키 포함
        """
        states = {}
        for trader in traders:
            state = trader.get_state()
            if state and state.get("exchange_name"):
                states[state["exchange_name"]] = state
        self.save(states)

    def load(self) -> dict:
        """저장된 상태 로드. 없거나 깨진 파일은 빈 dict."""
        try:
            if not os.path.exists(self.path):
                return {}
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            logger.info(f"  STATE | {len(data)}개 항목 로드 <- {self.path}")
            return data
        except Exception as e:
            logger.warning(f"  STATE | 로드 실패: {e}")
            return {}

    def clear(self) -> None:
        """상태 파일 삭제."""
        try:
            if os.path.exists(self.path):
                os.unlink(self.path)
                logger.info(f"  STATE | {self.path} 삭제됨")
        except OSError:
            pass

    def exists(self) -> bool:
        return os.path.exists(self.path)
