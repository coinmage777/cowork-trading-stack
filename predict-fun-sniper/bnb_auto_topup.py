"""Predict.fun Signer EOA BNB 자동 충전.

BNB 잔고가 임계치(0.002) 미만이면 Signer EOA의 USDC를 PancakeSwap으로 BNB swap.

주의:
  - BSC 네트워크 (chain_id 56)
  - Signer EOA가 USDC 보유해야 함 (Predict Account가 아님)
  - gas 비용: 약 0.0005 BNB 필요 (첫 swap 실패 방지 → 최소 여유분 유지)
  - 슬리피지 보호: 3%

환경변수:
  PREDICT_PRIVATE_KEY — Signer EOA private key (이미 존재)
  BNB_TOPUP_USDC_AMOUNT — 1회 스왑할 USDC 양 (기본 5)
  BNB_TOPUP_MIN_BNB — 이 잔고 이하면 swap (기본 0.001)

사용:
  python bnb_auto_topup.py              # 1회 실행 (cron용)
  python bnb_auto_topup.py --dry-run    # 시뮬레이션
  python bnb_auto_topup.py --force      # 잔고 무관 강제 swap (테스트용)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bnb_topup")

# BSC 주소
USDC_BSC = "<EVM_ADDRESS>"
WBNB = "<EVM_ADDRESS>"
PANCAKE_V2_ROUTER = "<EVM_ADDRESS>"

BSC_RPCS = [
    "https://bsc-dataseed.binance.org",
    "https://bsc-dataseed1.defibit.io",
    "https://bsc-dataseed1.ninicoin.io",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    try:
        from web3 import Web3
        from eth_account import Account
    except ImportError:
        logger.error("web3.py / eth-account 미설치 — pip install web3 eth-account")
        return

    pk = os.environ.get("PREDICT_PRIVATE_KEY", "").strip()
    if not pk:
        logger.error("PREDICT_PRIVATE_KEY 미설정")
        return
    if not pk.startswith("0x"):
        pk = "0x" + pk

    acct = Account.from_key(pk)
    addr = acct.address
    logger.info(f"Signer EOA: {addr}")

    min_bnb = float(os.environ.get("BNB_TOPUP_MIN_BNB", "0.001"))
    usdc_amount = float(os.environ.get("BNB_TOPUP_USDC_AMOUNT", "5"))

    # RPC 연결
    w3 = None
    for rpc in BSC_RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
            if w3.is_connected():
                logger.info(f"BSC RPC 연결: {rpc}")
                break
        except Exception:
            pass
    if not w3 or not w3.is_connected():
        logger.error("모든 BSC RPC 연결 실패")
        return

    # 현재 BNB 잔고
    bnb_wei = w3.eth.get_balance(addr)
    bnb = bnb_wei / 1e18
    logger.info(f"현재 BNB 잔고: {bnb:.6f}")

    if not args.force and bnb >= min_bnb:
        logger.info(f"BNB 충분 ({bnb:.6f} ≥ {min_bnb}) — 스킵")
        return

    # USDC 잔고 확인
    usdc_abi = [{
        "constant": True, "inputs": [{"name":"_owner","type":"address"}],
        "name": "balanceOf", "outputs": [{"name":"balance","type":"uint256"}],
        "type": "function"
    },{
        "constant": False, "inputs": [{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],
        "name": "approve", "outputs": [{"name":"","type":"bool"}],
        "type": "function"
    },{
        "constant": True, "inputs": [{"name":"_owner","type":"address"},{"name":"_spender","type":"address"}],
        "name": "allowance", "outputs": [{"name":"","type":"uint256"}],
        "type": "function"
    }]
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_BSC), abi=usdc_abi)
    usdc_bal = usdc.functions.balanceOf(addr).call() / 1e18  # USDC on BSC는 18 decimals
    logger.info(f"USDC 잔고: {usdc_bal:.2f}")

    if usdc_bal < usdc_amount:
        logger.error(f"USDC 부족 ({usdc_bal:.2f} < {usdc_amount}) — Signer EOA에 USDC 충전 필요")
        return

    if args.dry_run:
        logger.info(f"[DRY-RUN] {usdc_amount} USDC → BNB swap 예정")
        return

    # PancakeSwap V2 Router
    router_abi = [{
        "inputs":[
            {"name":"amountIn","type":"uint256"},
            {"name":"amountOutMin","type":"uint256"},
            {"name":"path","type":"address[]"},
            {"name":"to","type":"address"},
            {"name":"deadline","type":"uint256"}
        ],
        "name":"swapExactTokensForETH",
        "outputs":[{"name":"amounts","type":"uint256[]"}],
        "type":"function"
    },{
        "inputs":[
            {"name":"amountIn","type":"uint256"},
            {"name":"path","type":"address[]"}
        ],
        "name":"getAmountsOut",
        "outputs":[{"name":"","type":"uint256[]"}],
        "type":"function"
    }]
    router = w3.eth.contract(address=Web3.to_checksum_address(PANCAKE_V2_ROUTER), abi=router_abi)

    amount_in = int(usdc_amount * 1e18)
    path = [Web3.to_checksum_address(USDC_BSC), Web3.to_checksum_address(WBNB)]

    # 예상 출력 조회
    try:
        amounts_out = router.functions.getAmountsOut(amount_in, path).call()
        expected_bnb = amounts_out[-1] / 1e18
        logger.info(f"예상 수령 BNB: {expected_bnb:.6f}")
    except Exception as e:
        logger.error(f"getAmountsOut 실패: {e}")
        return

    min_out = int(amounts_out[-1] * 0.97)  # 3% 슬리피지 허용

    # allowance 체크 + approve
    current_allowance = usdc.functions.allowance(addr, Web3.to_checksum_address(PANCAKE_V2_ROUTER)).call()
    if current_allowance < amount_in:
        logger.info("USDC approve 필요 (최대치)")
        approve_tx = usdc.functions.approve(
            Web3.to_checksum_address(PANCAKE_V2_ROUTER), 2**256 - 1
        ).build_transaction({
            "from": addr,
            "nonce": w3.eth.get_transaction_count(addr),
            "gas": 100000,
            "gasPrice": w3.eth.gas_price,
        })
        signed = acct.sign_transaction(approve_tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        logger.info(f"approve tx: {tx_hash.hex()}")
        w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        logger.info("approve 완료")

    # swap
    import time
    deadline = int(time.time()) + 600
    swap_tx = router.functions.swapExactTokensForETH(
        amount_in, min_out, path, addr, deadline
    ).build_transaction({
        "from": addr,
        "nonce": w3.eth.get_transaction_count(addr),
        "gas": 250000,
        "gasPrice": w3.eth.gas_price,
    })
    signed = acct.sign_transaction(swap_tx)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    logger.info(f"swap tx: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    new_bnb = w3.eth.get_balance(addr) / 1e18
    logger.info(f"✓ swap 완료. 새 BNB 잔고: {new_bnb:.6f} (+{new_bnb-bnb:.6f})")

    # 텔레그램 알림
    import asyncio
    import aiohttp
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if token and chat:
        msg = (
            f"<b>⛽ BNB 자동 충전 완료</b>\n"
            f"  swap: {usdc_amount} USDC → {new_bnb-bnb:.6f} BNB\n"
            f"  새 잔고: {new_bnb:.6f} BNB\n"
            f"  tx: https://bscscan.com/tx/{tx_hash.hex()}"
        )
        async def send():
            async with aiohttp.ClientSession() as s:
                await s.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat, "text": msg, "parse_mode": "HTML"}
                )
        asyncio.run(send())


if __name__ == "__main__":
    main()
