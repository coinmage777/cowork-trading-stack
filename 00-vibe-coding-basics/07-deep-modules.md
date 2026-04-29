# 07 - Deep Modules: 얕은 모듈을 깊은 모듈로

AI 가 코드를 빨리 만들수록 "얕은 모듈" 이 빠르게 쌓입니다. 인터페이스가 구현만큼 큰 모듈 — caller 가 내부 사정을 다 알아야 쓸 수 있는 모듈. 처음에는 동작하지만 6 개월 후 stack overflow.

이 챕터는 **shallow → deep 리팩토링** 의 세 가지 구체적 패턴입니다. Matt Pocock 의 `/improve-codebase-architecture` skill 적용 결과를 일반화한 것입니다.

> 출처: 06 챕터 + https://github.com/mattpocock/skills/tree/main/skills/engineering/improve-codebase-architecture

## Deep vs Shallow — 정의

- **모듈** — 인터페이스 + 구현이 있는 모든 것 (함수 / 클래스 / 패키지)
- **인터페이스** — caller 가 알아야 할 모든 것 (시그니처 + 타입 + invariant + 에러 모드 + 순서 + config)
- **깊이** — 인터페이스에서 leverage. 작은 인터페이스 뒤에 큰 행동 = **deep**. 인터페이스가 구현만큼 크면 **shallow**.

shallow 의 신호:
- 함수가 그냥 SDK 호출 forward (`return self.client.fetch(x)`)
- 클래스가 모든 메서드를 다른 클래스에 위임 (forwarding)
- caller 가 내부 구현 알아야 동작 (예: "이 함수는 BSC 일 때 18 decimals, ETH 면 6 decimals")
- 같은 패턴이 5+ 곳에 복사됨 (변경 시 전부 수정)

## 패턴 1 — 문자열 → 도메인 타입

### Shallow

```python
def symbol_create(venue: str, coin: str) -> str:
    if venue == "hyena":
        return f"hyna:{coin}"
    elif venue == "paradex":
        return f"{coin}-USD-PERP"
    elif venue == "grvt":
        return f"{coin}_USDT_Perp"
    return f"{coin}-PERP"

# 호출 측 — 문자열 파싱 강제
def is_hip3_symbol(sym: str) -> bool:
    return sym.lower().startswith("hyna:")

if is_hip3_symbol(symbol):
    asset_id = compute_hip3_id(symbol)
```

문제:
- 모든 caller 가 거래소별 prefix / 포맷 규칙 외워야
- venue 가 늘 때마다 모든 caller 검토 필요
- 타입 시스템이 "BTC vs hyna:BTC" 차이 못 잡음

### Deep

```python
@dataclass
class Market:
    coin: str
    quote: str = "USDC"
    kind: Literal["perp", "spot"] = "perp"
    venue_dex: Optional[str] = None  # HIP-3 builder name

    def __str__(self) -> str:
        # 거래소별 렌더는 이 안에만
        if self.venue_dex:
            return f"{self.venue_dex}:{self.coin}"
        if self.quote == "USD":
            return f"{self.coin}-USD-PERP"
        return f"{self.coin}-PERP"

    @property
    def is_hip3(self) -> bool:
        return self.venue_dex is not None
```

caller:
```python
market = Market(coin="BTC", venue_dex="hyna")
if market.is_hip3:
    asset_id = market.hip3_asset_id()
order = ex.create_order(market, ...)
```

이득:
- 모든 string parsing 이 `Market` 안으로 사라짐
- 새 venue 추가 = `Market` 한 곳에 이름 추가
- 타입 시스템이 "string symbol vs Market" 강제 구분
- caller 코드 수십 군데에서 수백 LOC 사라짐

## 패턴 2 — Forwarder × N → `__getattr__` proxy

### Shallow

여러 거래소를 같은 인터페이스로 노출하는 wrapper 가 sync SDK 때문에 subprocess 격리 필요. 각 거래소마다 wrapper 가 호출 forward 하는 클래스 작성:

```python
class LighterBridge:
    def __init__(self, account):
        self.account = account
        self.proc = subprocess.Popen(...)

    async def get_position(self, symbol, **kw):
        return await self._call("get_position", symbol=symbol, **kw)

    async def get_collateral(self, **kw):
        return await self._call("get_collateral", **kw)

    async def get_mark_price(self, symbol, **kw):
        return await self._call("get_mark_price", symbol=symbol, **kw)

    # ... 7개 더 ...

class GrvtBridge:
    # 같은 10개 메서드 forwarder 다시 작성
    ...

class ReyaBridge:
    # 또 다시 ...
```

문제:
- 거래소 추가 시 forwarder 10개 새로 작성
- 메서드 시그니처 수정 시 N개 클래스 모두 수정
- 추상 클래스 / 부모 클래스 의 forwarder 가 다시 forwarder

### Deep

```python
class SubprocessExchange:
    """Generic JSON-RPC bridge. Forwards any method call to subprocess."""

    def __init__(self, exchange_name: str, account, venv_python: str):
        self.exchange_name = exchange_name
        self.account = account
        self.proc = subprocess.Popen(
            [venv_python, "-m", "exchange_bridge", "--exchange", exchange_name]
        )

    def __getattr__(self, name: str):
        # 알려지지 않은 메서드 호출 = JSON-RPC forward
        async def _forward(**kwargs):
            return await self._call(name, **kwargs)
        return _forward

    async def _call(self, method, **kwargs):
        msg = json.dumps({"method": method, "params": kwargs})
        self.proc.stdin.write(msg + "\n")
        return json.loads(self.proc.stdout.readline())
```

자식 측 (subprocess):
```python
async def serve_jsonrpc(exchange):
    """parent 가 보낸 모든 메서드 호출을 실제 SDK 인스턴스에 forward."""
    while True:
        line = sys.stdin.readline()
        req = json.loads(line)
        result = await getattr(exchange, req["method"])(**req["params"])
        print(json.dumps(result), flush=True)
```

이득:
- 거래소별 Bridge 클래스 5+ 개 → 1 개 generic 으로 통합
- 새 거래소 추가 = 0 코드 (factory 에 등록만)
- 메서드 추가 = 자식만 수정, parent 자동 forward

## 패턴 3 — 캐시 패턴 복붙 → `FreshnessAwareCache`

### Shallow

거래소 wrapper 가 mark price 를 가져올 때:

```python
# lighter.py
async def get_mark_price(self, symbol):
    cached = self._ws_cache.get(symbol)
    if cached and time.time() - cached["ts"] < 2.0:
        return cached["price"]
    try:
        await asyncio.wait_for(self._ws_ready[symbol].wait(), timeout=0.5)
        cached = self._ws_cache.get(symbol)
        if cached:
            return cached["price"]
    except asyncio.TimeoutError:
        pass
    return await self._rest_get_mark(symbol)

# paradex.py — 같은 30 lines, 다른 timeout (3.0s)
# hyperliquid_base.py — 또 같은 30 lines, 또 다른 timeout (1.5s)
```

문제:
- 같은 패턴 6+ 거래소에 복사
- timeout 값이 wrapper 마다 drift (0.5 / 2.0 / 3.0)
- WS reconnect 버그 fix 시 한 군데만 적용되고 나머지는 누락

### Deep

```python
class FreshnessAwareCache(Generic[K, V]):
    """WS-first, REST-fallback cache with bounded staleness."""

    def __init__(
        self,
        ws_get: Callable[[K], V | None],
        ws_wait: Callable[[K, float], Awaitable[None]],
        rest_get: Callable[[K], Awaitable[V]],
        max_age_s: float = 2.0,
        ws_wait_timeout_s: float = 0.5,
    ):
        self.ws_get = ws_get
        self.ws_wait = ws_wait
        self.rest_get = rest_get
        self.max_age_s = max_age_s
        self.ws_wait_timeout_s = ws_wait_timeout_s

    async def get(self, key: K) -> V:
        # 1. WS cache fresh?
        cached = self.ws_get(key)
        if cached and time.time() - cached.ts < self.max_age_s:
            return cached.value
        # 2. WS 도착 대기
        try:
            await asyncio.wait_for(self.ws_wait(key), timeout=self.ws_wait_timeout_s)
            cached = self.ws_get(key)
            if cached:
                return cached.value
        except asyncio.TimeoutError:
            pass
        # 3. REST fallback
        return await self.rest_get(key)
```

wrapper:
```python
class LighterExchange:
    def __init__(self, ...):
        self.mark_price = FreshnessAwareCache(
            ws_get=self._ws_cache.get,
            ws_wait=self._ws_ready_event,
            rest_get=self._rest_get_mark,
            max_age_s=2.0,
        )

    get_mark_price = lambda self, sym: self.mark_price.get(sym)
```

이득:
- 6 wrapper × 30 lines = 180 LOC → 한 클래스로 통합
- timeout 값이 한 곳에서 관리
- WS reconnect 버그 fix 한 번에 모든 거래소 적용
- 새 캐시 패턴 (orderbook / positions) 도 같은 클래스 재사용

## 회귀 테스트로 묶어두기

deep module 로 만든 후에는 **04 챕터 / TDD** 와 결합. 다음 30-strategy-patterns/_combined/tests/test_bug_regression.py 같은 형태로:

```python
def test_market_renders_hip3_correctly():
    m = Market(coin="BTC", venue_dex="hyna")
    assert str(m) == "hyna:BTC"
    assert m.is_hip3

def test_freshness_cache_uses_ws_when_fresh():
    cache = FreshnessAwareCache(...)
    # mock ws_get returns fresh value → REST never called
    ...
```

회귀 테스트가 없으면 deep module 도 다음 refactor 에서 깨집니다.

## 한 줄 요약

> AI 가 빨리 만든 shallow module 6+ 곳을 deep module 1 곳으로 합치면, 보통 **수백 LOC 절감 + 버그 fix 시 한 곳만 고치면 끝**. 단, 각 deep module 은 회귀 테스트로 잠가두기.

다음 챕터 (08) 부터는 운영 단계 패턴입니다. `60-ops-runbooks/` 폴더 참고.
