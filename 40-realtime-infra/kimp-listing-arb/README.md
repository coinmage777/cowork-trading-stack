# kimp-listing-arb

> 한 줄 요약: KR 신규 상장 → 글로벌 perp 가격 갭 ("김치 프리미엄") 자동 감지·진입·수렴 청산. HYPER 신규 상장 사례 (entry KIMP +30.23%, exit +2.33% → net +26.71%) 자동화 모듈입니다.

## 의존성 (Dependencies)

- Python 3.11+ (stdlib + ccxt)
- `aiohttp`, `ccxt` (글로벌 perp BBO)
- KR 거래소: bithumb private API (LIVE), upbit (paper only)
- (옵션) Telegram bot (알림)

## AI에게 어떻게 시켰나 (How this was built with Claude)

이 모듈을 만들 때 사용한 프롬프트 패턴:

> "업비트/빗썸 신규 상장 공지를 0.5~1초 폴링하는 listing_detector 가 이미 있어. 그 detector 가 발행하는 ListingEvent 를 받아서, 부트 딜레이 60초 후에 KR 현물 BBO + 글로벌 perp BBO + USDT/KRW 환율을 30초 폴링하면서 |KIMP| >= 5% 면 진입 (KR sell + global short, 또는 역프 시 반대), |KIMP| <= 1% 면 converged 청산, |KIMP| >= 30% 역방향이면 kill_gap. 4시간 timeout, kill switch 파일 가드, dry_run + live_confirm 트리플 락으로 LIVE flip."

AI가 자주 틀린 부분 (common AI mistakes for this code path):

- **USDT/KRW 환율 누락**: KR 가격은 KRW 인데 글로벌 perp 는 USDT. 환율 빼고 단순 % 비교하면 +1300% 같은 가짜 KIMP. 환율 polling + 캐시 필수.
- **출금 잠금 무시**: 신규 상장 토큰은 첫 1–24h 출금 잠겨있는 경우 흔함. AI 는 "글로벌에서 사서 KR 로 출금" 같은 시나리오를 권장하는데 실제로는 불가능. 본 모듈은 KR 매도 + 글로벌 short 양쪽 모두 매도/숏이라 출금 잠금 자체는 막지 않지만, **수렴 청산 시점 KR 매수 (buy back)** 단계에서 호가 부족 / 가격 점프 가능.
- **부트 딜레이 누락**: 상장 직후 30초~1분은 호가가 수직으로 튀어 KIMP 측정값 자체가 의미 없음. 부트 딜레이 60초 강제.

힌트: 이 모듈은 HYPER 신규 상장 사례 사후 분석 + 다른 listing 들 paper 검증으로 4번 이상 재작업했고, 그때마다 AI 가 놓친 것은 **USDT/KRW 환율, 출금 잠금, 부트 딜레이, kill switch 파일 race condition** 입니다.

## 모듈 구조 (file structure with one-liner per file)

| File | Purpose |
|------|---------|
| `kimp_listing_arb.py` | 메인 모듈 (`KimpListingArb`, `simulate_arb`, `calc_kimp_pct`) |
| `kimp_listing_arb_jobs.jsonl` | 잡 영속화 (진입/청산 기록) |

(추가로 부모 프로젝트의 `backend/main.py` lifespan 에서 `await kimp_listing_arb.start()` 호출, `backend/services/listing_detector.py` 가 이벤트 발행)

## 핵심 흐름

```
listing_detector (Upbit/Bithumb 공지 폴링, 0.5~1s)
    │
    ├─ ListingEvent (ticker, exchange, binance_perp, bybit_perp)
    │
    ▼
KimpListingArb._on_listing_event  (in-process callback)
    │
    ├─ 부트 딜레이 60s (KR 체결 안정화)
    │
    ▼
_monitor_arb_opportunity  (30s 폴링)
    │  ├─ KR 현물 BBO   (bithumb/upbit)
    │  ├─ 글로벌 perp BBO  (binance/bybit ccxt)
    │  └─ USDT/KRW
    │
    ├─ |KIMP| >= 5% 이면 진입 (paper or live)
    │     direction = kr_sell_global_short   (KIMP 양수)
    │              or kr_buy_global_long     (역프, KIMP 음수)
    │
    ▼
청산 조건 (양자택일)
    ├─ |KIMP| <= 1%  →  converged (수익 확정)
    ├─ |KIMP| >= 30% (역방향) → kill_gap (역선택 회피)
    ├─ 4시간 경과 → timeout (강제 청산)
    └─ kill switch 파일 존재 → 즉시 abort
```

## 환경변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `KIMP_ARB_ENABLED` | `true` | 모듈 자체 enable (false 면 detector 콜백 무시) |
| `KIMP_ARB_DRY_RUN` | `true` | true 면 paper 모드 (DB/JSONL 기록만, 실주문 X) |
| `KIMP_ARB_LIVE_CONFIRM` | `false` | 트리플 락의 3번째. true + dry_run=false + enabled 모두 충족해야 LIVE |
| `KIMP_ARB_ENTRY_GAP_PCT` | `5.0` | 진입 임계 KIMP % (절대값) |
| `KIMP_ARB_EXIT_GAP_PCT` | `1.0` | 청산 임계 (수렴) % |
| `KIMP_ARB_KILL_GAP_PCT` | `30.0` | 역방향으로 더 벌어지면 kill |
| `KIMP_ARB_MAX_SIZE_USD` | `100.0` | 1 거래당 max 사이즈 |
| `KIMP_ARB_DAILY_MAX_TRADES` | `10` | 일일 최대 진입 횟수 |
| `KIMP_ARB_DAILY_CAP_USD` | `500.0` | 일일 누적 노치오날 캡 |
| `KIMP_ARB_BOOT_DELAY_SEC` | `60` | 상장 직후 가격 안정화 대기 (초) |
| `KIMP_ARB_MONITOR_INTERVAL_SEC` | `30` | 갭 폴링 주기 |
| `KIMP_ARB_MONITOR_TIMEOUT_MIN` | `240` | 4h 안에 수렴 안 되면 timeout |
| `KIMP_ARB_MAX_EVENT_AGE_SEC` | `600` | 재시작 시 옛 이벤트 무시 |
| `KIMP_ARB_KILL_FILE` | `data/KILL_KIMP_ARB` | 파일 존재 시 모든 진입 차단 |
| `KIMP_ARB_JOBS_PATH` | `strategies_minara/data/kimp_listing_arb_jobs.jsonl` | 잡 영속화 경로 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | 알림 (없으면 no-op) |

## 사용 예시 (Usage)

### 1) backend 통합 (자동 시작)

`backend/main.py` 가 import 후 lifespan 에서 `await kimp_listing_arb.start()` 호출. 정상 가동 시 다른 서비스 (`listing_detector`, `listing_executor`) 와 같이 떠 있다. 상태 확인:

```bash
curl http://localhost:8000/api/auto/kimp-listing-arb?limit=20
```

### 2) 단독 백테스트 (HYPER 신규 상장 사례)

```bash
cd "<project path>"
python -m strategies_minara.kimp_listing_arb --backtest \
    --ticker HYPER --entry-kr-usd 56 --entry-global-usd 43 \
    --exit-kr-usd 44 --exit-global-usd 43 \
    --size-usd 100 --fee-pct 0.1 --slip-pct 0.2
```

출력 (JSON):

```json
{
  "ticker": "HYPER",
  "entry_kimp_pct": 30.23,
  "exit_kimp_pct": 2.33,
  "gross_pct": 27.91,
  "friction_pct": 1.20,
  "net_pct": 26.71,
  "net_usd": 26.71,
  "size_usd": 100.0
}
```

### 3) 단위 테스트 (Mock pricing)

`simulate_arb()` + `calc_kimp_pct()` 는 stdlib only — 외부 의존 없이 호출 가능.

## LIVE flip 절차

기본은 **paper 모드**. 실주문 활성화 순서:

1. 환경변수 3개 동시 설정:
   ```
   KIMP_ARB_ENABLED=true
   KIMP_ARB_DRY_RUN=false
   KIMP_ARB_LIVE_CONFIRM=true
   ```
2. KR 거래소 잔고 + 글로벌 거래소 USDT 잔고 모두 충분한지 확인
3. `data/KILL_KIMP_ARB` 파일이 없는지 확인 (있으면 모든 진입 차단)
4. **첫 LIVE 사례 발생 후 즉시** `kimp_listing_arb_jobs.jsonl` + Telegram 로그 확인

LIVE 가드:
- KR private 주문이 구현된 venue 만 LIVE 가능 (현재 `bithumb` 만, upbit 는 paper 폴백)
- daily_cap, per-trade size, kill_gap 모두 가드
- 한쪽만 체결 + 다른쪽 실패 시 자동 reverse 시도

## 실전 함정 (Battle-tested gotchas)

운영하면서 깨진 부분들:

- **Upbit private 주문 미구현**: Upbit 상장 케이스는 paper 모드만 지원. Bithumb 상장만 `submit_bithumb_spot_order` 통해 LIVE 가능.
- **신규 상장 토큰 출금 잠금**: 첫 1–24h 출금 잠겨있는 경우 흔함. KR 매도 + 글로벌 perp short 양쪽 모두 매도/숏이라 출금 잠금 자체는 막지 않지만, 수렴 청산 시점 KR 매수 (buy back) 단계에서 호가 부족 / 가격 점프 가능.
- **얇은 호가 → max_size_usd=100 부터 시작**: KR 시장가 주문은 보수적이지만 신규 토큰은 호가 얇음. $500+ 사이즈는 다음 검증 후 점진 확대.
- **`binance_perp` / `bybit_perp` flag false**: 글로벌 perp 미상장 케이스는 자동 skip. 한쪽만 (Bybit 만) 상장된 경우도 진행 (선호: binance).
- **USDT/KRW 환율 sub-second 흔들림**: 30초 폴링이라 sub-second 가격 흔들림 무시되어 진입 KIMP 가 계산상 약간 오인 가능. 영향은 0.1~0.3% 수준이지만 임계 근처에서 진입/non-진입 이 갈릴 수 있음.
- **kill_file race condition**: kill 파일 생성 직후 진입 사이클이 시작되면 1 cycle 동안 무시될 수 있음. 매 30초 + 진입 직전 두 번 체크.

## HYPER 신규 상장 시뮬레이션 (사용자 케이스 재현)

| 변수 | 값 | 비고 |
|------|----|------|
| 진입 KR 가격 | $56 (≈ 72,800 KRW @ 1300) | 업비트 상장 직후 새벽 1시 |
| 진입 글로벌 perp | $43 | Binance HYPER/USDT |
| 청산 KR 가격 | $44 | 1–2h 후 수렴 |
| 청산 글로벌 perp | $43 | flat |
| 진입 KIMP | +30.23% | KR sell + global short |
| 청산 KIMP | +2.33% | converged |
| Gross | +27.91% | KIMP delta |
| Friction (4 leg, 0.3%/leg) | -1.20% | 수수료+슬리피지 |
| Net | +26.71% | KIMP delta |

스케일업 가정 (보수 friction 0.6%/leg):
- net_pct ≈ +25.51% / 사례
- 일일 1–2 케이스, 월 5–10 케이스 추정 가능

## 응용 (How this fits with other modules)

- `40-realtime-infra/cross-venue-arb-scanner` 의 글로벌 perp BBO 와 본 모듈의 KR 현물 BBO 결합 → 통합 KIMP arb
- `60-ops-runbooks/telegram-control` 의 `/status`, `/kill` 명령으로 모듈 ON/OFF
- 부모 프로젝트 (`backend/main.py`) 에 in-process callback 으로 통합

결합 사용 시: 본 모듈은 `_system_services` dict 의 health aggregator 와 함께 띄워야 telegram-control 의 `/status` 에서 가시성 확보.

## TODO / 후속

- [ ] Upbit private 주문 어댑터 추가 → upbit 상장도 LIVE 지원
- [ ] DB 통합 (현재 JSONL 만) — `gap_history.db` 같은 sqlite 로 통합
- [ ] daily_report 에 KIMP arb PnL 합산
- [ ] 출금 잠금 사전 감지 → 수렴 청산 위험 감안 사이즈 조절
- [ ] 글로벌 perp 펀딩 레이트 페널티 가산 (장기 보유 시)

## 즉시 정지

```bash
echo "STOP" > "<project path>/data/KILL_KIMP_ARB"
```

다음 폴링 사이클 (30s 이내) 에 모든 신규 진입 차단 + 진행 중 잡은 KILL_GAP 처리.
