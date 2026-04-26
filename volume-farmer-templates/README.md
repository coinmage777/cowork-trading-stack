# volume-farmer-templates

Cross-venue delta-neutral volume farmer 템플릿. 두 거래소에 같은 자산 long/short 동시 진입 → hold → close → 반복. **시장 방향성 노출 0 (delta=0), 유일 비용은 fee drag**, 수익 원천은 거래량 → 포인트/에어드랍 expectation.

## 핵심 원리

```
t=0:   Venue A에 BTC LONG $50  + Venue B에 BTC SHORT $50  (delta=0)
t=5m:  HOLD (가격 변동에 양쪽 PnL 상쇄)
t=5m+: 양쪽 close → 거래량 $100 누적 (양쪽 leg)
t=6m:  GAP 60~180s random
t=6m+: 다음 cycle (자산 random rotation, direction random)
```

**비용**: ~0.07% (taker × 2 + 약간의 슬리피지). $1000 volume → $0.7 fee
**효익**: 포인트 시즌 활성 거래소에서 daily volume × 포인트 multiplier

## 시빌 회피 정교화 (v2)

기본 farmer는 패턴 노출 위험 (고정 size/hold/asset/direction). v2 정교화:

| 정교화 | 환경변수 | 효과 |
|--------|---------|------|
| **Asset rotation** | `*_ASSET_POOL=BTC,ETH,SOL,HYPE` | 자산 random 선택, BTC만 반복 X |
| **Size jitter** | `*_SIZE_JITTER_PCT=0.30` | $50 ± 30% (= $35–65) random |
| **Hold jitter** | `*_HOLD_MIN=180`, `*_HOLD_MAX=600` | 3–10분 random |
| **Cycle gap** | `*_CYCLE_GAP_MIN=60`, `*_CYCLE_GAP_MAX=180` | 다음 cycle 전 1–3분 random |
| **Direction random** | `*_DIRECTION_RANDOM=true` | long+short 또는 short+long 50/50 |
| **Timing micro-jitter** | (자동) | 진입 시 50–500ms random sleep |

## 4 Kill Switches (모든 farmer 공통)

1. **Kill file**: `data/KILL_{FARMER}` 파일 존재 시 즉시 graceful close + 종료
2. **Daily PnL stop**: 누적 손실 > `daily_stop_usd` 임계 → 종료
3. **Consecutive failures**: 5회 연속 OPEN/CLOSE 실패 → 종료
4. **Min collateral floor**: 잔고 < `min_collateral` → 진입 차단

## 파일 구조

```
volume-farmer-templates/
├── rise_volume_farmer.py        — Rise (RISE Chain) ↔ [Hyperliquid](https://miracletrade.com/?ref=coinmage) 페어
├── lighter_volume_farmer.py     — Lighter ↔ [Hyperliquid](https://miracletrade.com/?ref=coinmage) 페어
├── var_aster_farmer.py          — [Variational](https://omni.variational.io/?ref=OMNICOINMAGE) ↔ [Aster](https://www.asterdex.com/en/referral/e70505) 페어
├── ethereal_aster_farmer.py     — Ethereal ↔ [Aster](https://www.asterdex.com/en/referral/e70505) 페어
├── xyz_volume_farmer.py         — XYZ chain volume farmer
└── funding_arb_trader.py        — Funding rate arbitrage farmer (8h cycle)
```

## 주요 클래스

```python
@dataclass
class FarmerConfig:
    assets: List[str]                # ["BTC", "ETH", "SOL", "HYPE"]
    position_size_usd: float = 50
    leverage: int = 5
    hold_min_seconds: int = 180
    hold_max_seconds: int = 600
    cycle_gap_min: float = 60
    cycle_gap_max: float = 180
    daily_cap: int = 20              # round-trips/day (99999 = 무제한)
    daily_stop_usd: float = -5.0
    min_collateral: float = 100
    kill_file: str = "data/KILL_FARMER"
    asset_rotation: bool = True
    direction_random: bool = False
    size_jitter_pct: float = 0.30
    hold_jitter_pct: float = 0.20

class VolumeFarmer:
    async def run(self) -> None       # 메인 루프
    async def reconcile(self) -> bool  # 시작 시 dirty 포지션 검출
    async def open_round_trip(self) -> dict
    async def close_round_trip(self) -> dict
    async def emergency_close(self) -> None
```

## 사용 예시

```bash
# Rise farmer 환경변수 + 가동
export RISE_FARMER_ASSETS=BTC,ETH,SOL,HYPE
export RISE_FARMER_SIZE_USD=50
export RISE_FARMER_DAILY_CAP=99999  # no cap
export RISE_FARMER_DAILY_STOP_USD=-10
export RISE_FARMER_DIRECTION_RANDOM=true

nohup python -m volume_farmer_templates.rise_volume_farmer --live \
    >> logs/rise_farmer.log 2>&1 &

# 정지: kill file 생성 (graceful close)
touch data/KILL_RISE_FARMER

# 또는 강제: kill PID
kill <PID>
```

## 환경변수 (Rise farmer 예시)

```env
# 거래소 키
MAIN_WALLET_PK=<PRIVATE_KEY_FOR_RISE>
DREAMCASH_WALLET_PK=<PRIVATE_KEY_FOR_HL>

# Farmer 설정
RISE_FARMER_ASSETS=BTC,ETH,SOL,HYPE
RISE_FARMER_SIZE_USD=50
RISE_FARMER_LEVERAGE=10
RISE_FARMER_HOLD_MIN=180
RISE_FARMER_HOLD_MAX=600
RISE_FARMER_CYCLE_GAP_MIN=60
RISE_FARMER_CYCLE_GAP_MAX=180
RISE_FARMER_DAILY_CAP=99999
RISE_FARMER_DAILY_STOP_USD=-10
RISE_FARMER_MIN_COLLATERAL=50
RISE_FARMER_DIRECTION_RANDOM=true
RISE_FARMER_SIZE_JITTER_PCT=0.30
RISE_FARMER_HOLD_JITTER_PCT=0.20
RISE_FARMER_KILL_FILE=data/KILL_RISE_FARMER
RISE_FARMER_MAX_SPREAD_PCT=0.005  # 0.5% 게이트
```

## Funding-arb-aware mode

`funding_arb_trader.py`는 변형: 두 거래소 funding rate 차이가 임계 이상일 때만 진입. 차이 큰 방향 선택 (음수 funding venue → long, 양수 → short).

```env
FUNDING_ARB_MIN_DIFF_8H=0.0005     # 0.05%/8h 이상 차이만 진입
FUNDING_ARB_HOLD_HOURS=8           # 한 funding cycle hold
FUNDING_ARB_EXIT_DIFF_8H=0.0001    # 0.01% 미만 시 exit
```

기대 수익 (실증): APR 0.5–3% (단순 wash farmer는 -fee drag, funding-arb 결합 시 +EV)
