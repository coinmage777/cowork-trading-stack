"""레갭(Leverage Gap) 감지기 — Phase 7.

선물 거래소에서 단기간(기본 5분)에 발생한 급등(pump)을 감지하여
크로스-체인 아비트라지 기회를 포착한다.

ff_scanner 와의 차이:
- ff_scanner = 같은 시점의 거래소쌍 static spread (공간 차익)
- leverage_gap_scanner = 같은 거래소의 N분 전 대비 가격 변화 (시간 차익)

ff_scanner 가 "지금 Bybit vs Binance 가 벌어졌는가" 를 본다면,
이 스캐너는 "지금 Bybit 가 5분 전보다 얼마나 급등했는가 (다른 거래소 대비)" 를 본다.

사용 사례 (2026-04-21 노트): Bybit OPG 100%+ 갑작스러운 펌프 발생 →
BSC→Base 브릿지로 이동 → 느린 체인(Base)에서 follower 매수.
이 단계는 **감지만 한다**. 실제 브릿지/팔로워 실행은 Phase 3/4 에서 구현.

데이터 소스:
- poller.state: 현재 시점 futures_bbo (bid/ask) — 실시간
- gap_recorder SQLite: 1분 간격 히스토리 → N분 전 가격 조회

이벤트 출력:
- logger info
- data/lgap_events.jsonl append
- Telegram 알림 (티커×거래소 단위 쿨다운)
- on_leverage_gap 콜백 (Phase 4 브릿지 클라이언트가 구독)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# ENV helpers — ff_scanner 와 동일한 스타일
# ----------------------------------------------------------------------


def _env(key: str, default: str = '') -> str:
    return os.getenv(key, default).strip()


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, '').strip() or default)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(float(os.getenv(key, '').strip() or default))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key, '').strip().lower()
    if not v:
        return default
    return v in ('1', 'true', 'yes', 'y', 'on')


def _env_list(key: str, default: list[str]) -> list[str]:
    raw = os.getenv(key, '').strip()
    if not raw:
        return default
    return [t.strip().lower() for t in raw.split(',') if t.strip()]


# ----------------------------------------------------------------------
# 데이터 모델
# ----------------------------------------------------------------------


@dataclass
class LaggardInfo:
    """펌프 이벤트 발생 시 덜 펌프된(=기회가 있는) 거래소 정보."""
    exchange: str
    pump_pct: float
    current_price: float


@dataclass
class LeverageGapEvent:
    """감지된 레갭 이벤트."""
    ticker: str
    exchange: str           # 급등한 거래소
    pump_pct: float         # 5분 전 대비 상승률 (%)
    current_price: float    # 지금 선물 bid (USDT)
    past_price: float       # N분 전 선물 bid (USDT)
    lookback_sec: int       # 실제 비교에 쓴 과거 시점 간격 (초)
    detected_ts: int
    laggard_exchanges: list[LaggardInfo] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            'ticker': self.ticker,
            'exchange': self.exchange,
            'pump_pct': round(self.pump_pct, 3),
            'current_price': self.current_price,
            'past_price': self.past_price,
            'lookback_sec': self.lookback_sec,
            'detected_ts': self.detected_ts,
            'laggard_exchanges': [
                {
                    'exchange': lag.exchange,
                    'pump_pct': round(lag.pump_pct, 3),
                    'current_price': lag.current_price,
                }
                for lag in self.laggard_exchanges
            ],
        }


@dataclass
class LGapConfig:
    enabled: bool = True
    poll_interval_sec: int = 15
    pump_threshold_pct: float = 50.0        # 50%+ = 레갭 이벤트 판정
    laggard_max_pump_pct: float = 20.0      # 이 미만으로 움직인 거래소 = 팔로워 타겟
    lookback_min: int = 5                   # 5분 전과 비교
    notify_cooldown_sec: int = 1800         # 30분
    max_events_per_tick: int = 5
    history_path: str = 'data/lgap_events.jsonl'
    # 데이터 오염/상장 첫날 스파이크 걸러내기 — 비현실적 급등은 제외
    pump_sanity_cap_pct: float = 1000.0

    @classmethod
    def load(cls) -> 'LGapConfig':
        return cls(
            enabled=_env_bool('LGAP_ENABLED', True),
            poll_interval_sec=_env_int('LGAP_POLL_INTERVAL_SEC', 15),
            pump_threshold_pct=_env_float('LGAP_PUMP_THRESHOLD_PCT', 50.0),
            laggard_max_pump_pct=_env_float('LGAP_LAGGARD_MAX_PUMP_PCT', 20.0),
            lookback_min=_env_int('LGAP_LOOKBACK_MIN', 5),
            notify_cooldown_sec=_env_int('LGAP_NOTIFY_COOLDOWN_SEC', 1800),
            max_events_per_tick=_env_int('LGAP_MAX_EVENTS_PER_TICK', 5),
            history_path=_env('LGAP_HISTORY_PATH', 'data/lgap_events.jsonl'),
            pump_sanity_cap_pct=_env_float('LGAP_PUMP_SANITY_CAP_PCT', 1000.0),
        )


# ----------------------------------------------------------------------
# 스캐너 본체
# ----------------------------------------------------------------------


class LeverageGapScanner:
    """N분 전 대비 선물 가격 급등(pump)을 감지하는 시간 기반 스캐너.

    poller.state 로 현재값을, gap_recorder SQLite 로 과거값을 가져와서
    (ticker, exchange) 단위 pump_pct 를 계산한다.
    """

    def __init__(
        self,
        poller,
        gap_recorder,
        telegram_service=None,
        on_leverage_gap: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.cfg = LGapConfig.load()
        self.poller = poller
        self.gap_recorder = gap_recorder
        self.telegram = telegram_service
        self.on_leverage_gap = on_leverage_gap

        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._write_lock = asyncio.Lock()

        # 통계
        self._total_scans = 0
        self._total_detections = 0
        self._last_detection_ts: int = 0
        self._last_error: str = ''
        self._skip_no_history = 0

        # 알림 쿨다운 — key: "ticker|exchange"
        self._last_notify_ts: dict[str, float] = {}

    # ------------------------------------------------------------------
    # 라이프사이클
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running or not self.cfg.enabled:
            if not self.cfg.enabled:
                logger.info('[lgap_scanner] disabled via env')
            return
        self._running = True

        # history 파일 디렉토리 준비 (best-effort)
        try:
            Path(self.cfg.history_path).parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug('[lgap_scanner] history dir prep failed: %s', exc)

        self._task = asyncio.create_task(self._loop(), name='lgap_scanner_loop')
        logger.info(
            '[lgap_scanner] started | pump>=%.1f%% lookback=%dmin laggard<=%.1f%% poll=%ds',
            self.cfg.pump_threshold_pct,
            self.cfg.lookback_min,
            self.cfg.laggard_max_pump_pct,
            self.cfg.poll_interval_sec,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._scan()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._last_error = f'{type(exc).__name__}: {exc}'
                logger.warning('[lgap_scanner] scan err: %s', exc)
            await asyncio.sleep(self.cfg.poll_interval_sec)

    # ------------------------------------------------------------------
    # 스캔 본체
    # ------------------------------------------------------------------

    async def _scan(self) -> None:
        state = self.poller.state
        if not state:
            return
        self._total_scans += 1
        now = int(time.time())

        # 1) 과거 가격 맵 로드 (SQLite — 짧게 blocking sync)
        past_map = await self._load_past_price_map(now)
        if past_map is None:
            # gap_recorder 에러 → 다음 tick 재시도
            return
        if not past_map:
            # 아직 히스토리 없음 (봇 시작 직후) — 조용히 스킵
            self._skip_no_history += 1
            if self._skip_no_history % 20 == 1:  # 가끔만 로그
                logger.debug(
                    '[lgap_scanner] no historical data yet (ts range %d~%d)',
                    now - self.cfg.lookback_min * 60 - 120,
                    now - self.cfg.lookback_min * 60 + 60,
                )
            return

        # 2) 티커별로 현재 선물 bid 수집 + 과거와 비교
        events: list[LeverageGapEvent] = []
        for ticker, gap_result in state.items():
            if not gap_result or not gap_result.exchanges:
                continue
            past_by_exchange = past_map.get(ticker)
            if not past_by_exchange:
                continue

            # 거래소별 펌프율 계산
            pump_by_exchange: dict[str, tuple[float, float, float]] = {}
            # value: (pump_pct, current_price, past_price)
            for ex_name, ex_data in gap_result.exchanges.items():
                bbo = ex_data.futures_bbo if ex_data is not None else None
                if bbo is None:
                    continue
                current_bid = bbo.bid
                if current_bid is None or current_bid <= 0:
                    continue
                past_bid = past_by_exchange.get(ex_name)
                if past_bid is None or past_bid <= 0:
                    continue
                pump_pct = (current_bid - past_bid) / past_bid * 100.0
                # 음수(덤프)는 스캐너 범위 밖 — skip
                if pump_pct <= 0:
                    pump_by_exchange[ex_name] = (pump_pct, float(current_bid), float(past_bid))
                    continue
                # 비현실적 급등(상장/정지 해제/유령 틱) 은 제외
                if pump_pct > self.cfg.pump_sanity_cap_pct:
                    continue
                pump_by_exchange[ex_name] = (pump_pct, float(current_bid), float(past_bid))

            if not pump_by_exchange:
                continue

            # 3) threshold 이상 펌프된 거래소 색출
            for ex_name, (pump_pct, current, past) in pump_by_exchange.items():
                if pump_pct < self.cfg.pump_threshold_pct:
                    continue

                # 4) 동일 티커에서 덜 펌프된 거래소 = laggard (follower 타겟)
                laggards = [
                    LaggardInfo(
                        exchange=other_ex,
                        pump_pct=other_pump,
                        current_price=other_cur,
                    )
                    for other_ex, (other_pump, other_cur, _) in pump_by_exchange.items()
                    if other_ex != ex_name and other_pump < self.cfg.laggard_max_pump_pct
                ]
                # laggard 가장 덜 움직인 순서로
                laggards.sort(key=lambda l: l.pump_pct)

                events.append(LeverageGapEvent(
                    ticker=str(ticker),
                    exchange=ex_name,
                    pump_pct=pump_pct,
                    current_price=current,
                    past_price=past,
                    lookback_sec=self.cfg.lookback_min * 60,
                    detected_ts=now,
                    laggard_exchanges=laggards,
                ))

        if not events:
            return

        # 큰 pump 순
        events.sort(key=lambda e: -e.pump_pct)
        events = events[: max(1, self.cfg.max_events_per_tick)]

        self._total_detections += len(events)
        self._last_detection_ts = now

        for ev in events:
            logger.warning(
                '[lgap_scanner] PUMP %s %s +%.2f%% in %dmin (%.6g→%.6g) laggards=%d',
                ev.ticker, ev.exchange, ev.pump_pct,
                self.cfg.lookback_min,
                ev.past_price, ev.current_price, len(ev.laggard_exchanges),
            )
            await self._append_history(ev)
            await self._maybe_notify(ev, now)
            self._invoke_callback(ev)

    # ------------------------------------------------------------------
    # 과거 가격 로드
    # ------------------------------------------------------------------

    async def _load_past_price_map(self, now: int) -> Optional[dict[str, dict[str, float]]]:
        """gap_recorder SQLite 에서 N분 전 ± 60초 구간의 futures_bid_usdt 를 로드.

        반환: {ticker: {exchange: past_bid}}
        에러 시 None 반환 (다음 tick 재시도).
        None 과 빈 dict 는 구분됨 — 에러 vs 데이터 없음.
        """
        db_path = getattr(self.gap_recorder, 'db_path', None)
        if db_path is None:
            logger.debug('[lgap_scanner] gap_recorder has no db_path')
            return {}

        # N분 전 기준 ±60초 윈도우. gap_recorder 가 60초 간격이라 보장하진 않음.
        center = now - self.cfg.lookback_min * 60
        lo = center - 60
        hi = center + 60

        def _query() -> dict[str, dict[str, float]]:
            conn = sqlite3.connect(str(db_path), timeout=5)
            try:
                conn.execute('PRAGMA busy_timeout = 3000')
                cur = conn.execute(
                    """SELECT ticker, exchange, futures_bid_usdt, ts
                       FROM gap_history
                       WHERE ts BETWEEN ? AND ?
                         AND futures_bid_usdt IS NOT NULL
                         AND futures_bid_usdt > 0
                       ORDER BY ts""",
                    (lo, hi),
                )
                out: dict[str, dict[str, float]] = {}
                # 동일 (ticker, exchange) 중복 시 뒤쪽(=N분 전에 가까움) 값으로 덮어쓰기
                # — ORDER BY ts 이므로 마지막 레코드가 most-recent in window
                for ticker, exchange, bid, _ts in cur.fetchall():
                    if not ticker or not exchange:
                        continue
                    if bid is None or bid <= 0:
                        continue
                    out.setdefault(str(ticker), {})[str(exchange).lower()] = float(bid)
                return out
            finally:
                conn.close()

        try:
            return await asyncio.get_running_loop().run_in_executor(None, _query)
        except sqlite3.OperationalError as exc:
            # WAL lock, busy 등 → 다음 tick 에 재시도
            self._last_error = f'sqlite: {exc}'
            logger.debug('[lgap_scanner] sqlite busy/lock: %s', exc)
            return None
        except Exception as exc:  # noqa: BLE001
            self._last_error = f'{type(exc).__name__}: {exc}'
            logger.warning('[lgap_scanner] past price load err: %s', exc)
            return None

    # ------------------------------------------------------------------
    # 이벤트 기록/알림/콜백
    # ------------------------------------------------------------------

    async def _append_history(self, ev: LeverageGapEvent) -> None:
        path = Path(self.cfg.history_path)
        async with self._write_lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('a', encoding='utf-8') as f:
                    f.write(json.dumps(ev.to_json(), ensure_ascii=False) + '\n')
            except Exception as exc:  # noqa: BLE001
                logger.debug('[lgap_scanner] history append err: %s', exc)

    async def _maybe_notify(self, ev: LeverageGapEvent, now: int) -> None:
        key = f'{ev.ticker}|{ev.exchange}'
        last = self._last_notify_ts.get(key, 0.0)
        if now - last < self.cfg.notify_cooldown_sec:
            return
        self._last_notify_ts[key] = now

        # laggards 요약 (최대 3개)
        if ev.laggard_exchanges:
            lag_preview = ', '.join(
                f'{l.exchange}+{l.pump_pct:.1f}%@{l.current_price:.6g}'
                for l in ev.laggard_exchanges[:3]
            )
        else:
            lag_preview = '(none)'

        text = (
            f'🚀 레갭 감지: {ev.ticker} {ev.exchange} '
            f'+{ev.pump_pct:.2f}% in {self.cfg.lookback_min}min '
            f'({ev.past_price:.6g}→{ev.current_price:.6g})\n'
            f'laggards: {lag_preview}'
        )
        # low pump (<5%) = noisy → give filter key
        alert_key = 'lgap_low' if ev.pump_pct < 5.0 else None
        await self._notify(text, alert_key=alert_key)

    async def _notify(self, text: str, alert_key: str | None = None) -> None:
        if self.telegram is None:
            return
        try:
            send = getattr(self.telegram, '_send_message', None)
            if send is not None:
                try:
                    await send(text, alert_key=alert_key)
                    return
                except TypeError:
                    await send(text)
                    return
            send = getattr(self.telegram, 'send_message', None)
            if send is None:
                return
            await send(text)
        except Exception as exc:  # noqa: BLE001
            logger.debug('[lgap_scanner] telegram err: %s', exc)

    def _invoke_callback(self, ev: LeverageGapEvent) -> None:
        cb = self.on_leverage_gap
        if cb is None:
            return
        try:
            cb(ev.to_json())
        except Exception as exc:  # noqa: BLE001
            logger.warning('[lgap_scanner] callback err: %s', exc)

    # ------------------------------------------------------------------
    # 상태 / 최근 이벤트
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            'running': self._running,
            'enabled': self.cfg.enabled,
            'total_scans': self._total_scans,
            'total_detections': self._total_detections,
            'last_detection_ts': self._last_detection_ts,
            'last_error': self._last_error,
            'skip_no_history': self._skip_no_history,
            'config': {
                'pump_threshold_pct': self.cfg.pump_threshold_pct,
                'laggard_max_pump_pct': self.cfg.laggard_max_pump_pct,
                'lookback_min': self.cfg.lookback_min,
                'poll_interval_sec': self.cfg.poll_interval_sec,
                'notify_cooldown_sec': self.cfg.notify_cooldown_sec,
                'max_events_per_tick': self.cfg.max_events_per_tick,
                'history_path': self.cfg.history_path,
            },
        }

    def recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        """jsonl 에서 최근 N 개 이벤트 읽기 — 대시보드 용."""
        if limit <= 0:
            return []
        path = Path(self.cfg.history_path)
        if not path.exists():
            return []
        try:
            with path.open('r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as exc:  # noqa: BLE001
            logger.debug('[lgap_scanner] recent_events read err: %s', exc)
            return []
        out: list[dict[str, Any]] = []
        for raw in reversed(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except Exception:  # noqa: BLE001
                continue
            if len(out) >= limit:
                break
        return out
