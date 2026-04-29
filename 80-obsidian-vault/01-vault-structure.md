# 01 — Vault 폴더 구조

크립토 리서치와 트레이딩 일지를 같이 두는 볼트의 폴더 구조입니다. 정답은 없고, 시간이 지나면서 자연스럽게 굳어진 구조를 공유합니다.

## 최상위 폴더

```
Obsidian Vault/
├── Inbox/          # 분류 안 된 raw 노트
├── Projects/       # 프로젝트별 리서치
├── Trading/        # 매매 일지 / 전략
├── Airdrop/        # 에어드랍 추적
├── Memory/         # AI 메모리, 레퍼런스
├── Content/        # 블로그 / YT / 텔레그램 / 트위터 초안
├── Dashboard/      # Dataview 라이브 보드
├── Templates/      # Templater 템플릿
├── docs/           # 시스템 문서 (자기 문서화)
├── scripts/        # 자동화 스크립트
├── assets/         # 이미지 / 첨부
└── Archive/        # 더 이상 active 아닌 것
```

## 폴더별 역할과 명명

### Inbox/

분류 전 dump. 빠르게 떨구고 나중에 정리합니다.

명명: `YYYY-MM-DD - <topic>.md`

```
Inbox/
  2026-04-26 - hyperliquid funding 패턴.md
  2026-04-26 - lighter 점수 메커.md
  2026-04-27 - reconcile partial fill 디버깅.md
```

원칙: Inbox 에 있으면 미완성. 일주일에 한 번씩 비웁니다.

### Projects/

한 프로젝트 = 한 노트. 메커니즘 / 토크노믹스 / 리서치 / 액션 아이템을 한 파일에 누적합니다.

명명: `<ProjectName>.md` (PascalCase 또는 그냥 공식 이름)

```
Projects/
  Hyperliquid.md
  Lighter.md
  GRVT.md
  Predict-Fun.md
```

여러 프로젝트가 한 카테고리에 묶이면 서브폴더:

```
Projects/
  perp-dex/
    Hyperliquid.md
    Lighter.md
    GRVT.md
  prediction-market/
    Hyperliquid.md
    Predict-Fun.md
```

### Trading/

매매 활동. 일별 일지와 전략 가이드는 분리합니다.

```
Trading/
  daily/
    2026-04-26.md
    2026-04-27.md
  strategy/
    pair-trading-기본.md
    funding-arb.md
  onchain/
    btc-dominance-체크.md
```

`daily/` 는 매일, `strategy/` 는 가끔, `onchain/` 은 시장 환경 메모.

### Airdrop/

진행 중인 에어드랍을 한 노트씩. Kanban 플러그인으로 보드를 만들어도 좋습니다.

명명: `<Project>.md` 또는 `<Project>-airdrop.md`

```
Airdrop/
  Hyperliquid-airdrop.md
  Lighter-airdrop.md
  Nado-airdrop.md
  _kanban-board.md
```

`_kanban-board.md` 처럼 underscore prefix 로 메타 노트 구분.

### Memory/

AI 가 cross-session 으로 참조할 영구 정보. API 키 인덱스, 자주 쓰는 명령어, 개인 컨텍스트.

```
Memory/
  MEMORY.md          # 인덱스
  API-Keys.md        # 키 위치 (값은 .env 에)
  user-profile.md    # 자기 소개
  decision-style.md  # AI 에게 알려줄 결정 스타일
```

### Content/

콘텐츠 초안.

```
Content/
  blog/
    2026-04-26 - hyperliquid 입문.md
  youtube/
    스크립트-perp-dex-bot-소개.md
  telegram/
    weekly-summary.md
  twitter/
    threads-04-26-hyperliquid.md
```

### Dashboard/

Dataview / DataviewJS 가 들어간 라이브 보드. 노트라기보다 뷰입니다.

```
Dashboard/
  Home.md
  Airdrop-tracker.md
  PnL-week.md
  TGE-calendar.md
```

### Templates/

Templater 가 읽어가는 템플릿. 이 폴더는 Templater 설정에서 template folder 로 지정합니다.

```
Templates/
  daily-trade.md
  weekly-review.md
  project-research.md
  airdrop-tracking.md
  ai-session.md
```

### docs/, scripts/, assets/

볼트 자기 문서화 (`docs/memory.md`), 자동화 스크립트 (Inbox 정리 파이썬), 이미지 첨부.

### Archive/

active 아닌 것. 폴더 그대로 옮깁니다 (`Projects/Lighter.md` → `Archive/Projects/Lighter.md`).

## 한 노트가 여러 폴더에 걸칠 때

예: Hyperliquid 에어드랍 노트는 Airdrop / Projects / Trading 셋 다 관련.

처리 원칙:

1. 메인 위치 한 군데로 (예: `Airdrop/Hyperliquid-airdrop.md`)
2. 다른 폴더 노트에서 wikilink 로 참조 (`Projects/Hyperliquid.md` 안에 `[[Hyperliquid-airdrop]]`)
3. 태그로 cross-cut 관점 추가 (`#airdrop`, `#perp-dex`)

복사 / 중복 노트 만들지 마세요. wikilink 가 있는 이유입니다.

## 명명 규칙 요약

- 날짜 prefix: `YYYY-MM-DD - topic.md` (Inbox / daily 일지 / 콘텐츠)
- 프로젝트명: `ProjectName.md` (공식 이름 그대로)
- 메타 노트: `_name.md` (underscore prefix, sort 시 위쪽)
- 모두 소문자 또는 일관된 case (혼재 비추천)

## 예시 노트 1: 프로젝트 리서치

`Projects/Hyperliquid.md`:

```markdown
---
type: project
status: active
tier: S
last_review: 2026-04-26
tags: [perp-dex, defi, l1]
related: ["[[Pair-Trading]]", "[[perp-dex-bot]]"]
---

# Hyperliquid

## 한 줄 요약
HL 자체 L1 위에서 동작하는 perp DEX. 오더북 모델.

## 메커니즘
- HyperCore (matching engine) + HyperEVM (스마트 컨트랙트)
- ...

## 액션
- [ ] 신규 페어 추가 시 [[perp-dex-bot]] config 갱신
- [x] referral 등록
```

## 예시 노트 2: 매매 일지

`Trading/daily/2026-04-27.md`:

```markdown
---
date: 2026-04-27
pnl: +127
volume: 8400
exchanges: [hyperliquid, lighter]
---

# 2026-04-27

## 시장
- BTC 는 옆걸음, 펀딩 0.01% 부근

## 거래
| 시간 | 거래소 | 페어 | 사이드 | PnL |
|------|--------|------|--------|-----|
| 09:30 | HL | BTC-PERP | long | +85 |

## 메모
오늘은 [[Hyperliquid]] 펀딩 패턴이 평소와 달랐음.
```

frontmatter 의 `pnl`, `date` 가 Dataview 쿼리의 재료가 됩니다 (다음 챕터).
