"""5분 쿨다운 관리자."""

import time
from backend import config


class CooldownManager:
    """엔티티별 알림 쿨다운을 관리한다.

    Example:
        manager = CooldownManager()
        if manager.can_alert("BTC_binance_spot"):
            send_alert(...)
            manager.record_alert("BTC_binance_spot")
    """

    def __init__(self, cooldown_seconds: int = config.ALERT_COOLDOWN_SECONDS) -> None:
        self._cooldown_seconds = cooldown_seconds
        self._last_alert: dict[str, float] = {}

    def can_alert(self, entity_key: str) -> bool:
        """쿨다운이 지났는지 확인한다.

        Args:
            entity_key: 예) "BTC_binance_spot"

        Returns:
            True이면 알림 가능 (쿨다운 경과 또는 최초)
        """
        last = self._last_alert.get(entity_key)
        if last is None:
            return True
        return (time.monotonic() - last) >= self._cooldown_seconds

    def record_alert(self, entity_key: str) -> None:
        """알림 발송 시각을 기록한다.

        Args:
            entity_key: 예) "BTC_binance_spot"
        """
        self._last_alert[entity_key] = time.monotonic()

    def reset(self, entity_key: str) -> None:
        """특정 엔티티의 쿨다운을 초기화한다."""
        self._last_alert.pop(entity_key, None)

    def reset_all(self) -> None:
        """모든 쿨다운을 초기화한다."""
        self._last_alert.clear()
