# perp-dex-setup-guides

22개+ Perp DEX 거래소 API 발급 + WebSocket 연동 + EIP-712 서명 레퍼런스. 각 거래소별 docs_ref/ 폴더에 공식 문서 발췌, 실패 사례, key 발급 절차, IP whitelist, 권한 설정, 최소 주문 사이즈 등 운영 노트.

## 거래소별 가이드 폴더

```
perp-dex-setup-guides/docs_ref/
├── hyperliquid/         — Agent key, builder code, HIP-3 perpDexIndex
├── lighter/             — API key 웹 발급, change_api_key 서명 불일치 이슈
├── grvt/                — account_id (숫자), EIP-712 typed data
├── paradex/             — StarkNet L1/L2 키 분리, JWT auth
├── backpack/            — ED25519 key, 시크릿 인코딩
├── aster/               — BSC API + secret, withdrawal disable 권장
├── pacifica/            — HL HIP-3 builder, hyna:BTC 심볼
├── edgex/               — StarkNet account_id + StarkNet PK
├── reya/                — Arbitrum Orbit, Python 3.12 필수
├── risex/               — RISE Chain, EIP-712 VerifyWitness/RegisterSigner, chain_id 4153
├── standx/              — BSC DEX
├── treadfi/             — HL builder
├── hyena/               — HyENA Points, USDe 마진
├── decibel/             — pre-launch
├── variational/         — RFQ 방식, funding feed 없음
├── ostium/              — RWA perp
├── katana/              — HL 기반
├── ethereal/            — Ethena 생태계
├── bulk/                — 테스트넷, Ed25519
├── extended/            — StarkNet
├── hotstuff/            — HL 기반
└── nado/                — HL HIP-3, Nado Points
```

## 일반적 절차 (모든 거래소 공통)

### 1. Wallet 준비
- EVM 거래소: MetaMask 또는 hardware wallet에서 별도 trading-only wallet 생성 (메인 자금과 분리)
- StarkNet 거래소 (Paradex/EdgeX): L1 EVM wallet + L2 StarkNet account 분리
- 잔고 입금 ($100~$300 시작 권장)

### 2. API key 발급
- 거래소 웹 → Settings → API Management → Generate New Key
- **권한 설정**:
  - Trading: ON
  - Withdrawal: **OFF** (보안 critical, withdraw는 웹에서만)
  - Account info read: ON
- **IP whitelist**: 봇 운영 서버 IP 등록 (VPS IP)
- key + secret 즉시 `.env`에 저장 (다시 못 봄)

### 3. SDK 또는 wrapper 통합
- 각 거래소 README의 `사용 예시` 따라 wrapper 호출
- 첫 호출은 `get_collateral()`로 연결 검증
- 작은 주문 ($5~10) 1건으로 fill 확인

### 4. 위험 관리
- `manual_equity` 설정으로 거래소별 cap (예: $20-$50)
- `auto_disabled_exchanges.json`에 잔고 0 거래소 자동 등록
- `kill switch` 파일 경로 사전 설정 (`data/KILL_{EXCHANGE}`)

## 주요 운영 함정

### Hyperliquid
- **Agent key 시스템**: 메인 wallet은 sign만, agent wallet이 trade 실행. CLOSE_AGENT 활성 시 agent 키만 노출되도 메인 안전
- **Builder code rotation**: 같은 wallet으로 여러 builder 운영 시 별도 instance 만들면 충돌. `builder_rotation` list로 묶기
- **HIP-3 asset_id**: `140000 + perpDexIndex` (예: HyENA = 4 → 140004)

### Lighter
- **API key 웹에서 직접 발급**: SDK `change_api_key`는 서명 불일치 버그 (사용 X)
- **격리 venv 필수**: `__init__`이 sync HTTP 호출 → asyncio deadlock
- **stdout 오염**: `lighter_ws_client.py`의 `print()`이 subprocess stdout으로 흘러 JSON parse 에러 burst. `subprocess_wrapper.py`에 첫 char 가드 적용 필요

### GRVT
- **account_id는 숫자**: hex/string 아님
- **격리 venv**: SDK sync init
- **parse_order metadata KeyError**: 에러 응답 dict에는 `metadata` 키 없음. `.get('metadata', {}).get('client_order_id')` 안전 처리

### Paradex
- **L1 / L2 키 분리**: L1 EVM (Ethereum) PK + L2 StarkNet account secret 둘 다 필요
- **JWT auth**: 토큰 만료 (~30분) 시 자동 갱신 로직 필요

### Reya
- **Python 3.12 필수**: SDK 의존성 락
- **격리 venv** (reya_venv): 별도 venv + subprocess bridge

### RiseX
- **EIP-712 RegisterSigner + VerifyWitness**: 세션 키 등록 필수
- **chain_id 4153**: RISE Chain mainnet
- **`_target` 결정**: `addrs.router` 또는 `perp_v2.orders_manager` 또는 `contract_addresses.perps_manager` (precedence 주의 — Python `or` 연산자 우선순위 함정)

### Aster
- **withdraw OFF 강제**: BSC API 침해 시 즉시 자금 출금 가능. **withdraw 권한 절대 ON 하지 마**
- **available_collateral 사용**: total은 unrealized PnL로 음수 가능

## 환경변수 패턴 (.env 예시)

```env
# Hyperliquid
HL_AGENT_KEY=<EVM_PK_FOR_AGENT>
HL_WALLET=<MAIN_WALLET_ADDR>
HL_BUILDER_CODE_MIRACLE=<...>
HL_BUILDER_CODE_DREAMCASH=<...>

# Lighter
LIGHTER_API_KEY=<WEB_GENERATED>
LIGHTER_API_SECRET=<...>
LIGHTER_ACCOUNT_INDEX=<NUM>

# GRVT
GRVT_ACCOUNT_ID=<NUM>
GRVT_API_KEY=<...>
GRVT_PRIVATE_KEY=<EVM_PK>

# Paradex
PARADEX_L1_KEY=<EVM_PK>
PARADEX_L2_KEY=<STARKNET_PK>

# ... (나머지 거래소도 동일 패턴)
```
