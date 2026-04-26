"""
Auto-Claimer — resolved 마켓의 CTF 토큰을 USDC로 자동 redeem

SafeWeb3Service + Relayer API key auth 사용.
claim_venv (Python 3.14, poly-web3) 환경에서 실행.

사용법:
  claim_venv/Scripts/python auto_claimer.py              # 1회 실행
  claim_venv/Scripts/python auto_claimer.py --loop 300   # 5분마다 반복
"""

import json
import logging
import os
import time
import types
from pathlib import Path

import requests
from dotenv import load_dotenv

logger = logging.getLogger("polybot")


def create_claimer():
    """SafeWeb3Service + Relayer API key auth 기반 claimer 생성"""
    from py_builder_relayer_client.exceptions import RelayerApiException
    from py_builder_signing_sdk.config import BuilderConfig
    from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    from poly_web3 import RELAYER_URL, RelayClient, SafeWeb3Service

    creds = ApiCreds(
        api_key=os.getenv("POLYMARKET_API_KEY", ""),
        api_secret=os.getenv("POLYMARKET_SECRET", ""),
        api_passphrase=os.getenv("POLYMARKET_PASSPHRASE", ""),
    )
    clob = ClobClient(
        host=os.getenv("POLYMARKET_API_URL", "https://clob.polymarket.com"),
        chain_id=137,
        key=os.getenv("PRIVATE_KEY", ""),
        creds=creds,
        signature_type=2,
        funder=os.getenv("PROXY_WALLET", ""),
    )

    builder_creds = BuilderApiKeyCreds(
        key=os.getenv("BUILDER_KEY", ""),
        secret=os.getenv("BUILDER_SECRET", ""),
        passphrase=os.getenv("BUILDER_PASSPHRASE", ""),
    )
    relay = RelayClient(
        relayer_url=RELAYER_URL,
        chain_id=137,
        private_key=os.getenv("PRIVATE_KEY", ""),
        builder_config=BuilderConfig(local_builder_creds=builder_creds),
    )

    # Patch relay._post_request to use Relayer API key auth
    relayer_key = os.getenv("RELAYER_API_KEY", "")
    eoa = clob.get_address()

    def patched_post(self, method, request_path, body=None):
        headers = {
            "RELAYER_API_KEY": relayer_key,
            "RELAYER_API_KEY_ADDRESS": eoa,
            "Content-Type": "application/json",
        }
        resp = requests.request(
            method=method,
            url=f"{self.relayer_url}{request_path}",
            headers=headers,
            json=body if body else None,
        )
        if resp.status_code != 200:
            raise RelayerApiException(resp)
        try:
            return resp.json()
        except requests.JSONDecodeError:
            return resp.text

    relay._post_request = types.MethodType(patched_post, relay)

    service = SafeWeb3Service(
        clob_client=clob,
        relayer_client=relay,
        rpc_url="https://polygon-bor-rpc.publicnode.com",
    )

    return service, clob


def get_balance(clob) -> float:
    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=-1)
    bal = clob.get_balance_allowance(params)
    return int(bal.get("balance", "0")) / 1_000_000


def fetch_all_redeemable(user_address: str) -> list[dict]:
    """Fetch redeemable positions without the broken percentPnl > 0 filter.

    poly_web3's fetch_positions filters by percentPnl > 0, but Polymarket API
    reports curPrice=0 for resolved markets, making percentPnl=-100 even for
    winners. This bypass fetches all redeemable positions directly.
    """
    url = "https://data-api.polymarket.com/positions"
    all_positions: list[dict] = []
    offset = 0
    while True:
        params = {
            "user": user_address,
            "sizeThreshold": 0.01,
            "limit": 200,
            "redeemable": True,
            "sortBy": "RESOLVING",
            "sortDirection": "DESC",
            "offset": offset,
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            all_positions.extend(batch)
            if len(batch) < 200:
                break
            offset += 200
        except Exception as e:
            logger.error(f"[CLAIM] fetch_all_redeemable error: {e}")
            break
    return all_positions


def claim_all(service, clob, batch_size: int = 5) -> dict:
    bal_before = get_balance(clob)
    logger.info(f"[CLAIM] USDC before: ${bal_before:.2f}")

    # Use our own fetch to bypass the broken percentPnl > 0 filter
    user_address = clob.get_address()
    # Try proxy wallet first (funder), fall back to EOA
    proxy = os.getenv("PROXY_WALLET", "")
    positions = fetch_all_redeemable(proxy or user_address)
    if positions:
        logger.info(f"[CLAIM] Found {len(positions)} redeemable positions")
    results = service._redeem_from_positions(positions, batch_size)

    bal_after = get_balance(clob)
    delta = bal_after - bal_before
    # RedeemResult can be scalar or list — normalize
    if hasattr(results, "__len__"):
        batch_count = len(results)
    elif isinstance(results, list):
        batch_count = len(results)
    else:
        batch_count = 1 if results else 0
    logger.info(
        f"[CLAIM] Done: {batch_count} batches, "
        f"USDC ${bal_before:.2f} → ${bal_after:.2f} (+${delta:.2f})"
    )

    return {
        "batches": batch_count,
        "usdc_before": bal_before,
        "usdc_after": bal_after,
        "delta": delta,
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Polymarket Auto-Claimer")
    parser.add_argument(
        "--loop", type=int, default=0, help="Loop interval in seconds (0=run once)"
    )
    parser.add_argument("--batch", type=int, default=5, help="Batch size for redeem")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S"
    )

    load_dotenv(Path(__file__).parent / ".env")
    service, clob = create_claimer()

    if args.loop > 0:
        logger.info(f"[CLAIM] Auto-claim loop started (every {args.loop}s)")
        while True:
            try:
                claim_all(service, clob, batch_size=args.batch)
            except Exception as e:
                logger.error(f"[CLAIM] Loop error: {e}")
            time.sleep(args.loop)
    else:
        result = claim_all(service, clob, batch_size=args.batch)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
