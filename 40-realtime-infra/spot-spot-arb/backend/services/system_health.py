"""Unified observability dashboard + emergency global kill switch.

세션 내내 19개 이상의 서비스를 빌드해놓은 상태라, 전체 현황을 한번에
보여주고 긴급 정지까지 원클릭으로 처리할 수 있는 통합 진입점이 필요하다.

핵심 기능:
- ``SystemHealthAggregator.health_check()`` — 모든 서비스의 ``status()`` 를
  5초 타임아웃 병렬 호출. 통합 스키마로 반환.
- ``trigger_emergency_stop(reason)`` — 14개 kill switch 파일 동시 생성.
  각 서비스가 주기적으로 파일 존재 여부를 폴링하므로 즉시 정지됨.
- ``cancel_emergency_stop()`` — kill switch 파일 제거.
- ``aggregated_metrics()`` — 전 서비스 opportunities/executed/errors/PnL 집계.

모든 예외는 서비스 단위로 격리한다. 한 서비스의 ``status()`` 가 예외를
던져도 전체 헬스체크가 깨지면 안 됨 — 대시보드의 본질은 *어느* 서비스가
문제인지 드러내는 것.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 상수 — 알려진 kill switch 파일 목록 (서비스와 1:1 매핑)
# ---------------------------------------------------------------------------

KNOWN_KILL_SWITCH_FILES: tuple[str, ...] = (
    'data/KILL_ARB',              # auto_trigger (legacy 이름)
    'data/KILL_LISTING',          # listing_executor
    'data/KILL_DEX',              # dex_trader
    'data/KILL_BRIDGE',           # bridge_client
    'data/KILL_FOLLOWER',         # bithumb_follower
    'data/KILL_LP',               # lp_manager
    'data/KILL_WALLET',           # wallet_tracker
    'data/KILL_MARGIN_ARB',       # margin_sell_arb
    'data/KILL_PREPOSITION',      # preposition_hedge
    'data/KILL_ORACLE_DIV',       # oracle_divergence_short
    'data/KILL_HACK_HALT',        # hack_halt_detector
    'data/KILL_COMMODITY',        # commodity_basis_arb
    'data/KILL_CROSS_LONG',       # cross_listing_long
    'data/KILL_TRANSFER',         # auto_transfer_service
    'data/KILL_MERGE_SPLIT',      # 예약 (아직 미구현 서비스)
)

# Live-armed 탐지 대상 키 (서비스 status 내에서 찾아볼 후보들)
_LIVE_ARMED_KEYS = (
    'live_armed',
    'live_confirm',
    'live',
)

# 개별 status() 호출 타임아웃
_STATUS_TIMEOUT_SEC = 5.0

# JSONL 집계 시 최근 N시간 범위
_METRICS_LOOKBACK_SEC_DEFAULT = 24 * 3600

# aggregated_metrics 에서 읽을 JSONL 경로 목록
_METRIC_JOB_FILES: tuple[str, ...] = (
    'data/dex_jobs.jsonl',
    'data/bridge_jobs.jsonl',
    'data/follower_jobs.jsonl',
    'data/listing_hedge_jobs.jsonl',
    'data/margin_arb_jobs.jsonl',
    'data/preposition_jobs.jsonl',
    'data/cross_listing_jobs.jsonl',
    'data/commodity_basis_jobs.jsonl',
    'data/oracle_divergence_jobs.jsonl',
)


def _now_ts() -> int:
    return int(time.time())


def _safe_bool(v: Any) -> bool:
    try:
        return bool(v)
    except Exception:
        return False


async def _call_status(
    name: str,
    service: Any,
    timeout: float = _STATUS_TIMEOUT_SEC,
) -> tuple[str, dict[str, Any], float, str | None]:
    """status() 를 호출하고 latency/에러 포착.

    Returns
    -------
    (name, status_dict, latency_ms, error_msg_or_None)
    """
    started = time.perf_counter()
    err: str | None = None
    status_dict: dict[str, Any] = {}

    try:
        status_fn = getattr(service, 'status', None)
        if status_fn is None or not callable(status_fn):
            err = 'no_status_method'
        else:
            result = status_fn()
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=timeout)
            if isinstance(result, dict):
                status_dict = result
            else:
                err = f'status_returned_non_dict:{type(result).__name__}'
    except asyncio.TimeoutError:
        err = f'timeout_{timeout}s'
    except Exception as exc:  # noqa: BLE001
        err = f'{type(exc).__name__}:{exc}'

    latency_ms = (time.perf_counter() - started) * 1000.0
    return name, status_dict, latency_ms, err


def _classify_healthy(status: dict[str, Any], error_msg: str | None) -> bool:
    """서비스 단위 healthy 판정.

    Rules:
    - status() 호출 자체 실패면 unhealthy.
    - running=False 면 unhealthy (단, 명시적으로 enabled=False 면 healthy 로 취급).
    - total_errors 누적이 있어도 total_polls > 0 또는 last_error 비어있으면 healthy.
    """
    if error_msg:
        return False
    if not status:
        return False

    # 명시적 enabled=False → 운영상 의도된 휴지. healthy 로 인정.
    enabled = status.get('enabled')
    running = status.get('running')

    if enabled is False:
        return True

    # running 필드 있고 False 면 unhealthy
    if running is not None and not _safe_bool(running):
        return False

    # 에러 스파이크: last_error 가 비어있으면 (최소 최근 이력은 깨끗) OK
    last_error = status.get('last_error')
    if last_error:
        # 그래도 total_polls 가 활발히 올라가는 중이면 일시 오류로 본다.
        total_polls = (
            status.get('total_polls')
            or status.get('total_polls_upbit')
            or status.get('total_polls_bithumb')
            or 0
        )
        if not total_polls:
            return False

    return True


def _extract_live_armed(status: dict[str, Any]) -> bool:
    for key in _LIVE_ARMED_KEYS:
        if key in status and _safe_bool(status[key]):
            return True
    return False


def _status_summary_line(status: dict[str, Any]) -> str:
    """status dict 을 한 줄로 요약 (사람이 읽기 편한 형태)."""
    if not status:
        return 'no_status'
    parts: list[str] = []
    running = status.get('running')
    if running is not None:
        parts.append(f'running={bool(running)}')
    enabled = status.get('enabled')
    if enabled is not None:
        parts.append(f'enabled={bool(enabled)}')
    if status.get('dry_run') is True:
        parts.append('DRY')
    if _extract_live_armed(status):
        parts.append('LIVE')
    for k in ('total_polls', 'total_detections', 'total_executed',
              'total_errors', 'open_jobs', 'watchlist_size'):
        v = status.get(k)
        if v is None:
            continue
        if isinstance(v, dict):
            parts.append(f'{k}={len(v)}')
        elif isinstance(v, (list, tuple, set)):
            parts.append(f'{k}={len(v)}')
        else:
            parts.append(f'{k}={v}')
    return ' '.join(parts) if parts else 'ok'


class SystemHealthAggregator:
    """모든 서비스를 감싸는 통합 헬스체커 + 글로벌 킬 스위치.

    Parameters
    ----------
    services_dict:
        ``{name: service_instance}`` 매핑. ``service_instance.status()`` 는
        dict 또는 awaitable-dict 이어야 한다. 없으면 no_status_method 로 표시.
    telegram_service:
        긴급 정지 알림용. ``_send_message(text)`` 코루틴을 가진 객체.
        None 이면 알림 스킵.
    base_dir:
        kill switch 파일이 생성될 루트. 기본값은 cwd.
    """

    def __init__(
        self,
        services_dict: dict[str, Any] | None = None,
        *,
        telegram_service: Any | None = None,
        base_dir: Path | str | None = None,
    ) -> None:
        self._services: dict[str, Any] = dict(services_dict or {})
        self._telegram = telegram_service
        self._base_dir = Path(base_dir) if base_dir else Path.cwd()
        self._started_ts = _now_ts()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 서비스 등록 API (late-binding 용)
    # ------------------------------------------------------------------

    def register(self, name: str, service: Any) -> None:
        self._services[name] = service

    def unregister(self, name: str) -> None:
        self._services.pop(name, None)

    @property
    def service_names(self) -> list[str]:
        return sorted(self._services.keys())

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def health_check(self) -> dict[str, Any]:
        """모든 서비스 status() 병렬 호출 → 통합 스키마.

        개별 서비스 예외/타임아웃은 error_msg 로 리포트하고 전체는 계속 진행.
        """
        tasks = [
            _call_status(name, svc)
            for name, svc in self._services.items()
        ]
        results: list[tuple[str, dict[str, Any], float, str | None]]
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=False)
        else:
            results = []

        services_block: dict[str, Any] = {}
        services_with_errors: list[str] = []
        services_not_running: list[str] = []
        any_live_armed = False
        financial_state: dict[str, Any] = {}
        overall_healthy = True

        for name, status_dict, latency_ms, err in results:
            healthy = _classify_healthy(status_dict, err)
            if not healthy:
                overall_healthy = False
            if err:
                services_with_errors.append(name)

            running = status_dict.get('running')
            if running is False:
                services_not_running.append(name)

            if _extract_live_armed(status_dict):
                any_live_armed = True
                financial_state[f'{name}_live_armed'] = True

            # 대시보드에서 유용한 재무성 플래그 노출
            if 'dry_run' in status_dict:
                financial_state[f'{name}_dry_run'] = _safe_bool(
                    status_dict.get('dry_run')
                )
            if 'live_confirm' in status_dict:
                financial_state[f'{name}_live_confirm'] = _safe_bool(
                    status_dict.get('live_confirm')
                )

            services_block[name] = {
                'running': _safe_bool(running) if running is not None else None,
                'enabled': (
                    _safe_bool(status_dict.get('enabled'))
                    if 'enabled' in status_dict else None
                ),
                'healthy': healthy,
                'latency_ms': round(latency_ms, 2),
                'status_summary': _status_summary_line(status_dict),
                'last_error': status_dict.get('last_error') or err,
                # 서비스별 원본 status 는 프론트가 필요할 때 깊이 파볼 수 있게 보관.
                'raw': status_dict,
            }

        kill_switches_active = self._active_kill_switches()

        return {
            'timestamp': _now_ts(),
            'overall_healthy': overall_healthy and not kill_switches_active,
            'service_count': len(self._services),
            'healthy_count': sum(
                1 for v in services_block.values() if v['healthy']
            ),
            'services': services_block,
            'critical_flags': {
                'any_live_armed': any_live_armed,
                'kill_switches_active': kill_switches_active,
                'services_with_errors': services_with_errors,
                'services_not_running': services_not_running,
            },
            'financial_state': financial_state,
            'resource_usage': self._resource_usage(),
        }

    async def health_summary(self) -> dict[str, Any]:
        """1-line 요약 ("healthy: 18/19 | live_armed: 0 | errors_1h: 0")."""
        full = await self.health_check()
        errors_1h = self._count_recent_errors(window_sec=3600)
        healthy = full['healthy_count']
        total = full['service_count']
        live_armed_count = sum(
            1 for k, v in full['financial_state'].items()
            if k.endswith('_live_armed') and v
        )
        line = (
            f"healthy: {healthy}/{total} | "
            f"live_armed: {live_armed_count} | "
            f"errors_1h: {errors_1h} | "
            f"kill_switches: {len(full['critical_flags']['kill_switches_active'])}"
        )
        return {
            'timestamp': full['timestamp'],
            'summary': line,
            'overall_healthy': full['overall_healthy'],
            'healthy_count': healthy,
            'service_count': total,
            'live_armed_count': live_armed_count,
            'errors_1h': errors_1h,
            'kill_switches_active': full['critical_flags']['kill_switches_active'],
        }

    # ------------------------------------------------------------------
    # Global kill switch
    # ------------------------------------------------------------------

    def _active_kill_switches(self) -> list[str]:
        active: list[str] = []
        for rel in KNOWN_KILL_SWITCH_FILES:
            path = self._base_dir / rel
            try:
                if path.exists():
                    active.append(rel)
            except Exception:  # noqa: BLE001
                continue
        return active

    async def trigger_emergency_stop(
        self,
        reason: str,
        *,
        user: str | None = None,
    ) -> dict[str, Any]:
        """14개+ kill switch 파일을 일괄 생성.

        각 서비스의 폴링 루프가 파일 존재를 감지하면 즉시 live 진입을 차단.
        텔레그램 알림 병행.
        """
        async with self._lock:
            created: list[str] = []
            failed: list[dict[str, str]] = []
            ts = _now_ts()
            payload = {
                'reason': reason,
                'user': user or 'system',
                'ts': ts,
            }
            body = json.dumps(payload, ensure_ascii=False, indent=2)

            for rel in KNOWN_KILL_SWITCH_FILES:
                path = self._base_dir / rel
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    # 파일 내용에 reason 을 박아두면 사후 디버깅이 수월함.
                    path.write_text(body, encoding='utf-8')
                    created.append(rel)
                except Exception as exc:  # noqa: BLE001
                    failed.append({'file': rel, 'error': str(exc)})

            logger.critical(
                '[system_health] EMERGENCY STOP: reason=%r user=%r created=%d failed=%d',
                reason, user, len(created), len(failed),
            )

            await self._notify_telegram(
                f"\U0001F6A8 EMERGENCY STOP: {reason} \u2014 "
                f"{len(created)} kill switches activated"
                + (f" (user={user})" if user else '')
            )

            return {
                'ok': True,
                'reason': reason,
                'user': user,
                'timestamp': ts,
                'created': created,
                'failed': failed,
                'count': len(created),
            }

    async def cancel_emergency_stop(
        self,
        *,
        user: str | None = None,
    ) -> dict[str, Any]:
        """Kill switch 파일 제거. 서비스 재개는 각자 알아서."""
        async with self._lock:
            removed: list[str] = []
            failed: list[dict[str, str]] = []
            for rel in KNOWN_KILL_SWITCH_FILES:
                path = self._base_dir / rel
                try:
                    if path.exists():
                        path.unlink()
                        removed.append(rel)
                except Exception as exc:  # noqa: BLE001
                    failed.append({'file': rel, 'error': str(exc)})

            logger.warning(
                '[system_health] Kill switches cleared: removed=%d failed=%d user=%r',
                len(removed), len(failed), user,
            )

            await self._notify_telegram(
                f"\u2705 Kill switches cleared \u2014 {len(removed)} files removed"
                + (f" (user={user})" if user else '')
                + " | services must resume on their own"
            )

            return {
                'ok': True,
                'user': user,
                'timestamp': _now_ts(),
                'removed': removed,
                'failed': failed,
                'count': len(removed),
            }

    # ------------------------------------------------------------------
    # Aggregated metrics
    # ------------------------------------------------------------------

    async def aggregated_metrics(
        self,
        lookback_sec: int = _METRICS_LOOKBACK_SEC_DEFAULT,
    ) -> dict[str, Any]:
        """전 서비스에 걸친 단순 집계.

        - total_opportunities_detected: 서비스 status 내 total_detections/total_detected 합
        - total_orders_executed: total_executed / total_auto_shorts 합 (dry+live 구분)
        - total_errors_1h: last 1h 로그 기반 (경량 sampling)
        - total_pnl_last_24h: JSONL 파일들의 pnl_usd/realized_pnl 필드 합
        """
        health = await self.health_check()
        services = health['services']

        total_detected = 0
        total_executed = 0
        total_dry_run = 0
        total_errors = 0
        for _name, block in services.items():
            raw = block.get('raw') or {}
            for key in ('total_detections', 'total_detected'):
                v = raw.get(key)
                if isinstance(v, int):
                    total_detected += v
            for key in ('total_executed', 'total_auto_shorts'):
                v = raw.get(key)
                if isinstance(v, int):
                    total_executed += v
            v = raw.get('total_dry_run')
            if isinstance(v, int):
                total_dry_run += v
            v = raw.get('total_errors')
            if isinstance(v, int):
                total_errors += v

        pnl_24h = self._aggregate_pnl_from_jsonl(lookback_sec=lookback_sec)
        errors_1h = self._count_recent_errors(window_sec=3600)

        return {
            'timestamp': _now_ts(),
            'lookback_sec': lookback_sec,
            'total_opportunities_detected': total_detected,
            'total_orders_executed': total_executed,
            'total_dry_run_orders': total_dry_run,
            'total_errors_accumulated': total_errors,
            'total_errors_last_1h': errors_1h,
            'total_pnl_last_24h_usd': pnl_24h['pnl_usd'],
            'pnl_records_counted': pnl_24h['records'],
            'pnl_by_source': pnl_24h['by_source'],
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resource_usage(self) -> dict[str, Any]:
        """프로세스 리소스 (psutil 있으면 사용, 없으면 최소 정보)."""
        pid = os.getpid()
        uptime = _now_ts() - self._started_ts
        info: dict[str, Any] = {
            'process_pid': pid,
            'uptime_sec': uptime,
            'memory_mb': None,
            'cpu_pct': None,
        }
        try:
            import psutil  # type: ignore

            proc = psutil.Process(pid)
            with proc.oneshot():
                mem = proc.memory_info().rss
                info['memory_mb'] = round(mem / (1024 * 1024), 1)
                # interval=None → non-blocking, 두번째 호출부터 의미 있음.
                info['cpu_pct'] = proc.cpu_percent(interval=None)
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug('[system_health] resource_usage err: %s', exc)
        return info

    async def _notify_telegram(self, text: str) -> None:
        if self._telegram is None:
            return
        # 다양한 인터페이스 지원.
        for attr in ('_send_message', 'send_message', 'send_alert'):
            fn = getattr(self._telegram, attr, None)
            if callable(fn):
                try:
                    result = fn(text)
                    if inspect.isawaitable(result):
                        await result
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        '[system_health] telegram %s failed: %s', attr, exc
                    )
                    continue

    def _count_recent_errors(self, window_sec: int = 3600) -> int:
        """현재 모든 서비스의 last_error_ts 중 window 내 것만 카운트.

        이 값은 정확한 로그 파싱이 아니라 status() 가 노출한 last_error_ts 기반.
        더 정밀한 수치가 필요하면 log 파일 파싱으로 확장.
        """
        cutoff = _now_ts() - window_sec
        count = 0
        for svc in self._services.values():
            try:
                fn = getattr(svc, 'status', None)
                if fn is None:
                    continue
                s = fn()
                if inspect.isawaitable(s):
                    # 동기 카운터에선 awaitable 건너뜀. health_check 경로에서 따로 수집.
                    continue
                if not isinstance(s, dict):
                    continue
                ts = s.get('last_error_ts') or 0
                if isinstance(ts, (int, float)) and ts >= cutoff:
                    if s.get('last_error'):
                        count += 1
            except Exception:  # noqa: BLE001
                continue
        return count

    def _aggregate_pnl_from_jsonl(
        self,
        lookback_sec: int,
    ) -> dict[str, Any]:
        cutoff = _now_ts() - lookback_sec
        total_pnl = 0.0
        by_source: dict[str, float] = {}
        records = 0

        for rel in _METRIC_JOB_FILES:
            path = self._base_dir / rel
            if not path.exists():
                continue
            try:
                src_total = 0.0
                src_count = 0
                # 대용량 파일 보호 — 뒤에서부터 최대 5000줄만.
                with path.open('r', encoding='utf-8') as f:
                    lines = f.readlines()
                for line in lines[-5000:]:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = _extract_ts(obj)
                    if ts is not None and ts < cutoff:
                        continue
                    pnl = _extract_pnl(obj)
                    if pnl is None:
                        continue
                    src_total += pnl
                    src_count += 1
                if src_count:
                    by_source[rel] = round(src_total, 4)
                    total_pnl += src_total
                    records += src_count
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    '[system_health] pnl aggregate err %s: %s', rel, exc
                )
                continue

        return {
            'pnl_usd': round(total_pnl, 4),
            'records': records,
            'by_source': by_source,
        }


# ---------------------------------------------------------------------------
# 헬퍼 — jsonl 레코드 파싱
# ---------------------------------------------------------------------------

def _extract_ts(obj: Any) -> float | None:
    if not isinstance(obj, dict):
        return None
    for key in ('ts', 'timestamp', 'closed_ts', 'created_ts'):
        v = obj.get(key)
        if isinstance(v, (int, float)):
            # 밀리초 단위 방어
            return float(v / 1000.0) if v > 1e12 else float(v)
    return None


def _extract_pnl(obj: Any) -> float | None:
    if not isinstance(obj, dict):
        return None
    for key in ('pnl_usd', 'realized_pnl', 'pnl', 'realized_pnl_usd'):
        v = obj.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return None


# ---------------------------------------------------------------------------
# FastAPI 엔드포인트 등록 헬퍼
# ---------------------------------------------------------------------------

def register_endpoints(app: Any, aggregator: SystemHealthAggregator) -> None:
    """main.py 에서 1회 호출하여 5개 엔드포인트 등록.

    route 등록을 함수로 감싼 이유: main.py 의 import/구성 순서에 의존하지 않기
    위함. aggregator 는 클로저로 캡처.
    """

    @app.get('/api/system/health')
    async def _system_health() -> dict[str, Any]:
        return await aggregator.health_check()

    @app.get('/api/system/health/summary')
    async def _system_health_summary() -> dict[str, Any]:
        return await aggregator.health_summary()

    @app.post('/api/system/emergency-stop')
    async def _system_emergency_stop(payload: dict[str, Any]) -> dict[str, Any]:
        reason = str((payload or {}).get('reason') or '').strip()
        if not reason:
            return {
                'ok': False,
                'code': 'INVALID_INPUT',
                'message': 'reason required',
            }
        user = (payload or {}).get('user')
        user_str = str(user).strip() if user else None
        return await aggregator.trigger_emergency_stop(
            reason=reason, user=user_str
        )

    @app.post('/api/system/resume')
    async def _system_resume(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        user = (payload or {}).get('user') if payload else None
        user_str = str(user).strip() if user else None
        return await aggregator.cancel_emergency_stop(user=user_str)

    @app.get('/api/system/metrics')
    async def _system_metrics() -> dict[str, Any]:
        return await aggregator.aggregated_metrics()
