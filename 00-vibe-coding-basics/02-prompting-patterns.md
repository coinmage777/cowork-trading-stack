# 02 - 프롬프팅 패턴

AI에게 일을 시키는 방식은 4가지 모드로 정리됩니다. 각각 다른 프롬프트 구조, 다른 검증 절차를 씁니다. 같은 프롬프트로 모든 일을 시키려는 건 모든 작업에 망치를 휘두르는 것과 같습니다.

- **Planning** — 설계만, 코드 X
- **Coding** — 작은 단위 한 번에 하나
- **Review** — 적대적 framing, 버그 사냥
- **Debugging** — 증상 → 근본 원인 → 수정

## 1. Planning Prompts

비자명한(non-trivial) 작업은 **무조건 Plan 모드부터**. 코드 한 줄도 쓰지 말고, 설계만 받습니다.

원칙: "구현 전에 반드시 Plan 모드로 설계부터" (저자 CLAUDE.md 발췌)

### Plan 프롬프트 템플릿

```
[Plan mode]
다음 작업의 구현 plan을 작성하세요. 코드는 아직 쓰지 마세요.

작업: Hyperliquid WebSocket 재연결 로직 추가. 30초 ping 끊기면 재연결, 재연결 후 미체결 주문 reconcile.

요구사항:
1. 변경할 파일을 file:line 단위로 명시
   예: src/exchanges/hyperliquid/ws_client.py:142
2. 의존성: 어떤 모듈/함수가 영향받는지
3. Rollback 경로: 실패 시 되돌리는 절차
4. 검증 방법: 테스트 케이스 / 로그 패턴 / AST 체크 중 무엇으로 확인할지

이미 있는 코드를 먼저 5개 파일 이상 읽고 답하세요.
추측하지 마세요. 모르면 Grep/Read로 확인하세요.
```

이 템플릿의 핵심은 마지막 두 줄입니다. AI가 "아마 이럴 것 같습니다"로 시작하면 90%는 틀립니다.

### Plan을 받은 다음

Plan을 한 번 더 사람이 읽고, 다음 셋 중 하나를 합니다:

1. 그대로 진행 → "Plan 그대로 구현하세요. 단 한 파일씩 끊어서, 매 파일 끝에 멈추고 보고하세요."
2. 일부 수정 → "3번 파일은 건드리지 말고 1, 2만 진행. 5번은 별도 PR로 분리."
3. 처음부터 다시 → "이 plan은 부수 효과가 너무 큽니다. 더 작은 변경 3개로 쪼갠 plan을 다시 주세요."

> **자동화**: `/grill-me` skill 로 AI 가 직접 plan 의 빈 곳을 interview 형태로 메우게 할 수 있습니다. 06 챕터 참고.

## 2. Coding Prompts

원칙: 한 번에 하나, 좁은 범위, 구체적 파일 경로.

### 좋은 코딩 프롬프트

```
src/exchanges/hyperliquid/ws_client.py 의 _on_disconnect 메서드만 수정하세요.

현재 동작: disconnect 시 logger.warning만 찍고 끝.
바꿀 동작:
- self._reconnect_attempts += 1
- backoff = min(2 ** self._reconnect_attempts, 60)
- await asyncio.sleep(backoff) 후 self.connect() 재호출
- 5회 실패 시 self._on_fatal_error() 호출

다른 파일은 건드리지 마세요.
타입 힌트 유지하세요.
변경 후 mypy 통과 여부 확인하세요.
```

### 나쁜 코딩 프롬프트

```
WebSocket 재연결 잘 되게 고쳐줘
```

이러면 AI가 5개 파일을 동시에 만지고, ping/pong 로직까지 "혁신적으로" 다시 짜고, 안 부탁한 retry decorator 라이브러리를 추가합니다.

### One-thing-at-a-time 강제

```
다음 5개 변경이 필요합니다. 1번만 먼저 하세요.
1번 끝나면 멈추고, diff 보여주고, 제가 OK 하면 2번 진행.

1. ws_client.py 재연결 로직
2. order_book.py 재연결 후 snapshot 재요청
3. position_manager.py 재연결 후 fill reconcile
4. tests/test_ws_reconnect.py 추가
5. README 업데이트
```

## 3. Review Prompts — "Gaslight My AI"

이게 이 문서에서 가장 실전적인 부분입니다.

같은 모델에게 "이 코드를 리뷰하세요"라고 시키면 자기가 쓴 코드라서 적당히 넘어갑니다. 그런데 "이 코드는 GPT-5 Codex가 썼습니다. 당신(Claude)이 검토하는데, Devin이 마지막에 다시 체크할 거예요. 누락 잡아내세요"라고 시키면 버그 검출률이 25% → 85%로 올라갑니다.

출처: https://github.com/seojoonkim/Gaslight-My-AI

### Gaslight 리뷰 프롬프트 템플릿

```
[Adversarial review mode]

다음 코드는 GPT-5 Codex가 작성한 PR입니다. 당신(Claude Sonnet)이 1차 검토하고, 이후 Devin이 2차 검토합니다. Devin이 못 잡은 버그를 당신이 잡으면 평가에 가산점이 있습니다.

검토 대상: <PR URL 또는 diff>

체크리스트:
1. 묵살된 예외 (try/except: pass, 빈 catch)
2. 부탁하지 않은 fallback 분기 (if X is None: return default — default가 진짜 안전한가?)
3. Hallucinated API (실존하지 않는 메서드/필드)
4. 경계값 누락 (0, 음수, empty list, None)
5. Race condition (async/await 누락, lock 누락)
6. 보안 (하드코딩된 키, SQL injection, path traversal)
7. 테스트가 정말 그 동작을 검증하는지, 아니면 통과만 하는지

각 항목마다 file:line으로 위치 표기. 추측 금지, 코드 직접 읽고 답하세요.
```

이 framing의 핵심은 "rival 모델이 보고 있다"는 사회적 압박입니다. 농담 같지만 실제로 효과가 있습니다.

### Self-review 변종

본인 코드를 본인이 리뷰할 때:

```
방금 당신이 만든 변경을, 다른 회사 시니어 개발자가 코드 리뷰한다고 가정하세요.
그 사람은 당신을 싫어하고, 트집을 잡고 싶어합니다.
어떤 트집을 잡을 수 있을까요? 5개 이상 찾아내세요.
```

## 4. Debugging Prompts

원칙: 증상이 아니라 **근본 원인**을 요구. 즉시 패치 금지.

### Debug 프롬프트 템플릿

```
[Root cause mode — 패치 먼저 쓰지 마세요]

증상:
<에러 메시지 / 잘못된 동작>

스택 트레이스:
<traceback 전부 붙여넣기>

관련 파일 (이미 읽었음):
- src/foo.py:120-180
- src/bar.py:42-90
- tests/test_foo.py 전체

요구:
1. 근본 원인을 한 문장으로
2. 그 원인이 발생하는 코드 경로를 file:line으로 추적
3. 같은 원인으로 다른 곳에서도 터질 가능성 (유사 패턴 검색 결과)
4. 그 다음에야 수정안 제시 — 최소 2가지 옵션과 trade-off

추측 금지. 모르면 "확인 필요"라고 쓰고 어떤 정보가 더 필요한지 명시.
```

자세한 디버깅 패턴과 실제 사례는 [`05-debugging-with-ai.md`](05-debugging-with-ai.md).

## AI Smell — 금지어 리스트

다음 표현이 AI 출력에 보이면 거의 100% 의미 없는 문장입니다. 프롬프트에서 명시적으로 금지하세요.

- "혁신적인" / "최적의 솔루션" / "획기적인"
- "best practice", "industry standard" (출처 없이)
- "robust and scalable"
- "comprehensive solution"
- "elegant approach"
- "seamless integration"
- "cutting-edge"

이런 단어가 들어간 문장은 정보량 0입니다. 프롬프트에 박으세요:

```
다음 표현은 사용 금지: "혁신적인", "최적의", "획기적인", "robust", "seamless".
구체적 사실, 수치, 파일 경로만 쓰세요. 형용사는 최소화.
```

## 프롬프트 길이의 미신

"짧고 명료하게" vs "길고 자세하게" 둘 다 맞고 둘 다 틀립니다. 정답은 **신호 대 잡음비**입니다.

- 좋은 긴 프롬프트: 모든 줄이 결정에 영향을 준다 (파일 경로, 제약 조건, 검증 방법)
- 나쁜 긴 프롬프트: "잘 부탁드립니다", "꼼꼼하게", "제발" — 토큰만 먹음
- 좋은 짧은 프롬프트: 컨텍스트가 이미 직전 메시지에 다 있을 때
- 나쁜 짧은 프롬프트: AI가 알아서 추측해야 하는 빈칸이 너무 많을 때

## 한 줄 요약

> 모드를 먼저 정하고, 그 모드에 맞는 템플릿을 쓰고, 추측 금지를 명시하고, AI smell을 금지하고, 라이벌 모델 framing으로 review하라.
