# 80 — Obsidian Vault

크립토 리서치 / 트레이딩 / AI 워크플로우에 옵시디언을 어떻게 쓰는지 정리한 폴더입니다. 저도 입문 바이브코더로 옵시디언을 쓰면서 시행착오를 거친 내용입니다.

## 이 폴더가 다루는 것

- 볼트 폴더 구조 설계 (크립토 리서치 특화)
- 실제로 쓰는 템플릿 모음
- Dataview 로 만드는 라이브 대시보드
- wikilink 패턴과 그래프 뷰 활용
- Claude Code 같은 AI 와의 양방향 연동
- 한 노트에서 N 개 플랫폼 콘텐츠 만드는 파이프라인

## 이 폴더가 다루지 않는 것

- 옵시디언 설치 / 처음 켜기 (공식 문서 참고)
- 모바일 동기화 / Obsidian Sync 결제 (개인 취향 영역)
- Vim mode / hotkey 커스텀 (생산성보다 개인 취향)
- 플러그인 개발 (소비자 입장만 다룸)

## 챕터

1. [Vault 폴더 구조](01-vault-structure.md) — 크립토 리서치용 폴더 분할과 명명 규칙
2. [템플릿 모음](02-templates-collection.md) — 매매일지 / 주간리뷰 / 프로젝트 리서치 등 12 종
3. [Dataview 대시보드](03-dataview-dashboards.md) — 에어드랍 트래커, PnL 메트릭, TGE 캘린더
4. [Wikilink 패턴](04-wikilink-patterns.md) — 좋은 링크 vs 나쁜 링크, alias, MOC
5. [AI 연동](05-ai-integration.md) — Claude Code 와 옵시디언 양방향 워크플로우
6. [콘텐츠 파이프라인](06-content-pipeline.md) — 한 소스에서 블로그 / YT / 텔레그램 / 트위터 만들기

## 왜 옵시디언인가 (요약)

- 로컬 마크다운 파일이라 vendor lock-in 이 없습니다
- AI 가 그냥 텍스트 파일로 읽고 씁니다 (Claude Code 의 Read / Write 와 자연스럽게 결합)
- wikilink + 그래프 뷰로 흩어진 메모를 다시 발견하기 쉽습니다
- Dataview / Templater 로 정적인 노트를 동적인 대시보드로 바꿀 수 있습니다

자세한 동기는 [`ko/04-옵시디언-텔레그램.md`](../ko/04-옵시디언-텔레그램.md) 도입부 참고.

## 다른 폴더와의 관계

- [`60-ops-runbooks/05-obsidian-pipeline.md`](../60-ops-runbooks/05-obsidian-pipeline.md) — 4 단계 파이프라인 (Inbox → 분류 → 누적 → 멀티 플랫폼) 의 운영 측면. 이 폴더와 연결되지만 시점이 다릅니다 (운영 중심 vs 활용 중심).
- [`ko/04-옵시디언-텔레그램.md`](../ko/04-옵시디언-텔레그램.md) — 한국어 입문 가이드. "왜 / 무엇" 위주. 이 폴더는 "어떻게" 위주.

겹치는 부분이 있을 수 있지만 의도적으로 시점이 다릅니다. 이 폴더에 들어오면 옵시디언을 이미 쓰고 있다고 가정합니다.
