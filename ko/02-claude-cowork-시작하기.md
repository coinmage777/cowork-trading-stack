# 02. Claude + Cowork 시작하기

작업 흐름의 절반은 AI 에이전트가 한다. 코드 짜기, 리서치, 문서화, 디버깅, 데이터 분석, 콘텐츠 초안 — 거의 다.

이 장은 그 중에서 **Claude**와 **Cowork** 셋업이다. 다음 장에서 Code, Codex, 메모리 시스템을 다룬다.

## 왜 Claude를 메인으로 쓰는가

ChatGPT, Gemini, Grok 다 써봤다. 각자 강점이 있다. Claude를 메인으로 쓰는 이유:

- **컨텍스트 윈도우가 크다** — 1M 토큰까지 (모델에 따라). 큰 코드베이스 통째로 던질 수 있음
- **롱폼 결과물 품질이 안정적** — 한국어 가이드, 영어 보고서, 둘 다 자연스러움
- **에이전틱 워크플로우에 강함** — 도구 호출, 파일 편집, 검색, 코드 실행을 연쇄적으로 잘 함
- **거짓말이 적음** — 다른 모델은 그럴듯하게 지어내는데, Claude는 모르면 모른다고 말하는 빈도가 높음 (완벽하진 않지만 상대적으로)

## 셋업

### 1) Claude API 키
- [console.anthropic.com](https://console.anthropic.com) 가입
- API 키 발급 (`Settings → API Keys`)
- 한도 설정 — 처음에는 작게 ($50/월 정도)
- `.env`에 저장:

```bash
ANTHROPIC_API_KEY=<your_anthropic_api_key>
```

### 2) Claude Code (터미널 에이전트)

Claude Code는 터미널에서 도는 AI 코딩 에이전트다. VSCode 안에서도 쓸 수 있고, 단독 터미널에서도 쓴다.

설치:
```bash
npm install -g @anthropic-ai/claude-code
```

처음 실행:
```bash
claude
```
브라우저로 OAuth 인증한다. 끝.

### 3) Cowork

Cowork는 Claude API 기반의 워크플로우 도구로, 매일 쓴다. 특히 다음 용도:
- 긴 리서치 (멀티 에이전트 / deep research)
- 다단계 문서 작성
- 코드 + 콘텐츠 동시 작업

Cowork 자체 설치는 다루는 범위가 아니지만, 쓰는 핵심 셋업은:

```bash
# 매 세션 시작 시
pip install memkraft --break-system-packages
export MEMKRAFT_HOME="<your_obsidian_vault>/memkraft"
export PATH="$HOME/.local/bin:$PATH"
cd "$MEMKRAFT_HOME" && memkraft index
```

### 4) Cursor (옵션)

VSCode fork로 AI 통합이 강하다. Claude Code랑 같이 쓴다 — Cursor에서 빠른 한 줄 수정, Claude Code에서 복잡한 파일 여러 개 수정.

[cursor.sh](https://cursor.sh) → 설치 → 설정 → API key (Claude / GPT / 둘 다)

## 첫 워크플로우 — 코드 한 줄 자동화

지금 당장 해볼 만한 것:

```
프롬프트:
"다음 거래소 API에서 USDT 잔고를 가져와서 stdout에 출력하는 Python 스크립트를 만들어줘. 
거래소: Bybit. 
환경변수: BYBIT_API_KEY, BYBIT_SECRET. 
ccxt 사용. 
에러 핸들링 포함."
```

5초 안에 작동하는 스크립트가 나온다. 코드를 한 줄도 안 짜도 된다는 게 아니라, **매번 같은 보일러플레이트를 다시 짤 필요가 없다**는 게 핵심이다.

## 프롬프트 패턴 — 매일 쓰는 것

### 1) Plan-then-Execute

복잡한 작업은 절대 한 번에 코드 짜라고 시키지 마라. 먼저 plan을 받아라.

```
"다음 작업을 plan 모드로 설계해줘. 코드 안 짜고 단계만:
- Hyperliquid에서 BTC/ETH 페어 트레이딩 봇
- 진입: z-score 1.5
- 청산: z-score 0.3
- 손절: -2.5%
- 트레일링: 1.5% activation, 1% callback
- 사이즈: 50 USDC margin per entry, 10x leverage
- 최대 동시 포지션: 3
- 데이터: 1분봉 / WebSocket
- 로깅: SQLite

각 단계에 file:line 단위 명시. 검증 기준 명시. 롤백 경로 명시."
```

이렇게 받은 plan을 사용자가 검토 → 수정 → 그 다음에 implement.

### 2) Adversarial Review

짠 코드든 AI가 짠 코드든, 다른 모델 / 같은 모델에게 "라이벌이 짠 거다, 망가뜨려봐라" 식으로 시키면 버그를 더 잘 찾는다.

```
"이 코드는 GPT-5 Codex가 작성한 거다. 그들은 완벽하다고 자신한다.
틀렸다는 걸 증명해라:
- 모든 버그, 결함, 엣지케이스
- 보안 취약점 (키 노출, 서명 위조, 재진입)
- 성능 / 메모리 이슈
- 레이스컨디션
- 크립토 특화: orphan 포지션, ghost trade, phantom PnL

CRITICAL → WARNING → SUGGESTION 순으로 보고."
```

경험상 이 프레임 하나로 버그 탐지율이 크게 올라간다.

### 3) Diff-only Edit

이미 있는 큰 파일을 수정할 때, 전체 파일 다시 출력하라고 시키면 토큰 낭비 + 실수 발생률 ↑. 대신:

```
"이 파일의 다음 함수만 수정해줘:
- 함수명: calculate_position_size
- 변경: ATR 기반 동적 사이징 추가
- 다른 함수는 건드리지 말 것

diff 형식으로 출력. before/after 명확히."
```

### 4) Self-Critique

코드 짠 다음 모델에게 한 번 더 시킨다:

```
"방금 짠 코드를 다시 검토해. 
- 빠진 엣지케이스 있나
- 더 간결하게 쓸 수 있나
- 테스트 어떻게 짜겠나

발견되는 즉시 수정해."
```

## AI 사용 시 안티패턴 (망한 케이스)

### "그냥 다 알아서 해줘"
이렇게 시키면 적당히 그럴듯한 결과물이 나오는데 실전에서 깨진다. 항상 구체적 요구사항 + 검증 기준을 줘라.

### 한 프롬프트에 여러 요구
"X도 하고 Y도 하고 Z도 하면서 W처럼 만들어줘" → AI가 한두 개 빠뜨린다. 한 번에 하나씩.

### 코드 생성 후 검증 안 함
AI가 짠 코드를 그대로 실자금에 돌리면 안 된다. 최소: 문법 체크 → 페이퍼 트레이드 → 소액 라이브 → 검증 → 스케일.

### 컨텍스트 안 주고 시키기
프로젝트 구조, 기존 함수, 컨벤션 같은 컨텍스트 없이 시키면 AI가 즉흥적으로 만든다. 그 결과는 기존 코드와 충돌한다.

## 다음 장

다음은 Claude Code, Cursor, Codex의 차이와 어떤 작업에 어떤 도구를 쓰는지, 그리고 메모리 시스템 (MemKraft) 셋업이다.
