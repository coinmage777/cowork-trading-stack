# 06 - Craftsmanship Patterns (Matt Pocock skills)

AI 가 코드를 10 배 빠르게 만들면, 잘못된 아키텍처 / 얕은 모듈 / 약한 테스트 / 모호한 도메인 언어도 10 배 빠르게 쌓입니다. "빨라진다" 는 마법이 아니라 부채를 뽑는 속도가 빨라진다는 뜻이기도 합니다.

Matt Pocock 의 입장은 분명합니다. SDD (Specs-Driven Development) 처럼 사양만 잘 쓰면 AI 가 알아서 짜준다는 접근에 그는 회의적입니다. 대신 **fundamentals + craftsmanship** — 모듈 깊이, 테스트, 디버깅 루프, 도메인 언어 — 이 답이라고 봅니다. 그리고 그 craftsmanship 을 **slash command 로 자동화**한 게 그가 공개한 12 개 skill 입니다.

- 레포: https://github.com/mattpocock/skills
- 영상 (한 시간 분량 워크스루): https://www.youtube.com/watch?v=v4F1gFy-hqg

이 챕터는 그 중 6 개 핵심 패턴을 이 저장소의 트레이딩 봇 맥락에 맞게 풀어 씁니다.

---

## 1. `/grill-me` — Plan 검증 (40~100 questions)

설명: 사용자가 "이거 만들고 싶다" 라고 한 줄을 던지면, AI 가 그 plan 의 모든 branch 를 끄집어내기 위해 40~100 개 질문을 던집니다. 답하다 보면 본인도 몰랐던 결정 포인트가 드러납니다.

트레이딩 봇 예시 — "Polymarket V2 메이커 봇" 시작 시:

```
> /grill-me Polymarket V2 메이커 봇

Q1. 어떤 마켓 (정치 / 스포츠 / 크립토 / 전체)?
Q2. size: per-market 고정 vs 자본 비율?
Q3. spread: tick 기준 몇 bp?
Q4. 결제 직전 (resolution proximity) 몇 시간 안에 들어가나?
Q5. drawdown 시 자동 위축 룰?
Q6. kill 조건: 연속 N 회 reject 시? 일일 PnL -X% 시?
...
```

답하고 나면 코드 한 줄 짜기 전에 spec 이 사실상 완성됩니다. 이 저장소의 "plan 모드 먼저" 원칙 (`02-prompting-patterns.md`) 의 자동화 도구입니다.

---

## 2. `/tdd` — Red-Green-Refactor 자동화

설명: 코드 짜기 전 실패하는 테스트부터. AI 가 red (실패 테스트) → green (최소 통과 코드) → refactor (정리) 사이클을 진행합니다.

**봇 전략에 특히 중요한 이유**: 실 자금 위험 없이 로직을 검증할 수 있는 거의 유일한 방법입니다. 백테스트는 데이터 편향이 있고, dry-run 은 실시간이 아닙니다. 단위 테스트만이 결정론적입니다.

예시 — 트레일링 스탑 로직 추가:

```
> /tdd 트레일링 스탑: 진입가 +1.5% 도달 시 활성화, 그 후 최고가 -0.5% 도달 시 청산

[red 단계]
test_trailing_does_not_activate_below_threshold  → fail
test_trailing_activates_at_1_5_pct               → fail
test_trailing_exits_on_05_pct_drawback           → fail
test_trailing_resets_on_new_high                 → fail

[green 단계 — 최소 코드]
class TrailingStop:
    def update(self, price): ...

[refactor 단계]
- 매직 넘버 제거 (1.5, 0.5 → config)
- typing 보강
- edge case 추가 테스트
```

이 저장소의 `30-strategy-patterns/` 의 새 전략 추가는 거의 항상 이 사이클을 따릅니다.

---

## 3. `/improve-codebase-architecture` — Deep Modules

설명: John Ousterhout 의 "Philosophy of Software Design" 의 deep module 개념을 적용. 얕은 wrapper-of-wrapper-of-wrapper 패턴을 찾아내 정리합니다. **얕은 모듈 = 인터페이스 복잡도 ≈ 구현 복잡도**. **깊은 모듈 = 단순 인터페이스, 복잡한 구현**.

이 저장소의 `10-foundation-modules/` 가 deep module 의 좋은 예시입니다. 예: kill-switch 는 외부 인터페이스가 `is_killed() / kill(reason)` 두 개지만, 내부에서 file watching + Telegram broadcast + state persistence + cooldown 을 다 처리합니다.

반대 예시 — **얕은** 거래소 wrapper:

```python
# 나쁨: SDK pass-through 만 함
class HyperliquidExchange:
    def __init__(self, client): self.c = client
    def fetch_position(self, sym): return self.c.fetch_position(sym)
    def fetch_balance(self):       return self.c.fetch_balance()
```

위 코드는 SDK 를 한 겹 더 감쌌을 뿐, abstraction 이득이 없습니다. AI 는 종종 이런 wrapper 를 양산합니다.

**깊은** 거래소 wrapper:

```python
class HyperliquidExchange:
    async def get_position(self, sym):
        # WS cache hit → 즉시 반환
        # cache miss → REST fallback + cache 갱신
        # 401 → token refresh 후 재시도
        # 빈 결과 → ghost position 검증 (05 챕터 패턴)
        # 정규화: side / size / entry_price / unrealized_pnl
        ...
```

`/improve-codebase-architecture` 는 이런 얕은 wrapper 를 찾아 "이걸 deep 으로 합치는 게 좋겠다" 고 제안합니다.

---

## 4. `/diagnose` — Feedback-Loop-First 디버깅

설명: 5 단계 — Reproduce → Minimise → Hypothesise → Instrument → Fix. 핵심은 **30 초 안에 "버그 났음 / 안 났음" 신호를 만드는 것** 을 우선합니다. 가설 세우기 전에 신호 먼저.

이 저장소가 V1 폴리마켓 봇에서 8 일간 ghost position 이 누적됐던 이유 — feedback loop 가 부재했기 때문입니다. "포지션이 두 배 됐다" 는 신호가 운영자에게 도달하기까지 8 일이 걸렸습니다. `/diagnose` 가 강제하는 흐름이라면 첫 시간 안에 잡혔습니다.

5 단계 봇 예시:

```
[Reproduce]    staging 에서 fetch_orders 401 강제 주입 → 빈 리스트 반환 확인
[Minimise]    봇 전체 띄울 필요 없이, verify_order_fill 단위 테스트 1 개로 재현
[Hypothesise] except 가 너무 넓다 / 401 을 빈 리스트로 변환 / 빈 리스트 = "미체결" 해석
[Instrument]  except 절에 logger.exception 추가 + 401 카운터 메트릭
[Fix]         401 만 따로 잡고 raise, 나머지는 명시적 도메인 에러
```

`05-debugging-with-ai.md` 의 "Root Cause 강제" 와 같은 사상이지만, `/diagnose` 는 **단계를 강제하는 자동화** 입니다.

---

## 5. `/zoom-out` — 코드 지도

설명: 새 영역에 들어왔을 때 한 단계 위 추상화로 모듈 관계를 보게 합니다. 디테일에 빠지기 전에 지도부터.

이 저장소의 `20-exchange-wrappers/` 는 21 개 거래소 통합입니다. 처음 진입하는 사람이 binance/client.py 부터 읽기 시작하면 길을 잃습니다. `/zoom-out` 을 먼저 돌리면:

```
> /zoom-out 20-exchange-wrappers/

[Layer 1: factory]
create_exchange(name, **keys) → 거래소별 클래스 인스턴스

[Layer 2: 공통 인터페이스]
모든 클래스가 fetch_position / place_order / cancel_order / fetch_balance 구현

[Layer 3: 거래소별 quirk]
- Lighter: __init__ 가 sync HTTP → subprocess 격리
- GRVT: 비슷
- Hyperliquid: Rust signing in 50-rust-acceleration/

[Layer 4: 테스트]
tests/exchanges/test_<name>.py 가 인터페이스 충족 검증
```

이 지도를 먼저 받으면 그 다음 코드 읽기는 30% 시간으로 끝납니다.

---

## 6. `/caveman` — 토큰 압축

설명: AI 응답에서 군더더기 (인사말 / 자평 / 중복 요약) 를 제거합니다. Matt 의 측정으로 약 75% 토큰 절감. 긴 세션 비용이 의미 있게 줄어듭니다.

사용법: 세션 중 한 번 `/caveman` 입력하면, 그 후 모든 응답이 압축 톤으로 바뀝니다. 봇 운영처럼 24 시간 컨텍스트가 누적되는 작업에 특히 효과적입니다.

비교 — 같은 답:

```
[일반]
좋은 질문입니다! 트레일링 스탑은 트레이더가 자주 쓰는 기법인데요,
이걸 구현하려면 먼저 현재가를 추적하고, 최고가를 갱신하고, 그 다음에
드로다운을 계산해야 합니다. 코드는 다음과 같습니다: ...

[caveman]
trailing.py:42 — high_water_mark 갱신, drawback >= 0.5% 시 exit
```

뒤쪽은 정보 손실 없이 토큰 1/8 입니다.

---

## Workflow 융합

이 6 개를 어떻게 한 흐름에 엮느냐가 핵심입니다.

```
[새 기능]
/grill-me <한 줄 아이디어>     → spec 완성
   ↓
Plan 모드 (CLAUDE.md)            → file:line 단위 plan
   ↓
/tdd                              → red-green-refactor
   ↓
구현
   ↓
/improve-codebase-architecture    → deep module 검증

[디버깅]
/diagnose                         → 5 단계 강제

[새 코드 진입]
/zoom-out <폴더>                  → 지도 먼저

[선택적 — 긴 세션]
/caveman                          → 토큰 75% 절감
```

---

## 이 저장소 기존 패턴과의 관계

| 우리 챕터 | 매트의 skill | 관계 |
|---|---|---|
| `02-prompting-patterns.md` Plan 모드 | `/grill-me` | grill-me 가 Plan 의 빈 칸을 interview 형식으로 자동화 |
| `05-debugging-with-ai.md` Root Cause 강제 | `/diagnose` | diagnose 가 5 단계를 더 구조화 |
| `10-foundation-modules/` deep module 철학 | `/improve-codebase-architecture` | 우리는 결과 예시, 매트는 active 적용 도구 |
| `02-prompting-patterns.md` Coding 프롬프트 | `/tdd` | tdd 가 한 단계 더 위 — 테스트가 먼저 |

기존 패턴을 **대체** 하는 게 아니라 **자동화** 하는 도구입니다. CLAUDE.md 에 "Plan 모드 먼저" 라고 적어두는 것과, slash 한 번에 40 개 질문이 쏟아지는 것의 차이입니다.

---

## 설치

```bash
# 1. 클론
gh repo clone mattpocock/skills ~/.claude/plugins/mattpocock-skills

# 2. ~/.claude/skills/ 로 복사 (engineering, productivity 폴더 안의 모든 skill)
for d in ~/.claude/plugins/mattpocock-skills/skills/{engineering,productivity}/*/; do
  cp -rf "$d" ~/.claude/skills/$(basename "$d")
done

# 3. Claude Code 재시작 후 / 입력하면 보임:
#    grill-me, tdd, diagnose, improve-codebase-architecture,
#    zoom-out, caveman, grill-with-docs, triage,
#    to-issues, to-prd, setup-matt-pocock-skills, write-a-skill
```

12 개 모두 설치됩니다. 이 챕터에서는 6 개만 다뤘지만, `triage` (이슈 트리아지) `to-prd` (PRD 변환) 등도 본인 워크플로우에 맞으면 유용합니다.

---

## 한 줄 요약

> AI 는 코드 생성 속도를 10 배 올리지만, craftsmanship 이 없으면 부채 누적도 10 배 빠릅니다. Matt 의 6 개 skill — `/grill-me` `/tdd` `/improve-codebase-architecture` `/diagnose` `/zoom-out` `/caveman` — 이 그 craftsmanship 을 slash command 로 자동화합니다.
