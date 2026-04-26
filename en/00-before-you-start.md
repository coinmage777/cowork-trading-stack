# 00. Before You Start

A few things I want to clear up before you read further.

## What this guide is, and isn't

**This is** working notes on the crypto trading automation setup I actually run: pair trading bots, [Polymarket](https://polymarket.com/?ref=coinmage) auto-trading, kimchi premium arbitrage, cross-exchange arb, an Obsidian-based research pipeline, and AI agent integrations. Tools I use almost daily.

**This is not**:
- A get-rich-quick guide. I will be very surprised if anyone reads this and immediately makes serious money.
- A collection of guaranteed-profit strategies. Every strategy here can break, and most of mine have spent stretches in the red.
- Polished, production-grade code. The bots I run break, get patched, and break again. That cycle is the work.

This is not investment advice. It's a record of how I do this job.

## Prerequisites

### Tech stack
- **Python 3.10+** — most of the bots are async Python
- **Node.js** — some SDKs are JS-only
- **Git** — running a bot without version control eventually ends in disaster
- **VS Code or Cursor** — AI-integrated editors recommended
- **Terminal comfort** — bash, zsh, PowerShell, doesn't matter as long as one of them feels natural

### Infrastructure
- **Local machine** — for development, MacBook or Windows is fine
- **VPS** — for production, anything works (Contabo / Hetzner / DigitalOcean / AWS)
- **Google Drive** or equivalent cloud — content backup
- **Telegram** — alerts and remote control

### Capital
- Start small. Genuinely small. Don't deposit serious capital until a bot has run stable for a week.
- I started with maybe $50–$100 per exchange.
- Capital scales after the strategy is verified, not before.

### Time
- Initial setup: two weekends or so
- First bot stable: 2–4 weeks
- Daily monitoring: 30 minutes to an hour
- Big changes / debugging: depends, sometimes a full day

## How this guide is structured

Every chapter is meant to stand on its own, but the order builds naturally:
1. Automation mindset (Ch 1)
2. AI tooling setup (Ch 2–3)
3. Knowledge infrastructure (Ch 4)
4. Light automation first (Ch 5: volume farming)
5. Real trading bots (Ch 6–10)
6. Operations / infra / exchange setup (Ch 11–12)
7. Roadmap + reference (Ch 13–15)

## Security principles — actually follow these

- **Never** put private keys, API secrets, or passphrases directly in code
- Don't commit `.env` files (add to `.gitignore`)
- API keys: trading permission only, **never withdrawal**
- Turn on IP whitelisting wherever it's available
- For meaningful capital, use multisig or hardware wallets

Every code example in this guide has sensitive material replaced with placeholders. When you fill in your own keys, double-check that line.

## On the nature of this material

This material is a snapshot at the moment of publication. Strategies and tools change continuously, so some of what is described may already be out of date by the time it is read. Treat the chapters as a working framework rather than a fixed playbook.

Good alpha stops being alpha the moment it is written down. The goal here is not "the secret to making money right now" — it is closer to "a framework for working this way and building your own approach."
