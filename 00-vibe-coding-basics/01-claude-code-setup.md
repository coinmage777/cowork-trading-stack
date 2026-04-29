# 01 - Claude Code 셋업

Claude Code는 Anthropic의 CLI 기반 코딩 에이전트입니다. Cursor가 IDE에 AI를 끼워 넣는 방식이라면, Claude Code는 터미널을 메인으로 두고 IDE/에디터를 곁들이는 방식입니다. 저자는 둘 다 쓰지만, 장기간 도는 작업(백테스트, 리팩터링, 디버깅)은 거의 Claude Code로 합니다.

## 설치

### Mac / Linux

```bash
# Node 18+ 필요
npm install -g @anthropic-ai/claude-code

# 첫 실행 - 브라우저로 OAuth 로그인
claude
```

설치 후 `~/.claude/`가 생깁니다. 여기에 settings.json, projects/, plugins/, memory/ 등이 누적됩니다.

### Windows (PowerShell + Git Bash)

```powershell
# Node 설치 (winget 권장)
winget install OpenJS.NodeJS.LTS

# 글로벌 설치
npm install -g @anthropic-ai/claude-code

# 실행
claude
```

#### 한글 Windows 필수 환경변수

한글 Windows(코드페이지 949)에서 Python/Node 출력에 한글이 섞이면 `UnicodeEncodeError`로 죽는 경우가 많습니다. 시스템 환경변수에 다음을 추가하세요.

```
PYTHONIOENCODING=utf-8
PYTHONUTF8=1
```

PowerShell 프로필(`$PROFILE`)에는 다음을 박아 둡니다.

```powershell
chcp 65001 > $null
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
```

이걸 안 하면 Claude Code가 만든 Python 스크립트를 실행할 때 `cp949 codec can't encode character`로 멈춥니다. 저자가 한 달에 두세 번씩 당하던 함정입니다.

#### Git Bash에서 실행

Windows에서는 PowerShell보다 Git Bash가 ANSI escape, 색상, fg/bg 등 호환성이 좋습니다. Claude Code도 Git Bash에서 더 안정적으로 동작합니다.

```bash
# ~/.bashrc
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1
alias cc='claude'
```

## 인증과 모델 선택

```bash
claude /login        # OAuth 또는 API 키
claude /model        # opus / sonnet / haiku 전환
claude /config       # 설정 UI
```

장기 코딩 세션은 Sonnet, 진짜 어려운 디버깅/설계는 Opus.

## MCP 서버 (Model Context Protocol)

MCP는 Claude Code가 외부 도구(파일시스템, GitHub, DB, Notion, Telegram 등)를 호출할 수 있게 해주는 표준 인터페이스입니다. `~/.claude/settings.json`의 `mcpServers`에 등록합니다.

자주 쓰는 것:

- `filesystem` - 특정 디렉토리 read/write 권한
- `github` - PR/이슈 조회, 코멘트 작성
- `playwright` - 브라우저 자동화 (스크래핑, 시각 디버깅)
- `postgres` / `sqlite` - DB 직접 쿼리

설정 예:

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_xxx" }
    }
  }
}
```

설치 후 Claude Code 재시작하면 `/mcp` 슬래시 커맨드에서 활성/비활성 토글 가능합니다.

## 슬래시 커맨드

기본 제공:

- `/help` - 명령 목록
- `/clear` - 대화 컨텍스트 초기화 (코딩 한 사이클 끝나면 매번 권장)
- `/compact` - 컨텍스트 요약하여 토큰 절약
- `/cost` - 현재 세션 비용
- `/permissions` - 어떤 도구를 얼마나 자유롭게 쓸 수 있는지 설정
- `/review` - 현재 PR/diff 리뷰 (이 레포에 등록되어 있으면)

### 커스텀 슬래시 커맨드

`~/.claude/commands/` 또는 `<project>/.claude/commands/` 아래 `.md` 파일을 만들면 그게 슬래시 커맨드가 됩니다. 예시:

`~/.claude/commands/plan.md`:

```markdown
---
description: Plan-first mode. Output design, file:line references, rollback path.
---

다음 작업을 구현하기 전 Plan을 먼저 작성하세요.

요구:
- 변경할 파일을 file:line 단위로 명시
- 의존성과 부수 효과 정리
- 실패 시 rollback 절차
- 검증 방법 (테스트/로그/AST)

코드는 아직 쓰지 마세요. Plan만.

작업: $ARGUMENTS
```

이러면 `/plan order book reconnection logic` 같이 호출 가능합니다.

## IDE 연동

### VS Code / Cursor

Claude Code는 별도 IDE 플러그인 없이 터미널에서 도는 게 기본이지만, VS Code/Cursor 통합 터미널 안에서 `claude`를 띄우면 파일 변경 알림이 IDE에 즉시 반영됩니다. Cursor의 자체 AI와 Claude Code를 동시에 쓰는 게 가능하고, 저자는 Cursor는 자동완성용, Claude Code는 다단계 작업용으로 역할 분담합니다.

### tmux 권장 (Mac/Linux)

장시간 도는 작업은 tmux 세션 안에서 Claude Code를 띄워야 SSH 끊겨도 살아남습니다.

```bash
tmux new -s cc-trading
claude
# Ctrl+b d 로 detach, ssh 끊김 OK
tmux attach -t cc-trading
```

Windows에서는 tmux가 없으니 Windows Terminal + 백그라운드 trigger file 패턴으로 대체합니다 ([04-cowork-workflow.md](04-cowork-workflow.md) 참조).

## 권한 (Permissions)

Claude Code는 기본적으로 파일 수정과 명령 실행 시 매번 허락을 받습니다. 자주 쓰는 read-only 명령(`git status`, `ls`, `npm test` 등)은 미리 allowlist에 등록해두면 흐름이 끊기지 않습니다.

```json
// .claude/settings.json
{
  "permissions": {
    "allow": [
      "Bash(git status)",
      "Bash(git diff:*)",
      "Bash(npm test:*)",
      "Read(*)",
      "Grep(*)"
    ]
  }
}
```

`fewer-permission-prompts` 스킬을 쓰면 transcript에서 자주 나온 read-only 호출을 자동 추출해 줍니다.

## 셋업 검증

설치가 끝나면 다음 한 줄로 sanity check:

```bash
claude -p "현재 폴더의 파일 5개를 리스트하고, 각 파일이 무엇을 하는지 한 줄로 요약하세요."
```

응답이 정상이면 끝. 한글 깨지면 위 PYTHONIOENCODING 환경변수부터 다시 확인.
