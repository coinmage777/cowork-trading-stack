# polymarket-bot

[Polymarket](https://polymarket.com/?ref=coinmage) Up/Down 단기 마켓 + [Predict.fun](https://predict.fun/?ref=coinmage) 1시간 마켓 자동 베팅 봇. 5+ 전략 (expiry_snipe / reversal / Stoikov MM / Bayesian directional / merge_split arb / cold_split) + 자동 리딤 + 잔고 추적 + Telegram 통제.

## 핵심 기능

- **Multi-strategy**: 동일 봇에서 5+ 전략 동시 실행 (각 전략 LIVE/SHADOW/PAPER mode 분리)
- **Triple-lock LIVE**: 모든 전략은 `ENABLED=true` + `DRY_RUN=false` + `LIVE_CONFIRM=true` 모두 충족해야 LIVE
- **Whitelist**: `LIVE_ONLY_STRATEGIES=predict_snipe,hedge_arb` — 명시된 전략만 LIVE 진입
- **Circuit breaker**: 일일 손실 > `DAILY_STOP_LOSS` 시 자동 정지
- **Auto-claimer**: 만료된 winning 포지션 자동 리딤 (별도 venv)
- **Balance snapshots**: 10분마다 USDC 잔고 기록 → 실제 PnL truth
- **Singleton lock**: 중복 인스턴스 차단 (psutil + atexit)

## 전략 요약

| 전략 | 설명 | 현재 상태 |
|------|------|----------|
| `reversal_sniper` | 5분 BTC/ETH/SOL 꼬리 리버설 | DRY_RUN |
| `mm` | Stoikov-Avellaneda MM + Bayesian directional prior | DRY_RUN (γ=0.4, K=4.0) |
| `merge_split_arb` | YES+NO=$1 무위험 관계 활용 | DRY_RUN |
| `cold_split` | Multi-outcome cold market arb | DRY_RUN |
| `hedge_arb` | Polymarket vs Predict.fun 같은 자산 페어 헷지 | DRY_RUN |

## 파일 구조

```
polymarket-bot/
├── main.py              — 통합 실행 (모든 strategy loop)
├── market_scanner.py    — Polymarket API 스캔 + 주문 실행 (CLOB)
├── data_logger.py       — SQLite trade DB
├── auto_claimer.py      — 만료 포지션 자동 리딤
├── config.py            — 전략별 설정 + .env 로딩
├── strategies/
│   ├── expiry_snipe.py
│   ├── reversal_sniper.py
│   ├── mm_strategy.py
│   ├── bayesian_prior.py
│   ├── merge_split_arb.py
│   └── cold_split.py
├── predict_sniper.py    — Predict.fun sniper
├── predict_client.py    — Predict.fun SDK wrapper
└── bnb_monitor.py       — Predict.fun signer EOA BNB 가스 모니터
```

## 사용 예시

```bash
# 메인 실행 (LIVE)
python main.py --mode live

# Shadow only (시뮬레이션)
python main.py --mode shadow

# 자동 리딤 (별도 venv)
claim_venv/bin/python auto_claimer.py

# 일일 리포트
python poly_report.py --days 7
```

## 환경변수 (.env)

```env
# Polymarket CLOB
POLYMARKET_API_KEY=<...>
POLYMARKET_SECRET=<...>
POLYMARKET_PASSPHRASE=<...>

# Wallet
PRIVATE_KEY=<EVM_PK>
PROXY_WALLET=<PROXY_ADDR>

# Builder
BUILDER_KEY=<...>
BUILDER_SECRET=<...>
BUILDER_PASSPHRASE=<...>
RELAYER_API_KEY=<...>

# Predict.fun
PREDICT_API_KEY=<...>
PREDICT_PRIVATE_KEY=<EVM_PK>
PREDICT_ACCOUNT=<PREDICT_ACCOUNT_ADDR>

# Whitelist
LIVE_ONLY_STRATEGIES=predict_snipe

# Circuit breaker
DAILY_STOP_LOSS=-30.0

# Predict.fun 튜닝
PREDICT_SNIPE_BET_SIZE=3
PREDICT_SNIPE_MAX_ENTRY_PRICE=0.70
PREDICT_SNIPE_MIN_EDGE=0.03
PREDICT_SNIPE_MAX_MINUTES=60.0

# MM
MM_ENABLED=true
MM_DRY_RUN=true
MM_GAMMA=0.4
MM_K=4.0
MM_MIN_SPREAD_BPS=500

# Telegram
TELEGRAM_BOT_TOKEN=<...>
TELEGRAM_CHAT_ID=<...>

# DB
DB_PATH=trades_v2.db
```

## 의존성

- `py-clob-client` (Polymarket CLOB)
- `predict-sdk` (Predict.fun)
- `web3`, `eth-account`
- `sqlite3` (WAL 모드)
- `aiohttp`, `httpx`
