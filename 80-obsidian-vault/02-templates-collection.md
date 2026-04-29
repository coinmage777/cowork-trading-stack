# 02 — 템플릿 모음

같은 형식의 노트를 자주 만든다면 템플릿이 답입니다. Templater 플러그인을 설치하고 `Templates/` 폴더를 template folder 로 지정해두면, hotkey 한 번으로 frontmatter 가 채워진 노트가 생성됩니다.

이 챕터는 12 개 템플릿을 다룹니다. 모두 frontmatter + 본문 구조입니다.

## 사용 플러그인

- **Templater** (필수) — `<% tp.date.now(...) %>` 같은 변수
- **Periodic Notes** (선택) — daily/weekly 노트에 템플릿 자동 연결
- **Calendar** (선택) — daily 노트로 점프

## 1. 매매 일지 (daily-trade.md)

`Templates/daily-trade.md`:

```markdown
---
type: trade-journal
date: <% tp.date.now("YYYY-MM-DD") %>
pnl: 0
volume: 0
exchanges: []
tags: [trading, daily]
---

# 매매 일지 — <% tp.date.now("YYYY-MM-DD") %>

## 시장 환경
- BTC: 
- ETH: 
- 펀딩비:
- 주요 이벤트:

## 거래
| 시간 | 거래소 | 페어 | 사이드 | 사이즈 | 진입 | 청산 | PnL |
|------|--------|------|--------|--------|------|------|-----|

## 회고
- 잘한 점:
- 개선할 점:

## 다음 액션
- [ ]
```

`pnl`, `volume` 을 숫자로 채워야 Dataview 가 합산할 수 있습니다 (따옴표 X).

## 2. 주간 리뷰 (weekly-review.md)

```markdown
---
type: weekly-review
week: <% tp.date.now("YYYY-[W]ww") %>
start: <% tp.date.weekday("YYYY-MM-DD", 1) %>
end: <% tp.date.weekday("YYYY-MM-DD", 7) %>
pnl_week: 0
tags: [trading, weekly]
---

# 주간 리뷰 — <% tp.date.now("YYYY-[W]ww") %>

## 숫자
- 주간 PnL:
- 거래 횟수:
- 승률:
- 최대 손실:

## 이번 주 배운 것
1.

## 다음 주 계획
- [ ]

## 관련
- 일지:
- 리서치:
```

## 3. 프로젝트 리서치 (project-research.md)

```markdown
---
type: project
project: <% tp.file.title %>
status: research
tier: 
last_review: <% tp.date.now("YYYY-MM-DD") %>
website: 
twitter: 
docs: 
tags: [project]
related: []
---

# <% tp.file.title %>

## 한 줄 요약

## 메커니즘
- 

## 토크노믹스
- TGE:
- 분배:
- 락업:

## 팀 / 백커
- 

## 리스크
- 

## 액션
- [ ]

## 출처
- 
```

`<% tp.file.title %>` 는 파일명을 그대로 본문 헤더에 넣어줍니다.

## 4. 에어드랍 추적 (airdrop-tracking.md)

```markdown
---
type: airdrop
project: <% tp.file.title %>
status: farming
tier: 
season: 
deadline: 
estimated_value: 
points: 0
wallets: []
tags: [airdrop]
---

# <% tp.file.title %> 에어드랍

## 메커니즘
- 어떻게 점수 쌓는지:
- 마감일:
- 청구일:

## 진행 상황
| 날짜 | 활동 | 점수 |
|------|------|------|

## 체크리스트
- [ ] 지갑 연결
- [ ] 첫 거래
- [ ] 데일리 체크인
- [ ] referral 등록

## 메모
```

## 5. 거래소 노트 (exchange.md)

```markdown
---
type: exchange
exchange: <% tp.file.title %>
chain: 
referral_code: 
referral_link: 
api_key_location: 
tags: [exchange]
---

# <% tp.file.title %>

## 기본
- 체인:
- 주력 상품:
- 수수료:

## API
- 키 위치: [[API-Keys]]
- rate limit:
- 문서:

## referral
- 코드:
- 링크:

## 매매 메모
```

## 6. AI 세션 로그 (ai-session.md)

```markdown
---
type: ai-session
date: <% tp.date.now("YYYY-MM-DD HH:mm") %>
agent: claude-code
project: 
tags: [ai, session]
---

# AI 세션 — <% tp.date.now("YYYY-MM-DD HH:mm") %>

## 목표

## 입력 / 컨텍스트
- 작업 디렉토리:
- 관련 노트:

## 결과
- 생성된 파일:
- 수정된 파일:

## 다음 단계
- [ ]
```

세션이 길어지면 `Inbox/` 에 두고 정리되면 `Archive/` 로 이동.

## 7. 봇 운영 노트 (bot-ops.md)

```markdown
---
type: bot-ops
bot: 
host: 
status: running
last_check: <% tp.date.now("YYYY-MM-DD") %>
tags: [ops, bot]
---

# <% tp.file.title %>

## 호스트
- 머신:
- 디렉토리:
- 실행 방법: tmux / systemd / docker

## 모니터링
- 로그 위치:
- 텔레그램 채널:
- 알람 조건:

## 최근 이벤트
| 날짜 | 이벤트 | 대응 |
|------|--------|------|

## 트러블슈팅
- 
```

## 8. 인터뷰 / 컨퍼런스 노트 (interview.md)

```markdown
---
type: interview
date: <% tp.date.now("YYYY-MM-DD") %>
event: 
speaker: 
tags: [interview, conference]
---

# <% tp.file.title %>

## 컨텍스트
- 행사:
- 일자:
- 관련 프로젝트:

## 핵심 발언
- 

## 인사이트
- 

## 후속
- [ ]
```

## 9. 트윗 스레드 초안 (twitter-thread.md)

```markdown
---
type: twitter-thread
date: <% tp.date.now("YYYY-MM-DD") %>
topic: 
status: draft
tags: [content, twitter]
---

# 스레드 — <% tp.file.title %>

## 1/N (훅)

## 2/N

## 3/N

## CTA

## 출처 노트
- [[ ]]
```

## 10. 블로그 가이드 초안 (blog-guide.md)

```markdown
---
type: blog
date: <% tp.date.now("YYYY-MM-DD") %>
title: <% tp.file.title %>
target: naver-blog
status: draft
tags: [content, blog]
---

# <% tp.file.title %>

## 후킹 (3 줄 이내)

## 본문 헤더 1

## 본문 헤더 2

## 결론 + CTA

## 참고 노트
- [[ ]]
```

## 11. 회의 노트 (meeting.md)

```markdown
---
type: meeting
date: <% tp.date.now("YYYY-MM-DD HH:mm") %>
participants: []
agenda: 
tags: [meeting]
---

# <% tp.file.title %>

## 안건
- 

## 결정 사항
- 

## 액션 아이템
- [ ] @담당자 — 액션 — 마감
```

## 12. 일일 노트 (daily.md)

Periodic Notes 와 연동.

```markdown
---
date: <% tp.date.now("YYYY-MM-DD") %>
weekday: <% tp.date.now("dddd") %>
tags: [daily]
---

# <% tp.date.now("YYYY-MM-DD dddd") %>

## 오늘 할 것
- [ ]

## 메모

## 매매 일지
- [[Trading/daily/<% tp.date.now("YYYY-MM-DD") %>|매매 일지]]

## 어제 / 내일
- 어제: [[<% tp.date.now("YYYY-MM-DD", -1) %>]]
- 내일: [[<% tp.date.now("YYYY-MM-DD", 1) %>]]
```

## Templater 변수 정리

자주 쓰는 것:

- `<% tp.date.now("YYYY-MM-DD") %>` — 현재 날짜
- `<% tp.date.now("YYYY-MM-DD", -1) %>` — 어제
- `<% tp.date.weekday("YYYY-MM-DD", 1) %>` — 이번 주 월요일
- `<% tp.file.title %>` — 파일명
- `<% tp.file.creation_date() %>` — 파일 생성 시각

frontmatter 안에서도 동작합니다. 단 `:` 콜론 뒤 공백 주의.

## hotkey 매핑 제안

- `Ctrl+Alt+T` — Insert Templater (템플릿 선택)
- 매매 일지는 Periodic Notes 가 자동 생성하게 두고, 나머지는 hotkey 로 부르는 패턴이 무난합니다.
