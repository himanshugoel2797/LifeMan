# Auth

Two classes of bearer credential. The unauthenticated UI is templated
with the master token; paired devices carry their own tokens. The
middleware decides whether a given (peer, token) pair is allowed.

## Why two tokens

The web UI renders server-side and the page itself templates
`window.LIFEMAN_TOKEN` so browser JS can `fetch('/api/...')` with the
right `Authorization` header. That works because the server binds to
loopback and the browser is on the same machine — no other process
ever sees the token in transit.

That model breaks the moment a phone or tablet wants to talk to the
kernel from elsewhere on the LAN. Reusing the master token on those
clients leaks it to anything that can intercept LAN traffic; refusing
to bind off-loopback at all means the companion app architecture in
[CLIENT_DESIGN.MD](../../CLIENT_DESIGN.MD) is dead on arrival.

So the kernel issues *device tokens* — long-lived, per-device, scoped
to the API surface only.

## Credentials

| | Master token | Device token |
|-|--------------|--------------|
| Source | `LIFEMAN_TOKEN` env var (random per install) | Issued via pairing flow |
| Templated into UI | Yes | No |
| Storage | Process env / config | Hashed (sha256) in `device_tokens` |
| Loopback only? | Yes (rejected over the wire) | No (network-OK) |
| Mints pairing codes? | Yes | No (a paired device cannot bring in more devices) |
| Revocation | Restart with new token | `DELETE /api/auth/devices/{id}` |

## Pairing flow

1. **User on the host** opens `/system`, hits *Generate pairing code*.
   That POSTs `/api/auth/pairing-codes` with the master token.
   - Server returns an 8-character Crockford-base32 code (no
     `0`/`O`/`1`/`I`/`L` to remove ambiguity), single-use, 5-minute TTL.
   - Code is shown in the UI for the user to type into the device or
     scan as a QR.
2. **The new device** POSTs `/api/auth/pair` with
   `{code, name, platform, capabilities}`. No bearer token required —
   the pairing code *is* the credential here.
   - Server atomically marks the code consumed, generates a 256-bit
     token via `secrets.token_urlsafe(32)`, hashes it with SHA-256,
     and inserts a row into `device_tokens`.
   - Plaintext token is returned exactly once in the response. After
     that, only the hash is on disk — losing the DB does not leak it.
3. **Device** stores the plaintext token in OS-level secure storage
   (Android Keystore-backed `EncryptedSharedPreferences`, Windows
   DPAPI) and uses it as a normal `Authorization: Bearer …` from then
   on.

## Where the gates fire

```
                              ┌────────────────────────────────────┐
incoming request ──→  middleware  ──→  require_auth  ──→  route    │
                                                                   │
       ┌─ loopback peer ───────→ pass through to require_auth      │
       │                                                           │
       ├─ non-loopback peer + LIFEMAN_ALLOW_NETWORK=false           │
       │     → 403 at the middleware (no further checks)            │
       │                                                           │
       └─ non-loopback peer + allow_network=true                    │
             /api/* → pass through, require_auth gates              │
             /, /static, /events (UI) → 403                          │
                                                                   │
                              require_auth                           │
                                                                   │
            no token / unknown token   →  401                        │
            master token + loopback    →  ok, principal=master       │
            master token + non-loopback →  401 (never goes over wire)│
            device token, not revoked  →  ok, principal=device       │
            device token, revoked      →  401                         │
                                                                   ▼
```

Two layers because the middleware runs *before* dependency injection,
so it sees the peer host but doesn't know what token (if any) the
caller will produce. We use it to keep the UI surface (templates,
static files) loopback-only even when the API is opened to the LAN.

## Endpoints

| Method | Path | Caller | Purpose |
|--------|------|--------|---------|
| `POST` | `/api/auth/pairing-codes` | Master (loopback) | Mint a single-use code (5-min TTL) |
| `POST` | `/api/auth/pair` | Anyone (no auth) | Consume a code, return device token |
| `GET`  | `/api/auth/devices` | Master or device | List paired devices |
| `DELETE` | `/api/auth/devices/{id}` | Master, or device-revoking-self | Revoke; subsequent requests 401 |

## SSE / WebSocket

`EventSource` and `WebSocket` constructors can't set custom headers,
so `/events` and the build-chat terminal accept `?token=<bearer>`.
The same rules apply — master tokens loopback-only, device tokens
network-OK once `LIFEMAN_ALLOW_NETWORK=true`.

## Threat model and what we explicitly don't defend against

* **Loopback peer impersonation.** Anything else on `127.0.0.1` is
  trusted. If you give untrusted local users shell access on the host
  running lifeman, they can read `LIFEMAN_TOKEN` from the env / config
  anyway. This is a single-user system.
* **Compromised paired device.** If an attacker pulls the device
  token off a phone, they get whatever that device can do (today: full
  API access). Mitigation today: revoke + re-pair. Future scoped
  capabilities would let the user limit the blast radius.
* **MITM between device and host.** We don't ship TLS termination — if
  you're routing this beyond the LAN, terminate TLS at a reverse proxy
  (nginx, caddy) and put lifeman behind it. The device-token model
  *requires* a confidential channel between phone and host; on a
  shared/untrusted network you must add one.
* **Brute force of pairing codes.** 30^8 ≈ 6.6e11 codespace and a
  5-minute TTL with single-use semantics make online enumeration
  unattractive, but we don't add per-IP throttling. If you expose the
  pairing endpoint over the public internet (don't), put a rate
  limiter in front.

## Code map

* [src/lifeman/devices.py](../../src/lifeman/devices.py) — code
  generation, hashing, lookup, revoke.
* [src/lifeman/auth.py](../../src/lifeman/auth.py) — `require_auth`
  dependency and the `Principal` it returns.
* [src/lifeman/routes/auth.py](../../src/lifeman/routes/auth.py) —
  HTTP endpoints listed above.
* [src/lifeman/main.py](../../src/lifeman/main.py) —
  `_refuse_non_loopback_clients` middleware and `_enforce_loopback_only`
  bind check.
* [tests/test_auth_pairing.py](../../tests/test_auth_pairing.py) —
  end-to-end coverage of the flow.
