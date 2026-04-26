from mpdex.utils.hyperliquid_base import HyperliquidBase
from eth_account import Account
from .mpdex_hl_sign_bridge import sign_l1_action as hl_sign_l1_action, sign_user_signed_action as hl_sign_user_signed_action
import time

class HyperliquidExchange(HyperliquidBase):
    USD_CLASS_TRANSFER_TYPES = [
        {"name": "hyperliquidChain", "type": "string"},
        {"name": "amount", "type": "string"},
        {"name": "toPerp", "type": "bool"},
        {"name": "nonce", "type": "uint64"},
    ]

    def __init__(
        self,
        wallet_address=None,
        wallet_private_key=None,
        agent_api_address=None,
        agent_api_private_key=None,
        by_agent=True,
        vault_address=None,
        builder_code=None,
        builder_fee_pair=None,
        *,
        FrontendMarket=False,
        proxy=None,
        cloid_prefix=None,
        builder_rotation=None,
    ):
        super().__init__(
            wallet_address=wallet_address,
            vault_address=vault_address,
            builder_code=builder_code,
            builder_fee_pair=builder_fee_pair,
            FrontendMarket=FrontendMarket,
            proxy=proxy,
        )
        self.cloid_prefix = cloid_prefix
        if builder_rotation:
            self.builder_rotation = list(builder_rotation)
        self.by_agent = by_agent
        self.wallet_private_key = wallet_private_key #if not by_agent else None
        self.agent_api_address = agent_api_address if by_agent else None
        self.agent_api_private_key = agent_api_private_key if by_agent else None

    def _get_wallet(self, *, for_user_action: bool = False):
        """
        서명용 wallet 객체 반환.
        - for_user_action=True: User Signed Action은 반드시 실제 wallet으로 서명해야 함
        - for_user_action=False: 일반 L1 액션은 agent 또는 wallet 사용
        """
        if for_user_action:
            # User Signed Action은 agent가 아닌 실제 wallet 필요
            priv = self.wallet_private_key
            if not priv:
                raise RuntimeError("wallet_private_key가 필요합니다 (User Signed Action)")
        else:
            priv = (self.agent_api_private_key if self.by_agent else self.wallet_private_key) or ""
            if not priv:
                if self.by_agent:
                    raise RuntimeError("agent_api_private_key가 필요합니다")
                else:
                    raise RuntimeError("wallet_private_key가 필요합니다")

        priv = priv[2:] if priv.startswith("0x") else priv
        return Account.from_key(bytes.fromhex(priv))
    
    async def _make_signed_payload(self, action: dict) -> dict:
        """일반 주문용 (sign_l1_action)"""
        nonce = int(time.time() * 1000)
        wallet = self._get_wallet(for_user_action=False)
        sig = hl_sign_l1_action(wallet, action, self.vault_address, nonce, None, True)
        payload = {"action": action, "nonce": nonce, "signature": sig}
        if self.vault_address:
            payload["vaultAddress"] = self.vault_address
        return payload

    async def _make_transfer_payload(self, action: dict) -> dict:
        """
        usdClassTransfer 전용 (sign_user_signed_action)
        
        action 입력 예시:
        {
            "type": "usdClassTransfer",
            "amount": "100",
            "toPerp": true,
            "nonce": 1234567890123
        }
        
        sign_user_signed_action이 다음 필드를 삽입:
        - signatureChainId: "0x66eee"
        - hyperliquidChain: "Mainnet"
        """
        # [IMPORTANT] User Signed Action은 반드시 실제 wallet으로 서명
        wallet = self._get_wallet(for_user_action=True)

        # nonce는 action에서 가져옴
        nonce = action.get("nonce") or int(time.time() * 1000)
        action["nonce"] = nonce  # 확실히 설정

        # sign_user_signed_action이 signatureChainId, hyperliquidChain을 action에 삽입
        sig = hl_sign_user_signed_action(
            wallet=wallet,
            action=action,
            payload_types=self.USD_CLASS_TRANSFER_TYPES,
            primary_type="HyperliquidTransaction:UsdClassTransfer",
            is_mainnet=True,
        )

        # 최종 payload: action에는 이미 signatureChainId, hyperliquidChain이 삽입됨
        payload = {
            "action": action,
            "nonce": nonce,
            "signature": sig,
        }

        return payload