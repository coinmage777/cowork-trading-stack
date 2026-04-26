# 11. Operational Infrastructure & Principles

Writing the bot is half the work. Running it 24/7 reliably is the other half. The following covers infrastructure and principles drawn from operational experience.

## VPS setup

### Choosing a VPS

Common options:
- **Contabo** — best value (≈$10–30/mo). Reasonable stability.
- **Hetzner** — Europe-based, very stable
- **DigitalOcean / Vultr** — standard
- **AWS / GCP** — only when scale demands

A reasonable starting point: a Contabo VPS, 4GB RAM, Ubuntu 22.04, ~$15/mo.

### Base setup

```bash
# After SSH key registration, log in as root
ssh root@<your_vps_ip>

# Updates
apt update && apt upgrade -y

# Essentials
apt install -y git python3 python3-pip python3-venv tmux htop ufw fail2ban

# Firewall
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw enable

# Non-root user (don't run bots as root)
adduser bot
usermod -aG sudo bot
su - bot
```

### Python venv

```bash
mkdir ~/perp-bot && cd ~/perp-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

When isolated venvs are needed (SDK conflict / Python version):
- `nado_venv/` — Python 3.10
- `reya_venv/` — Python 3.12 (mandatory)
- `bulk_venv/` — bulk-keychain (needs Rust build)
- `claim_venv/` — [Polymarket](https://polymarket.com/?ref=coinmage) claim-only

Each venv runs independently, communicating with the main bot via subprocess bridge.

### Google Drive integration (optional)

Content and notes can be kept on Google Drive and mounted on the VPS so files are the same locally and remotely.

```bash
# Install rclone
curl https://rclone.org/install.sh | sudo bash

# Configure Google Drive
rclone config
# OAuth or service-account JSON

# Mount
mkdir /mnt/gdrive
rclone mount gdrive: /mnt/gdrive --daemon

# Register systemd service (auto-mount on reboot)
```

## Process management

### tmux for persistence

```bash
tmux new -s bot
source venv/bin/activate
python multi_runner.py

# Ctrl-b d to detach (bot keeps running)
# reattach:
tmux attach -t bot
```

### systemd service (more robust)

`/etc/systemd/system/perp-bot.service`:

```ini
[Unit]
Description=Perp Trading Bot
After=network.target

[Service]
Type=simple
User=bot
WorkingDirectory=/home/bot/perp-bot
ExecStart=/home/bot/perp-bot/venv/bin/python multi_runner.py
Restart=on-failure
RestartSec=30
StandardOutput=append:/home/bot/perp-bot/logs/bot.log
StandardError=append:/home/bot/perp-bot/logs/bot.log

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable perp-bot
systemctl start perp-bot
systemctl status perp-bot
journalctl -u perp-bot -f  # live logs
```

### Watchdog

A typical setup combines systemd with a separate watchdog. systemd catches process death; the watchdog catches "alive but hung."

```bash
# watchdog.sh
#!/bin/bash
LOG=/home/bot/perp-bot/logs/bot.log
LAST_MOD=$(stat -c %Y "$LOG")
NOW=$(date +%s)
DIFF=$((NOW - LAST_MOD))

if [ $DIFF -gt 300 ]; then
    # 5 minutes without log writes = zombie
    pkill -f multi_runner.py
    sleep 5
    systemctl restart perp-bot
    echo "$(date) restarted (stale)" >> /home/bot/perp-bot/logs/watchdog.log
fi
```

cron, every minute:
```
* * * * * /home/bot/perp-bot/watchdog.sh
```

## Logging / monitoring

### Structured logs

Plain-text logs only support grep. JSON logs are analyzable.

```python
import logging
import json

class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_obj = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if hasattr(record, "exchange"):
            log_obj["exchange"] = record.exchange
        if hasattr(record, "trade_id"):
            log_obj["trade_id"] = record.trade_id
        return json.dumps(log_obj, ensure_ascii=False)

handler = logging.FileHandler("logs/bot.json")
handler.setFormatter(JsonFormatter())
logger = logging.getLogger("bot")
logger.addHandler(handler)

logger.info("position opened", extra={"exchange": "hyperliquid", "trade_id": 12345})
```

### DB logging

Every trade to SQLite:

```sql
CREATE TABLE trades (
    id INTEGER PRIMARY KEY,
    timestamp TEXT,
    exchange TEXT,
    symbol TEXT,
    side TEXT,
    qty REAL,
    entry_price REAL,
    exit_price REAL,
    pnl REAL,
    fee REAL,
    funding REAL,
    closed_at TEXT,
    close_reason TEXT,
    strategy TEXT
);

CREATE TABLE equity_snapshots (
    id INTEGER PRIMARY KEY,
    timestamp TEXT,
    exchange TEXT,
    balance_usd REAL
);
```

Balance snapshots every 10 minutes are the source of truth. Log PnL % can be inaccurate (leverage estimation, fees, funding). Balance snapshots come straight from exchange APIs and are correct.

### Equity Tracker

Core infrastructure:

```python
async def equity_tracker_loop():
    while True:
        for exchange in exchanges:
            try:
                balance = await exchange.get_collateral()
                db.insert_equity_snapshot(
                    timestamp=datetime.utcnow().isoformat(),
                    exchange=exchange.name,
                    balance_usd=float(balance),
                )
            except Exception as e:
                logger.error(f"equity tracker {exchange.name}: {e}")
        await asyncio.sleep(600)  # 10 min
```

This computes daily / weekly / monthly PnL. **Never compare with log PnL — balance snapshots are the truth.**

## Security

### 1) API key management

- Store in `.env`, never commit
- `.gitignore` includes `.env`, `secrets/`, `*.key`
- Exchange API keys: **trading permissions only**, withdrawal off
- IP whitelist whenever available

### 2) Env-var loading

```python
import os
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("EXCHANGE_API_KEY")
if not API_KEY:
    raise ValueError("EXCHANGE_API_KEY not set")
```

Don't put keys directly in config.yaml. Reference env vars:

```yaml
exchanges:
  hyperliquid:
    keys:
      private_key: ${HYPERLIQUID_PRIVATE_KEY}
      wallet_address: ${HYPERLIQUID_WALLET}
```

### 3) Verify no keys in code

Pre-commit hook to auto-check:

```bash
# .git/hooks/pre-commit
#!/bin/bash
if git diff --cached | grep -E '0x[a-fA-F0-9]{40}|sk-[a-zA-Z0-9]{32,}'; then
    echo "ERROR: API key or address found in commit"
    exit 1
fi
```

### 4) Multisig / hardware wallets

For meaningful funds:
- Operating capital only on hot wallet (trading-capable key)
- Real treasury on multisig / hardware wallet (cold)
- Even on hot-wallet loss, treasury safe

### 5) VPS security

- SSH key auth only (disable password)
- Disable root SSH login
- fail2ban for brute force
- `apt upgrade` regularly
- Close all unused ports

## Backups

### Code
- Git push to private repo (GitHub)
- Daily auto-push (cron)

### DB
- Daily SQLite dump → Google Drive
```bash
sqlite3 trades.db ".backup '/mnt/gdrive/backups/trades_$(date +%Y%m%d).db'"
```

### Configs
- `.env` → 1Password / Bitwarden (never plaintext backup)

### Logs
- Compress / delete logs > 30 days (disk space)
- < 30 days: compressed, backed up to Google Drive

## Restart / hot reload

### Graceful restart

If the bot has open positions, restart can lose state. A working pattern:

1. **State manager**: persist DCA / trailing state to JSON on every change
```python
def save_state(state: dict):
    with open("trader_state.json", "w") as f:
        json.dump(state, f)
```

2. **Restore on restart**:
```python
def load_state():
    if Path("trader_state.json").exists():
        with open("trader_state.json") as f:
            return json.load(f)
    return {}
```

3. **Orphan prevention**: at restart, query exchange for actual positions → reconcile against DB open trades. Mismatch → mark closed.

### Hot reload

Update params without restart:

```python
import signal

def reload_config(signum, frame):
    logger.info("Reloading config")
    config.load()
    apply_to_strategies()

signal.signal(signal.SIGHUP, reload_config)
```

```bash
kill -HUP $(pgrep -f multi_runner.py)
```

Windows doesn't support SIGHUP from outside → file-trigger pattern (Ch 4).

## Operational principles — earned the hard way

### 1) Change → verify → scale

Any new strategy / param:
1. Pass backtest
2. Paper trade 1 week
3. Small live (5% of capital)
4. 1-week monitor → expand if clean
5. **Never** go full size in one shot

### 2) One change at a time

Five params at once = no attribution. A/B style.

### 3) Data-driven decisions

"Cut SL by feel" is forbidden. Pull DB data, decide. Before any change, review SL distribution / WR distribution / WR by DCA depth.

### 4) Circuit breaker mandatory

Daily -X% loss → auto-stop. The safety net for unbounded loss. Typical defaults: 5–10% of capital.

### 5) Alert priorities

- CRITICAL (immediate): daily X% loss, balance 0, crash → Telegram + push
- WARNING (immediate): exchange off, WS disconnect → Telegram only
- INFO (daily): PnL summary → Telegram only
- DEBUG: every entry / exit → log only

Too many alerts and they get tuned out. Only what matters.

### 6) Post-mortems

After significant losses or incidents, write a reproducible post-mortem:

```markdown
# Post-Mortem: 2026-04-13 27-day red streak

## What happened
27 consecutive days of -1–3% daily loss. Cumulative loss in the low four figures.

## Why
TP 0.4% / SL 3%, requiring 94% break-even WR. Actual WR ~70% → structurally negative.
Cause: pnl_percent designed on margin basis. R:R math not verified.

## How fixed
- TP 0.4% → 2%, SL 3% → 2.5%, leverage 15x → 10x
- Break-even WR dropped to 55%
- Verified actual WR 67–84% → flipped to profit

## How to prevent
- Every new strategy must verify break-even WR < 60%
- TP/SL designed by R:R, not margin %
- Backtest PF > 1.5 mandatory
```

These accumulate as operational capital. The same mistake should not recur twice.

### 7) Zombie process check

After bot restart, the previous instance may still be alive. Always check:
```bash
ps aux | grep python | grep multi_runner
```

Two instances of the same bot = duplicate orders = financial accident. Confirm kill before relaunch.

## Next chapter

Next: exchange API setup. Per-exchange API keys, permissions, gotchas.
