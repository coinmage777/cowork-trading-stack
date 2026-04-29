# telegram-control

> 한 줄 요약: Telegram bot 으로 트레이딩 봇을 원격 제어하기 위한 모듈. 명령어 기반 (`/status` `/pnl` `/balance` `/positions` `/restart` `/reload` `/close` `/kill <ex>` `/revive <ex>` `/bnb`) + CHAT_ID 화이트리스트 + 위험 명령 2단계 확인 (`/yes`) 으로 구성됩니다.

## 의존성 (Dependencies)

- `aiohttp` (Telegram getUpdates polling)
- `psutil` (process status)
- `subprocess` (daily_report 호출)
- (옵션) systemd unit 파일

## AI에게 어떻게 시켰나 (How this was built with Claude)

이 모듈을 만들 때 사용한 프롬프트 패턴:

> "@BotFather 발급 token 으로 Telegram getUpdates polling 봇 만들어 줘. 명령어 16+ 개 (`/status` `/pnl` `/balance` `/positions` `/funding` `/restart` `/reload` `/close` `/kill <ex>` `/revive <ex>` `/clear_cb` `/bnb` `/log <n>` `/help`) 처리. CHAT_ID 화이트리스트로 인증, `/close` 같은 위험 명령은 `/yes` 추가 입력 후 실행하는 2단계 확인. 봇 내부 상태는 `triggers/status.trigger` 파일을 통해 운영 중인 트레이딩 봇과 sidecar 로 통신."

AI가 자주 틀린 부분 (common AI mistakes for this code path):

- **getUpdates offset 관리 실수**: Telegram getUpdates 의 `offset` 파라미터 안 주면 같은 메시지가 폴링마다 재처리. 명령 한번 보냈는데 `/close` 가 무한 반복되는 사고 가능.
- **CHAT_ID 화이트리스트 누락**: AI 는 처음에 "supabase 등에서 사용자 등록" 같은 과한 설계를 하지만 단일 사용자 봇은 `TELEGRAM_CHAT_ID` 또는 콤마 구분 list 만으로 충분. 누락 시 모르는 사용자가 `/close` 보낼 수 있음.
- **2단계 확인 timeout 부재**: `/close` → `/yes` 사이 시간 제한 없으면 옛 confirm pending 이 다음날까지 살아있어 오작동. 60초 timeout 가드 필요.

힌트: 이 모듈은 6+ 개 운영 봇을 모니터링하면서 5번 이상 재작업했고, 그때마다 AI 가 놓친 것은 **getUpdates offset, /yes timeout, CHAT_ID 화이트리스트** 입니다.

## 모듈 구조 (file structure with one-liner per file)

| File | Purpose |
|------|---------|
| `telegram_commander.py` | 메인 봇 (aiohttp + getUpdates 폴링, 명령 dispatcher) |
| `alert_filters.json` | 알림 필터 (info/warn/crit 임계 + dedup 윈도우) |

## 명령어 리스트

| 명령 | 설명 | 위험 |
|------|------|------|
| `/start` | 봇 사용법 안내 | - |
| `/status` | 모든 거래소 현황 (PID, uptime, WS 상태) | - |
| `/pnl` | 일일/누적 PnL 리포트 (`daily_report.py` 호출) | - |
| `/balance` | 거래소별 잔고 (`equity_tracker.json` 기반) | - |
| `/positions` | 현재 오픈 포지션 (모든 거래소) | - |
| `/funding` | 거래소별 funding rate 차이 매트릭스 | - |
| `/restart` | graceful restart 트리거 (`triggers/restart.trigger`) | 중 |
| `/reload` | config hot reload | 저 |
| `/close` | 전체 청산 + 봇 종료 → `/yes` 확인 필요 | **고** |
| `/kill <exchange>` | 특정 거래소 자동 비활성 | 중 |
| `/revive <exchange>` | 자동 비활성된 거래소 재활성 | 중 |
| `/clear_cb` | circuit breaker 해제 | 저 |
| `/log <n>` | 최근 N줄 로그 | - |
| `/help` | 명령어 도움말 | - |

## 사용 예시 (Usage)

```bash
# 봇 시작 (별도 daemon)
python telegram_commander.py
# 또는 systemd:
# sudo systemctl start perp-dex-commander.service
```

Telegram 채팅에서:

```
사용자: /status
봇: 17 거래소 활성. PID 951352 (multi_runner) uptime 6h 30m.
    Trader-B PID 40644. Farmer-X PID 998563 (no cap).
    오늘 trades 43, WR 93%.

사용자: /pnl
봇: 어제 일일 PnL 리포트 (daily_report.py 출력).
    Perp DEX 거래소별 / Rise farmer / 누적 7d 등 항목별 표시.

사용자: /close
봇: 전체 청산 + 봇 종료. /yes 입력으로 확인.

사용자: /yes
봇: 청산 시작. 포지션 12개 close 중...
```

## 실전 함정 (Battle-tested gotchas)

운영하면서 깨진 부분들:

- **getUpdates offset 누락 → `/close` 무한 재실행**: 처음 offset 관리 빼먹어서 `/close` 메시지 한 번 보낸 게 폴링마다 다시 dispatch 되어 청산이 두 번 돌아간 사고. `last_update_id + 1` 로 offset 강제.
- **`/yes` confirm pending 이 다음날까지 살아있음**: 사용자가 `/close` 보낸 다음 그냥 잠들었다가, 다음날 다른 명령 의도로 `/yes` 비슷한 메시지 보냈는데 청산 실행됨. 60초 timeout 추가 후 안전.
- **CHAT_ID 화이트리스트 누락**: 처음 단일 사용자라고 무시했는데 token 우연히 노출 시 누구나 `/close` 가능. `TELEGRAM_ALLOWED_CHAT_IDS` 콤마 구분 list 필수.
- **`daily_report.py` subprocess 가 한국어 windows 에서 깨짐**: Korean Windows 의 cp949 codec 으로 한글 출력이 깨져 telegram 에 question marks. `PYTHONIOENCODING=utf-8` 환경변수 + subprocess `encoding='utf-8'` 명시 후 정상.
- **systemd unit 의 working dir 누락**: `daily_report.py` 가 상대 경로로 config 를 찾는데 systemd 의 default cwd 가 `/` 라 NotFound. unit 의 `WorkingDirectory=` 명시 필요.

## 응용 (How this fits with other modules)

- `20-exchange-wrappers/_combined` 의 `get_collateral`, `get_position` 호출 결과를 본 모듈이 `/balance`, `/positions` 명령으로 노출
- `30-strategy-patterns/volume-farmer` 의 kill switch 파일을 본 모듈의 `/kill <farmer>` 명령이 생성/삭제
- `40-realtime-infra/cross-venue-arb-scanner` 의 funding rate 매트릭스를 `/funding` 명령으로 표시
- `40-realtime-infra/kimp-listing-arb` 의 LIVE flip 트리플 락 상태를 `/status` 가 노출

결합 사용 시: 본 봇을 systemd unit 으로 띄우면서 `triggers/` 디렉터리를 운영 봇과 공유하면, 모든 sidecar 파일을 한 봇에서 통제 가능.

## 환경변수

```env
TELEGRAM_BOT_TOKEN=<...>          # @BotFather 발급
TELEGRAM_CHAT_ID=<USER_ID>         # 화이트리스트 (단일)
# 또는 다중 사용자:
TELEGRAM_ALLOWED_CHAT_IDS=<id1>,<id2>,<id3>

# 봇 코드 경로 (subprocess 호출용)
BOT_BASE_DIR=/opt/perp-dex-bot/multi-perp-dex
DAILY_REPORT_SCRIPT=scripts/daily_report.py

# Korean Windows 운영 시
PYTHONIOENCODING=utf-8
```

## 거래소 가입 링크

