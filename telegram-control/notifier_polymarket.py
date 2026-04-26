"""Telegram 알림 모듈 (Polymarket 봇용 — 2026-04-19 통합 버전).

Perp DEX 봇의 개선판과 동일:
- HTML 파싱 에러 자동 재시도 (plain text fallback)
- 모든 알림 notifications_log.json에 기록 (대시보드용)
- dedup 필터 (alert_filters.json 통해 비활성화 가능)
- Discord webhook 병행 전송 (DISCORD_WEBHOOK 환경변수)

환경변수:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  DISCORD_WEBHOOK (선택)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_last_sent: dict[str, float] = {}
_lock = asyncio.Lock()
_FILTER_PATH = Path(__file__).resolve().parent / "alert_filters.json"
_LOG_PATH = Path(__file__).resolve().parent / "notifications_log.json"
_LOG_MAX = 500


def _token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def _chat_id() -> str:
    return os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def _discord_webhook() -> str:
    return os.environ.get("DISCORD_WEBHOOK", "").strip()


def is_enabled() -> bool:
    return bool(_token() and _chat_id())


def _filters() -> dict:
    if not _FILTER_PATH.exists():
        return {"disabled_keys": [], "disabled_prefixes": []}
    try:
        return json.loads(_FILTER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"disabled_keys": [], "disabled_prefixes": []}


def _is_filtered(dedup_key: str) -> bool:
    if not dedup_key:
        return False
    f = _filters()
    if dedup_key in f.get("disabled_keys", []):
        return True
    for prefix in f.get("disabled_prefixes", []):
        if dedup_key.startswith(prefix):
            return True
    return False


def _append_log(message: str, dedup_key: Optional[str], sent: bool) -> None:
    """알림 이력을 JSON에 append. Dashboard Alerts 탭용."""
    try:
        import re
        plain = re.sub(r"<[^>]+>", "", message)[:400]
        import datetime as _dt
        entry = {
            "ts": time.time(),
            "iso": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "message": plain,
            "key": dedup_key or "",
            "sent": sent,
            "source": "polymarket",
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


async def notify(
    message: str,
    *,
    dedup_key: Optional[str] = None,
    dedup_seconds: int = 600,
    silent: bool = False,
) -> bool:
    """Telegram + Discord 메시지 전송. 실패/미설정/필터됨 시 False."""
    if _is_filtered(dedup_key or ""):
        return False

    # Discord 병행 전송 (있으면)
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
                        import re as _re
                        plain = _re.sub(r"<[^>]+>", "", message)[:4000]
                        retry_payload = {
                            "chat_id": _chat_id(),
                            "text": plain,
                            "disable_notification": silent,
                        }
                        async with session.post(url, json=retry_payload) as retry_resp:
                            if retry_resp.status == 200:
                                return True
                            logger.debug(f"[notifier] Telegram retry {retry_resp.status}")
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
