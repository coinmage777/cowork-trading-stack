# rust-services

> **Advanced — 처음 vibe coding 학습자라면 이 폴더는 건너뛰셔도 됩니다.**
> PyO3 + maturin Rust 가속이 필요한 단계는 봇 운영이 어느 정도 안정화된 후입니다.

> 한 줄 요약: Python 핫패스를 Rust 로 마이그한 마이크로서비스. PyO3 + maturin 빌드. **hl-sign** (Hyperliquid signature, p50 717μs → 200μs, **3.5x speedup**) + **gap-recorder** (SQLite WAL batched, **169K rows/sec**) 두 모듈로 구성됩니다.

## 의존성 (Dependencies)

Rust:
- `pyo3` (PyO3 bindings)
- `rusqlite` (SQLite, bundled)
- `parking_lot` (Mutex)
- `serde`, `serde_json`
- `keccak-asm` (HL action hashing)
- `secp256k1` (ECDSA)

빌드 환경:
- **Rust 1.75+**
- **maturin**: `pip install maturin`
- **Python 3.12** (wheel CP312)
- **Linux x86_64** (manylinux_2_34, Windows / macOS 도 가능)

## AI에게 어떻게 시켰나 (How this was built with Claude)

이 모듈을 만들 때 사용한 프롬프트 패턴:

> "Hyperliquid action signing (msgpack + keccak + ECDSA) 이 분당 수천 번 호출되는데 Python 으로 p50 717μs 가 너무 느리다. PyO3 + maturin 으로 Rust 마이그하고, Python 측에는 `mpdex_hl_sign_bridge.py` shim 을 만들어서 `try: from rust ...` `except ImportError: ...Python fallback` 패턴으로 drop-in replacement 가능하게 해 줘. 검증은 ① 기존 Python 구현으로 만든 100개 ref vector 와 비트 동일 ② 1000건 real live order replay 100% 일치 ③ live 5건 equality test 까지 통과해야 live 환경 적용."

AI가 자주 틀린 부분 (common AI mistakes for this code path):

- **PyO3 GIL release 누락**: Rust 함수가 1ms 이상 걸리면 GIL 잡고 있을 이유 없는데 AI 는 `py.allow_threads` 안 붙임. 멀티스레드 시 무의미한 직렬화 발생.
- **secp256k1 v 값 27/28 vs 0/1 혼동**: Ethereum 의 `v = recovery_id + 27` 인데 Rust 라이브러리는 raw recovery_id (0/1) 를 리턴. 그대로 hex 인코딩하면 HL 가 401 리턴.
- **rusqlite WAL 모드 자동 활성 가정**: `journal_mode=WAL` 은 명시적 `PRAGMA journal_mode=WAL` 를 한번 실행해야 함. AI 는 connection string 에 넣으면 된다고 답하는 경우.
- **검증 vector 가 비트 동일 vs JSON equal 혼동**: msgpack 직렬화 결과는 dict 순서에 따라 달라짐. Rust 와 Python 의 dict iteration order 가 같은지 명시적으로 검증해야 함.

힌트: 이 모듈은 hl-sign 만 검증 단계 100/100 + 1000/1000 + 5/5 에서 한번씩 vector 깨지면서 3번 재작업했고, 그때마다 AI 가 놓친 것은 **secp256k1 v 값 27 추가, msgpack 의 dict ordering, GIL release** 입니다.

## 빌드 결과 요약

| 모듈 | LOC (Rust+Python) | 검증 | Speedup | Production |
|------|---------------------|------|---------|------------|
| **hl-sign** | ~500 + bridge 100 | 100/100 ref vectors + 1000/1000 real orders + 5/5 live equality | **3.5x** (p50 717μs → 200μs) | Active |
| **gap-recorder** | 344 + 162 | 200 rows insert/select round-trip + WAL 모드 confirmed | **169K rows/sec** | Shadow mode |

## 모듈 구조 (file structure with one-liner per file)

### hl-sign

| File | Purpose |
|------|---------|
| `hl-sign/Cargo.toml` | Rust 패키지 매니페스트 |
| `hl-sign/src/lib.rs` | Rust impl (PyO3 bindings) — msgpack + keccak + ECDSA |
| `hl-sign/pyproject.toml` | maturin 빌드 설정 |
| `hl-sign/mpdex_hl_sign_bridge.py` | Python shim (Rust 우선, 실패 시 Python fallback) |
| `hl-sign/verify_vectors.py` | 100 ref vector 검증 |
| `hl-sign/replay_1000.py` | 1000 real order replay |
| `hl-sign/benchmark.py` | Python vs Rust 속도 측정 |
| `hl-sign/vectors.json` | Ref vectors (test data) |
| `hl-sign/tests/` | 단위 테스트 |

### gap-recorder

| File | Purpose |
|------|---------|
| `gap-recorder/Cargo.toml` | Rust 패키지 매니페스트 |
| `gap-recorder/src/lib.rs` | Rust GapRecorder (rusqlite + parking_lot) |
| `gap-recorder/pyproject.toml` | maturin 빌드 설정 |
| `gap-recorder/mpdex_gap_recorder_bridge.py` | Python shim |
| `gap-recorder/gap_recorder_verify.py` | 검증 (200 rows insert/select) |
| `gap-recorder/gap_recorder_benchmark.py` | 169K rows/sec 측정 |

## 사용 예시 (Usage)

### hl-sign

```bash
cd hl-sign
pip install maturin
maturin build --release
pip install target/wheels/mpdex_hl_sign-0.1.0-*.whl

# 검증 (필수, 자금 위험 회피)
python verify_vectors.py    # 100 ref vectors
python replay_1000.py       # 1000 real live orders
python benchmark.py         # speedup 측정
```

```python
# Drop-in replacement (bridge 가 fallback 내장)
from mpdex_hl_sign_bridge import sign_l1_action, sign_user_signed_action

sig = sign_l1_action(wallet, action, vault_addr, nonce, expires_after, is_mainnet)
```

### gap-recorder

```python
from mpdex_gap_recorder_bridge import GapRecorderClient

rec = GapRecorderClient("funding_rates.db", flush_threshold=100)
rec.record_price_gap(
    timestamp, symbol, max_ex, min_ex,
    max_p, min_p, gap_usd, gap_pct, all_prices, actionable,
)
rec.flush()  # 100 rows 누적 시 자동 flush

# Retention prune
deleted = rec.prune_older_than(48.0)  # 48h
```

## 실전 함정 (Battle-tested gotchas)

운영하면서 깨진 부분들:

- **secp256k1 v=27 누락 → 401 burst**: Rust `secp256k1` 가 recovery_id 0/1 을 리턴하는데, Ethereum 은 `v = 27 + recovery_id`. 처음 그대로 sign 해서 보냈더니 HL 401 만 일관되게 떨어져 시그니처 byte ordering 의심하다가 우회로 발견.
- **msgpack dict ordering**: Python `dict` 는 insertion order 보존이지만 Rust `HashMap` 은 random. action dict 직렬화 시 BTreeMap 또는 명시적 정렬 필요. ref vector 100개 중 4개가 깨짐.
- **PyO3 GIL release 누락 → 멀티스레드 직렬화**: 여러 거래소 wrapper 가 같은 hl-sign 호출하는데 GIL 안 풀려서 Python fallback 보다 느린 케이스 발생. `py.allow_threads(...)` 로 해결.
- **gap-recorder WAL 모드 미활성**: 단순 connection 만으로는 WAL 안 켜짐. Rust 측 init 시 `PRAGMA journal_mode=WAL` + `PRAGMA synchronous=NORMAL` 명시 호출 후 169K rows/sec 달성.
- **bridge fallback 의 silent 사용**: maturin 빌드 안 된 환경에서 bridge 가 Python fallback 으로 자동 폴백되는데 사용자는 인지 못함. log warning 추가.

## 응용 (How this fits with other modules)

- `20-exchange-wrappers/_combined/_common/hl_sign.py` 가 본 hl-sign 의 host 모듈 (PyO3 wheel 설치 시 자동 우선 사용)
- `40-realtime-infra/cross-venue-arb-scanner/funding_collector` 의 hot path 가 gap-recorder 의 SQLite WAL batched insert 호출
- `30-strategy-patterns/_combined/strategy_evolver` 의 백테스트 score DB 도 gap-recorder 위에 쌓을 수 있음 (현재 미적용)

결합 사용 시: bridge shim 의 fallback 패턴 덕분에 wheel 빌드 안 된 환경 (예: Windows ARM) 에서도 코드는 작동. 단 성능 이득은 없음.

## 마이그 후보 평가 (백로그)

이전 분석에서 다른 hot path 도 검토:

| 후보 | ROI | 비고 |
|------|-----|------|
| nado_pair_scalper momentum/zscore/correlation | MED | 분당 300회, ~150μs avg. py-spy >5% CPU 확인 후 진행 |
| strategy_evolver backtest | LOW | 12h 1회 배치, latency 무관 |
| DH BBO loop | NOT 추천 | CPU 0.19% 만, IO bound. Python httpx 최적화가 더 효율 |

## 환경변수

본 모듈 자체는 환경변수 X. host 측 (perp-dex-wrappers) 의 HL key 를 그대로 사용.

## 거래소 가입 링크

- Hyperliquid: https://miracletrade.com/?ref=coinmage
