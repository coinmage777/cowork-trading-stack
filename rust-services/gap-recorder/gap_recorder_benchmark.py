"""
Benchmark for mpdex_gap_recorder — measures rows/sec for BBO ingest against a
pure-Python sqlite3 baseline. Intentionally modest target (>= 50K rows/sec on
VPS hardware); previous session's "298K rows/sec" claim wasn't reproducible.

Run:
    python gap_recorder_benchmark.py [--rows 200000] [--threshold 2000]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
import time
from statistics import median

from mpdex_gap_recorder_bridge import GapRecorderClient


def bench_rust(db_path: str, n_rows: int, threshold: int) -> tuple[float, float]:
    """Returns (elapsed_seconds, rows_per_sec)."""
    rec = GapRecorderClient(db_path, flush_threshold=threshold)
    start = time.perf_counter()
    for i in range(n_rows):
        rec.record_bbo(
            "hyperliquid",
            "BTC",
            bid=100_000.0 + (i % 1000) * 0.5,
            ask=100_001.0 + (i % 1000) * 0.5,
            ts_unix=1_700_000_000.0 + i * 0.001,
        )
    rec.flush()
    elapsed = time.perf_counter() - start
    rec.close()
    return elapsed, n_rows / elapsed if elapsed > 0 else float("inf")


def bench_python_baseline(db_path: str, n_rows: int, batch: int) -> tuple[float, float]:
    """Pure-Python sqlite3 with executemany batching — the realistic baseline
    for comparing against (naive per-row commit would be ~40 rows/sec)."""
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute(
        "CREATE TABLE IF NOT EXISTS bbo_bench ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts_unix REAL, exchange TEXT, "
        "symbol TEXT, bid REAL, ask REAL, mid REAL, spread_bps REAL)"
    )
    con.commit()

    start = time.perf_counter()
    buf: list[tuple] = []
    for i in range(n_rows):
        bid = 100_000.0 + (i % 1000) * 0.5
        ask = 100_001.0 + (i % 1000) * 0.5
        mid = (bid + ask) / 2
        spread_bps = (ask - bid) / mid * 10_000.0
        buf.append(
            (1_700_000_000.0 + i * 0.001, "hyperliquid", "BTC", bid, ask, mid, spread_bps)
        )
        if len(buf) >= batch:
            con.executemany(
                "INSERT INTO bbo_bench (ts_unix, exchange, symbol, bid, ask, mid, spread_bps) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                buf,
            )
            con.commit()
            buf.clear()
    if buf:
        con.executemany(
            "INSERT INTO bbo_bench (ts_unix, exchange, symbol, bid, ask, mid, spread_bps) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            buf,
        )
        con.commit()
    elapsed = time.perf_counter() - start
    con.close()
    return elapsed, n_rows / elapsed if elapsed > 0 else float("inf")


def bench_latency(db_path: str, n_samples: int = 10_000) -> dict:
    """Per-call record_bbo latency distribution (no-flush buffering cost)."""
    rec = GapRecorderClient(db_path, flush_threshold=10**9)  # never auto-flush
    latencies_us: list[float] = []
    # Warmup
    for _ in range(500):
        rec.record_bbo("hyperliquid", "BTC", bid=100_000.0, ask=100_001.0,
                       ts_unix=1_700_000_000.0)
    for i in range(n_samples):
        t0 = time.perf_counter_ns()
        rec.record_bbo(
            "hyperliquid",
            "BTC",
            bid=100_000.0 + i * 0.01,
            ask=100_001.0 + i * 0.01,
            ts_unix=1_700_000_000.0 + i * 0.001,
        )
        latencies_us.append((time.perf_counter_ns() - t0) / 1000.0)
    rec.flush()
    rec.close()
    latencies_us.sort()
    return {
        "n": n_samples,
        "p50_us": latencies_us[n_samples // 2],
        "p95_us": latencies_us[int(n_samples * 0.95)],
        "p99_us": latencies_us[int(n_samples * 0.99)],
        "max_us": latencies_us[-1],
        "median_us": median(latencies_us),
    }


def _rm_db(path: str) -> None:
    for ext in ("", "-wal", "-shm"):
        p = path + ext
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=200_000)
    ap.add_argument("--threshold", type=int, default=2000)
    ap.add_argument("--baseline-batch", type=int, default=2000)
    args = ap.parse_args()

    print(f"mpdex_gap_recorder benchmark")
    print(f"  rows           : {args.rows:,}")
    print(f"  flush_threshold: {args.threshold}")
    print()

    # --- Rust throughput ---
    rust_db = tempfile.NamedTemporaryFile(suffix="-rust.db", delete=False).name
    _rm_db(rust_db)
    elapsed, rps = bench_rust(rust_db, args.rows, args.threshold)
    print(f"[Rust  ] {args.rows:>10,} rows in {elapsed:7.3f}s = {rps:>12,.0f} rows/sec")

    # --- Latency distribution ---
    lat_db = tempfile.NamedTemporaryFile(suffix="-lat.db", delete=False).name
    _rm_db(lat_db)
    lat = bench_latency(lat_db, n_samples=min(20_000, args.rows))
    print(
        f"[Rust  ] per-call latency (n={lat['n']:,}): "
        f"p50={lat['p50_us']:.2f}us p95={lat['p95_us']:.2f}us "
        f"p99={lat['p99_us']:.2f}us max={lat['max_us']:.2f}us"
    )

    # --- Python sqlite3 baseline ---
    py_db = tempfile.NamedTemporaryFile(suffix="-py.db", delete=False).name
    _rm_db(py_db)
    elapsed_py, rps_py = bench_python_baseline(py_db, args.rows, args.baseline_batch)
    print(
        f"[Python] {args.rows:>10,} rows in {elapsed_py:7.3f}s = {rps_py:>12,.0f} "
        f"rows/sec (executemany batch={args.baseline_batch})"
    )

    speedup = rps / rps_py if rps_py > 0 else float("inf")
    print()
    print(f"Speedup Rust vs Python: {speedup:.2f}x")

    _rm_db(rust_db)
    _rm_db(lat_db)
    _rm_db(py_db)


if __name__ == "__main__":
    main()
