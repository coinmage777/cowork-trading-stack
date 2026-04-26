"""
Auto Redeemer — 폴리마켓 resolve된 포지션 자동 클레임

poly-web3 라이브러리를 사용하여 resolve된 마켓의 위닝 토큰을 자동으로 USDC로 환수.
Builder API 키 필요 (polymarket.com/settings?tab=builder)

사용법:
    - 봇 내부: main.py의 _redeem_loop()에서 주기적 호출
    - 단독 실행: python auto_redeemer.py (1회 실행)

환경변수:
    POLYMARKET_API_KEY, POLYMARKET_SECRET, POLYMARKET_PASSPHRASE — CLOB 인증
    PRIVATE_KEY — 지갑 private key
    PROXY_WALLET — 프록시 지갑 주소
    BUILDER_KEY, BUILDER_SECRET, BUILDER_PASSPHRASE — Builder 인증
"""

import os
import logging
import time
from typing import Optional

logger = logging.getLogger("polybot.redeemer")


class AutoRedeemer:
    """resolve된 위닝 포지션 자동 클레임"""

    def __init__(self, config=None):
        """
        Parameters:
            config: Config 객체 (None이면 환경변수에서 직접 로드)
        """
        self._service = None
        self._initialized = False
        self._last_redeem_time = 0
        self._redeem_count = 0
        self._total_redeemed = 0
        self._errors = 0
        self._config = config

        # 설정
        self.redeem_interval = 120      # 2분마다 체크
        self.batch_size = 10            # 한번에 최대 10개 redeem
        self.retry_delay = 30           # 실패 시 재시도 대기

    def _init_service(self) -> bool:
        """poly-web3 서비스 초기화 (lazy)"""
        if self._initialized:
            return self._service is not None

        self._initialized = True

        try:
            from py_clob_client.client import ClobClient
            from py_builder_relayer_client.client import RelayClient
            from py_builder_signing_sdk.config import BuilderConfig
            from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
            from poly_web3 import RELAYER_URL, PolyWeb3Service
        except ImportError as e:
            logger.warning(
                f"[REDEEM] poly-web3 미설치 — 자동 클레임 비활성화. "
                f"설치: pip install poly-web3 ({e})"
            )
            return False

        try:
            # 환경변수 or config에서 키 로드
            if self._config:
                api_key = self._config.clob_api_key
                private_key = self._config.private_key
                proxy_wallet = self._config.proxy_wallet
                chain_id = self._config.chain_id
                clob_url = self._config.clob_api_url
            else:
                api_key = os.getenv("POLYMARKET_API_KEY", "")
                private_key = os.getenv("PRIVATE_KEY", "")
                proxy_wallet = os.getenv("PROXY_WALLET", "")
                chain_id = 137
                clob_url = os.getenv("POLYMARKET_API_URL", "https://clob.polymarket.com")

            builder_key = os.getenv("BUILDER_KEY", "")
            builder_secret = os.getenv("BUILDER_SECRET", "")
            builder_passphrase = os.getenv("BUILDER_PASSPHRASE", "")

            if not all([api_key, private_key, proxy_wallet, builder_key, builder_secret, builder_passphrase]):
                missing = []
                if not api_key: missing.append("POLYMARKET_API_KEY")
                if not private_key: missing.append("PRIVATE_KEY")
                if not proxy_wallet: missing.append("PROXY_WALLET")
                if not builder_key: missing.append("BUILDER_KEY")
                if not builder_secret: missing.append("BUILDER_SECRET")
                if not builder_passphrase: missing.append("BUILDER_PASSPHRASE")
                logger.warning(f"[REDEEM] 누락된 키: {', '.join(missing)} — 자동 클레임 비활성화")
                return False

            # CLOB client
            clob_client = ClobClient(
                clob_url,
                key=private_key,
                chain_id=chain_id,
                signature_type=1,  # 1=Proxy
                funder=proxy_wallet,
            )
            clob_client.set_api_creds(clob_client.create_or_derive_api_creds())

            # Builder relayer client
            relayer_client = RelayClient(
                RELAYER_URL,
                chain_id,
                private_key,
                BuilderConfig(
                    local_builder_creds=BuilderApiKeyCreds(
                        key=builder_key,
                        secret=builder_secret,
                        passphrase=builder_passphrase,
                    )
                ),
            )

            # PolyWeb3 서비스
            self._service = PolyWeb3Service(
                clob_client=clob_client,
                relayer_client=relayer_client,
                rpc_url="https://polygon-bor.publicnode.com",
            )

            logger.info("[REDEEM] 자동 클레임 초기화 완료")
            return True

        except Exception as e:
            logger.error(f"[REDEEM] 초기화 실패: {e}", exc_info=True)
            return False

    def redeem_all(self) -> list:
        """
        resolve된 모든 포지션 redeem.

        Returns:
            redeem 결과 리스트 (빈 리스트 = 클레임할 것 없음)
        """
        if not self._init_service():
            return []

        try:
            results = self._service.redeem_all(batch_size=self.batch_size)

            if not results:
                logger.debug("[REDEEM] 클레임 가능한 포지션 없음")
                return []

            # None이 있으면 실패한 건
            success = [r for r in results if r is not None]
            failed = [r for r in results if r is None]

            self._redeem_count += len(success)
            self._last_redeem_time = time.time()

            if failed:
                self._errors += len(failed)
                logger.warning(f"[REDEEM] {len(success)}건 성공, {len(failed)}건 실패 (재시도 필요)")
            else:
                logger.info(f"[REDEEM] {len(success)}건 클레임 완료")

            return results

        except Exception as e:
            self._errors += 1
            logger.error(f"[REDEEM] 클레임 에러: {e}", exc_info=True)
            return []

    def should_check(self) -> bool:
        """redeem 체크 시점인지"""
        return time.time() - self._last_redeem_time >= self.redeem_interval

    def get_stats(self) -> dict:
        """통계"""
        return {
            "initialized": self._initialized,
            "service_ok": self._service is not None,
            "total_redeemed": self._redeem_count,
            "errors": self._errors,
            "last_redeem": self._last_redeem_time,
        }


# 단독 실행: python auto_redeemer.py
if __name__ == "__main__":
    import sys
    from pathlib import Path
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    redeemer = AutoRedeemer()
    results = redeemer.redeem_all()
    if results:
        success = sum(1 for r in results if r is not None)
        print(f"클레임 완료: {success}건")
    else:
        print("클레임 가능한 포지션 없음")
