# env-loader

> 한 줄 요약 (One-liner): `.env` 파일을 읽고 config 안의 `${VAR}` 참조를 환경변수로 치환하는 stdlib-only 로더.

## 의존성 (Dependencies)
- Python 3.10+
- stdlib only (`os`, `re`)

## AI에게 어떻게 시켰나 (How AI built it)

처음 프롬프트 (initial prompt):
> "config dict 안에 `${BOT_TOKEN}` 같은 placeholder가 박혀있는데, os.environ으로 치환해줘. 키가 없으면 명시적 에러. python-dotenv 같은 외부 라이브러리는 쓰지 마."

AI가 자주 틀린 것 (Common AI mistakes for this pattern):
- 환경변수 미설정 시 그냥 빈 문자열로 치환 (silent failure). 반드시 `ValueError`를 던져야 운영자가 즉시 인지함.
- nested dict/list를 재귀 처리하지 않음. config는 거의 항상 중첩되므로 재귀 필수.
- `python-dotenv` 같은 외부 라이브러리를 끌어다 씀 — 거래봇은 deps를 최소화해야 충돌이 적음.

## 코드 (드롭인 단위)
`env_loader.py` 한 파일. `resolve_env_vars(obj)` (재귀 치환), `load_dotenv(path)` (미니멀 .env 파서), `require(*keys)` (필수 키 검증) 세 함수.

## 사용 예시 (Usage)

```python
from env_loader import load_dotenv, resolve_env_vars, require

# 1) .env 로드 (이미 export된 값은 보존, override=True로 덮어쓰기 가능)
load_dotenv(".env")

# 2) 필수 키 검증 — 누락 시 ValueError
require("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")

# 3) config dict 치환
import yaml
cfg = yaml.safe_load(open("config.yaml"))
cfg = resolve_env_vars(cfg)   # 모든 "${VAR}" 자리 치환됨
```

## 실전 함정 (Battle-tested gotchas)
- `.env`에 따옴표를 붙이면 (`KEY="value"`) 값에 따옴표가 포함되는 라이브러리도 있음 — 이 모듈은 양쪽이 매칭되는 경우 stripping함. mismatched quote는 그대로 둠.
- Korean Windows에서 `.env`를 읽을 때 BOM이 붙은 UTF-8이면 첫 키가 `﻿KEY`로 깨짐. 저장 시 "UTF-8 (no BOM)"으로 저장할 것.
- `${VAR}` 안에 다시 `${...}`가 들어있는 nested 치환은 지원 안 함. 한 단계만 치환됨.

## 응용 예시 (Real-world usage in this repo)
- 이 모듈은 `multi-perp-dex/strategies/main.py` 부팅 시 `config.yaml`을 로드하기 직전에 호출되어 plaintext 비밀키 누출을 막습니다.
- `health-monitor`, `telegram-notifier`도 시작 시 `require()`를 호출해 필수 환경변수가 빠진 채로 부팅하는 사고를 방지합니다.
