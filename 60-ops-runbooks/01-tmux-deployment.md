# 01 — tmux Deployment Pattern

Contabo VPS 한 대에서 여러 봇을 동시에 돌릴 때 쓰는 패턴입니다. 가볍고, 디버깅하기 좋고, 죽었을 때 복구가 명확합니다.

## 왜 tmux인가

대안 비교:

- `nohup python main.py &` — 가장 간단하지만 로그가 한 파일에 섞이고, 실시간 attach가 안 됩니다. 봇이 멈춘 건지 멀쩡한 건지 보려면 `tail -f`만 가능. interactive 디버깅 불가.
- `screen` — tmux의 옛날 버전. 키바인딩이 더 까다롭고, 분할 화면 기능이 약합니다. 이미 tmux 쓰고 있으면 굳이 갈 이유 없음.
- `systemd` (다음 runbook 참고) — 안정 운영용. 하지만 매번 unit 파일 만들고 reload하기는 번거롭습니다. **개발/실험중인 봇은 tmux, 안정화된 봇은 systemd**.

tmux의 장점:

- **Re-attachable** — `tmux attach -t <name>` 으로 언제든 봇 콘솔에 들어가서 입력/출력 직접 볼 수 있음
- **Named sessions** — 봇별로 이름이 붙어 있어서 `tmux ls`로 한눈에 파악
- **Clean kill** — `tmux kill-session -t <name>` 한 줄로 끝
- **Detach-safe** — SSH 끊어져도 봇은 계속 돌아감 (`Ctrl+b d`로 명시적 detach)

## 네이밍 컨벤션

세션 이름 = `<bot-purpose>` 형식. 짧고 의미 있게.

실제 운영중인 예시:

```
tmux ls
standxmm: 1 windows (created Mon Apr 14 03:22:11 2026)
standxmm2: 1 windows (created Tue Apr 15 09:41:33 2026)
trader-a: 1 windows (created Wed Apr 16 12:08:55 2026)
mpdex: 1 windows (created Thu Apr 17 18:30:01 2026)
```

`standxmm`은 standard XMM 전략 1번, `standxmm2`는 같은 전략 다른 계정. 같은 봇 여러 인스턴스를 돌릴 때는 숫자만 붙입니다.

피해야 할 이름: `bot1`, `test`, `aaa`. 1주일 지나면 뭐가 뭔지 모릅니다.

## Standard launch 패턴

```bash
tmux new-session -d -s <name> -c /opt/<bot-dir> 'venv/bin/python main.py 2>&1 | tee -a logs.txt'
```

각 옵션 의미:

- `-d` — detached 상태로 시작. 세션이 만들어지지만 attach는 안 함. 스크립트로 일괄 기동할 때 필수
- `-s <name>` — 세션 이름
- `-c /opt/<bot-dir>` — working directory. venv 경로 활성화 안 해도 venv 안의 python을 직접 호출하면 자동으로 그 venv 환경 사용
- `2>&1` — stderr를 stdout으로 합침
- `tee -a logs.txt` — append 모드로 파일에 기록 + 화면에도 출력 (attach 했을 때 실시간으로 보임)

`tee -a`가 핵심입니다. detach하고 다시 attach해도 그 사이 로그가 `logs.txt`에 남아 있습니다. 봇 재시작해도 같은 파일에 누적됩니다.

## Attach / Detach 워크플로우

```bash
# 진입
tmux attach -t standxmm

# 빠져나오기 (봇은 계속 돌아감)
Ctrl+b 누른 다음 d

# 강제 종료 (봇도 같이 죽음)
tmux kill-session -t standxmm

# 모든 세션 보기
tmux ls
```

attach 한 상태에서 봇 출력에 입력을 넣을 수도 있습니다 (interactive 봇이라면). 일반적으로는 그냥 모니터링용.

## Status snapshot 패턴

attach 없이 봇 상태를 확인하고 싶을 때 — 봇이 직접 `status.txt`를 60초마다 덮어쓰게 합니다.

봇 코드 내부:

```python
def write_status_snapshot():
    with open('status.txt', 'w') as f:
        f.write(f"timestamp: {datetime.utcnow().isoformat()}\n")
        f.write(f"open_positions: {len(positions)}\n")
        f.write(f"pnl_24h: {pnl_24h:.2f}\n")
        f.write(f"last_order_id: {last_order_id}\n")
```

운영시:

```bash
cat /opt/perp-dex-bot/status.txt
```

attach 없이도 한눈에 상태 파악. 모니터링 셸 스크립트도 이걸 파싱하면 됩니다.

## 가장 많이 만나는 함정

### 1. VPS 재부팅하면 tmux 세션 다 날아갑니다

tmux는 그냥 user-level 프로세스입니다. systemd가 아닙니다. VPS가 패치 재부팅이라도 한 번 하면 모든 세션이 사라집니다.

**해결**:

- 진짜 24/7 봇은 systemd로 (`02-systemd-service.md` 참고)
- tmux 봇은 `crontab @reboot`로 자동 기동 등록 (어색한 절충안)

```cron
@reboot tmux new-session -d -s standxmm -c /opt/standxmm 'venv/bin/python main.py 2>&1 | tee -a logs.txt'
```

### 2. Korean Windows에서 SSH 접속하면 한글이 깨집니다

증상: 봇이 한글 로그를 출력하는데 attach하면 `???`로 보임.

원인: SSH 클라이언트의 LANG이 cp949로 잡혀서 tmux도 cp949 모드로 들어갑니다.

해결:

```bash
LANG=en_US.UTF-8 ssh root@your-contabo-ip
```

또는 Windows Terminal 프로파일 설정에서 `LANG=en_US.UTF-8` 환경 변수 추가.

서버 측 `~/.bashrc`에도 한 줄:

```bash
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8
```

### 3. 같은 이름 세션을 두 번 만들면 에러

```
duplicate session: standxmm
```

기존 세션이 살아 있으면 새로 못 만듭니다. 의도한 거면 먼저 kill:

```bash
tmux kill-session -t standxmm 2>/dev/null
tmux new-session -d -s standxmm -c /opt/standxmm '...'
```

스크립트로 봇 재시작할 때 자주 빠뜨리는 부분.

### 4. tee 없이 시작했다가 detach하면 그 사이 로그가 사라집니다

`tmux attach`로 보고 있을 때만 화면에 보이는 로그는 detach하는 순간 메모리에서 휘발됩니다 (tmux scrollback buffer 한도 안에서만 보존). `tee -a logs.txt`를 처음부터 붙이세요.

## 다음 단계

- 봇이 죽었다 살아나야 한다면 → `02-systemd-service.md`
- Windows 개발기에서 같은 봇 돌릴 때 SIGHUP 같은 게 안 먹힌다면 → `03-windows-vs-linux.md`
