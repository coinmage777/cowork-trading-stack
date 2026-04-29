#!/usr/bin/env python3
"""Generate 1000 additional random (but deterministic) real-order-like actions
and verify bit-for-bit match Python vs Rust."""
import sys
import random
import hashlib
import json

sys.path.insert(0, "<INSTALL_DIR>/multi-perp-dex/mpdex/exchanges")

import mpdex_hl_sign as rust
from hl_sign import sign_l1_action as py_sign, action_hash as py_hash
from eth_account import Account


def deterministic_key(i: int) -> str:
    return "0x" + hashlib.sha256(f"replay-{i}".encode()).hexdigest()


def sample(i, rng):
    """Broader sample: every shape seen in real Hyperliquid orders."""
    variant = i % 12
    if variant == 0:
        return {
            "type": "order",
            "orders": [{
                "a": rng.randint(0, 300),
                "b": rng.choice([True, False]),
                "p": f"{rng.uniform(0.0001, 200000):.{rng.randint(2,6)}f}",
                "s": f"{rng.uniform(0.000001, 100):.{rng.randint(3,8)}f}",
                "r": rng.choice([True, False]),
                "t": {"limit": {"tif": rng.choice(["Gtc", "Ioc", "Alo", "FrontendMarket"])}},
            }],
            "grouping": rng.choice(["na", "normalTpsl", "positionTpsl"]),
        }
    if variant == 1:
        n = rng.randint(2, 8)
        return {
            "type": "order",
            "orders": [{
                "a": rng.randint(0, 100),
                "b": rng.choice([True, False]),
                "p": f"{rng.uniform(1, 100000):.4f}",
                "s": f"{rng.uniform(0.001, 50):.4f}",
                "r": False,
                "t": {"limit": {"tif": "Gtc"}},
            } for _ in range(n)],
            "grouping": "na",
        }
    if variant == 2:
        return {
            "type": "cancel",
            "cancels": [{"a": rng.randint(0, 200), "o": rng.randint(1, 10**12)}],
        }
    if variant == 3:
        return {
            "type": "cancelByCloid",
            "cancels": [{"asset": rng.randint(0, 100),
                         "cloid": f"0x{rng.getrandbits(128):032x}"}],
        }
    if variant == 4:
        n = rng.randint(2, 10)
        return {
            "type": "cancel",
            "cancels": [{"a": rng.randint(0, 50), "o": rng.randint(1, 10**12)} for _ in range(n)],
        }
    if variant == 5:
        return {
            "type": "order",
            "orders": [{
                "a": rng.randint(0, 100),
                "b": True,
                "p": f"{rng.uniform(100, 60000):.2f}",
                "s": f"{rng.uniform(0.001, 1):.5f}",
                "r": False,
                "t": {"limit": {"tif": "Gtc"}},
                "c": f"0x{rng.getrandbits(128):032x}",
            }],
            "grouping": "na",
            "builder": {"b": f"0x{rng.getrandbits(160):040x}", "f": rng.randint(0, 100)},
        }
    if variant == 6:
        return {
            "type": "order",
            "orders": [{
                "a": rng.randint(0, 50),
                "b": False,
                "p": f"{rng.uniform(100, 5000):.2f}",
                "s": f"{rng.uniform(0.01, 3):.4f}",
                "r": True,
                "t": {"trigger": {
                    "isMarket": rng.choice([True, False]),
                    "triggerPx": f"{rng.uniform(100, 5000):.2f}",
                    "tpsl": rng.choice(["tp", "sl"]),
                }},
            }],
            "grouping": "normalTpsl",
        }
    if variant == 7:
        return {"type": "scheduleCancel", "time": rng.randint(10**12, 2 * 10**13)}
    if variant == 8:
        # Hyperliquid HIP-3 asset ids (>=140000)
        return {
            "type": "order",
            "orders": [{
                "a": 140000 + rng.randint(0, 50),
                "b": rng.choice([True, False]),
                "p": f"{rng.uniform(100, 70000):.3f}",
                "s": f"{rng.uniform(0.001, 5):.4f}",
                "r": False,
                "t": {"limit": {"tif": "Gtc"}},
            }],
            "grouping": "na",
        }
    if variant == 9:
        # updateLeverage
        return {
            "type": "updateLeverage",
            "asset": rng.randint(0, 100),
            "isCross": rng.choice([True, False]),
            "leverage": rng.randint(1, 50),
        }
    if variant == 10:
        # updateIsolatedMargin
        return {
            "type": "updateIsolatedMargin",
            "asset": rng.randint(0, 100),
            "isBuy": rng.choice([True, False]),
            "ntli": rng.randint(-10**8, 10**8),
        }
    # variant 11: twap order
    return {
        "type": "twapOrder",
        "twap": {
            "a": rng.randint(0, 50),
            "b": rng.choice([True, False]),
            "s": f"{rng.uniform(0.01, 20):.4f}",
            "r": False,
            "m": rng.randint(5, 1440),
            "t": rng.choice([True, False]),
        },
    }


def main():
    rng = random.Random(1000)
    n_ok = 0
    hash_fails = []
    sig_fails = []
    for i in range(1000):
        pk = deterministic_key(i)
        acct = Account.from_key(pk)
        act = sample(i, rng)
        vault = None
        if i % 9 == 4:
            vault = "0x" + hashlib.sha256(f"vault-R{i}".encode()).hexdigest()[:40]
        nonce = 10**12 + i * 91 + rng.randint(0, 10_000)
        expires = None
        if i % 17 == 11:
            expires = nonce + rng.randint(60_000, 3_600_000)
        mainnet = (i % 3) != 0

        py_h = py_hash(act, vault, nonce, expires)
        rs_h = rust.action_hash(act, vault, nonce, expires)
        if py_h != rs_h:
            hash_fails.append((i, py_h.hex(), rs_h.hex()))
            continue

        py_sig = py_sign(acct, act, vault, nonce, expires, mainnet)
        rs_sig = rust.sign_l1_action(pk, act, vault, nonce, expires, mainnet)
        if py_sig != rs_sig:
            sig_fails.append((i, py_sig, rs_sig))
            continue
        n_ok += 1

    print(f"OK: {n_ok}/1000")
    print(f"hash mismatches: {len(hash_fails)}")
    print(f"sig mismatches: {len(sig_fails)}")
    for f in hash_fails[:3]:
        print("HASH", f)
    for f in sig_fails[:3]:
        print("SIG", f)
    if hash_fails or sig_fails:
        sys.exit(1)


if __name__ == "__main__":
    main()
