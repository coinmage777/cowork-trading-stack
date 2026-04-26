"""Rust bridge for gap_recorder.

Spawns the Rust `gap-recorder` release binary as a subprocess and streams
observations to it over stdin as LDJSON. Preserves the API surface of
`gap_recorder.GapRecorder` (start, stop, stats, status) so callers can swap in
this class without other changes.

Fallback: if the binary is missing, unbuildable, or exits unexpectedly, this
bridge quietly falls back to the pure-Python `GapRecorder`. A subsequent restart
attempt picks up a newly-built binary on the next `start()` call.

ENV:
    GAP_RECORDER_RUST_BINARY    explicit path to gap-recorder[.exe]
    GAP_RECORDER_RUST_DISABLED  '1' to force-disable Rust path
    GAP_RECORDER_STATS_BIND     default 127.0.0.1:38744
    GAP_RECORDER_DB             db path (passed through to Rust)
    GAP_RECORDER_RETENTION_HOURS
    GAP_RECORDER_FLUSH_ROWS / _MS
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .gap_recorder import GapRecorder  # fallback

logger = logging.getLogger(__name__)


def _find_binary() -> Optional[Path]:
    """Locate the Rust binary. Returns None if not found."""
    if os.getenv('GAP_RECORDER_RUST_DISABLED', '').strip() in ('1', 'true', 'yes', 'on'):
        return None

    explicit = os.getenv('GAP_RECORDER_RUST_BINARY', '').strip()
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
        logger.warning('[gap_recorder_rust] GAP_RECORDER_RUST_BINARY=%s not found', explicit)
        return None

    exe = 'gap-recorder.exe' if platform.system() == 'Windows' else 'gap-recorder'

    # PATH lookup
    which = shutil.which(exe)
    if which:
        return Path(which)

    # Local workspace layout: backend/services/ → ../../rust-services/target/release/
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2] / 'rust-services' / 'target' / 'release' / exe,
        here.parents[2] / 'rust-services' / 'target' / 'debug' / exe,
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


class RustGapRecorder:
    """Drop-in replacement for `GapRecorder` that forwards rows to a Rust
    subprocess. On any failure, transparently falls back to the Python
    implementation (created lazily).
    """

    def __init__(self, poller, db_path: Optional[str] = None) -> None:
        self.poller = poller
        self.db_path = db_path or os.getenv('GAP_RECORDER_DB', 'data/gap_history.db')
        self.retention_hours = int(os.getenv('GAP_RECORDER_RETENTION_HOURS', '168'))
        self.interval_sec = int(os.getenv('GAP_RECORDER_INTERVAL_SEC', '60'))
        self.stats_bind = os.getenv('GAP_RECORDER_STATS_BIND', '127.0.0.1:38744')

        self._binary = _find_binary()
        self._proc: Optional[subprocess.Popen] = None
        self._stdin_lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._fallback: Optional[GapRecorder] = None
        self._tick_count = 0
        self._last_rows = 0
        self._start_ts = 0.0
        # ensure db parent exists (Rust also does this, but be defensive)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    # ---------- lifecycle ----------
    async def start(self) -> None:
        if self._running:
            return

        if self._binary is None:
            logger.warning('[gap_recorder_rust] binary not found; falling back to Python impl')
            await self._start_fallback()
            return

        try:
            env = os.environ.copy()
            env['GAP_RECORDER_DB'] = str(self.db_path)
            env['GAP_RECORDER_STATS_BIND'] = self.stats_bind
            env.setdefault('GAP_RECORDER_FLUSH_ROWS', '100')
            env.setdefault('GAP_RECORDER_FLUSH_MS', '1000')
            env.setdefault('GAP_RECORDER_RETENTION_HOURS', str(self.retention_hours))
            env.setdefault('RUST_LOG', 'info,gap_recorder=info')

            self._proc = subprocess.Popen(
                [str(self._binary)],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=env,
                bufsize=0,
            )
            # quick liveness probe — did it crash immediately?
            await asyncio.sleep(0.2)
            if self._proc.poll() is not None:
                stderr = b''
                try:
                    stderr = self._proc.stderr.read() if self._proc.stderr else b''
                except Exception:
                    pass
                logger.error(
                    '[gap_recorder_rust] binary exited rc=%s stderr=%s; falling back',
                    self._proc.returncode,
                    stderr.decode(errors='replace')[:500],
                )
                self._proc = None
                await self._start_fallback()
                return

            self._running = True
            self._start_ts = time.time()
            self._task = asyncio.create_task(self._loop(), name='gap_recorder_rust_loop')
            logger.info(
                '[gap_recorder_rust] started pid=%s binary=%s db=%s stats=%s',
                self._proc.pid, self._binary, self.db_path, self.stats_bind,
            )
        except Exception as exc:
            logger.exception('[gap_recorder_rust] spawn failed: %s; falling back', exc)
            await self._start_fallback()

    async def _start_fallback(self) -> None:
        if self._fallback is None:
            self._fallback = GapRecorder(self.poller, self.db_path)
        await self._fallback.start()
        self._running = True

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        if self._fallback is not None:
            await self._fallback.stop()

        if self._proc is not None:
            try:
                if self._proc.stdin and not self._proc.stdin.closed:
                    self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                try:
                    self._proc.wait(timeout=2)
                except Exception:
                    pass
            self._proc = None

    # ---------- main loop ----------
    async def _loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('[gap_recorder_rust] tick err: %s', exc)
                # if subprocess died mid-flight, attempt fallback
                if self._proc is not None and self._proc.poll() is not None:
                    logger.error(
                        '[gap_recorder_rust] subprocess died rc=%s; switching to fallback',
                        self._proc.returncode,
                    )
                    self._proc = None
                    try:
                        await self._start_fallback()
                    except Exception:
                        logger.exception('[gap_recorder_rust] fallback also failed')
                    return
            await asyncio.sleep(self.interval_sec)

    async def _tick(self) -> None:
        state = getattr(self.poller, 'state', None)
        if not state:
            return
        if self._proc is None or self._proc.stdin is None:
            return
        now = int(time.time())

        rows: list[bytes] = []
        for ticker, gap_result in state.items():
            if gap_result is None:
                continue
            bithumb_ask = getattr(getattr(gap_result, 'bithumb', None), 'ask', None)
            usdt_krw = getattr(getattr(gap_result, 'bithumb', None), 'usdt_krw_last', None)
            exchanges = getattr(gap_result, 'exchanges', None) or {}
            for ex_name, ex_data in exchanges.items():
                spot_gap = getattr(ex_data, 'spot_gap', None)
                futures_gap = getattr(ex_data, 'futures_gap', None)
                if spot_gap is None and futures_gap is None:
                    continue
                futures_bid = None
                fb = getattr(ex_data, 'futures_bbo', None)
                if fb is not None:
                    futures_bid = getattr(fb, 'bid', None)
                rows.append(
                    (json.dumps({
                        'ts': now,
                        'ticker': ticker,
                        'exchange': ex_name,
                        'spot_gap': spot_gap,
                        'futures_gap': futures_gap,
                        'bithumb_ask': bithumb_ask,
                        'futures_bid_usdt': futures_bid,
                        'usdt_krw': usdt_krw,
                    }, ensure_ascii=False) + '\n').encode('utf-8')
                )

        if not rows:
            return

        payload = b''.join(rows)
        # run blocking write in executor; stdin write can block if pipe full
        async with self._stdin_lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._write_sync, payload)
        self._tick_count += 1
        self._last_rows = len(rows)

    def _write_sync(self, payload: bytes) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError('subprocess stdin unavailable')
        try:
            self._proc.stdin.write(payload)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise RuntimeError(f'subprocess stdin write failed: {e}') from e

    # ---------- public status/stats API ----------
    def stats(self, hours: int = 24, min_samples: int = 10) -> list[dict[str, Any]]:
        """Rust daemon does not expose SQL aggregates over /stats. For backward
        compat we proxy to the Python impl which opens the same db read-only."""
        if self._fallback is not None:
            return self._fallback.stats(hours, min_samples)
        # transient Python reader against the same file
        try:
            tmp = GapRecorder(self.poller, self.db_path)
            return tmp.stats(hours, min_samples)
        except Exception as exc:
            logger.warning('[gap_recorder_rust] stats fallback err: %s', exc)
            return []

    def _query_rust_stats(self) -> Optional[dict[str, Any]]:
        try:
            url = f'http://{self.stats_bind}/stats'
            with urllib.request.urlopen(url, timeout=1.0) as r:
                return json.loads(r.read().decode())
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def status(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            'running': self._running,
            'enabled': True,
            'interval_sec': self.interval_sec,
            'retention_hours': self.retention_hours,
            'db_path': str(self.db_path),
            'backend': 'rust' if self._proc is not None and self._fallback is None else (
                'fallback-python' if self._fallback is not None else 'unknown'
            ),
            'binary': str(self._binary) if self._binary else None,
            'subprocess_pid': self._proc.pid if self._proc else None,
            'stats_bind': self.stats_bind,
            'tick_count': self._tick_count,
            'last_tick_rows': self._last_rows,
        }
        if self._fallback is not None:
            return {**base, **self._fallback.status()}
        r = self._query_rust_stats()
        if r:
            base['rust_stats'] = r
            base['total_inserts'] = r.get('total_inserts', 0)
        return base

    # close() alias for any legacy callers
    async def close(self) -> None:
        await self.stop()


if __name__ == '__main__':
    # self-check: look up binary, print status
    logging.basicConfig(level=logging.INFO)
    b = _find_binary()
    print(f'binary: {b}')
    if b is None:
        print('no rust binary found - fallback would be used')
        sys.exit(0)
    print('binary found, ready for use')
