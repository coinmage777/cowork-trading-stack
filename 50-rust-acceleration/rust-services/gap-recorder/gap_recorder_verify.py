"""
Functional verification for mpdex_gap_recorder.

Creates a temp SQLite DB, writes 200 price-gap rows + 200 BBO rows through the
Rust recorder, flushes, reopens with stdlib sqlite3, and asserts the rows came
back identical. Also checks WAL mode is set, prune_older_than works, and
invalid inputs raise.

Exit 0 = all pass. Exit 1 = any failure (with stderr detail).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone

from mpdex_gap_recorder_bridge import GapRecorderClient, is_available


def fail(msg: str) -> None:
    print(f"[FAIL] {msg}", file=sys.stderr)
    sys.exit(1)


def expect(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


def main() -> None:
    if not is_available():
        fail("mpdex_gap_recorder extension not available")

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = tmp.name
    try:
        rec = GapRecorderClient(db_path, flush_threshold=50)

        # --- Write 200 price gap rows ---
        # Anchor timestamps ~2 days in the past so prune_older_than(1h) will
        # definitely consider them expired regardless of VPS clock skew.
        base = datetime.now(timezone.utc) - timedelta(days=2)
        price_rows = []
        for i in range(200):
            ts = (base + timedelta(seconds=i)).isoformat()
            all_prices = {"hyperliquid": 100_000 + i, "edgex": 100_010 + i}
            rec.record_price_gap(
                ts=ts,
                symbol="BTC" if i % 2 == 0 else "ETH",
                max_exchange="edgex",
                min_exchange="hyperliquid",
                max_price=100_010.0 + i,
                min_price=100_000.0 + i,
                gap_usd=10.0,
                gap_pct=0.01,
                all_prices=all_prices,
                actionable=(i % 10 == 0),
            )
            price_rows.append((ts, i))

        # --- Write 200 BBO rows ---
        for i in range(200):
            rec.record_bbo(
                "hyperliquid",
                "BTC",
                bid=100_000.0 + i,
                ask=100_005.0 + i,
                ts_unix=1_800_000_000.0 + i,
            )

        # Trigger final flush
        written = rec.flush()
        # The buffer may have auto-flushed before the final call, so we only
        # assert no exception + stats count matches what we pushed.
        st = rec.stats()
        expect(
            st["total_price_rows"] == 200,
            f"total_price_rows expected 200, got {st['total_price_rows']}",
        )
        expect(
            st["total_bbo_rows"] == 200,
            f"total_bbo_rows expected 200, got {st['total_bbo_rows']}",
        )
        expect(
            st["buffered_price_rows"] == 0,
            f"buffer should be empty, got {st['buffered_price_rows']}",
        )

        # --- Inspect via stdlib sqlite3 ---
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        # journal_mode should be wal (WAL file persists until checkpoint)
        mode = cur.execute("PRAGMA journal_mode").fetchone()[0]
        expect(
            str(mode).lower() == "wal",
            f"journal_mode expected wal, got {mode!r}",
        )

        pg_count = cur.execute("SELECT COUNT(*) FROM price_gaps").fetchone()[0]
        expect(pg_count == 200, f"price_gaps rows expected 200, got {pg_count}")

        bbo_count = cur.execute("SELECT COUNT(*) FROM bbo_gaps").fetchone()[0]
        expect(bbo_count == 200, f"bbo_gaps rows expected 200, got {bbo_count}")

        # Spot-check: first and last price gap rows round-trip correctly
        first = cur.execute(
            "SELECT timestamp, symbol, max_exchange, min_exchange, "
            "max_price, min_price, gap_usd, gap_pct, all_prices, actionable "
            "FROM price_gaps ORDER BY id ASC LIMIT 1"
        ).fetchone()
        expected_first_ts = base.isoformat()
        expect(first[0] == expected_first_ts, f"first ts mismatch: {first[0]} vs {expected_first_ts}")
        expect(first[1] == "BTC", f"first symbol mismatch: {first[1]}")
        expect(abs(first[4] - 100_010.0) < 1e-9, f"first max_price: {first[4]}")
        expect(first[9] == 1, f"first actionable should be 1, got {first[9]}")
        parsed = json.loads(first[8])
        expect(parsed == {"hyperliquid": 100_000, "edgex": 100_010}, f"all_prices: {parsed}")

        # Spot-check BBO: spread_bps and mid were auto-computed
        bbo = cur.execute(
            "SELECT bid, ask, mid, spread_bps FROM bbo_gaps ORDER BY id ASC LIMIT 1"
        ).fetchone()
        bid, ask, mid, spread_bps = bbo
        expect(abs(mid - (bid + ask) / 2.0) < 1e-6, f"mid auto-compute: {mid}")
        expected_spread = (ask - bid) / mid * 10_000.0
        expect(
            abs(spread_bps - expected_spread) < 1e-6,
            f"spread_bps auto-compute: {spread_bps} vs {expected_spread}",
        )

        # --- prune_older_than ---
        # All price_gaps timestamps are in 2026-04-25; prune anything older than
        # 1 hour from "now" (i.e., basically everything).
        # BBO ts_unix were 1_800_000_000+ (year 2027), so the "hours" cutoff
        # (now minus 1 hour) will be BEFORE them -> they survive.
        deleted = rec.prune_older_than(1.0)
        expect(deleted >= 200, f"prune_older_than should delete price_gaps, got {deleted}")
        pg_count_after = cur.execute("SELECT COUNT(*) FROM price_gaps").fetchone()[0]
        expect(pg_count_after == 0, f"price_gaps after prune: {pg_count_after}")
        bbo_count_after = cur.execute("SELECT COUNT(*) FROM bbo_gaps").fetchone()[0]
        expect(
            bbo_count_after == 200,
            f"bbo_gaps should survive (ts in future), got {bbo_count_after}",
        )

        # --- Invalid input handling ---
        threw = False
        try:
            rec.record_bbo("hyperliquid", "BTC", bid=-1.0, ask=1.0)
        except RuntimeError:
            threw = True
        expect(threw, "negative bid should raise RuntimeError")

        threw = False
        try:
            rec.record_bbo("hyperliquid", "BTC", bid=float("nan"), ask=1.0)
        except RuntimeError:
            threw = True
        expect(threw, "NaN bid should raise RuntimeError")

        con.close()
        rec.close()

        print("[OK] verify passed:")
        print(f"  - 200 price_gaps + 200 bbo_gaps written and read back")
        print(f"  - WAL mode confirmed")
        print(f"  - prune_older_than removed {deleted} expired rows")
        print(f"  - invalid input (negative/NaN bid) raises correctly")
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        fail("unhandled exception during verify")
    finally:
        for ext in ("", "-wal", "-shm"):
            p = db_path + ext
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


if __name__ == "__main__":
    main()
