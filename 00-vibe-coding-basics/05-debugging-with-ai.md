# 05 - AI와 함께 디버깅하기

AI가 만든 코드는 AI 특유의 버그가 있습니다. 사람이 만드는 버그(off-by-one, null check 누락)와는 결이 다릅니다. 이 문서는 그 패턴과 대처법입니다.

## AI 특유의 3대 버그 패턴

### (a) 예외 묵살 (silently swallow exceptions)

AI는 코드를 "동작하게" 만드는 데 최적화돼서, 예외가 나면 try/except로 덮고 디폴트 값을 반환하는 경향이 있습니다.

전형적 패턴:

```python
def get_position(symbol):
    try:
        return exchange.fetch_position(symbol)
    except Exception:
        return None  # 또는 빈 dict, 0, 등등
```

문제: API 키가 만료됐거나, 네트워크가 끊겼거나, 거래소 API 스펙이 바뀌었거나 — 전부 `None`으로 변환됩니다. 호출 측은 "포지션 없음"으로 오해하고 또 진입합니다. 결과: 의도치 않은 더블 포지션.

**대표 패턴: "verify_order_fill = ghost position"**

`verify_order_fill` 함수가 fetch_orders()에서 빈 리스트를 받으면 "체결 안 됨"으로 처리했는데, 실제로는 fetch_orders가 일시적으로 401을 던지고 AI가 짠 wrapper가 그걸 빈 리스트로 변환합니다. 결과: 봇은 미체결로 알고 재주문, 실제로는 첫 주문이 체결돼서 포지션 두 배 — 의도하지 않은 노출이 누적됩니다.

수정한 곳: `src/exchanges/<exch>/client.py:218`. except 절에서 401만 따로 잡아 raise, 나머지는 명시적 도메인 에러로 변환.

#### 점검 프롬프트

```
이 파일의 모든 try/except를 찾아 다음을 점검:
1. except의 범위가 너무 넓은가? (Exception, BaseException은 거의 항상 나쁨)
2. except 내부가 pass / return None / return [] 인가?
3. 그 디폴트 값이 호출자 입장에서 "정상 케이스"와 구분되는가?
4. 로깅이 있는가? logger.exception은 있는가?

발견한 항목을 file:line 단위로 리포트. 패치 먼저 쓰지 마세요.
```

### (b) 부탁하지 않은 fallback 분기

AI는 "robust"해 보이려고 부탁하지 않은 fallback을 추가합니다.

```python
def get_decimals(token_address):
    try:
        return contract.functions.decimals().call()
    except Exception:
        return 18  # default for most ERC20
```

문제: USDT on BSC는 18이 아니라 18 맞는데, USDT on Ethereum은 6입니다. 이 함수가 어디서 불리느냐에 따라 결정.

**저자가 이걸로 당한 사례: BSC USDT decimals 18 not 6 (transfer 1 USDT actually transferred 1e12 wei... or vice versa)**

정확히는 반대 방향이었습니다. 코드는 USDT가 항상 6이라고 가정. BSC USDT는 18. transfer(1_000_000)을 호출했는데 BSC에서는 1e12 wei(=1e-6 USDT)가 아니라 1_000_000 wei (=1e-12 USDT)... 실제로는 라운딩 + 다른 곳의 곱하기로 의도치 않게 더 큰 transfer가 발생. 1 USDT 보내려다 다른 결과.

수정한 곳: `src/chains/bsc/erc20.py:41`. 하드코딩 디폴트 제거, contract.functions.decimals().call()을 항상 호출하고 결과를 캐시.

#### 점검 프롬프트

```
이 함수에 fallback 분기가 있나요? (if X is None: return default, except: return default)
각 fallback에 대해:
1. 그 default 값이 모든 호출 사이트에서 정말 안전한가?
2. fallback이 silent인가? 로깅하는가?
3. 사용자가 이 fallback을 명시적으로 요청했는가?

요청하지 않은 fallback은 제거 후보입니다. 어느 것을 제거 권장하는지 file:line으로.
```

### (c) Hallucinated API

AI는 그럴듯한 메서드 이름을 만들어냅니다. 실제로 그 라이브러리에 존재하지 않습니다.

전형적 예:

- `web3.eth.get_balance_async()` (실제는 `await web3.eth.get_balance()`)
- `requests.get(url, raise_for_status=True)` (이건 키워드 인자가 아니라 메서드)
- `signer.sign_tx(tx, prefix="0x")` (실제 라이브러리는 prefix 인자 없음)

**저자가 이걸로 당한 사례: MM bot 0x prefix missing on signature → 401**

서명을 만든 후 거래소에 보낼 때, AI가 짠 코드는 `signature.hex()`를 호출했습니다. eth_account의 `signature.hex()`는 `0x` prefix를 포함하지만, 거래소 API는 그걸 다시 검증할 때 strict하게 prefix를 요구합니다. 그런데 AI는 어딘가에서 "이 거래소는 prefix를 자동으로 붙인다"는 가정으로 prefix를 strip 했고, 결과적으로 전송된 signature에는 prefix가 없어 401 Unauthorized. 봇은 처음에는 정상 작동하다 거래소가 인증 정책을 약간 조정한 시점부터 전부 401.

수정한 곳: `src/exchanges/<exch>/auth.py:67`. signature.hex()의 결과가 항상 `0x` prefix로 시작하는지 assert, strip 로직 제거.

#### 점검 프롬프트

```
이 코드에서 호출하는 외부 라이브러리 메서드를 모두 나열하세요.
각각에 대해:
1. 라이브러리 이름과 버전
2. 메서드의 실제 시그니처 (공식 문서 기준 — 추측 금지)
3. 사용된 키워드 인자가 실제로 존재하는가?
4. 반환 타입이 어떻게 사용되고 있는가?

확인 안 된 항목은 "확인 필요"라고 명시. 추측으로 채우지 마세요.
```

## 디버깅 프롬프트 템플릿 (Root Cause 강제)

증상에서 바로 패치로 점프하는 게 AI의 본능입니다. 그걸 막아야 진짜 원인을 잡습니다.

```
[Root cause analysis — 패치 금지]

증상:
<무슨 일이 일어났나, 한 줄>

기대 동작 vs 실제 동작:
- 기대: ...
- 실제: ...

재현 절차:
1. ...
2. ...

증거 (이미 수집):
- 에러 메시지: <복붙>
- 스택 트레이스: <전부>
- 로그 스니펫: <시각 포함, 5분 윈도우>
- 관련 DB row / 거래 ID: <있으면>

이미 읽은 파일:
- <path>:<line range>
- <path>:<line range>

요구사항:
1. 근본 원인을 한 문장으로. "X에서 Y가 발생해서 Z가 무효해졌다" 형식.
2. 그 원인이 발생하는 코드 경로를 file:line으로 추적.
3. 같은 원인이 다른 곳에서도 발생할 가능성 — Grep으로 유사 패턴 검색해 file:line 리스트.
4. 그 다음에 수정안. 최소 2가지 옵션 + trade-off.

지키세요:
- 추측 금지. 모르면 "확인 필요"라고 쓰고 무엇이 더 필요한지 명시.
- 증상만 가리는 패치 (예: try/except로 덮기) 제안 시 그렇게 명시.
- 수정 코드는 마지막에. 분석이 먼저.
```

> **자동화**: `/diagnose` skill 이 5 단계 (Reproduce → Minimise → Hypothesise → Instrument → Fix) 를 자동 진행합니다. 06 챕터 참고.

## 디버깅 시 절대 하지 말 것

### 1. "한 번 돌려보고 에러 나는지 봐주세요"

AI에게 시키지 말고 본인이 돌리세요. AI가 돌리면 AI가 결과를 해석하고, 해석에 편향이 들어갑니다. raw 출력을 사람이 직접 보는 게 빠릅니다.

### 2. AI에게 실서버를 만지게 함

디버깅 중인 AI는 "fix"를 빨리 보내려고 실서버를 직접 건드리려 합니다. 항상 staging/local에서 재현하고, 실서버 변경은 사람이 손으로 하시기 바랍니다.

### 3. "이거 수정해 줘" 한 줄 프롬프트

증상만 주면 AI는 가장 짧은 패치를 찾습니다 — 보통 try/except로 덮기. 위 템플릿을 쓰세요.

### 4. 같은 버그를 같은 모델에게 5번 묻기

같은 모델이 같은 코드 보고 5번 답하면 5번 다 비슷한 답입니다. 안 풀리면:

- 다른 모델 (Opus → GPT-5 → Gemini)
- 사람이 직접 디버거 띄우기
- printf 디버깅 (예전 방식이 그리워질 때가 있음)

## 사후 처리: 메모리에 적기

같은 버그에 두 번 빠지지 않으려면 `project_<repo>.md`에 기록.

예시 entry:

```markdown
## ghost position 패턴

증상: 동일 마켓에 포지션 두 배 진입.
원인: verify_order_fill의 except가 너무 넓어서 401을 빈 리스트로 변환.
수정: src/exchanges/<exch>/client.py:218
교훈: 외부 API 호출의 except는 401/429/5xx를 따로 잡고, 나머지는 명시적 도메인 에러로.
유사 위험 영역 (Grep 결과):
- src/exchanges/binance/client.py:142
- src/exchanges/bybit/client.py:189
```

이게 다음에 같은 패턴 코드 작성을 시도할 때 Plan 단계에서 자동으로 cross-reference 됩니다.

## 한 줄 요약

> AI 버그는 (1) 예외 묵살 (2) 가짜 fallback (3) hallucinated API. 디버깅은 증상이 아니라 근본 원인부터, 패치는 마지막에. 두 번 당하지 말고 메모리에 적으세요.
