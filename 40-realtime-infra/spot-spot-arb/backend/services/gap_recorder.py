"""Gap 히스토리 기록기 — 매 60초마다 poller.state를 SQLite에 저장.

용도:
1. 역프/김프 이벤트 사후 분석 (어떤 티커가 자주, 언제, 얼마나)
2. /api/auto/gap-stats 로 ticker별 min/max/avg/stdev 조회
3. 알파 수집 — 진입 타이밍 / 최적 임계값 튜닝 데이터
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


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


@dataclass
class GapRow:
    ts: int
    ticker: str
    exchange: str
    spot_gap: Optional[float]
    futures_gap: Optional[float]
    bithumb_ask: Optional[float]
    futures_bid_usdt: Optional[float]
    usdt_krw: Optional[float]


class GapRecorder:
    def __init__(self, poller, db_path: Optional[str] = None) -> None:
        self.poller = poller
        self.enabled = _env_bool('GAP_RECORDER_ENABLED', True)
        self.interval_sec = _env_int('GAP_RECORDER_INTERVAL_SEC', 60)
        self.retention_hours = _env_int('GAP_RECORDER_RETENTION_HOURS', 168)  # 7일

        self.db_path = Path(db_path or os.getenv('GAP_RECORDER_DB', 'data/gap_history.db'))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._total_inserts = 0
        self._last_prune_ts = 0.0

    def _init_db(self) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS gap_history (
                    ts INTEGER NOT NULL,
                    ticker TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    spot_gap REAL,
                    futures_gap REAL,
                    bithumb_ask REAL,
                    futures_bid_usdt REAL,
                    usdt_krw REAL
                );
                CREATE INDEX IF NOT EXISTS idx_gap_ticker_ts ON gap_history (ticker, ts DESC);
                CREATE INDEX IF NOT EXISTS idx_gap_ts ON gap_history (ts DESC);
            """)
            conn.commit()
        finally:
            conn.close()

    async def start(self) -> None:
        if self._running or not self.enabled:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name='gap_recorder_loop')
        logger.info(
            '[gap_recorder] started | db=%s interval=%ds retention=%dh',
            self.db_path, self.interval_sec, self.retention_hours,
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
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('[gap_recorder] tick err: %s', exc)
            await asyncio.sleep(self.interval_sec)

    async def _tick(self) -> None:
        state = self.poller.state
        if not state:
            return
        now = int(time.time())
        rows: list[tuple] = []
        for ticker, gap_result in state.items():
            if gap_result is None:
                continue
            bithumb_ask = getattr(gap_result.bithumb, 'ask', None)
            usdt_krw = getattr(gap_result.bithumb, 'usdt_krw_last', None)
            for ex_name, ex_data in (gap_result.exchanges or {}).items():
                spot_gap = getattr(ex_data, 'spot_gap', None)
                futures_gap = getattr(ex_data, 'futures_gap', None)
                if spot_gap is None and futures_gap is None:
                    continue
                futures_bid = None
                if ex_data.futures_bbo is not None:
                    futures_bid = ex_data.futures_bbo.bid
                rows.append((
                    now, ticker, ex_name,
                    spot_gap, futures_gap,
                    bithumb_ask, futures_bid, usdt_krw,
                ))

        if not rows:
            return

        # async 실행 루프 블로킹 피하기 — 짧게 sync 삽입
        def _insert_sync():
            conn = sqlite3.connect(str(self.db_path), timeout=10)
            try:
                conn.executemany(
                    """INSERT INTO gap_history
                       (ts, ticker, exchange, spot_gap, futures_gap, bithumb_ask, futures_bid_usdt, usdt_krw)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    rows,
                )
                conn.commit()
            finally:
                conn.close()

        await asyncio.get_running_loop().run_in_executor(None, _insert_sync)
        self._total_inserts += len(rows)

        # 시간당 1회 prune
        if now - self._last_prune_ts > 3600:
            self._last_prune_ts = now
            asyncio.get_running_loop().run_in_executor(None, self._prune)

    def _prune(self) -> None:
        cutoff = int(time.time()) - self.retention_hours * 3600
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=10)
            try:
                conn.execute('DELETE FROM gap_history WHERE ts < ?', (cutoff,))
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.debug('[gap_recorder] prune err: %s', exc)

    def stats(self, hours: int = 24, min_samples: int = 10) -> list[dict[str, Any]]:
        """최근 N시간 동안 ticker별 futures_gap 통계."""
        cutoff = int(time.time()) - hours * 3600
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=10)
            try:
                cur = conn.execute(
                    """SELECT ticker, exchange, COUNT(*) as n,
                              MIN(futures_gap) as gap_min,
                              MAX(futures_gap) as gap_max,
                              AVG(futures_gap) as gap_avg,
                              SUM(CASE WHEN futures_gap < 9900 THEN 1 ELSE 0 END) as n_reverse,
                              SUM(CASE WHEN futures_gap > 10100 THEN 1 ELSE 0 END) as n_kimp
                       FROM gap_history
                       WHERE ts > ? AND futures_gap IS NOT NULL
                       GROUP BY ticker, exchange
                       HAVING n >= ?
                       ORDER BY n_reverse DESC, gap_min ASC
                       LIMIT 100""",
                    (cutoff, min_samples),
                )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
            finally:
                conn.close()
        except Exception as exc:
            logger.warning('[gap_recorder] stats err: %s', exc)
            return []

    def status(self) -> dict[str, Any]:
        return {
            'running': self._running,
            'enabled': self.enabled,
            'interval_sec': self.interval_sec,
            'retention_hours': self.retention_hours,
            'db_path': str(self.db_path),
            'total_inserts': self._total_inserts,
        }
