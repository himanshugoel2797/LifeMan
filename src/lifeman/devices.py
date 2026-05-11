"""Device pairing and per-device bearer credentials.

The master `LIFEMAN_TOKEN` is templated into the unauthenticated UI and
must stay loopback-only. To let the Android / Windows / wearable companion
apps reach the kernel from elsewhere on the LAN we issue *device tokens*:
long-lived per-device bearer credentials, stored hashed at rest, scoped
to the API surface only.

Pairing flow (see docs/concepts/auth.md):

    1. Loopback caller (`POST /api/auth/pairing-codes`) generates a short
       human-typeable code with a 5-minute TTL.
    2. The new device — over the network — calls `POST /api/auth/pair`
       with `{code, name, platform, capabilities}`. If the code is unused
       and unexpired, the server consumes it, issues a token, and returns
       the plaintext to the caller exactly once.
    3. Subsequent device requests authenticate with that token in the
       `Authorization: Bearer …` header.

Tokens are hashed with SHA-256 before persistence — losing the DB does
not leak credentials. Plaintext is never logged.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from lifeman.db import get_db


# Crockford base32 alphabet: omits the visually ambiguous 0/O and 1/I/L
# so a paired-from-screen code is unambiguous when typed by hand.
_PAIRING_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
_PAIRING_CODE_LEN = 8
_PAIRING_TTL_MINUTES = 5

# Plaintext token length before base64url. 32 bytes → 256 bits of entropy
# is overkill by every practical measure but matches `secrets.token_urlsafe(32)`
# elsewhere in the codebase.
_TOKEN_BYTES = 32


@dataclass(frozen=True)
class PairingCode:
    code: str
    expires_at: str
    issued_at: str


@dataclass(frozen=True)
class IssuedDeviceToken:
    """Returned to the device exactly once on successful pair."""

    device_id: str
    name: str
    platform: str
    token: str  # plaintext, only at issuance time
    created_at: str


@dataclass(frozen=True)
class DeviceRow:
    id: str
    name: str
    platform: str
    capabilities: dict
    created_at: str
    last_seen_at: str | None
    revoked_at: str | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_token(token: str) -> str:
    """SHA-256 hex digest. Used for both storage and constant-time lookup.

    SHA-256 is fine here because the input is 256 bits of entropy generated
    by us — we are not defending against an offline attack on a low-entropy
    user password, just preventing plaintext leaks if the DB is exfiltrated.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _generate_code() -> str:
    return "".join(secrets.choice(_PAIRING_ALPHABET) for _ in range(_PAIRING_CODE_LEN))


def _generate_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def _generate_device_id() -> str:
    return secrets.token_hex(8)


# ---------------------------------------------------------------------------
# Pairing codes
# ---------------------------------------------------------------------------


async def issue_pairing_code(note: str = "") -> PairingCode:
    """Mint a fresh single-use pairing code with a 5-minute TTL.

    Collisions are astronomically unlikely (30^8 ≈ 6.6e11 codespace, single-
    use within 5 min) but we still INSERT and surface the IntegrityError
    rather than overwrite a live code.
    """
    db = await get_db()
    now = _now()
    expires = (datetime.now(timezone.utc) + timedelta(minutes=_PAIRING_TTL_MINUTES)).isoformat()
    for _ in range(8):  # retry on the impossibly-unlikely collision
        code = _generate_code()
        try:
            await db.execute(
                "INSERT INTO pairing_codes (code, issued_at, expires_at, note) "
                "VALUES (?, ?, ?, ?)",
                (code, now, expires, note),
            )
            await db.commit()
            return PairingCode(code=code, issued_at=now, expires_at=expires)
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError("could not allocate a unique pairing code after 8 attempts")


async def consume_pairing_code(
    code: str,
    *,
    name: str,
    platform: str,
    capabilities: dict | None = None,
) -> IssuedDeviceToken:
    """Validate the code, atomically mark it consumed, and mint a device token.

    Raises ``ValueError`` if the code is unknown, already consumed, or expired.
    """
    import json

    db = await get_db()
    now = _now()
    rows = await db.execute_fetchall(
        "SELECT * FROM pairing_codes WHERE code = ?", (code,)
    )
    if not rows:
        raise ValueError("unknown pairing code")
    row = dict(rows[0])
    if row["consumed_at"] is not None:
        raise ValueError("pairing code already consumed")
    if row["expires_at"] <= now:
        raise ValueError("pairing code expired")

    device_id = _generate_device_id()
    token = _generate_token()
    token_hash = _hash_token(token)

    # Atomic consume: only mark the code consumed if it's still unconsumed.
    # If a second pair request lands at the same instant, exactly one wins.
    cur = await db.execute(
        "UPDATE pairing_codes SET consumed_at = ?, device_id = ? "
        "WHERE code = ? AND consumed_at IS NULL",
        (now, device_id, code),
    )
    if cur.rowcount == 0:
        raise ValueError("pairing code already consumed")

    await db.execute(
        "INSERT INTO device_tokens "
        "(id, name, platform, token_hash, capabilities_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            device_id,
            name,
            platform,
            token_hash,
            json.dumps(capabilities or {}),
            now,
        ),
    )
    await db.commit()

    # Register the device's output channel so the router can dispatch
    # to it without a kernel restart. Imported lazily to keep the module
    # import-clean (devices.py is loaded by auth.py during request auth,
    # before the outputs subsystem is necessarily ready).
    from lifeman.outputs.channels.devices import register_device_channel
    register_device_channel(device_id, name, capabilities or {})

    return IssuedDeviceToken(
        device_id=device_id,
        name=name,
        platform=platform,
        token=token,
        created_at=now,
    )


# ---------------------------------------------------------------------------
# Device-token lookup / management
# ---------------------------------------------------------------------------


async def lookup_device_by_token(token: str) -> DeviceRow | None:
    """Return the device row matching this plaintext token, or None.

    Hashes the input and matches against ``token_hash`` — constant-time
    lookup at the index level, no plaintext compare. Revoked rows return
    ``None`` so callers don't need to re-check.
    """
    import json

    if not token:
        return None
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM device_tokens WHERE token_hash = ?",
        (_hash_token(token),),
    )
    if not rows:
        return None
    r = dict(rows[0])
    if r.get("revoked_at"):
        return None
    try:
        caps = json.loads(r.get("capabilities_json") or "{}")
    except json.JSONDecodeError:
        caps = {}
    return DeviceRow(
        id=r["id"],
        name=r["name"],
        platform=r.get("platform") or "",
        capabilities=caps,
        created_at=r["created_at"],
        last_seen_at=r.get("last_seen_at"),
        revoked_at=r.get("revoked_at"),
    )


async def touch_last_seen(device_id: str) -> None:
    """Best-effort update of the last-seen timestamp; cheap, but not on the
    hot path of every request — callers decide when to invoke."""
    db = await get_db()
    await db.execute(
        "UPDATE device_tokens SET last_seen_at = ? WHERE id = ?",
        (_now(), device_id),
    )
    await db.commit()


async def list_devices(*, include_revoked: bool = True) -> list[DeviceRow]:
    import json

    db = await get_db()
    if include_revoked:
        rows = await db.execute_fetchall(
            "SELECT * FROM device_tokens ORDER BY created_at DESC"
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM device_tokens WHERE revoked_at IS NULL "
            "ORDER BY created_at DESC"
        )
    out: list[DeviceRow] = []
    for r in rows:
        d = dict(r)
        try:
            caps = json.loads(d.get("capabilities_json") or "{}")
        except json.JSONDecodeError:
            caps = {}
        out.append(
            DeviceRow(
                id=d["id"],
                name=d["name"],
                platform=d.get("platform") or "",
                capabilities=caps,
                created_at=d["created_at"],
                last_seen_at=d.get("last_seen_at"),
                revoked_at=d.get("revoked_at"),
            )
        )
    return out


async def revoke_device(device_id: str) -> bool:
    """Mark the device revoked. Returns False if the id doesn't exist or
    was already revoked."""
    db = await get_db()
    cur = await db.execute(
        "UPDATE device_tokens SET revoked_at = ?, "
        "push_transport = NULL, push_endpoint = NULL "
        "WHERE id = ? AND revoked_at IS NULL",
        (_now(), device_id),
    )
    await db.commit()
    if (cur.rowcount or 0) > 0:
        # Drop the output channel so the router stops dispatching to a
        # device whose token will now bounce off auth.
        from lifeman.outputs.channels.devices import unregister_device_channel
        unregister_device_channel(device_id)
        return True
    return False


# ---------------------------------------------------------------------------
# Push endpoint management
# ---------------------------------------------------------------------------
#
# A paired device may register a UnifiedPush distributor endpoint with the
# kernel so we can wake the app when an output is queued and the SSE
# connection is offline. The endpoint URL is opaque to the kernel — the
# distributor issued it and routes incoming POSTs to the device. We only
# need to remember which URL belongs to which device.

PUSH_TRANSPORTS = ("unifiedpush",)


@dataclass(frozen=True)
class PushEndpoint:
    transport: str
    endpoint: str


async def set_device_push_endpoint(
    device_id: str, *, transport: str, endpoint: str,
) -> bool:
    """Store this device's push endpoint. Returns False if the device row
    is missing or revoked."""
    if transport not in PUSH_TRANSPORTS:
        raise ValueError(f"unsupported push transport: {transport!r}")
    db = await get_db()
    cur = await db.execute(
        "UPDATE device_tokens SET push_transport = ?, push_endpoint = ? "
        "WHERE id = ? AND revoked_at IS NULL",
        (transport, endpoint, device_id),
    )
    await db.commit()
    return (cur.rowcount or 0) > 0


async def clear_device_push_endpoint(device_id: str) -> bool:
    """Wipe a device's push endpoint (uninstall, app data clear, 410 Gone)."""
    db = await get_db()
    cur = await db.execute(
        "UPDATE device_tokens SET push_transport = NULL, push_endpoint = NULL "
        "WHERE id = ?",
        (device_id,),
    )
    await db.commit()
    return (cur.rowcount or 0) > 0


async def get_device_push_endpoint(device_id: str) -> PushEndpoint | None:
    """Look up the push endpoint registered for this device, if any."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT push_transport, push_endpoint FROM device_tokens "
        "WHERE id = ? AND revoked_at IS NULL",
        (device_id,),
    )
    if not rows:
        return None
    r = dict(rows[0])
    transport = r.get("push_transport")
    endpoint = r.get("push_endpoint")
    if not transport or not endpoint:
        return None
    return PushEndpoint(transport=transport, endpoint=endpoint)
