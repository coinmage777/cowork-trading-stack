# 00 - Vibe Coding Basics

이 폴더는 AI 코딩(주로 Claude Code, Cursor, Codex)을 처음 실전에 적용하려는 분들을 위한 진입점입니다. 추상적인 조언이 아니라, 저자가 실제 크립토 트레이딩 봇을 만들면서 정착된 패턴만 모았습니다.

대상 독자: Claude Code/Cursor/Codex 이름은 들어봤지만, 아직 실제로 돌아가는 시스템을 AI와 함께 만들어본 적이 없는 분.

## 왜 "vibe coding"인가

"분위기로 코딩한다"는 비꼼이 아니라, AI에게 의도를 전달하고 결과를 검증하는 일련의 협업 프로토콜을 가리킵니다. 키보드를 두드리는 시간보다 프롬프트를 다듬고 결과물을 review하는 시간이 더 길어집니다.

## 읽는 순서

1. [`01-claude-code-setup.md`](01-claude-code-setup.md) — Claude Code CLI 설치, MCP 서버, slash command, IDE 연동. Mac/Windows(한글 Windows 포함) 둘 다.
2. [`02-prompting-patterns.md`](02-prompting-patterns.md) — Planning / Coding / Review / Debugging 4가지 모드별 프롬프트 패턴. "Gaslight My AI"(라이벌 모델 framing) 포함.
3. [`03-memory-system.md`](03-memory-system.md) — `CLAUDE.md`(프로젝트 레벨) + auto-memory(세션 간) 운용법. user/feedback/project/reference 4 타입 분류.
4. [`04-cowork-workflow.md`](04-cowork-workflow.md) — tmux 세션, `/loop` 모드, background agent, Telegram 모바일 원격 제어, "trigger file" 패턴(Windows에서 SIGHUP 대체).
5. [`05-debugging-with-ai.md`](05-debugging-with-ai.md) — AI가 만든 버그를 잡는 법. 예외 묵살, 가짜 fallback, hallucinated API. 실제 손실로 배운 케이스 3개.
6. [`06-craftmanship-patterns.md`](06-craftmanship-patterns.md) — Matt Pocock 의 12 skill 중 6 개 핵심 패턴 (`/grill-me` `/tdd` `/improve-codebase-architecture` `/diagnose` `/zoom-out` `/caveman`). craftsmanship 을 slash command 로 자동화.
7. [`07-deep-modules.md`](07-deep-modules.md) — shallow → deep 리팩토링 3 패턴 (Symbol/Market 타입, `__getattr__` proxy, FreshnessAwareCache).

## 한 줄 원칙

> 코드 수정 전 관련 파일 5개 이상 읽기. 추측 금지, Grep/Read로 확인. 변경 후 반드시 검증.

이 세 줄이 안 되면 AI는 그냥 빠르게 망가지는 코드 생성기입니다.
