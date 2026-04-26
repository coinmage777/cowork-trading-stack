# Websocket

Mainnet websocket URL: <wss://ws.pacifica.fi/ws>\
Testnet websocket URL: <wss://test-ws.pacifica.fi/ws>

The API service provides a universal endpoint for websocket streams. The subscribed data will be streamed in the corresponding channel after the connection is established.

### Subscription Message

```
{
    "method": "subscribe",
    "params": { ... }
}
```

### Unsubscription Message

```
{
    "method": "unsubscribe",
    "params": { ... }
}
```

### Heartbeat and Timeout

A webscoket connection will be closed if no message is sent for the past 60 seconds, or the connection has been alive for 24 hours.&#x20;

To keep the connection alive without messages in 60 seconds, we can send a heartbeat message&#x20;

```
{
    "method": "ping"
}
```

and alive connection will respond with

```
{
    "channel": "pong"
}
```

# Websocket

Mainnet websocket URL: <wss://ws.pacifica.fi/ws>\
Testnet websocket URL: <wss://test-ws.pacifica.fi/ws>

The API service provides a universal endpoint for websocket streams. The subscribed data will be streamed in the corresponding channel after the connection is established.

### Subscription Message

```
{
    "method": "subscribe",
    "params": { ... }
}
```

### Unsubscription Message

```
{
    "method": "unsubscribe",
    "params": { ... }
}
```

### Heartbeat and Timeout

A webscoket connection will be closed if no message is sent for the past 60 seconds, or the connection has been alive for 24 hours.&#x20;

To keep the connection alive without messages in 60 seconds, we can send a heartbeat message&#x20;

```
{
    "method": "ping"
}
```

and alive connection will respond with

```
{
    "channel": "pong"
}
```
# Prices

Refer to [Websocket](https://docs.pacifica.fi/api-documentation/api/websocket) for establishing the websocket connection.

### Params

```json
{
    "method": "subscribe",
    "params": {
        "source": "prices"
    }
}
```

### Stream

```json
{
    "channel": "prices",
    "data": [
        {
            "funding": "0.0000125",
            "mark": "105473",
            "mid": "105476",
            "next_funding": "0.0000125",
            "open_interest": "0.00524",
            "oracle": "105473",
            "symbol": "BTC",
            "timestamp": 1749051612681,
            "volume_24h": "63265.87522",
            "yesterday_price": "955476"
        }
        // ... other symbol prices
    ],
}
```

| Field               | Type           | Description               |
| ------------------- | -------------- | ------------------------- |
| `'funding'`         | decimal string | Funding rate              |
| `'mark'`            | decimal string | Mark price                |
| `'timestamp'`       | number         | Timestamp in milliseconds |
| `'mid'`             | decimal string | Mid price                 |
| `'next_funding'`    | decimal string | Next funding rate         |
| `'open_interest'`   | decimal string | Open interest amount      |
| `'oracle'`          | decimal string | Oracle price              |
| `'symbol'`          | string         | Symbol                    |
| `'volume_24h'`      | decimal string | 24 hour volume in USD     |
| `'yesterday_price'` | decimal string | Previous day price        |
# Orderbook

Refer to [Websocket](https://docs.pacifica.fi/api-documentation/api/websocket) for establishing the websocket connection.

### Params

```json
{
    "method": "subscribe",
    "params": {
        "source": "book",
        "symbol": "SOL",
        "agg_level": 1  // Aggregation level
    }
}
```

where `agg_level`can be one of `1, 2, 5, 10, 100, 1000`.

### Stream

```json
{
  "channel": "book",
  "data": {
    "l": [
      [
        {
          "a": "37.86",
          "n": 4,
          "p": "157.47"
        },
        // ... other aggegated bid levels
      ],
      [
        {
          "a": "12.7",
          "n": 2,
          "p": "157.49"
        },
        {
          "a": "44.45",
          "n": 3,
          "p": "157.5"
        },
        // ... other aggregated ask levels
      ]
    ],
    "s": "SOL",
    "t": 1749051881187
  }
}
```

The `book` websocket stream updates once every 100ms&#x20;

| Field | Type           | Description                                                                                                                                   |
| ----- | -------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `'l'` | array          | \[Bids, Asks]                                                                                                                                 |
| `'a'` | decimal string | Total amount in aggregation level.                                                                                                            |
| `'n'` | integer        | Number of orders in aggregation level.                                                                                                        |
| `'p'` | decimal string | <ul><li>In bids array, this is highest price in aggregation level.</li><li>In asks array, this is lowest price is aggregation level</li></ul> |
| `'s'` | string         | Symbol                                                                                                                                        |
| `'t'` | number         | Timestamp in milliseconds                                                                                                                     |
# Account leverage

Refer to [Websocket](https://docs.pacifica.fi/api-documentation/api/websocket) for establishing the websocket connection.

## Leverage

### Params

```json
{
    "method": "subscribe",
    "params": {
        "source": "account_leverage",
        "account": "42trU9A5..."
    }
}
```

### Stream

```json
{
    "channel": "account_leverage",
    "data": {
        "u": "42trU9A5..."
        "s": "BTC",
        "l": "12",
        "t": 1234567890
    }
}
```

<table><thead><tr><th width="249">Field</th><th>Type</th><th>Description</th></tr></thead><tbody><tr><td><code>'u'</code></td><td>string</td><td>Account address</td></tr><tr><td><code>'s'</code></td><td>string</td><td>Symbol</td></tr><tr><td><code>'l'</code></td><td>integer string</td><td>New leverage</td></tr><tr><td><code>'t'</code></td><td>number</td><td>Timestamp in milliseconds</td></tr></tbody></table>

# Account info

Refer to [Websocket](https://docs.pacifica.fi/api-documentation/api/websocket) for establishing the websocket connection.

## Leverage

### Params

```json
{
    "method": "subscribe",
    "params": {
        "source": "account_info",
        "account": "42trU9A5..."
    }
}
```

### Stream

```json
{
    "channel": "account_info",
    "data": {
        "ae": "2000",
        "as": "1500",
        "aw": "1400",
        "b": "2000",
        "f": 1,
        "mu": "500",
        "cm": "400",
        "oc": 10,
        "pb": "0",
        "pc": 2,
        "sc": 2,
        "t": 1234567890
    }
}
```

<table><thead><tr><th width="249">Field</th><th>Type</th><th>Description</th></tr></thead><tbody><tr><td><code>'ae'</code></td><td>string</td><td>Account equity</td></tr><tr><td><code>'as'</code></td><td>string</td><td>Available to spend</td></tr><tr><td><code>'aw'</code></td><td>string</td><td>Availale to withdraw</td></tr><tr><td><code>'b'</code></td><td>string</td><td>Account balance</td></tr><tr><td><code>'f'</code></td><td>integer</td><td>Account fee tier</td></tr><tr><td><code>'mu'</code></td><td>string</td><td>Total margin used</td></tr><tr><td><code>'cm'</code></td><td>string</td><td>Maintenance margin required in cross mode</td></tr><tr><td><code>'oc'</code></td><td>integer</td><td>Orders count</td></tr><tr><td><code>'pb'</code></td><td>string</td><td>Pending balance</td></tr><tr><td><code>'pc'</code></td><td>integer</td><td>Positions count</td></tr><tr><td><code>'sc'</code></td><td>integer</td><td>Stop order count</td></tr><tr><td><code>'t'</code></td><td>number</td><td>Timestamp in milliseconds</td></tr></tbody></table>

# Account positions

Refer to [Websocket](https://docs.pacifica.fi/api-documentation/api/websocket) for establishing the websocket connection.

### Params

```json
{
    "method": "subscribe",
    "params": {
        "source": "account_positions",
        "account": "42trU9A5..."
    }
}
```

### Positions Snapshots&#x20;

Upon subscription, the `account_positions` websocket immediately returns a snapshot of all current positions, then begins streams all changes made to an account's positions in a best effort picture of current state.  \
\
We recommend using  `account_positions` for initialization, and `account_trades`, to construct up-to-date positions state.

### Stream

```json
{
  "channel": "subscribe",
  "data": {
    "source": "account_positions",
    "account": "BrZp5..."
  }
}
// this is the initialization snapshot
{
  "channel": "account_positions", 
  "data": [
    {
      "s": "BTC",
      "d": "bid",
      "a": "0.00022",
      "p": "87185",
      "m": "0",
      "f": "-0.00023989",
      "i": false,
      "l": null,
      "t": 1764133203991
    }
  ],
  "li": 1559395580
}
// this shows the position being increased by an order filling
{
  "channel": "account_positions",
  "data": [
    {
      "s": "BTC",
      "d": "bid",
      "a": "0.00044",
      "p": "87285.5",
      "m": "0",
      "f": "-0.00023989",
      "i": false,
      "l": "-95166.79231",
      "t": 1764133656974
    }
  ],
  "li": 1559412952
}
// this shows the position being closed
{
  "channel": "account_positions",
  "data": [],
  "li": 1559438203
}
```

<table><thead><tr><th width="196.800048828125">Field</th><th width="187.4000244140625">Type</th><th>Description</th></tr></thead><tbody><tr><td><code>'s'</code></td><td>string</td><td>Symbol</td></tr><tr><td><code>'d'</code></td><td>string</td><td>Position side (bid, ask)</td></tr><tr><td><code>'a'</code></td><td>decimal string</td><td>Position amount</td></tr><tr><td><code>'p'</code></td><td>decimal string</td><td>Average entry price</td></tr><tr><td><code>'m'</code></td><td>decimal string</td><td>Position margin</td></tr><tr><td><code>'f'</code></td><td>decimal string</td><td>Position funding fee</td></tr><tr><td><code>'i'</code></td><td>bool</td><td>Is position isolated?</td></tr><tr><td><code>'l'</code></td><td>decimal string</td><td>Liquidation price in USD (null if not applicable)</td></tr><tr><td><code>'t'</code></td><td>number</td><td>Timestamp in milliseconds</td></tr><tr><td><code>'li'</code></td><td>number</td><td>Exchange-wide nonce. Used to reliably determine exchange event ordering. Sequential and not subject to clock drift.</td></tr></tbody></table>

# Account orders

Refer to [Websocket](https://docs.pacifica.fi/api-documentation/api/websocket) for establishing the websocket connection.

### Params

```json
{
    "method": "subscribe",
    "params": {
        "source": "account_orders",
        "account": "42trU9A5..."
    }
}
```

### Orders Snapshots&#x20;

Upon subscription, the `account_orders` websocket immediately returns a snapshot of all current orders, then begins streams all changes made to an account's orders in a best effort picture of current state.  \
\
We recommend using  `account_orders` for initialization, and `account_order_updates` to construct up-to-date local order state.

### Stream

```json
{
  "channel": "account_orders",
  "data": [
    {
      "i": 1879999120,
      "I": null,
      "s": "BTC",
      "d": "bid",
      "p": "80000",
      "a": "0.00025",
      "f": "0",
      "c": "0",
      "t": 1765935070713,
      "st": null,
      "ot": "limit",
      "sp": null,
      "ro": false
    }
  ],
  "li": 1880004176
} // this is the initialization snapshot with an existing order

{
  "channel": "account_orders",
  "data": [
    {
      "i": 1880009776,
      "I": null,
      "s": "BTC",
      "d": "bid",
      "p": "81000",
      "a": "0.00024",
      "f": "0",
      "c": "0",
      "t": 1765935092314,
      "st": null,
      "ot": "limit",
      "sp": null,
      "ro": false
    },
    {
      "i": 1879999120,
      "I": null,
      "s": "BTC",
      "d": "bid",
      "p": "80000",
      "a": "0.00025",
      "f": "0",
      "c": "0",
      "t": 1765935070713,
      "st": null,
      "ot": "limit",
      "sp": null,
      "ro": false
    }
  ],
  "li": 1880009851
} // this is an update after another order was placed
```

<table><thead><tr><th width="173.800048828125">Field</th><th width="214.199951171875">Type</th><th>Description</th></tr></thead><tbody><tr><td><code>'i'</code></td><td>integer</td><td>Order ID</td></tr><tr><td><code>'I'</code></td><td>Full UUID string</td><td>Client order ID</td></tr><tr><td><code>'s'</code></td><td>string</td><td>Symbol</td></tr><tr><td><code>'d'</code></td><td>string</td><td>Side: [<code>bid</code>, <code>ask</code>]</td></tr><tr><td><code>'p'</code></td><td>decimal string</td><td>Average filled price</td></tr><tr><td><code>'a'</code></td><td>decimal string</td><td>Original amount</td></tr><tr><td><code>'f'</code></td><td>decimal string</td><td>Filled amount</td></tr><tr><td><code>'c'</code></td><td>decimal string</td><td>Cancelled amount</td></tr><tr><td><code>'t'</code></td><td>integer</td><td>Timestamp (milliseconds)</td></tr><tr><td><code>'st'</code></td><td>string</td><td>Stop type (TP/SL)</td></tr><tr><td><code>'ot'</code></td><td>string</td><td>Order type [<code>market</code>, <code>limit</code>]</td></tr><tr><td><code>'sp'</code></td><td>string</td><td>Stop price</td></tr><tr><td><code>'ro'</code></td><td>bool</td><td>Reduce only</td></tr><tr><td><code>'li'</code></td><td>integer</td><td>Exchange-wide nonce. Used to reliably determine exchange event ordering. Sequential and not subject to clock drift.</td></tr></tbody></table>
