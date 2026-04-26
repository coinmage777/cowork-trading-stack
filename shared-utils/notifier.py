"""Telegram 알림 공통 모듈.

환경변수로 설정:
  TELEGRAM_BOT_TOKEN: 봇 토큰 (BotFather에서 발급)
  TELEGRAM_CHAT_ID:   알림 받을 chat ID

둘 중 하나라도 없으면 no-op으로 동작 (봇은 영향 없음).
쓰로틀링: 같은 dedup_key는 기본 10분 내 중복 전송 안 함.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import aiohttp
import json
from pathlib import Path

logger = logging.getLogger(__name__)

_last_sent: dict[str, float] = {}
_lock = asyncio.Lock()
_FILTER_PATH = Path(__file__).resolve().parent.parent / "alert_filters.json"
_LOG_PATH = Path(__file__).resolve().parent.parent / "notifications_log.json"
_LOG_MAX = 500  # 최대 보관 개수


def _append_log(message: str, dedup_key: Optional[str], sent: bool) -> None:
    """알림 히스토리를 JSON 로그에 append (대시보드 Alerts 탭용)."""
    try:
        # HTML 태그 제거 (plain text로)
        import re
        plain = re.sub(r"<[^>]+>", "", message)[:400]
        entry = {
            "ts": time.time(),
            "iso": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            "message": plain,
            "key": dedup_key or "",
            "sent": sent,
        }
        data = []
        if _LOG_PATH.exists():
            try:
                data = json.loads(_LOG_PATH.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    data = []
            except Exception:
                data = []
        data.append(entry)
        data = data[-_LOG_MAX:]  # 최신 N개만 유지
        _LOG_PATH.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.debug(f"[notifier] 로그 저장 실패: {e}")


def _token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def _chat_id() -> str:
    return os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def _discord_webhook() -> str:
    return os.environ.get("DISCORD_WEBHOOK", "").strip()


def is_enabled() -> bool:
    return bool(_token() and _chat_id())


def _filters() -> dict:
    """알림 필터 로드. {"disabled_keys": ["ws_fallback_...", ...]} 형식"""
    if not _FILTER_PATH.exists():
        return {"disabled_keys": [], "disabled_prefixes": []}
    try:
        return json.loads(_FILTER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"disabled_keys": [], "disabled_prefixes": []}


def _is_filtered(dedup_key: str) -> bool:
    """사용자가 /alerts off로 막은 알림인지 체크."""
    if not dedup_key:
        return False
    f = _filters()
    if dedup_key in f.get("disabled_keys", []):
        return True
    for prefix in f.get("disabled_prefixes", []):
        if dedup_key.startswith(prefix):
            return True
    return False


async def notify(
    message: str,
    *,
    dedup_key: Optional[str] = None,
    dedup_seconds: int = 600,
    silent: bool = False,
) -> bool:
    """Telegram + Discord 메시지 전송. 실패/미설정/필터됨 시 False 반환."""
    # 필터 체크
    if _is_filtered(dedup_key or ""):
        return False

    # Discord 웹훅 병행 전송 (있으면)
    webhook = _discord_webhook()
    if webhook:
        try:
            import re as _re
            plain = _re.sub(r"<[^>]+>", "", message)[:1900]
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
                await s.post(webhook, json={"content": plain})
        except Exception as e:
            logger.debug(f"[discord] 전송 실패: {e}")

    if not is_enabled():
        _append_log(message, dedup_key, sent=False)
        return False

    if dedup_key:
        now = time.time()
        async with _lock:
            last = _last_sent.get(dedup_key, 0.0)
            if now - last < dedup_seconds:
                return False
            _last_sent[dedup_key] = now

    # 로그 저장 (전송 시도 전)
    _append_log(message, dedup_key, sent=True)

    payload = {
        "chat_id": _chat_id(),
        "text": message[:4000],
        "disable_notification": silent,
        "parse_mode": "HTML",
    }
    url = f"https://api.telegram.org/bot{_token()}/sendMessage"
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status == 400:
                    # HTML 파싱 에러(메시지에 <, > 포함 등) → parse_mode 없이 재시도
                    body = await resp.text()
                    if "can't parse entities" in body or "Unsupported start tag" in body:
                        import re as _re
                        plain = _re.sub(r"<[^>]+>", "", message)[:4000]
                        retry_payload = {
                            "chat_id": _chat_id(),
                            "text": plain,
                            "disable_notification": silent,
                        }
                        async with session.post(url, json=retry_payload) as retry_resp:
                            if retry_resp.status == 200:
                                return True  # 재시도 성공 — 로그 조용히
                            logger.warning(f"[notifier] Telegram retry {retry_resp.status}: {(await retry_resp.text())[:100]}")
                            return False
                    # 다른 400 (retry 불가능) — DEBUG로 강등
                    logger.debug(f"[notifier] Telegram 400: {body[:200]}")
                    return False
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"[notifier] Telegram {resp.status}: {body[:200]}")
                    return False
                return True
    except Exception as e:
        logger.warning(f"[notifier] send failed: {e}")
        return False


def notify_sync(message: str, **kwargs) -> bool:
    """동기 컨텍스트에서 호출용. 실행 중인 루프가 있으면 create_task, 없으면 asyncio.run."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        loop.create_task(notify(message, **kwargs))
        return True
    try:
        return asyncio.run(notify(message, **kwargs))
    except Exception as e:
        logger.warning(f"[notifier] sync send failed: {e}")
        return False
