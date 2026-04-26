# 02. Claude + Cowork 시작하기

작업 흐름의 절반은 AI 에이전트가 수행합니다. 코드 작성, 리서치, 문서화, 디버깅, 데이터 분석, 콘텐츠 초안까지 거의 전부에 해당합니다.

이 장은 그중에서 **Claude**와 **Cowork** 셋업을 다룹니다. 다음 장에서 Code, Codex, 메모리 시스템을 다룹니다.

## 왜 Claude를 메인으로 사용하는가

ChatGPT, Gemini, Grok 모두 사용해 보았습니다. 각자 강점이 있습니다. Claude를 메인으로 사용하는 이유는 다음과 같습니다.

- **컨텍스트 윈도우가 큽니다** — 1M 토큰까지 지원합니다 (모델에 따라). 큰 코드베이스를 통째로 전달할 수 있습니다.
- **롱폼 결과물 품질이 안정적입니다** — 한국어 가이드, 영어 보고서 모두 자연스럽습니다.
- **에이전틱 워크플로우에 강합니다** — 도구 호출, 파일 편집, 검색, 코드 실행을 연쇄적으로 잘 수행합니다.
- **거짓말이 적습니다** — 다른 모델은 그럴듯하게 지어내는 경향이 있는 반면, Claude는 모르면 모른다고 답하는 빈도가 높습니다 (완벽하지는 않지만 상대적으로 그렇습니다).

## 셋업

### 1) Claude API 키
- [console.anthropic.com](https://console.anthropic.com) 가입
- API 키 발급 (`Settings → API Keys`)
- 한도 설정 — 처음에는 작게 설정하는 것을 권장합니다 ($50/월 정도)
- `.env`에 저장:

```bash
ANTHROPIC_API_KEY=<your_anthropic_api_key>
```

### 2) Claude Code (터미널 에이전트)

Claude Code는 터미널에서 동작하는 AI 코딩 에이전트입니다. VSCode 안에서도 사용할 수 있고, 단독 터미널에서도 사용할 수 있습니다.

설치:
```bash
npm install -g @anthropic-ai/claude-code
```

처음 실행:
```bash
claude
```
브라우저로 OAuth 인증을 진행하면 끝입니다.

### 3) Cowork

Cowork는 Claude API 기반의 워크플로우 도구이며, 매일 사용합니다. 특히 다음 용도에 활용합니다.
- 긴 리서치 (멀티 에이전트 / deep research)
- 다단계 문서 작성
- 코드 + 콘텐츠 동시 작업

Cowork 자체 설치는 다루는 범위가 아니지만, 사용에 필요한 핵심 셋업은 다음과 같습니다.

```bash
# 매 세션 시작 시
pip install memkraft --break-system-packages
export MEMKRAFT_HOME="<your_obsidian_vault>/memkraft"
export PATH="$HOME/.local/bin:$PATH"
cd "$MEMKRAFT_HOME" && memkraft index
```

### 4) Cursor (옵션)

VSCode fork로 AI 통합이 강합니다. Claude Code와 함께 사용합니다 — Cursor에서는 빠른 한 줄 수정, Claude Code에서는 복잡한 파일 여러 개 수정에 활용합니다.

[cursor.sh](https://cursor.sh) → 설치 → 설정 → API key (Claude / GPT / 둘 다)

## 첫 워크플로우 — 코드 한 줄 자동화

지금 당장 시도해 볼 만한 예시입니다.

```
프롬프트:
"다음 거래소 API에서 USDT 잔고를 가져와서 stdout에 출력하는 Python 스크립트를 만들어줘. 
거래소: Bybit. 
환경변수: BYBIT_API_KEY, BYBIT_SECRET. 
ccxt 사용. 
에러 핸들링 포함."
```

5초 안에 작동하는 스크립트가 생성됩니다. 코드를 한 줄도 작성하지 않아도 된다는 의미가 아니라, **매번 같은 보일러플레이트를 다시 작성할 필요가 없다**는 점이 핵심입니다.

## 프롬프트 패턴 — 매일 사용하는 것

### 1) Plan-then-Execute

복잡한 작업은 절대 한 번에 코드를 작성하라고 시키지 마세요. 먼저 plan을 받으시기 바랍니다.

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

이렇게 받은 plan을 사용자가 검토 → 수정한 뒤에 implement 단계로 넘어갑니다.

### 2) Adversarial Review

작성한 코드든 AI가 작성한 코드든, 다른 모델 또는 같은 모델에게 "라이벌이 작성한 것이다, 망가뜨려 보아라" 식으로 시키면 버그를 더 잘 찾아냅니다.

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

경험상 이 프레임 하나만으로도 버그 탐지율이 크게 올라갑니다.

### 3) Diff-only Edit

이미 존재하는 큰 파일을 수정할 때, 전체 파일을 다시 출력하라고 시키면 토큰 낭비와 실수 발생률이 함께 증가합니다. 대신 다음과 같이 요청합니다.

```
"이 파일의 다음 함수만 수정해줘:
- 함수명: calculate_position_size
- 변경: ATR 기반 동적 사이징 추가
- 다른 함수는 건드리지 말 것

diff 형식으로 출력. before/after 명확히."
```

### 4) Self-Critique

코드를 작성한 다음 모델에게 한 번 더 시킵니다.

```
"방금 짠 코드를 다시 검토해. 
- 빠진 엣지케이스 있나
- 더 간결하게 쓸 수 있나
- 테스트 어떻게 짜겠나

발견되는 즉시 수정해."
```

## AI 사용 시 안티패턴 (실패 사례)

### "그냥 다 알아서 해줘"
이렇게 시키면 적당히 그럴듯한 결과물이 나오지만 실전에서 깨집니다. 항상 구체적 요구사항과 검증 기준을 함께 제시하시기 바랍니다.

### 한 프롬프트에 여러 요구
"X도 하고 Y도 하고 Z도 하면서 W처럼 만들어줘" → AI가 한두 개를 빠뜨립니다. 한 번에 하나씩 요청하세요.

### 코드 생성 후 검증 안 함
AI가 작성한 코드를 그대로 실자금에 돌리면 안 됩니다. 최소한 다음 절차를 거쳐야 합니다: 문법 체크 → 페이퍼 트레이드 → 소액 라이브 → 검증 → 스케일.

### 컨텍스트 안 주고 시키기
프로젝트 구조, 기존 함수, 컨벤션 같은 컨텍스트 없이 시키면 AI가 즉흥적으로 만듭니다. 그 결과는 기존 코드와 충돌합니다.

## 다음 장

다음 장에서는 Claude Code, Cursor, Codex의 차이와 어떤 작업에 어떤 도구를 사용하는지, 그리고 메모리 시스템 (MemKraft) 셋업을 다룹니다.
