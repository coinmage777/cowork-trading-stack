# Cryptomage Trading Guide

> A practitioner's playbook for automating crypto trading across Perp DEXs, prediction markets, and cross-venue arbitrage — built and battle-tested in production.
> 페어 트레이딩, 김프, 폴리마켓, 크로스 거래소 차익까지 — 실전에서 굴러가는 자동화 시스템 풀 가이드.

---

## English (Primary — Global Audience)

This guide documents an end-to-end approach to crypto trading automation: how I think about it, the tooling I use, the strategies I run, and the operational discipline that keeps the lights on.

It is written for someone who can code a little, has read a few DeFi posts, and wants to stop clicking buttons. It is not financial advice. It is not a "guaranteed alpha" pitch. It is a description of what a working setup looks like — including the failures that shaped it.

### Who this is for

- Builders who want to ship trading bots without an institutional team
- Researchers who farm airdrops and need a structured workflow
- Content creators who treat trading as both a livelihood and a topic
- Anyone allergic to manual order entry who has decided to automate

### Table of Contents (English)

1. [Before You Start](en/00-before-you-start.md)
2. [Why Automation First](en/01-why-automation-first.md)
3. [Getting Started with Cowork](en/02-getting-started-with-cowork.md)
4. [Code, Codex, Memory](en/03-code-codex-memory.md)
5. [Obsidian + Telegram](en/04-obsidian-telegram.md)
6. [Volume Farmer](en/05-volume-farmer.md)
7. [Multi-Perp Pair Trading](en/06-multi-perp-pair-trading.md)
8. [Kimchi Premium & Cross-Venue Arb](en/07-kimchi-cross-venue-arb.md)
9. [Minara Backtesting](en/08-minara-backtesting.md)
10. [Polymarket Bot](en/09-polymarket-bot.md)
11. [Gold Cross-Exchange Arb](en/10-gold-cross-exchange-arb.md)
12. [Operational Infra & Principles](en/11-infra-principles.md)
13. [Exchange API Setup](en/12-exchange-api-setup.md)
14. [Step-by-Step Roadmap](en/13-roadmap.md)
15. [Public Code](en/14-public-code.md)
16. [Glossary](en/15-glossary.md)

---

## 한국어 (Korean — Original)

이 가이드는 크립토 트레이딩 자동화를 어떻게 생각하고, 어떤 툴을 쓰고, 어떤 전략을 굴리고, 어떻게 운영하는지를 한 번에 정리한 문서다.

코드를 조금 쓸 수 있고, DeFi 글을 몇 편 읽어봤고, 더 이상 수동으로 버튼 누르고 싶지 않은 사람을 위해 썼다. 투자 권유가 아니다. 확정 수익을 약속하지도 않는다. 실제로 굴러가는 시스템이 어떻게 생겼는지 — 그 안에서 깨지면서 배운 것까지 — 적은 글이다.

### 누구에게 도움이 되나

- 팀 없이 혼자 트레이딩 봇을 만드는 빌더
- 에어드랍 파밍을 체계적으로 하고 싶은 리서처
- 트레이딩이 직업이자 콘텐츠 소재인 크리에이터
- 수동 주문이 지긋지긋해서 자동화로 넘어가려는 모두

### 목차 (한국어)

1. [시작하기 전에](ko/00-시작하기-전에.md)
2. [왜 자동화부터인가](ko/01-왜-자동화부터인가.md)
3. [Claude + Cowork 시작하기](ko/02-claude-cowork-시작하기.md)
4. [Code, Codex, 메모리](ko/03-code-codex-메모리.md)
5. [옵시디언 + 텔레그램](ko/04-옵시디언-텔레그램.md)
6. [Volume Farmer](ko/05-volume-farmer.md)
7. [멀티 Perp 페어 트레이딩](ko/06-멀티-perp-페어-트레이딩.md)
8. [김프 + Cross-Venue 차익](ko/07-김프-cross-venue-차익.md)
9. [Minara 백테스팅](ko/08-minara-백테스팅.md)
10. [Polymarket 봇](ko/09-polymarket-봇.md)
11. [Gold Cross-Exchange Arb](ko/10-gold-cross-exchange-arb.md)
12. [운영 인프라 & 원칙](ko/11-운영-인프라-원칙.md)
13. [거래소 API 셋팅](ko/12-거래소-API-셋팅.md)
14. [단계별 로드맵](ko/13-단계별-로드맵.md)
15. [공개 코드](ko/14-공개-코드.md)
16. [용어 정리](ko/15-용어-정리.md)

---

## Infrastructure / 인프라 코드

The full infrastructure that powers the strategies in this guide is included in this repository as 14 self-contained modules. Each module has its own README, requirements, and entry points — clone the repo, install per-module dependencies, fill in your own keys via `.env`, and you have a working starting point.

이 가이드에서 다루는 전략들이 실제로 굴러가는 풀스택 인프라를 14개 자가완결 모듈로 정리했습니다. 모듈마다 자체 README/requirements/엔트리포인트가 있어서, 레포 클론 → 모듈별 의존성 설치 → 본인 키를 `.env`에 주입하시면 시작할 수 있습니다.

### Modules / 모듈

| Module | Description |
|--------|-------------|
| [`perp-dex-wrappers/`](./perp-dex-wrappers/) | 22 Perp DEX integrations behind a unified factory. HL / Lighter / GRVT / Paradex / Backpack / Aster / Pacifica / EdgeX / Reya and more. <br>22개 Perp DEX 통합 래퍼 (factory + 공통 인터페이스) |
| [`perp-dex-setup-guides/`](./perp-dex-setup-guides/) | API key issuance + WebSocket reference per exchange. <br>거래소별 API key 발급 + WebSocket 연동 레퍼런스 |
| [`volume-farmer-templates/`](./volume-farmer-templates/) | Cross-venue delta-neutral volume farmers (Rise / Lighter / Var-Aster / Ethereal-Aster) + funding-arb. <br>크로스-베뉴 델타뉴트럴 볼륨 파머 + 펀딩 아비 템플릿 |
| [`cross-venue-arb-scanner/`](./cross-venue-arb-scanner/) | Multi-exchange concurrent ticker fetch → spread divergence detection (new listings like HYPER). <br>다중 거래소 동시 ticker fetch → spread 발산 탐지 |
| [`spot-spot-arb/`](./spot-spot-arb/) | KR (Bithumb / Upbit) ↔ global CEX/DEX spot arbitrage (FastAPI backend + React frontend + Rust services). <br>김프(빗썸/업비트) ↔ 글로벌 CEX/DEX 차익거래 풀스택 |
| [`aster-spot-buyer/`](./aster-spot-buyer/) | Aster (BSC) spot auto-buyer + farmer hedge leg. <br>Aster 현물 자동 매수 + farmer 헷지 레그 |
| [`pancake-deposit-helper/`](./pancake-deposit-helper/) | PancakeSwap V2 swap + Stargate / Across V3 cross-chain bridge helper. <br>팬케이크 V2 스왑 + 크로스체인 브릿지 헬퍼 |
| [`polymarket-bot/`](./polymarket-bot/) | Polymarket Up/Down sniper + auto_claimer + Stoikov MM + Bayesian prior + reversal/merge_split. <br>폴리마켓 스나이퍼 + 자동 클레임 + Stoikov MM |
| [`predict-fun-sniper/`](./predict-fun-sniper/) | Predict.fun (BSC) 1-hour market mid-time sniper + JWT auth + BNB gas monitor. <br>Predict.fun 1시간 마켓 mid-time 스나이퍼 |
| [`rust-services/`](./rust-services/) | hl-sign (HL signature, 3.5x speedup) + gap-recorder (SQLite WAL batched, 169K rows/sec). PyO3 + maturin. <br>HL 서명 가속 + gap-recorder 고속 기록 (PyO3 + maturin) |
| [`shared-utils/`](./shared-utils/) | Telegram notifier, subprocess_wrapper (isolated venv), health_monitor, file-based trigger_watcher, state/equity tracker. <br>공용 유틸: Telegram, 격리 venv subprocess, 헬스체크, 파일 트리거, 상태/잔고 트래커 |
| [`strategy-templates/`](./strategy-templates/) | pair_trader, nado_pair_scalper (regime filter + DCA), strategy_evolver (GA), momentum / donchian / grid signals. <br>전략 템플릿: 페어 / 스캘퍼 / 자가진화 / 모멘텀 / 돈치안 / 그리드 |
| [`backtest-templates/`](./backtest-templates/) | ccxt OHLCV backtester + RSI70 / SuperTrend / Mean-Rev / BB-Upper Pine→Python ports. <br>ccxt 기반 백테스터 + Pine→Python 포팅 전략 |
| [`telegram-control/`](./telegram-control/) | Remote bot control via Telegram: `/status` `/pnl` `/balance` `/positions` `/restart` `/reload` `/close` `/kill` `/revive` `/bnb`. <br>텔레그램으로 봇 원격 제어 |

### Quick Start / 빠른 시작

```bash
# 1. Python env
python -m venv venv
source venv/bin/activate    # macOS/Linux
# or
venv\Scripts\activate       # Windows

# 2. Module-specific deps (each module has its own requirements.txt)
pip install -r perp-dex-wrappers/requirements.txt
# ... repeat per module you actually use

# 3. Fill in your own keys
cp .env.example .env        # if a module ships one — otherwise see module README
# edit .env with your keys (PRIVATE_KEY, exchange API keys, RPC URLs, Telegram, ...)

# 4. Run a module
python -m {module}.{entry_point}
```

Per-module ENV variables and entry points are documented inside each module's own README.

모듈마다 필요한 환경변수와 실행 명령은 해당 모듈 README에 적혀 있습니다.

### Exchanges / 거래소

이 인프라에서 사용하는 거래소와 가입 링크는 [EXCHANGES.md](./EXCHANGES.md)에 정리되어 있습니다.

The exchanges and protocols used in this infrastructure are listed in [EXCHANGES.md](./EXCHANGES.md).

### Architecture Principles / 아키텍처 원칙

- **Async-first** — every exchange wrapper is `asyncio`-based; concurrent fetch/order is natural / 모든 래퍼 asyncio 기반
- **Factory pattern** — `create_exchange("hyperliquid", **keys)` unified entry point / 통일된 팩토리 진입점
- **Isolated processes** — SDKs that do sync HTTP in `__init__` (Lighter, GRVT, Reya, Bulk) are spawned as subprocess bridges to avoid main event loop deadlocks / sync HTTP SDK는 subprocess 격리
- **Triple-lock for live** — `ENABLED=true` + `DRY_RUN=false` + `LIVE_CONFIRM=true` all required to send real orders / 라이브 전환은 3단계 락
- **Kill switches** — every automation has (a) kill file, (b) daily PnL stop, (c) consecutive-failure cap, (d) min collateral floor / 모든 자동화에 4종 킬스위치
- **State persistence** — `trader_state.json` style file-based managers so bots restore positions on restart / 파일 기반 상태 매니저로 재시작 복원

### Sanitization Notice / 보안 안내

All secrets, private keys, and personal wallet addresses have been stripped from this repository. Placeholders use the form `<...>` or `<DESCRIPTOR>`. You must inject your own credentials via environment variables — never commit them.

이 레포에서는 모든 시크릿/프라이빗 키/개인 지갑 주소가 제거되어 있습니다. 자리표시자는 `<...>` 또는 `<DESCRIPTOR>` 형태입니다. 본인 크리덴셜은 환경변수로 주입하시고, 절대 커밋하지 마세요.

---

## Disclaimer / 면책

**English**: Nothing in this repository is financial, legal, or tax advice. Crypto trading involves substantial risk of loss. Code samples are illustrative; do not run them with real funds without auditing them yourself. The author has no fiduciary duty to readers. Past performance — yours, mine, or anyone else's — does not predict future returns. Use of any exchange link is at your own discretion.

**한국어**: 이 저장소의 어떤 내용도 투자/법률/세무 조언이 아니다. 크립토 트레이딩은 큰 손실 가능성을 동반한다. 코드 예시는 어디까지나 설명용이며, 직접 감사하지 않은 코드를 실자금으로 돌리지 마라. 저자는 독자에 대한 어떤 신탁의무도 없다. 과거 성과는 — 것이든 타인 것이든 — 미래를 보장하지 않는다. 거래소 사용과 가입 링크 클릭은 전적으로 본인 판단이다.

## License

Content: CC BY-NC 4.0 (attribution, non-commercial). Code snippets within: MIT.
콘텐츠: CC BY-NC 4.0 (출처 표기, 비상업적 이용). 본문 내 코드 스니펫: MIT.

## Contact

GitHub: [@coinmage777](https://github.com/coinmage777)
