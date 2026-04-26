"""감사 로그 — 주문/청산/출금 중요 이벤트 별도 기록.

trading.log와 독립적으로 **append-only** 파일로 감사 추적.
파일 크기 제한 (일 단위 rotation), 암호화는 선택.

사용:
  from strategies.audit_log import AuditLogger
  audit = AuditLogger()
  audit.log("ORDER_OPEN", exchange="miracle", direction="btc_long", margin=50, price=75000)
  audit.log("ORDER_CLOSE", exchange="miracle", pnl_pct=1.5, reason="trailing")
  audit.log("WITHDRAWAL", exchange="hyperliquid_2", amount=100)
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class AuditLogger:
    def __init__(self, base_dir: Path = None):
        self.base_dir = Path(base_dir or Path(__file__).resolve().parent.parent)
        self.audit_dir = self.base_dir / "audit_logs"
        self.audit_dir.mkdir(exist_ok=True)
        self._lock = threading.Lock()

    def _log_path(self) -> Path:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        return self.audit_dir / f"audit_{today}.jsonl"

    def log(self, event_type: str, **fields):
        """이벤트 기록 (append-only)."""
        entry = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "event": event_type,
            **fields,
        }
        try:
            with self._lock:
                with self._log_path().open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            logger.warning(f"[audit] log 실패: {e}")

    def list_recent(self, days: int = 1) -> list:
        """최근 N일 로그."""
        all_entries = []
        for p in sorted(self.audit_dir.glob("audit_*.jsonl")):
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        all_entries.append(json.loads(line))
                    except Exception:
                        pass
        return all_entries[-1000:]


_DEFAULT = None


def get_audit_logger() -> AuditLogger:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = AuditLogger()
    return _DEFAULT
