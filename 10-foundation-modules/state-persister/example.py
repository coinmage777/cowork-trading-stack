"""state-persister 사용 데모."""
from state_manager import StateManager

sm = StateManager(path="./trader_state.json")

# 저장 — atomic, 중간에 죽어도 기존 파일 무사
sm.save({
    "lighter": {
        "exchange_name": "lighter",
        "symbol": "BTC",
        "side": "long",
        "entry_price": 75000.0,
        "amount": 0.001,
    },
    "nado": {
        "exchange_name": "nado",
        "symbol": "ETH",
        "side": "short",
        "entry_price": 3500.0,
        "amount": 0.05,
    },
})

# 봇 재시작 후 — load
state = sm.load()
print(state["lighter"]["entry_price"])   # 75000.0

# 포지션 다 닫혔으면 클리어
sm.clear()
print("exists?", sm.exists())  # False
