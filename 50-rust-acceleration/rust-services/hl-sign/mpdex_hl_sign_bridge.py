"""Drop-in compatibility shim.

Tries the Rust extension (`mpdex_hl_sign`). If import succeeds, exposes
`sign_l1_action(wallet, action, active_pool, nonce, expires_after, is_mainnet)`
that transparently calls the Rust implementation. On any failure or unexpected
input, falls back to the original Python implementation in
`mpdex.exchanges.hl_sign`.

Opt-in, non-breaking. To activate, change the import in hyperliquid.py from:
    from mpdex.exchanges.hl_sign import sign_l1_action
to:
    from mpdex.exchanges.mpdex_hl_sign_bridge import sign_l1_action
"""
from __future__ import annotations
from typing import Any, Dict, Optional

from . import hl_sign as _py_impl

try:
    import mpdex_hl_sign as _rust_impl
    _RUST_OK = True
except Exception:
    _rust_impl = None
    _RUST_OK = False


def _wallet_private_key(wallet) -> Optional[str]:
    # eth_account LocalAccount exposes `.key` as HexBytes
    for attr in ("key", "_private_key"):
        if hasattr(wallet, attr):
            k = getattr(wallet, attr)
            try:
                if hasattr(k, "hex"):
                    return "0x" + k.hex()
                return str(k)
            except Exception:
                continue
    return None


def sign_l1_action(wallet,
                   action: Dict[str, Any],
                   active_pool: Optional[str],
                   nonce: int,
                   expires_after: Optional[int],
                   is_mainnet: bool) -> Dict[str, Any]:
    if _RUST_OK:
        pk = _wallet_private_key(wallet)
        if pk is not None:
            try:
                return _rust_impl.sign_l1_action(
                    pk, action, active_pool, int(nonce),
                    int(expires_after) if expires_after is not None else None,
                    bool(is_mainnet),
                )
            except Exception:
                pass
    return _py_impl.sign_l1_action(wallet, action, active_pool, nonce, expires_after, is_mainnet)


# Passthroughs for the rest of the Python module surface
action_hash = _py_impl.action_hash
construct_phantom_agent = _py_impl.construct_phantom_agent
l1_payload = _py_impl.l1_payload
sign_inner = _py_impl.sign_inner
sign_approve_builder_fee = _py_impl.sign_approve_builder_fee
sign_user_signed_action = _py_impl.sign_user_signed_action
