# bbo-loop — 스캐폴드 (현재 비활성)

BBO polling hot path의 Rust 후보 구현. **분석 결과 `ROI 낮음`**으로 판정되어 실제 연동은 보류. 스캐폴드/벤치/테스트만 존재.

## 분석 요약

`backend/services/poller.py::_poll_all_bbos`의 CPU 비용은 전체 3초 iteration 대비 **약 0.19%**. 나머지 99.8%는 HTTP wait + asyncio overhead. → Rust 마이그레이션으로 얻을 절대 시간 절약이 작고, PyO3/subprocess 브릿지 유지 비용을 넘기지 못함.

### Python vs Rust 벤치 (동일 시뮬 데이터)

| 항목 | Python | Rust | Speedup |
|------|--------|------|---------|
| Binance bookTicker parse (spot 500 + futures 250) | 0.499 ms | 0.076 ms | 6.6x |
| build_gap_result (500 tickers × 8 exchanges) | 3.420 ms | 1.645 ms | 2.1x |
| calculate_impact_gap × 1000 | 1.217 ms | 0.001 ms | 1217x |

## 권고 (Case A)

Rust 대신 Python 내에서 IO 최적화:
1. `httpx.AsyncClient` 전역 싱글톤 + HTTP/2 + connection pooling (`http2=True, limits=httpx.Limits(max_connections=50)`)
2. ccxt `fetch_tickers` → 거래소별 bookTicker 직접 API 호출 (Binance는 이미 적용됨; Bybit/OKX/Bitget도 bookTicker v5 endpoint 존재)
3. `asyncio.gather`의 return_exceptions=True + 타임아웃 세분화 (거래소당 3s)

## Rust 재고려 조건

다음 중 하나 이상 충족 시 재평가:
- BBO_POLL_INTERVAL이 1초 미만으로 짧아짐 (CPU ratio >3%)
- 거래소 수 30+ 또는 티커 수 3000+ (build_gap_result 시간 비선형 증가)
- 기회 감지(`calculate_impact_gap`)를 1 iter당 수천 번 호출하는 신규 알파 전략

## 빌드

```bash
cd rust-services
export CARGO_TARGET_DIR=/tmp/dh-bbo-target  # Windows Google Drive lock 회피
cargo test -p bbo-loop --release --tests
cargo run -p bbo-loop --release --bin bbo-bench
```

## 파일 구조

- `src/lib.rs` — 핫스팟 3종: `parse_binance_booktickers`, `calculate_gap*`, `calculate_impact_gap`, `build_gap_result`. 7 unit test 포함.
- `src/bin/bench.rs` — `profile_python.py`와 동일한 시뮬 데이터로 timing 측정.
- `tests/parity.rs` — random 1000 input에서 수식 일치 검증 (오차 1e-12 이내).
- `profile_python.py` — Python 쪽 동일 벤치.

## 이 scaffold가 미래에 쓰인다면

PyO3 wrapper 필요:
```toml
[dependencies]
pyo3 = { version = "0.22", features = ["extension-module"] }
# crate-type = ["cdylib"]
```
+ `maturin develop --release` → `from bbo_loop_rust import parse_binance_booktickers` 식으로 import.

현재 scaffold는 subprocess binary 형태이므로 PyO3 추가 없이도 `stdin JSON → stdout JSON` 프로토콜로 호환 가능 (gap-recorder와 동일 패턴).
