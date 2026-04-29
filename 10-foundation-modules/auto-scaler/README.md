# auto-scaler

> 한 줄 요약: 봇의 거래소별 자본 배분을 자동으로 조정하는 capital sizing 모듈. 잔고 기준 정적 (`auto_scaler`) + 성과 기반 동적 (`dynamic_scaler`) 두 종류.

## 의존성

- Python 3.10+
- stdlib only (calc 로직)
- equity tracker JSON 입력 (외부 의존 — `10-foundation-modules/state-persister/` 와 연동)

## AI에게 어떻게 시켰나

처음 프롬프트:

> "여러 거래소에서 같은 전략을 돌릴 때 거래소별 자본을 어떻게 분배할지 결정하는 sizing 모듈. 두 모드: 정적 (잔고 비례) + 동적 (최근 N일 PnL 기반 가중). 각 거래소별 max_size 와 min_size cap 도 강제. 외부 의존성 없이 dict in / dict out 형태로."

AI 가 자주 틀린 부분:
- **min_size cap 미적용**: 잔고가 작은 거래소에 과소 배분 → 거래 자체가 불가능. floor 강제 필요
- **PnL 기반 가중 시 winner-take-all**: 한 거래소가 이긴다고 100% 몰빵 → drawdown 시 폭망. softmax / 분산 floor 적용

## 모듈

| File | 모드 |
|------|------|
| `auto_scaler.py` | 잔고 비례 (정적) — 단순, 안정 |
| `dynamic_scaler.py` | 최근 N일 PnL 기반 가중 (동적) — 성과 따라 capital 이동 |

## 사용 예시

```python
from auto_scaler import compute_allocations

balances = {"hl": 5000, "lighter": 3000, "aster": 2000}
total_size = 1000  # 분배할 자본

allocations = compute_allocations(
    balances,
    total_size=total_size,
    min_size=50,      # 거래소별 최소
    max_size=500,     # 거래소별 최대
)
print(allocations)  # {"hl": 500, "lighter": 300, "aster": 200}
```

## 실전 함정

- **자본 변동 잦은 venue 의 oscillation**: 매 사이클마다 재배분하면 거래소 간 자본이 왔다갔다 → 이체 비용 누적. 최소 변경 임계 (예: ±10%) 도입
- **동적 가중치 lag**: 최근 7일 PnL 기준이면 추세 반전 후 7일간 계속 패자에 몰빵. 최근 PnL × 모멘텀 신호 결합 권장

## 응용

- `30-strategy-patterns/_combined/strategy_evolver.py` 와 결합 시 자동 튜닝 + 자동 배분
- `60-ops-runbooks/telegram-control/` 에서 `/balance` 명령으로 결과 확인
