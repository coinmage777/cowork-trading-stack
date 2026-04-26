#!/usr/bin/env python3
"""Generate 100 deterministic reference vectors for hl_sign compatibility tests."""
import json
import sys
import random
import hashlib
from eth_account import Account

sys.path.insert(0, "<INSTALL_DIR>/multi-perp-dex/mpdex/exchanges")
from hl_sign import sign_l1_action, action_hash


def deterministic_key(i: int) -> str:
    seed = hashlib.sha256(f"hl-sign-ref-{i}".encode()).digest()
    return "0x" + seed.hex()


def sample_action(i: int, rng: random.Random) -> dict:
    variant = i % 8
    if variant == 0:
        return {
            "type": "order",
            "orders": [{
                "a": rng.randint(0, 200),
                "b": rng.choice([True, False]),
                "p": f"{rng.uniform(100, 80000):.4f}",
                "s": f"{rng.uniform(0.0001, 10):.6f}",
                "r": rng.choice([True, False]),
                "t": {"limit": {"tif": rng.choice(["Gtc", "Ioc", "Alo"])}},
            }],
            "grouping": "na",
        }
    elif variant == 1:
        n = rng.randint(2, 5)
        return {
            "type": "order",
            "orders": [{
                "a": rng.randint(0, 50),
                "b": rng.choice([True, False]),
                "p": f"{rng.uniform(10, 5000):.3f}",
                "s": f"{rng.uniform(0.01, 5):.4f}",
                "r": False,
                "t": {"limit": {"tif": "Gtc"}},
            } for _ in range(n)],
            "grouping": "na",
        }
    elif variant == 2:
        return {
            "type": "cancel",
            "cancels": [{"a": rng.randint(0, 100), "o": rng.randint(1, 10**10)}],
        }
    elif variant == 3:
        return {
            "type": "cancelByCloid",
            "cancels": [{"asset": rng.randint(0, 100), "cloid": f"0x{rng.getrandbits(128):032x}"}],
        }
    elif variant == 4:
        n = rng.randint(2, 4)
        return {
            "type": "cancel",
            "cancels": [{"a": rng.randint(0, 50), "o": rng.randint(1, 10**10)} for _ in range(n)],
        }
    elif variant == 5:
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
            "builder": {"b": "0x" + "ab" * 20, "f": 1},
        }
    elif variant == 6:
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
            "grouping": "na",
        }
    else:
        return {"type": "scheduleCancel", "time": rng.randint(10**12, 2 * 10**13)}


def main():
    rng = random.Random(42)
    vectors = []
    for i in range(100):
        pk = deterministic_key(i)
        acct = Account.from_key(pk)
        action = sample_action(i, rng)
        vault = None
        if i % 7 == 3:
            vault = "0x" + hashlib.sha256(f"vault-{i}".encode()).hexdigest()[:40]
        nonce = 10**12 + i * 137 + rng.randint(0, 10_000)
        expires = None
        if i % 11 == 5:
            expires = nonce + 300_000
        is_mainnet = (i % 5) != 0

        h = action_hash(action, vault, nonce, expires)
        sig = sign_l1_action(acct, action, vault, nonce, expires, is_mainnet)

        vectors.append({
            "private_key": pk,
            "action": action,
            "vault_address": vault,
            "nonce": nonce,
            "expires_after": expires,
            "is_mainnet": is_mainnet,
            "expected_action_hash": "0x" + h.hex(),
            "expected_r": sig["r"],
            "expected_s": sig["s"],
            "expected_v": sig["v"],
        })

    json.dump(vectors, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
