# telegram-control

Telegram bot으로 트레이딩 봇 원격 제어. 명령어 기반 (`/status` `/pnl` `/balance` `/positions` `/restart` `/reload` `/close` `/kill <ex>` `/revive <ex>` `/bnb`). CHAT_ID 화이트리스트 보안.

## 핵심 기능

- **명령어 인식**: `/start` 외 16+ 명령
- **CHAT_ID 화이트리스트**: 등록된 사용자만 명령 실행 가능
- **2단계 확인**: 위험 명령 (`/close`)은 `/yes` 추가 입력 후 실행
- **Status dump**: `triggers/status.trigger` 통해 봇 내부 상태 JSON 받기
- **Subprocess invoke**: `daily_report.py` 등 서브프로세스 호출로 PnL 리포트

## 명령어 리스트

| 명령 | 설명 | 위험 |
|------|------|------|
| `/start` | 봇 사용법 안내 | - |
| `/status` | 모든 거래소 현황 (PID, uptime, WS 상태) | - |
| `/pnl` | 일일/누적 PnL 리포트 (daily_report.py 호출) | - |
| `/balance` | 거래소별 잔고 (equity_tracker.json 기반) | - |
| `/positions` | 현재 오픈 포지션 (모든 거래소) | - |
| `/funding` | 거래소별 funding rate 차이 매트릭스 | - |
| `/restart` | graceful restart 트리거 (`triggers/restart.trigger`) | 중 |
| `/reload` | config hot reload | 저 |
| `/close` | 전체 청산 + 봇 종료 → `/yes` 확인 필요 | **고** |
| `/kill <exchange>` | 특정 거래소 자동 비활성 | 중 |
| `/revive <exchange>` | 자동 비활성된 거래소 재활성 | 중 |
| `/clear_cb` | circuit breaker 해제 | 저 |
| `/bnb` | Predict.fun signer EOA BNB 잔고 | - |
| `/log <n>` | 최근 N줄 로그 | - |
| `/help` | 명령어 도움말 | - |

## 파일 구조

```
telegram-control/
├── telegram_commander.py    — 메인 봇 (aiohttp + getUpdates 폴링)
├── alert_filters.json       — 알림 필터 (info/warn/crit 임계)
└── README.md
```

## 사용 예시

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
    Polymarket PID 40644. Rise farmer PID 998563 (no cap).
    오늘 trades 43, WR 93%.

사용자: /pnl
봇: 어제 +$5.30 (Perp DEX). Rise farmer -$1.74 (volume $2400).
    누적 7d +$xxx.

사용자: /close
봇: ⚠️ 전체 청산 + 봇 종료. /yes 입력으로 확인.

사용자: /yes
봇: 청산 시작. 포지션 12개 close 중...
```

## 환경변수

```env
TELEGRAM_BOT_TOKEN=<...>          # @BotFather 발급
TELEGRAM_CHAT_ID=<USER_ID>         # 화이트리스트 (단일)
# 또는 다중 사용자:
TELEGRAM_ALLOWED_CHAT_IDS=<id1>,<id2>,<id3>

# 봇 코드 경로 (subprocess 호출용)
BOT_BASE_DIR=/opt/perp-dex-bot/multi-perp-dex
DAILY_REPORT_SCRIPT=scripts/daily_report.py
```

## 의존성

- `aiohttp` (Telegram getUpdates polling)
- `psutil` (process status)
- `subprocess` (daily_report 호출)
