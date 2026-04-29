# state-persister

> 한 줄 요약 (One-liner): JSON 파일에 상태를 atomic write로 저장하고 재시작 시 복원하는 미니 모듈. tmp 파일 + `os.replace`로 부분 손상 방지.

## 의존성 (Dependencies)
- Python 3.10+
- stdlib only

## AI에게 어떻게 시켰나 (How AI built it)

처음 프롬프트 (initial prompt):
> "봇이 죽었다 살아날 때 포지션 정보 잃으면 안 됨. JSON 파일로 저장하고 로드. 단 저장 중간에 또 죽어도 기존 파일이 깨지면 안 됨 (atomic write)."

AI가 자주 틀린 것 (Common AI mistakes for this pattern):
- `open(path, 'w')`로 바로 쓰는 코드 — 쓰는 도중에 SIGKILL 받으면 파일이 반쯤 잘린 채로 남고 다음 부팅에서 JSON 파싱 에러로 죽음. 반드시 tmp + `os.replace`.
- Windows에서 `os.rename`이 destination 존재 시 실패함 → `os.replace`만 cross-platform 안전. AI는 자주 `os.rename`을 씀.
- `default=str`을 안 넣어서 `Decimal`, `datetime`, `Path` 등이 들어오면 직렬화 실패. trader 상태에는 거의 항상 들어있음.
- 빈 상태일 때 빈 dict `{}`를 그대로 저장 → 다음 부팅 시 "이전 상태 있음"으로 오해. 비어있으면 파일을 지우는 게 깔끔함.

## 코드 (드롭인 단위)
`state_manager.py` — `StateManager` 클래스. `save(dict)`, `load() -> dict`, `clear()`, `save_all(traders)` (trader.get_state() 호출 헬퍼).

## 사용 예시 (Usage)

```python
from state_manager import StateManager

sm = StateManager("./data/trader_state.json")

# 저장 — atomic
sm.save({
    "lighter": {"exchange_name": "lighter", "side": "long", "entry": 75000.0},
    "nado":    {"exchange_name": "nado",    "side": "short", "entry": 3500.0},
})

# 부팅 시 복원
state = sm.load()
for ex_name, snap in state.items():
    trader = build_trader(ex_name)
    trader.restore(snap)

# 포지션 다 청산되면 정리
sm.clear()
```

## 실전 함정 (Battle-tested gotchas)
- 같은 파일을 여러 프로세스가 동시에 쓰면 한 쪽이 다른 쪽 결과를 덮어씀. 본 모듈은 single-writer 가정. 멀티 writer가 필요하면 `config-lock` 모듈 같이 쓰기.
- 백업: `state.json.bak`을 같이 두는 운영자가 많은데, 본 모듈은 그걸 안 만듦. cron으로 별도 backup해야 함. atomic write이므로 어떤 시점에 cp 떠도 일관된 스냅샷.
- `default=str` 때문에 `Decimal('123.45')`은 문자열 `"123.45"`로 저장됨. 로드할 때 다시 `Decimal()`로 파싱하는 책임은 호출자에게.

## 응용 예시 (Real-world usage in this repo)
- `multi-perp-dex/strategies/main.py` 종료 hook에서 `sm.save_all(traders)` 호출.
- 부팅 직후 `sm.load()`로 포지션 정보 복원 후 거래소 실제 포지션과 reconcile.
- `trigger-watcher`의 close.trigger 처리 끝에 `sm.clear()` — 청산 완료 표시.
