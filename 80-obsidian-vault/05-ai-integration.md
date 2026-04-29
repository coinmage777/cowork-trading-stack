# 05 — AI 연동

옵시디언 노트가 결국 마크다운 파일이라서, Claude Code 같은 AI 에게 그냥 절대경로로 시키면 됩니다. 별도 통합 플러그인이 필요 없습니다. 다만 잘 시키는 패턴과 함정이 있습니다.

## AI 가 옵시디언 파일을 읽는 패턴

### 절대경로로 Read

```
"내 볼트 Projects/Hyperliquid.md 를 읽고 핵심 메커니즘만 5 줄로 요약해줘.
파일 위치: /c/Users/A/Obsidian Vault/Projects/Hyperliquid.md"
```

Claude Code 의 Read tool 이 그냥 파일을 읽습니다. 한국어 / 영어 frontmatter 둘 다 잘 처리됩니다.

### 여러 노트 묶어서

```
"다음 두 노트를 비교해줘:
- /c/Users/A/Obsidian Vault/Projects/Hyperliquid.md
- /c/Users/A/Obsidian Vault/Projects/Lighter.md
"
```

긴 컨텍스트 모델일수록 묶음 비교가 자연스럽습니다.

### Glob 으로 패턴 매칭

특정 폴더의 모든 노트를 한꺼번에 보고 싶을 때:

```
"내 매매 일지 최근 7 개 (Trading/daily/) 를 보고
이번 주 PnL 패턴을 정리해줘."
```

AI 가 Glob 으로 파일 리스트를 뽑고 Read 로 하나씩 봅니다.

## AI 가 노트를 작성하는 패턴

### 새 노트 한 개

```
"Lighter 의 점수 메커니즘 리서치 노트를 작성해줘.
- 위치: /c/Users/A/Obsidian Vault/Projects/Lighter.md
- frontmatter 에 type: project, status: research, tier 비워두기
- 본문 헤더는: 한 줄 요약 / 메커니즘 / 토크노믹스 / 리스크 / 액션
- wikilink 가능하면 [[perp-dex-bot]], [[Hyperliquid]] 사용
"
```

### Inbox 자동 적재

세션 끝날 때 AI 에게:

```
"이번 세션에서 알게 된 내용을 Inbox 에 마크다운으로 떨궈줘.
- 파일명: YYYY-MM-DD - <topic>.md (오늘 날짜)
- 위치: /c/Users/A/Obsidian Vault/Inbox/
- 형식: 헤더 + bullet, frontmatter 없음
"
```

분류는 나중에 사람이 합니다 (또는 다른 세션에서 AI 에게 분류 시킴).

## "이 노트 요약" 프롬프트

긴 리서치 노트 요약:

```
"/c/Users/A/Obsidian Vault/Projects/Hyperliquid.md 를 읽고
- 한 줄 요약
- 메커니즘 핵심 3 가지
- 내가 모르고 있을 만한 디테일 2 가지
형식으로 답해줘."
```

"내가 모를 만한" 같은 메타 프롬프트가 의외로 효과 좋습니다 (검토용).

## "관련 노트 찾기" 프롬프트

backlink 가 부족할 때 AI 에게 의미 기반 매칭 시키기:

```
"Projects/Hyperliquid.md 와 주제가 관련된 노트를 내 볼트에서 찾아줘.
- Projects/ 와 Trading/ 폴더만 보면 됨
- 파일 이름 + 한 줄 이유로 답해줘
- wikilink 형식 [[..]] 으로
"
```

결과를 `Hyperliquid.md` 의 Related 섹션에 붙여 넣으면 됩니다.

## MemKraft / Obsidian 메모리 vs Claude memory

비교:

| 항목 | Claude built-in memory | Obsidian + MemKraft |
|------|----------------------|---------------------|
| 위치 | Anthropic 서버 / 세션 | 로컬 마크다운 |
| 영구성 | 세션 / 계정 단위 | 영구, 백업 가능 |
| AI 가 직접 수정 | 일부 가능 | Read / Write 자유 |
| 사람이 편집 | 어려움 | 그냥 텍스트 편집 |
| 다른 AI 와 공유 | 불가 | 가능 (그냥 파일) |

### 보완 관계

- **Claude memory** 는 사용자 선호 / 결정 스타일 같은 "톤" 정보
- **MemKraft (옵시디언)** 는 entity 단위 진행 상황 / 사실
- 둘이 충돌하지 않게: Claude memory 에는 "Obsidian Memory/MEMORY.md 를 읽으라" 는 포인터를 두고 사실 데이터는 옵시디언에 둠

예: 사용자 메모리에는

```
- 사용자 본인은 입문 바이브코더
- 사실 데이터는 ~/Obsidian Vault/Memory/MEMORY.md 에서 읽기
```

이렇게 두면 AI 가 새 세션에서 자연스럽게 옵시디언을 본다.

## AI 세션을 Inbox 로 자동 적재

세션 끝 hook 또는 사용자 명령어:

```
"이번 세션 transcript 의 핵심을 정리해서
Inbox/<오늘날짜> - <세션 주제>.md 로 떨궈줘.
- 작업한 파일 목록
- 결정한 사항
- 미해결 이슈
- 다음 세션에 봐야 할 것
"
```

이렇게 떨군 Inbox 노트를 다음 세션 시작할 때 AI 가 읽어보면 컨텍스트가 자연스럽게 이어집니다.

자동화 (선택): Claude Code 의 settings.json hook 으로 세션 stop 시 자동 실행. 단 자동 실행은 Inbox 가 너무 빨리 차므로, 명시적으로 부르는 패턴이 무난합니다.

## AI 가 wikilink 잘못 만드는 함정

자주 보는 실수:

### 1. 없는 노트로 wikilink 만들기

AI 가 본문에 `[[BTC-Dominance-2026-Q1-분석]]` 처럼 **너무 구체적인** wikilink 를 만듭니다. 그런 노트는 없습니다. unresolved link 가 늘어납니다.

방지: 프롬프트에

```
"wikilink 는 실제 존재하는 노트만 사용. 확실하지 않으면 일반 텍스트로 써줘."
```

### 2. alias hallucinate

`[[Hyperliquid|HL팀]]` 처럼 사실이 아닌 라벨. alias 는 본문 가독성용이지 라벨링용 아닙니다.

방지: 처음 wikilink 들어갈 때만 alias, 두 번째부터 이미 본문에 풀 이름이 나왔으면 그냥 `[[Hyperliquid]]`.

### 3. 헤더 링크 깨짐

`[[Hyperliquid#메커니즘]]` 인데 그 노트에 `## 메커니즘` 헤더가 없으면 깨집니다. AI 가 헤더 이름을 추측하면 자주 틀립니다.

방지: 헤더 링크는 사람이 직접. AI 에게는 노트 단위 wikilink 만 시키세요.

### 4. 폴더 경로 누락

같은 이름의 노트가 여러 폴더에 있을 때 (`Inbox/Hyperliquid.md`, `Projects/Hyperliquid.md`) AI 가 어느 쪽인지 모릅니다.

방지: 프롬프트에 폴더 경로 명시 — `[[Projects/Hyperliquid|Hyperliquid]]`.

## frontmatter 형식 일관성

AI 에게 노트를 만들게 할 때 frontmatter 형식을 정확히 알려주세요.

```
"frontmatter 는 정확히 이 형식:
---
type: project
status: research
tier: 
last_review: YYYY-MM-DD (오늘 날짜)
tags: [perp-dex]
related: []
---
"
```

이걸 안 알려주면 AI 가 임의로 키 이름을 만들어서 Dataview 쿼리가 안 잡힙니다. 한 번 형식이 흐트러지면 일괄 수정해야 해서 미리 알려주는 게 비용이 적습니다.

## 정리: 좋은 AI + 옵시디언 워크플로우

1. AI 세션 시작 시 `Memory/MEMORY.md` 읽히기 (포인터 따라 자동)
2. 작업하면서 새 정보 → Inbox 떨구기
3. 세션 끝날 때 Inbox 정리 / 분류 (사람이 또는 AI 에게)
4. 일주일 한 번 Inbox 비우기 (Projects / Trading / Memory 로)
5. 매월 한 번 wikilink / Unresolved 정리 (앞 챕터 참고)

이 흐름이 굴러가면 옵시디언이 AI 의 외부 메모리 역할을 합니다. 새 모델이 나와도 데이터는 그대로 남습니다.
