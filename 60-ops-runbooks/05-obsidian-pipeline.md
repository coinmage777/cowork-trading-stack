# 05 — Obsidian Pipeline

봇이나 코드 자체는 git에 있지만, **그 봇을 만들면서 알게 된 것들**, **시장 리서치**, **블로그/유튜브용 정리**는 별도 흐름이 필요합니다. 이 runbook은 Obsidian Vault를 single source of truth로 두고 AI 결과를 멀티 플랫폼으로 내보내는 파이프라인입니다.

## 구성요소

### Obsidian Vault

위치: `<Drive>/Obsidian Vault/`

규모 (현재):

- 145+ markdown 노트
- 670+ wikilinks (`[[..]]`)
- 카테고리: Projects/, Trading/, Memory/, Inbox/, Archive/

**왜 Obsidian인가**:

- plain markdown (lock-in 없음, 망해도 파일은 남음)
- wikilink로 그래프 형성 (관련 노트 발견)
- local-first (인터넷 끊겨도 동작)
- AI가 직접 read/write 가능 (그냥 파일이라서)

### MemKraft layer

Obsidian 위에 얹은 entity tracker. 34+ entity (Hyperliquid, GRVT, Pair-Trading, perp-dex-bot, ...) 을 cross-session으로 추적. AI 세션이 새로 시작해도 이전 맥락을 잃지 않게.

저장 형태: 각 entity가 한 노트, 노트 frontmatter에 메타데이터 (last_updated, related_entities, status), 본문에 시간순 로그.

## 4단계 파이프라인

```
[1] Claude Code session
    └─> markdown 노트 출력
[2] Inbox/ 에 날짜prefix로 저장
[3] daily: Inbox 정리 → 카테고리 폴더로 이동
[4] weekly: 누적된 노트 → 블로그/YT/Telegram/Twitter 변환
```

### Step 1: Research in Claude Code session

AI에게 시키는 작업:

- 시장 동향 정리 (예: "Hyperliquid 최근 1주 funding rate 패턴")
- 코드 디버깅 결과 (예: "reconcile_positions에서 partial fill 처리하는 패턴")
- 새 protocol 분석 (예: "특정 protocol 메커니즘")

출력 형식: markdown. wikilink는 가능한 한 포함 (`[[Hyperliquid]]`, `[[Pair-Trading]]` 등) — Obsidian에서 그래프 자동 형성.

### Step 2: Save to Inbox/

날짜 prefix 컨벤션:

```
Inbox/
  YYYY-MM-DD - Hyperliquid funding pattern.md
  YYYY-MM-DD - reconcile_positions partial fill.md
  YYYY-MM-DD - protocol mechanism.md
```

`YYYY-MM-DD - <topic>.md`. 날짜로 sort하면 자동으로 시간순.

이 단계에서는 분류하지 않습니다. 빠르게 dump.

### Step 3: Daily inbox processing

매일 또는 며칠에 한 번:

```
Inbox/<note>.md → 적절한 카테고리로 mv
```

카테고리 결정:

- `Projects/` — 진행중인 프로젝트 관련 (perp-dex-bot)
- `Trading/` — 시장/전략 리서치
- `Memory/` — 시스템 / 인프라 / API 키 등 referencable 한 정보 (예: API-Keys.md)
- `Archive/` — 더 이상 active 아닌 것

이때 wikilink 채워넣기, frontmatter 정비, 다른 노트와 cross-link.

이 작업도 AI에게 시킬 수 있습니다 — "Inbox/ 에 있는 노트들 카테고리 분류해줘".

### Step 4: Weekly fanout

누적된 노트들을 multi-platform으로 내보냅니다.

## "MD source of truth → multi-platform fanout" 원칙

같은 markdown 노트 1개를 input으로, 4종 output:

```
                ┌─> Naver blog (HTML, dark theme variant)
single .md ─────┼─> YouTube script (대본 형식)
                ├─> Telegram summary (3줄 요약)
                └─> Twitter thread (10 트윗 분할)
```

각 변환은 다른 prompt + 같은 source. AI에게:

- "이 노트를 Naver SmartEditor 3 호환 HTML로. dark theme 색상."
- "이 노트를 6분짜리 YouTube 대본으로. intro/main/outro 구분."
- "이 노트를 텔레그램용 3줄 요약 + 핵심 1문장."
- "이 노트를 트위터 thread로 (각 트윗 280자 이하)."

### 왜 이 패턴이 AI-친화적인가

1. **단일 source of truth** — 사실관계 수정은 한 곳만 (markdown). 4개 플랫폼 다 따로 고치면 drift 발생.
2. **error 격리** — Naver HTML이 깨졌다고 YouTube 대본도 다시 생성할 필요 없음. output별로 retry.
3. **prompt iteration이 쉬움** — "트위터 prompt 별로네" → 트위터 변환 prompt만 고치면 됨. 다른 출력은 영향 없음.
4. **버전 관리** — markdown은 git에 들어감. HTML/script는 derived artifact, .gitignore.

## Naver SmartEditor 3 quirks (Korean blogging 특이사항)

네이버 블로그에 markdown으로 바로 붙일 수가 없어서 HTML로 변환. 그런데 SmartEditor 3가 HTML을 sanitize하면서 많이 깎아냅니다:

### 잘리는 것들

- `<style>` 태그 — 외부 / 내부 스타일시트 다 strip
- `<pre>` 태그 — 코드 블럭 표현이 사라짐
- `class="..."` 속성 — 모든 class 제거 (CSS 매칭 불가)
- `<script>` (당연)
- 일부 attribute (`data-*`, `id=`)

### 살아남는 것들

- inline `style="..."` — 모든 styling은 inline으로
- `<table>` — 코드 블록 워크어라운드로 사용 (`<pre>` 대신 1행 1열 `<table>`에 monospace inline style)
- `<div>`, `<p>`, `<span>` 기본 구조
- `<img>` (단 src는 네이버가 다시 호스팅)
- `<a href="...">`

### 코드블록 워크어라운드 예시

```html
<table style="background:#1e1e1e; border-radius:4px; padding:8px; width:100%;">
  <tr>
    <td style="font-family: 'Courier New', monospace; color:#d4d4d4; white-space:pre;">
def reconcile_positions():
    positions = exchange.get_positions()
    state.update(positions)
    </td>
  </tr>
</table>
```

`white-space:pre`가 줄바꿈 보존, `monospace` 폰트로 코드 느낌, dark 배경.

### 테스트된 컴포넌트

dark theme HTML 컴포넌트 (제목 박스, 인용구, 표, 코드 블록, 강조 박스 등) 는 별도로 저장해 두고 재사용. CLAUDE.md에 "naver html components" 섹션을 두어 AI가 reference 하도록.

## 운영 팁

### Inbox 노트 prefix 규칙 강제

매번 손으로 `YYYY-MM-DD - ...` 치면 실수. AI 시스템 프롬프트에 "Obsidian Inbox/ 에 저장할 때는 항상 YYYY-MM-DD prefix" 박아두기.

### Wikilink는 손으로라도 채워라

`[[Hyperliquid]]` 같은 wikilink가 그래프를 만듭니다. AI에게 "관련 entity 자동 wikilink" 시키면 됩니다. 그래프가 풍부해질수록 나중에 노트 발견이 쉬움.

### Vault git backup

Obsidian Vault 자체를 git repo로 (private). 하루 1번 commit. clouds sync (Dropbox/iCloud) 와 별개로 history 확보. PK 같은 게 들어가지 않게 `.gitignore`에 `Memory/API-Keys.md` 추가.

### MemKraft entity 노트는 frontmatter 일관성 유지

```yaml
---
type: entity
name: Hyperliquid
status: active
last_updated: YYYY-MM-DD
related: [Pair-Trading, perp-dex-bot, GRVT]
---
```

이 frontmatter를 일관되게 두면 dataview plugin으로 쿼리 가능 (예: "최근 7일 업데이트된 active entity 다").

## 이 파이프라인의 한계

- **자동화 정도**: Step 3 (Inbox 정리) 와 Step 4 (fanout) 는 여전히 사람 trigger 필요. 완전 자동은 아님.
- **외부 플랫폼 변경에 약함**: Naver SmartEditor가 사양 바꾸면 HTML 컴포넌트 다시 테스트.
- **번역**: 한국어 노트 → 영어 트위터 등 cross-lingual은 따로 prompt. quality 편차 있음.

그래도 "1 source, N outputs" 원칙이 깨지지 않으면 콘텐츠 작업의 mental load가 크게 줄어듭니다. AI는 변환에 강하니까.

## 끝

이 5개 runbook (`01` ~ `05`) 이 cowork-trading-stack의 운영 know-how입니다. Telegram 봇으로 봇을 원격 제어하는 패턴은 별도 모듈 `telegram-control/` 참고.
