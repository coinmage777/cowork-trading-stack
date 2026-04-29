# triple-lock-live

> 한 줄 요약 (One-liner): 실거래 진입을 위해 3개의 환경변수가 **모두** 만족해야만 통과시키는 fail-fast 잠금. dry-run을 깜빡한 사고를 막기 위해 만든 30줄짜리 가드.

## 의존성 (Dependencies)
- Python 3.10+
- stdlib only

## AI에게 어떻게 시켰나 (How AI built it)

처음 프롬프트 (initial prompt):
> "ENABLED=true, DRY_RUN=false, LIVE_CONFIRM=true 세 개 다 만족해야 실거래로 진입. 하나라도 빠지면 부팅 거부. 30줄 이내로."

AI가 자주 틀린 것 (Common AI mistakes for this pattern):
- "DRY_RUN이 없으면 dry-run으로 간주"가 아니라 "DRY_RUN!=false면 dry-run". 이 차이를 AI가 자주 거꾸로 짠다 (default를 live로 둠 → 사고).
- truthy 판정을 `bool(val)`로 하면 빈 문자열 외에는 다 True가 됨 — `"false"` 문자열도 truthy. `.lower() in {"1","true","yes"}`로 명시 매칭 필수.
- 한 함수에 다 욱여넣지 말고 `is_live()`(non-throwing, dict-friendly)와 `require_live()`(throwing, boot-time)를 분리해야 status 대시보드에서 재사용 가능.

## 코드 (드롭인 단위)
`triple_lock.py` — `is_live()`, `require_live()`, `status()` 세 함수. 환경변수 세 개만 본다.

## 사용 예시 (Usage)

```python
from triple_lock import require_live, is_live, status

# 부팅 시점 — fail-fast
require_live()
# RuntimeError: [triple_lock] 실거래 잠금 — DRY_RUN=true.
# 환경변수 확인: ENABLED=true, DRY_RUN=false, LIVE_CONFIRM=true

# 또는 분기로
live, reason = is_live()
if live:
    await start_real_trading()
else:
    logger.info(f"dry-run mode: {reason}")
    await start_simulation()

# trigger-watcher의 status_dumper 콜백에서
def my_status():
    return {"trade_lock": status(), ...}
```

## 실전 함정 (Battle-tested gotchas)
- `.env`에 `DRY_RUN=False` (대문자 F)로 적어두고 실거래 모드로 진입했다가 사고 — 본 코드는 `.lower()` 처리하므로 `False`/`FALSE`/`false` 모두 동일하게 받지만, **다른 라이브러리는 그렇지 않음**. 일관되게 소문자로 쓸 것.
- systemd unit 파일에 `Environment="DRY_RUN=false"` 식으로 박아두면 deploy 후에도 변경 안 됨 → 새 운영자가 `.env`를 고쳐도 부팅 시 systemd 환경변수가 우선이라 안 먹힘. unit 파일은 비워두고 `.env` 단일 진실로 관리할 것.
- `LIVE_CONFIRM`은 일부러 sudo-like 한 단계 더 둔 것. 이걸 자동화 (`LIVE_CONFIRM=true`를 systemd unit에 박아두기)하면 의미 없음. 사람이 매 배포마다 손으로 토글하라는 의도.

## 응용 예시 (Real-world usage in this repo)
- `multi-perp-dex/strategies/main.py` 부팅 직후 `require_live()` 호출.
- `trigger-watcher`의 status.out dump에 `triple_lock.status()`가 포함되어 운영자가 한 눈에 라이브/드라이런을 확인.
- Contabo 운영 절차: 새 코드 배포 후 `LIVE_CONFIRM=false`로 1시간 dry-run → 로그 검증 → `LIVE_CONFIRM=true` 토글 → 재시작.
