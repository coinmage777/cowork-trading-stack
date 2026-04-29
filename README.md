# Cowork Trading Stack

> 바이브 코딩 입문 가이드 — Claude Code 와 Codex 로 실제 굴러가는 프로덕션 시스템을 만드는 법.
> 사례 연구는 6 개월 넘게 라이브로 운영 중인 크립토 트레이딩 인프라입니다 (다수의 거래소 래퍼, 실시간 차익 스캐너, Rust 가속 핫패스).

---

## 이 저장소가 무엇인가

흔한 "Claude 프롬프팅 튜토리얼" 도, "확정 수익 봇" 모음도 아닙니다. 혼자서 AI 와 진짜 시스템을 만들 때 매일이 어떻게 흘러가는지에 대한 기록입니다. 그 답이 6 개월짜리 크립토 트레이딩 스택이 됐을 뿐입니다 — 솔로 개발자가 월요일에 짠 코드를 금요일에 실자금으로 검증할 수 있는 거의 유일한 환경이기 때문입니다. 여기서 배운 패턴은 트레이딩이 아닌 영역에도 그대로 옮겨 쓸 수 있습니다.

**이런 분들에게 추천합니다.** AI 로 토이 프로젝트는 잘 짜지는데 production 까지 못 가는 분, 솔로로 멀티 모듈 시스템을 6 개월 이상 끌어가야 하는 분, "프롬프트는 되는데 출하 단계에서 깨진다" 는 단계에 있는 분.

---

## 왜 크립토 트레이딩이 vibe coding 케이스 스터디로 좋은가

실자금이 걸려 있으면 "대충 돌아가는 듯하다" 로 넘어갈 수 없습니다. 거래소 래퍼·전략·스케줄러·노티파이어처럼 한 세션에 AI 가 잘 만드는 크기로 모듈이 자연스럽게 나뉘고, ship → observe → adjust 사이클이 매주 강제됩니다. 공개 API 와 지갑 하나로 누구나 재현할 수 있고, 봇 한 개의 폭발 반경이 작아 실험 부담도 낮습니다.

---

## 무엇을 배우는가

- 큰 시스템을 **AI 가 잘 만들 수 있는 단위로 쪼개는 법** — "트레이딩 봇 만들어 줘" 가 아니라 "이 6 개 메서드 인터페이스를 구현하는 BackpackExchange 클래스 + WS reconnect 스텁 짜 줘"
- **출하 가능한 코드를 뽑는 프롬프트 패턴** — Plan → Code → Review → Debug, 각 단계별로 다른 마인드셋
- **메모리 시스템** — `CLAUDE.md` (프로젝트별) + auto-memory (세션 간) 로 다중 월 단위 상태 유지
- **AI 가 만든 버그 디버깅** — Gaslight My AI 패턴 (라이벌 모델 프레이밍) 등
- **라이브 시스템 삼중 락** — `ENABLED=true` + `DRY_RUN=false` + `LIVE_CONFIRM=true` 모두 켜져야 실주문 발사
- **자동화 4 종 킬 스위치** — kill file / 일일 PnL stop / 연속 실패 cap / 최소 자본 floor

---

## 5 분 시작 (Quick Start)

처음 오신 분이라면 다음 세 문서만 읽으셔도 vibe coding 의 핵심은 잡힙니다.

1. [`00-vibe-coding-basics/01-claude-code-setup.md`](00-vibe-coding-basics/01-claude-code-setup.md) — Claude Code 셋업 (10 분)
2. [`00-vibe-coding-basics/02-prompting-patterns.md`](00-vibe-coding-basics/02-prompting-patterns.md) — 실전 프롬프트 패턴
3. [`30-strategy-patterns/_combined/README.md`](30-strategy-patterns/_combined/README.md) — 전략 모듈을 조합하는 방식

코드를 한 번 직접 돌려보고 싶으시면 가장 가벼운 모듈로 5 분 안에 띄워볼 수 있습니다.

```bash
git clone https://github.com/coinmage777/cowork-trading-stack.git
cd cowork-trading-stack/30-strategy-patterns/volume-farmer/
python -m venv venv && source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                # API 키 채우고 DRY_RUN=true 로 돌려보기
```

모듈마다 자체 README 와 의존성을 가집니다. 최상위에 "한 번에 다 돌리는 명령" 은 없습니다 — 안전하지도, 가능하지도 않기 때문입니다.

---

## 저장소 구조

```
00-vibe-coding-basics/    AI 코딩 입문 7 장 (Matt Pocock craftsmanship + deep modules 포함)
10-foundation-modules/    드롭인 빌딩 블록 (kill-switch, notifier, auto-scaler 등 11 개)
20-exchange-wrappers/     22 개 거래소 통합 + setup-guides
30-strategy-patterns/     전략 템플릿 (pair, MM, DCA, volume-farmer, aster-spot-buyer, backtest)
40-realtime-infra/        실시간 스캐너 (cross-venue arb, kimp listing, spot-spot, wallet trackers)
50-rust-acceleration/     PyO3 핫패스 (advanced)
60-ops-runbooks/          tmux / systemd / telegram-control 운영 5 종 + 도구
80-obsidian-vault/        Obsidian 활용법 (구조 / 템플릿 / Dataview / AI 연동 / 콘텐츠 파이프라인)
99-glossary/              용어 정리
ko/                       장문 한국어 가이드 (10 챕터)
```

**누구에게 어느 폴더가 맞는가.** AI 로 새 시스템을 짜는 빌더라면 `10-foundation-modules/` + `30-strategy-patterns/` 를 먼저 봅니다. 이미 돌고 있는 시스템을 안정적으로 운영하려는 분은 `60-ops-runbooks/` + `40-realtime-infra/` 가 출발점입니다. 성능을 더 짜내고 싶은 고급 사용자는 `50-rust-acceleration/` 과 `20-exchange-wrappers/_combined/` 의 격리 프로세스 패턴이 흥미로울 것입니다.

---

## 장문 가이드 (한국어)

본격 내러티브는 [`ko/`](ko/) 에 있습니다. 입문 → cowork 시작 → 코드/메모리 → 운영 원칙 → 로드맵 순서로 8 개 챕터가 이어집니다. 트레이딩 deep-dive 가 따로 필요하면 `ko/_archive/` 도 함께 보시기 바랍니다.

---

## 통합된 거래소

`20-exchange-wrappers/` 의 래퍼는 전략 코드가 돌아가는 substrate 입니다. 11 개 거래소에 가입 링크가 있고, 그 외 Paradex, Backpack, Drift, dYdX, Vest, Bulk 등 가입 링크 없는 거래소도 통합되어 있습니다. API 셋업은 `20-exchange-wrappers/setup-guides/` 를 참고하시기 바랍니다.

- [Hyperliquid](https://app.hyperliquid.xyz/join/COINMAGE) — 메인 perp venue. `50-rust-acceleration/` 에 자체 Rust signing
- [Lighter](https://app.lighter.xyz/?referral=GMYPZWQK69X4) — zk-perp. SDK 가 `__init__` 에서 sync HTTP → subprocess 로 격리
- [Aster](https://www.asterdex.com/en/referral/e70505) — BSC perp + spot. spot leg 가 farmer 헷지에 사용
- [GRVT](https://grvt.io/exchange/sign-up?ref=1O9U2GG) — 격리 프로세스 패턴
- [Pacifica](https://app.pacifica.fi?referral=cryptocurrencymage) — Solana 기반 perp
- [EdgeX](https://pro.edgex.exchange/referral/570254647)
- [Reya](https://app.reya.xyz/trade?referredBy=8src0ch8) — 격리 프로세스 패턴
- [Extended](https://app.extended.exchange/join/COINMAGE)
- [Variational](https://omni.variational.io/?ref=OMNICOINMAGE) — RFQ 모델
- [Standx](https://standx.com/referral?code=coinmage)
- [Nado](https://app.nado.xyz?join=NX9LLaL) — pair scalper 템플릿이 `30-strategy-patterns/` 에

---

## 아키텍처 원칙

머리로 정한 게 아니라 깨지면서 정착한 패턴입니다.

- **Async-first** — 모든 거래소 래퍼가 `asyncio` 기반. concurrent fetch / order 가 디폴트.
- **Factory 패턴** — `create_exchange("hyperliquid", **keys)` 가 유일한 진입점. 새 거래소 추가 시 caller 코드 변경 0.
- **나쁜 SDK 는 격리 프로세스** — Lighter, GRVT, Reya, Bulk 모두 `__init__` 에서 sync HTTP 로 메인 루프를 막아서, subprocess bridge 로 분리합니다.
- **삼중 락** — `ENABLED=true` + `DRY_RUN=false` + `LIVE_CONFIRM=true` 셋 다 독립으로 켜져야 실주문 발사.
- **4 종 킬 스위치** — kill file / 일일 PnL stop / 연속 실패 cap / 최소 자본 floor.
- **상태 영속화** — `trader_state.json` 류 파일 매니저. 재시작 시 포지션 / 미체결 / 카운터를 클린 복원합니다.
- **텔레그램 = control plane** — `/status`, `/pnl`, `/balance`, `/positions`, `/restart`, `/reload`, `/close`, `/kill`, `/revive`. 5 초 안에 폰으로 죽일 수 없으면 production 이 아닙니다.

---

## 면책

이 저장소의 어떤 내용도 투자 / 법률 / 세무 조언이 아닙니다. 크립토 트레이딩은 큰 손실 가능성을 동반하며, 코드 예시는 설명용입니다. 직접 감사하지 않은 코드를 실자금으로 돌리지 마시기 바랍니다. 저자는 독자에 대한 어떤 신탁의무도 없고, 과거 성과는 미래를 보장하지 않습니다. 거래소 사용과 가입 링크 클릭은 본인 판단입니다.

---

## 라이선스

- 콘텐츠 (장문 가이드 / 리드미): **CC BY-NC 4.0** — 출처 표기 + 비상업적 이용
- 코드 (모듈 디렉터리 내 모든 것): **MIT**

---

## 채널

- GitHub: [@coinmage777](https://github.com/coinmage777)
- YouTube: https://www.youtube.com/@cryptocurrencymage
- Telegram: https://t.me/cryptocurrencymage
- Blog: https://blog.naver.com/coinmage
- Twitter / X: [@coinmage](https://x.com/coinmage)
