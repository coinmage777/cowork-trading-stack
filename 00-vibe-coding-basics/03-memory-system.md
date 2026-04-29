# 03 - 메모리 시스템

Claude Code의 메모리는 두 층입니다.

1. **`CLAUDE.md`** — 프로젝트 레벨. 레포 안에 체크인. 이 프로젝트에서만 적용되는 규칙.
2. **Auto-memory** — 세션을 넘어 지속되는 사용자 메모리. `~/.claude/projects/<encoded-cwd>/memory/MEMORY.md`에 저장. 사용자 본인의 선호/습관/이력.

이 둘을 헷갈려서 한 곳에 다 욱여넣으면 둘 다 망가집니다.

## CLAUDE.md (프로젝트 레벨)

레포 루트(`/CLAUDE.md`)나 서브폴더(`/some-module/CLAUDE.md`)에 두면 Claude Code가 자동으로 읽습니다. 서브폴더는 그 폴더에서 작업할 때만 로드.

### 프로젝트 CLAUDE.md에 들어가야 할 것

- 이 코드베이스의 **불변 규칙** (예: "DB 마이그레이션은 항상 alembic, 직접 SQL 금지")
- 자주 쓰는 명령 (예: "테스트는 `pytest -x -v`, 린트는 `ruff check .`")
- 위험한 영역 경고 (예: "`order_executor.py`는 실제 자금 다룸 — 수정 시 plan 필수")
- 빌드/배포 절차
- 도메인 용어 정의

### 들어가면 안 되는 것

- 사용자 개인 정보 (이메일, API 키)
- 사용자의 코딩 스타일 선호 (이건 auto-memory)
- 일회성 TODO

### 저자의 실제 CLAUDE.md 핵심 줄

```
- 코드 수정 전 관련 파일 최소 5개 이상 읽기
- 추측으로 답하지 말 것 — 모르면 Grep/Read로 확인
- 변경 후 반드시 검증 — AST 문법 체크, 테스트 실행, 로그 확인 중 하나
- 한 번에 하나씩 — 여러 변경 동시 X
- 구현 전에 반드시 Plan 모드로 설계부터
- 데이터 기반 결정 — 파라미터 조정 시 DB/로그에서 실제 수치 뽑고 판단
- AI 협업 게으름 방지 (안 읽고 답하지 말 것)
```

이 7줄이 프로젝트 CLAUDE.md의 첫 화면입니다. 이게 없으면 AI가 갈수록 게을러집니다.

## Auto-memory (사용자 레벨)

Claude Code는 대화 중에 사용자가 "이거 기억해 둬"라고 하거나 명백한 패턴을 보이면 자동으로 메모리에 적습니다. 위치는 OS마다 다르지만 일반적으로:

- Mac/Linux: `~/.claude/projects/<encoded>/memory/MEMORY.md`
- Windows: `C:\Users\<user>\.claude\projects\<encoded>\memory\MEMORY.md`

저자는 이 파일을 **인덱스**로만 쓰고, 실제 내용은 분리된 `.md` 파일들로 정리합니다. 이유: MEMORY.md가 길어지면 매 세션 시작 시 토큰을 너무 먹고, 검색도 어렵습니다.

### 레이어드 메모리 구조

```
~/.claude/projects/<...>/memory/
├── MEMORY.md                              # 인덱스 (한 줄 요약 + 링크)
├── user_profile.md                        # 사용자 정체성
├── feedback_decision_style.md             # 피드백/의사결정 선호
├── feedback_private_repo_autonomous.md
├── project_openwebui_comfyui.md           # 프로젝트별 노트
├── project_perp_dex_2026_04_27.md
├── project_config_yaml_plaintext_pk.md
├── reference_api_keys.md                  # 외부 참조 정보
├── reference_predict_fun_referral.md
└── reference_nansen_api.md
```

`MEMORY.md`의 한 줄:

```markdown
- [User profile](user_profile.md) — Coinmage crypto creator, Windows 11 + RTX 5070 Ti, ComfyUI Desktop installed
- [Decision style](feedback_decision_style.md) — when multiple setup paths exist, user wants me to pick the best one autonomously
- [Open WebUI + ComfyUI gotchas](project_openwebui_comfyui.md) — ComfyUI Desktop uses port 8000 not 8188; Korean Windows needs PYTHONIOENCODING=utf-8
```

이런 식으로 인덱스 파일 하나만 매번 읽히고, 실제 상세는 필요할 때만 찾아 읽도록 유도합니다.

## 4가지 메모리 타입

저자가 운용하는 분류:

### 1. user_*

사용자 정체성, 환경, 도구.

- 운영체제, 하드웨어 (예: Windows 11 + RTX 5070 Ti)
- 자주 쓰는 에디터, 셸, 터미널
- 직무/관심사 (예: crypto content creator)
- 언어 선호 (한국어 존댓말)

업데이트 빈도: 거의 없음. 변동이 있을 때만.

### 2. feedback_*

사용자가 AI에게 어떻게 일하길 원하는지.

- 자율성 수준 (예: "private repo는 묻지 말고 commit/push/merge")
- 톤/스타일 (예: "이모지 금지, 짧은 문단, 형용사 최소화")
- 검증 강도 (예: "변경 후 AST + 테스트 둘 다")

업데이트 빈도: 가끔. 사용자가 "다음부터는 X로 해줘"라고 하면 즉시 적습니다.

### 3. project_*

특정 프로젝트의 함정, 결정 이력, 개선 백로그.

- 알려진 버그/제약 (예: "ComfyUI Desktop은 8188이 아니라 8000")
- 미적용 백로그 (예: "Phase C-heavy 8개 미적용")
- 보안 이슈 (예: "config.yaml line 384, 486에 plaintext 개인키 있음")

업데이트 빈도: 작업 단위로. 한 사이클 끝날 때마다 갱신.

### 4. reference_*

외부 정보 — API 키, 가입 코드, URL, 자격증명 위치.

- API 키 위치 (실제 키가 아니라 "Obsidian Vault/Memory/API-Keys.md에 있음" 같은 포인터)
- 가입 코드
- 외부 서비스 엔드포인트

업데이트 빈도: 새 서비스 가입 시.

**키는 메모리에 직접 저장하지 마세요.** 위치 포인터만 저장하고, 실제 키는 OS 키체인이나 password manager에.

## 언제 무엇을 적을 것인가

세션 끝에 "오늘 뭘 배웠나, 뭐가 의미 있나"를 자문합니다.

- 같은 함정에 두 번 이상 빠졌다 → `project_*`에 기록
- 사용자가 "다음부턴 이렇게 해줘"라고 했다 → `feedback_*`
- 외부 시스템의 비자명한 동작 (예: 한글 Windows + Python 인코딩) → `project_*` 또는 `user_*`
- 일회성 TODO → 메모리에 적지 말고 이슈 트래커에

매 세션 끝마다 다 적을 필요는 없습니다. **재발 가능성이 있는 것만**.

## CLAUDE.md vs auto-memory 의사결정 트리

```
이 정보가 다른 사람에게도 유효한가?
├── Yes → CLAUDE.md (프로젝트 레포에 commit)
└── No → auto-memory
        │
        이 정보가 모든 프로젝트에 적용되는가?
        ├── Yes → ~/.claude/CLAUDE.md (글로벌)
        └── No → ~/.claude/projects/<this>/memory/
```

## 메모리 위생

- 6개월에 한 번씩 MEMORY.md 인덱스를 훑고 죽은 항목 정리
- 같은 정보가 여러 파일에 중복되면 통합
- 너무 길어진 파일은 쪼갬 (`project_perp_dex.md` → `project_perp_dex_phase_a.md`, `project_perp_dex_phase_b.md`)

게으르면 메모리가 덤프 폴더가 되고, 그 시점부터 AI가 매 세션마다 잘못된 가정을 깔고 시작합니다.
