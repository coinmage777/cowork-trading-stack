# Hyperliquid Builder Codes

Hyperliquid의 [HIP-1 builder fee](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint#approve-builder-fee) 시스템을 활용해 같은 HL 지갑에서 여러 프론트엔드(빌더 코드)로 거래량을 라운드로빈 분배하는 설정 레퍼런스입니다.

This documents the Hyperliquid HIP-1 builder fee config used to round-robin volume across multiple HL frontends from the same wallet.

---

## 환경변수 / Env vars

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

---

## config.yaml — builder rotation 예시

```yaml
hyperliquid_2:
  keys:
    builder_rotation:
      - {name: miracle,    builder_code: '0x4950994884602d1b6c6d96e4fe30f58205c39395', fee_pair: {base: '1 50'}, cloid_prefix: '0x4d455243'}
      - {name: dreamcash,  builder_code: '0x4950994884602d1b6c6d96e4fe30f58205c39395', fee_pair: {base: '1 50'}}
      - {name: based,      builder_code: '0x1924b8561eeF20e70Ede628A296175D358BE80e5', fee_pair: {base: '1 50'}}
      - {name: bullpen,    builder_code: '0xf944069b489f1ebf4c3c6a6014d58cbef7c7009', fee_pair: {base: '1 50'}}
      - {name: supercexy,  builder_code: '0x7975cafdff839ed5047244ed3a0dd82a89866081', fee_pair: {base: '1 50'}}
```

`hyperliquid_base.py`의 builder rotation 인덱스가 매 `create_order` 호출마다 다음 builder의 code/fee/cloid_prefix를 자동 적용합니다.

The `_rotation_index` in `hyperliquid_base.py` round-robins through this list on every `create_order` call.

---

## fee_pair 형식

`base`는 perpetual fee를 의미합니다. `'1 50'` = 0.0001 (1bp) 진입, 0.005 (50bp) 한도. 실제 fee 산정은 [HL builder fee API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint#approve-builder-fee)에 따릅니다.

`base` is the perpetual fee config. `'1 50'` means 0.0001 (1bp) entry, 0.005 (50bp) cap. Actual fee accrual follows the [HL builder fee API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint#approve-builder-fee).

---

## cloid_prefix

[Miracle Trade](https://miracletrade.com/?ref=coinmage)와 같이 cloid 접두사를 요구하는 프론트엔드는 `cloid_prefix` 필드로 자동 적용됩니다. UUID4 suffix가 자동으로 따라붙습니다.

Frontends like [Miracle Trade](https://miracletrade.com/?ref=coinmage) require a specific cloid prefix; this is auto-applied via `cloid_prefix`. A UUID4 suffix is appended automatically.

---

## 프론트엔드 가입 / Frontend sign-ups

Builder code가 활성화된 HL 프론트엔드의 가입 링크는 [README.md](../README.md)의 모듈 테이블 또는 perp-dex-wrappers 본 README에서 확인하실 수 있습니다.

Sign-up links for HL frontends with active builder codes are listed in the root [README.md](../README.md) module table and this directory's [README.md](./README.md).
