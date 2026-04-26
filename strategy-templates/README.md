# strategy-templates

검증된 전략 파일 모음 — pair_trader (BTC/ETH spread mean reversion), nado_pair_scalper (모멘텀 + 스프레드 + DCA + 트레일링), strategy_evolver (GA 가중치 자동 튜닝), 신호 빌딩블록 (momentum/donchian/grid).

## 핵심 전략

### 1. pair_trader.py
- BTC/ETH 비율 기반 mean reversion
- Spread z-score 진입, 수렴 시 청산
- 거래소별 동일 인터페이스 (perp-dex-wrappers 사용)
- regime_filter (Kaufman ER + ADX + RSI bounded) 통합

### 2. nado_pair_scalper.py

```yaml
leverage: 10                  # 이전 15에서 줄임 (변동성 노출 ↓)
margin_per_entry: 50
max_entries: 3                # DCA depth (이전 20에서 학습 후 축소)
momentum_threshold: 2.0       # 그룹별 multiplier 0.8x~2.0x
spread_take_profit: 0.02      # 2% margin (이전 0.4%에서 증가, R:R 정상화)
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
2. Momentum (보조): 14분 가격변화율 >= threshold → trailing stop. WR 88%

**청산 우선순위**: hard_stop → trailing → 고정TP → loss_cap → 모멘텀이탈/스프레드수렴 → DCA

**R:R 수학**:
- TP $13.5 vs SL $24 (수수료 포함) = 1:1.8
- Break-even WR 55% (이전 94%에서 정상화)

### 3. strategy_evolver.py
GA (genetic algorithm) 기반 가중치 자동 튜닝.
- 5개 시그널: momentum_diff, spread_zscore, bollinger_breakout, rsi_divergence, volatility_ratio
- population_size 20 × 3 stages × 12시간 사이클
- **가중치 하한선**: `min_signal_weights: {momentum_diff: 0.15, spread_zscore: 0.10}` — 핵심 시그널 0 방지

### 4. signals/
빌딩블록 시그널:
- `momentum.py` — N-period 가격변화율
- `donchian.py` — N-period high/low breakout
- `grid.py` — Grid trading
- `bollinger.py` — BB upper/lower
- `regime_gate.py` — Kaufman ER + ADX + RSI bounded

## 파일 구조

```
strategy-templates/
├── pair_trader.py
├── nado_pair_scalper.py
├── strategy_evolver.py
├── signals/
│   ├── momentum.py
│   ├── donchian.py
│   ├── grid.py
│   ├── bollinger.py
│   └── regime_gate.py
└── (config 예시 yaml 포함)
```

## 사용 예시

```python
import asyncio
from nado_pair_scalper import NadoPairScalper, NadoPairConfig
from perp_dex_wrappers._common.factory import create_exchange

async def main():
    ex = await create_exchange("hyperliquid", ...).init()

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

## 환경변수

전략 자체는 환경변수 X. 거래소 wrapper에서 키 주입.

## 의존성

- `pandas`, `numpy` (시그널 계산)
- `asyncio`, `aiohttp`
- perp-dex-wrappers (거래소 인터페이스)
- shared-utils (notifier, state_manager)
