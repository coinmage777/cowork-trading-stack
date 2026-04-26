# 12. 거래소 API 셋팅

운영해온 거래소들의 API 셋팅, 권한, 함정 정리. 거래소마다 패턴이 다르니까 일반론 + 쓰는 것 위주로.

## 일반 원칙

### 권한
- **트레이딩** O
- **읽기 (잔고 / 포지션 / 주문)** O
- **출금** 절대 X
- **계정 관리 / 보안 변경** 절대 X

### IP 화이트리스트
가능하면 무조건. VPS의 고정 IP 등록.

### 키 보관
`.env` 파일 → `.gitignore`. 절대 코드 / git에 직접 X.

### 서명 / 인증 방식
거래소마다 다름:
- HMAC-SHA256 (Binance, Bybit, OKX 등 CEX 다수)
- ed25519 (일부 신생 DEX)
- ECDSA + secp256k1 (이더리움 기반 DEX)
- StarkNet 서명 (StarkNet DEX)
- EIP-712 (이더리움 typed data)

각 방식의 SDK가 있을 수 있고 없을 수도. 없으면 직접 구현.

## CEX (중앙거래소)

### Binance / Bybit / OKX / Bitget

비슷한 패턴:
1. 계정 → API Management 페이지
2. "Create API"
3. 권한 선택: **Read** + **Trade**, 출금 OFF
4. IP 화이트리스트
5. API Key + Secret 저장

```python
import ccxt.async_support as ccxt

ex = ccxt.bybit({
    "apiKey": os.getenv("BYBIT_API_KEY"),
    "secret": os.getenv("BYBIT_SECRET"),
    "options": {"defaultType": "swap"},  # USDT perp
})
```

ccxt가 대부분 거래소 통합 추상화 제공. 빠른 시작에 유용.

### 함정
- **권한 변경**: 일부 거래소는 권한 변경 시 새 키 발급 필요
- **만료**: API 키에 만료 기한이 있는 경우 (Bybit 등)
- **Sub-account**: 메인 계정 vs 서브 계정 — 트레이딩 / 차익은 서브 권장 (격리)
- **Rate Limit**: 거래소마다 다름. 초과 시 IP / 키 일시 차단. 개별 거래소 docs 참조

## [Hyperliquid](https://miracletrade.com/?ref=coinmage) 및 HL 기반 프론트엔드

### Hyperliquid 직접

- 지갑(이더리움 키)으로 직접 인증, 별도 API 키 X
- agent wallet 시스템 — 메인 지갑이 위임한 별도 키로 트레이딩 (메인 지갑 노출 X)
- 거래는 EIP-712 서명

```python
# 메인 지갑은 자금 보관, agent로 트레이딩 위임
main_wallet = "0x..."  # 메인 지갑
agent_private_key = os.getenv("HYPERLIQUID_AGENT_KEY")  # 위임된 키
```

### HL 기반 프론트엔드 (Miracle, DreamCash, HyENA, Based 등)

이들은 별도 거래소가 아니라 HL 위에 builder code 얹은 프론트엔드:
- 같은 HL 지갑으로 거래
- builder code에 따라 라우팅 / 수수료 분배
- 일부는 cloid prefix도 필요 (Miracle: `0x4d455243...`)

config 예시:
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

### Builder Rotation

같은 지갑에서 여러 builder code의 볼륨을 분산:

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

봇이 매 주문마다 라운드로빈으로 다른 builder 적용. 같은 지갑으로 여러 프론트엔드의 포인트 동시 파밍.

## DEX (탈중앙거래소)

### Lighter

- 독립 DEX (HL 기반 X)
- API key는 웹에서 Generate 필수 (자동 생성 X)
- SDK 있지만 서명 불일치 등 함정 많음
- Python SDK가 sync HTTP 호출을 init에서 함 → async 봇과 deadlock → 격리 venv + subprocess bridge로 해결

### [EdgeX](https://pro.edgex.exchange/referral/570254647)

- StarkNet 기반
- 인증: account_id (StarkNet) + StarkNet private key
- StarkNet 서명 직접 구현 또는 SDK

### [GRVT](https://grvt.io/exchange/sign-up?ref=1O9U2GG)

- 독립 DEX
- account_id (숫자) + API key
- 격리 venv 필수 (SDK 호환성)

### dYdX v4

- Cosmos 기반
- 인증: BIP39 mnemonic
- 직접 SDK 사용 (dydx-v4-client-py)

### [Reya](https://app.reya.xyz/trade?referredBy=8src0ch8)

- Arbitrum Orbit 위
- EIP-712 서명
- 공식 SDK 사용 (Python 3.12 필수 → 격리 venv)

### Backpack

- Solana 기반 자체 DEX
- ed25519 서명
- 자체 SDK

### Paradex

- StarkNet 기반
- 자체 SDK

### [Aster](https://www.asterdex.com/en/referral/e70505), Ostium 등

- 각자 독자 인증 패턴
- 보통 SDK 또는 REST + 서명

## 신생 DEX의 일반적 함정

### 1) SDK 미성숙
- 문서화 부족
- 버그 많음 (cancel_orders가 작동 안 한다든가)
- 직접 구현해야 할 때도 있음

### 2) 서명 알고리즘 다양성
- HMAC, ed25519, ECDSA, StarkNet, EIP-712 등
- 서명 잘못하면 401 / Invalid signature 반환

### 3) 심볼 포맷
- 거래소마다 다름: BTC-USD, BTCUSDT, btc_usd, BTC-USD-PERP, hyna:BTC
- 추상화 레이어 (SymbolAdapter) 필요

### 4) 마켓 / 심볼 리스트 동기화
- 신생 DEX는 마켓 추가 / 제거 빈번
- 봇이 캐시한 마켓이 stale일 수 있음 → 주기적 재조회

### 5) WebSocket 불안정
- 끊김 빈번
- 자동 재연결 + heartbeat 필수
- WS 죽으면 REST 폴백

### 6) Rate Limit
- 신생 거래소는 종종 너그럽지만, 갑자기 강화될 수 있음
- 항상 conservative하게 호출 빈도 디자인

### 7) Mark Price 0 / Empty Orderbook
- 유동성 낮은 마켓에서 mark price = 0 반환하기도
- 매번 검증: `if mark_price <= 0: raise ValueError`

## 운영해본 거래소들 (참고)

이 리스트는 어느 시점에든 통합 시도해본 거래소들. 안정성 / 사용성은 시간 따라 변함:

**Tier 1 (안정)**
- Hyperliquid + 그 위 프론트엔드들
- Lighter
- EdgeX
- GRVT

**Tier 2 (사용 가능)**
- Bybit, OKX, Bitget (CEX)
- Backpack
- Paradex
- Aster

**Tier 3 (실험적 / 함정 많음)**
- 새 출시 DEX 다수
- 운영해보고 결정

## 통합 추상화 — Factory + Adapter

새 거래소 추가가 쉬워야 한다. 패턴:

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

# 거래소 어댑터
@register_exchange("hyperliquid")
class HyperliquidExchange(BaseExchange):
    async def get_mark_price(self, symbol: str): ...
    async def create_order(self, ...): ...
    # ...
```

config.yaml에 거래소 이름만 적으면 자동 로드:
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

### 격리 모드

SDK가 async 이벤트 루프와 호환 안 되면 격리:
```python
# isolated bridge — 별도 프로세스로 SDK 호출
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

## 환경변수 관리

`.env` 구조 (예시):

```bash
# 거래소 키 (실제 값은 placeholder)
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

# 봇 인프라
TELEGRAM_BOT_TOKEN=<your_telegram_bot_token>
TELEGRAM_CHAT_ID=<your_telegram_chat_id>

# AI
ANTHROPIC_API_KEY=<your_anthropic_key>
OPENAI_API_KEY=<your_openai_key>

# 경로
OBSIDIAN_VAULT=<your_obsidian_vault_path>
DB_PATH=<your_db_path>

# Polymarket / Predict.fun
POLYMARKET_PRIVATE_KEY=<your_polymarket_key>
PREDICT_API_KEY=<your_predict_key>
PREDICT_PRIVATE_KEY=<your_predict_signer_key>
PREDICT_ACCOUNT=<your_predict_account_address>
```

`.gitignore`에 `.env` 등록 필수. 백업은 1Password / Bitwarden 같은 시크릿 매니저로.

## 새 거래소 검증 체크리스트

신규 거래소 추가 전 확인:

- [ ] API docs 읽기 — 인증, rate limit, 심볼 포맷
- [ ] SDK 존재 여부 — 있으면 사용, 없으면 REST 직접
- [ ] 테스트넷 / 페이퍼 모드 있는지
- [ ] 출금 권한 분리 가능한지 (있어야 함)
- [ ] IP 화이트리스트 가능한지
- [ ] 서명 알고리즘 — 환경에서 작동 검증
- [ ] WebSocket 안정성 — 1시간 연결 유지 테스트
- [ ] 펀딩비 / 수수료 정확히 파악
- [ ] 마켓 / 심볼 리스트 안정적인지
- [ ] 작은 사이즈 라이브 테스트 → 진입 / 청산 / 잔고 조회 / 주문 취소 모두 작동
- [ ] 에러 케이스 (mark price 0, 빈 오더북) 핸들링

이 체크리스트 통과 후 사이즈 늘리기.

## 다음 장

다음은 단계별 로드맵 — 0에서 시작해 본격 봇 운영까지 어떤 순서로 가는가.
