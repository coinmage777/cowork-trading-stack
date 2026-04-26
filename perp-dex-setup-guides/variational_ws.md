## variational ws에 대해 LLM과 나눈 대화 ##

# get_mark_price에서 활용하면됨 #

> wss://omni-ws-server.prod.ap-northeast-1.variational.io/prices 이 웹소캣에서 {
  "action": "subscribe",
  "instruments": [
    {
      "underlying": "BTC",
      "instrument_type": "perpetual_future",
      "settlement_asset": "USDC",
      "funding_interval_s": 3600
    }
  ]
} 이거 구독해서 데이터 오는거 확인해봐

● WebSocket에 연결해서 BTC perpetual future 데이터를 구독해보겠습니다.

● Bash(python3 << 'EOF' timeout: 30s
      import asyncio…)
  ⎿  Connected to wss://omni-ws-server.prod.ap-northeast-1.variational.io/prices
     Sent: {
       "action": "subscribe",
     … +166 lines (ctrl+o to expand)

● WebSocket 연결 및 구독이 정상 작동합니다. 데이터 요약:

  연결 정보:
  - 채널: instrument_price:P-BTC-USDC-3600
  - 약 2초마다 pricing 업데이트, 5초마다 heartbeat

  수신 데이터 구조:
  | 필드                    | 값 예시             | 설명                          |
  |-------------------------|---------------------|-------------------------------|
  | price                   | 91238.14 → 91216.25 | perpetual future 가격         |
  | underlying_price        | ~91277              | BTC 현물 가격                 |
  | native_price            | 0.9993~0.9996       | 정규화 가격                   |
  | interest_rate           | -0.00000241         | funding rate                  |
  | delta                   | 1                   | perpetual은 항상 1            |
  | gamma/theta/vega/rho/iv | 0                   | 옵션용 그릭스 (perpetual은 0) |

  정상적으로 실시간 BTC-USDC perpetual future 가격 데이터가 스트리밍되고 있습니다.

> 어느채널에서 오는지 어떻게 알아냈어?

● 서버가 응답으로 알려줬습니다.

  구독 메시지를 보내면, 서버가 보내주는 응답 데이터 안에 channel 필드가 포함되어 있었습니다:

  {
    "channel": "instrument_price:P-BTC-USDC-3600",  // ← 서버가 알려줌
    "pricing": {
      "price": "91238.14",
      ...
    }
  }

  채널 이름 패턴을 보면:
  - P = Perpetual
  - BTC = underlying
  - USDC = settlement_asset
  - 3600 = funding_interval_s

  구독 요청에 넣은 파라미터들이 조합되어 채널 이름이 자동 생성된 것으로 보입니다.

> 그럼 ETH를 구독해서 테스트해봐 3600 funding_interval_s로

● Bash(python3 << 'EOF' timeout: 30s
      import asyncio…)
  ⎿  Connected to wss://omni-ws-server.prod.ap-northeast-1.variational.io/prices
     Sent: {
       "action": "subscribe",
     … +88 lines (ctrl+o to expand)

● ETH도 정상 작동합니다.

  채널: instrument_price:P-ETH-USDC-3600

  ETH 데이터:
  | 필드             | 값         |
  |------------------|------------|
  | price            | 3135.72    |
  | underlying_price | 3137.31    |
  | native_price     | 0.9995     |
  | interest_rate    | 0.00007057 |

  BTC와 비교하면 ETH는 interest_rate가 양수(+0.00007057)인 반면, BTC는 음수(-0.00000241)였네요. ETH가 살짝 프리미엄, BTC는 살짝 디스카운트 상태입니다.

> data를 받을때 채널별로 보통 받잖아. 그럼 어떤식으로 정규화 해야겠어?

● 채널 이름을 파싱해서 키로 쓰거나, 채널 전체를 키로 쓰는 방식이 일반적입니다.

  # 방법 1: 채널 이름 그대로 키로 사용
  prices = {}
  # prices["P-BTC-USDC-3600"] = {...}
  # prices["P-ETH-USDC-3600"] = {...}

  # 방법 2: 파싱해서 구조화된 키로 사용
  prices = {
      ("BTC", "USDC", 3600): {...},
      ("ETH", "USDC", 3600): {...},
  }

  # 방법 3: underlying 기준 nested dict (가장 유연)
  prices = {
      "BTC": {
          "USDC": {
              3600: {"price": ..., "underlying_price": ...}
          }
      },
      "ETH": {
          "USDC": {
              3600: {"price": ..., "underlying_price": ...}
          }
      }
  }

  채널 파싱 함수 예시:

  def parse_channel(channel: str) -> dict:
      # "instrument_price:P-BTC-USDC-3600"
      _, instrument = channel.split(":")
      parts = instrument.split("-")
      return {
          "type": parts[0],           # P (perpetual)
          "underlying": parts[1],      # BTC
          "settlement": parts[2],      # USDC
          "funding_interval": int(parts[3])  # 3600
      }

  어떤 방식이 좋을지는 사용 패턴에 따라 다릅니다:
  - 조회가 underlying 기준 → 방법 3
  - 단순 저장/조회 → 방법 1
  - 여러 조건 조합 조회 → 방법 2 (튜플 키)