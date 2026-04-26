StandX Perps Authentication
⚠️ This document is under construction.

This document explains how to obtain JWT access tokens for the StandX Perps API through wallet signatures.

Prerequisites
Valid wallet address and corresponding private key
Development environment with ed25519 algorithm support
Authentication Flow
1. Prepare Wallet and Temporary ed25519 Key Pair
Prepare Wallet: Ensure you have a blockchain wallet with its address and private key.
Generate Temporary ed25519 Key Pair and requestId
2. Get Signature Data
Request signature data from the server:

Note: Code examples provided below are for reference purposes only and demonstrate the general implementation approach. Adapt them to your specific production environment.

Using curl
curl 'https://api.standx.com/v1/offchain/prepare-signin?chain=<chain>' \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "address": "<your_wallet_address>",
    "requestId": "<base58_encoded_public_key>"
  }'
TypeScript/ES6 Implementation Reference
import axios from "axios";
 
const chain = "bsc"; // or "solana"
const walletAddress = "<your_wallet_address>";
const url = `https://api.standx.com/v1/offchain/prepare-signin?chain=${chain}`;
 
const data = {
  address: walletAddress,
  requestId: requestId, // requestId from previous step
};
 
try {
  const response = await axios.post(url, data, {
    headers: { "Content-Type": "application/json" },
  });
 
  if (response.data.success) {
    const signedData = response.data.signedData;
    // Use signedData for next step
  }
} catch (error) {
  console.error("Request failed:", error.message);
}
Request Parameters
Parameter	Type	Required	Description
chain	string	Yes	Blockchain network: bsc or solana
address	string	Yes	Wallet address
requestId	string	Yes	Base58-encoded ed25519 public key from step 1
Success Response
{
  "success": true,
  "signedData": "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9..."
}
3. Parse and Verify Signature Data
signedData is a JWT string that must be verified using StandX’s public key.

Get Verification Public Key
# Using curl
curl 'https://api.standx.com/v1/offchain/certs'
Example signedData Payload
{
  "domain": "standx.com",
  "uri": "https://standx.com",
  "statement": "Sign in with Ethereum to access more StandX features...",
  "version": "1",
  "chainId": 56,
  "nonce": "74Gd7Plf3a1TMVElc",
  "address": "0x...",
  "requestId": "<requestId>",
  "issuedAt": "2025-10-12T17:46:44.731Z",
  "message": "standx.com wants you to sign in with your Ethereum account:\n...",
  "exp": 1760291384,
  "iat": 1760291204
}
4. Sign the Message
Sign payload.message with your wallet private key to generate the signature.

TypeScript/ES6 Implementation Reference
import { ethers } from "ethers";
 
const provider = new ethers.JsonRpcProvider(
  "https://bsc-dataseed.binance.org/"
);
const privateKey = "<your_wallet_private_key>"; // Keep secure; use environment variables
const wallet = new ethers.Wallet(privateKey, provider);
 
// Sign using the message from the parsed payload
const signature = await wallet.signMessage(payload.message);
5. Get Access Token
Submit the signature and original signedData to the login endpoint.

Optional Parameter:

expiresSeconds (number): Token expiration time in seconds. Defaults to 604800 (7 days) if not specified. This controls how long the JWT access token remains valid before requiring re-authentication.
Security Note: For security best practices, avoid setting excessively long expiration times. Shorter token lifetimes reduce the risk of unauthorized access if a token is compromised. Consider your security requirements when configuring this value.

Using curl
curl 'https://api.standx.com/v1/offchain/login?chain=<chain>' \
  -H 'Content-Type: application/json' \
  --data-raw '{
    "signature": "0x...",
    "signedData": "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9...",
    "expiresSeconds": 604800
  }'
TypeScript/ES6 Implementation Reference
const loginUrl = `https://api.standx.com/v1/offchain/login?chain=${chain}`;
 
try {
  const loginResponse = await axios.post(
    loginUrl,
    {
      signature,
      signedData,
      expiresSeconds, // default: seconds for 7days
    },
    {
      headers: { "Content-Type": "application/json" },
    }
  );
 
  const { token, address, chain } = loginResponse.data;
  // Store token for subsequent API requests
} catch (error) {
  console.error("Login failed:", error.message);
}
Success Response
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "address": "0x...",
  "alias": "user123",
  "chain": "bsc",
  "perpsAlpha": true
}
6. Use Access Token
Use the obtained token for subsequent API requests by adding Authorization: Bearer <token> to the request headers.

Body Signature Flow
Basic Flow
Prepare a key pair
Build message: {version},{id},{timestamp},{payload}
Sign with private key
Base64 encode signature
Attach signature to request headers
{
    ...
    "authorization": "Bearer <token>",
    "x-request-sign-version": "v1",
    "x-request-id": "uuid",
    "x-request-timestamp": "timestamp",
    "x-request-signature": "signature",
    ...
}
Code example (only for reference):
import { ed25519 } from "@noble/curves/ed25519";
import { base58 } from "@scure/base";
import { v4 as uuidv4 } from "uuid";
 
/**
 * Sign request and return Base64-encoded signature.
 */
function encodeRequestSignature(
  xRequestVersion: string,
  xRequestId: string,
  xRequestTimestamp: number,
  payload: string,
  signingKey: Uint8Array
): string {
  // Build message to sign: "{version},{id},{timestamp},{payload}"
  const signMsg = `${xRequestVersion},${xRequestId},${xRequestTimestamp},${payload}`;
 
  // Sign message with Ed25519 private key
  const messageBytes = Buffer.from(signMsg, "utf-8");
  const signature = ed25519.sign(messageBytes, signingKey);
 
  // Base64 encode the signature
  return Buffer.from(signature).toString("base64");
}
 
// --- Example Usage ---
 
// Generate Ed25519 key pair
const privateKey = ed25519.utils.randomSecretKey();
const publicKey = ed25519.getPublicKey(privateKey);
 
// Generate requestId (base58-encoded public key)
const requestId = base58.encode(publicKey);
 
// Prepare request parameters
const xRequestVersion = "v1";
const xRequestId = uuidv4();
const xRequestTimestamp = Date.now();
 
const payloadDict = {
  user_id: 12345,
  data: "some important information",
};
const payloadStr = JSON.stringify(payloadDict);
 
// Generate signature
const signature = encodeRequestSignature(
  xRequestVersion,
  xRequestId,
  xRequestTimestamp,
  payloadStr,
  privateKey
);
 
// Verify signature (optional)
try {
  const verifyMsg = `v1,${xRequestId},${xRequestTimestamp},${payloadStr}`;
  const signatureBytes = Buffer.from(signature, "base64");
  const messageBytes = Buffer.from(verifyMsg, "utf-8");
 
  const isValid = ed25519.verify(signatureBytes, messageBytes, publicKey);
  if (!isValid) throw new Error("Verification failed");
} catch (error) {
  console.error("Signature verification error:", error.message);
}
 
// Send Request with Body Signature
fetch("/api/request_need_body_signature", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    authorization: `Bearer ${token}`,
    "x-request-sign-version": "v1",
    "x-request-id": xRequestId,
    "x-request-timestamp": xRequestTimestamp.toString(),
    "x-request-signature": signature,
  },
  body: payloadStr,
});
Complete Authentication Class Example
Here’s a complete implementation using a class-based approach:

import { ed25519 } from "@noble/curves/ed25519";
import { base58 } from "@scure/base";
 
export type Chain = "bsc" | "solana";
 
export interface SignedData {
  domain: string;
  uri: string;
  statement: string;
  version: string;
  chainId: number;
  nonce: string;
  address: string;
  requestId: string;
  issuedAt: string;
  message: string;
  exp: number;
  iat: number;
}
 
export interface LoginResponse {
  token: string;
  address: string;
  alias: string;
  chain: string;
  perpsAlpha: boolean;
}
 
export interface RequestSignatureHeaders {
  "x-request-sign-version": string;
  "x-request-id": string;
  "x-request-timestamp": string;
  "x-request-signature": string;
}
 
export class StandXAuth {
  private ed25519PrivateKey: Uint8Array;
  private ed25519PublicKey: Uint8Array;
  private requestId: string;
  private baseUrl = "https://api.standx.com";
 
  constructor() {
    const privateKey = ed25519.utils.randomSecretKey();
    this.ed25519PrivateKey = privateKey;
    this.ed25519PublicKey = ed25519.getPublicKey(privateKey);
    this.requestId = base58.encode(this.ed25519PublicKey);
  }
 
  async authenticate(
    chain: Chain,
    walletAddress: string,
    signMessage: (msg: string) => Promise<string>
  ): Promise<LoginResponse> {
    const signedDataJwt = await this.prepareSignIn(chain, walletAddress);
    const payload = this.parseJwt<SignedData>(signedDataJwt);
    const signature = await signMessage(payload.message);
    return this.login(chain, signature, signedDataJwt);
  }
 
  private async prepareSignIn(chain: Chain, address: string): Promise<string> {
    const res = await fetch(
      `${this.baseUrl}/v1/offchain/prepare-signin?chain=${chain}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ address, requestId: this.requestId }),
      }
    );
    const data = await res.json();
    if (!data.success) throw new Error("Failed to prepare sign-in");
    return data.signedData;
  }
 
  private async login(
    chain: Chain,
    signature: string,
    signedData: string,
    expiresSeconds: number = 604800 // default: 7 days
  ): Promise<LoginResponse> {
    const res = await fetch(
      `${this.baseUrl}/v1/offchain/login?chain=${chain}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ signature, signedData, expiresSeconds }),
      }
    );
    return res.json();
  }
 
  signRequest(
    payload: string,
    requestId: string,
    timestamp: number
  ): RequestSignatureHeaders {
    const version = "v1";
    const message = `${version},${requestId},${timestamp},${payload}`;
    const signature = ed25519.sign(
      Buffer.from(message, "utf-8"),
      this.ed25519PrivateKey
    );
 
    return {
      "x-request-sign-version": version,
      "x-request-id": requestId,
      "x-request-timestamp": timestamp.toString(),
      "x-request-signature": Buffer.from(signature).toString("base64"),
    };
  }
 
  private parseJwt<T>(token: string): T {
    const base64Url = token.split(".")[1];
    const base64 = base64Url.replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(Buffer.from(base64, "base64").toString("utf-8"));
  }
}
 
// Usage Example
import { ethers } from "ethers";
 
async function example() {
  // Initialize auth
  const auth = new StandXAuth();
 
  // Setup wallet
  const provider = new ethers.JsonRpcProvider(
    "https://bsc-dataseed.binance.org/"
  );
  const privateKey = process.env.WALLET_PRIVATE_KEY!;
  const wallet = new ethers.Wallet(privateKey, provider);
 
  // Authenticate
  const loginResponse = await auth.authenticate(
    "bsc",
    wallet.address,
    async (message) => wallet.signMessage(message)
  );
 
  console.log("Access Token:", loginResponse.token);
 
  // Sign a request
  const payload = JSON.stringify({
    symbol: "BTC-USD",
    side: "buy",
    order_type: "limit",
    qty: "0.1",
    price: "50000",
    time_in_force: "gtc",
    reduce_only: false,
  });
 
  const headers = auth.signRequest(payload, crypto.randomUUID(), Date.now());
 
  // Make authenticated request
  await fetch("https://perps.standx.com/api/new_order", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${loginResponse.token}`,
      ...headers,
    },
    body: payload,
  });
}

StandX Perps HTTP API List
⚠️ This document is under construction.

API Overview
Base URL
https://perps.standx.com
Authentication
All endpoints except public endpoints require JWT authentication. Include the JWT token in the Authorization header:

Authorization: Bearer <your_jwt_token>
Token Validity: 7 days

Body Signature
Some endpoints require body signature. Add the following headers to signed requests:

x-request-sign-version: v1
x-request-id: <random_string>
x-request-timestamp: <timestamp_in_milliseconds>
x-request-signature: <your_body_signature>
See Authentication Guide for implementation details.

Session ID
For new_order and cancel_order requests, you will want to know the results of these requests after actual matching. To obtain these results, you need to add the following information to the header in these interface requests:

x-session-id: <your_custom_session_id>
Note that this session_id needs to be consistent with the session_id used in your ws-client.

Request Format
int parameters (e.g., timestamp) are expected as JSON integers, not strings
decimal parameters (e.g., price) are expected as JSON strings, not floats
Trade Endpoints
Create New Order
POST /api/new_order

Note: A successful response indicates the order was submitted, not necessarily executed. Some orders (e.g., ALO) may be rejected during matching if conditions are not met. Subscribe to Order Response Stream for real-time execution status.

To receive order updates via Order Response Stream, add the x-session-id header to your request. This session_id must be consistent with the session_id used in your ws-client.

Authentication Required • Body Signature Required

Required Parameters

Parameter	Type	Description
symbol	string	Trading pair (see Reference)
side	enum	Order side (see Reference)
order_type	enum	Order type (see Reference)
qty	decimal	Order quantity
time_in_force	enum	Time in force (see Reference)
reduce_only	boolean	Only reduce position if true
Optional Parameters

Parameter	Type	Description
price	decimal	Order price (required for limit orders)
cl_ord_id	string	Client order ID (auto-generated if omitted)
margin_mode	enum	Margin mode (see Reference). Must match position
leverage	int	Leverage value. Must match position
Request Example:

{
  "symbol": "BTC-USD",
  "side": "buy",
  "order_type": "limit",
  "qty": "0.1",
  "price": "50000",
  "time_in_force": "gtc",
  "reduce_only": false
}
Response Example:

{
  "code": 0,
  "message": "success",
  "request_id": "xxx-xxx-xxx"
}
Cancel Order
POST /api/cancel_order

To receive order updates via Order Response Stream, add the x-session-id header to your request. This session_id must be consistent with the session_id used in your ws-client.

Authentication Required • Body Signature Required

Parameters

At least one of order_id or cl_ord_id is required.

Parameter	Type	Description
order_id	int	Order ID to cancel
cl_ord_id	string	Client order ID to cancel
Request Example:

{
  "order_id": 2424844
}
Response Example:

{
  "code": 0,
  "message": "success",
  "request_id": "xxx-xxx-xxx"
}
Cancel Multiple Orders
POST /api/cancel_orders

Authentication Required • Body Signature Required

Parameters

At least one of order_id_list or cl_ord_id_list is required.

Parameter	Type	Description
order_id_list	int[]	Order IDs to cancel
cl_ord_id_list	string[]	Client order IDs to cancel
Request Example:

{
  "order_id_list": [2424844]
}
Response Example:

[]
Change Leverage
POST /api/change_leverage

Authentication Required • Body Signature Required

Required Parameters

Parameter	Type	Description
symbol	string	Trading pair (see Reference)
leverage	int	New leverage value
Request Example:

{
  "symbol": "BTC-USD",
  "leverage": 10
}
Response Example:

{
  "code": 0,
  "message": "success",
  "request_id": "xxx-xxx-xxx"
}
Change Margin Mode
POST /api/change_margin_mode

Authentication Required • Body Signature Required

Required Parameters

Parameter	Type	Description
symbol	string	Trading pair (see Reference)
margin_mode	enum	Margin mode (see Reference)
Request Example:

{
  "symbol": "BTC-USD",
  "margin_mode": "cross"
}
Response Example:

{
  "code": 0,
  "message": "success",
  "request_id": "xxx-xxx-xxx"
}
User Endpoints
Transfer Margin
POST /api/transfer_margin

Authentication Required • Body Signature Required

Required Parameters

Parameter	Type	Description
symbol	string	Trading pair (see Reference)
amount_in	decimal	Amount to transfer
Request Example:

{
  "symbol": "BTC-USD",
  "amount_in": "1000.0"
}
Response Example:

{
  "code": 0,
  "message": "success",
  "request_id": "xxx-xxx-xxx"
}
Query Order
GET /api/query_order

⚠️ NOTE: Orders may be rejected due mis-qualification due async matching network structure. To receive the order updates in real-time, please check Order Response Stream.

Authentication Required

Query Parameters

At least one of order_id or cl_ord_id is required.

Parameter	Type	Description
order_id	int	Order ID to query
cl_ord_id	string	Client order ID to query
Response Example:

{
  "avail_locked": "3.071880000",
  "cl_ord_id": "01K2BK4ZKQE0C308SRD39P8N9Z",
  "closed_block": -1,
  "created_at": "2025-08-11T03:35:25.559151Z",
  "created_block": -1,
  "fill_avg_price": "0",
  "fill_qty": "0",
  "id": 1820682,
  "leverage": "10",
  "liq_id": 0,
  "margin": "0",
  "order_type": "limit",
  "payload": null,
  "position_id": 15,
  "price": "121900.00",
  "qty": "0.060",
  "reduce_only": false,
  "remark": "",
  "side": "sell",
  "source": "user",
  "status": "open",
  "symbol": "BTC-USD",
  "time_in_force": "gtc",
  "updated_at": "2025-08-11T03:35:25.559151Z",
  "user": "bsc_0x..."
}
Query User Orders
GET /api/query_orders

Authentication Required

Query Parameters

Parameter	Type	Description
symbol	string	Trading pair (see Reference)
status	enum	Order status (see Reference)
order_type	enum	Order type (see Reference)
start	string	Start time in ISO 8601 format
end	string	End time in ISO 8601 format
last_id	number	Last order ID for pagination
limit	number	Results limit (default: 100, max: 500)
Response Example:

{
  "page_size": 1,
  "result": [
    {
      "avail_locked": "3.071880000",
      "cl_ord_id": "01K2BK4ZKQE0C308SRD39P8N9Z",
      "closed_block": -1,
      "created_at": "2025-08-11T03:35:25.559151Z",
      "created_block": -1,
      "fill_avg_price": "0",
      "fill_qty": "0",
      "id": 1820682,
      "leverage": "10",
      "liq_id": 0,
      "margin": "0",
      "order_type": "limit",
      "payload": null,
      "position_id": 15,
      "price": "121900.00",
      "qty": "0.060",
      "reduce_only": false,
      "remark": "",
      "side": "sell",
      "source": "user",
      "status": "new",
      "symbol": "BTC-USD",
      "time_in_force": "gtc",
      "updated_at": "2025-08-11T03:35:25.559151Z",
      "user": "bsc_0x..."
    }
  ],
  "total": 1
}
Query User All Open Orders
GET /api/query_open_orders

Authentication Required

Query Parameters

Parameter	Type	Description
symbol	string	Trading pair (see Reference)
limit	number	Results limit (default: 500, max: 1200)
Response Example:

{
  "page_size": 1,
  "result": [
    {
      "avail_locked": "3.071880000",
      "cl_ord_id": "01K2BK4ZKQE0C308SRD39P8N9Z",
      "closed_block": -1,
      "created_at": "2025-08-11T03:35:25.559151Z",
      "created_block": -1,
      "fill_avg_price": "0",
      "fill_qty": "0",
      "id": 1820682,
      "leverage": "10",
      "liq_id": 0,
      "margin": "0",
      "order_type": "limit",
      "payload": null,
      "position_id": 15,
      "price": "121900.00",
      "qty": "0.060",
      "reduce_only": false,
      "remark": "",
      "side": "sell",
      "source": "user",
      "status": "new",
      "symbol": "BTC-USD",
      "time_in_force": "gtc",
      "updated_at": "2025-08-11T03:35:25.559151Z",
      "user": "bsc_0x..."
    }
  ],
  "total": 1
}
Query User Trades
GET /api/query_trades

Authentication Required

Query Parameters

Parameter	Type	Description
symbol	string	Trading pair (see Reference)
last_id	number	Last trade ID for pagination
side	string	Order side (see Reference)
start	string	Start time in ISO 8601 format
end	string	End time in ISO 8601 format
limit	number	Results limit (default: 100, max: 500)
Response Example:

{
  "page_size": 1,
  "result": [
    {
      "created_at": "2025-08-11T03:36:19.352620Z",
      "fee_asset": "DUSD",
      "fee_qty": "0.121900",
      "id": 409870,
      "order_id": 1820682,
      "pnl": "1.62040",
      "price": "121900",
      "qty": "0.01",
      "side": "sell",
      "symbol": "BTC-USD",
      "updated_at": "2025-08-11T03:36:19.352620Z",
      "user": "bsc_0x...",
      "value": "1219.00"
    }
  ],
  "total": 1
}
Query Position Config
GET /api/query_position_config

Authentication Required

Required Parameters

Parameter	Type	Description
symbol	string	Trading pair (see Reference)
Response Example:

{
  "symbol": "BTC-USD",
  "leverage": 10,
  "margin_mode": "cross"
}
Query User Positions
GET /api/query_positions

Authentication Required

Query Parameters

Parameter	Type	Description
symbol	string	Trading pair (see Reference)
Response Example:

[
  {
    "bankruptcy_price": "109608.01",
    "created_at": "2025-08-10T09:05:50.265265Z",
    "entry_price": "121737.96",
    "entry_value": "114433.68240",
    "holding_margin": "11443.3682400",
    "id": 15,
    "initial_margin": "11443.36824",
    "leverage": "10",
    "liq_price": "112373.50",
    "maint_margin": "2860.30367500",
    "margin_asset": "DUSD",
    "margin_mode": "isolated",
    "mark_price": "121715.05",
    "mmr": "3.993223845366698695025800014",
    "position_value": "114412.14700",
    "qty": "0.940",
    "realized_pnl": "31.61532",
    "status": "open",
    "symbol": "BTC-USD",
    "time": "2025-08-11T03:41:40.922818Z",
    "updated_at": "2025-08-10T09:05:50.265265Z",
    "upnl": "-21.53540",
    "user": "bsc_0x..."
  }
]
Query User Balances
Endpoint: /api/query_balance

Method: GET

Authentication: Required

Description: Unified balance snapshot.

Response Fields:

Name	Type	Description
isolated_balance	decimal	Isolated wallet total
isolated_upnl	decimal	Isolated unrealized PnL
cross_balance	decimal	Cross wallet free balance
cross_margin	decimal	Cross margin used (executed positions only)
cross_upnl	decimal	Cross unrealized PnL
locked	decimal	Order lock (margin + fee), already includes safety factor b
cross_available	decimal	cross_balance - cross_margin - locked + cross_upnl
balance	decimal	Total account assets = cross_balance + isolated_balance
upnl	decimal	Total unrealized PnL = cross_upnl + isolated_upnl
equity	decimal	Account equity = balance + upnl
pnl_freeze	decimal	24h realized PnL (for display)
Response Example:

{
  "isolated_balance": "11443.3682400",
  "isolated_upnl": "-21.53540",
  "cross_balance": "1088575.259316737",
  "cross_margin": "2860.30367500",
  "cross_upnl": "31.61532",
  "locked": "0.000000000",
  "cross_available": "1085746.571",
  "balance": "1100018.627556737",
  "upnl": "10.07992",
  "equity": "1100028.707476657",
  "pnl_freeze": "31.61532"
}
Notes:

cross_available may be negative depending on PnL and locks;
Public Endpoints
Query Symbol Info
GET /api/query_symbol_info

Required Parameters

Parameter	Type	Description
symbol	string	Trading pair (see Reference)
Response Example:

[
  {
    "base_asset": "BTC",
    "base_decimals": 9,
    "created_at": "2025-07-10T05:15:32.089568Z",
    "def_leverage": "10",
    "depth_ticks": "0.01,0.1,1",
    "enabled": true,
    "maker_fee": "0.0001",
    "max_leverage": "20",
    "max_open_orders": "100",
    "max_order_qty": "100",
    "max_position_size": "1000",
    "min_order_qty": "0.001",
    "price_cap_ratio": "0.3",
    "price_floor_ratio": "0.3",
    "price_tick_decimals": 2,
    "qty_tick_decimals": 3,
    "quote_asset": "DUSD",
    "quote_decimals": 9,
    "symbol": "BTC-USD",
    "taker_fee": "0.0004",
    "updated_at": "2025-07-10T05:15:32.089568Z"
  }
]
Query Symbol Market
GET /api/query_symbol_market

Required Parameters

Parameter	Type	Description
symbol	string	Trading pair (see Reference)
Response Example:

{
  "base": "BTC",
  "funding_rate": "0.00010000",
  "high_price_24h": "122164.08",
  "index_price": "121601.158461",
  "last_price": "121599.94",
  "low_price_24h": "114098.44",
  "mark_price": "121602.43",
  "mid_price": "121599.99",
  "next_funding_time": "2025-08-11T08:00:00Z",
  "open_interest": "15.948",
  "quote": "DUSD",
  "spread": ["121599.94", "121600.04"],
  "symbol": "BTC-USD",
  "time": "2025-08-11T03:44:40.922233Z",
  "volume_24h": "9030.51800000000002509"
}
Query Symbol Price
GET /api/query_symbol_price

Required Parameters

Parameter	Type	Description
symbol	string	Trading pair (see Reference)
Response Example:

{
  "base": "BTC",
  "index_price": "121601.158461",
  "last_price": "121599.94",
  "mark_price": "121602.43",
  "mid_price": "121599.99",
  "quote": "DUSD",
  "spread_ask": "121600.04",
  "spread_bid": "121599.94",
  "symbol": "BTC-USD",
  "time": "2025-08-11T03:44:40.922233Z"
}
Note: last_price, mid_price, spread_ask, spread_bid may be null if no recent trades.

Query Depth Book
GET /api/query_depth_book

Required Parameters

Parameter	Type	Description
symbol	string	Trading pair (see Reference)
Response Example:

{
  "asks": [
    ["121895.81", "0.843"],
    ["121896.11", "0.96"]
  ],
  "bids": [
    ["121884.01", "0.001"],
    ["121884.31", "0.001"]
  ],
  "symbol": "BTC-USD"
}
Query Recent Trades
GET /api/query_recent_trades

Required Parameters

Parameter	Type	Description
symbol	string	Trading pair (see Reference)
Response Example:

[
  {
    "is_buyer_taker": true,
    "price": "121720.18",
    "qty": "0.01",
    "quote_qty": "1217.2018",
    "symbol": "BTC-USD",
    "time": "2025-08-11T03:48:47.086505Z"
  },
  {
    "is_buyer_taker": true,
    "price": "121720.18",
    "qty": "0.01",
    "quote_qty": "1217.2018",
    "symbol": "BTC-USD",
    "time": "2025-08-11T03:48:46.850415Z"
  }
]
Query Funding Rates
GET /api/query_funding_rates

Required Parameters

Parameter	Type	Description
symbol	string	Trading pair (see Reference)
start_time	int	Start time in milliseconds
end_time	int	End time in milliseconds
Response Example:

[
  {
    "id": 1,
    "symbol": "BTC-USD",
    "funding_rate": "0.0001",
    "index_price": "121601.158461",
    "mark_price": "121602.43",
    "premium": "0.0001",
    "time": "2025-08-11T03:48:47.086505Z",
    "created_at": "2025-08-11T03:48:47.086505Z",
    "updated_at": "2025-08-11T03:48:47.086505Z"
  }
]
Kline Endpoints
Get Server Time
GET /api/kline/time

Response Example:

1620000000
Get Kline History
GET /api/kline/history

Required Parameters

Parameter	Type	Description
symbol	string	Trading pair (see Reference)
from	u64	Unix timestamp in seconds
to	u64	Unix timestamp in seconds
resolution	enum	Resolution (see Reference)
Optional Parameters

Parameter	Type	Description
countBack	u64	The required amount of bars to load
Response Example:

{
  "s": "ok",
  "t": [1754897028, 1754897031],
  "c": [121897.95, 121903.04],
  "o": [121896.02, 121898.05],
  "h": [121897.95, 121903.15],
  "l": [121895.92, 121898.05],
  "v": [0.09, 10.542]
}
Health Check
Health
GET /api/health

Response:

OK
Misc
Region and Server Time
GET https://geo.standx.com/v1/region

Response Example:

{
  "systemTime": 1761970177865,
  "region": "jp"
}

StandX Perps WebSocket API List
The WebSocket API provides two streams: Market Stream for market data and user account updates, and Order Response Stream for asynchronous order creation responses.

⚠️ This document is under construction.

Connection Management
Both WebSocket streams implement the following connection management behavior:

Ping/Pong Mechanism
Server Ping Interval: The server sends a WebSocket Ping frame every 10 seconds
Client Response: Clients must respond with a Pong frame when receiving a Ping
Timeout: If the server does not receive a Ping/Pong response within 5 minutes, the connection will be terminated with error:
{
  "code": 408,
  "message": "disconnecting due to not receive Pong within 5 minute period"
}
Note: Most modern browsers and WebSocket libraries automatically handle ping/pong frames, so you might not need to implement this manually. However, if your environment doesn’t support automatic ping/pong handling, you can proactively send ping frames to the server. Example using the npm ws library:

import WebSocket from "ws";
// ...
private ws: WebSocket;
//...
ping(): void {
  this.lastPingTime = Date.now();
  this.ws.ping();
  console.log(`[${new Date().toISOString()}] Ping server`);
}
Market Stream
Base Endpoint: wss://perps.standx.com/ws-stream/v1

Available Channels
[
  // public channels
  { channel: "price", symbol: "<symbol>" },
  { channel: "depth_book", symbol: "<symbol>" },
  { channel: "public_trade", symbol: "<symbol>" },
  // user-level authenticated channels
  { channel: "order" },
  { channel: "position" },
  { channel: "balance" },
  { channel: "trade" },
]
Subscribe to Depth Book
Request:
{ "subscribe": { "channel": "depth_book", "symbol": "BTC-USD" } }
Response:
{
  "seq": 3,
  "channel": "depth_book",
  "symbol": "BTC-USD",
  "data": {
    "asks": [
      ["121896.02", "0.839"],
      ["121896.32", "1.051"]
    ],
    "bids": [
      ["121884.22", "0.001"],
      ["121884.52", "0.001"]
    ],
    "symbol": "BTC-USD"
  }
}
Subscribe to Symbol Price
Request:
{ "subscribe": { "channel": "price", "symbol": "BTC-USD" } }
Response:
{
  "seq": 13,
  "channel": "price",
  "symbol": "BTC-USD",
  "data": {
    "base": "BTC",
    "index_price": "121890.651250",
    "last_price": "121897.95",
    "mark_price": "121897.56",
    "mid_price": "121898.00",
    "quote": "DUSD",
    "spread": ["121897.95", "121898.05"],
    "symbol": "BTC-USD",
    "time": "2025-08-11T07:23:50.923602474Z"
  }
}
Authentication Request
Log in with JWT
Request:
{
  "auth": {
    "token": "<your_jwt_token>",
    "streams": [{ "channel": "order" }]
  }
}
auth.streams is Optional, which enables the user to subscribe to specific channels right after authentication.

Response:
{ "seq": 1, "channel": "auth", "data": { "code": 200, "msg": "success" } }
User Orders Subscription
Request:
{ "subscribe": { "channel": "order" } }
Response:
{
  "seq": 35,
  "channel": "order",
  "data": {
    "avail_locked": "0",
    "cl_ord_id": "01K2C9H93Y42RW8KD6RSVWVDVV",
    "closed_block": -1,
    "created_at": "2025-08-11T10:06:37.182464902Z",
    "created_block": -1,
    "fill_avg_price": "121245.21",
    "fill_qty": "1.000",
    "id": 2547027,
    "leverage": "15",
    "liq_id": 0,
    "margin": "8083.013333334",
    "order_type": "market",
    "payload": null,
    "position_id": 15,
    "price": "121245.20",
    "qty": "1.000",
    "reduce_only": false,
    "remark": "",
    "side": "buy",
    "source": "user",
    "status": "filled",
    "symbol": "BTC-USD",
    "time_in_force": "ioc",
    "updated_at": "2025-08-11T10:06:37.182465022Z",
    "user": "bsc_0x..."
  }
}
User Position Subscription
Request:
{ "subscribe": { "channel": "position" } }
Response:
{
  "seq": 36,
  "channel": "position",
  "data": {
    "created_at": "2025-08-10T09:05:50.265265Z",
    "entry_price": "121677.65",
    "entry_value": "2879988.1154631481396099405228",
    "id": 15,
    "initial_margin": "191999.219856667",
    "leverage": "15",
    "margin_asset": "DUSD",
    "margin_mode": "isolated",
    "qty": "23.669",
    "realized_pnl": "158.197103148",
    "status": "open",
    "symbol": "BTC-USD",
    "updated_at": "2025-08-10T09:05:50.265265Z",
    "user": "bsc_0x..."
  }
}
User Balance Subscription
Request:
{ "subscribe": { "channel": "balance" } }
Response:
{
  "seq": 37,
  "channel": "balance",
  "data": {
    "account_type": "perps",
    "created_at": "2025-08-09T09:36:54.504639Z",
    "free": "906946.976225666",
    "id": "bsc_0x...",
    "inbound": "0",
    "is_enabled": true,
    "kind": "user",
    "last_tx": "",
    "last_tx_updated_at": 0,
    "locked": "0.000000000",
    "occupied": "0",
    "outbound": "0",
    "ref_id": 0,
    "token": "DUSD",
    "total": "923207.752500717",
    "updated_at": "2025-08-09T09:36:54.504639Z",
    "version": 0,
    "wallet_id": "bsc_0x..."
  }
}
Order Response Stream
This WebSocket channel provides real-time order status updates for the new order API. Since order creation is asynchronous, this channel notifies clients about order responses, including ALO order rejections.

Base Endpoint: wss://perps.standx.com/ws-api/v1

Request Structure
All WebSocket requests follow this structure:

{
  "session_id": "<uuid>",
  "request_id": "<uuid>",
  "method": "<method>",
  "header": {
    "x-request-id": "",
    "x-request-timestamp": "",
    "x-request-signature": ""
  },
  "params": "<json string>"
}
Fields:

session_id: UUID that remains consistent throughout the session
request_id: Unique UUID for each request
method: Operation to perform (auth:login, order:new, order:cancel)
header: Required for order:new and order:cancel methods (authentication headers)
params: JSON-stringified parameters specific to the method
Methods
auth:login
Authenticate using JWT token.

Parameters:

{ "token": "<jwt>" }
Example Request:

{
  "session_id": "consistent-session-id",
  "request_id": "unique-request-id",
  "method": "auth:login",
  "params": "{\"token\":\"your.jwt.token\"}"
}
order:new
Create a new order. Parameters are the same as the HTTP API new_order payload.

order:cancel
Cancel an existing order. Parameters are the same as the HTTP API cancel_order payload.

Order Response Format
Success Response:

{
  "code": 0,
  "message": "success",
  "request_id": "<UUID>"
}
Rejection Response:

{
  "code": 400,
  "message": "alo order rejected",
  "request_id": "<UUID>"
}

StandX Perps API Reference
⚠️ This document is under construction.

Enums
Symbol
Available symbols (Trading pairs):

BTC-USD
Margin Mode
cross
isolated
Token
Available tokens:

DUSD
Order Side (side)
buy
sell
Order Type (order_type)
limit
market
Order Status (status)
open
canceled
filled
rejected
untriggered
Time In Force (time_in_force)
Value	Description
gtc	Good Til Canceled - Order remains active until canceled
ioc	Immediate Or Cancel - Fill as much as possible immediately, cancel the rest
alo	Add Liquidity Only - Order added to book without immediate execution; only executes as resting order
Resolution
Kline resolutions:

1T - 1 tick
3S - 3 seconds
1 - 1 minute
5 - 5 minutes
15 - 15 minutes
60 - 60 minutes (1 hour)
1D - 1 day
1W - 1 week
1M - 1 month
Error Responses
Common Error Codes
Code	Description
400	Bad Request - Invalid request parameters
401	Unauthorized - Authentication required or invalid token
403	Forbidden - Insufficient permissions
404	Not Found - Resource not found
429	Too Many Requests - Rate limit exceeded
500	Internal Server Error - Server error