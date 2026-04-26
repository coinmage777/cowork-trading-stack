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

## Disclaimer / 면책

**English**: Nothing in this repository is financial, legal, or tax advice. Crypto trading involves substantial risk of loss. Code samples are illustrative; do not run them with real funds without auditing them yourself. The author has no fiduciary duty to readers. Past performance — yours, mine, or anyone else's — does not predict future returns. Use of any exchange or referral link is at your own discretion.

**한국어**: 이 저장소의 어떤 내용도 투자/법률/세무 조언이 아니다. 크립토 트레이딩은 큰 손실 가능성을 동반한다. 코드 예시는 어디까지나 설명용이며, 직접 감사하지 않은 코드를 실자금으로 돌리지 마라. 저자는 독자에 대한 어떤 신탁의무도 없다. 과거 성과는 — 것이든 타인 것이든 — 미래를 보장하지 않는다. 거래소 사용과 레퍼럴 클릭은 전적으로 판단이다.

## License

Content: CC BY-NC 4.0 (attribution, non-commercial). Code snippets within: MIT.
콘텐츠: CC BY-NC 4.0 (출처 표기, 비상업적 이용). 본문 내 코드 스니펫: MIT.

## Contact

GitHub: [@coinmage777](https://github.com/coinmage777)
