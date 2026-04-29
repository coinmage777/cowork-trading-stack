"""triple-lock-live 사용 데모."""
import os
from triple_lock import is_live, require_live, status

# 1) 부팅 시점 — 셋 다 만족해야 진행
os.environ["ENABLED"] = "true"
os.environ["DRY_RUN"] = "false"
os.environ["LIVE_CONFIRM"] = "true"
require_live()  # 통과

# 2) 한 개 빠지면 RuntimeError
os.environ["LIVE_CONFIRM"] = "false"
live, reason = is_live()
print(f"live={live} reason={reason}")  # live=False reason=LIVE_CONFIRM!=true

# 3) 대시보드용 상태 dump
print(status())
