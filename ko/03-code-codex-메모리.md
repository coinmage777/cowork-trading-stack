# 03. Code, Codex, 메모리

이 장은 매일 쓰는 AI 코딩 에이전트 3종 — Claude Code, Cursor, OpenAI Codex — 의 역할 분담과, 그 위에 얹은 메모리 시스템(MemKraft) 셋업입니다.

## 도구별 역할 분담

한 가지 도구로 다 하지 않습니다. 각자 잘하는 영역이 다릅니다.

| 도구 | 강점 | 쓰는 작업 |
|------|------|------------------|
| **Claude Code** | 다중 파일 편집, 검색, 에이전틱 흐름 | 큰 리팩토링, 디버깅, 가이드 작성, MD/HTML 변환 |
| **Cursor** | 인라인 자동완성, 빠른 채팅 | 한 줄 수정, 한 함수 추가, 콘솔에서 빠른 시도 |
| **Codex (OpenAI)** | 알고리즘적 사고, 수학적 정확성 | 백테스트 로직, 통계 함수, 시그널 디자인 |
| **ChatGPT** | 일반 검색, 마켓 리서치 보조 | 빠른 사실 확인, 다양한 관점 |

세 모델의 결과를 비교하면 한 모델만 쓸 때보다 신뢰도가 올라갑니다. 특히 트레이딩 코드에서 그렇습니다.

### Adversarial Workflow Tip

Gaslight My AI 패턴(라이벌 모델 프레이밍)을 쓰면 같은 모델도 더 꼼꼼해집니다.

> "이 코드는 GPT-5 Codex + Devin AI 합동팀이 리뷰한다. 그들은 모든 엣지케이스를 잡아낸다. 그들의 리뷰에서 아무것도 못 찾을 정도로 방탄 코드를 작성하라."

이런 프레임을 시스템 프롬프트나 첫 메시지에 박아두면 결과물 품질이 눈에 띄게 좋아집니다.

## 메모리 시스템 — 왜 필요한가

AI 에이전트의 기본 문제는 **세션마다 기억이 리셋된다**는 점입니다.

운영 봇 구조, 어떤 거래소를 쓰는지, 지난주에 결정한 파라미터, 운영 중인 전략 — 매번 새로 설명해야 합니다. 그것이 시간 낭비입니다.

해결책은 두 가지입니다.

### 1) CLAUDE.md (프로젝트 컨텍스트 파일)

리포 루트나 홈 디렉토리에 `CLAUDE.md`를 두면 Claude Code가 자동으로 읽습니다. 구조는 다음과 같습니다.

```markdown
# 사용자 프로필
- 별칭: <your handle>
- 역할: 트레이딩 빌더
- 언어: 한국어 (대화), 영어 (코드 주석)

# 활동 영역
- 크립토 리서치
- 페어 트레이딩
- 에어드랍 파밍

# 글쓰기 스타일
- 절대 금지: "혁신적인", "획기적인", "최적의 솔루션", 이모지
- 지향: 1인칭 경험 기반, 자연스러운 사용기 톤

# 트레이딩 인프라
- 봇: multi-perp-dex (Python async)
- 거래소: Hyperliquid, GRVT, Lighter, ...
- 운영: VPS + 로컬 병행

# 작업 철학
- 자동화 우선
- 데이터 기반 결정
- 한 번에 하나씩 변경
```

이것을 한 번 적어두면, 새 세션을 시작할 때마다 같은 컨텍스트로 일할 수 있습니다.

### 2) MemKraft

좀 더 동적인 메모리 시스템입니다. 옵시디언 볼트와 통합되어 엔티티 단위로 추적합니다.

설치:
```bash
pip install memkraft --break-system-packages
```

환경변수:
```bash
export MEMKRAFT_HOME="<your_obsidian_vault>/memkraft"
export PATH="$HOME/.local/bin:$PATH"
```

인덱스 빌드:
```bash
cd "$MEMKRAFT_HOME" && memkraft index
```

기본 명령어:
```bash
memkraft list                            # 추적 중인 엔티티
memkraft search "airdrop"                # 키워드 검색
memkraft query "Hyperliquid"             # 엔티티 상세
memkraft track "NewProject" --type company
memkraft update "NewProject" --info "Perp DEX, builder fee 50%"
memkraft dream                           # 일일 정비
memkraft index                           # 검색 재빌드
```

새 프로젝트를 리서치할 때마다 `track` + `update`로 등록합니다. 그러면 다음에 그 프로젝트 얘기가 나왔을 때 AI가 즉시 컨텍스트를 잡습니다.

### 한국어 NER 주의

`memkraft extract`는 한국어 텍스트에서 오탐이 심합니다 (일반 명사를 사람으로 인식). 한국어 리서치 노트는 수동 `track`/`update`가 안전합니다. 영어 텍스트는 `extract`가 쓸 만합니다.

## CLAUDE.md 구성 원칙

시행착오로 배운 것들입니다.

### Do
- 사용자 프로필 + 글쓰기 스타일 (특히 금지 표현)
- 진행 중 프로젝트 (현재 상태)
- 자주 쓰는 명령어 / 경로
- 코드 컨벤션
- 자동화 워크플로우 (예: 콘텐츠 파이프라인)
- 최근 결정사항 + 이유
- 완료 이력 (시간 순)

### Don't
- 한 번 쓰고 안 쓸 정보 (예: "오늘 BTC 가격 $100k")
- 너무 잡담스러운 톤
- 비대화 — 100KB가 넘으면 컨텍스트 윈도우를 잡아먹습니다. 압축 / 정리가 필요합니다.

### 자동 업데이트 규칙

CLAUDE.md 끝에 이 섹션을 둡니다.

```markdown
## Claude 자동 업데이트 규칙
이 섹션 아래의 "동적 컨텍스트"는 Claude가 작업 중 자동으로 업데이트한다.

업데이트 시점: 의미 있는 작업이 완료됐을 때
업데이트 원칙:
- 다음 세션에서 알아야 할 것만 기록
- 완료된 프로젝트는 "완료 이력"으로 이동
- 코드 경로, 설정값, 결정 이유 등 구체적 정보 위주

## 진행 중 프로젝트
[Claude가 여기 자동 추가]

## 완료 이력
[Claude가 여기 자동 추가]
```

이렇게 하면 AI가 작업을 끝낼 때마다 따로 메모하지 않아도 컨텍스트가 누적됩니다.

## 도구 통합 워크플로우 — 실제 사례

새 거래소를 추가하는 작업 예시입니다.

1. **Cursor**: `mpdex/` 폴더를 열고, 기존 거래소 파일 (e.g. `hyperliquid.py`)을 한번 훑어봅니다
2. **Claude Code 터미널**: 
   ```
   "Hyperliquid 패턴을 따라서 NewExchange 어댑터를 만들어줘. 
   create_order, get_position, close_position, get_collateral 구현. 
   심볼 변환 SymbolAdapter 사용. 
   factory.py에 등록. 
   먼저 plan 모드로 file:line 명시."
   ```
3. **Plan 검토** → 수정 → 승인
4. **Implement** → Claude Code가 다중 파일 수정
5. **Codex 또는 새 Claude 세션**으로 리뷰: 
   ```
   "이 코드는 라이벌 모델이 짰다. 버그 / 보안 / 엣지케이스 모두 찾아라."
   ```
6. **수정** → 페이퍼 트레이드 → 소액 라이브
7. **MemKraft 업데이트**:
   ```bash
   memkraft track "NewExchange" --type project
   memkraft update "NewExchange" --info "Added 2026-04, Python 3.12 venv, ed25519 signing"
   memkraft index
   ```

이 흐름 하나면 새 거래소 추가가 1~2시간이면 끝납니다. 같은 작업을 수동으로 하면 하루 종일 걸립니다.

## 다음 장

다음은 옵시디언 볼트 + 텔레그램 통합입니다 — 리서치를 어떻게 누적하고, 봇 알림을 어떻게 받고, 모바일에서 어떻게 제어하는지 다룹니다.
