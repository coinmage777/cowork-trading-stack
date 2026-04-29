"""Telegram 알림 + Discord 미러링 + dedup throttling.

환경변수로 설정:
  TELEGRAM_BOT_TOKEN: 봇 토큰 (BotFather에서 발급)
  TELEGRAM_CHAT_ID:   알림 받을 chat ID
  DISCORD_WEBHOOK:    (선택) Discord 웹훅 URL — 있으면 병행 전송

둘 중 하나라도 없으면 Telegram은 no-op (봇은 영향 없음).
쓰로틀링: 같은 dedup_key는 기본 600초 내 중복 전송 안 함.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_last_sent: dict[str, float] = {}
_lock = asyncio.Lock()
_FILTER_PATH = Path(__file__).resolve().parent / "alert_filters.json"
_LOG_PATH = Path(__file__).resolve().parent / "notifications_log.json"
_LOG_MAX = 500  # 최대 보관 개수


def _append_log(message: str, dedup_key: Optional[str], sent: bool) -> None:
    """알림 히스토리를 JSON 로그에 append (대시보드 Alerts 탭용)."""
    try:
        plain = re.sub(r"<[^>]+>", "", message)[:400]
        entry = {
            "ts": time.time(),
            "iso": datetime.now(timezone.utc).isoformat(),
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
        data = data[-_LOG_MAX:]
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
    """알림 필터 로드. {"disabled_keys": [...], "disabled_prefixes": [...]} 형식."""
    if not _FILTER_PATH.exists():
        return {"disabled_keys": [], "disabled_prefixes": []}
    try:
        return json.loads(_FILTER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"disabled_keys": [], "disabled_prefixes": []}


def _is_filtered(dedup_key: str) -> bool:
    """사용자가 막은 알림인지 체크."""
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
    if _is_filtered(dedup_key or ""):
        return False

    # Discord 웹훅 병행 전송 (있으면)
    webhook = _discord_webhook()
    if webhook:
        try:
            plain = re.sub(r"<[^>]+>", "", message)[:1900]
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
                    body = await resp.text()
                    if "can't parse entities" in body or "Unsupported start tag" in body:
                        # HTML 파싱 에러 → 태그 제거 후 재시도
                        plain = re.sub(r"<[^>]+>", "", message)[:4000]
                        retry_payload = {
                            "chat_id": _chat_id(),
                            "text": plain,
                            "disable_notification": silent,
                        }
                        async with session.post(url, json=retry_payload) as retry_resp:
                            if retry_resp.status == 200:
                                return True
                            logger.warning(
                                f"[notifier] Telegram retry {retry_resp.status}: "
                                f"{(await retry_resp.text())[:100]}"
                            )
                            return False
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
