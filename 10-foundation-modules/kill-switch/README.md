# kill-switch

> 한 줄 요약 (One-liner): `KILL_*` 빈 파일 하나로 봇 진입을 즉시 차단/해제하는 file-based kill switch. SSH 한 줄 (`touch KILL_ALL`)로 비상정지.

## 의존성 (Dependencies)
- Python 3.10+
- stdlib only

## AI에게 어떻게 시켰나 (How AI built it)

처음 프롬프트 (initial prompt):
> "비상정지를 위한 가장 단순한 메커니즘. SSH로 들어가서 빈 파일 하나 만들면 봇이 진입 안 하게. signal/REST API/DB 다 안 됨 — 운영자 SSH 권한만 있으면 동작해야 함. 거래소별로도 막을 수 있게."

AI가 자주 틀린 것 (Common AI mistakes for this pattern):
- 파일을 매번 stat() 호출하면 IO 많아질까봐 캐싱하다가 캐시 invalidation을 잘못해서 release 후에도 막히는 버그.
- `KILL_ALL` 파일이 디렉토리로 잘못 만들어진 경우 (`mkdir KILL_ALL`) 처리 안 함 — 본 코드는 `is_file()`을 안 쓰고 `exists()`만 보지만, 거래봇 환경에선 그쪽이 외려 더 안전 (디렉토리도 차단).
- "파일 내용을 읽어서 reason까지 표시"를 시키면 매번 read를 호출하는 코드를 짬 — exists만 본다는 단순성을 깨뜨림.

## 코드 (드롭인 단위)
`kill_switch.py` — `KillSwitch` 클래스 하나. 파일 존재만 체크하므로 stdlib만 씀. 매 진입 직전 `check(exchange)` 호출.

## 사용 예시 (Usage)

```python
from kill_switch import KillSwitch

ks = KillSwitch(data_dir="/var/lib/mybot/data")

# 봇 메인 루프 안에서 — 매 신규 진입 직전
async def try_open_position(exchange, symbol):
    ok, reason = ks.check(exchange)
    if not ok:
        logger.warning(f"[KILL] {exchange} 진입 차단: {reason}")
        return
    await exchange.create_order(...)

# 외부에서 (운영자가 SSH 들어와서)
# $ touch /var/lib/mybot/data/KILL_ALL          # 전체 차단
# $ touch /var/lib/mybot/data/KILL_lighter      # lighter만 차단
# $ rm    /var/lib/mybot/data/KILL_lighter      # 해제
```

## 실전 함정 (Battle-tested gotchas)
- `data_dir`을 git repo 안에 두면 실수로 `KILL_*` 파일이 commit/push될 수 있음. `.gitignore`에 반드시 `KILL_*` 추가.
- 봇이 systemd로 재시작되면서 `KILL_*` 파일은 보존됨 (의도). 새 코드 배포 후에도 막혀있어야 안전. 해제는 명시적으로.
- Windows에서 SMB 공유 마운트 위에 두면 mtime이 옛 값으로 보일 수 있음. 로컬 디스크 권장.

## 응용 예시 (Real-world usage in this repo)
- `multi-perp-dex/strategies/main.py`의 신규 진입 분기마다 호출됩니다.
- `health-monitor`가 sustained drawdown을 감지하면 `ks.engage(exchange)`로 자동 차단합니다 (수동 release 필요).
- 콘타보 운영 노트: 새벽 3시 거래소 점검 알림이 오면 SSH로 `touch KILL_lighter` 한 줄로 막고 다음날 풉니다.
