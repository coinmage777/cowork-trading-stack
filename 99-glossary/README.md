# Glossary (KO/EN) — Crypto Trading + AI Coding

> 한 줄 요약: 본 레포 전반에서 자주 등장하는 암호화폐 트레이딩 + AI 코딩 + 인프라 핵심 용어 사전. 한국어/영어 병기, 검색 (Ctrl+F) 친화적 thematic group.

목차:
- [1. Trading Mechanics (트레이딩 메카닉)](#1-trading-mechanics-트레이딩-메카닉)
- [2. Order Types & Flags (주문 타입과 플래그)](#2-order-types--flags-주문-타입과-플래그)
- [3. Risk & Performance (리스크와 성과 지표)](#3-risk--performance-리스크와-성과-지표)
- [4. Strategy Concepts (전략 개념)](#4-strategy-concepts-전략-개념)
- [5. Indicators (지표)](#5-indicators-지표)
- [6. Exchange-Specific (거래소별)](#6-exchange-specific-거래소별)
- [7. Onchain & Wallets (온체인과 지갑)](#7-onchain--wallets-온체인과-지갑)
- [8. Airdrops & Points (에어드랍과 포인트)](#8-airdrops--points-에어드랍과-포인트)
- [9. AI Coding (AI 코딩, vibe coding)](#9-ai-coding-ai-코딩-vibe-coding)
- [10. Python & Async (파이썬과 비동기)](#10-python--async-파이썬과-비동기)
- [11. Infrastructure (인프라)](#11-infrastructure-인프라)
- [12. Networking & Resilience (네트워크와 회복성)](#12-networking--resilience-네트워크와-회복성)
- [13. Secrets & Config (비밀키와 설정)](#13-secrets--config-비밀키와-설정)

---

## 1. Trading Mechanics (트레이딩 메카닉)

**Perp DEX (영어)** / 무기한 선물 탈중앙거래소
> 만기 없는 선물 (perpetual futures) 을 온체인 또는 하이브리드로 거래하는 DEX. funding rate 으로 spot 가격에 anchoring. 본 레포의 `20-exchange-wrappers/` 가 22+ perp DEX 를 추상화.

**CLOB (영어)** / 중앙지정가호가장
> Central Limit Order Book. 전통적 호가장 방식. Hyperliquid, Lighter, GRVT 등 대부분 perp DEX 가 채택.

**Maker (영어)** / 메이커
> 호가장에 유동성을 *공급* 하는 주문 (limit 미체결 → 거래장에 남음). 일반적으로 수수료가 낮거나 리베이트.

**Taker (영어)** / 테이커
> 호가장의 유동성을 *소비* 하는 주문 (market 또는 즉시 체결되는 limit). maker 보다 수수료 높음.

**Mark Price (영어)** / 마크 프라이스
> 청산/PnL 계산 기준이 되는 공식 가격. index price + funding 보정. mid price 와는 다름.

**Mid Price (영어)** / 미드 프라이스
> Best bid 와 Best ask 의 산술평균. 호가장 기준 fair value 추정치.

**Funding Rate (영어)** / 펀딩 레이트
> Perp 가 spot 에 anchoring 하도록 long ↔ short 이 주기적으로 (보통 1h/8h) 주고받는 비율. positive 면 long 이 short 에 지급.

**Leverage (영어)** / 레버리지
> 담보 대비 포지션 명목가 비율. 5x = 담보 $1000 으로 명목가 $5000 포지션.

**Liquidation (영어)** / 청산
> 마진 비율이 maintenance margin 아래로 떨어져 거래소가 강제로 포지션을 닫는 사건. 자산을 잃음.

**Notional (영어)** / 노셔널 / 명목가
> 포지션 사이즈 × 가격. 예: 0.1 BTC @ $70000 → notional $7000.

**Slippage (영어)** / 슬리피지
> 주문 체결가가 의도한 가격과 벌어지는 정도. market order 시 호가장 깊이 부족이 주요 원인.

**Spread (영어)** / 스프레드
> Best ask − best bid. tight 한 시장일수록 좁음. MM 의 수익 원천.

**BBO (영어)** / 베스트 비드 오퍼
> Best Bid and Offer. 호가장의 최우선 매수/매도 가격 한 쌍.

**Orderbook (영어)** / 호가장
> 매수/매도 limit order 의 가격별 수량을 모아둔 데이터 구조.

**Open Interest (OI)** / 미결제약정
> 시장에 열려 있는 perp 포지션의 총 명목가. 청산되거나 닫혀야 줄어듦.

**Market Maker (MM)** / 시장조성자
> 양방향 호가를 지속적으로 제공하여 spread 수익 또는 거래소 리워드 (uptime program) 를 노리는 참여자.

**Volume Farming (영어)** / 볼륨 파밍
> 거래소의 거래량 기반 리워드/포인트를 얻기 위해 인위적 볼륨을 만들어내는 전략 (보통 self-trading 또는 hedge against MM).

---

## 2. Order Types & Flags (주문 타입과 플래그)

**Limit Order (영어)** / 지정가 주문
> 특정 가격 이상/이하로만 체결하는 주문.

**Market Order (영어)** / 시장가 주문
> 즉시 체결을 우선하는 주문. taker fee.

**Hard Stop (영어)** / 하드 스탑
> 절대 price level 에서 무조건 청산하는 stop loss.

**Trailing Stop (영어)** / 트레일링 스탑
> 가격 상승을 따라 stop 가격이 함께 올라가는 동적 stop.

**IOC (영어)** / 즉시체결-잔량취소
> Immediate-Or-Cancel. 즉시 체결 가능한 만큼만 체결, 잔량은 자동 취소.

**GTC (영어)** / 취소시까지유효
> Good-Til-Cancelled. 취소하기 전까지 호가장에 남음.

**Post-Only (영어)** / 포스트온리
> maker 로만 체결되도록 강제. taker 로 체결될 수 있는 상황이면 취소 (slippage 방지).

**Reduce-Only (영어)** / 리듀스온리
> 포지션 *축소* 만 허용. 같은 방향 추가 진입 방지. risk control 핵심 플래그.

**Cloid (영어)** / 클라이언트 주문 ID
> Client Order ID. 클라이언트가 발급하는 주문 식별자. HL 에서는 builder code attribution 의 prefix 로 사용 (`0x4d455243...` = "MERC" → Miracle).

---

## 3. Risk & Performance (리스크와 성과 지표)

**R:R (Risk:Reward)** / 손익비
> 한 트레이드의 손실 한도 대비 목표 수익 비율. 1:2 = 손실 $1 risk 로 $2 노림.

**WR (Win Rate)** / 승률
> 익절 트레이드 비율. 단독으로는 무의미 (R:R 과 함께 봐야).

**Profit Factor (영어)** / 수익 팩터
> 총 수익 / 총 손실 절대값. > 1.0 이면 흑자, > 1.5 이면 견실.

**Sharpe Ratio (영어)** / 샤프 지수
> (수익률 − 무위험 수익률) / 수익률 표준편차. 위험조정 수익 대표 지표.

**Drawdown (영어)** / 낙폭
> 최고점 대비 현재 자산 감소율. MDD = Max Drawdown.

**PnL (영어)** / 손익
> Profit and Loss. realized (실현) vs unrealized (미실현) 구분.

**Sizing (영어)** / 포지션 사이징
> 트레이드당 risk 노출 결정 로직. fixed fractional, Kelly 등.

---

## 4. Strategy Concepts (전략 개념)

**Mean Reversion (영어)** / 평균 회귀
> 가격이 평균에서 벗어나면 다시 돌아온다는 가정. bollinger band, Z-score 등 기반.

**Momentum (영어)** / 모멘텀
> 추세 지속 가정. breakout, MA cross 등.

**Pair Trading (영어)** / 페어 트레이딩
> 두 자산의 spread (또는 비율) 의 mean reversion 을 노리는 시장중립 전략. 본 레포 `30-strategy-patterns/_combined/pair_trader.py`.

**Funding Arb (영어)** / 펀딩 차익거래
> Perp 의 funding 을 받기 위해 spot 또는 다른 perp 와 헤지하여 가격 노출 0 으로 funding 만 수익.

**Cross-Venue Arb (영어)** / 거래소간 차익거래
> 두 venue 간 가격 차이를 동시 매매로 잡음. 본 레포 `40-realtime-infra/cross-venue-arb-scanner`.

**DCA (영어)** / 분할매수
> Dollar Cost Averaging. 정해진 간격으로 일정 금액씩 매수.

**Regime Filter (영어)** / 레짐 필터
> 시장 상태 (trend / range / vol regime) 를 식별하여 전략을 on/off. 백테스트 과적합 방지의 핵심.

**Backtesting (영어)** / 백테스팅
> 과거 데이터로 전략을 시뮬레이션. lookahead bias / survivorship bias 주의.

**Overfitting (영어)** / 과적합
> 백테스트 데이터에만 잘 맞고 실전에서 깨지는 모델/파라미터 상태.

**Delta-Neutral (영어)** / 델타 중립
> Long 과 short 포지션이 명목가 (notional) 기준으로 상쇄되어 가격 방향 노출이 0 인 상태. 펀딩 차익/볼륨 파밍 전략의 기반.

---

## 5. Indicators (지표)

**RSI (영어)** / 상대강도지수
> Relative Strength Index. 0–100, 70+ 과매수 / 30− 과매도. 본 레포 전략에서 사용.

**ATR (영어)** / 평균진폭
> Average True Range. 변동성 측정 지표. position sizing 에 자주 사용.

**EMA / SMA (영어)** / 지수/단순 이동평균
> Exponential / Simple Moving Average. 추세 판단의 기본.

---

## 6. Exchange-Specific (거래소별)

**Hyperliquid (HL)** / 하이퍼리퀴드
> CLOB 기반 perp DEX. 자체 L1 + msgpack action signing. HIP-3 builder 시스템 보유.

**HIP-3 / Builder Code** / HIP-3 빌더 코드
> Hyperliquid Improvement Proposal 3. builder 가 자기 sub-DEX 를 만들 수 있게 함. cloid prefix 로 attribution. asset_id = `140000 + perpDexIndex`.

**Agent Key (영어)** / 에이전트 키
> 메인 wallet 과 분리된 거래 전용 hot key. 출금/approve 권한 없음. HL 의 핵심 보안 모델.

**Lighter (영어)** / 라이터
> CLOB 기반 perp DEX. SDK 가 sync HTTP in `__init__` 라서 subprocess 격리 필수 (대표 deadlock 사례).

**GRVT (영어)** / 그라빗
> CLOB perp DEX. `account_id` 가 numeric, EIP-712 서명, symbol 포맷 `BTC_USDT_Perp`.

**Standx (영어)** / 스탠드엑스
> BSC perp DEX. EIP-712 personal_sign 인증, MM uptime program. **Signature `0x` Prefix Bug**: `eth_account` 0.13+ 에서 `signature.hex()` 가 `0x` 를 떨어뜨려 서버가 인식 못 함 → wrapper 단 monkey-patch 로 prepend 필수.

**Aster (영어)** / 아스터
> BSC perp + spot DEX. Binance-style API + USDT-M futures + builder rebate.

**MM Uptime Program** / MM 가동시간 리워드
> Standx 등에서 mark price 근처 양면 호가를 일정 시간 이상 유지하면 받는 리워드.

---

## 7. Onchain & Wallets (온체인과 지갑)

**EOA (영어)** / 외부소유계정
> Externally Owned Account. 일반 사용자 지갑 (private key 보유). contract address 와 구분.

**Gas (영어)** / 가스
> 트랜잭션 실행 비용 (chain native token 으로 지급).

**Nonce (영어)** / 논스
> EOA 의 트랜잭션 시퀀스 카운터. 누락/중복 시 트랜잭션 거부.

**RPC (영어)** / RPC 노드
> Remote Procedure Call. chain 데이터 read/write 진입점 (예: Alchemy, Infura, public RPC).

**Chain ID (영어)** / 체인 ID
> EVM chain 식별자. mainnet=1, BSC=56, Polygon=137, Arbitrum=42161, RISE=4153.

**BSC (영어)** / 바이낸스 스마트 체인
> Binance Smart Chain. EVM 호환. Aster, Standx 등 perp DEX 호스트.

**Arbitrum (영어)** / 아비트럼
> Optimistic rollup L2. Reya 의 base.

**Polygon (영어)** / 폴리곤

**Solana / SPL** / 솔라나 / SPL 토큰
> Non-EVM L1. SPL = Solana Program Library token (ERC-20 의 솔라나 대응). Backpack 등이 사용.

**Decimals (영어)** / 데시멀
> 토큰의 소수점 자릿수. USDC=6, ETH=18 등. 실수하면 1000x 차이 사고.

**Approve (영어)** / 어프루브
> ERC-20 spender 권한 부여 트랜잭션. infinite approve 는 보안 위험.

---

## 8. Airdrops & Points (에어드랍과 포인트)

**Airdrop (영어)** / 에어드랍
> 프로토콜이 사용자에게 토큰을 무상 분배.

**Snapshot (영어)** / 스냅샷
> Eligibility 결정 시점의 on-chain 상태 캡처.

**Eligibility (영어)** / 자격
> 에어드랍/포인트 수령 자격 조건 (활동 기준 등).

**Multiplier (영어)** / 부스터
> 활동량/시간/가입 코드 등에 따라 포인트가 곱해지는 가중치.

**Season (영어)** / 시즌
> 포인트 프로그램의 운영 단위 (예: Season 1, 2). TGE 시점에 정산.

**TGE (영어)** / 토큰생성이벤트
> Token Generation Event. 토큰이 처음 발행되어 포인트 → 토큰 환산이 일어나는 시점.

**Sybil (영어)** / 시빌
> 한 사람이 여러 지갑으로 자격을 부풀리는 행위. 운영진 detection 시 박탈.

---

## 9. AI Coding (AI 코딩, vibe coding)

**Vibe Coding (영어)** / 바이브 코딩
> 정확한 스펙 없이 LLM 과 대화하며 코드를 만들어가는 스타일. Andrej Karpathy 가 명명. 본 레포의 핵심 작업 방식.

**Claude Code (영어)** / 클로드 코드
> Anthropic 의 공식 CLI. agentic 코딩 환경.

**Agent / Subagent** / 에이전트 / 서브에이전트
> Agent: 도구를 자율적으로 사용하여 다단계 작업을 수행하는 LLM 인스턴스. Subagent: 메인 agent 가 위임하여 분리된 컨텍스트로 실행하는 보조 agent.

**MCP Server (영어)** / MCP 서버
> Model Context Protocol server. agent 가 외부 도구/리소스에 표준화된 방식으로 접근하게 함.

**Slash Command (영어)** / 슬래시 커맨드
> Claude Code/Cursor 에서 `/cmd` 로 호출하는 사용자 정의 명령.

**Plan Mode (영어)** / 계획 모드
> Claude Code 가 변경 전 계획만 제시하는 모드. write 안 함.

**/loop (영어)** / /loop
> 본 레포에서 사용되는 반복 작업 슬래시 커맨드. recurring task 자동화.

**Prompt Cache (영어)** / 프롬프트 캐시
> 시스템 프롬프트/긴 컨텍스트를 caching 하여 latency/cost 절감. Anthropic API 의 cache_control.

**CLAUDE.md** / CLAUDE.md
> Claude Code 가 자동으로 읽는 프로젝트 컨텍스트 파일. 프로젝트 루트에 둠.

**Hook (영어)** / 훅
> 특정 이벤트 (PreToolUse, PostToolUse, Stop 등) 시 실행되는 커스텀 스크립트. Claude Code settings.json 에서 설정.

**Status Line (영어)** / 상태줄
> 터미널 하단의 모델/상태 표시 줄. 커스터마이즈 가능.

**Hallucination (영어)** / 환각
> 모델이 사실이 아닌 내용을 자신 있게 생성하는 현상.

**Gaslight My AI Pattern** / AI 가스라이팅 패턴
> 모델 답변을 일부러 의심하며 "정말? 다시 봐" 식으로 push 해서 더 깊은 검증을 유도. 본 레포 디버깅의 핵심 패턴.

**Root Cause vs Symptom** / 근본 원인 vs 증상
> 모델이 증상만 patch 하는 경향 → root cause 까지 파라고 명시 강요.

---

## 10. Python & Async (파이썬과 비동기)

**venv (영어)** / 가상환경
> Python 가상환경. `python -m venv .venv`. 의존성 격리. 본 사용자 기본 (단순한 경우 Docker 보다 venv 우선).

**asyncio / async/await** / 비동기 표준 라이브러리
> Python 의 비동기 표준. event loop 단일 스레드 코루틴 스케줄러.

**Sync HTTP in `__init__`** / 생성자 sync HTTP (deadlock)
> 클래스 생성자에서 `requests.get` 같은 동기 HTTP 호출 → asyncio loop 블록 → deadlock. **Lighter SDK** 가 대표 사례.

**Subprocess Bridge** / 서브프로세스 브리지
> sync 코드를 별도 프로세스로 격리하고 stdin/stdout JSON 으로 통신. `10-foundation-modules/subprocess-bridge/`.

**Python 3.10/3.11/3.12** / 파이썬 버전
> 본 레포 거래소들이 요구하는 버전 다양. Reya 는 3.12 필수.

---

## 11. Infrastructure (인프라)

**tmux (영어)** / tmux
> 터미널 멀티플렉서. SSH 끊겨도 세션 유지. 본 운영의 기본.

**systemd (영어)** / systemd
> Linux init/service manager. `systemctl start/stop/status`.

**SIGHUP / SIGTERM / SIGKILL** / 시그널
> SIGHUP=1 (hangup, reload), SIGTERM=15 (graceful), SIGKILL=9 (forced). 본 레포 봇은 SIGTERM 에 graceful shutdown 보장.

**Contabo (영어)** / 콘타보
> 본 사용자가 사용하는 VPS 호스팅 (perp-dex-bot 운영지).

**ComfyUI (영어)** / 컴피UI
> Stable Diffusion 그래프 기반 UI. Desktop 버전은 port 8000.

---

## 12. Networking & Resilience (네트워크와 회복성)

**WebSocket (WS)** / 웹소켓
> 양방향 지속 연결 프로토콜. 거래소 실시간 데이터의 표준.

**Reconnect (영어)** / 재연결
> WS 끊김 후 다시 붙는 로직.

**Exponential Backoff + Jitter** / 지수 백오프 + 지터
> 1, 2, 4, 8, 16... 처럼 지수적으로 재시도 간격 증가 + random noise 추가하여 thundering herd 방지. HL WS 에는 `[2,5,10,30,60,180,300]` 같은 시퀀스가 효과적.

**Heartbeat (영어)** / 하트비트
> 연결 유지를 확인하는 주기적 ping.

**API Rate Limit** / API 속도 제한
> 시간당 최대 요청 수. 초과 시 429 반환. backoff 필수.

**Idempotency (영어)** / 멱등성
> 같은 요청을 여러 번 보내도 결과가 한 번 보낸 것과 같음. 주문 재시도에 중요.

---

## 13. Secrets & Config (비밀키와 설정)

**.env (영어)** / .env 파일
> 환경변수를 담는 파일. git 에 commit 금지.

**Env Var (영어)** / 환경변수
> 프로세스 환경에 박힌 key=value. 비밀키 표준 보관 방식.

**API Key (영어)** / API 키
> 거래소가 발급하는 인증 토큰 쌍 (key + secret).

**Private Key (PK)** / 프라이빗 키
> EOA 의 비대칭 키쌍 중 비밀 부분. 누출 = 자산 전부 도둑맞음.

**Plaintext PK Leak** / 평문 PK 유출
> 코드/설정 파일에 PK 가 평문으로 박혀 git 에 노출되는 사고. 본 사용자의 perp-dex-bot config.yaml 에 과거 2 건 있었음 (line 384, 486).

**Hot vs Cold Wallet** / 핫 / 콜드 월렛
> Hot: 온라인 거래용 지갑 (최소한의 자산만). Cold: 오프라인 보관 지갑 (메인 자산).

**가입 코드 (Referral Code)**
> 가입 시 추천인 식별자. 본 사용자의 가입 코드: Lighter `GMYPZWQK69X4`, Nado `NX9LLaL`.

---

## 부록: 본 레포 모듈 인덱스

- `10-foundation-modules/` — 공통 인프라 (subprocess bridge 등)
- `20-exchange-wrappers/` — 거래소별 wrapper (HL, Lighter, GRVT, Aster, Standx 개별 + `_combined/` 22 개 union)
- `30-strategy-patterns/` — 전략 (pair trader, volume farmer 등)
- `40-realtime-infra/` — 실시간 인프라 (cross-venue arb scanner)
- `50-rust-acceleration/` — Rust 가속 핫패스 (advanced)
- `60-ops-runbooks/` — 운영 (telegram control 등)
- `30-strategy-patterns/`, `40-realtime-infra/` — 전략·실시간 인프라 모듈
- `99-glossary/` — 본 사전

---

> 이 사전은 본 레포 작업 중 자주 등장하는 핵심 용어 약 100 개로 좁힌 버전입니다. 더 깊이 들어가는 niche 용어는 각 모듈 README 또는 case study 문서에서 다룹니다.
