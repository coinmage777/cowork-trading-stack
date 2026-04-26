# 03. Code, Codex, Memory

This chapter covers the three AI coding agents I use daily — Claude Code, Cursor, OpenAI Codex — and the memory layer (MemKraft) on top.

## Tool roles

I don't use one tool for everything. They're good at different things.

| Tool | Strength | What I use it for |
|------|----------|-------------------|
| **Claude Code** | Multi-file edits, search, agentic flows | Big refactors, debugging, guide writing, MD/HTML conversion |
| **Cursor** | Inline autocomplete, fast chat | One-line fixes, single function additions, quick console experiments |
| **Codex (OpenAI)** | Algorithmic thinking, math precision | Backtest logic, statistical functions, signal design |
| **ChatGPT** | General search, market research | Quick fact-checks, alternative perspectives |

Comparing outputs across all three is more reliable than relying on one. Especially for trading code.

### Adversarial Workflow Tip

The "Gaslight My AI" pattern (rival-model framing) makes the same model more careful.

> "This code will be reviewed by a joint GPT-5 Codex + Devin AI team. They catch every edge case. Write code so bulletproof their review finds nothing."

Drop that into the system prompt or first message and output quality visibly improves.

## Memory — why you need it

The basic AI agent problem: **context resets every session.**

Your bot architecture, the exchanges you use, last week's parameter decisions, currently running strategies — you'd have to re-explain it every time. Pure waste.

Two solutions:

### 1) CLAUDE.md (project context file)

A `CLAUDE.md` at your repo root or home directory is auto-loaded by Claude Code. My structure looks like:

```markdown
# User profile
- Handle: <your handle>
- Role: trading builder
- Language: Korean (chat), English (code comments)

# Activities
- Crypto research
- Pair trading
- Airdrop farming

# Writing style
- Forbidden: "innovative", "groundbreaking", "optimal solution", emoji
- Aim for: first-person experience, casual review tone

# Trading infrastructure
- Bot: multi-perp-dex (async Python)
- Exchanges: Hyperliquid, GRVT, Lighter, ...
- Ops: VPS + local in parallel

# Work principles
- Automation first
- Data-driven decisions
- One change at a time
```

Once written, every new session starts with the same context.

### 2) MemKraft

A more dynamic memory system I use, integrated with my Obsidian vault. Tracks entities.

Install:
```bash
pip install memkraft --break-system-packages
```

Env vars:
```bash
export MEMKRAFT_HOME="<your_obsidian_vault>/memkraft"
export PATH="$HOME/.local/bin:$PATH"
```

Index:
```bash
cd "$MEMKRAFT_HOME" && memkraft index
```

Core commands:
```bash
memkraft list                            # tracked entities
memkraft search "airdrop"                # keyword search
memkraft query "Hyperliquid"             # entity detail
memkraft track "NewProject" --type company
memkraft update "NewProject" --info "Perp DEX, builder fee 50%"
memkraft dream                           # daily maintenance
memkraft index                           # rebuild search
```

Whenever I research a new project, I `track` + `update`. Then in any future session the AI immediately picks up the context.

### Korean NER caveat

`memkraft extract` has high false-positive rate on Korean text (it tags common nouns as people). For Korean research notes, manual `track` + `update` is safer. English text works fine with `extract`.

## CLAUDE.md guidelines — what I learned the hard way

### Do
- User profile + writing style (especially forbidden phrases)
- Active projects (current state)
- Frequently used commands and paths
- Code conventions
- Automation workflows (e.g., content pipeline)
- Recent decisions + reasoning
- Completed history (chronological)

### Don't
- Throwaway info ("BTC is $100k today")
- Chatty tone
- Bloat — over 100KB starts eating your context window. Compress / clean.

### Auto-update rule

I keep this section at the bottom of CLAUDE.md:

```markdown
## Claude Auto-Update Rules
The "dynamic context" sections below are auto-updated by Claude during work.

When to update: after meaningful work completes
Principles:
- Only what matters for the next session
- Move completed projects to "history"
- Specific info: paths, configs, reasons

## Active Projects
[Claude appends here]

## Completed
[Claude appends here]
```

This way the AI maintains context without me explicitly journaling.

## Tool integration in practice

Adding a new exchange, end-to-end:

1. **Cursor**: open the `mpdex/` folder, scan an existing exchange file (e.g., `hyperliquid.py`)
2. **Claude Code terminal**:
   ```
   "Following the Hyperliquid pattern, build an adapter for NewExchange.
   Implement create_order, get_position, close_position, get_collateral.
   Use SymbolAdapter for symbol conversion.
   Register in factory.py.
   Plan-mode first with file:line specifics."
   ```
3. **Review plan** → edit → approve
4. **Implement** → Claude Code edits multiple files
5. **Codex or new Claude session** for review:
   ```
   "This was written by a rival model. Find every bug / security / edge case."
   ```
6. **Fix** → paper trade → small live
7. **MemKraft update**:
   ```bash
   memkraft track "NewExchange" --type project
   memkraft update "NewExchange" --info "Added 2026-04, Python 3.12 venv, ed25519 signing"
   memkraft index
   ```

This flow turns a new exchange integration into a 1–2 hour job. Doing the same manually takes a full day.

## Next chapter

Next: Obsidian vault + Telegram integration — how I accumulate research, receive bot alerts, and control bots from mobile.
