# 03 — Dataview 대시보드

Dataview 는 노트의 frontmatter 와 인라인 필드를 데이터베이스처럼 쿼리합니다. 같은 노트라도 보는 각도를 바꿀 수 있어서, 한번 누적해두면 계속 재활용됩니다.

이 챕터는 6 개 라이브 보드를 다룹니다. DQL (간단) 과 DataviewJS (자유도 높음) 을 섞어 씁니다.

## 사전 준비

- Dataview 플러그인 설치
- 설정에서 "Enable JavaScript Queries" 활성화 (DataviewJS 쓰려면)
- "Enable Inline JavaScript Queries" 도 켜두면 본문 안에서 한 줄 표현 가능

## 1. 에어드랍 트래커

`Dashboard/Airdrop-tracker.md`. Tier / 마감일 / 상태별로 그룹.

전제: 각 에어드랍 노트 frontmatter 에 `type: airdrop`, `tier`, `status`, `deadline`, `estimated_value` 가 있어야 합니다 (templates 챕터의 4번 템플릿).

````markdown
# 에어드랍 트래커

## 진행 중 (Tier 별)

```dataview
TABLE tier, status, deadline, estimated_value
FROM "Airdrop"
WHERE type = "airdrop" AND status = "farming"
SORT tier ASC, deadline ASC
```

## 마감 임박 (30 일 이내)

```dataview
TABLE deadline, tier, estimated_value
FROM "Airdrop"
WHERE type = "airdrop" AND deadline AND date(deadline) - date(today) < dur(30 days)
SORT deadline ASC
```

## Tier 별 합계

```dataview
TABLE length(rows) as "개수", sum(rows.estimated_value) as "예상 합계"
FROM "Airdrop"
WHERE type = "airdrop"
GROUP BY tier
SORT tier ASC
```
````

화면에서 보는 결과: 각 표가 노트 안에서 실시간 렌더링됩니다. 노트를 수정해 deadline 을 바꾸면 이 보드도 따라 갱신됩니다.

## 2. PnL 메트릭

`Dashboard/PnL-week.md`. 매매 일지에서 자동 집계.

전제: `Trading/daily/YYYY-MM-DD.md` 에 `pnl: 숫자` frontmatter.

````markdown
# PnL 대시보드

## 이번 주

```dataviewjs
const since = dv.date("today").minus(dv.duration("7 days"));
const pages = dv.pages('"Trading/daily"').where(p => p.date && p.date >= since);
const total = pages.array().reduce((s, p) => s + (p.pnl || 0), 0);
const days = pages.length;
const avg = days ? (total / days).toFixed(1) : 0;

dv.paragraph(`**${days} 일** 거래, **합계 ${total}**, 일평균 **${avg}**`);

dv.table(
  ["날짜", "PnL", "거래소"],
  pages.sort(p => p.date, 'desc').map(p => [p.file.link, p.pnl, p.exchanges])
);
```

## 월별 PnL

```dataview
TABLE sum(rows.pnl) as "PnL", length(rows) as "거래일"
FROM "Trading/daily"
WHERE pnl
GROUP BY dateformat(date, "yyyy-MM") as month
SORT month DESC
```
````

DataviewJS 를 쓰면 단순 SUM 외에 평균, 표준편차, 승률 같은 계산도 자유롭게 가능합니다.

## 3. 프로젝트 status board

`Dashboard/Projects.md`. 진행 중 / pause / archive 분리.

````markdown
# 프로젝트 보드

## Active

```dataview
TABLE status, tier, last_review
FROM "Projects"
WHERE type = "project" AND status = "active"
SORT tier ASC, last_review DESC
```

## 30 일 이상 미터치

```dataview
TABLE last_review, status
FROM "Projects"
WHERE type = "project" AND date(today) - date(last_review) > dur(30 days)
SORT last_review ASC
```

이 보드의 의미: "리뷰 안 한 지 오래된 프로젝트" 가 자연스럽게 떠오릅니다. archive 후보 발견용.
````

## 4. TGE 캘린더

`Dashboard/TGE-calendar.md`. 임박한 토큰 출시 모음.

전제: 프로젝트 노트 frontmatter 에 `tge_date: YYYY-MM-DD`.

````markdown
# TGE 캘린더

## 다음 90 일

```dataview
TABLE tge_date as "TGE", tier, status
FROM "Projects"
WHERE tge_date AND date(tge_date) >= date(today) AND date(tge_date) - date(today) < dur(90 days)
SORT tge_date ASC
```

## 이미 발생 (최근 30 일)

```dataview
TABLE tge_date as "TGE", tier
FROM "Projects"
WHERE tge_date AND date(today) - date(tge_date) < dur(30 days) AND date(tge_date) <= date(today)
SORT tge_date DESC
```
````

## 5. 이번 주 액션 자동 추출

전제: 노트 본문에 `- [ ]` 체크박스 사용.

````markdown
# 이번 주 액션

## 미완료 태스크 전체

```dataview
TASK
FROM "Projects" OR "Airdrop" OR "Trading"
WHERE !completed
GROUP BY file.link
```

## 마감 임박 (인라인 필드 `due::` 사용)

```dataview
TASK
WHERE !completed AND due AND date(due) - date(today) < dur(7 days)
SORT due ASC
```
````

본문에 인라인 필드를 쓰는 패턴:

```markdown
- [ ] Hyperliquid referral 등록 [due:: 2026-04-30] [tier:: high]
```

`[due:: 2026-04-30]` 처럼 인라인 필드를 박으면 Dataview 가 인식합니다.

## 6. Memory entity counter

MemKraft 같은 entity tracker 를 쓴다면, entity 노트 개수 / 마지막 갱신을 보드로.

전제: entity 노트가 `Memory/entities/` 아래 있고 `type: entity`, `last_updated` frontmatter.

````markdown
# Memory 엔티티

## 통계

```dataviewjs
const entities = dv.pages('"Memory/entities"').where(p => p.type === "entity");
const stale = entities.where(p => dv.date("today").minus(dv.date(p.last_updated)) > dv.duration("30 days"));
dv.paragraph(`총 **${entities.length}** 엔티티, 30 일 이상 미갱신: **${stale.length}**`);
```

## 최근 갱신

```dataview
TABLE last_updated, status
FROM "Memory/entities"
WHERE type = "entity"
SORT last_updated DESC
LIMIT 20
```

## 미갱신 (30 일 이상)

```dataview
TABLE last_updated
FROM "Memory/entities"
WHERE type = "entity" AND date(today) - date(last_updated) > dur(30 days)
SORT last_updated ASC
```
````

## 일반 팁

### Dataview 가 안 보일 때

- frontmatter 의 키 이름과 쿼리 키 이름을 확인하세요 (대소문자 일치)
- `date()` 함수로 감싸야 비교가 됩니다 (문자열 비교 아님)
- 빈 frontmatter 필드는 `null` 이 아니라 빈 문자열일 수 있어 `WHERE field` 로 거르세요

### 성능

- `FROM` 을 가능한 좁게 잡으세요 (`FROM "Projects"` 가 `FROM ""` 보다 훨씬 빠름)
- 큰 볼트에서 DataviewJS 가 무거우면 일부 보드를 detached file 로 분리해서 자주 안 여는 보드는 안 그리도록 합니다

### 대시보드 vs 일반 노트

대시보드는 "보는 화면" 이고 데이터는 일반 노트에 있습니다. 대시보드를 잃어도 데이터는 안전합니다. 그래서 대시보드는 마음껏 망가뜨리고 다시 짜도 됩니다.

## 시작 추천 순서

1. 매매 일지 frontmatter 에 `pnl` 만 일관되게 넣기
2. PnL 보드 한 개 (위 2번)
3. 프로젝트 노트에 `status`, `tier`, `last_review` 넣고 보드 만들기
4. 그 다음에 에어드랍 / TGE 등 확장

처음부터 모든 보드를 만들 필요 없습니다. 데이터가 쌓여야 의미가 생깁니다.
