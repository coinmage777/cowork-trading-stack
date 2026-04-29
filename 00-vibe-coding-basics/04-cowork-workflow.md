# 04 - Cowork Workflow

저자가 Claude Code와 일상적으로 일하는 방식. "vibe coding"의 실제 동작 표면입니다.

## 하루의 모양

대략적인 흐름:

1. 아침: 어제 돌려둔 백테스트/봇 상태 확인 (모바일 Telegram으로)
2. 메인 머신 앞: tmux 세션 attach, 어제 끊긴 지점에서 재개
3. 새 작업: Plan → Code (한 파일씩) → Review (Gaslight) → Commit
4. 외출 중: Telegram으로 봇 상태 모니터링, 필요하면 `/restart`
5. 저녁: 장기 작업(리팩터링, 백테스트)을 `/loop`이나 background agent로 걸어두고 detach

## tmux 세션 (Mac/Linux)

장기 작업의 기본 컨테이너. SSH가 끊겨도, 노트북을 닫아도 살아남습니다.

```bash
# 세션 생성
tmux new -s perp-dex

# 윈도우 분할 (좌측 코드, 우측 로그)
# Ctrl+b "  -> horizontal split
# Ctrl+b %  -> vertical split

# 좌측: claude code
claude

# 우측: 로그 tail
tail -f logs/perp_dex.log

# detach: Ctrl+b d
# 다시 붙기: tmux attach -t perp-dex
```

저자의 표준 세션 4개:

- `cc-main` — 메인 코딩 세션
- `cc-bot` — 실행 중인 트레이딩 봇 + 로그
- `cc-monitor` — 모니터링 대시보드 / DB 쿼리
- `cc-tg` — Telegram bot 프로세스

## /loop 모드

`/loop` 슬래시 커맨드는 같은 프롬프트(또는 슬래시 커맨드)를 일정 간격으로 반복 실행합니다.

쓰는 곳:

- 5분마다 PR 상태 확인 → "/loop 5m /babysit-prs"
- 1시간마다 봇 PnL 체크 → "/loop 1h /check-pnl"
- 모델이 알아서 페이싱 → "/loop /watch-deploy" (간격 생략)

주의: `/loop`는 토큰을 빠르게 먹습니다. 안 쓸 때는 명시적으로 멈춥니다.

## Background Agents

Claude Code는 메인 대화와 별개로 background agent를 띄울 수 있습니다. 메인 세션은 인터랙티브하게 다른 일 하면서, agent는 따로 백테스트나 리팩터링 같은 긴 작업을 돕니다.

전형적 사용:

```
이 리팩터링은 background agent에게 맡기세요.
- 입력: refactor_plan.md
- 출력: 각 파일 수정 후 git commit, PR 생성
- 끝나면 알림

저는 그동안 다른 모듈 작업합니다.
```

실패 시 agent가 멈추므로, agent에게는 "막히면 stop, 추측 금지"를 명시하세요.

## Telegram 모바일 원격 제어

집 밖에서 봇을 제어해야 할 때를 대비해 Telegram bot을 운용합니다. `60-ops-runbooks/telegram-control/` 모듈에 코드/배포 가이드가 있습니다.

### 핵심 커맨드

```
/status      현재 봇 상태 (실행/정지, 마지막 거래 시각, PnL)
/restart     봇 재시작 (graceful)
/close       모든 포지션 청산 후 봇 정지
/positions   현재 포지션 목록
/logs 100    마지막 100줄 로그
/pnl 1d      24시간 PnL
/kill        강제 종료 (위험, 확인 절차 있음)
```

### 인증

Telegram bot은 chat_id 화이트리스트로 인증합니다.

```python
# config.yaml
telegram:
  bot_token: "${TELEGRAM_BOT_TOKEN}"
  allowed_chat_ids:
    - 123456789  # 본인 chat_id만
```

추가로 `/close`처럼 위험한 커맨드는 6자리 OTP를 매번 받습니다. 봇 토큰이 노출돼도 OTP 없으면 청산 불가.

### 알림 채널 분리

- **#trading-alerts** — 체결, 청산, 에러 (high frequency)
- **#trading-status** — 일일 요약, PnL (low frequency)
- **#trading-emergency** — 손절 발동, API 키 실패, balance 이상 (page 알림)

채널 분리 안 하면 알림 피로로 진짜 emergency를 놓칩니다.

## "Trigger File" 패턴 (Windows-friendly)

Linux/Mac이면 SIGHUP으로 봇에게 "config 다시 읽어"를 시키면 됩니다. Windows는 SIGHUP이 없습니다.

대안: **trigger file**. 봇이 N초마다 특정 파일의 mtime을 체크하고, 변하면 액션을 수행합니다.

### 구조

```
trigger/
├── reload_config       # touch 하면 config 재로드
├── pause               # 존재하면 신규 진입 정지
├── close_all           # 존재하면 전 포지션 청산 후 자기삭제
└── reload_strategy     # touch 하면 전략 모듈 hot reload
```

### 봇 측 코드 (개념)

```python
class TriggerWatcher:
    def __init__(self, dir: Path):
        self.dir = dir
        self._mtime: dict[str, float] = {}

    async def poll(self):
        for path in self.dir.iterdir():
            mtime = path.stat().st_mtime
            prev = self._mtime.get(path.name, 0)
            if mtime > prev:
                self._mtime[path.name] = mtime
                await self.handle(path)

    async def handle(self, path: Path):
        if path.name == "reload_config":
            await self.bot.reload_config()
        elif path.name == "close_all":
            await self.bot.close_all_positions()
            path.unlink()  # one-shot
        elif path.name == "pause":
            self.bot.paused = True
```

### Telegram → trigger file 연결

Telegram bot이 명령을 받으면 trigger file을 touch만 하고 끝납니다. 실제 봇 프로세스는 자기 페이스로 trigger를 polling. 이 분리가 안전합니다 — Telegram bot이 죽어도 봇은 살고, 봇이 죽어도 다음 재기동 시 trigger를 처리합니다.

```python
# telegram_handler.py
@bot.command("pause")
async def cmd_pause(ctx):
    if not authorized(ctx.chat.id):
        return
    Path("trigger/pause").touch()
    await ctx.reply("Pause requested. Bot will pause within 5s.")
```

상세 구현은 `60-ops-runbooks/telegram-control/` 참조.

## 일일 루틴 체크리스트

- [ ] 아침: Telegram `/status` 확인
- [ ] 어제 PR 머지/리뷰 정리
- [ ] 새 작업 → Plan first
- [ ] 한 사이클(plan → code → review → commit) 끝나면 `/clear`
- [ ] 점심/저녁 외출 전: 봇 상태 OK 확인, 알림 채널 살아있는지 확인
- [ ] 자기 전: 메모리 업데이트(오늘 배운 것), trigger file 정리, tmux detach

## 안티 패턴

- 한 tmux 세션에서 코딩 + 봇 실행 동시에 → 봇 죽으면 코딩 컨텍스트도 잃음. 분리하세요.
- Telegram에 봇 토큰 + 거래소 API 키 같이 두기 → Telegram이 털리면 다 털림. 분리.
- trigger file을 git에 commit → `.gitignore`에 추가하세요.
- `/loop`을 켜둔 채 잠자리 → 새벽에 토큰 폭주. 명시적으로 끄고 자세요.

## 한 줄 요약

> tmux로 세션 격리, /loop과 background agent로 시간 격리, Telegram + trigger file로 위치 격리. 격리되어야 안전하게 야망찬 작업을 시킬 수 있습니다.
