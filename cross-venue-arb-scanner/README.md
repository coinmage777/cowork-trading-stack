# cross-venue-arb-scanner

다중 CEX/DEX 동시 ticker fetch → spread divergence 탐지. 신규 listing 토큰 (HYPER, 갓 TGE된 코인 등)이 venue 간 가격 발견 단계에서 13%+ gap 발생하는 순간 포착.

## 핵심 기능

- **6+ venue 동시 fetch**: ccxt async + `asyncio.gather(return_exceptions=True)` → 한 venue timeout 시 나머지 영향 X
- **VWAP 기반 gap**: top-of-book BID/ASK 외에 size_usd notional 기준 VWAP 계산 → 실제 진입 가능한 spread 측정
- **부분체결 강등**: fill < 50% 시 severity 다운그레이드 (호가 깊이 부족 = fake gap 방지)
- **3단계 임계**: info / warn / crit (예: 0.3% / 1.5% / 4%)
- **Dedup**: 같은 페어 30분 내 중복 알림 X
- **Telegram 알림**: warn/crit 즉시 푸시
- **JSONL 시그널 로그**: 사후 분석용 구조화 로그

## 파일 구조

```
cross-venue-arb-scanner/
├── hyper_listing_arb_scanner.py    — HYPER 등 신규 listing 다중 venue gap 스캐너 (메인)
├── funding_arb.py                  — Funding rate cross-venue arb (delta=0 + funding 수익)
├── spot_perp_arb.py                — Spot vs Perp basis arb (cash & carry)
├── funding_scan.py                 — 거래소별 funding rate 스냅샷
├── funding_monitor.py              — 실시간 funding 차이 모니터
├── funding_report.py               — 일일 funding 수익 리포트
├── listing_arb_scanner.py          — 일반 신규 listing 스캐너 (HYPER 외)
├── listing_arb_runner.py           — auto-trade runner (사전 자금 분산 필요)
└── basis_arb_trader.py             — Basis (perp-spot) arb 자동 실행
```

## 주요 클래스

```python
class HyperArbScanner:
    venues: List[str] = ["binance", "bybit", "bitget", "gate", "mexc", "kucoin"]
    size_usd: float = 500            # VWAP 계산 noational
    interval_sec: float = 10          # poll 주기
    info_threshold: float = 0.3       # %
    warn_threshold: float = 1.5
    crit_threshold: float = 4.0

    async def fetch_all_venues(self) -> Dict[str, OrderBook]
    def compute_pairs(self, books: Dict) -> List[Signal]  # NxN-N pairs
    async def notify(self, signal: Signal) -> None
    async def run(self, duration: int = 0) -> None  # 0=infinite
```

## 사용 예시

```bash
# 5분 1회 + Telegram 알림
python -m cross_venue_arb_scanner.hyper_listing_arb_scanner \
    --token HYPER --quote USDT \
    --duration 300 --interval 10 \
    --warn 2 --crit 5

# Daemon (Ctrl+C로 종료)
nohup python -m cross_venue_arb_scanner.hyper_listing_arb_scanner \
    --duration 0 --size-usd 1000 --interval 5 \
    >> logs/arb_scanner.log 2>&1 &

# 다른 토큰
python -m cross_venue_arb_scanner.hyper_listing_arb_scanner \
    --token NEWCOIN --quote USDT
```

출력:
```
=== 12:20:22 UTC | HYPER/USDT | size=$500 ===
VENUE      BID        ASK        BID_VWAP   ASK_VWAP   FILL_USD
binance    0.165000   0.165400   0.165000   0.165516   $500/500
kucoin     0.173500   0.173600   0.168671   0.182448   $500/194
[WARN] BUY binance @ 0.165400 -> SELL kucoin @ 0.173500 | gap=4.90% (vwap 1.91%)
```

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

## Funding rate arbitrage (별도 모드)

`funding_arb.py` — 두 venue funding rate 차이 활용:
```
A funding +0.5% (long pays short)
B funding -0.2% (short pays long)
A short + B long → 차이 +0.7%/8h 자동 수익
```

기대 APR 0.5–3% (변동성 큼).
