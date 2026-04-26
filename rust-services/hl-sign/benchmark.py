#!/usr/bin/env python3
"""Compare throughput + latency: Python hl_sign vs Rust mpdex_hl_sign."""
import json
import sys
import time

sys.path.insert(0, "<INSTALL_DIR>/multi-perp-dex/mpdex/exchanges")

import mpdex_hl_sign as rust
from hl_sign import sign_l1_action as py_sign
from eth_account import Account


def main():
    with open("vectors.json") as f:
        vectors = json.load(f)

    N = 2000
    base = vectors[:20]
    pk_to_acct = {v["private_key"]: Account.from_key(v["private_key"]) for v in base}

    # warm up
    for v in base:
        py_sign(pk_to_acct[v["private_key"]], v["action"], v["vault_address"],
                v["nonce"], v["expires_after"], v["is_mainnet"])
        rust.sign_l1_action(v["private_key"], v["action"], v["vault_address"],
                            v["nonce"], v["expires_after"], v["is_mainnet"])

    t0 = time.perf_counter()
    py_lat = []
    for i in range(N):
        v = base[i % len(base)]
        t1 = time.perf_counter()
        py_sign(pk_to_acct[v["private_key"]], v["action"], v["vault_address"],
                v["nonce"], v["expires_after"], v["is_mainnet"])
        py_lat.append(time.perf_counter() - t1)
    py_total = time.perf_counter() - t0

    t0 = time.perf_counter()
    rust_lat = []
    for i in range(N):
        v = base[i % len(base)]
        t1 = time.perf_counter()
        rust.sign_l1_action(v["private_key"], v["action"], v["vault_address"],
                            v["nonce"], v["expires_after"], v["is_mainnet"])
        rust_lat.append(time.perf_counter() - t1)
    rust_total = time.perf_counter() - t0

    def pct(xs, p):
        s = sorted(xs)
        k = int(len(s) * p)
        return s[min(k, len(s) - 1)]

    print(f"Iterations: {N}")
    print(f"Python   total {py_total:.3f}s  sig/s {N/py_total:8.0f}  p50 {pct(py_lat,0.5)*1e6:7.1f}us  p99 {pct(py_lat,0.99)*1e6:7.1f}us")
    print(f"Rust     total {rust_total:.3f}s  sig/s {N/rust_total:8.0f}  p50 {pct(rust_lat,0.5)*1e6:7.1f}us  p99 {pct(rust_lat,0.99)*1e6:7.1f}us")
    print(f"Speedup: {py_total/rust_total:.1f}x")


if __name__ == "__main__":
    main()
