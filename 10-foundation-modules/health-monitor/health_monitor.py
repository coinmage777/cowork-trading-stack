"""봇 헬스 모니터 — kill-switch + circuit-breaker + telegram-notifier orchestrator.

equity_tracker.json의 잔고 시계열을 주기적으로 읽어 다음을 판단:

  1) 일일 PnL → CircuitBreaker에 주입 (외부 모듈 위임)
  2) 거래소별 잔고 < min → KillSwitch.engage(exchange) (외부 모듈 위임)
  3) 잔고 급락 (peak 대비 -X% 지속) → notify (외부 모듈 위임)

본 모듈은 **orchestrator** — 실제 stop / kill / notify 로직은 주입받은
객체에 위임. 따라서 단독으로 동작하려면 다음 객체가 필요:

  - circuit_breaker: `record_pnl_delta(usd) | set_pnl_today(usd)` 메서드
  - kill_switch:     `engage(exchange, reason)` 메서드
  - notifier:        `notify(msg, dedup_key=..., dedup_seconds=...)` async

세 개 모두 None이면 본 모듈은 로깅만 함 (no-op orchestration).

사용:
    monitor = HealthMonitor(
        equity_tracker_path=Path("./equity_tracker.json"),
        circuit_breaker=cb,
        kill_switch=ks,
        notifier=notifier,
    )
    asyncio.create_task(monitor.run())
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class HealthMonitor:
    def __init__(
        self,
        *,
        equity_tracker_path: Path,
        circuit_breaker=None,
        kill_switch=None,
        notifier=None,
        min_exchange_balance: float = 5.0,
        balance_drop_pct: float = -20.0,
        balance_drop_window_seconds: int = 1800,
        check_interval_seconds: int = 60,
        baseline_from_start: bool = True,
        grace_period_seconds: int = 300,
        excluded_from_drop: Optional[set[str]] = None,
    ):
        self.equity_tracker_path = Path(equity_tracker_path)
        self.circuit_breaker = circuit_breaker
        self.kill_switch = kill_switch
        self.notifier = notifier
        self.min_exchange_balance = min_exchange_balance
        self.balance_drop_pct = balance_drop_pct
        self.balance_drop_window_seconds = balance_drop_window_seconds
        self.check_interval_seconds = check_interval_seconds
        self.baseline_from_start = baseline_from_start
        self.grace_period_seconds = grace_period_seconds
        self.excluded_from_drop = excluded_from_drop or set()

        self._started_at: float = time.time()
        self._balance_history: dict[str, deque[tuple[float, float]]] = defaultdict(deque)
        self._running = False

    async def run(self) -> None:
        self._running = True
        logger.info(
            f"[health] 시작: min_bal=${self.min_exchange_balance}, "
            f"drop_pct={self.balance_drop_pct}%, "
            f"interval={self.check_interval_seconds}s"
        )
        while self._running:
            try:
                snap = self._load_latest_snapshot()
                if snap:
                    await self._update_pnl(snap)
                    await self._check_balances(snap)
            except Exception as e:
                logger.warning(f"[health] 체크 에러: {e}")
            await asyncio.sleep(self.check_interval_seconds)

    def stop(self) -> None:
        self._running = False

    # ---------- internal ----------
    def _load_latest_snapshot(self) -> Optional[dict]:
        try:
            data = json.loads(self.equity_tracker_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, list) or not data:
            return None
        return data[-1]

    def _load_all_snapshots(self) -> list:
        try:
            data = json.loads(self.equity_tracker_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        return data if isinstance(data, list) else []

    async def _update_pnl(self, latest: dict) -> None:
        """오늘 PnL을 circuit_breaker에 통째로 주입."""
        if not self.circuit_breaker:
            return
        # grace
        if time.time() - self._started_at < self.grace_period_seconds:
            return
        pnl = self._compute_today_pnl()
        if pnl is None:
            return
        try:
            self.circuit_breaker.set_pnl_today(pnl)
        except AttributeError:
            # fallback: record_pnl_delta only
            pass

    def _compute_today_pnl(self) -> Optional[float]:
        data = self._load_all_snapshots()
        if len(data) < 2:
            return None

        if self.baseline_from_start:
            snapshots = [
                e for e in data
                if self._parse_ts(e.get("timestamp")) >= self._started_at - 60
            ]
        else:
            today = datetime.now(tz=timezone.utc).date()
            snapshots = []
            for entry in data:
                try:
                    dt = datetime.fromisoformat(
                        (entry.get("timestamp") or "").replace("Z", "+00:00")
                    )
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt.date() == today:
                        snapshots.append(entry)
                except Exception:
                    continue

        if len(snapshots) < 2:
            return None

        def total(snap: dict) -> float:
            return sum(float(v or 0.0) for v in snap.get("exchanges", {}).values())

        return total(snapshots[-1]) - total(snapshots[0])

    async def _check_balances(self, latest: dict) -> None:
        """거래소별 잔고: min 미만 → kill_switch, 급락 → notify."""
        latest_ts = self._parse_ts(latest.get("timestamp"))
        balances = latest.get("exchanges", {})
        for ex, bal in balances.items():
            if ex in self.excluded_from_drop:
                continue
            try:
                bal_f = float(bal or 0.0)
            except Exception:
                continue
            if bal_f <= 0.01:
                continue  # API 에러 가능성 — 히스토리 제외

            # 히스토리 업데이트
            hist = self._balance_history[ex]
            hist.append((latest_ts, bal_f))
            cutoff = latest_ts - self.balance_drop_window_seconds
            while hist and hist[0][0] < cutoff:
                hist.popleft()

            # 1) 최저 잔고 → kill_switch
            if bal_f < self.min_exchange_balance:
                if self.kill_switch:
                    try:
                        self.kill_switch.engage(
                            ex, reason=f"balance ${bal_f:.2f} < ${self.min_exchange_balance}"
                        )
                    except Exception as e:
                        logger.debug(f"[health] kill_switch.engage 실패: {e}")
                await self._notify(
                    f"<b>[LOW BALANCE]</b> {ex} ${bal_f:.2f} < ${self.min_exchange_balance}",
                    dedup_key=f"low_bal_{ex}",
                )

            # 2) 급락 (peak 대비 sustained drop)
            if len(hist) >= 7 and bal_f > 0:
                peak_bal = max(b for _, b in hist)
                if peak_bal > 10.0:
                    drop_pct = (bal_f - peak_bal) / peak_bal * 100.0
                    recent = list(hist)[-5:]
                    sustained = all(
                        (b - peak_bal) / peak_bal * 100.0 <= self.balance_drop_pct
                        for _, b in recent
                    )
                    if drop_pct <= self.balance_drop_pct and sustained:
                        await self._notify(
                            f"<b>[BALANCE DROP]</b> {ex} peak ${peak_bal:.2f} -> "
                            f"${bal_f:.2f} ({drop_pct:+.1f}%, sustained)",
                            dedup_key=f"drop_{ex}",
                            dedup_seconds=3600,
                        )

    async def _notify(self, msg: str, *, dedup_key: str, dedup_seconds: int = 600) -> None:
        if not self.notifier:
            logger.warning(f"[health] {msg}")
            return
        try:
            await self.notifier.notify(msg, dedup_key=dedup_key, dedup_seconds=dedup_seconds)
        except Exception as e:
            logger.debug(f"[health] notify 실패: {e}")

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
