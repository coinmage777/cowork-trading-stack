from mpdex.utils.hyperliquid_base import HyperliquidBase
import aiohttp

SUPERSTACK_BASE = "https://wallet-service.superstack.xyz"

async def get_superstack_payload(api_key: str, action: dict, vault_address: str) -> dict:
    url = f"{SUPERSTACK_BASE}/api/exchange"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    req = {"action": action, **({"vaultAddress": vault_address} if vault_address else {})}
    async with aiohttp.ClientSession() as s:
        async with s.post(url, headers=headers, json=req) as r:
            r.raise_for_status()
            data = await r.json()
    return data.get("payload") or {}

class SuperstackExchange(HyperliquidBase):
    def __init__(
        self,
        wallet_address=None,
        api_key=None,
        vault_address=None,
        builder_fee_pair=None,
        *,
        FrontendMarket=False,
        proxy=None,
    ):
        super().__init__(
            wallet_address=wallet_address,
            vault_address=vault_address,
            builder_code="<EVM_ADDRESS>",  # Superstack 고정
            builder_fee_pair=builder_fee_pair,
            FrontendMarket=FrontendMarket,
            proxy=proxy,
        )
        self.api_key = api_key

    async def _make_signed_payload(self, action: dict) -> dict:
        payload = await get_superstack_payload(self.api_key, action, self.vault_address)
        if self.vault_address:
            payload["vaultAddress"] = self.vault_address
        return payload
    
    async def _make_transfer_payload(self, action: dict) -> dict:
        """usdClassTransfer도 Superstack API로 서명"""
        return await self._make_signed_payload(action)