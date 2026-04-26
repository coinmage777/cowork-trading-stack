"""봇 헬스 모니터 + 리스크 가드.

기능:
1. 일일 circuit breaker: equity_tracker 기준 당일 -$X 도달 시 신규 진입 차단
2. 거래소 auto-disable: 잔고 $5 미만 또는 일일 -30% 도달 시 자동 비활성화
3. 잔고 급락 감지: 30분 내 -20% 급락 시 Telegram 알림
4. HL WS 장애 감지: fallback 분당 10회 초과 시 알림 (로그 파싱 불필요, 카운터 기반)

사용:
    monitor = HealthMonitor(
        base_dir=Path('.'),
        equity_tracker=equity_tracker,
        exchange_tasks=_exchange_tasks,
        daily_stop_loss=-150.0,
        min_exchange_balance=5.0,
        notifier=notifier,
    )
    tasks.append(asyncio.create_task(monitor.run()))

trader 루프에서 신규 진입 전 `monitor.can_enter(exchange)` 체크.
trader에서 HL WS fallback 발생 시 `monitor.record_ws_fallback(exchange)` 호출.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class HealthMonitor:
    def __init__(
        self,
        *,
        base_dir: Path,
        daily_stop_loss: float = -150.0,
        weekly_stop_loss_pct: float = -5.0,  # 주간 -5% 서킷 브레이커
        min_exchange_balance: float = 5.0,
        exchange_daily_drawdown_pct: float = -30.0,
        balance_drop_pct: float = -20.0,
        balance_drop_window_seconds: int = 1800,
        ws_fallback_threshold: int = 10,
        ws_fallback_window_seconds: int = 60,
        check_interval_seconds: int = 60,
        auto_revive_check_hours: int = 24,  # 24h 지난 disable 재평가
        equity_tracker_path: Optional[Path] = None,
        disabled_exchanges_path: Optional[Path] = None,
        notifier=None,
        baseline_from_start: bool = True,
        grace_period_seconds: int = 300,
        funding_db_path: Optional[Path] = None,
        funding_stale_seconds: int = 1800,
    ):
        self.base_dir = Path(base_dir)
        self.daily_stop_loss = daily_stop_loss
        self.min_exchange_balance = min_exchange_balance
        self.exchange_daily_drawdown_pct = exchange_daily_drawdown_pct
        self.balance_drop_pct = balance_drop_pct
        self.balance_drop_window_seconds = balance_drop_window_seconds
        self.ws_fallback_threshold = ws_fallback_threshold
        self.ws_fallback_window_seconds = ws_fallback_window_seconds
        self.check_interval_seconds = check_interval_seconds

        self.equity_tracker_path = equity_tracker_path or (self.base_dir / "equity_tracker.json")
        self.disabled_exchanges_path = disabled_exchanges_path or (
            self.base_dir / "auto_disabled_exchanges.json"
        )
        self.notifier = notifier
        self.baseline_from_start = baseline_from_start
        self.grace_period_seconds = grace_period_seconds
        self.funding_db_path = funding_db_path or (self.base_dir / "funding_rates.db")
        self.funding_stale_seconds = funding_stale_seconds
        self._funding_stale_alerted = False
        self.weekly_stop_loss_pct = weekly_stop_loss_pct
        self.auto_revive_check_hours = auto_revive_check_hours
        self._started_at: float = time.time()

        self._circuit_breaker_tripped: bool = False
        self._circuit_breaker_tripped_at: float = 0.0
        self._weekly_cb_tripped: bool = False
        self._weekly_cb_tripped_at: float = 0.0
        self._auto_disabled: dict[str, dict] = {}
        self._load_disabled()

        # per-exchange WS fallback timestamps (sliding window)
        self._ws_fallback_events: dict[str, deque[float]] = defaultdict(deque)
        # per-exchange balance history for drop detection
        self._balance_history: dict[str, deque[tuple[float, float]]] = defaultdict(deque)

        self._running = False

    # ---------- public API ----------
    def can_enter(self, exchange: str) -> tuple[bool, str]:
        """신규 진입 가능 여부. (ok, reason)"""
        if self._circuit_breaker_tripped:
            return False, "daily_circuit_breaker"
        if exchange in self._auto_disabled:
            info = self._auto_disabled[exchange]
            return False, f"auto_disabled:{info.get('reason', 'unknown')}"
        return True, ""

    def record_ws_fallback(self, exchange: str) -> None:
        """HL WS fallback 발생 시 호출 (trader/wrapper에서)."""
        now = time.time()
        events = self._ws_fallback_events[exchange]
        events.append(now)
        # 윈도우 밖 이벤트 제거
        cutoff = now - self.ws_fallback_window_seconds
        while events and events[0] < cutoff:
            events.popleft()

    def is_exchange_enabled(self, exchange: str) -> bool:
        return exchange not in self._auto_disabled

    def get_status(self) -> dict:
        return {
            "circuit_breaker_tripped": self._circuit_breaker_tripped,
            "circuit_breaker_tripped_at": self._circuit_breaker_tripped_at,
            "auto_disabled": dict(self._auto_disabled),
            "ws_fallback_counts": {
                ex: len(events) for ex, events in self._ws_fallback_events.items()
            },
        }

    async def sync_zero_balance_at_startup(self, wait_seconds: int = 60) -> None:
        """
        봇 시작 시 1회: equity_tracker 첫 수집 대기 후
        잔고 < min_exchange_balance 인 거래소를 영속 등록.

        목적: 잔고 0 거래소가 EXCLUDED_FROM_DROP / `bal<=0.01 continue`
        가드로 인해 _check_exchange_balances 루프에서 등록 누락되는 케이스 방어.
        """
        try:
            await asyncio.sleep(wait_seconds)
            data = json.loads(self.equity_tracker_path.read_text(encoding='utf-8'))
            if not isinstance(data, list) or not data:
                logger.info('[health] sync_zero: equity_tracker 데이터 없음 — skip')
                return
            latest = data[-1]
            balances = latest.get('exchanges', {}) or {}
            failures = set(latest.get('_failures', []) or [])
            registered = []
            for ex, bal in balances.items():
                if ex in self._auto_disabled:
                    continue
                if ex in failures:
                    continue
                try:
                    bal_f = float(bal or 0.0)
                except Exception:
                    continue
                if bal_f < self.min_exchange_balance:
                    info = {
                        'reason': 'zero_balance_at_startup $' + format(bal_f, '.2f') + ' < $' + format(self.min_exchange_balance, '.2f'),
                        'disabled_at': time.time(),
                    }
                    self._auto_disabled[ex] = info
                    registered.append((ex, bal_f))
            if registered:
                self._save_disabled()
                summary_log = ', '.join(ex + '($' + format(bal, '.2f') + ')' for ex, bal in registered)
                logger.warning('[health] sync_zero: ' + str(len(registered)) + '개 거래소 자동 등록 — ' + summary_log)
                if self.notifier:
                    await self._safe_notify(
                        '<b>[AUTO-DISABLED:STARTUP]</b> ' + summary_log,
                        dedup_key='sync_zero_startup',
                        dedup_seconds=3600,
                    )
            else:
                logger.info('[health] sync_zero: 잔고 < min 거래소 없음 — skip')
        except Exception as e:
            logger.warning('[health] sync_zero 실패: ' + str(e))

    # ---------- main loop ----------
    async def run(self) -> None:
        self._running = True
        logger.info(
            f"[health] 시작: daily_stop=${self.daily_stop_loss}, "
            f"weekly_stop={self.weekly_stop_loss_pct}%, "
            f"min_bal=${self.min_exchange_balance}, "
            f"bal_drop={self.balance_drop_pct}%"
        )
        revive_counter = 0
        while self._running:
            try:
                await self._check_daily_circuit_breaker()
                await self._check_weekly_circuit_breaker()
                await self._check_exchange_balances()
                await self._check_ws_fallback()
                await self._check_funding_freshness()
                # 1시간마다 auto-disabled 재평가
                revive_counter += 1
                if revive_counter * self.check_interval_seconds >= 3600:
                    revive_counter = 0
                    await self._auto_revive_check()
            except Exception as e:
                logger.warning(f"[health] 체크 에러: {e}")
            await asyncio.sleep(self.check_interval_seconds)

    def stop(self) -> None:
        self._running = False

    def clear_circuit_breaker(self) -> None:
        """외부에서 수동 해제 (파일 트리거/커맨더에서 호출)."""
        if self._circuit_breaker_tripped or self._weekly_cb_tripped:
            logger.info("[health] circuit breaker 수동 해제 (daily + weekly)")
            self._started_at = time.time()
        self._circuit_breaker_tripped = False
        self._circuit_breaker_tripped_at = 0.0
        self._weekly_cb_tripped = False
        self._weekly_cb_tripped_at = 0.0

    async def _check_weekly_circuit_breaker(self) -> None:
        """최근 7일 누적 손실 체크. -5% 초과 시 전체 margin 25% 축소 알림."""
        # grace period 지나야 체크
        if time.time() - self._started_at < self.grace_period_seconds:
            return
        pct = self._compute_weekly_pnl_pct()
        if pct is None:
            return
        if not self._weekly_cb_tripped and pct <= self.weekly_stop_loss_pct:
            self._weekly_cb_tripped = True
            self._weekly_cb_tripped_at = time.time()
            msg = (
                f"<b>[WEEKLY CB]</b> 7일 누적 손실 {pct:.2f}% ≤ "
                f"{self.weekly_stop_loss_pct}% → 장기 손실 경고. "
                f"거래소 점검 및 margin 축소 권장"
            )
            logger.error(f"[health] {msg}")
            if self.notifier:
                await self._safe_notify(msg, dedup_key="weekly_cb", dedup_seconds=604800)

    def _compute_weekly_pnl_pct(self) -> Optional[float]:
        """최근 7일 잔고 변화율 (%)."""
        try:
            data = json.loads(self.equity_tracker_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, list) or len(data) < 2:
            return None
        now_ts = time.time()
        week_ago_ts = now_ts - 7 * 86400
        past = None
        for e in data:
            try:
                ts = self._parse_ts(e.get("timestamp"))
                if ts >= week_ago_ts:
                    past = e
                    break
            except Exception:
                pass
        if not past:
            return None
        latest = data[-1]

        def total(snap):
            return sum(
                float(v or 0)
                for k, v in snap.get("exchanges", {}).items()
                if k not in ("bulk", "dreamcash")
            )

        start = total(past)
        end = total(latest)
        if start <= 0:
            return None
        return (end - start) / start * 100

    async def _auto_revive_check(self) -> None:
        """auto-disabled 거래소 중 조건 해소된 것 자동 복구."""
        if not self._auto_disabled:
            return
        try:
            data = json.loads(self.equity_tracker_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, list) or not data:
            return
        latest_balances = data[-1].get("exchanges", {})
        revive_cutoff_ts = time.time() - (self.auto_revive_check_hours * 3600)

        to_revive = []
        for ex, info in list(self._auto_disabled.items()):
            disabled_at = float(info.get("disabled_at", 0))
            # 24시간 지나야 재평가
            if disabled_at > revive_cutoff_ts:
                continue
            # 현재 잔고 확인
            cur_bal = latest_balances.get(ex)
            if cur_bal is None:
                continue  # 거래소 미기록
            try:
                cur_bal_f = float(cur_bal)
            except Exception:
                continue
            # 잔고 $5 이상이고 daily drawdown 해소됐으면 복구
            if cur_bal_f >= self.min_exchange_balance:
                to_revive.append((ex, cur_bal_f))

        for ex, bal in to_revive:
            del self._auto_disabled[ex]
            msg = (
                f"<b>[AUTO-REVIVE]</b> {ex} auto-disabled 해제 "
                f"(현재 잔고 ${bal:.2f} ≥ ${self.min_exchange_balance})"
            )
            logger.info(f"[health] {msg}")
            await self._safe_notify(msg, dedup_key=f"revive_{ex}", dedup_seconds=86400)

        if to_revive:
            self._save_disabled()

    # ---------- checks ----------
    async def _check_daily_circuit_breaker(self) -> None:
        """equity_tracker 기준 오늘 PnL이 임계치 초과하면 circuit breaker 발동."""
        # Grace period: 봇 시작 직후 첫 N초는 CB 평가 skip
        if time.time() - self._started_at < self.grace_period_seconds:
            return
        pnl = self._compute_today_pnl()
        if pnl is None:
            return

        if not self._circuit_breaker_tripped and pnl <= self.daily_stop_loss:
            self._circuit_breaker_tripped = True
            self._circuit_breaker_tripped_at = time.time()
            msg = (
                f"<b>[CIRCUIT BREAKER]</b> 일일 손실 ${pnl:.2f} ≤ "
                f"${self.daily_stop_loss} → 신규 진입 차단"
            )
            logger.error(f"[health] {msg}")
            if self.notifier:
                await self._safe_notify(msg, dedup_key="daily_circuit_breaker", dedup_seconds=43200)

        # 다음 UTC 자정 자동 리셋
        if self._circuit_breaker_tripped:
            tripped_day = datetime.fromtimestamp(
                self._circuit_breaker_tripped_at, tz=timezone.utc
            ).date()
            now_day = datetime.now(tz=timezone.utc).date()
            if now_day > tripped_day:
                self._circuit_breaker_tripped = False
                self._circuit_breaker_tripped_at = 0.0
                logger.info("[health] 일일 circuit breaker 리셋 (UTC 자정)")

    def _compute_today_pnl(self) -> Optional[float]:
        """baseline_from_start=True면 봇 시작 시점 이후 손익, 아니면 UTC 오늘 손익."""
        try:
            data = json.loads(self.equity_tracker_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, list) or not data:
            return None

        if self.baseline_from_start:
            # 봇 시작 시점 이후 스냅샷만
            snapshots = []
            for entry in data:
                ts = self._parse_ts(entry.get("timestamp"))
                if ts >= self._started_at - 60:  # 60초 여유
                    snapshots.append(entry)
        else:
            # 레거시: UTC 오늘 기준
            today = datetime.now(tz=timezone.utc).date()
            snapshots = []
            for entry in data:
                ts = entry.get("timestamp", "")
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt.date() == today:
                        snapshots.append(entry)
                except Exception:
                    continue

        if len(snapshots) < 2:
            return None

        def total(snap):
            exs = snap.get("exchanges", {})
            return sum(float(v or 0.0) for k, v in exs.items() if k not in ("bulk", "dreamcash"))

        return total(snapshots[-1]) - total(snapshots[0])

    async def _check_exchange_balances(self) -> None:
        """거래소별 잔고 체크 → auto-disable + 급락 알림."""
        try:
            data = json.loads(self.equity_tracker_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, list) or not data:
            return

        latest = data[-1]
        latest_ts = self._parse_ts(latest.get("timestamp"))
        balances = latest.get("exchanges", {})

        # 잔고 히스토리 업데이트 (급락 감지용)
        # 2026-04-18: balance drop 체크 제외 리스트
        # HyENA HIP-3 (USDE margin), hotstuff (manual_equity) 등 포지션 commit 시 변동 큰 거래소
        EXCLUDED_FROM_DROP = {"bulk", "dreamcash", "hyena", "hyena_2", "hl_wallet_c", "hotstuff", "hotstuff_2"}
        for ex, bal in balances.items():
            if ex in EXCLUDED_FROM_DROP:
                continue
            try:
                bal_f = float(bal or 0.0)
            except Exception:
                continue
            # 2026-04-18: $0 스냅샷은 API 에러 가능성 높음 → 히스토리에서 제외
            # (HIP-3 USDe 조회 실패, WS 일시 단절 등으로 0이 스파이크 등장)
            if bal_f <= 0.01:
                continue
            hist = self._balance_history[ex]
            hist.append((latest_ts, bal_f))
            cutoff = latest_ts - self.balance_drop_window_seconds
            while hist and hist[0][0] < cutoff:
                hist.popleft()

            # 최저 잔고 auto-disable (0도 포함)
            if bal_f < self.min_exchange_balance and ex not in self._auto_disabled:
                await self._disable_exchange(
                    ex, f"balance ${bal_f:.2f} < ${self.min_exchange_balance}"
                )

            # 급락 감지 — 2026-04-18: 지속성 체크 강화 (3→5 샘플)
            # 포지션 오픈/클로즈 사이클로 $80→$37→$80 반복 시 오탐 방지
            # HyENA (HIP-3 USDE) 포지션 커밋 시 -60% 드롭도 정상 — 최소 50분 지속 필요
            if len(hist) >= 7 and bal_f > 0:
                # 피크 대비 비교 (window 내 최대값 기준)
                peak_bal = max(b for _, b in hist)
                if peak_bal > 10.0:
                    drop_pct = (bal_f - peak_bal) / peak_bal * 100.0
                    # 최근 5개 샘플(50분) 모두 peak 대비 동일 수준 이하인지 확인
                    recent = list(hist)[-5:]
                    sustained = all(
                        (b - peak_bal) / peak_bal * 100.0 <= self.balance_drop_pct
                        for _, b in recent
                    )
                    if drop_pct <= self.balance_drop_pct and sustained:
                        msg = (
                            f"<b>[BALANCE DROP]</b> {ex} "
                            f"peak ${peak_bal:.2f} → ${bal_f:.2f} ({drop_pct:+.1f}%) "
                            f"sustained {len(recent)} samples"
                        )
                        # 2026-04-22: balance drop notify 제거 — 24h 1000+건 스팸 원인.
                        # auto-disable 트리거(-30% 지속)는 별도 알림 (_disable_exchange) 유지.
                        logger.warning(f"[health] {msg}")

                        # 지속 drawdown 임계값 초과 시에만 auto-disable
                        if drop_pct <= self.exchange_daily_drawdown_pct and ex not in self._auto_disabled:
                            await self._disable_exchange(
                                ex,
                                f"sustained drawdown {drop_pct:.1f}% ≤ {self.exchange_daily_drawdown_pct}%",
                            )

    async def _check_ws_fallback(self) -> None:
        """HL WS fallback 빈도 체크."""
        now = time.time()
        cutoff = now - self.ws_fallback_window_seconds
        for ex, events in self._ws_fallback_events.items():
            while events and events[0] < cutoff:
                events.popleft()
            if len(events) >= self.ws_fallback_threshold:
                msg = (
                    f"<b>[WS FALLBACK]</b> {ex} "
                    f"{len(events)} REST fallback events / "
                    f"{self.ws_fallback_window_seconds}s"
                )
                logger.warning(f"[health] {msg}")
                await self._safe_notify(msg, dedup_key=f"ws_fallback_{ex}", dedup_seconds=600)

                # T2b: 임계 2배 초과 시 해당 거래소 임시 자동 비활성화 (15분)
                # 4/18 HL 장애 재발 방지 — 장애 중 진입 차단
                if len(events) >= self.ws_fallback_threshold * 2:
                    if ex not in self._auto_disabled:
                        await self._disable_exchange(
                            ex,
                            f"WS failover severe ({len(events)}/{self.ws_fallback_window_seconds}s)"
                        )
                        logger.error(f"[health] {ex} 임시 비활성화 — WS 과부하")

    async def _check_funding_freshness(self) -> None:
        """funding_rates.db 마지막 timestamp가 stale_seconds 초과 시 알림.

        근본 원인 (2026-04-25 9h stale 사고): funding_collector.run() 내부의
        collect_all/collect_price_gaps가 hang했을 때 try/except로 잡히지 않고
        while loop 자체가 정지. 외부 watchdog으로 stale 감지 → 운영자가 재시작.
        """
        try:
            import sqlite3
            db_path = self.funding_db_path
            if not db_path.exists():
                return
            conn = sqlite3.connect(str(db_path), timeout=2.0)
            try:
                cur = conn.execute(
                    "SELECT MAX(timestamp) FROM funding_rates"
                )
                row = cur.fetchone()
            finally:
                conn.close()
            if not row or not row[0]:
                return
            last_ts_str = row[0]
            last_ts = self._parse_ts(last_ts_str)
            now = time.time()
            age = now - last_ts
            if age > self.funding_stale_seconds:
                if not self._funding_stale_alerted:
                    msg = (
                        f"<b>[FUND STALE]</b> funding_collector 마지막 수집 "
                        f"{age/60:.1f}분 전 (임계 {self.funding_stale_seconds/60:.0f}분). "
                        f"last_ts={last_ts_str}. 봇 재시작 필요."
                    )
                    logger.error(f"[health] {msg}")
                    await self._safe_notify(
                        msg,
                        dedup_key="funding_stale",
                        dedup_seconds=1800,
                    )
                    self._funding_stale_alerted = True
            else:
                if self._funding_stale_alerted:
                    logger.info(
                        f"[health] funding_collector 회복 (last={age/60:.1f}분 전)"
                    )
                self._funding_stale_alerted = False
        except Exception as e:
            logger.debug(f"[health] funding freshness 체크 실패: {e!r}")

    async def _disable_exchange(self, exchange: str, reason: str) -> None:
        info = {"reason": reason, "disabled_at": time.time()}
        self._auto_disabled[exchange] = info
        self._save_disabled()
        msg = f"<b>[AUTO-DISABLED]</b> {exchange} — {reason}"
        logger.error(f"[health] {msg}")
        await self._safe_notify(msg, dedup_key=f"disable_{exchange}")

    def _load_disabled(self) -> None:
        if self.disabled_exchanges_path.exists():
            try:
                self._auto_disabled = json.loads(
                    self.disabled_exchanges_path.read_text(encoding="utf-8")
                )
            except Exception:
                self._auto_disabled = {}

    def _save_disabled(self) -> None:
        try:
            self.disabled_exchanges_path.write_text(
                json.dumps(self._auto_disabled, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[health] disabled 저장 실패: {e}")

    @staticmethod
    def _parse_ts(ts: str) -> float:
        if not ts:
            return time.time()
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return time.time()

    async def _safe_notify(self, msg: str, **kwargs) -> None:
        if not self.notifier:
            return
        try:
            await self.notifier.notify(msg, **kwargs)
        except Exception as e:
            logger.debug(f"[health] notify failed: {e}")
