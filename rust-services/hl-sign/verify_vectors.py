#!/usr/bin/env python3
"""Load vectors.json, call Rust module, compare byte-for-byte with Python reference."""
import json
import sys

sys.path.insert(0, "<INSTALL_DIR>/multi-perp-dex/mpdex/exchanges")

import mpdex_hl_sign as rust


def main(path="vectors.json"):
    with open(path) as f:
        vectors = json.load(f)

    hash_mismatches = []
    sig_mismatches = []

    for i, v in enumerate(vectors):
        expected_hash = bytes.fromhex(v["expected_action_hash"][2:])
        rust_hash = rust.action_hash(v["action"], v["vault_address"], v["nonce"], v["expires_after"])
        if rust_hash != expected_hash:
            hash_mismatches.append((i, expected_hash.hex(), rust_hash.hex()))

        rust_sig = rust.sign_l1_action(
            v["private_key"],
            v["action"],
            v["vault_address"],
            v["nonce"],
            v["expires_after"],
            v["is_mainnet"],
        )
        if (rust_sig["r"] != v["expected_r"]
                or rust_sig["s"] != v["expected_s"]
                or rust_sig["v"] != v["expected_v"]):
            sig_mismatches.append((i, v["expected_r"], v["expected_s"], v["expected_v"],
                                   rust_sig["r"], rust_sig["s"], rust_sig["v"]))

    total = len(vectors)
    print(f"Total vectors: {total}")
    print(f"action_hash mismatches: {len(hash_mismatches)}")
    print(f"signature mismatches: {len(sig_mismatches)}")
    for m in hash_mismatches[:5]:
        print(f"HASH MISMATCH idx={m[0]}")
        print(f"  expected: {m[1]}")
        print(f"  got:      {m[2]}")
    for m in sig_mismatches[:5]:
        print(f"SIG MISMATCH idx={m[0]}")
        print(f"  expected r={m[1]} s={m[2]} v={m[3]}")
        print(f"  got      r={m[4]} s={m[5]} v={m[6]}")

    if hash_mismatches or sig_mismatches:
        sys.exit(1)
    print(f"\nALL {total} VECTORS MATCH (action_hash + signatures)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "vectors.json")
