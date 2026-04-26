# -*- coding: utf-8 -*-
# 공식 SDK 서명 부분만 발췌/정리: trading_service.py에서 import해서 사용
from typing import Any, Optional, Dict, TypedDict
import msgpack
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_hex

# 타입 정의 (필요한 최소만)
class SignatureDict(TypedDict):
    r: str
    s: str
    v: int

def address_to_bytes(address: str) -> bytes:
    # 0x 프리픽스 허용
    return bytes.fromhex(address[2:] if address.startswith(("0x", "0X")) else address)

def action_hash(action: Dict[str, Any],
                vault_address: Optional[str],
                nonce: int,
                expires_after: Optional[int]) -> bytes:
    """
    공식 SDK: msgpack(action) + nonce(8 bytes BE) + (00|01+vault) + (옵션) 00 + expires(8 bytes BE)
    -> keccak(bytes)
    """
    data = msgpack.packb(action)  # dict 삽입순서 보존(파이썬3.7+)
    data += int(nonce).to_bytes(8, "big")
    if vault_address is None:
        data += b"\x00"
    else:
        data += b"\x01"
        data += address_to_bytes(vault_address)
    if expires_after is not None:
        data += b"\x00"
        data += int(expires_after).to_bytes(8, "big")
    return keccak(data)

def construct_phantom_agent(conn_hash: bytes, is_mainnet: bool) -> Dict[str, Any]:
    """
    source: 'a'(mainnet) / 'b'(testnet)
    connectionId: bytes32
    """
    if not isinstance(conn_hash, (bytes, bytearray)) or len(conn_hash) != 32:
        raise ValueError(f"connectionId must be bytes32, got {type(conn_hash)} len={len(conn_hash) if isinstance(conn_hash,(bytes,bytearray)) else 'N/A'}")
    return {"source": "a" if is_mainnet else "b", "connectionId": conn_hash}

def l1_payload(phantom_agent: Dict[str, Any]) -> Dict[str, Any]:
    """
    EIP-712 Agent 타입 페이로드 (SDK 원문)
    """
    return {
        "domain": {
            "chainId": 1337,
            "name": "Exchange",
            "verifyingContract": "<EVM_ADDRESS>",
            "version": "1",
        },
        "types": {
            "Agent": [
                {"name": "source", "type": "string"},
                {"name": "connectionId", "type": "bytes32"},
            ],
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
        },
        "primaryType": "Agent",
        "message": phantom_agent,
    }

def sign_inner(wallet, data: Dict[str, Any]) -> SignatureDict:
    """
    wallet: eth_account.Account.from_key(...) 로 만든 객체
    data: encode_typed_data(full_message=data) 가능한 dict
    반환: {'r': '0x..', 's': '0x..', 'v': 27/28}
    """
    structured = encode_typed_data(full_message=data)
    signed = wallet.sign_message(structured)
    return {"r": to_hex(signed["r"]), "s": to_hex(signed["s"]), "v": int(signed["v"])}

def sign_l1_action(wallet,
                   action: Dict[str, Any],
                   active_pool: Optional[str],
                   nonce: int,
                   expires_after: Optional[int],
                   is_mainnet: bool) -> SignatureDict:
    """
    공식 SDK L1 서명: action_hash → phantomAgent → EIP712(Agent) → 서명
    wallet: Account.from_key(...) 로 생성
    active_pool: vault address(없으면 None)
    """
    h = action_hash(action, active_pool, nonce, expires_after)
    agent = construct_phantom_agent(h, is_mainnet)
    data = l1_payload(agent)
    return sign_inner(wallet, data)

# (선택) User-signed tx 들이 필요하면 아래 함수들도 그대로 노출
def user_signed_payload(primary_type: str, payload_types: list[dict], action: Dict[str, Any]) -> Dict[str, Any]:
    chain_id = int(action["signatureChainId"], 16)
    return {
        "domain": {
            "name": "HyperliquidSignTransaction",
            "version": "1",
            "chainId": chain_id,
            "verifyingContract": "<EVM_ADDRESS>",
        },
        "types": {
            primary_type: payload_types,
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
        },
        "primaryType": primary_type,
        "message": action,
    }

# agent wallet으로 사용 불가능. main wallet으로만 가능함!
def sign_approve_builder_fee(wallet, action, is_mainnet):
    return sign_user_signed_action(
        wallet,
        action,
        [
            {"name": "hyperliquidChain", "type": "string"},
            {"name": "maxFeeRate", "type": "string"},
            {"name": "builder", "type": "address"},
            {"name": "nonce", "type": "uint64"},
        ],
        "HyperliquidTransaction:ApproveBuilderFee",
        is_mainnet,
    )

def sign_user_signed_action(wallet,
                            action: Dict[str, Any],
                            payload_types: list[dict],
                            primary_type: str,
                            is_mainnet: bool) -> SignatureDict:
    """
    공식 SDK와 동일하게 원본 action(dict)을 in-place로 수정하여
    - action["signatureChainId"], action["hyperliquidChain"]을 주입합니다.
    """
    # in-place 주입 (중요!)
    action["signatureChainId"] = "0x66eee"
    action["hyperliquidChain"] = "Mainnet" if is_mainnet else "Testnet"

    data = user_signed_payload(primary_type, payload_types, action)
    return sign_inner(wallet, data)