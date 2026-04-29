# strategy-templates

> 한 줄 요약: 검증된 전략 코드 모음. `pair_trader` (BTC/ETH spread mean reversion), `nado_pair_scalper` (모멘텀 + 스프레드 + DCA + 트레일링), `strategy_evolver` (GA 가중치 자동 튜닝), 그리고 빌딩블록 시그널 (momentum/donchian/grid/bollinger/regime_gate) 을 한 곳에 모아 두었습니다.

## 의존성 (Dependencies)

- `pandas`, `numpy` (시그널 계산)
- `asyncio`, `aiohttp`
- `20-exchange-wrappers/_combined` (거래소 인터페이스)
- `10-foundation-modules/` (telegram-notifier, state-persister)

## AI에게 어떻게 시켰나 (How this was built with Claude)

이 모듈을 만들 때 사용한 프롬프트 패턴:

> "BTC/ETH 비율 기반 mean reversion 전략을 짜 줘. perp-dex-wrappers 의 `create_exchange` 를 받아서 양쪽 자산에 동시에 진입 → spread z-score 가 임계 이상이면 진입, 0 근처로 수렴하면 청산. DCA depth 는 config 로, hard stop / trailing stop / 고정 TP 세 가지 청산 경로를 우선순위로 처리. R:R 수학은 **margin 기준이 아니라 notional 기준** 으로 짜 줘 (TP 0.4% / SL 3% 같은 비대칭 조합은 break-even WR 이 90% 가 넘어가서 실전 불가)."

AI가 자주 틀린 부분 (common AI mistakes for this code path):

- **PnL 계산을 margin 기준으로 함**: nado_pair_scalper 초기 버전이 `pnl_percent = (price_now - entry) / margin` 으로 짜여 있어서 R:R 수학이 깨졌음. notional 기준으로 (`/ entry_price`) 바꿔야 했고, TP 0.4% / SL 3% 같은 조합은 break-even WR 94% → 그래서 TP 2% / SL 2.5% 로 정상화.
- **regime filter 부재**: 추세장에서 mean reversion 진입 → 한쪽으로만 끌려가 큰 손실. Kaufman ER + ADX + RSI bounded 게이트 추가.
- **DCA 깊이 과다**: AI 는 "더 떨어지면 더 사면 됨" 으로 max_entries 20+ 짜리를 권장하지만, 실전에서는 4번째 진입에서 hard stop 에 묶여 망함. 3 으로 줄여야 함.

힌트: 이 모듈은 nado_pair_scalper 만 8번 이상 재작업했고, 그때마다 AI 가 놓친 것은 **R:R 수학 (margin vs notional), regime filter 누락, DCA 깊이, momentum threshold 의 그룹별 multiplier (0.8x~2.0x)** 입니다.

## 모듈 구조 (file structure with one-liner per file)

| File | Purpose |
|------|---------|
| `pair_trader.py` | BTC/ETH spread z-score mean reversion (regime_filter 통합) |
| `nado_pair_scalper.py` | 모멘텀 + 스프레드 + DCA + 트레일링 복합 전략 |
| `strategy_evolver.py` | GA 기반 5-시그널 가중치 자동 튜닝 (population 20 × 3 stages × 12h cycle) |
| `signals/momentum.py` | N-period 가격변화율 |
| `signals/donchian.py` | N-period high/low breakout |
| `signals/grid.py` | Grid trading 시그널 |
| `signals/bollinger.py` | BB upper/lower |
| `signals/regime_gate.py` | Kaufman ER + ADX + RSI bounded 게이트 |

## 핵심 전략

### 1. pair_trader.py
- BTC/ETH 비율 기반 mean reversion
- Spread z-score 진입, 수렴 시 청산
- 거래소별 동일 인터페이스 (`20-exchange-wrappers` 사용)
- regime_filter (Kaufman ER + ADX + RSI bounded) 통합

### 2. nado_pair_scalper.py

```yaml
leverage: 10                  # 이전 15에서 줄임 (변동성 노출 ↓)
margin_per_entry: 50
max_entries: 3                # DCA depth (이전 20 에서 학습 후 축소)
momentum_threshold: 2.0       # 그룹별 multiplier 0.8x~2.0x
spread_take_profit: 0.02      # 2% margin (이전 0.4% 에서 증가, R:R 정상화)
stop_loss_percent: 2.5        # hard stop
trailing_activation: 1.5      # %
trailing_callback: 1.0
trailing_tighten_above: 3.0
trailing_tighten_callback: 0.5
spread_zscore_entry: 1.5
spread_ma_period: 60
no_entry_hours: [1, 7, 9]     # UTC (KST 10/16/18시 차단)
```

**진입 경로**:
1. Spread z-score (주력): `|z| >= 1.5` 진입 → `|z| < 0.3` 수렴 청산. WR 74–84%
2. Momentum (보조): 14분 가격변화율 ≥ threshold → trailing stop. WR 88%

**청산 우선순위**: hard_stop → trailing → 고정TP → loss_cap → 모멘텀이탈/스프레드수렴 → DCA

**R:R 수학**:
- TP : SL = 1:1.8 (수수료 포함)
- Break-even WR 55% (이전 94% 에서 정상화)

### 3. strategy_evolver.py
GA (genetic algorithm) 기반 가중치 자동 튜닝.
- 5개 시그널: momentum_diff, spread_zscore, bollinger_breakout, rsi_divergence, volatility_ratio
- population_size 20 × 3 stages × 12시간 사이클
- **가중치 하한선**: `min_signal_weights: {momentum_diff: 0.15, spread_zscore: 0.10}` — 핵심 시그널이 0 으로 사라지지 않도록.

## 사용 예시 (Usage)

```python
import asyncio
from nado_pair_scalper import NadoPairScalper, NadoPairConfig
from perp_dex_wrappers._common.factory import create_exchange

async def main():
    ex = await create_exchange("hyperliquid", wallet="...", agent_key="...").init()

    cfg = NadoPairConfig(
        leverage=10,
        margin_per_entry=50,
        max_entries=3,
        momentum_threshold=2.0,
        spread_take_profit=0.02,
        stop_loss_percent=2.5,
        trailing_activation=1.5,
        trailing_callback=1.0,
        no_entry_hours=[1, 7, 9],
        # regime_filter
        regime_er_min=0.45,
        regime_adx_min=30.0,
        regime_rsi_min=40.0,
        regime_rsi_max=80.0,
        regime_filter_shadow_only=True,
    )

    scalper = NadoPairScalper(exchange=ex, config=cfg, exchange_name="HL")
    await scalper.run()

asyncio.run(main())
```

## 실전 함정 (Battle-tested gotchas)

운영하면서 깨진 부분들:

- **R:R 수학 - margin vs notional**: nado_pair_scalper 의 `pnl_percent` 를 margin 기준으로 계산하니 TP 0.4% / SL 3% 조합의 break-even WR 이 94%. 실전에서 절대 못 만족하는 숫자. notional 기준으로 변환하고 TP 2% / SL 2.5% 로 정상화 → break-even 55%.
- **DCA 깊이 폭주**: max_entries 20 부근에서 최악의 drawdown 누적. 3 으로 축소 후 안정.
- **regime filter 부재**: 강한 추세장 (BTC 방향성 강할 때) 에 mean reversion 진입 → 큰 손실. `signals/regime_gate.py` 의 ER >= 0.45 + ADX >= 30 + RSI bounded 40~80 게이트 적용 후 손실 빈도 ↓.
- **no_entry_hours**: 한국 시간 새벽 / 점심 / 저녁 출퇴근 시간대 (UTC 1, 7, 9 시) 에 진입 빈도가 높지만 WR 가 떨어지는 경향이 있어 차단.
- **strategy_evolver 가 핵심 시그널 weight 를 0 으로 만듦**: GA 가 12h 안에 spread_zscore weight 를 0.0 으로 깎아버린 케이스 발생. 백테스트 score 기반으로 그렇게 갔지만 실전 신뢰성을 잃음. `min_signal_weights` 하한선 추가.

## 응용 (How this fits with other modules)

- `20-exchange-wrappers/_combined` → **이 모듈** (전략 entry/exit) → `60-ops-runbooks/telegram-control` (실시간 제어)
- 시그널 빌딩블록 (`signals/*.py`) 은 `30-strategy-patterns/backtest-templates` 백테스트에도 그대로 import 가능
- `strategy_evolver.py` 결과는 nado_pair_scalper 의 `weights.yaml` 로 hot reload 됨 (12h 주기)

결합 사용 시: 전략 인스턴스 1개당 거래소 wrapper 1개를 주입하므로, 같은 전략을 여러 venue 에서 동시에 돌리려면 multi_runner 로 띄우면 됩니다.

## 환경변수

전략 자체는 환경변수 X. 거래소 wrapper 에서 키 주입.

## 거래소 가입 링크

- Hyperliquid: https://miracletrade.com/?ref=coinmage
- Lighter: https://app.lighter.xyz/?referral=GMYPZWQK69X4
- Nado: https://app.nado.xyz?join=NX9LLaL
