# Get market info

```
GET /api/v1/info
```

#### Response

* Status 200: Success

```json
{
  "success": true,
  "data": [
    {
      "symbol": "ETH",
      "tick_size": "0.1",
      "min_tick": "0",
      "max_tick": "1000000",
      "lot_size": "0.0001",
      "max_leverage": 50,
      "isolated_only": false,
      "min_order_size": "10",
      "max_order_size": "5000000",
      "funding_rate": "0.0000125",
      "next_funding_rate": "0.0000125",
      "created_at": 1748881333944
    },
    {
      "symbol": "BTC",
      "tick_size": "1",
      "min_tick": "0",
      "max_tick": "1000000",
      "lot_size": "0.00001",
      "max_leverage": 50,
      "isolated_only": false,
      "min_order_size": "10",
      "max_order_size": "5000000",
      "funding_rate": "0.0000125",
      "next_funding_rate": "0.0000125",
      "created_at": 1748881333944
    },
    ....
  ],
  "error": null,
  "code": null
}
```

<table><thead><tr><th width="208">Field</th><th width="186">Type</th><th>Description</th></tr></thead><tbody><tr><td><code>"symbol"</code></td><td>string</td><td>Trading pair symbol</td></tr><tr><td><code>"tick_size"</code></td><td>decimal string</td><td>Tick size. All prices are denominated as a multiple of this.</td></tr><tr><td><code>"min_tick"</code></td><td>decimal string</td><td>Minimum tick. API submitted price cannot be below this value</td></tr><tr><td><code>"max_tick"</code></td><td>decimal string</td><td>Maximum tick. API submitted price cannot be above this value</td></tr><tr><td><code>"lot_size"</code></td><td>decimal string</td><td>Lot size. All order sizes (token denominated) are denominated as a multiple of this.</td></tr><tr><td><code>"max_leverage"</code></td><td>integer</td><td>Maximum leverage allowed on this symbol when opening positions</td></tr><tr><td><code>"isolated_only"</code></td><td>boolean</td><td>If the market is set to only allow isolated positions</td></tr><tr><td><code>"min_order_size"</code></td><td>decimal string</td><td>Minimum order size (denominated in USD)</td></tr><tr><td><code>"max_order_size"</code></td><td>decimal string</td><td>Maximum order size (denominated in USD)</td></tr><tr><td><code>"funding_rate"</code></td><td>decimal string</td><td>Funding rate paid in the past funding epoch (hour)</td></tr><tr><td><code>"next_funding_rate"</code></td><td>decimal string</td><td>Estimated funding rate to be paid in the next funding epoch (hour)</td></tr><tr><td><code>"created_at"</code></td><td>ISO 8601 string</td><td>Timestamp when the market was listed on Pacifica. Markets are returned oldest first.</td></tr></tbody></table>

* Status 500: Internal server error

#### Code Example (Python)

```json
import requests

response = requests.get(
    "/api/v1/info",
    headers={"Accept": "*/*"},
)

data = response.json()
```

# Update leverage

```
POST /api/v1/account/leverage
```

#### Operation Type (for signing)

| Header Field | Type   | Content             |
| ------------ | ------ | ------------------- |
| `"type"`     | string | `"update_leverage"` |

Request Body

<table><thead><tr><th width="176">Field</th><th width="98">Type</th><th width="95">Need</th><th>Description</th><th>Example</th></tr></thead><tbody><tr><td><code>"account"</code></td><td>string</td><td>required</td><td><p></p><p>User's wallet address</p><p></p></td><td><code>42trU9A5...</code></td></tr><tr><td><code>"symbol"</code></td><td>string</td><td>required</td><td>Trading pair symbol</td><td><code>BTC</code></td></tr><tr><td><code>"leverage"</code></td><td>integer</td><td>required</td><td>New leverage value</td><td><code>10</code></td></tr><tr><td><code>"timestamp"</code></td><td>integer</td><td>required</td><td>Current timestamp in milliseconds</td><td><code>1716200000000</code></td></tr><tr><td><code>"expiry_window"</code></td><td>integer</td><td>optional</td><td>Signature expiry in milliseconds</td><td><code>30000</code></td></tr><tr><td><code>"agent_wallet"</code></td><td>string</td><td>optional</td><td>Agent wallet address</td><td><code>69trU9A5...</code></td></tr><tr><td><code>"signature"</code></td><td>string</td><td>required</td><td><p></p><p>Cryptographic signature</p><p></p></td><td><code>5j1Vy9Uq...</code></td></tr></tbody></table>

```json
{
  "account": "42trU9A5...",
  "symbol": "BTC",
  "leverage": 10,
  "timestamp": 1716200000000,
  "expiry_window": 30000,
  "agent_wallet": "69trU9A5...",
  "signature": "5j1Vy9UqY..."
}
```

#### Response

* Status 200: Leverage updated successfully

```json
 {
    "success": true
  }
```

* Status 400: Invalid request parameters

```json
  {
    "error": "Invalid leverage",
    "code": 400
  }
```

* Status 500: Internal server error

#### Code Example (Python)

```python
import requests

payload = {
    "account": "42trU9A5...",
    "signature": "5j1Vy9Uq...",
    "timestamp": 1716200000000,
    "symbol": "BTC",
    "leverage": 10
}

response = requests.post(
    "/api/v1/account/leverage",
    json=payload,
    headers={"Content-Type": "application/json"}
)

data = response.json()
```
