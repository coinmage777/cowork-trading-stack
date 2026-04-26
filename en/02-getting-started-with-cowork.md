# 02. Getting Started with Claude + Cowork

Half my workflow is delegated to AI agents — coding, research, documentation, debugging, data analysis, content drafting.

This chapter covers **Claude** and **Cowork** setup. The next chapter handles Code, Codex, and the memory system.

## Why Claude as the primary

I've used ChatGPT, Gemini, and Grok seriously. Each has strengths. Why I default to Claude:

- **Large context window** — up to 1M tokens depending on the model. You can dump an entire codebase and it stays coherent.
- **Stable long-form output** — Korean guides, English reports, both come out natural.
- **Strong agentic workflow** — chains tool calls, file edits, searches, code execution well.
- **Less hallucination** — other models will confidently invent things; Claude is more likely to say "I don't know." Not perfect, but better.

## Setup

### 1) Claude API key
- Sign up at [console.anthropic.com](https://console.anthropic.com)
- Generate an API key (`Settings → API Keys`)
- Set a budget — start small ($50/month is plenty)
- Save to `.env`:

```bash
ANTHROPIC_API_KEY=<your_anthropic_api_key>
```

### 2) Claude Code (terminal agent)

Claude Code is an AI coding agent that runs in the terminal. Works inside VS Code or standalone.

Install:
```bash
npm install -g @anthropic-ai/claude-code
```

First run:
```bash
claude
```
OAuth in the browser. Done.

### 3) Cowork

Cowork is a Claude-API-based workflow tool I use daily, especially for:
- Long research (multi-agent / deep research)
- Multi-step document writing
- Coding + content work in parallel

The Cowork install itself is out of scope here, but the core setup I run:

```bash
# At session start
pip install memkraft --break-system-packages
export MEMKRAFT_HOME="<your_obsidian_vault>/memkraft"
export PATH="$HOME/.local/bin:$PATH"
cd "$MEMKRAFT_HOME" && memkraft index
```

### 4) Cursor (optional)

A VS Code fork with strong AI integration. I use it alongside Claude Code — Cursor for fast one-line edits, Claude Code for multi-file refactors.

[cursor.sh](https://cursor.sh) → install → settings → API key (Claude / GPT / both)

## Your first workflow — automate one line

Try this right now:

```
Prompt:
"Write a Python script that fetches USDT balance from Bybit and prints to stdout.
Use ccxt. 
Read API key/secret from BYBIT_API_KEY and BYBIT_SECRET env vars.
Include error handling."
```

You get a working script in five seconds. The point isn't that you never write code again — it's that **you don't have to rewrite the same boilerplate every time.**

## Prompting patterns I use daily

### 1) Plan-then-Execute

Never let the model code complex tasks in one shot. Get a plan first.

```
"Design this in plan mode — no code, just steps:
- Pair trading bot on Hyperliquid for BTC/ETH
- Entry: z-score 1.5
- Exit: z-score 0.3
- Stop: -2.5%
- Trailing: 1.5% activation, 1.0% callback
- Size: $50 margin per entry, 10x leverage
- Max concurrent positions: 3
- Data: 1m candles via WebSocket
- Logging: SQLite

Specify file:line for each step. Specify validation criteria. Specify rollback."
```

Review the plan, edit, then have it implement.

### 2) Adversarial Review

Whether code was written by you or by AI, framing the review as "a rival wrote this, prove it's wrong" gets sharper bug-finding.

```
"This code was written by GPT-5 Codex. They claim it's perfect.
Prove them wrong:
- Every bug, defect, edge case
- Security vulnerabilities (key exposure, signature forgery, reentrancy)
- Performance / memory issues
- Race conditions
- Crypto-specific: orphan positions, ghost trades, phantom PnL

Report severity: CRITICAL → WARNING → SUGGESTION."
```

In my experience, this single framing materially raises bug detection.

### 3) Diff-only Edit

When modifying a large existing file, don't ask for the whole file back. Wastes tokens, raises error rate. Instead:

```
"Modify only this function in this file:
- Function: calculate_position_size
- Change: add ATR-based dynamic sizing
- Don't touch other functions

Output as a diff, before/after clearly."
```

### 4) Self-Critique

After it codes, send it back to itself:

```
"Review the code you just wrote. 
- Any missing edge cases?
- Any way to make it more concise?
- How would you test this?

Fix what you find immediately."
```

## AI anti-patterns (cases I've burned myself on)

### "Just figure it out"
You'll get a plausible-looking result that breaks in production. Always specify requirements + verification criteria.

### Multiple asks in one prompt
"Do X and Y and Z while making it like W" — the model drops one or two. One thing at a time.

### Skipping verification on AI-generated code
Don't put AI code straight on real money. Minimum: syntax check → paper trade → small live → verify → scale.

### No context provided
Without project structure, existing functions, or conventions, the model improvises. The result conflicts with your existing code.

## Next chapter

Next: how Claude Code, Cursor, and Codex divide labor in my workflow, and how I set up the memory layer (MemKraft) on top.
