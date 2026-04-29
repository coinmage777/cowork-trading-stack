# volume-farmer-templates

> 한 줄 요약: 두 거래소에 같은 자산 long/short 동시 진입 → hold → close → 반복하는 cross-venue delta-neutral volume farmer. 시장 방향성 노출 = 0, 유일 비용은 fee drag, 수익 원천은 거래량 → 포인트/에어드랍 expectation 입니다.

## 의존성 (Dependencies)

- Python 3.11+
- `20-exchange-wrappers/_combined` (양쪽 venue 의 wrapper)
- `aiohttp`, `asyncio`
- `10-foundation-modules/` (telegram-notifier, kill-switch)

## AI에게 어떻게 시켰나 (How this was built with Claude)

이 모듈을 만들 때 사용한 프롬프트 패턴:

> "두 거래소에 동시에 long/short 진입 → hold → close 하는 delta-neutral volume cycling 봇을 짜 줘. 단일 패턴 반복 방지를 위해 ① 자산 random rotation (BTC/ETH/SOL/HYPE) ② size jitter ±30% ③ hold time jitter (3~10분 random) ④ direction 50/50 random 을 모두 적용하고, 4개의 kill switch (kill file, daily PnL stop, consecutive failures, min collateral) 도 반드시 포함. open/close 가 한쪽만 체결되는 상황은 emergency_close 로 처리."

AI가 자주 틀린 부분 (common AI mistakes for this code path):

- **dirty 포지션 reconcile 누락**: 봇 재시작 시 한쪽 거래소에만 포지션이 남아있으면 delta-neutral 가정이 깨짐. `reconcile()` 호출 후 dirty detect 시 즉시 emergency_close 해야 함.
- **size jitter 가 거래소 minimum 미만**: BTC 0.001 minimum 인 거래소에서 jitter 가 0.0008 로 떨어지면 주문 거부. minimum floor 가드 필요.
- **양쪽 fee 불일치 무시**: AI 는 "delta=0 이니 PnL=0 이고 비용은 fee 만" 으로 단순화하지만 양쪽 venue 의 maker/taker fee 가 다르고, 특히 funding 차이가 누적되면 hold 1시간만 넘어도 PnL 이 흔들림.
- **Kill file race condition**: kill file 생성 → 봇 다음 사이클까지 기다리는 30초 사이에 신규 진입이 시작되는 경우 있어, 매 cycle 시작 시점뿐 아니라 진입 직전에도 한 번 더 체크해야 함.

힌트: 이 모듈은 5개 farmer (rise / lighter / var-aster / ethereal-aster / xyz) 에 걸쳐 12번 이상 재작업했고, 그때마다 AI 가 놓친 것은 **dirty 포지션 reconcile, jitter floor, kill file double check, websocket reconnect backoff `[2,5,10,30,60,180,300]`** 입니다.

## 모듈 구조 (file structure with one-liner per file)

| File | Purpose |
|------|---------|
| `rise_volume_farmer.py` | Rise (RISE Chain) ↔ Hyperliquid 페어 farmer |
| `lighter_volume_farmer.py` | Lighter ↔ Hyperliquid 페어 farmer |
| `var_aster_farmer.py` | Variational ↔ Aster 페어 farmer |
| `ethereal_aster_farmer.py` | Ethereal ↔ Aster 페어 farmer |
| `xyz_volume_farmer.py` | XYZ chain volume farmer |
| `funding_arb_trader.py` | Funding rate arbitrage farmer (8h cycle, 차이 큰 방향 자동 선택) |

## 핵심 원리

```
t=0:   Venue A 에 BTC LONG $50  + Venue B 에 BTC SHORT $50  (delta=0)
t=5m:  HOLD (가격 변동에 양쪽 PnL 상쇄)
t=5m+: 양쪽 close → 거래량 $100 누적 (양쪽 leg)
t=6m:  GAP 60~180s random
t=6m+: 다음 cycle (자산 random rotation, direction random)
```

**비용**: ~0.07% (taker × 2 + 약간의 슬리피지). $1000 volume → $0.7 fee
**효익**: 포인트 시즌 활성 거래소에서 daily volume × 포인트 multiplier

## 운영 패턴 다양화 (v2)

기본 farmer 는 패턴 노출 위험 (고정 size/hold/asset/direction). v2 정교화:

| 정교화 | 환경변수 | 효과 |
|--------|---------|------|
| **Asset rotation** | `*_ASSET_POOL=BTC,ETH,SOL,HYPE` | 자산 random 선택, BTC 만 반복 X |
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
    async def run(self) -> None
    async def reconcile(self) -> bool         # 시작 시 dirty 포지션 검출
    async def open_round_trip(self) -> dict
    async def close_round_trip(self) -> dict
    async def emergency_close(self) -> None
```

## 사용 예시 (Usage)

```bash
# Rise farmer 환경변수 + 가동
export RISE_FARMER_ASSETS=BTC,ETH,SOL,HYPE
export RISE_FARMER_SIZE_USD=50
export RISE_FARMER_DAILY_CAP=99999
export RISE_FARMER_DAILY_STOP_USD=-10
export RISE_FARMER_DIRECTION_RANDOM=true

nohup python -m volume_farmer_templates.rise_volume_farmer --live \
    >> logs/rise_farmer.log 2>&1 &

# 정지: kill file 생성 (graceful close)
touch data/KILL_RISE_FARMER

# 또는 강제: kill PID
kill <PID>
```

## Funding-arb-aware mode

`funding_arb_trader.py` 는 변형: 두 거래소 funding rate 차이가 임계 이상일 때만 진입. 차이 큰 방향 선택 (음수 funding venue → long, 양수 → short).

```env
FUNDING_ARB_MIN_DIFF_8H=0.0005     # 0.05%/8h 이상 차이만 진입
FUNDING_ARB_HOLD_HOURS=8           # 한 funding cycle hold
FUNDING_ARB_EXIT_DIFF_8H=0.0001    # 0.01% 미만 시 exit
```

기대 수익 (실증): APR 0.5–3% (단순 volume cycler 는 -fee drag, funding-arb 결합 시 +EV).

## 실전 함정 (Battle-tested gotchas)

운영하면서 깨진 부분들:

- **dirty 포지션 reconcile 누락 → delta 가 살아있는 채로 cycle 진입**: 봇 재시작 시 한쪽 거래소에만 포지션이 남아있으면, 다음 cycle open 시 같은 자산을 두 번 진입하는 사고. `reconcile()` 에서 dirty detect → emergency_close 후 시작.
- **Lighter subprocess stdout 오염 → JSON parse error burst**: farmer 가 Lighter 쪽 호출 결과를 stdin/stdout 으로 받는데, 격리 venv 안 SDK 의 `print()` 가 메시지를 더럽혀 매 cycle parse error. `subprocess_wrapper.py` 첫 char `{`/`[` 가드.
- **WS reconnect backoff 가 너무 늦음**: 처음에 `[10, 30, 60, 120, 300]` 으로 두니 짧은 disconnect 에서 회복이 30 초 이상 걸려 cycle 이 몇 번씩 누락. `[2, 5, 10, 30, 60, 180, 300]` 으로 바꾸니 99% 케이스가 5초 내 회복.
- **HL builder code 인식 실패 → Miracle 포인트 0**: cloid 가 `0x4d455243` (= "MERC") prefix 로 시작해야 builder 가 인식. wrapper 에서 `_make_cloid` 가 prefix 강제하는지 확인.
- **kill file race condition**: 사용자가 kill file 만든 직후 진입 사이클이 시작 → kill 무시. 매 cycle 시작 + 진입 직전 두 번 체크.
- **size jitter 가 거래소 min 보다 작아짐**: $50 ± 30% = $35 floor 인데 BTC 의 거래소 min 이 $50 인 venue 에서 distortion. min 가드 + 자산별 별도 floor.

## 응용 (How this fits with other modules)

- `20-exchange-wrappers/_combined` (long/short 두 leg) → **이 모듈** → `60-ops-runbooks/telegram-control` (kill / 잔고 모니터)
- `40-realtime-infra/cross-venue-arb-scanner` 의 funding rate snapshot 은 funding-arb 모드의 entry trigger 로 들어옵니다.
- `30-strategy-patterns/aster-spot-buyer` 의 Aster wrapper 는 var_aster_farmer / ethereal_aster_farmer 의 hedge venue 로 동작.

결합 사용 시: 같은 wallet 으로 여러 farmer 띄우면 nonce 충돌 가능. farmer 별로 별도 EVM PK 권장.

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

## 거래소 가입 링크

- Hyperliquid: https://miracletrade.com/?ref=coinmage
- Lighter: https://app.lighter.xyz/?referral=GMYPZWQK69X4
- Aster: https://www.asterdex.com/en/referral/e70505
- Variational: https://omni.variational.io/?ref=OMNICOINMAGE
