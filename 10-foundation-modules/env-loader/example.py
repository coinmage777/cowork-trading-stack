"""env-loader 사용 데모."""
import os
from env_loader import load_dotenv, resolve_env_vars, require

# 1) .env 파일 로드 (없으면 0 반환)
loaded = load_dotenv(".env")
print(f"loaded {loaded} vars from .env")

# 2) 필수 키 검증 — 데모용 임시 세팅
os.environ.setdefault("BOT_TOKEN", "demo-token")
os.environ.setdefault("CHAT_ID", "12345")
require("BOT_TOKEN", "CHAT_ID")

# 3) 중첩 config 안의 ${VAR} 치환
cfg = {
    "telegram": {"token": "${BOT_TOKEN}", "chat": "${CHAT_ID}"},
    "exchanges": ["${BOT_TOKEN}-suffix"],
}
resolved = resolve_env_vars(cfg)
print(resolved)
