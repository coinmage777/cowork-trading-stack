# 04 — Git Private Repo Flow with AI

AI (Claude Code) 가 코드를 짜고 사람이 review/merge하는 워크플로우는 일반적인 솔로 개발자 흐름과 약간 다릅니다. 이 runbook은 그 차이와 best practice를 정리합니다.

## 핵심 규칙: Private repo는 자율 merge, Public repo는 확인 후

봇 코드처럼 시크릿 (API key, RPC endpoint, 전략 파라미터) 이 함께 있는 private repo의 경우:

```
sanitize → commit → push → merge (질문 없이)
```

AI가 자율적으로 끝까지 갑니다. 어차피 보는 사람은 owner 한 명이고, branch protection도 없습니다. 매 단계 confirm 받으면 워크플로우가 깨집니다.

Public repo (또는 외부 contributor 가 있는 repo) 의 경우:

```
sanitize → commit → push → "merge할까요?" 확인 → merge
```

다른 사람의 변경사항이 있을 수 있고, 공개되어 있어 실수가 비싸기 때문.

## Pre-commit sanitize 패턴

push 전에 plaintext 시크릿이 staged 되어 있는지 grep으로 검사:

```bash
git diff --staged | grep -iE 'PRIVATE_KEY|api_key|secret_key|0x[a-fA-F0-9]{64}'
```

매칭되면 commit 보류하고 사람한테 알림.

이걸 git pre-commit hook으로 박을 수도 있지만, AI 워크플로우에서는 매번 실행되는 보장이 없으니 (AI가 hook을 우회할 수도 있음) **AI 자체의 절차로 강제**하는 게 안전합니다.

### 추가 grep 패턴 (확장)

```bash
# 알려진 API key 접두사
grep -rE '(sk-or-|sk-ant-|nsn_|sk-proj-)' .

# 64자 hex (private key 전형)
grep -rE '0x[a-fA-F0-9]{64}' .

# .env 가 추적되고 있는지
git ls-files | grep -E '\.env$'

# 큰 바이너리 (모델 파일, DB dump 등)
git log --stat --all | grep -E '\| +[0-9]{4,}'
```

## "Branch per feature"는 솔로에선 anti-pattern

오픈소스나 팀 협업에서는 `feature/xxx` 브랜치 → PR → review → merge 흐름이 기본입니다. 솔로 + AI 워크플로우에서는:

- review할 사람이 자기 자신
- PR 화면 vs `git diff`가 같은 정보
- 브랜치 늘어나면 mental overhead만 증가

그냥 `main`에 직접 commit합니다. 단:

- commit 단위는 의미 있게 자르기 (한 commit = 한 논리적 변경)
- commit message는 정직하게 (`fix: reconcile_positions handles partial fill correctly` 같은)
- 큰 실험은 stash 또는 임시 브랜치, 끝나면 main으로 squash

## Squash on merge for AI work

AI 한 번의 작업 = 보통 5~30 commit. 그 중 절반은 "fix typo", "actually fix the typo", "lint" 같은 노이즈.

main에 그대로 들어가면 history가 더러워집니다. 옵션:

### Option A: Local squash before push

```bash
git rebase -i HEAD~10  # 10개 commit을 squash 모드로
```

(주의: `-i` flag는 interactive editor를 띄우니 AI 자율 실행에는 안 맞음. 사람이 마지막에 정리할 때만)

### Option B: Squash-on-merge in GitHub

PR 워크플로우라면 PR 머지 시 "Squash and merge" 선택. 모든 commit이 한 commit으로.

### Option C: AI에게 미리 commit 그룹화 시키기

작업 마지막에 AI한테 "logical group으로 commit 정리해줘"라고 요청. AI가 reset + 다시 add by chunk.

실용적으로는 B + C 조합이 무난합니다.

## gh CLI 활용

`gh` (GitHub CLI) 는 brower 안 열고 PR/repo 작업 가능:

```bash
# private repo clone
gh repo clone coinmage777/cowork-trading-stack

# 새 PR 생성 (현재 브랜치 → main)
gh pr create --title "Add reconcile_positions" --body "Recovers state on restart"

# PR 보기
gh pr view 42

# PR merge (squash)
gh pr merge 42 --squash --delete-branch

# 현재 repo의 status
gh repo view --web
```

AI 워크플로우에서 좋은 점: 모든 명령어가 비대화형. 결과가 stdout으로 나와서 파싱 가능.

## 푸시 전 sanitize 체크리스트

매번 실행하는 4단계:

```bash
# 1. Plaintext private key (64자 hex with 0x prefix)
grep -rE '0x[a-fA-F0-9]{64}' --include='*.py' --include='*.yaml' --include='*.json' .

# 2. .env 가 추적되고 있는지
git ls-files | grep -E '(^|/)\.env$'

# 3. 알려진 API key 접두사
grep -rE '(sk-or-|sk-ant-|nsn_|sk-proj-)' --include='*.py' --include='*.yaml' --include='*.md' .

# 4. 큰 바이너리 추가됐는지 (1MB+)
git log --stat --since='1 day ago' | awk '/[0-9]+ Bytes|[0-9]+ KiB|[0-9]+ MiB/ && $1+0 > 1000'
```

다 비어 있으면 push:

```bash
git push origin main
```

매칭되면 stop, 사람 호출.

## 실제 사고 사례: config.yaml에 plaintext PK 2개

`perp-dex-bot/config.yaml` 의 line 384, 486에 plaintext private key가 들어가 있던 적이 있습니다 (private repo였지만 그래도 hygiene 문제). 발견 후 조치:

1. `.env`로 PK를 빼냄
2. config.yaml에는 `${WALLET_PK_1}` 같은 placeholder만
3. 봇 코드에 `os.path.expandvars()` 또는 `dotenv` 로딩
4. git history에서도 제거 (`git filter-repo` 또는 BFG repo cleaner) — 옛 commit에 남아 있으면 의미 없음
5. (만약 그 PK로 거래소 자금이 있었다면) 키 폐기 + 자금 새 지갑으로 이동

이 사고 이후로 push 전 grep을 SOP로 박았습니다.

## Public repo로 전환할 때

private repo를 public으로 풀려면 추가 sweep:

1. **모든 history sweep** — `git log -p | grep -iE '<pattern>'` 식으로 옛 commit 포함 검사
2. **dependency leak** — `requirements.txt`에 사내/private package 가 있는지
3. **README review** — 내부 사람만 알아야 할 인프라 정보 (Contabo IP, DB host 등) 가 적혀 있는지
4. **license 추가** — public repo는 license 없으면 default가 "all rights reserved" → 다른 사람이 못 씀
5. **CONTRIBUTING.md / CODE_OF_CONDUCT.md** — 기여 받을 거면 필요

이 sweep을 PR-style review로 (AI에게) 시키면 빠릅니다: "이 repo를 public으로 풀 건데 보안/품질 측면에서 issue 있는 곳 list up 해줘".

## 다음 단계

- 코드 push 외에, AI 작업 결과를 markdown 노트로 축적/배포하는 흐름 → `05-obsidian-pipeline.md`
