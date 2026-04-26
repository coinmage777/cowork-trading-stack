# Exchanges / 거래소

이 인프라에서 사용하는 거래소와 프로토콜 목록입니다. 신규 가입은 아래 링크를 이용하시면 됩니다.

This is the list of exchanges and protocols used in this infrastructure. The links below are sign-up entry points.

---

## Perp DEX

| 거래소 / Exchange | 가입 / Sign-up |
|---|---|
| Hyperliquid (Miracle Trade) | https://miracletrade.com/?ref=coinmage |
| GRVT | https://grvt.io/exchange/sign-up?ref=1O9U2GG |
| Aster | https://www.asterdex.com/en/referral/e70505 |
| EdgeX | https://pro.edgex.exchange/referral/570254647 |
| Reya | https://app.reya.xyz/trade?referredBy=8src0ch8 |
| Pacifica (HIP-3) | https://app.pacifica.fi?referral=cryptocurrencymage |
| Extended | https://app.extended.exchange/join/COINMAGE |
| Variational | https://omni.variational.io/?ref=OMNICOINMAGE |
| Standx | https://standx.com/referral?code=coinmage |
| Based | https://app.based.one/register?ref=COINMAGE |
| Avantis | https://www.avantisfi.com/referral?code=coinmage |
| TradeGenius (HL frontend) | https://www.tradegenius.com/ref/A33HVN |
| Silhouette | https://app.silhouette.exchange/refer/Z208T |
| Theo | https://app.theo.xyz/invite?invite=3830e057-064a-4406-9b50-140c6a92667c |
| Hylo (SOL leverage) | https://hylo.so/leverage?ref=RGCOFI |
| Glider | https://glider.fi/r/0d172cd0 |
| Onre Finance | https://app.onre.finance/earn/leaderboard?ref=FQLFHEYMW |
| xStocks (DeFi) | https://defi.xstocks.fi/points?ref=M7PED292 |

---

## Hyperliquid Builder Codes (HL 기반 거래소)

Hyperliquid 거래 시 `builder_code` 필드에 사용합니다. 거래량에 비례한 builder fee가 분배됩니다.

These are used in the `builder_code` field for Hyperliquid trades. Builder fee is distributed proportionally to volume.

```env
# Miracle Trade (메인)
HL_BUILDER_MIRACLE=0x5eb46BFBF7C6004b59D67E56749e89e83c2CaF82

# Miracle / DreamCash / Bullpen 공유
HL_BUILDER_DREAMCASH=0x4950994884602d1b6c6d96e4fe30f58205c39395

# HyENA (HL HIP-3)
HL_BUILDER_HYENA=0x1924b8561eeF20e70Ede628A296175D358BE80e5

# Based / Based Gold
HL_BUILDER_BASED=0x1924b8561eeF20e70Ede628A296175D358BE80e5

# Dexari
HL_BUILDER_DEXARI=0x4c8731897503f86a2643959cbaa1e075e84babb7

# Liquid
HL_BUILDER_LIQUID=0x0000000bfbf4c62c43c2e71ef0093f382bf7a7b4

# Bullpen
HL_BUILDER_BULLPEN=0xf944069b489f1ebf4c3c6a6014d58cbef7c7009

# Supercexy
HL_BUILDER_SUPERCEXY=0x7975cafdff839ed5047244ed3a0dd82a89866081

# 추가 builder codes
HL_BUILDER_OTHER=0x6D4E7F472e6A491B98CBEeD327417e310Ae8ce48
```

`config.yaml`에 builder_rotation으로 묶어서 사용합니다 / Group into `builder_rotation` in `config.yaml`:
```yaml
hyperliquid_2:
  keys:
    builder_rotation:
      - {name: miracle, builder_code: '0x4950994884602d1b6c6d96e4fe30f58205c39395', fee_pair: {base: '1 50'}, cloid_prefix: '0x4d455243'}
      - {name: dreamcash, builder_code: '0x4950994884602d1b6c6d96e4fe30f58205c39395', fee_pair: {base: '1 50'}}
      - {name: based, builder_code: '0x1924b8561eeF20e70Ede628A296175D358BE80e5', fee_pair: {base: '1 50'}}
      - {name: bullpen, builder_code: '0xf944069b489f1ebf4c3c6a6014d58cbef7c7009', fee_pair: {base: '1 50'}}
      - {name: supercexy, builder_code: '0x7975cafdff839ed5047244ed3a0dd82a89866081', fee_pair: {base: '1 50'}}
```

---

## 예측시장 / Prediction Markets

| 플랫폼 / Platform | 가입 / Sign-up | 비고 / Note |
|---|---|---|
| Polymarket | https://polymarket.com/?ref=coinmage | |
| Predict.fun | https://predict.fun/?ref=coinmage | BSC 기반 1시간 마켓 / BSC, hourly markets |
| Drift Predict | https://predict.drift.trade | (필요 시 추가 / TBA) |

---

## CEX (제휴 거래소 / Affiliated CEX)

가격 fetch와 백테스트 데이터에 주로 사용합니다.

These are used mainly for price fetching and backtest data.

| 거래소 / Exchange | 가입 / Sign-up | 수수료 할인 / Fee discount |
|---|---|---|
| Bybit | https://www.bybit.com/invite?ref=coinmage | 30% |
| OKX | https://www.okx.com/join/coinmage | 20% rebate |
| Bitget | https://www.bitget.com/expressly?clacCode=coinmage | 30% |
| Gate.io | https://www.gate.io/signup?ref=coinmage | 40% rebate |
| BingX | https://bingx.com/invite/coinmage | 50% rebate |
| HashKey | https://hashkey.com/?ref=coinmage | 20% |
| Pionex | https://www.pionex.com/?ref=coinmage | 그리드 수수료 할인 / grid fee discount |

(링크는 시기에 따라 변경될 수 있습니다 / Links may change over time.)

---

## 기타 DeFi / Earn / L1·L2

| 프로토콜 / Protocol | 가입 / Sign-up |
|---|---|
| Linea | https://referrals.linea.build/?refCode=plOzXsJ9qL |
| Plume Network | https://miles.plumenetwork.xyz/join?invite=PLUME-MC0GB |
| Base App | https://base.app/invite/coinmage/VGM6F57T |
| Minara (백테스트 / backtest) | https://minara.ai/r/Y5LALR |

---

## 사용 흐름 / Workflow

1. **Perp DEX 가입** — 위 링크로 가입 → API key 발급 → 코드 통합 (`perp-dex-setup-guides/` 참고)
2. **HL builder 활용** — `config.yaml`의 builder_rotation에 코드 등록 → 거래량 자동 분배
3. **Polymarket** — 가입 후 `polymarket-bot/` 환경변수 주입
4. **CEX** — 백테스트 (`backtest-templates/`) 및 가격 fetch에 사용

---

## 채널 / Channels

운영자 채널입니다. 실제 운영 노하우와 링크 최신 상태는 채널에서 업데이트됩니다.

Operator channels. Live operations notes and link updates are posted here.

- YouTube: https://www.youtube.com/@cryptocurrencymage
- Telegram: https://t.me/cryptocurrencymage
- Blog: https://blog.naver.com/coinmage
- Twitter: @coinmage
