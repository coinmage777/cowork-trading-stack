"""
config_lock.py — config.yaml 쓰기 잠금
auto_optimizer와 strategy_evolver가 동시에 config를 수정하지 않도록 보호.
"""
import json
import os
import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

LOCK_TIMEOUT = 120  # 2분 — 이보다 오래 잠기면 stale로 간주


class ConfigLock:
    def __init__(self, config_path: str):
        self.lock_path = Path(config_path).with_suffix(".lock")

    def acquire(self, owner: str, timeout: int = 30) -> bool:
        """잠금 획득. timeout초 내 획득 못하면 False 반환."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._try_lock(owner):
                return True
            time.sleep(1)
        logger.warning(f"[ConfigLock] {owner}: 잠금 획득 실패 ({timeout}초 대기)")
        return False

    def release(self, owner: str):
        """잠금 해제."""
        try:
            if self.lock_path.exists():
                data = json.loads(self.lock_path.read_text(encoding="utf-8"))
                if data.get("owner") == owner:
                    self.lock_path.unlink()
        except Exception:
            pass

    def _try_lock(self, owner: str) -> bool:
        if self.lock_path.exists():
            try:
                data = json.loads(self.lock_path.read_text(encoding="utf-8"))
                age = time.time() - data.get("time", 0)
                if age < LOCK_TIMEOUT:
                    return False
                logger.info(f"[ConfigLock] stale lock 제거 ({age:.0f}초 경과)")
            except Exception:
                pass
        try:
            self.lock_path.write_text(json.dumps({
                "owner": owner,
                "pid": os.getpid(),
                "time": time.time(),
            }), encoding="utf-8")
            return True
        except Exception:
            return False
