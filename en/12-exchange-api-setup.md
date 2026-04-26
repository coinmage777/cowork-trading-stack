# 12. Exchange API Setup

API setup, permissions, and gotchas across common exchanges. Patterns differ per exchange, so this covers the general principles plus a representative configuration.

## General principles

### Permissions
- **Trading** ✓
- **Read (balance / positions / orders)** ✓
- **Withdrawal** never
- **Account / security management** never

### IP whitelist
Always when available. Register the VPS's static IP.

### Key storage
`.env` files → `.gitignore`. Never in code or git.

### Signing / auth methods
Per exchange:
- HMAC-SHA256 (Binance, Bybit, OKX, most CEX)
- ed25519 (some new DEXs)
- ECDSA + secp256k1 (Ethereum-based DEXs)
- StarkNet signing (StarkNet DEXs)
- EIP-712 (Ethereum typed data)

SDKs may exist or not; if not, implement directly.

## CEX (centralized exchanges)

### Binance / Bybit / OKX / Bitget

Similar pattern:
1. Account → API Management
2. "Create API"
3. Permissions: **Read** + **Trade**, withdraw OFF
4. IP whitelist
5. Save API Key + Secret

```python
import ccxt.async_support as ccxt

ex = ccxt.bybit({
    "apiKey": os.getenv("BYBIT_API_KEY"),
    "secret": os.getenv("BYBIT_SECRET"),
    "options": {"defaultType": "swap"},  # USDT perp
})
```

ccxt provides a unified abstraction across most exchanges. Useful for fast starts.

### Gotchas
- **Permission edits**: some exchanges require a new key on permission change
- **Expiry**: some keys expire (Bybit etc.)
- **Sub-accounts**: main vs sub — trading / arb on a sub is recommended (isolation)
- **Rate limits**: exchange-specific. Hitting them throttles or blocks. Read each exchange's docs.

## [Hyperliquid](https://miracletrade.com/?ref=coinmage) and HL-based front-ends

### Hyperliquid directly

- Authenticated via Ethereum private key, no separate API key
- Agent wallet system — main wallet delegates trading to a separate key (main not exposed)
- EIP-712 signed orders

```python
# Main wallet holds funds; agent does the trading
main_wallet = "0x..."  # main wallet
agent_private_key = os.getenv("HYPERLIQUID_AGENT_KEY")  # delegated key
```

### HL-based front-ends (Miracle, DreamCash, HyENA, Based, etc.)

These are not separate exchanges — they're front-ends with builder codes on top of HL:
- Same HL wallet trades
- Builder code handles routing / fee distribution
- Some require a cloid prefix too (Miracle: `0x4d455243...`)

Config example:
```yaml
exchanges:
  hyperliquid_2:
    keys:
      private_key: ${HL_AGENT_KEY}
      wallet_address: ${HL_MAIN_WALLET}
      builder_code: 0x4950994884602d1b6c6d96e4fe30f58205c39395  # public Miracle builder
      builder_fee_pair: { base: "1 25" }
      cloid_prefix: 0x4d455243
```

### Builder rotation

Distribute volume across multiple builder codes from the same wallet:

```yaml
builder_rotation:
  - name: miracle
    builder_code: 0x...
    fee_pair: { base: "1 25" }
    cloid_prefix: 0x4d455243
  - name: dreamcash
    builder_code: 0x...
    fee_pair: { base: "2 30" }
    cloid_prefix: 0x...
```

Round-robin per order. One wallet farms multiple front-ends' points simultaneously.

## DEXs

### [Lighter](https://app.lighter.xyz/?referral=GMYPZWQK69X4)
- Standalone DEX (not HL-based)
- API key must be Generated on the web (no auto-issue)
- SDK exists but has gotchas (signature mismatches etc.)
- Python SDK runs sync HTTP at init → deadlocks async bots → solved with isolated venv + subprocess bridge

### [EdgeX](https://pro.edgex.exchange/referral/570254647)
- StarkNet-based
- Auth: account_id (StarkNet) + StarkNet private key
- StarkNet signing directly or via SDK

### [GRVT](https://grvt.io/exchange/sign-up?ref=1O9U2GG)
- Standalone DEX
- account_id (numeric) + API key
- Isolated venv required (SDK compatibility)

### dYdX v4
- Cosmos-based
- Auth: BIP39 mnemonic
- Use SDK directly (dydx-v4-client-py)

### [Reya](https://app.reya.xyz/trade?referredBy=8src0ch8)
- Arbitrum Orbit
- EIP-712 signing
- Official SDK (Python 3.12 required → isolated venv)

### Backpack
- Solana-based, native DEX
- ed25519 signing
- Native SDK

### Paradex
- StarkNet-based
- Native SDK

### [Aster](https://www.asterdex.com/en/referral/e70505), Ostium, etc.
- Each has its own auth pattern
- Usually SDK or REST + signing

## Common gotchas with new DEXs

### 1) Immature SDKs
- Sparse docs
- Bugs (e.g., cancel_orders that doesn't actually work)
- Sometimes parts must be reimplemented

### 2) Signing variety
- HMAC, ed25519, ECDSA, StarkNet, EIP-712
- Wrong signing → 401 / Invalid signature

### 3) Symbol formats
- Differ across exchanges: BTC-USD, BTCUSDT, btc_usd, BTC-USD-PERP, hyna:BTC
- An abstraction is needed (SymbolAdapter)

### 4) Market list staleness
- New DEXs add / remove markets often
- Cached market lists go stale → periodic refetch

### 5) WebSocket flakiness
- Disconnects frequent
- Auto-reconnect + heartbeat mandatory
- WS death → REST fallback

### 6) Rate limits
- New exchanges are often lenient but tighten unexpectedly
- Always design conservatively

### 7) Mark price 0 / empty book
- In low-liquidity markets, mark price returns 0 sometimes
- Validate every time: `if mark_price <= 0: raise ValueError`

## Integrated exchanges (reference)

A reference list of exchanges integrated at some point. Stability and usability change over time:

**Tier 1 (stable)**
- Hyperliquid + its front-ends
- [Lighter](https://app.lighter.xyz/?referral=GMYPZWQK69X4)
- EdgeX
- GRVT

**Tier 2 (usable)**
- Bybit, OKX, Bitget (CEX)
- Backpack
- Paradex
- Aster

**Tier 3 (experimental / bumpy)**
- Many newly launched DEXs
- Decide after running

## Integration abstraction — Factory + Adapter

Adding a new exchange must be cheap. A typical pattern:

```python
# factory.py
EXCHANGE_REGISTRY = {}

def register_exchange(name):
    def decorator(cls):
        EXCHANGE_REGISTRY[name] = cls
        return cls
    return decorator

def create_exchange(name: str, key_params: dict):
    if name not in EXCHANGE_REGISTRY:
        raise ValueError(f"Unknown exchange: {name}")
    return EXCHANGE_REGISTRY[name](**key_params)

# Adapter
@register_exchange("hyperliquid")
class HyperliquidExchange(BaseExchange):
    async def get_mark_price(self, symbol: str): ...
    async def create_order(self, ...): ...
    # ...
```

The exchange is then named in config.yaml:
```yaml
exchanges:
  hyperliquid_2:
    keys:
      private_key: ${HL_KEY}
  lighter:
    keys:
      api_key: ${LIGHTER_KEY}
    isolated: true
    venv_path: system
```

### Isolated mode

When an SDK doesn't play nice with async event loops, isolate it:
```python
class LighterBridge:
    def __init__(self, venv_path):
        self.process = subprocess.Popen(
            [venv_path, "lighter_worker.py"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )
    
    async def call(self, method, **kwargs):
        # JSON RPC over stdin/stdout
        ...
```

## Env-var management

A typical `.env` structure (placeholders shown):

```bash
# Exchange keys
HYPERLIQUID_AGENT_KEY=<your_hl_agent_private_key>
HYPERLIQUID_WALLET=<your_hl_wallet_address>

LIGHTER_API_KEY=<your_lighter_api_key>
LIGHTER_ACCOUNT_ID=<your_lighter_account_id>

EDGEX_PRIVATE_KEY=<your_edgex_starknet_key>
EDGEX_ACCOUNT_ID=<your_edgex_account_id>

GRVT_API_KEY=<your_grvt_api_key>
GRVT_ACCOUNT_ID=<your_grvt_account_id>

BYBIT_API_KEY=<your_bybit_api_key>
BYBIT_SECRET=<your_bybit_secret>

# Bot infra
TELEGRAM_BOT_TOKEN=<your_telegram_bot_token>
TELEGRAM_CHAT_ID=<your_telegram_chat_id>

# AI
ANTHROPIC_API_KEY=<your_anthropic_key>
OPENAI_API_KEY=<your_openai_key>

# Paths
OBSIDIAN_VAULT=<your_obsidian_vault_path>
DB_PATH=<your_db_path>

# Polymarket / Predict.fun
POLYMARKET_PRIVATE_KEY=<your_polymarket_key>
PREDICT_API_KEY=<your_predict_key>
PREDICT_PRIVATE_KEY=<your_predict_signer_key>
PREDICT_ACCOUNT=<your_predict_account_address>
```

`.gitignore` must include `.env`. Backup via 1Password / Bitwarden style secret manager.

## New-exchange validation checklist

Before adding a new exchange:

- [ ] Read API docs — auth, rate limits, symbol formats
- [ ] SDK exists or REST direct
- [ ] Testnet / paper mode available
- [ ] Withdrawal permission separable (must be)
- [ ] IP whitelist available
- [ ] Signature algorithm — verified working in the target environment
- [ ] WebSocket stability — 1-hour connection-keep test
- [ ] Funding / fees clearly understood
- [ ] Market / symbol list stable
- [ ] Small-size live test → entry / exit / balance / cancel all work
- [ ] Error handling (mark price 0, empty order book)

Pass the checklist before scaling size.

## Next chapter

Next: a step-by-step roadmap — from zero to a real bot in production, in what order.
