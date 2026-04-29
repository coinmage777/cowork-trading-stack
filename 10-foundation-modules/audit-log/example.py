"""audit-log 사용 데모."""
from audit_log import AuditLogger, get_audit_logger

audit = AuditLogger(base_dir="./")

audit.log("ORDER_OPEN",
          exchange="lighter", symbol="BTC", side="long",
          margin=50.0, price=75000.0)
audit.log("ORDER_CLOSE",
          exchange="lighter", symbol="BTC", pnl_pct=1.5, reason="trailing")
audit.log("WITHDRAWAL",
          exchange="hyperliquid_2", amount=100, tx="0xabc...")

# 다른 모듈에서 같은 인스턴스 공유 — 싱글턴 헬퍼
get_audit_logger().log("CIRCUIT_BREAKER_TRIPPED", reason="daily -$150")

# 최근 7일 조회
for entry in audit.list_recent(days=7):
    print(entry["ts"], entry["event"], entry)
