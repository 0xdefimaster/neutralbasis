# Read-only WebSocket: proposal and security analysis

**Status:** NOT ENABLED. This is a proposal; enabling it is the owner's decision.

**Correction to doc 31:** doc 31 said "the venv has no WebSocket client". That was wrong:
- `extended_rfq.WebSocket` is a minimal RFC 6455 client already in the repo.
- `v2/marketdata.TxFlowDepthStream` uses it for TxFlow `l2BookDepth`, off by default.
- `aiohttp 3.14.5` is installed.

What is missing is a policy for it, not a client.

## Why

- **INIT** (Phase 9, doc 34): REST polling cannot meet the 2 s alignment.
  - The Hyperliquid ctx and book reads alone are 2.5–7.8 s apart when read in sequence.
  - The Variational quote is 1.4–56 s old.
- **Push data:** a WS push of the HL book and asset context would carry source times only milliseconds apart.
- **Variational is still the limit:**
  - Variational WS: **UNKNOWN**. The public docs read on 2026-11-12 describe REST `/metadata/v2/stats` only.
  - To verify: read docs.variational.io/technical-documentation for a WS section; never probe an undocumented endpoint.

## The security problem

`pilot/network_guard.py` checks:

| Event | What is checked |
|---|---|
| `socket.getaddrinfo` | Host allowlist |
| `socket.connect` | Address |
| `http.client.send` | Method, path and body |

A WebSocket opened with its own TLS socket (`extended_rfq.WebSocket`) passes only the host check. Its upgrade request and every frame bypass `http.client`, so **no path or message is inspected**.

On `api.hyperliquid.xyz/ws` that matters. The same socket accepts `{"method": "post", "request": {"type": "action", ...}}`, which carries signed exchange actions, alongside the read-only subscriptions. A frame-level allowlist is therefore required. Without one, the guard would allow a WS that could carry an order frame if any code ever sent one.

## Proposed design (to implement only after approval)

1. **One read-only client:** `v2/ws_readonly.py` wraps `extended_rfq.WebSocket`. Every outgoing frame goes through `check_frame(host, text)` before `send`.
2. **Allowlist per host** (anything else raises and closes the socket):

   | Host | Allowed frames |
   |---|---|
   | `api.hyperliquid.xyz` /ws | `{"method":"subscribe"/"unsubscribe","subscription":{"type": T, "coin": C}}`, T ∈ {`l2Book`, `activeAssetCtx`, `trades`}, C a listed coin or `xyz:` coin; `{"method":"ping"}` |
   | `rpc.txflow.com` /ws | the existing `l2BookDepth` subscribe/unsubscribe and ping (`v2/marketdata.py`) |
   | other | none |

   The `post` method, any `action`, `signature`, `nonce`, `vaultAddress`, `user` or address field, and any type outside the list are refused. This is the same rule as `v2.network_policy.has_account_key` for HTTP.
3. **Upgrade path pinned:** the client opens only `wss://api.hyperliquid.xyz/ws` and `wss://rpc.txflow.com/ws`. The guard's host allowlist stays as it is.
4. **Incoming frames are data, never instructions:** they are parsed for `time`, `seq` and levels only. Every frame records its source time and receive time, as REST answers do.
5. **Switch:** `NB_V2_READONLY_WS=1`, off by default. It is refused when `NB_V2=1` is combined with anything except the documented subscriptions, and in any mode with trading credentials. PAPER has none.
6. **Tests before enabling:**
   - every allowed frame passes;
   - `post`, `action`, unknown types, address fields and binary frames are refused;
   - the socket closes on a refused frame;
   - a reconnect re-subscribes only allowed frames;
   - integrity: TxFlow `seq`/`prevSeq` gap → resync (already in `SeqBook`), HL `time` monotonic.

## What it would and would not fix

| Would fix | Would not fix |
|---|---|
| HL ctx/book skew (ms instead of seconds) | Variational quote age (1–56 s; refresh about every 40 s) |
| TxFlow book freshness (push with sequence numbers) | TxFlow settled funding (not published at all) |
| Fewer REST requests, so fewer 429s on shared limits | INIT 2 s alignment while the Variational quote is old |

## Owner decision needed

- Approve a network-policy change that adds a frame-level allowlist and enables a read-only WS for Hyperliquid (xyz, native) and TxFlow; or keep REST only.
- Either way, no order path is involved.
