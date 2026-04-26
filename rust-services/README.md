# rust-services

Python 핫패스를 Rust로 마이그한 마이크로서비스. PyO3 + maturin 빌드. **hl-sign** (Hyperliquid signature, 3.5x speedup) + **gap-recorder** (SQLite WAL batched, 169K rows/sec).

## 빌드 결과 요약

| 모듈 | LOC (Rust+Python) | 검증 | Speedup | Production |
|------|---------------------|------|---------|------------|
| **hl-sign** | ~500 + bridge 100 | 100/100 ref vectors + 1000/1000 real orders + 5/5 live equality | **3.5x** (p50 717μs → 200μs) | ✅ Active |
| **gap-recorder** | 344 + 162 | 200 rows insert/select round-trip + WAL 모드 confirmed | 169K rows/sec | Shadow mode |

## hl-sign

Hyperliquid action signing (msgpack + keccak + ECDSA). HL/Miracle/DreamCash/HyENA/TreadFi 등 17+ 거래소 매 주문 시 호출.

### 빌드
```bash
cd hl-sign
pip install maturin
maturin build --release
pip install target/wheels/mpdex_hl_sign-0.1.0-*.whl
```

### 검증 (필수, 자금 위험 회피)
```bash
python verify_vectors.py    # 100 ref vectors
python replay_1000.py       # 1000 real production orders
python benchmark.py         # speedup 측정
```

### Python 사용
```python
# Drop-in replacement (bridge가 fallback 내장)
from mpdex_hl_sign_bridge import sign_l1_action, sign_user_signed_action

sig = sign_l1_action(wallet, action, vault_addr, nonce, expires_after, is_mainnet)
```

### 파일 구조
```
hl-sign/
├── Cargo.toml
├── src/lib.rs                          — Rust impl (PyO3 bindings)
├── pyproject.toml
├── mpdex_hl_sign_bridge.py             — Python shim (Rust 우선, 실패 시 Python fallback)
├── verify_vectors.py                   — 100 ref vector 검증
├── replay_1000.py                      — 1000 real order replay
├── benchmark.py                        — Python vs Rust 속도 측정
├── vectors.json                        — Ref vectors (test data)
└── tests/
```

## gap-recorder

Funding/spread/BBO gap 데이터 SQLite WAL 모드 batched insert. funding_collector.py의 hot path.

### 사용
```python
from mpdex_gap_recorder_bridge import GapRecorderClient

rec = GapRecorderClient("funding_rates.db", flush_threshold=100)
rec.record_price_gap(timestamp, symbol, max_ex, min_ex, max_p, min_p, gap_usd, gap_pct, all_prices, actionable)
rec.flush()  # 100 rows 누적 시 자동 flush

# Retention prune
deleted = rec.prune_older_than(48.0)  # 48h
```

### 파일 구조
```
gap-recorder/
├── Cargo.toml
├── src/lib.rs                          — Rust GapRecorder (rusqlite + parking_lot)
├── pyproject.toml
├── mpdex_gap_recorder_bridge.py        — Python shim
├── gap_recorder_verify.py              — 검증 (200 rows insert/select)
├── gap_recorder_benchmark.py           — 169K rows/sec 측정
└── target/wheels/...whl
```

## 빌드 환경

- **Rust 1.75+**
- **maturin**: `pip install maturin`
- **Python 3.12** (wheel CP312)
- **Linux x86_64** (manylinux_2_34)

Windows / macOS 빌드도 가능 (maturin이 알아서 처리). 단 cross-compile은 별도 setup.

## 마이그 후보 평가 (백로그)

이전 분석에서 다른 hot path도 검토:

| 후보 | ROI | 비고 |
|------|-----|------|
| nado_pair_scalper momentum/zscore/correlation | MED | 분당 300회, ~150μs avg. py-spy >5% CPU 확인 후 진행 |
| Polymarket mm Stoikov quotes | LOW | 0.2 Hz, ~500μs. SHADOW only |
| strategy_evolver backtest | LOW | 12h 1회 배치, latency 무관 |
| DH BBO loop | NOT 추천 | CPU 0.19%만, IO bound. Python httpx 최적화가 더 효율 |

## 의존성

Rust:
- `pyo3` (PyO3 bindings)
- `rusqlite` (SQLite, bundled)
- `parking_lot` (Mutex)
- `serde`, `serde_json`
- `keccak-asm` (HL action hashing)
- `secp256k1` (ECDSA)
