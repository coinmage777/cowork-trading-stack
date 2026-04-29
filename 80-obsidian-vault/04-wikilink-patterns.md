# 04 — Wikilink 패턴

`[[..]]` 가 옵시디언의 핵심 기능입니다. 노트끼리 연결하면 그래프가 형성되고, backlink 로 reverse 방향 탐색이 됩니다. 그런데 막 쓰면 죽은 링크 / 모호한 링크가 쌓여서 오히려 방해가 됩니다. 패턴을 잡고 가세요.

## 기본 문법

- `[[Hyperliquid]]` — 노트 이름 그대로 링크
- `[[Hyperliquid|HL]]` — alias. 본문에는 "HL" 로 표시
- `[[Hyperliquid#메커니즘]]` — 헤더 링크
- `[[Hyperliquid#^abc123]]` — 블록 링크 (특정 단락)
- `![[Hyperliquid#한 줄 요약]]` — embed (그 부분 내용을 현재 노트에 끼워 넣음)

## 좋은 wikilink vs 나쁜 wikilink

### 나쁜 예: 노트 이름과 본문이 안 맞을 때

```markdown
어제 [[2026-04-26]] 에 매매를 했고, 그때 보던 [[프로젝트]] 가 흥미로웠다.
```

문제: `[[프로젝트]]` 가 너무 일반적이라 그래프가 한 군데로 뭉칩니다. 어떤 프로젝트인지 미래의 내가 모릅니다.

### 좋은 예

```markdown
어제 [[Trading/daily/2026-04-26|매매 일지]] 에 적었듯, [[Hyperliquid]] 펀딩 패턴이 평소와 달랐다.
```

- 폴더 경로 포함해서 ambiguity 제거
- alias 로 본문 가독성 유지
- 구체적인 노트 이름

## alias 활용

같은 대상을 여러 이름으로 부를 때 노트 frontmatter 에 alias 를 박습니다.

```markdown
---
aliases: [HL, Hyper, hl-perp]
---

# Hyperliquid
```

이러면 본문에서 `[[HL]]`, `[[Hyper]]`, `[[hl-perp]]` 어떻게 써도 같은 노트로 연결됩니다.

언제 alias 가 유용한가:

- 거래소 약어 (HL, GRVT, BNB)
- 한국어 / 영어 혼용 (`Hyperliquid` / `하이퍼리퀴드`)
- 프로젝트 코드명 / 공식명 (`Pair-Trading` / `pair-trading-bot`)

남용하면 그래프가 지저분해지므로 자주 쓰는 대상에만 적용하세요.

## 폴더 경로를 wikilink 에 넣어야 할 때

같은 이름의 노트가 여러 폴더에 있을 때:

```
Trading/daily/2026-04-26.md
Inbox/2026-04-26 - random note.md
```

`[[2026-04-26]]` 만 쓰면 Obsidian 이 두 개 중 하나로 연결합니다. 의도한 게 아닐 수 있어요. 명시적으로:

```markdown
[[Trading/daily/2026-04-26|매매 일지]]
```

## 그래프 뷰 활용

설정 → Graph view 에서 색상 / 필터를 조정하면 무의미한 점들을 빼고 의미 있는 클러스터가 보입니다.

추천 그룹 색상:

- `path:Projects/` → 파란색
- `path:Trading/` → 녹색
- `path:Airdrop/` → 노란색
- `tag:#perp-dex` → 빨간색

새 클러스터가 보이면 "내가 의식 못 하던 연결" 일 수 있습니다. 예: `Hyperliquid` 와 `pair-trading` 노트가 자주 같이 묶이면, 그 둘을 묶는 MOC 노트를 만들 시점.

## Backlink 로 reverse research

특정 노트 (예: `Hyperliquid.md`) 를 열면 사이드바 backlink 패널에 "이 노트를 언급한 다른 노트" 목록이 뜹니다.

활용 패턴:

- `Hyperliquid` 를 1 년 만에 다시 보면 backlink 에 매매 일지 / 리서치 / 콘텐츠 초안이 다 모여 있습니다 — 자연스러운 timeline
- "이 거래소에서 작년에 뭘 했나" 가 backlink 한 번에 정리됨
- 새 노트 쓸 때 backlink 에 안 보이는 노트는 "아직 연결 안 된 영역"

## 죽은 wikilink 청소

wikilink 를 만들었는데 그 이름의 노트가 없으면 "unresolved link" 가 됩니다. 노트로 점프하면 빈 노트가 새로 생깁니다.

종종 정리:

- `Settings → Files & Links → Detect all file extensions` 켜기
- 코어 플러그인 `Outgoing Links` / 커뮤니티 플러그인 `Find Orphaned Files` 로 고아 노트 찾기
- 그래프 뷰에서 "Unresolved" 토글로 깨진 링크 표시

청소 방법:

1. 의도한 노트인데 안 만든 거면 → 만들기
2. 오타였으면 → 본문에서 wikilink 수정
3. 더 이상 필요 없으면 → wikilink 삭제

옵시디언이 자동 rename refactor 를 지원하므로 노트 이름을 바꿔도 wikilink 는 따라옵니다 (단 Obsidian 내에서 rename 한 경우).

## MOC (Map of Content) 패턴

MOC 는 한 주제의 인덱스 노트입니다. 위키의 카테고리 페이지와 비슷합니다.

예: `MOC-perp-dex.md`

```markdown
---
type: moc
tags: [moc, perp-dex]
---

# Perp DEX MOC

## 거래소
- [[Hyperliquid]]
- [[Lighter]]
- [[GRVT]]
- [[Nado]]

## 전략
- [[pair-trading-기본]]
- [[funding-arb]]

## 봇 / 도구
- [[perp-dex-bot]]

## 리서치 노트
- [[2026-04-26 - hyperliquid funding 패턴]]
- [[2026-04-26 - lighter 점수 메커]]

## 외부 링크
- [공식 문서](https://hyperliquid.xyz/docs)
```

언제 MOC 가 필요한가:

- 한 주제 노트가 5 개 이상 모이고
- 그 주제에 새 노트를 자주 추가하고
- 매번 검색하기 귀찮을 때

너무 일찍 만들면 비어 있는 MOC 만 늘어납니다. 노트가 충분히 쌓인 다음에 만드세요.

## tag vs wikilink

| 용도 | 추천 |
|------|------|
| 가로지르는 분류 (status, tier, type) | tag |
| 특정 대상 / 노트 레퍼런스 | wikilink |
| 임시 grouping | tag |
| 영구 관계 | wikilink |

예시:

```markdown
이 노트는 [[Hyperliquid]] 분석이고, #perp-dex #리서치 카테고리에 들어갑니다.
```

`Hyperliquid` 는 구체적인 대상이라 wikilink. `#perp-dex` 는 카테고리라 tag.

## 정리 체크리스트

가끔 (월 1 회) 점검:

- [ ] 그래프 뷰에 의미 없는 외톨이 노트 — Inbox / Archive 로 보내거나 삭제
- [ ] Unresolved link 50 개 이상 — 한 번 정리
- [ ] alias 가 너무 많은 노트 — 진짜 필요한 alias 만 남기기
- [ ] MOC 가 만든 지 오래됐는데 새 링크 추가 안 됨 — 그 주제가 dead 일 수도

오래 쓸수록 정리에 시간 들지만, 그게 옵시디언이 기억해주는 비용입니다.
