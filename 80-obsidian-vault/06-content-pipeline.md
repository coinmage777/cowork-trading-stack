# 06 — 콘텐츠 파이프라인

옵시디언에 쌓아둔 노트는 그 자체로 가치가 있지만, 같은 내용을 여러 플랫폼에 풀면 도달이 곱빼기로 늘어납니다. 한 마크다운 소스에서 N 개 출력 (블로그 / YouTube / 텔레그램 / 트위터) 을 만드는 패턴을 정리합니다.

전체 흐름:

```
[옵시디언 마크다운 (단일 소스)]
        │
        ├─→ 네이버 블로그 HTML
        ├─→ 다크 테마 HTML 프리뷰
        ├─→ YouTube 스크립트 (구어체)
        ├─→ 텔레그램 요약 (300 자)
        └─→ Twitter 스레드 (8 ~ 10 트윗)
```

## 단일 소스 형식

`Content/blog/2026-04-26 - hyperliquid 입문.md`:

```markdown
---
title: 하이퍼리퀴드 입문
date: 2026-04-26
target: [naver-blog, youtube, telegram, twitter]
status: draft
---

# 하이퍼리퀴드 입문

## 한 줄 요약
HL 자체 L1 위에서 동작하는 perp DEX 다.

## 왜 알아둘 만한가
- 오더북 모델
- 자체 L1 + EVM
- ...

## 기본 사용법
1. 지갑 연결
2. 거래소 입금
3. 페어 선택

## 리스크
- ...

## 결론
```

이 한 파일이 모든 출력의 원본입니다. 출력별 파일을 따로 만들지 않습니다 — AI 에게 변환을 시킵니다.

## 1. MD → 네이버 블로그 HTML

네이버 SmartEditor 3 의 한계 때문에 일반 markdown 변환기가 잘 안 먹습니다. quirks 를 피한 HTML 만 붙여넣어야 합니다.

### SmartEditor 3 quirks (체크리스트)

- `<style>` 태그 strip 됨 → 인라인 style 만 사용
- `<pre>` 태그 안 됨 → `<table>` 기반 코드 블록으로 회피
- `class=` 속성 strip → 인라인 style 로
- `<script>` 당연히 strip
- `<details>` / `<summary>` 같은 최신 태그도 strip 위험
- 외부 CSS 링크 (`<link>`) 모두 잘림

### 검증된 인라인 스타일 components

#### 헤더 1

```html
<h2 style="color:#1a73e8;font-size:24px;font-weight:700;border-bottom:2px solid #1a73e8;padding-bottom:8px;margin-top:24px;">
헤더 텍스트
</h2>
```

#### 인용 박스

```html
<blockquote style="border-left:4px solid #1a73e8;background:#f5f9ff;padding:12px 16px;margin:16px 0;color:#333;">
인용 내용
</blockquote>
```

#### 코드 블록 (table 기반)

```html
<table style="background:#1e1e1e;color:#d4d4d4;font-family:'D2Coding',monospace;width:100%;border-collapse:collapse;margin:16px 0;">
<tr><td style="padding:12px 16px;white-space:pre-wrap;font-size:13px;">
const x = 42;
console.log(x);
</td></tr>
</table>
```

`<pre>` 가 안 되니 table 한 칸을 코드 영역으로 씁니다.

#### 강조 박스

```html
<div style="background:#fff8e1;border:1px solid #ffd54f;padding:12px 16px;border-radius:6px;margin:16px 0;">
<strong>주의:</strong> 본문 내용
</div>
```

### AI 변환 프롬프트

```
"이 마크다운을 네이버 SmartEditor 3 에 붙여넣을 인라인 HTML 로 변환해줘.
규칙:
- <style>, class=, <pre>, <script> 사용 금지
- 모든 스타일은 인라인 style=""
- 코드 블록은 <table> 기반
- 검증된 component (헤더 / 인용 / 코드 / 강조) 만 사용
- 본문 색상 #333, 헤더 #1a73e8

원본:
[여기에 markdown 본문 붙여넣기]
"
```

검증 방법: 네이버 블로그에 붙여넣고 미리보기 → 깨진 게 있으면 component 별로 줄여가며 디버깅.

## 2. MD → 다크 테마 HTML 프리뷰

블로그 올리기 전에 자기 화면에서 보는 용도. 외부 CSS 자유롭게 써도 됩니다.

```html
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>preview</title>
<style>
body { background:#0d1117; color:#c9d1d9; font-family:-apple-system,sans-serif; max-width:720px; margin:40px auto; padding:0 20px; line-height:1.7; }
h1, h2, h3 { color:#58a6ff; }
code { background:#161b22; padding:2px 6px; border-radius:3px; }
pre { background:#161b22; padding:16px; border-radius:6px; overflow-x:auto; }
blockquote { border-left:3px solid #58a6ff; padding-left:16px; color:#8b949e; }
a { color:#58a6ff; }
</style>
</head>
<body>
<!-- markdown → html (pandoc 결과 또는 AI 변환) -->
</body>
</html>
```

pandoc 한 줄:

```bash
pandoc input.md -o preview.html --standalone
```

AI 에게 직접 시켜도 됩니다 — 다크 테마 컬러 팔레트 줘서.

## 3. MD → YouTube 스크립트 (구어체)

블로그 톤이 글말이라 영상 그대로 읽으면 어색합니다. 구어체 변환 필요.

### AI 변환 프롬프트

```
"이 마크다운을 YouTube 영상 스크립트 (구어체) 로 변환해줘.
규칙:
- '~입니다' '~했습니다' 줄이고 '~이에요' '~했어요' 또는 '~해요'
- 문장 짧게 (한 문장 20 단어 이내)
- 헤더는 (코너 1) 같은 영상 표지로
- 시작에 '안녕하세요' 식 인삿말 추가
- 끝에 구독 / 좋아요 멘트
- 한 단락 끝나면 (Pause) 표시

원본:
[markdown]
"
```

### 출력 예

```
안녕하세요. 오늘은 하이퍼리퀴드를 다뤄볼게요.

(Pause)

(코너 1) 한 줄 요약

하이퍼리퀴드는 자체 블록체인 위에서 돌아가는 perp DEX 예요.
이게 무슨 말이냐면, 다른 거래소처럼 중앙 서버 가 있는 게 아니라...
```

다듬기는 직접 — AI 가 만든 구어체가 자기 말투랑 미묘하게 다를 수 있습니다.

## 4. MD → 텔레그램 요약 (300 자)

텔레그램 채널 / 그룹은 짧은 글이 잘 먹힙니다.

### AI 변환 프롬프트

```
"이 마크다운을 텔레그램 게시글로 요약해줘.
규칙:
- 300 자 이내 (한국어 기준)
- 첫 줄: 굵게 (마크다운 ** 또는 텔레그램은 일반 텍스트로 헤더)
- 본문: 핵심 3 가지 bullet
- 마지막: 출처 / 자세히 보기 링크 placeholder
- 이모지 금지

원본:
[markdown]
"
```

### 출력 예

```
하이퍼리퀴드 입문 정리

- 자체 L1 위에서 동작하는 perp DEX
- 오더북 모델 (AMM 아님)
- 시작은 지갑 연결 → 입금 → 페어 선택

자세히: [블로그 링크]
```

## 5. MD → Twitter 스레드

8 ~ 10 트윗이 무난합니다. 첫 트윗 (훅) 이 가장 중요.

### AI 변환 프롬프트

```
"이 마크다운을 Twitter 스레드 8 트윗으로 변환해줘.
규칙:
- 1/N 형식으로 번호 매기기
- 1/N 은 훅 (질문 또는 강한 한 줄)
- 각 트윗 280 자 이내
- 마지막은 CTA (관련 링크 / 다음 콘텐츠 예고)
- 한국어, 존댓말
- 이모지 금지

원본:
[markdown]
"
```

### 출력 예

```
1/9 하이퍼리퀴드, 다른 거래소랑 뭐가 다를까요?

자체 블록체인 위에서 돌아가는 perp DEX 라는 점이 핵심입니다.

2/9 ...
```

## 자동화 가능 vs 수동

| 단계 | 자동화 | 수동 |
|------|--------|------|
| MD → 다크 HTML 프리뷰 | pandoc 한 줄 | 거의 없음 |
| MD → 네이버 HTML | AI 변환 + 검증 component | 붙여넣기, 미리보기 확인 |
| MD → YT 스크립트 | AI 1 차 변환 | 말투 다듬기, 발음 어려운 단어 교체 |
| MD → 텔레그램 | AI 100% | 검토만 |
| MD → Twitter | AI 1 차 변환 | 첫 트윗 훅 자기 손으로 |

원칙: AI 1 차, 사람 2 차. AI 가 100% 정확한 영역은 단순 변환 (텔레그램 요약 / 다크 HTML) 뿐입니다.

## 한 번에 여러 출력 만들기

배치 프롬프트:

```
"다음 마크다운을 네 가지 형식으로 변환해줘:
1. 네이버 블로그 HTML (인라인 스타일, <pre> 금지)
2. YouTube 스크립트 (구어체)
3. 텔레그램 300 자
4. Twitter 스레드 8 트윗

원본:
[markdown 한 번 붙여넣기]
"
```

긴 컨텍스트 모델 (Sonnet, Opus) 한 번에 다 받습니다. 각 출력을 `Content/<plat>/<날짜>-<주제>.md` 로 저장.

## 워크플로우 정리

1. 원본 노트 한 개 작성 (`Content/blog/날짜-주제.md`)
2. AI 에게 4 ~ 5 개 출력 동시 변환 시킴
3. 각 출력 별로 5 ~ 10 분 다듬기
4. 플랫폼 별 발행 (네이버 → YT → 텔레그램 → Twitter 순)
5. 각 발행 링크를 원본 노트 frontmatter 에 추가

```markdown
---
title: 하이퍼리퀴드 입문
date: 2026-04-26
published:
  blog: https://blog.naver.com/...
  youtube: https://youtu.be/...
  telegram: https://t.me/.../123
  twitter: https://x.com/.../...
---
```

이러면 그 노트 하나가 발행 history 까지 들고 있게 됩니다. 6 개월 후 "지난번 그 영상 어디 갔지" 검색이 쉬워집니다.
