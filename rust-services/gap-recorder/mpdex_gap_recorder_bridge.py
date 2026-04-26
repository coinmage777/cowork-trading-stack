"""
mpdex_gap_recorder_bridge
--------------------------
Thin Python wrapper around the `mpdex_gap_recorder` Rust extension.

Two use-cases:
  (a) Drop-in `price_gaps` producer for funding_rates.db
      (matches funding_collector's INSERT but batched + WAL-tuned).
  (b) High-frequency BBO recorder (tick-level bid/ask snapshots)
      — writes to a new `bbo_gaps` table that the Rust module creates on first
      open. Readers on `funding_rates.db` (funding_simulator, funding_arb,
      dashboards) are unaffected.

Usage:
    from mpdex_gap_recorder_bridge import GapRecorderClient

    rec = GapRecorderClient("<INSTALL_DIR>/multi-perp-dex/funding_rates.db")
    rec.record_price_gap(
        ts="2026-04-25T10:00:00+00:00",
        symbol="BTC",
        max_exchange="edgex", min_exchange="hyperliquid",
        max_price=101_250.0, min_price=101_200.0,
        gap_usd=50.0, gap_pct=0.0493,
        all_prices={"hyperliquid": 101200, "edgex": 101250},
        actionable=False,
    )
    rec.record_bbo("hyperliquid", "BTC", bid=101_199.5, ask=101_200.5)
    rec.flush()

Fallback behaviour: if the Rust extension isn't installed, the shim raises a
clear ImportError at construction time (no silent Python fallback — caller
must handle it). This is intentional: if the Rust path is missing in prod we
want a loud failure, not a 7000x throughput regression.
"""
from __future__ import annotations

import json
import time
from typing import Any, Mapping, Optional

try:
    from mpdex_gap_recorder import GapRecorder as _RustRecorder  # type: ignore
    _AVAILABLE = True
    _IMPORT_ERR: Optional[Exception] = None
except Exception as exc:  # pragma: no cover - exercised only when wheel missing
    _RustRecorder = None  # type: ignore
    _AVAILABLE = False
    _IMPORT_ERR = exc


def is_available() -> bool:
    """Returns True if the Rust extension loaded successfully."""
    return _AVAILABLE


class GapRecorderClient:
    """Batched SQLite recorder; see module docstring."""

    def __init__(self, db_path: str, flush_threshold: int = 1000) -> None:
        if not _AVAILABLE:
            raise ImportError(
                "mpdex_gap_recorder extension is not installed. Build it with "
                "`maturin build --release` under rust-services/gap-recorder/, "
                f"then `pip install` the wheel. Original import error: {_IMPORT_ERR!r}"
            )
        if flush_threshold <= 0:
            raise ValueError("flush_threshold must be > 0")
        self._rec = _RustRecorder(db_path, flush_threshold)
        self._db_path = db_path
        self._closed = False

    @property
    def db_path(self) -> str:
        return self._db_path

    # ---- price_gaps (drop-in for funding_collector) ----

    def record_price_gap(
        self,
        *,
        ts: str,
        symbol: str,
        max_exchange: str,
        min_exchange: str,
        max_price: float,
        min_price: float,
        gap_usd: float,
        gap_pct: float,
        all_prices: Optional[Mapping[str, Any]] = None,
        actionable: bool = False,
    ) -> None:
        serialized = json.dumps(all_prices) if all_prices is not None else None
        self._rec.record_price_gap(
            ts,
            symbol,
            max_exchange,
            min_exchange,
            float(max_price),
            float(min_price),
            float(gap_usd),
            float(gap_pct),
            serialized,
            bool(actionable),
        )

    # ---- bbo_gaps (high-frequency BBO snapshots) ----

    def record_bbo(
        self,
        exchange: str,
        symbol: str,
        *,
        bid: float,
        ask: float,
        ts_unix: Optional[float] = None,
        mid: Optional[float] = None,
        spread_bps: Optional[float] = None,
    ) -> None:
        ts = float(ts_unix) if ts_unix is not None else time.time()
        self._rec.record_bbo(
            ts, exchange, symbol, float(bid), float(ask), mid, spread_bps
        )

    # ---- control ----

    def flush(self) -> int:
        return int(self._rec.flush())

    def prune_older_than(self, hours: float) -> int:
        if hours <= 0:
            raise ValueError("hours must be > 0")
        return int(self._rec.prune_older_than(float(hours)))

    def stats(self) -> dict:
        total_price, total_bbo, buf_price, buf_bbo = self._rec.stats()
        return {
            "total_price_rows": int(total_price),
            "total_bbo_rows": int(total_bbo),
            "buffered_price_rows": int(buf_price),
            "buffered_bbo_rows": int(buf_bbo),
        }

    def close(self) -> None:
        if self._closed:
            return
        self._rec.close()
        self._closed = True

    def __enter__(self) -> "GapRecorderClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self) -> None:  # best-effort flush on GC
        try:
            self.close()
        except Exception:
            pass


__all__ = ["GapRecorderClient", "is_available"]
