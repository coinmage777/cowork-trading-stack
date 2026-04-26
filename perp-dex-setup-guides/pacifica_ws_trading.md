# Create market order

The [Pacifica Python SDK](https://github.com/pacifica-fi/python-sdk/blob/f2385d42e9ae5276ba2ba85505d51db2eefd2715/ws/create_order.py) provides a comprehensive example on using this endpoint.

### Request

```json
{
  "id": "<UUID>",
  "params": {
    "create_market_order": {
      "account": "AwX6321...",
      "signature": "5vnYpt...",
      "timestamp": 1749223025396,
      "expiry_window": 5000,
      "symbol": "BTC",
      "reduce_only": false,
      "amount": "0.001",
      "side": "bid",
      "slippage_percent": "0.5",
      "client_order_id": "<UUID>"
    }
  }
}
```

<table><thead><tr><th width="194">Field</th><th width="98">Type</th><th width="95">Need</th><th>Description</th><th>Example</th></tr></thead><tbody><tr><td><code>"id"</code></td><td>Full UUID string</td><td>required</td><td>Client-defined request ID</td><td><code><UUID></code></td></tr><tr><td><code>"params"</code></td><td>object</td><td>required</td><td>Contains action type and action parameters</td><td><code>"create_order"</code></td></tr><tr><td><code>"create_market_order"</code></td><td>object</td><td>required</td><td>Specifies action type and contains parameters</td><td>See examples.</td></tr><tr><td><code>"account"</code></td><td>string</td><td>required</td><td><p></p><p>User's wallet address</p><p></p></td><td><code>42trU9A5...</code></td></tr><tr><td><code>"agent_wallet"</code></td><td>string</td><td>optional</td><td>Agent wallet address</td><td><code>69trU9A5...</code></td></tr><tr><td><code>"signature"</code></td><td>string</td><td>required</td><td><p></p><p>Cryptographic signature</p><p></p></td><td><code>5j1Vy9Uq...</code></td></tr><tr><td><code>"timestamp"</code></td><td>integer</td><td>required</td><td>Current timestamp in milliseconds</td><td><code>1716200000000</code></td></tr><tr><td><code>"expiry_window"</code></td><td>integer</td><td>optional</td><td>Signature expiry in milliseconds</td><td><code>30000</code></td></tr><tr><td><code>"symbol"</code></td><td>string</td><td>required</td><td>Trading pair symbol</td><td><code>BTC</code></td></tr><tr><td><code>"reduce_only"</code></td><td>boolean</td><td>required</td><td>Whether the order is reduce-only</td><td><code>false</code></td></tr><tr><td><code>"amount"</code></td><td>string</td><td>required</td><td>Order amount</td><td><code>0.1</code></td></tr><tr><td><code>"side"</code></td><td>string</td><td>required</td><td>Order side (bid/ask)</td><td><code>bid</code></td></tr><tr><td><code>"slippage_percent"</code></td><td>string</td><td>required</td><td>Maximum allowed slippage in percentage, e.g. "0.5" means 0.5% max slippage</td><td><code>0.5</code></td></tr><tr><td><code>"client_order_id"</code></td><td>Full UUID string</td><td>optional</td><td>Client-defined order ID</td><td><code><UUID></code></td></tr><tr><td><code>"take_profit"</code></td><td>object</td><td>optional</td><td>Take profit stop order configuration</td><td>See next three rows</td></tr><tr><td><code>"stop_price"</code></td><td>string</td><td>required (if <code>"take_profit"</code> exists)</td><td>Stop trigger price</td><td><code>55000</code></td></tr><tr><td><code>"limit_price"</code></td><td>string</td><td>optional</td><td>Limit price for the triggered order</td><td><code>54950</code></td></tr><tr><td><code>"client_order_id"</code></td><td>Full UUID string</td><td>optional</td><td>Client-defined order ID for the stop order</td><td><code><UUID></code></td></tr><tr><td><code>"stop_loss"</code></td><td>object</td><td>optional</td><td>Stop loss order configuration</td><td>See next three rows</td></tr><tr><td><code>"stop_price"</code></td><td>string</td><td>required (if <code>"stop_loss"</code> exists)</td><td>Stop trigger price</td><td><code>48000</code></td></tr><tr><td><code>"limit_price"</code></td><td>string</td><td>optional</td><td>Limit price for the triggered order</td><td><code>47950</code></td></tr><tr><td><code>"client_order_id"</code></td><td>Full UUID string</td><td>optional</td><td>Client-defined order ID for the stop order</td><td><code><UUID></code></td></tr></tbody></table>

### Response

```json
{
  "code": 200,
  "data": {
    "I": "<UUID>",
    "i": 645953,
    "s": "BTC"
  },
  "id": "<UUID>",
  "t": 1749223025962,
  "type": "create_market_order"
}
```

| Field    | Type    | Description                             |
| -------- | ------- | --------------------------------------- |
| `'code'` | integer | Status code                             |
| `'data'` | object  | Contains information about placed order |
| `'I'`    | string  | CLOID (if provided)                     |
| `'i'`    | integer | Order ID                                |
| `'s'`    | string  | Symbol                                  |
| `'id'`   | string  | Client-defined request ID               |
| `'t'`    | integer | Timestamp in milliseconds               |
| `'type'` | string  | Specifies action type                   |

Note: In order to protect liquidity providers from adverse selection, all market orders are subject to a \~200ms delay.

# Create limit order

The [Pacifica Python SDK](https://github.com/pacifica-fi/python-sdk/blob/f2385d42e9ae5276ba2ba85505d51db2eefd2715/ws/create_order.py) provides a comprehensive example on using this endpoint.

### Request

```json
{
  "id": "<UUID>",
  "params": {
    "create_order": {
      "account": "AwX6321...",
      "signature": "5vnYpt...",
      "timestamp": 1749223025396,
      "expiry_window": 5000,
      "symbol": "BTC",
      "price": "100000.00",
      "reduce_only": false,
      "amount": "0.001",
      "side": "bid",
      "tif": "GTC",
      "client_order_id": "<UUID>"
    }
  }
}
```

<table><thead><tr><th width="188">Field</th><th width="98">Type</th><th width="95">Need</th><th>Description</th><th>Example</th></tr></thead><tbody><tr><td><code>"id"</code></td><td>Full UUID string</td><td>required</td><td>Client-defined request ID</td><td><code><UUID></code></td></tr><tr><td><code>"params"</code></td><td>object</td><td>required</td><td>Contains action type and action parameters</td><td><code>"create_order"</code></td></tr><tr><td><code>"create_order"</code></td><td>object</td><td>required</td><td>Specifies action type and contains parameters</td><td>See examples.</td></tr><tr><td><code>"account"</code></td><td>string</td><td>required</td><td><p></p><p>User's wallet address</p><p></p></td><td><code>42trU9A5...</code></td></tr><tr><td><code>"agent_wallet"</code></td><td>string</td><td>optional</td><td>Agent wallet address</td><td><code>69trU9A5...</code></td></tr><tr><td><code>"signature"</code></td><td>string</td><td>required</td><td><p></p><p>Cryptographic signature</p><p></p></td><td><code>5j1Vy9Uq...</code></td></tr><tr><td><code>"timestamp"</code></td><td>integer</td><td>required</td><td>Current timestamp in milliseconds</td><td><code>1716200000000</code></td></tr><tr><td><code>"expiry_window"</code></td><td>integer</td><td>optional</td><td>Signature expiry in milliseconds</td><td><code>30000</code></td></tr><tr><td><code>"symbol"</code></td><td>string</td><td>required</td><td>Trading pair symbol</td><td><code>BTC</code></td></tr><tr><td><code>"price"</code></td><td>string</td><td>required</td><td>Order price</td><td><code>50000</code></td></tr><tr><td><code>"reduce_only"</code></td><td>boolean</td><td>required</td><td>Whether the order is reduce-only</td><td><code>false</code></td></tr><tr><td><code>"amount"</code></td><td>string</td><td>required</td><td>Order amount</td><td><code>0.1</code></td></tr><tr><td><code>"side"</code></td><td>string</td><td>required</td><td>Order side (bid/ask)</td><td><code>bid</code></td></tr><tr><td><code>"tif"</code></td><td>string</td><td>required</td><td>Time in force (<code>GTC</code>, <code>IOC</code>, <code>ALO</code>, <code>TOB</code>)</td><td><code>GTC</code></td></tr><tr><td><code>"client_order_id"</code></td><td>Full UUID string</td><td>optional</td><td>Client-defined order ID</td><td><code><UUID></code></td></tr><tr><td><code>"take_profit"</code></td><td>object</td><td>optional</td><td>Take profit stop order configuration</td><td>See next three rows</td></tr><tr><td><code>"stop_price"</code></td><td>string</td><td>required (if <code>"take_profit"</code> exists)</td><td>Stop trigger price</td><td><code>55000</code></td></tr><tr><td><code>"limit_price"</code></td><td>string</td><td>optional</td><td>Limit price for the triggered order</td><td><code>54950</code></td></tr><tr><td><code>"client_order_id"</code></td><td>string</td><td>Full UUID string</td><td>Client-defined order ID for the stop order</td><td><code><UUID></code></td></tr><tr><td><code>"stop_loss"</code></td><td>object</td><td>optional</td><td>Stop loss order configuration</td><td>See next three rows</td></tr><tr><td><code>"stop_price"</code></td><td>string</td><td>required (if <code>"stop_loss"</code> exists)</td><td>Stop trigger price</td><td><code>48000</code></td></tr><tr><td><code>"limit_price"</code></td><td>string</td><td>optional</td><td>Limit price for the triggered order</td><td><code>47950</code></td></tr><tr><td><code>"client_order_id"</code></td><td>string</td><td>Full UUID string</td><td>Client-defined order ID for the stop order</td><td><code><UUID></code></td></tr></tbody></table>

### Response

```json
{
  "code": 200,
  "data": {
    "I": "<UUID>",
    "i": 645953,
    "s": "BTC"
  },
  "id": "<UUID>",
  "t": 1749223025962,
  "type": "create_order"
}
```

| Field    | Type    | Description                             |
| -------- | ------- | --------------------------------------- |
| `'code'` | integer | Status code                             |
| `'data'` | object  | Contains information about placed order |
| `'I'`    | string  | CLOID (if provided)                     |
| `'i'`    | integer | Order ID                                |
| `'s'`    | string  | Symbol                                  |
| `'id'`   | string  | Client-defined request ID               |
| `'t'`    | integer | Timestamp in milliseconds               |
| `'type'` | string  | Specifies action type                   |

Note: In order to protect liquidity providers from adverse selection, all TIF GTC, and TIF IOC orders are subject to a \~200ms delay.
# Cancel order

The [Pacifica Python SDK](https://github.com/pacifica-fi/python-sdk/blob/f2385d42e9ae5276ba2ba85505d51db2eefd2715/ws/cancel_order.py) provides a comprehensive example on using this endpoint.

### Request

```json
{
  "id": "<UUID>",
  "params": {
    "cancel_order": {
      "account": "AwX6321...",
      "signature": "4RqbgB...",
      "timestamp": 1749223343149,
      "expiry_window": 5000,
      "symbol": "BTC",
      "client_order_id": "<UUID>"
    }
  }
}
```

<table><thead><tr><th width="188">Field</th><th width="98">Type</th><th width="95">Need</th><th>Description</th><th>Example</th></tr></thead><tbody><tr><td><code>"id"</code></td><td>Full UUID string</td><td>required</td><td>Client-defined request ID</td><td><code><UUID></code></td></tr><tr><td><code>"params"</code></td><td>object</td><td>required</td><td>Contains action type and action parameters</td><td><code>"cancel_order"</code></td></tr><tr><td><code>"cancel_order"</code></td><td>object</td><td>required</td><td>Specifies action type and contains parameters</td><td>See examples.</td></tr><tr><td><code>"account"</code></td><td>string</td><td>required</td><td><p></p><p>User's wallet address</p><p></p></td><td><code>42trU9A5...</code></td></tr><tr><td><code>"agent_wallet"</code></td><td>string</td><td>optional</td><td>Agent wallet address</td><td><code>69trU9A5...</code></td></tr><tr><td><code>"signature"</code></td><td>string</td><td>required</td><td><p></p><p>Cryptographic signature</p><p></p></td><td><code>5j1Vy9Uq...</code></td></tr><tr><td><code>"timestamp"</code></td><td>integer</td><td>required</td><td>Current timestamp in milliseconds</td><td><code>1716200000000</code></td></tr><tr><td><code>"expiry_window"</code></td><td>integer</td><td>optional</td><td>Signature expiry in milliseconds</td><td><code>30000</code></td></tr><tr><td><code>"symbol"</code></td><td>string</td><td>required</td><td>Trading pair symbol</td><td><code>BTC</code></td></tr><tr><td><code>"order_id"</code></td><td>integer</td><td>required (if no CLOID)</td><td>Exchange-assigned order ID</td><td><code>123</code></td></tr><tr><td><code>"client_order_id"</code></td><td>Full UUID string</td><td>required (if no OID)</td><td>Client-defined order ID</td><td><code><UUID></code></td></tr></tbody></table>

### Response

```json
{
  "code": 200,
  "data": {
    "I": "<UUID>",
    "i": null,
    "s": "BTC"
  },
  "id": "<UUID>",
  "t": 1749223343610,
  "type": "cancel_order"
}
```

| Field    | Type    | Description                             |
| -------- | ------- | --------------------------------------- |
| `'code'` | integer | Status code                             |
| `'data'` | object  | Contains information about placed order |
| `'I'`    | string  | CLOID (if provided)                     |
| `'i'`    | integer | Order ID                                |
| `'s'`    | string  | Symbol                                  |
| `'id'`   | string  | Same as above request ID                |
| `'t'`    | integer | Timestamp in milliseconds               |
| `'type'` | string  | Specifies action type                   |

Cancel are not subject to any speedbump.

# Cancel all orders

The [Pacifica Python SDK](https://github.com/pacifica-fi/python-sdk/blob/f2385d42e9ae5276ba2ba85505d51db2eefd2715/ws/cancel_all_orders.py) provides a comprehensive example on using this endpoint.

### Request

```json
{
  "id": "<UUID>",
  "params": {
    "cancel_all_orders": {
      "account": "AwX6f3...",
      "signature": "2XP8fz...",
      "timestamp": 1749221927343,
      "expiry_window": 5000,
      "all_symbols": true,
      "exclude_reduce_only": false
    }
  }
}
```

<table><thead><tr><th width="184">Field</th><th width="98">Type</th><th width="123">Need</th><th>Description</th><th>Example</th></tr></thead><tbody><tr><td><code>"id"</code></td><td>Full UUID string</td><td>required</td><td>Client-defined request ID</td><td><code><UUID></code></td></tr><tr><td><code>"params"</code></td><td>object</td><td>required</td><td>Contains action type and action parameters</td><td><code>"cancel_all_orders"</code></td></tr><tr><td><code>"cancel_order"</code></td><td>object</td><td>required</td><td>Specifies action type and contains parameters</td><td>See examples.</td></tr><tr><td><code>"account"</code></td><td>string</td><td>required</td><td><p></p><p>User's wallet address</p><p></p></td><td><code>42trU9A5...</code></td></tr><tr><td><code>"agent_wallet"</code></td><td>string</td><td>optional</td><td>Agent wallet address</td><td><code>69trU9A5...</code></td></tr><tr><td><code>"signature"</code></td><td>string</td><td>required</td><td><p></p><p>Cryptographic signature</p><p></p></td><td><code>5j1Vy9Uq...</code></td></tr><tr><td><code>"timestamp"</code></td><td>integer</td><td>required</td><td>Current timestamp in milliseconds</td><td><code>1716200000000</code></td></tr><tr><td><code>"expiry_window"</code></td><td>integer</td><td>optional</td><td>Signature expiry in milliseconds</td><td><code>30000</code></td></tr><tr><td><code>"all_symbols"</code></td><td>boolean</td><td>required</td><td>Whether to cancel orders for all symbols</td><td><code>true</code></td></tr><tr><td><code>"exclude_reduce_only"</code></td><td>boolean</td><td>required</td><td>Whether to exclude reduce-only orders</td><td><code>false</code></td></tr><tr><td><code>"symbol"</code></td><td>string</td><td>required<br>(if <code>"all_symbols"</code> is false)</td><td>Trading pair symbol</td><td><code>BTC</code></td></tr></tbody></table>

```json
{
  "code": 200,
  "data": {
    "cancelled_count": 10
  },
  "id": "<UUID>",
  "t": 1749221787291,
  "type": "cancel_all_orders"
}
```

| Field               | Type    | Description                             |
| ------------------- | ------- | --------------------------------------- |
| `'code'`            | integer | Status code                             |
| `'data'`            | object  | Contains information about placed order |
| `'cancelled_count'` | string  | Number of orders successfully cancelled |
| `'id'`              | string  | Same as above request ID                |
| `'t'`               | integer | Timestamp in milliseconds               |
| `'type'`            | string  | Specifies action type                   |
