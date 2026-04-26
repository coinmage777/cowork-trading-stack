# KIMP Listing Arb — KR 신규 상장 → 글로벌 perp 갭 따리

Leo Telegram 의 **HYPER 4/25 사례** 자동화. 업비트/빗썸 상장 직후 KR 현물이
글로벌 perp 대비 +30% 까지 벌어지는 **김치 프리미엄(KIMP)** 을 자동 감지·진입·수렴 청산.

---

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

---

## 환경변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `KIMP_ARB_ENABLED` | `true` | 모듈 자체 enable (false 면 detector 콜백 무시) |
| `KIMP_ARB_DRY_RUN` | `true` | true 면 paper 모드 (DB/JSONL 기록만, 실주문 X) |
| `KIMP_ARB_LIVE_CONFIRM` | `false` | 트리플 락의 3번째. true + dry_run=false + enabled 모두 충족해야 LIVE |
| `KIMP_ARB_ENTRY_GAP_PCT` | `5.0` | 진입 임계 KIMP % (절대값) |
| `KIMP_ARB_EXIT_GAP_PCT` | `1.0` | 청산 임계 (수렴) % |
| `KIMP_ARB_KILL_GAP_PCT` | `30.0` | 역방향으로 더 벌어지면 kill (역선택 방지) |
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

---

## 사용 예시

### 1) backend 통합 (자동 시작)

`backend/main.py` 가 import 후 lifespan 에서 `await kimp_listing_arb.start()` 호출.
정상 가동 시 다른 서비스(`listing_detector`, `listing_executor`)와 같이 떠 있다.
상태 확인:

```bash
curl http://localhost:8000/api/auto/kimp-listing-arb?limit=20
```

### 2) 단독 백테스트 (HYPER 4/25 사례)

```bash
cd "C:\Users\A\내 드라이브\DH_bithumb_arb-main"
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

`strategies_minara/kimp_listing_arb.py` 의 `simulate_arb()` + `calc_kimp_pct()`
는 stdlib only — 외부 의존 없이 호출 가능.

---

## LIVE flip 절차

기본은 **paper 모드**. 실주문 활성화는 다음 순서로:

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

---

## 운영 가이드

### 즉시 정지
```bash
echo "STOP" > "C:/Users/A/내 드라이브/DH_bithumb_arb-main/data/KILL_KIMP_ARB"
```
다음 폴링 사이클(30s 이내)에 모든 신규 진입 차단 + 진행 중 잡은 KILL_GAP 처리.

### 모니터링 핵심 메트릭
- `total_detected` — listing 이벤트 수신 수
- `total_entered` — 실제 진입 수
- `total_closed` — 수렴 청산 (수익)
- `total_aborted` — kill_gap / timeout / 에러로 abort
- `daily_trades` / `daily_spent_usd` — 일일 캡 진척률

### 알림
Telegram 환경변수 설정 시 진입/청산 모두 자동 푸시. dedup 미적용 — 이벤트 단위 1:1.

---

## 위험 / 한계

1. **Upbit private API 미구현** — Upbit 상장 케이스는 paper 모드만 지원.
   Bithumb 상장은 `submit_bithumb_spot_order` 통해 LIVE 가능.
2. **출금 비활성화** — 신규 상장 토큰은 첫 1–24h 출금 잠겨 있는 경우 흔함.
   현재 모듈은 출금 가능 여부 사전 체크하지 않음. KR spot 매도 + 글로벌 perp short
   양쪽 모두 매도/숏이라 출금 잠금 자체는 막지 않지만, **수렴 청산 시점에**
   KR 매수(buy back) 단계에서 호가 부족 / 가격 점프로 슬리피지 발생 가능.
3. **체결률** — KR 시장가 주문은 보수적이지만 신규 토큰은 호가 얇음.
   `max_size_usd=100` 부터 시작 추천.
4. **글로벌 perp 미상장** — listing 이벤트 payload 의 `binance_perp`/`bybit_perp`
   가 false 면 자동 skip. 부분적 한쪽만 있어도 진행 (선호: binance).
5. **KIMP 부호 오인** — entry 직후 USDT/KRW 환율 급변하면 진입 KIMP 가
   계산상 오인될 수 있음. 30s 폴링이라 sub-second 가격 흔들림은 포함 안 됨.

---

## HYPER 4/25 시뮬레이션 (사용자 케이스 재현)

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
| Net | +26.71% | size $100 → **+$26.71** |

→ Leo 의 트윗 narrative ("30% 진입, 2.3% 정리") 와 일치.

스케일업 가정 (size=$1000, friction 보수 0.6%/leg):
- net_pct ≈ +25.51% → **+$255 / 사례**
- 일일 1–2 케이스, 월 5–10 케이스 추정 가능

---

## 통합 지점

- `backend/main.py` — `kimp_listing_arb` 인스턴스 + lifespan start/stop + GET endpoint
- `backend/services/listing_detector.py` — `add_listener` 로 자동 구독
- `_system_services` dict — health aggregator 포함

검증:
```bash
python -c "import ast; ast.parse(open(r'backend/main.py', encoding='utf-8').read()); print('OK')"
python -c "from strategies_minara.kimp_listing_arb import KimpListingArb; print('OK')"
```

---

## TODO / 후속

- [ ] Upbit private 주문 어댑터 추가 → upbit 상장도 LIVE 지원
- [ ] DB 통합 (현재 JSONL 만) — `gap_history.db` 같은 sqlite 로 통합
- [ ] daily_report 에 KIMP arb PnL 합산
- [ ] 출금 잠금 사전 감지 → 수렴 청산 위험 감안 사이즈 조절
- [ ] 글로벌 perp 펀딩 레이트 페널티 가산 (장기 보유 시)
