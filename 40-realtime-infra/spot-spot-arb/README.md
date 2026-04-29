# spot-spot-arb

KR (빗썸/업비트) ↔ 글로벌 (Binance/Bybit/Gate/MEXC/KuCoin) ↔ DEX (PancakeSwap) 차익거래 통합 시스템. FastAPI backend + React frontend + Rust services. Kimchi premium 추적, NTT 토큰 스캐너, hack/halt detector, dex trader.

## 핵심 기능

### Backend (FastAPI)

- **Multi-venue BBO poller**: 8개 거래소 동시 ticker fetch (3초 주기)
- **Gap calculator**: spread + impact gap 계산 (Rust 마이크로서비스로 가속)
- **Wallet tracker**: 6개 wallet (Circle/Alameda/Wintermute/Jump/Tether-multisig 등) 온체인 행동 감시
- **Listing hunter**: KR 거래소 신상장 공지 폴링 → 글로벌 perp short 헷지 트리거
- **NTT scanner**: Wormhole NTT 토큰 manager 주소 watchlist
- **Hack/halt detector**: Binance 가격 급변 감지 (5초 폴링)
- **Bridge client**: Stargate / Across V3 cross-chain 자동화
- **DEX trader**: PancakeSwap V2 직접 swap (자금 이동)

### Frontend (React + Vite + TS)

- Live KIMP (kimchi premium) dashboard
- Venue별 가격 매트릭스
- Signal alert feed
- SNATCH-style 디자인 (cream/yellow/mono palette)

### Rust services

- **gap-recorder** (rust-services 모듈 사용): SQLite WAL batched insert
- **bbo-loop** (분석만): IO bound이라 Rust 마이그 ROI 낮음. Python httpx 최적화 권고

## 파일 구조

```
spot-spot-arb/
├── backend/
│   ├── main.py                — FastAPI 진입
│   ├── config.py              — 거래소/wallet/scanner 설정
│   ├── services/
│   │   ├── poller.py          — 3초 BBO loop
│   │   ├── gap_calculator.py  — gap 계산 (Rust shim)
│   │   ├── wallet_tracker.py  — 온체인 wallet 감시
│   │   ├── listing_hunter.py  — KR 상장 공지 폴링
│   │   ├── ntt_scanner.py     — Wormhole NTT manager
│   │   ├── hack_halt_detector.py — Binance 급변 감지
│   │   └── consensus_mirror.py — multi-venue 가격 합의
│   ├── exchanges/
│   │   ├── manager.py         — 거래소 통합 매니저
│   │   ├── bithumb.py         — 빗썸 spot
│   │   ├── upbit.py           — 업비트 spot
│   │   ├── binance.py         — Binance USDM
│   │   └── ... (각 venue)
│   ├── strategies_minara/     — 검증 전략 paper trader (RSI70, SuperTrend 등)
│   └── trading/
│       ├── dex_trader.py      — PancakeSwap V2 swap
│       └── bridge_client.py   — Stargate/Across bridge
├── frontend/
│   ├── src/
│   │   ├── App.tsx
│   │   ├── components/
│   │   │   ├── snatch/        — SNATCH 스타일 컴포넌트 (KPIBlock, VenueBadge, TabNav 등)
│   │   │   ├── PerpDexDashboard.tsx
│   │   │   └── ...
│   │   └── hooks/
│   ├── package.json
│   └── vite.config.ts
└── rust-services/
    ├── gap-recorder/         — SQLite WAL batched (169K rows/sec)
    └── bbo-loop/             — (보류, ROI 낮음)
```

## 사용 예시

### Backend
```bash
cd backend
uvicorn main:app --reload --port 8000
# OR (production)
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2
```

### Frontend
```bash
cd frontend
npm install
npm run dev   # vite dev server (http://localhost:5173)
npm run build # production build
```

### Paper traders (strategies_minara)
```bash
python strategies_minara/paper_trader_rsi70_btc_4h.py
```

## 환경변수

```env
# 거래소 API
BITHUMB_API_KEY=<...>
BITHUMB_SECRET=<...>
UPBIT_API_KEY=<...>
UPBIT_SECRET=<...>
BINANCE_API_KEY=<...>
BINANCE_SECRET=<...>
BYBIT_API_KEY=<...>
BYBIT_SECRET=<...>

# DB
DB_URL=sqlite:///data/kimchi_arb.db
GAP_HISTORY_DB=data/gap_history.db

# Bridge
PRIVATE_KEY=<EVM_PK>
BSC_RPC=https://bsc-dataseed1.binance.org

# Frontend
VITE_API_URL=http://localhost:8000

# Telegram
TELEGRAM_BOT_TOKEN=<...>
TELEGRAM_CHAT_ID=<...>
```

## 의존성

### Backend
- `fastapi`, `uvicorn`
- `ccxt`, `httpx`
- `sqlalchemy`, `aiosqlite`
- `web3.py`

### Frontend
- React 18+, TypeScript, Vite
- Tailwind CSS (SNATCH 스타일)
- TanStack Query, axios
