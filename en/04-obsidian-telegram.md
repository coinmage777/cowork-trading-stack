# 04. Obsidian + Telegram

Knowledge has to compound to become alpha, and alerts have to fire for systems to actually run. Obsidian and Telegram are the two pillars of my workflow.

## Obsidian — research compounding system

My vault has 145+ notes and 670+ wikilinks. It contains 30+ project research files, 90+ tracked airdrops, 20+ blog guides, and trading journals.

### Why Obsidian

- **Local files** — no cloud lock-in. Plain `.md` files, move via git / rclone / email
- **Wikilinks** — `[[BTC]]` auto-links to other notes. Graph view visualizes the network.
- **Strong plugins** — Dataview, Templater, Calendar, Kanban, Tasks
- **Markdown** — easy export to blog / GitHub / Notion

### Vault structure

My top-level folders:

```
Obsidian Vault/
├── Airdrop/               # Airdrop tracking + Kanban + ROI
├── Content/               # Blog / YouTube / Telegram content
├── Dashboard/             # Dashboards (Home, metrics, links)
├── Inbox/                 # Capture (uncategorized raw notes)
├── Memory/                # AI memory (Topics, Daily, Archive)
├── Projects/              # 30+ project research files
├── Templates/             # 12 templates (trade journal, weekly review, etc.)
├── Trading/               # Trading (daily/, onchain/, strategy guides)
├── assets/                # Images
├── docs/                  # System docs (memory, research)
└── scripts/               # Automation scripts
```

### Core note patterns

- **Dashboard/Home.md** — entry point. Dataview queries auto-display recent notes, active projects, airdrop deadlines
- **Projects/{ProjectName}.md** — one file per project. Mechanism / tokenomics / research / action items
- **Trading/daily/YYYY-MM-DD.md** — daily trade journal, auto-generated from template
- **Memory/MEMORY.md** — AI agent memory index (paired with MemKraft from Ch 3)

### Plugins I run

Eight:
1. **Dataview** — query notes like a database. Core of dashboards.
2. **Templater** — auto-generate trade journals / project notes
3. **Calendar** — calendar view of date notes
4. **Kanban** — airdrop progress board
5. **Advanced Tables** — markdown table editor
6. **Periodic Notes** — daily / weekly / monthly notes
7. **Tasks** — `- [ ]` checkboxes globally queryable
8. **Tag Wrangler** — tag cleanup

### Dataview examples

Active projects auto-list:

````markdown
```dataview
TABLE status, last_review, tier
FROM "Projects"
WHERE status = "active"
SORT last_review DESC
```
````

This week's PnL:

````markdown
```dataview
TABLE sum(rows.pnl) as "PnL"
FROM "Trading/daily"
WHERE date >= date(today) - dur(7 days)
GROUP BY week
```
````

### Trade journal template

`Templates/daily-trade.md`:

```markdown
---
date: <% tp.date.now("YYYY-MM-DD") %>
pnl: 
volume: 
exchanges: 
---

# Trade Journal — <% tp.date.now("YYYY-MM-DD") %>

## Market context
- BTC: 
- ETH: 
- Funding: 

## Entries / Exits
| Time | Exchange | Pair | Side | Size | Entry | Exit | PnL |
|------|----------|------|------|------|-------|------|-----|

## Signals / post-mortem

## Tomorrow
- [ ]
```

I auto-generate this note every day with a script that pulls from the bot DB.

### Graph view

Obsidian's real weapon. Wikilinks form a graph automatically. My vault has clear clusters around 47 core notes — "exchange-related notes" / "strategy-related notes" emerge visually.

### Backup

I sync the entire vault to a private git repo:

```bash
cd "$OBSIDIAN_VAULT"
git init
git remote add origin <your_private_obsidian_repo>
git add .
git commit -m "init"
git push -u origin main
```

Then daily:
```bash
git add . && git commit -m "$(date +%Y-%m-%d)" && git push
```

Cron does this daily for me automatically.

## Telegram — alerts + remote control

Telegram is half my bot's UI. Alerts, commands, PnL reports, kill-switch — all there.

### Create the bot

1. On Telegram, find [@BotFather](https://t.me/BotFather)
2. `/newbot` → name → token returned
3. Start a chat with your new bot → `/start`
4. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` → grab `chat.id`

### Env vars

```bash
TELEGRAM_BOT_TOKEN=<your_telegram_bot_token>
TELEGRAM_CHAT_ID=<your_telegram_chat_id>
```

### Minimal alerter

```python
import os
import aiohttp

async def notify(text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json={"chat_id": chat_id, "text": text}) as r:
            await r.text()

# usage
await notify("BOT START")
await notify(f"Position opened: BTC short, est PnL +0.5%")
```

### Dedup / throttle

Avoiding alert spam matters. My pattern:

```python
class Notifier:
    def __init__(self, throttle_minutes=10):
        self._last_sent = {}
        self._throttle = throttle_minutes * 60

    async def send(self, key: str, text: str):
        now = time.time()
        if key in self._last_sent and now - self._last_sent[key] < self._throttle:
            return
        self._last_sent[key] = now
        await notify(text)
```

Same alert key won't fire twice within 10 minutes.

### Alert priorities

What my bots send:

| Priority | Cases | Frequency |
|----------|-------|-----------|
| **CRITICAL** | Daily loss > X%, exchange balance 0, BNB too low (gas), bot crash | Immediate |
| **WARNING** | Exchange disabled, balance plummet, WS disconnect | Immediate |
| **INFO** | Bot start / stop, daily PnL summary | Once per day |
| **DEBUG** | Every entry / exit | Log only, not Telegram |

### Remote control — Telegram Commander

A separate bot that accepts commands. My commander supports:

| Command | Action |
|---------|--------|
| `/status` | Bot state / positions / PnL summary |
| `/pnl` | Daily PnL report (calls daily_report.py) |
| `/balance` | Per-exchange balances |
| `/positions` | Active positions list |
| `/restart` | Creates trigger file → bot graceful-restarts |
| `/reload` | Trigger file → config hot-reload |
| `/close` | Close all + exit (`/yes` two-step confirm) |
| `/kill <ex>` | Disable a specific exchange |
| `/revive <ex>` | Re-enable a disabled exchange |
| `/bnb` | BNB gas balance (for [Polymarket](https://polymarket.com/?ref=coinmage)) |

### Whitelist security

The commander bot **must** restrict by chat_id whitelist:

```python
ALLOWED_CHAT_IDS = [int(x) for x in os.getenv("TELEGRAM_ALLOWED_CHATS", "").split(",")]

async def handle(update):
    if update.message.chat.id not in ALLOWED_CHAT_IDS:
        return  # silently drop
    # ...
```

Without this, anyone who finds your token can `/close` your positions. Game over.

### File-trigger pattern (Windows-friendly)

Windows doesn't accept SIGHUP / SIGTERM from outside processes. So I use a file-trigger pattern:

```python
# trigger_watcher.py
TRIGGER_DIR = Path("triggers")

async def watch():
    while True:
        for trigger_file in TRIGGER_DIR.glob("*.trigger"):
            kind = trigger_file.stem
            if kind == "restart":
                await graceful_restart()
            elif kind == "reload":
                await hot_reload_config()
            elif kind == "close":
                await close_all_and_exit()
            trigger_file.unlink()  # remove after handling
        await asyncio.sleep(2)
```

The commander bot, when receiving `/restart`, writes `triggers/restart.trigger`. The bot detects within 2 seconds → handles → deletes the file.

OS-independent and clean for mobile control via Telegram.

## Next chapter

Next, the smallest possible automation: Volume Farmer. The simplest pattern for automating exchange volume farming.
