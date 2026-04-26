"""Benchmark harness for gap-recorder.

Spawns the release binary, pipes `rows` LDJSON lines at `rate` rows/sec (or as
fast as possible if rate=0), then polls /stats for latency/throughput stats.

Run:
    python benches/bench_throughput.py --rows 50000 --rate 5000 --db bench.db
"""

import argparse
import json
import os
import pathlib
import random
import subprocess
import sys
import time
import urllib.request


def build_line(i: int) -> bytes:
    row = {
        "ts": 1_700_000_000 + i,
        "ticker": f"T{i % 20}",
        "exchange": random.choice(["binance", "bybit", "okx", "bitget"]),
        "spot_gap": 10000.0 + random.random() * 300 - 150,
        "futures_gap": 9900.0 + random.random() * 400 - 200,
        "bithumb_ask": 100.0 + random.random(),
        "futures_bid_usdt": 0.99 + random.random() * 0.01,
        "usdt_krw": 1350.0 + random.random() * 5,
    }
    return (json.dumps(row) + "\n").encode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", required=True)
    ap.add_argument("--db", default="bench_gap.db")
    ap.add_argument("--rows", type=int, default=50_000)
    ap.add_argument("--rate", type=int, default=5000, help="rows/sec; 0=unbounded")
    ap.add_argument("--port", type=int, default=38900)
    args = ap.parse_args()

    # ensure clean db
    db = pathlib.Path(args.db).resolve()
    if db.exists():
        db.unlink()
    # wal/shm files
    for suf in ("-wal", "-shm"):
        p = pathlib.Path(str(db) + suf)
        if p.exists():
            p.unlink()

    bind = f"127.0.0.1:{args.port}"
    env = os.environ.copy()
    env["GAP_RECORDER_DB"] = str(db)
    env["GAP_RECORDER_STATS_BIND"] = bind
    env["GAP_RECORDER_FLUSH_ROWS"] = "200"
    env["GAP_RECORDER_FLUSH_MS"] = "500"
    env["GAP_RECORDER_PRUNE_SEC"] = "86400"
    env["RUST_LOG"] = "warn"

    print(f"[bench] spawning {args.binary}")
    p = subprocess.Popen(
        [args.binary],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
    )

    # wait for stats endpoint
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://{bind}/health", timeout=0.5).read()
            break
        except Exception:
            time.sleep(0.05)
    else:
        print("[bench] FAIL: server never started")
        p.kill()
        return 1

    print(f"[bench] streaming {args.rows} rows at {'unbounded' if args.rate == 0 else f'{args.rate} rows/sec'}")

    written = 0
    rss_samples = []

    # approximate pacing: sleep every 1000 rows
    rate = args.rate
    t0 = time.perf_counter()
    try:
        for i in range(args.rows):
            p.stdin.write(build_line(i))
            written += 1
            if rate > 0 and i > 0 and i % 1000 == 0:
                expected = i / rate
                actual = time.perf_counter() - t0
                if actual < expected:
                    time.sleep(expected - actual)
        p.stdin.flush()
    except BrokenPipeError:
        print("[bench] FAIL: broken pipe (binary died?)")
        print(p.stderr.read().decode(errors="replace"))
        return 1

    # close stdin to flush final batch but don't kill process yet
    # let it commit then sample stats
    p.stdin.close()
    elapsed = time.perf_counter() - t0
    print(f"[bench] wrote {written} rows in {elapsed:.2f}s (producer-side)")

    # give it up to 5s to drain
    drain_deadline = time.time() + 5
    last = None
    while time.time() < drain_deadline:
        try:
            resp = urllib.request.urlopen(f"http://{bind}/stats", timeout=1).read()
            last = json.loads(resp)
            if last.get("rows_in_db", 0) >= written:
                break
        except Exception:
            pass
        time.sleep(0.1)

    if last is None:
        print("[bench] FAIL: /stats unreachable")
        p.kill()
        return 1

    # try to grab RSS via tasklist (Windows) or ps (Unix)
    rss_mb = None
    try:
        if sys.platform == "win32":
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {p.pid}", "/FO", "CSV", "/NH"],
                stderr=subprocess.DEVNULL,
            ).decode(errors="replace")
            # e.g. "gap-recorder.exe","1234","Console","1","12,345 K"
            parts = out.strip().split('","')
            if len(parts) >= 5:
                rss_mb = float(parts[4].replace(",", "").replace(" K", "").replace('"', "")) / 1024.0
        else:
            out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(p.pid)]).decode()
            rss_mb = float(out.strip()) / 1024.0
    except Exception:
        pass

    p.kill()
    p.wait(timeout=3)

    print()
    print("=== benchmark result ===")
    print(f"  rows written:        {written}")
    print(f"  rows_in_db:          {last.get('rows_in_db')}")
    print(f"  total_inserts:       {last.get('total_inserts')}")
    print(f"  recent rows/sec:     {last.get('recent_rows_per_sec'):.1f}")
    print(f"  effective rows/sec:  {written / elapsed:.1f}")
    print(f"  p50 insert latency:  {last.get('p50_insert_us')} us")
    print(f"  p99 insert latency:  {last.get('p99_insert_us')} us")
    print(f"  parse_errors:        {last.get('parse_errors')}")
    print(f"  insert_errors:       {last.get('insert_errors')}")
    print(f"  db size MB:          {last.get('db_size_mb'):.2f}")
    if rss_mb is not None:
        print(f"  RSS MB:              {rss_mb:.1f}")
    else:
        print(f"  RSS MB:              (unavailable)")
    print()
    return 0 if last.get("rows_in_db", 0) == written else 2


if __name__ == "__main__":
    sys.exit(main())
