# 03 — Windows vs Linux

개발은 Windows에서 (workstation이 거기 있으니까), 운영은 Linux Contabo VPS에서. 이 둘은 비슷해 보이지만 봇 운영 관점에서 자잘하게 다릅니다. 미리 알면 디버깅 시간이 절반으로 줄어듭니다.

## 가장 큰 차이: SIGHUP이 Windows에 없습니다

Linux 운영 환경에서는 `systemctl reload <bot>` 한 줄로 hot config reload가 됩니다. Windows에는 SIGHUP signal 자체가 존재하지 않습니다 (`signal.SIGHUP`을 import만 해도 AttributeError).

같은 봇 코드를 양쪽에서 돌리려면 signal에 의존하지 않는 trigger 메커니즘이 필요합니다. 그래서 file-based trigger 패턴을 씁니다 — `10-foundation-modules/trigger-watcher/` 참고.

### File-based trigger 패턴

봇이 1초마다 `triggers/` 디렉토리를 polling합니다:

```
triggers/
  restart.trigger    # 만들면 봇이 graceful 재시작
  reload.trigger     # 만들면 config 다시 로드 (재시작 없이)
  close.trigger      # 만들면 모든 포지션 정리하고 종료
```

운영자는:

```bash
# Linux
touch /opt/perp-dex-bot/triggers/reload.trigger

# Windows (PowerShell)
New-Item -ItemType File /opt/perp-dex-bot/triggers/reload.trigger -Force
```

봇이 trigger 파일을 감지하면 자기가 지운 다음 해당 동작 수행:

```python
TRIGGER_DIR = Path("triggers")

def check_triggers():
    if (TRIGGER_DIR / "reload.trigger").exists():
        (TRIGGER_DIR / "reload.trigger").unlink()
        config.reload()
    if (TRIGGER_DIR / "restart.trigger").exists():
        (TRIGGER_DIR / "restart.trigger").unlink()
        graceful_restart()
    if (TRIGGER_DIR / "close.trigger").exists():
        (TRIGGER_DIR / "close.trigger").unlink()
        close_all_and_exit()
```

장점:

- 양 OS에서 동일하게 동작
- 원격 SSH로도 `touch` 한 줄
- Telegram 봇 같은 것에서 그냥 `Path.touch()` 호출하면 됨 (signal 처리 안 해도 됨)

단점:

- 1초 polling이라 즉발성은 SIGHUP보다 떨어짐 (실용상 충분)
- trigger 파일이 안 지워지면 무한 트리거 (`unlink()`를 잊지 말 것)

## Path separator hell

Windows: `C:\Users\<user>\bot\config.yaml`  
Linux: `/opt/bot/config.yaml`

Python에서 path를 다룰 때 가장 흔한 실수:

### 안티패턴

```python
log_path = f"{base_dir}\\logs\\{date}.txt"  # Windows에서만 동작
log_path = base_dir + "/" + "logs/" + date + ".txt"  # 양쪽에서 동작은 하지만 fragile
```

`\\`는 Windows에서만 동작 (Linux에서는 backslash가 그냥 파일명 일부). 또한 `\`는 regex 메타문자라서 path를 정규식에 넣으면 escape 지옥.

### 올바른 패턴

```python
from pathlib import Path

log_path = Path(base_dir) / "logs" / f"{date}.txt"
```

`pathlib.Path`는 OS에 맞게 알아서 separator 처리합니다. `/` 연산자가 path join이라 직관적이고, 문자열로 변환할 때만 OS-specific separator로 나옵니다.

### Forward-slash trick

사실 Windows의 Python (그리고 거의 모든 Windows API) 은 forward slash도 받아들입니다:

```python
path = "C:/Users/<user>/bot/config.yaml"  # Windows에서도 동작
open(path)  # OK
```

그래서 코드에 절대 경로를 하드코딩해야 한다면 `/`를 쓰는 게 양 OS에서 안전합니다. backslash escape 문제도 없어집니다.

## PYTHONIOENCODING — Korean Windows 함정

Korean Windows의 `cmd.exe`/`powershell` 기본 인코딩은 cp949 (또는 ms949). Python이 stdout에 한글이나 이모지를 print하려고 하면:

```
UnicodeEncodeError: 'cp949' codec can't encode character '\U0001f600'
```

봇이 print 한 줄 때문에 죽는 황당한 상황. 해결:

### 영구 설정 (권장)

PowerShell에서 한 번:

```powershell
setx PYTHONIOENCODING utf-8
```

새 콘솔 세션부터 적용됩니다 (현재 세션은 영향 없음). 시스템 환경변수로 박힙니다.

### 임시 (스크립트 안에서)

```python
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
```

main.py 맨 위에. Python 3.7+ 필요.

### Linux는 보통 신경 안 써도 됨

Linux는 보통 `LANG=en_US.UTF-8` 또는 `LANG=C.UTF-8`이 기본이라 자연스럽게 utf-8. 컨테이너에서는 가끔 `LANG=POSIX`로 떨어져서 같은 에러가 나기도 하는데, 그때는 Dockerfile에:

```dockerfile
ENV LANG=C.UTF-8
ENV PYTHONIOENCODING=utf-8
```

## WSL2 vs Native Python 선택

Windows에서 Python 봇 개발할 때 두 가지 길:

### Native Windows Python

- 설치 간단 (python.org installer)
- 빠름 (filesystem이 native NTFS)
- VSCode/PyCharm 디버거가 그냥 동작
- 단점: SIGUSR1, SIGHUP 같은 Unix signal 안 됨, fork 안 됨, 권한 비트 (chmod 600) 의미 없음

### WSL2 (Ubuntu)

- prod와 거의 동일한 환경 (Linux)
- signal, fork 다 됨
- 단점: filesystem 성격이 미묘 — Windows 쪽 (`/mnt/c/...`) 파일 access는 매우 느림. WSL의 `~`(`/home/<user>/`)에 두면 빠름.
- file watching (inotify) 은 WSL 내부에서는 동작, `/mnt/c/`에서는 동작 안 함
- VSCode Remote-WSL extension으로 비교적 매끄럽게 개발 가능

### 권장

- **Trigger watcher 같은 file watching 코드 테스트** → WSL2 (inotify가 native하게 동작)
- **빠른 iteration / 디버거 break point** → Native Python
- **prod에 가까운 환경 검증** → WSL2 또는 Contabo에 staging 인스턴스

## "Dev on Windows, Prod on Linux Contabo" 흐름

실제 워크플로우:

1. Windows에서 코드 작성 (Cursor / VSCode + Claude Code)
2. Windows에서 unit test + 작은 dry-run
3. private GitHub repo에 push (`04-git-private-flow.md` 참고)
4. Contabo VPS에서 `git pull`
5. Contabo에서 venv 활성화하고 다시 한 번 dry-run
6. systemd service 재시작 (`systemctl restart <bot>`)

이 흐름 덕분에 OS 차이 함정에 맞아도 prod까지 가기 전에 한 번 더 거름.

## 빠뜨리지 말 것 체크리스트

새 봇을 Windows에서 짜기 시작할 때:

- [ ] `pathlib.Path` 사용, f-string + `\` 금지
- [ ] `signal.SIGHUP` 대신 file-based trigger
- [ ] `setx PYTHONIOENCODING utf-8` 한 번 실행
- [ ] `requirements.txt`에 platform-specific 패키지 (`pywin32` 같은) 가 들어가지 않게 주의 — Linux pip이 깨짐
- [ ] hardcoded path 있으면 forward slash 또는 `Path()`로

Linux로 옮길 때 한 번 더:

- [ ] `chmod 600 .env`
- [ ] venv를 새로 만들기 (Windows venv는 Linux에서 동작 안 함, 경로가 박혀 있음)
- [ ] systemd unit 파일에 `EnvironmentFile` 명시
- [ ] timezone 차이 — `datetime.utcnow()`로 통일하면 안전

## 다음 단계

- 코드 push할 때 시크릿 누출 방지 → `04-git-private-flow.md`
