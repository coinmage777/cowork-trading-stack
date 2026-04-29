# audit-log

> 한 줄 요약 (One-liner): trading.log와 분리된 **append-only** JSONL 감사 로그. 일자별 파일 (`audit_YYYY-MM-DD.jsonl`). 주문/청산/출금 등 중요 이벤트만 별도 추적.

## 의존성 (Dependencies)
- Python 3.10+
- stdlib only

## AI에게 어떻게 시켰나 (How AI built it)

처음 프롬프트 (initial prompt):
> "trading.log는 디버그 라인 너무 많아서 사고 났을 때 추적이 어려움. 중요 이벤트(주문/청산/출금/CB)만 별도 JSONL로 append-only로 남기고 싶음. 일자별 파일 자동 분리. 스레드 안전."

AI가 자주 틀린 것 (Common AI mistakes for this pattern):
- 한 파일에 통째로 누적 → 1년 운영하면 GB 단위. 일자별 파일 분리가 운영상 필수.
- `logging` 모듈을 그대로 쓰려는 시도 — formatter / handler 설정이 trading.log와 섞여서 분리 의도가 깨짐. 별도 file write가 명확.
- Lock 안 잡고 멀티스레드에서 append → JSON 한 줄이 두 줄이 섞여서 파싱 불가능한 corrupted line. `threading.Lock` 필수.
- `default=str` 빼먹어서 `Decimal('123.45')`나 `datetime`이 들어오면 직렬화 실패하고 라인 누락.
- 파일 핸들을 객체에 들고 있다가 fd leak — 매번 `open()/close()` 가 더 안전. append는 빠름 (O_APPEND atomic on POSIX).

## 코드 (드롭인 단위)
`audit_log.py` — `AuditLogger` 클래스 + `get_audit_logger()` 싱글턴 헬퍼. 일자별 파일 자동 분리, threading.Lock 보호, JSONL 포맷.

## 사용 예시 (Usage)

```python
from audit_log import AuditLogger

audit = AuditLogger(base_dir="/var/lib/mybot/")

# 주문 라이프사이클
audit.log("ORDER_OPEN",
          exchange="lighter", symbol="BTC", side="long",
          margin=50.0, leverage=3, price=75000.0)
audit.log("ORDER_CLOSE",
          exchange="lighter", symbol="BTC",
          pnl_usd=12.5, pnl_pct=1.5, reason="trailing_stop")

# 위험 이벤트
audit.log("CIRCUIT_BREAKER_TRIPPED", reason="daily_pnl=-150")
audit.log("WITHDRAWAL", exchange="hl_main", amount=100, tx="0xabc...")

# 사후 추적
for entry in audit.list_recent(days=7):
    print(entry["ts"], entry["event"])
```

## 실전 함정 (Battle-tested gotchas)
- JSONL 파일이 1MB만 넘어도 `list_recent`에서 다 메모리에 올림. 운영 1년 넘으면 별도 cron으로 오래된 파일 압축/이관 필요.
- UTC 자정 직후 첫 이벤트가 새 파일에 쓰이는데, 같은 이벤트가 directly 이전 파일에 남아있어야 한다고 가정하면 사고. 파일 경계는 UTC date 기준이라는 점을 인지.
- `default=str`로 인해 모든 비표준 객체가 문자열로 저장됨. 사후 분석 시 `Decimal` 타입 복원 책임은 reader에게.

## 응용 예시 (Real-world usage in this repo)
- `multi-perp-dex/strategies/main.py`의 모든 `create_order` / `close_position` 직후 호출.
- `health-monitor`가 `kill_switch.engage()`를 부를 때 audit에도 기록.
- 분기 정산 / 회계 정리 시 `list_recent(days=90)`으로 통째로 떠서 PnL 검증에 사용.
