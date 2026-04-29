"""감사 로그 — append-only 타임스탬프 이벤트 기록.

trading.log와 독립적으로 **append-only** JSONL 파일로 감사 추적.
파일은 일자별 분리 (`audit_YYYY-MM-DD.jsonl`).
스레드 안전 (threading.Lock).

사용:
  from audit_log import AuditLogger
  audit = AuditLogger()
  audit.log("ORDER_OPEN", exchange="lighter", direction="btc_long", margin=50, price=75000)
  audit.log("ORDER_CLOSE", exchange="lighter", pnl_pct=1.5, reason="trailing")
  audit.log("WITHDRAWAL", exchange="hyperliquid_2", amount=100)

  recent = audit.list_recent(days=7)  # 최근 7일 이벤트
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class AuditLogger:
    def __init__(self, base_dir: str | Path = "."):
        self.base_dir = Path(base_dir)
        self.audit_dir = self.base_dir / "audit_logs"
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _log_path(self) -> Path:
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        return self.audit_dir / f"audit_{today}.jsonl"

    def log(self, event_type: str, **fields) -> None:
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

    def list_recent(self, days: int = 1, max_entries: int = 1000) -> list:
        """최근 N일 로그 (오름차순 timestamp). 최대 max_entries개."""
        from datetime import timedelta
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
        all_entries: list[dict] = []
        for p in sorted(self.audit_dir.glob("audit_*.jsonl")):
            # 파일명 일자가 cutoff 이전이면 skip
            try:
                day_str = p.stem.replace("audit_", "")
                day = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if day < cutoff - timedelta(days=1):
                    continue
            except Exception:
                pass
            try:
                with p.open("r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            all_entries.append(json.loads(line))
                        except Exception:
                            pass
            except Exception:
                continue
        return all_entries[-max_entries:]


_DEFAULT: AuditLogger | None = None


def get_audit_logger(base_dir: str | Path = ".") -> AuditLogger:
    """싱글턴 헬퍼. 처음 호출 시 base_dir 결정, 이후 호출은 인자 무시."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = AuditLogger(base_dir=base_dir)
    return _DEFAULT
