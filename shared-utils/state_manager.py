"""
State Manager
=============
종료 시 트레이더 상태를 JSON으로 저장, 재시작 시 복원.
포지션 청산 없이 graceful restart 가능.
"""

import json
import os
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).resolve().parent.parent / "trader_state.json"


class StateManager:

    def __init__(self, path: str = None):
        self.path = str(path or STATE_FILE)

    def save_all(self, traders) -> None:
        """모든 트레이더 상태를 저장 (포지션 있는 것만)"""
        states = {}
        for trader in traders:
            state = trader.get_state()
            if state:
                states[state["exchange_name"]] = state

        if not states:
            self.clear()
            return

        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(states, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp, self.path)
            logger.info(f"  STATE │ {len(states)}개 트레이더 상태 저장")
        except Exception as e:
            logger.error(f"  STATE │ 저장 실패: {e}")
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def load(self) -> dict:
        """저장된 상태 로드. 없으면 빈 dict."""
        try:
            if not os.path.exists(self.path):
                return {}
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            logger.info(f"  STATE │ {len(data)}개 트레이더 상태 로드")
            return data
        except Exception as e:
            logger.warning(f"  STATE │ 로드 실패: {e}")
            return {}

    def clear(self) -> None:
        """상태 파일 삭제"""
        try:
            if os.path.exists(self.path):
                os.unlink(self.path)
        except OSError:
            pass
