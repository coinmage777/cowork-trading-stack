# perp-dex-setup-guides

> 한 줄 요약: 22개+ Perp DEX 의 API 발급, WebSocket 연동, EIP-712 서명 절차를 모은 운영 레퍼런스. 각 거래소별 실패 사례, key 권한 설정, IP whitelist, 최소 주문 사이즈 같은 실전 노트 모음입니다.

## 의존성 (Dependencies)

문서 모음이라 코드 의존성은 없습니다. 다만 가이드를 따라가려면:

- 각 거래소의 공식 SDK 또는 `20-exchange-wrappers/_combined` 의 wrapper 가 있어야 합니다.
- WebSocket 검증 스크립트 (`*_ws.md` 안의 코드) 는 `aiohttp`, `websockets` 사용.

## AI에게 어떻게 시켰나 (How this was built with Claude)

이 모듈을 만들 때 사용한 프롬프트 패턴:

> "각 거래소 docs 사이트의 API 발급 절차랑, 내가 실제로 발급하다가 막혔던 step (예: Lighter `change_api_key` 서명 불일치, Reya 가 Python 3.12 만 받음) 을 모아서 거래소별로 폴더 하나씩 만들어 줘. 각 폴더에는 ① key 발급 절차 ② WebSocket auth 흐름 ③ EIP-712 typed data 예시 ④ 운영 시 발견된 버그 노트 를 마크다운으로 정리해 줘."

AI가 자주 틀린 부분 (common AI mistakes for this code path):

- **API 권한 설정**: AI 는 "Trading + Withdrawal 둘 다 ON" 으로 시키는 경우가 자주 있는데, 봇 운영에서는 withdraw 는 무조건 OFF 가 맞음. 침해 시 자금 출금 차단을 위함.
- **IP whitelist 누락**: VPS IP 를 등록 안 하고 봇 가동 → "geo restriction" 같은 모호한 에러로 401/403 이 떨어지는데 AI 는 시그니처 디버깅으로 잘못 유도.
- **EIP-712 chain_id**: 거래소가 자기네 L2/sidechain (RiseX 4153, Reya Orbit) 인 걸 모르고 mainnet chain_id 로 답함.

힌트: 이 모듈은 22번 이상 새 거래소를 통합하면서 갱신되었고, 그때마다 AI가 놓친 것은 **API key 발급 페이지의 권한 토글, JWT 토큰 만료, WebSocket auth payload 안의 timestamp ms vs s 차이** 입니다.

## 모듈 구조 (file structure with one-liner per file)

| File | Purpose |
|------|---------|
| `backpack_ws.md` | Backpack WS subscribe + ED25519 서명 예시 |
| `edgex_ws.md` | EdgeX StarkNet WS auth + L2 키 흐름 |
| `extended.html` | Extended docs 백업 |
| `hyperliquid_architecture.md` | HL agent key + builder code + HIP-3 indexing 정리 |
| `lighter_ws.md` | Lighter WS auth, account_index, sync init 함정 |
| `pacifica_rest.md` | Pacifica REST 발급 절차 |
| `pacifica_ws_fetch.md` | Pacifica market data WS |
| `pacifica_ws_trading.md` | Pacifica trading WS (HL HIP-3 builder) |
| `proxy_test.py` | 거래소별 proxy 통과 여부 테스트 |
| `standx.md` | Standx (BSC DEX) 발급 절차 |
| `variational_ws.md` | Variational RFQ WS (funding feed 없음) 흐름 |

각 거래소 폴더에는 추가로:

- `docs_ref/{exchange}/` — 공식 docs 발췌
- 실패 사례 / IP whitelist / minimum size 노트

## 일반적 절차 (모든 거래소 공통)

### 1. Wallet 준비
- EVM 거래소: MetaMask 또는 hardware wallet 에서 별도 trading-only wallet 생성 (메인 자금과 분리)
- StarkNet 거래소 (Paradex/EdgeX): L1 EVM wallet + L2 StarkNet account 분리
- 잔고 입금 ($100~$300 시작 권장)

### 2. API key 발급
- 거래소 웹 → Settings → API Management → Generate New Key
- **권한 설정**:
  - Trading: ON
  - Withdrawal: **OFF** (보안 critical, withdraw 는 웹에서만)
  - Account info read: ON
- **IP whitelist**: 봇 운영 서버 IP 등록 (VPS IP)
- key + secret 즉시 `.env` 에 저장 (다시 못 봄)

### 3. SDK 또는 wrapper 통합
- 각 거래소 README 의 `사용 예시` 따라 wrapper 호출
- 첫 호출은 `get_collateral()` 로 연결 검증
- 작은 주문 ($5–10) 1건으로 fill 확인

### 4. 위험 관리
- `manual_equity` 설정으로 거래소별 cap (예: $20-$50)
- `auto_disabled_exchanges.json` 에 잔고 0 거래소 자동 등록
- `kill switch` 파일 경로 사전 설정 (`data/KILL_{EXCHANGE}`)

## 사용 예시 (Usage)

`docs_ref/lighter/` 의 노트 따라가는 예시:

```bash
# 1. https://app.lighter.xyz 접속 → wallet connect
# 2. Settings → API Keys → Generate (웹에서 발급, SDK change_api_key 사용 X)
# 3. .env 에 LIGHTER_API_KEY, LIGHTER_API_SECRET, LIGHTER_ACCOUNT_INDEX 저장
# 4. 격리 venv 빌드:
python -m venv .lighter_venv
.lighter_venv/Scripts/pip install lighter-python-sdk==<pinned>

# 5. 봇은 subprocess bridge 통해 호출
python -m perp_dex_wrappers.lighter.lighter_subprocess_wrapper --probe
```

## 실전 함정 (Battle-tested gotchas)

운영하면서 깨진 부분들:

### Hyperliquid
- **Agent key 시스템**: 메인 wallet 은 sign 만, agent wallet 이 trade 실행. CLOSE_AGENT 활성 시 agent 키만 노출되도 메인은 안전.
- **Builder code rotation**: 같은 wallet 으로 여러 builder 운영 시 별도 instance 만들면 충돌. `builder_rotation` list 로 묶어야 함.
- **HIP-3 asset_id**: `140000 + perpDexIndex` (예: HyENA index=4 → 140004). 이 식 모르고 절대값 입력하면 잘못된 자산에 주문이 들어감.

### Lighter
- **API key 웹에서 직접 발급**: SDK `change_api_key` 는 서명 불일치 버그 (사용 X).
- **격리 venv 필수**: `__init__` 이 sync HTTP 호출 → asyncio deadlock.
- **stdout 오염**: `lighter_ws_client.py` 의 `print()` 이 subprocess stdout 으로 흘러 JSON parse 에러 burst. `subprocess_wrapper.py` 에 첫 char 가드 적용.

### GRVT
- **account_id 는 숫자**: hex/string 아님.
- **격리 venv**: SDK sync init.
- **parse_order metadata KeyError**: 에러 응답 dict 에는 `metadata` 키 없음. `.get('metadata', {}).get('client_order_id')` 안전 처리.

### Paradex
- **L1 / L2 키 분리**: L1 EVM (Ethereum) PK + L2 StarkNet account secret 둘 다 필요.
- **JWT auth**: 토큰 만료 (~30분) 시 자동 갱신 로직 필요.

### Reya
- **Python 3.12 필수**: SDK 의존성 락.
- **격리 venv** (reya_venv): 별도 venv + subprocess bridge.

### RiseX
- **EIP-712 RegisterSigner + VerifyWitness**: 세션 키 등록 필수.
- **chain_id 4153**: RISE Chain mainnet (이더리움 mainnet 1 아님).
- **`_target` 결정**: `addrs.router` 또는 `perp_v2.orders_manager` 또는 `contract_addresses.perps_manager` 우선순위 함정 (Python `or` 평가 순서).

### Aster
- **withdraw OFF 강제**: BSC API 침해 시 즉시 자금 출금 가능. **withdraw 권한 절대 ON 금지**.
- **available_collateral 사용**: `total` 은 unrealized PnL 로 음수 가능.

### WebSocket 공통
- **재연결 backoff**: `[2, 5, 10, 30, 60, 180, 300]` 초가 `[10, 30, 60, 120, 300]` 보다 신뢰성 있음 (짧은 disconnect 빈번 발생).
- **timestamp 단위**: ms 인지 s 인지 거래소별 다름. signature 가 맞는데 401 이 나면 90% 확률로 timestamp 단위 문제.

## 응용 (How this fits with other modules)

- 본 가이드 → `20-exchange-wrappers/_combined` 의 wrapper 구현 → 30/40 의 전략/스캐너
- 신규 거래소 통합 시: 본 폴더에 `docs_ref/{name}/` 추가 → wrapper 작성 → 전략 등록
- `60-ops-runbooks/telegram-control` 의 `/balance`, `/positions` 명령은 본 가이드대로 발급된 key 가 모두 정상이어야 작동

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
```

## 거래소 가입 링크

- Hyperliquid: https://miracletrade.com/?ref=coinmage
- Lighter: https://app.lighter.xyz/?referral=GMYPZWQK69X4
- Aster: https://www.asterdex.com/en/referral/e70505
- Pacifica: https://app.pacifica.fi?referral=cryptocurrencymage
- GRVT: https://grvt.io/exchange/sign-up?ref=1O9U2GG
- EdgeX: https://pro.edgex.exchange/referral/570254647
- Reya: https://app.reya.xyz/trade?referredBy=8src0ch8
- Extended: https://app.extended.exchange/join/COINMAGE
- Variational: https://omni.variational.io/?ref=OMNICOINMAGE
- Standx: https://standx.com/referral?code=coinmage
- Nado: https://app.nado.xyz?join=NX9LLaL
