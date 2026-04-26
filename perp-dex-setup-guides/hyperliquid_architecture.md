# Hyperliquid Architecture Guide

이 문서는 `multi-perp-dex` 프로젝트의 Hyperliquid 관련 코드 구조를 설명합니다.

## 파일 구조

```
mpdex/utils/
├── hyperliquid_base.py      # 추상 베이스 클래스 (공통 로직)
├── common_hyperliquid.py    # 공유 캐시 및 유틸리티

wrappers/
├── hyperliquid.py           # 네이티브 Hyperliquid 구현
├── hyperliquid_ws_client.py # WebSocket 클라이언트 (HLWSClientRaw, HLWSClientPool)
├── superstack.py            # Superstack (HL 기반)
├── treadfi_hl.py            # TreadFi Hyperliquid 통합
```

## 클래스 계층 구조

```
MultiPerpDex (ABC)
    ↓
MultiPerpDexMixin (기본 구현)
    ↓
HyperliquidBase (공통 HL 로직)
    ├── HyperliquidExchange   # 네이티브 HL (private key 서명)
    ├── SuperstackExchange    # Superstack API 서명
    └── TreadfiHlExchange     # TreadFi 세션 기반
```

---

## 기본 설정

```python
HyperliquidBase(
    wallet_address: str,           # 지갑 주소
    vault_address: str = None,     # 볼트/서브계정 주소 (선택)
    builder_code: str = None,      # 빌더 수수료 코드
    builder_fee_pair: dict = None, # 수수료 구조
    fetch_by_ws: bool = True,      # ⚠️ 기본값 True: WS 우선, REST 폴백
    FrontendMarket: bool = False,  # FrontendMarket vs GTc 주문 타입
)
```

**중요**: `fetch_by_ws=True`가 기본값이므로 대부분의 조회 작업은 WebSocket을 먼저 시도합니다.

---

## WS vs REST 사용 구분

### 조회 작업 (fetch_by_ws=True 기준)

| 메서드 | WS 시도 | REST 폴백 | 설명 |
|--------|---------|-----------|------|
| `get_position(symbol)` | ✓ `get_position_ws()` | ✓ `get_position_rest()` | 포지션 조회 |
| `get_collateral()` | ✓ `get_collateral_ws()` | ✓ `get_collateral_rest()` | 담보/잔고 조회 |
| `get_mark_price(symbol)` | ✓ `get_mark_price_ws()` | ✓ `get_mark_price_rest()` | 마크 가격 |
| `get_open_orders(symbol)` | ✓ `get_open_orders_ws()` | ✓ `get_open_orders_rest()` | 오픈 주문 |
| `get_spot_balance(coin)` | ✓ `get_spot_balance_ws()` | ✗ 미지원 | 스팟 잔고 |
| `get_orderbook(symbol)` | ✓ WS 전용 | ✗ | 오더북 |

### 액션 작업 (prefer_ws 파라미터)

| 메서드 | WS 시도 | REST 폴백 | 설명 |
|--------|---------|-----------|------|
| `create_order(...)` | ✓ `_send_action()` | ✓ | 주문 생성 |
| `cancel_orders(...)` | ✓ `_send_action()` | ✓ | 주문 취소 |
| `update_leverage(...)` | ✓ `_send_action()` | ✓ | 레버리지 설정 |
| `transfer_to_spot(...)` | ✓ `_send_action()` | ✓ | Perp→Spot 전송 |
| `transfer_to_perp(...)` | ✓ `_send_action()` | ✓ | Spot→Perp 전송 |

### 항상 REST

- `get_mark_price_rest()` → `/info` (metaAndAssetCtxs)
- `get_collateral_rest()` → 병렬 `/info` (clearinghouseState per DEX)

---

## 주요 메서드 요약

### 초기화 & 종료

| 메서드 | 설명 |
|--------|------|
| `await init()` | 비동기 초기화: 공유 캐시 로드, WS 클라이언트 생성 |
| `await close()` | HTTP 세션 종료, WS 풀 연결 해제 |

### 자산 조회

| 메서드 | 반환 | 설명 |
|--------|------|------|
| `get_collateral()` | `{total_collateral, available_collateral, ...}` | 담보 정보 |
| `get_position(symbol)` | `{entry_price, unrealized_pnl, side, size}` 또는 `None` | 포지션 |
| `get_spot_balance(coin)` | `{coin, total, available}` | 스팟 잔고 |
| `get_mark_price(symbol)` | `float` 또는 `None` | 마크 가격 |

### 주문 관리

| 메서드 | 파라미터 | 설명 |
|--------|----------|------|
| `create_order(...)` | symbol, side, amount, price, order_type, is_reduce_only, is_spot, tif, slippage | 주문 생성 |
| `cancel_orders(symbol, open_orders)` | 심볼, 취소할 주문 리스트 (None이면 전체) | 주문 취소 |
| `get_open_orders(symbol)` | 심볼 | 오픈 주문 조회 |

### 레버리지 & 전송

| 메서드 | 설명 |
|--------|------|
| `update_leverage(symbol, leverage)` | 레버리지 설정 (None이면 최대치) |
| `transfer_to_spot(amount)` | Perp→Spot USDC 전송 |
| `transfer_to_perp(amount)` | Spot→Perp USDC 전송 |

### 오더북

| 메서드 | 설명 |
|--------|------|
| `subscribe_orderbook(symbol)` | l2Book 구독 (레퍼런스 카운팅) |
| `unsubscribe_orderbook(symbol)` | l2Book 구독 해제 |
| `get_orderbook(symbol)` | `{bids, asks, time}` 반환 |

---

## WebSocket 클라이언트 구조

### HLWSClientRaw (단일 연결)

`wrappers/hyperliquid_ws_client.py`에 위치한 저수준 WS 클라이언트.

**특징:**
- DEX/유저 조합당 하나의 연결
- 자동 재연결 (지수 백오프: 1s~8s)
- JSON 기반 ping/pong 유지
- 429 레이트 리밋 처리 (Retry-After 헤더 지원)

**주요 메서드:**

```python
# 연결 관리
await connect()
await close()
await ensure_connected_and_subscribed()

# 구독
await ensure_user_streams(address)      # 유저 스트림 구독
await ensure_allmids_for(dex)           # 가격 스트림 구독
await subscribe_orderbook(symbol)       # 오더북 구독 (레퍼런스 카운팅)
await unsubscribe_orderbook(symbol)     # 오더북 해제

# 액션
await post_info(payload, timeout=6.0)   # info 요청
await post_action(payload, timeout=8.0) # action 요청 (주문/취소)

# 데이터 조회
get_price(symbol)                       # 캐시된 가격
get_orderbook(symbol)                   # 캐시된 오더북
get_positions_norm_for_user(address)    # 캐시된 포지션
get_open_orders_for_user(address)       # 캐시된 오픈 주문
```

**채널 종류:**

| 채널 | 데이터 |
|------|--------|
| `allMids` | Perp/Spot 가격 |
| `spotState` | 유저 스팟 잔고 |
| `allDexsClearinghouseState` | 유저 포지션/마진 (DEX별) |
| `openOrders` | 유저 오픈 주문 |
| `l2Book` | 오더북 (bids/asks) |

### HLWSClientPool (연결 풀)

**목적:** 여러 인스턴스 간 WS 연결 공유

```python
USER_SUB_LIMIT = 10  # 소켓당 최대 유저 수
```

| 설정값 | 동작 |
|--------|------|
| `USER_SUB_LIMIT = 10` | 소켓 하나에 유저 10명 공유 |
| `USER_SUB_LIMIT = 1` | 유저마다 개별 WS 연결 |

**주요 메서드:**

```python
# 클라이언트 획득/해제
client = await pool.acquire(ws_url, http_base, address, ...)  # 참조 카운트++
await pool.release(client=client)                              # 참조 카운트--, 0이면 종료
```

---

## 오더북 레퍼런스 카운팅

여러 인스턴스가 같은 오더북을 구독할 때 충돌 방지:

```python
# 내부 구조
_orderbook_sub_counts: Dict[str, int] = {}  # coin -> 구독자 수
_orderbook_sub_lock = asyncio.Lock()        # race condition 방지

# 동작
Instance A: subscribe("BTC")  → count: 0→1, 실제 구독 전송
Instance B: subscribe("BTC")  → count: 1→2, 전송 안함 (이미 구독됨)
Instance B: unsubscribe("BTC") → count: 2→1, 전송 안함 (A가 아직 사용 중)
Instance A: unsubscribe("BTC") → count: 1→0, 실제 해제 전송
```

---

## 공유 캐시 (common_hyperliquid.py)

프로세스 전역에서 공유되는 메타데이터:

```python
_HL_SHARED_CACHE = {
    "dex_list": [...],           # DEX 목록 (hl + HIP-3)
    "idx2name": {...},           # spot 토큰 인덱스→이름
    "name2idx": {...},           # spot 토큰 이름→인덱스
    "pair_by_index": {...},      # spot 페어 인덱스→페어명
    "bq_by_index": {...},        # spot 페어 인덱스→(base, quote)
    "perp_asset_meta": {...},    # perp 자산 메타 (szDec, maxLev, ...)
}
```

**초기화:** `await init_shared_hl_cache(session)`
- `/info` REST 호출 (spotMeta, perpDexs, allPerpMetas)
- Lock으로 중복 초기화 방지

---

## 서브클래스 구현

### HyperliquidExchange (hyperliquid.py)

네이티브 Hyperliquid, `eth_account` 사용:

```python
HyperliquidExchange(
    wallet_address,
    wallet_private_key=None,    # 지갑 직접 서명
    by_agent=False,             # 에이전트 API 사용 여부
    agent_api_private_key=None, # 에이전트 키
)
```

### SuperstackExchange (superstack.py)

Superstack API 위임:

```python
SuperstackExchange(
    wallet_address,
    api_key,  # Superstack API 키
)
```

### TreadfiHlExchange (treadfi_hl.py)

TreadFi 프론트엔드 API:

```python
TreadfiHlExchange(
    login_wallet,
    trading_wallet,
    session_cookies,  # 인증 쿠키
)
```

---

## 에러 처리

| 상황 | 처리 |
|------|------|
| WS 타임아웃 | REST 폴백 |
| 429 레이트 리밋 | 지수 백오프 + Retry-After |
| 연결 끊김 | 자동 재연결 + 재구독 |
| 없는 자산 | `RuntimeError` 발생 |

---

## 사용 예시

```python
from mpdex import create_exchange, symbol_create

async def main():
    # 인스턴스 생성
    hl = await create_exchange("hyperliquid", {
        "wallet_address": "0x...",
        "wallet_private_key": "...",
    })

    symbol = symbol_create("hyperliquid", "BTC")  # "BTC"

    # 조회 (WS 우선)
    collateral = await hl.get_collateral()
    position = await hl.get_position(symbol)
    price = await hl.get_mark_price(symbol)

    # 주문 (WS 우선)
    await hl.create_order(symbol, "buy", 0.001, order_type="market")

    # 오더북 구독
    await hl.subscribe_orderbook(symbol)
    orderbook = await hl.get_orderbook(symbol)

    # 종료
    await hl.close()
```
