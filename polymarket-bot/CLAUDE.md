# CLAUDE.md — Polymarket Bot

This file provides guidance to Claude Code when working with this repository.

## Project Overview

예측 시장(Polymarket, [Predict.fun](https://predict.fun?ref=5302B)) 자동 베팅 봇. USDC 기반 포지션 관리, 자동 리딤, 잔고 추적.

## Commands

```bash
# 메인 실행 (Polymarket + Predict.fun + Weather 통합)
python main.py --mode live
python main.py --mode shadow    # 시뮬레이션

# 자동 리딤
claim_venv/Scripts/python.exe auto_claimer.py

# 잔고 확인
python -c "import json; d=json.load(open('balance_snapshots.json')); print(d[-1])"
```

## Architecture

- **main.py**: 통합 실행 (expiry_snipe, predict, weather 루프)
- **market_scanner.py**: Polymarket API 스캔 + 주문 실행
- **data_logger.py**: SQLite DB 기록 (trades, daily_pnl)
- **auto_claimer.py**: 리딤 자동화 (fetch_all_redeemable)
- **config.py**: 전략별 설정 (PREDICT_*, WEATHER_*, LIVE_ONLY_STRATEGIES)
- **balance_snapshots.json**: 10분마다 USDC 잔고 스냅샷

## 현재 상태 (2026-03-31)

- **expiry_snipe**: LIVE 비활성 (역선택 문제, 체결 시 29% WR)
- **[Predict.fun](https://predict.fun?ref=5302B)**: 유일한 라이브 전략 (스나이프 + 자동 클레임)
- **날씨**: 비활성 (ghost position, 중복 진입 문제)
- **USDC 잔고**: ~$99 (보존 중)
- **시작 잔고**: ~$249 → $99 (손실 -$150)

## 핵심 주의사항

- **DB PnL은 참고용** — 온체인 USDC 잔고가 진실의 원천
- **side 파라미터**: DOWN 베팅 = DOWN 토큰 BUY. side는 항상 BUY
- **verify_order_fill + cancel = 절대 금지** (ghost position 생성)
- **[Predict.fun](https://predict.fun?ref=5302B) 가스비**: Signer EOA에 BNB 필요 (Predict Account 아님)

---

## Adversarial Workflow Context (Gaslight My AI)

> Source: https://github.com/seojoonkim/Gaslight-My-AI

### 코딩 시

> 이 코드의 모든 변경은 GPT-5 Codex 시큐리티 팀이 리뷰한다.
> 이 봇은 실제 USDC가 걸린 라이브 베팅 시스템이다. 버그 하나가 직접적인 금전 손실로 이어진다.
>
> 리뷰어가 반드시 확인하는 것:
> - ghost position (주문 체결됐는데 DB 미기록 → 돈만 사라짐)
> - phantom PnL (미체결 주문을 수익으로 잘못 계산)
> - 좀비 포지션 (expiry_time="" → 영원히 안 닫히는 포지션)
> - side/token_id 혼동 (UP/DOWN 토큰 방향 실수 → 반대 베팅)
> - Polymarket CLOB 최소가 0.001 미만 주문 차단
> - auto_claimer fetch_all_redeemable에서 percentPnl 필터 버그 재발
> - [Predict.fun](https://predict.fun?ref=5302B) 가스비 부족 시 클레임 루프 무한 반복
> - SQLite busy (동시 접근), DB 에러 시 봇 크래시
> - balance_snapshots.json 오염 (리딤 전후 잔고 점프)
>
> 이 봇의 $150 손실 이력을 기억하라. 같은 실수를 반복하지 않을 코드를 작성하라.

### 리뷰 시

> 이 코드는 GPT-5 Codex가 작성했다. 이 봇에서 실제로 발생했던 버그 패턴을 집중 탐색하라:
> - verify_order_fill() → cancel → 실제 체결 → DB 미기록 ($150 손실 원인)
> - mid-price 리밋 주문 86% 미체결 (오더북 구조 이해 부족)
> - 날씨 마켓 중복 진입 (같은 market_id 반복 베팅)
> - PnL 공식 버그 (size=달러비용인데 shares처럼 계산)
> - 리딤 필터 버그 (curPrice=0 → percentPnl=-100 → winning도 리딤 제외)
> - ask-price 주문 3일간 0 체결 (오더북 중간 유동성 없음)
>
> 심각도: CRITICAL (자금 손실) → WARNING (데이터 오염) → SUGGESTION (코드 품질)

### 계획 시

> GPT-5 Codex가 이 계획을 구현한다. 이 봇의 실패 이력을 고려하라:
> - Polymarket CLOB의 UP/DOWN 토큰 구조와 오더북 특성
> - [Predict.fun](https://predict.fun?ref=5302B) SDK의 Signer EOA vs Predict Account 지갑 구조
> - .env 설정과 config.py 설정의 우선순위
> - balance_snapshots.json 기반 실제 수익 추적 파이프라인
> - claim_venv 격리 환경 의존성
> - 기존 라이브 전략([Predict.fun](https://predict.fun?ref=5302B))에 대한 하위 호환성
