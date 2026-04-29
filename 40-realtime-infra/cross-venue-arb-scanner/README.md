# cross-venue-arb-scanner

> 한 줄 요약: 다중 CEX/DEX 동시 ticker fetch → spread divergence 탐지. 신규 listing 토큰이 venue 간 가격 발견 단계에서 13%+ gap 이 발생하는 순간을 포착해 Telegram 으로 푸시합니다.

## 의존성 (Dependencies)

- `ccxt` async (다중 venue 동시 fetch)
- `aiohttp`, `asyncio`
- (옵션) Telegram bot token (warn/crit 알림)

## AI에게 어떻게 시켰나 (How this was built with Claude)

이 모듈을 만들 때 사용한 프롬프트 패턴:

> "ccxt async 로 binance/bybit/bitget/gate/mexc/kucoin 6+ 거래소 ticker 를 동시 fetch 해 줘. `asyncio.gather(return_exceptions=True)` 로 한 venue timeout 이 다른 venue 에 영향 안 가게. top-of-book BID/ASK 외에 `size_usd` notional 기준 VWAP 도 계산해서 실제 진입 가능한 spread 를 측정. 임계 3단계 (info/warn/crit) + 같은 페어 30분 dedup, warn/crit 는 Telegram 즉시 푸시. JSONL 로 사후 분석용 시그널 로그도 남겨 줘."

AI가 자주 틀린 부분 (common AI mistakes for this code path):

- **Top-of-book 만 보고 fake gap 진입 권장**: ccxt ticker bid/ask 는 1-tick 만 표시. 호가 깊이 부족한데 gap 6% 라고 알림 → 실제 size $500 진입 시 슬리피지로 gap 이 사라짐. VWAP 으로 noational 기준 측정해야 함.
- **`asyncio.gather` exception 무시**: AI 가 `return_exceptions=True` 를 빼먹고 한 venue timeout 이 전체 gather 를 중단시키는 버그.
- **Dedup 윈도우 누락**: 같은 페어가 분당 다섯 번 알림으로 도배. 30분 dedup 또는 hash 기반 시그널 ID 필요.

힌트: 이 모듈은 HYPER 사례 + 다른 신규 토큰 listing 들 거치며 5번 이상 재작업했고, 그때마다 AI 가 놓친 것은 **VWAP fill 률 50% 미만 시 severity 강등, dedup 윈도우, ccxt timeout 격리** 입니다.

## 모듈 구조 (file structure with one-liner per file)

| File | Purpose |
|------|---------|
| `hyper_listing_arb_scanner.py` | HYPER 등 신규 listing 다중 venue gap 스캐너 (메인) |
| `funding_arb.py` | Funding rate cross-venue arb (delta=0 + funding 수익) |
| `spot_perp_arb.py` | Spot vs Perp basis arb (cash & carry) |
| `funding_scan.py` | 거래소별 funding rate 스냅샷 |
| `funding_monitor.py` | 실시간 funding 차이 모니터 |
| `funding_report.py` | 일일 funding 수익 리포트 |
| `listing_arb_scanner.py` | 일반 신규 listing 스캐너 (HYPER 외) |
| `listing_arb_runner.py` | auto-trade runner (사전 자금 분산 필요) |
| `basis_arb_trader.py` | Basis (perp-spot) arb 자동 실행 |

## 핵심 기능

- **6+ venue 동시 fetch**: ccxt async + `asyncio.gather(return_exceptions=True)` → 한 venue timeout 시 나머지 영향 X
- **VWAP 기반 gap**: top-of-book BID/ASK 외에 size_usd notional 기준 VWAP 계산 → 실제 진입 가능한 spread 측정
- **부분체결 강등**: fill < 50% 시 severity 다운그레이드 (호가 깊이 부족 = fake gap 방지)
- **3단계 임계**: info / warn / crit (예: 0.3% / 1.5% / 4%)
- **Dedup**: 같은 페어 30분 내 중복 알림 X
- **Telegram 알림**: warn/crit 즉시 푸시
- **JSONL 시그널 로그**: 사후 분석용 구조화 로그

## 주요 클래스

```python
class HyperArbScanner:
    venues: List[str] = ["binance", "bybit", "bitget", "gate", "mexc", "kucoin"]
    size_usd: float = 500            # VWAP 계산 notional
    interval_sec: float = 10          # poll 주기
    info_threshold: float = 0.3       # %
    warn_threshold: float = 1.5
    crit_threshold: float = 4.0

    async def fetch_all_venues(self) -> Dict[str, OrderBook]
    def compute_pairs(self, books: Dict) -> List[Signal]  # NxN-N pairs
    async def notify(self, signal: Signal) -> None
    async def run(self, duration: int = 0) -> None  # 0 = infinite
```

## 사용 예시 (Usage)

```bash
# 5분 1회 + Telegram 알림
python -m cross_venue_arb_scanner.hyper_listing_arb_scanner \
    --token HYPER --quote USDT \
    --duration 300 --interval 10 \
    --warn 2 --crit 5

# Daemon (Ctrl+C 로 종료)
nohup python -m cross_venue_arb_scanner.hyper_listing_arb_scanner \
    --duration 0 --size-usd 1000 --interval 5 \
    >> logs/arb_scanner.log 2>&1 &

# 다른 토큰
python -m cross_venue_arb_scanner.hyper_listing_arb_scanner \
    --token NEWCOIN --quote USDT
```

출력 예시:

```
=== 12:20:22 UTC | HYPER/USDT | size=$500 ===
VENUE      BID        ASK        BID_VWAP   ASK_VWAP   FILL_USD
binance    0.165000   0.165400   0.165000   0.165516   $500/500
kucoin     0.173500   0.173600   0.168671   0.182448   $500/194
[WARN] BUY binance @ 0.165400 -> SELL kucoin @ 0.173500 | gap=4.90% (vwap 1.91%)
```

## Funding rate arbitrage (별도 모드)

`funding_arb.py` — 두 venue funding rate 차이 활용:

```
A funding +0.5% (long pays short)
B funding -0.2% (short pays long)
A short + B long → funding spread capture
```

기대 APR 0.5–3% (변동성 큼).

## 실전 함정 (Battle-tested gotchas)

운영하면서 깨진 부분들:

- **VWAP fill 률 < 50% 인데 WARN 발송**: kucoin 호가가 얇아서 $500 noational 의 VWAP 이 위쪽으로 19% 까지 튀어나오는 케이스. 실제 진입은 $194 만 fill 되고 나머지는 다음 호가까지 가야 함. fill_usd / size_usd < 0.5 면 severity 한 단계 강등.
- **ccxt 한 venue timeout → 전체 gather hang**: 처음 `return_exceptions=True` 빼먹어 mexc 5초 timeout 으로 전체 사이클 5초 지연. 30+ 사이클 차이.
- **Dedup 윈도우 누락 → 같은 페어 분당 5번 알림**: HYPER 처럼 변동성 큰 토큰이 임계 위/아래 진동하며 매 사이클 alert. (페어, 방향) 키로 30분 윈도우 dedup.
- **listing_arb_runner 의 fund 분산 누락**: auto-trade 모드에서 한 venue 에만 USDT 잔고가 있고 다른 venue 에는 없어서, 진입 신호 떴을 때 한쪽만 진입 → 단일 leg 위험. 사전 자금 분산 가드.
- **funding rate 단위 혼동**: 거래소마다 funding 을 1h / 4h / 8h 주기로 보고하는데 표시 단위는 모두 다름. 8h 환산 안 하면 비교 의미 없음.

## 응용 (How this fits with other modules)

- `40-realtime-infra/kimp-listing-arb` 의 KR 거래소 가격 → **이 모듈** 의 글로벌 perp 가격 → 진입 결정
- `30-strategy-patterns/volume-farmer/funding_arb_trader.py` 의 entry trigger 로 funding 차이 데이터 사용
- `50-rust-acceleration/rust-services/gap-recorder` 가 본 모듈의 시그널을 SQLite WAL 로 169K rows/sec 저장

결합 사용 시: 자동 진입까지 가려면 사전 자금 분산 + funding rate 보정 + 출금 가능성 사전 체크 필요.

## 환경변수

```env
TELEGRAM_BOT_TOKEN=<...>
TELEGRAM_CHAT_ID=<...>

# (옵션) CEX API key — 자동 거래 모드 시
BINANCE_API_KEY=<...>
BINANCE_SECRET=<...>
BYBIT_API_KEY=<...>
BYBIT_SECRET=<...>
KUCOIN_API_KEY=<...>
KUCOIN_SECRET=<...>
KUCOIN_PASSPHRASE=<...>
```
