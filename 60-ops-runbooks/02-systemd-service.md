# 02 — systemd Service Pattern

VPS 재부팅 후에도 살아남아야 하는 봇 (perp-dex-bot) 은 systemd unit으로 관리합니다. tmux는 사람이 디버깅할 때 좋고, systemd는 기계가 24/7 돌릴 때 좋습니다.

## 핵심 설계 원칙 4가지

### 1. `Restart=on-failure` + `RestartSec=30`

가장 load-bearing한 설정입니다. 이 두 줄이 없으면 systemd 쓸 이유가 거의 없습니다.

- `Restart=on-failure` — exit code가 0이 아닐 때만 재시작. SIGTERM으로 의도적으로 종료하면 재시작 안 함 (수동 정지가 가능해짐)
- `RestartSec=30` — 30초 기다렸다가 재시작. 0초로 두면 거래소 API rate limit에 걸려서 무한 루프

`Restart=always`는 함정입니다. 의도적으로 봇을 멈추고 싶어도 systemd가 계속 살려서 디버깅 불가.

### 2. `Type=simple` not `Type=forking`

봇이 foreground에서 메인 프로세스로 돌면 `simple`. fork해서 데몬화하는 봇이면 `forking`. 

Python 봇은 99% `simple`입니다. `forking`은 C/C++ 데몬에서나 쓰는 옵션이고, 잘못 쓰면 systemd가 PID 추적을 놓칩니다.

### 3. `WorkingDirectory` + `EnvironmentFile`

```ini
WorkingDirectory=/opt/perp-dex-bot/multi-perp-dex
EnvironmentFile=/opt/perp-dex-bot/multi-perp-dex/.env
```

`.env` 파일이 working dir에 있어도 systemd가 자동으로 안 읽습니다. 명시적으로 `EnvironmentFile`로 지정해야 `os.environ`에 들어옵니다.

`.env` 권한은 반드시 `chmod 600`. systemd unit 파일도 `chmod 644` (root만 쓸 수 있게).

### 4. SIGKILL이 SIGTERM보다 안전합니다 (거래봇 한정)

이게 가장 직관에 반하는 부분입니다.

일반 서비스는 SIGTERM으로 graceful shutdown 하는 게 맞습니다. 그런데 거래봇은:

- SIGTERM 받으면 봇 코드의 graceful close 경로가 실행됨 → "포지션 다 닫고 종료" 트리거
- 만약 그 순간 거래소가 flash-crash 중이면, market order로 청산하다가 슬리피지로 큰 손실
- SIGKILL로 죽이면 봇은 그냥 사라지고, 포지션은 거래소에 그대로 남음
- 다음 기동시 `reconcile_positions()`이 거래소에서 현재 포지션을 읽어와서 상태 복원

즉, "프로세스를 죽이는 것"과 "포지션을 닫는 것"을 분리합니다. systemd는 프로세스만 죽이고, 포지션은 봇이 자기 로직으로만 닫습니다.

```ini
KillMode=mixed
KillSignal=SIGKILL
```

`KillMode=mixed`는 메인 프로세스에는 `SIGKILL`을, 자식 프로세스에는 `SIGTERM`을 보냅니다. 자식 (예: aiohttp worker) 은 graceful하게 끝낼 시간을 주고, 메인은 즉시 죽임.

물론 봇 코드가 idempotent해야 합니다 — 같은 주문을 두 번 실행해도 거래소에 중복 주문이 안 들어가야 함. 보통 `client_order_id`로 dedup.

## 샘플 unit 파일

`/etc/systemd/system/perp-dex-bot.service`:

```ini
[Unit]
Description=perp-dex-bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/perp-dex-bot/multi-perp-dex
EnvironmentFile=/opt/perp-dex-bot/multi-perp-dex/.env
ExecStart=/opt/perp-dex-bot/multi-perp-dex/main_venv/bin/python -m strategies.multi_runner --config config.yaml
Restart=on-failure
RestartSec=30
KillMode=mixed
KillSignal=SIGKILL
StandardOutput=append:/var/log/perp-dex/stdout.log
StandardError=append:/var/log/perp-dex/stderr.log

[Install]
WantedBy=multi-user.target
```

설치 후:

```bash
mkdir -p /var/log/perp-dex
systemctl daemon-reload
systemctl enable perp-dex-bot
systemctl start perp-dex-bot
```

## 운영 명령어

```bash
# 시작
systemctl start perp-dex-bot

# 정지 (SIGKILL이 날아감, 포지션은 안 닫힘)
systemctl stop perp-dex-bot

# 재시작
systemctl restart perp-dex-bot

# 상태 확인
systemctl status perp-dex-bot

# 라이브 로그 tail
journalctl -u perp-dex-bot -f

# 최근 1시간 로그
journalctl -u perp-dex-bot --since '1 hour ago'

# 부팅시 자동 시작 등록/해제
systemctl enable perp-dex-bot
systemctl disable perp-dex-bot
```

`StandardOutput=append:/var/log/...`로 직접 파일에 쓰게 했으니 `tail -f`로도 볼 수 있습니다. journalctl과 파일, 둘 다 남기는 이중 안전장치.

## Hot config reload (SIGHUP)

봇 코드에 SIGHUP 핸들러를 만들어 두면 config.yaml만 다시 로드 가능 (재시작 없이):

```python
import signal

def handle_sighup(signum, frame):
    logger.info("SIGHUP received, reloading config")
    config.reload()

signal.signal(signal.SIGHUP, handle_sighup)
```

운영시:

```bash
systemctl reload perp-dex-bot
```

이게 SIGHUP을 보냅니다 (unit 파일에 `ExecReload`가 정의되어 있으면 그것을 실행, 없으면 SIGHUP).

**중요**: 이건 Linux 전용입니다. Windows에는 SIGHUP이 없습니다. 이런 경우 file-based trigger pattern을 써야 합니다 — 다음 runbook (`03-windows-vs-linux.md`) 참고.

## Unit 파일 변경 후 빠뜨리지 말 것

`.service` 파일을 수정하고 그냥 `systemctl restart` 하면 옛날 설정으로 재시작됩니다. 반드시:

```bash
systemctl daemon-reload
systemctl restart perp-dex-bot
```

`daemon-reload`를 먼저. systemd는 unit 파일을 메모리에 캐시하기 때문에 명시적으로 reload 시켜야 합니다.

## 함정들

### 1. 봇이 즉시 종료되면 `RestartSec` 무시하고 무한 루프 위험

봇이 시작되자마자 (예: API key가 잘못돼서) exit하면 systemd가 30초 기다렸다가 다시 시작 → 또 즉시 exit → 30초 → ... 무한 루프.

`StartLimitInterval` + `StartLimitBurst`로 안전장치:

```ini
[Unit]
StartLimitIntervalSec=300
StartLimitBurst=5
```

5분 안에 5번 재시작 실패하면 systemd가 포기하고 멈춥니다.

### 2. `journalctl`이 너무 커집니다

기본 설정이면 `/var/log/journal/`가 무한 증가합니다. `/etc/systemd/journald.conf`:

```
SystemMaxUse=2G
MaxRetentionSec=2week
```

### 3. 환경변수 quoting

`.env` 파일에서:

```
API_KEY=abc123
```

는 OK. 하지만:

```
API_KEY="abc123"
```

은 따옴표까지 값에 포함됩니다 (systemd EnvironmentFile은 따옴표 처리 안 함). docker-compose의 `.env`와 다릅니다. 따옴표 빼고 쓰세요.

### 4. venv 경로를 ExecStart에서 직접 호출

```ini
ExecStart=/opt/perp-dex-bot/multi-perp-dex/main_venv/bin/python -m ...
```

`source venv/bin/activate` 같은 건 systemd에서 안 먹힙니다. venv 안의 `python` 실행파일을 절대 경로로 직접 부르면 venv가 자동 활성화된 상태로 실행됩니다.

## 다음 단계

- Windows 개발 환경에서 같은 봇 돌릴 때 SIGHUP, signal handling 차이 → `03-windows-vs-linux.md`
- private repo로 운영중인 봇 코드 sanitize/push 흐름 → `04-git-private-flow.md`
