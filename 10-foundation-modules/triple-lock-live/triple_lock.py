"""실거래 진입을 위한 3중 잠금 (triple lock).

라이브 모드 가드 — 다음 세 조건이 **모두** 참일 때만 실거래.
하나라도 빠지면 dry-run 또는 부팅 거부.

  1) ENABLED=true            (봇 자체 활성)
  2) DRY_RUN=false           (시뮬레이션 모드 아님)
  3) LIVE_CONFIRM=true       (운영자가 명시적으로 라이브 확인)

세 줄짜리 환경변수 검증이지만, 한 줄로 만들면 사람이 실수로 한두 개를
빠뜨려서 시뮬 인 줄 알고 진짜 돈을 굴리는 사고가 일어남. 그래서 별도 모듈.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _truthy(val: str | None) -> bool:
    if val is None:
        return False
    return val.strip().lower() in ("1", "true", "yes", "y", "on")


def is_live() -> tuple[bool, str]:
    """
    라이브 모드 진입 가능 여부.

    Returns:
        (live, reason): live=True면 실거래 가능. False면 reason에 막힌 사유.
    """
    enabled = _truthy(os.environ.get("ENABLED"))
    dry_run = _truthy(os.environ.get("DRY_RUN"))
    live_confirm = _truthy(os.environ.get("LIVE_CONFIRM"))

    if not enabled:
        return False, "ENABLED!=true"
    if dry_run:
        return False, "DRY_RUN=true"
    if not live_confirm:
        return False, "LIVE_CONFIRM!=true"
    return True, "ok"


def require_live() -> None:
    """라이브 모드가 아니면 RuntimeError. 부팅 시점에 호출하여 fail-fast."""
    live, reason = is_live()
    if not live:
        raise RuntimeError(
            f"[triple_lock] 실거래 잠금 — {reason}. "
            f"환경변수 확인: ENABLED=true, DRY_RUN=false, LIVE_CONFIRM=true"
        )
    logger.warning(
        "[triple_lock] LIVE MODE 활성 — 실거래 진행. "
        "ENABLED + DRY_RUN=false + LIVE_CONFIRM=true 모두 통과"
    )


def status() -> dict:
    """현재 3중 락 상태 dict — 대시보드/status.out 출력용."""
    return {
        "ENABLED": _truthy(os.environ.get("ENABLED")),
        "DRY_RUN": _truthy(os.environ.get("DRY_RUN")),
        "LIVE_CONFIRM": _truthy(os.environ.get("LIVE_CONFIRM")),
        "is_live": is_live()[0],
        "reason": is_live()[1],
    }
