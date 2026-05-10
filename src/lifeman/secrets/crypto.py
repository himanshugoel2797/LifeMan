"""Master-key resolution and AES-256-GCM helpers.

The master key is the only thing protecting at-rest secrets. It is
deliberately kept *outside* the SQLite DB so a leaked DB backup alone
doesn't disclose values.
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from lifeman.config import settings

log = logging.getLogger("lifeman.secrets.crypto")

_KEY_BYTES = 32   # AES-256
_NONCE_BYTES = 12  # GCM standard

_cached_key: bytes | None = None


def _key_file() -> Path:
    return settings.data_dir / "master.key"


def resolve_master_key() -> bytes:
    """Find or create the master key. Memoised after the first call.

    Resolution order:
      1. `LIFEMAN_MASTER_KEY` env var (urlsafe base64, 32 bytes).
      2. `<data_dir>/master.key` (raw 32 bytes, mode 0600).
      3. Auto-generated, written to (2). Logged at WARNING — back it up.
    """
    global _cached_key
    if _cached_key is not None:
        return _cached_key

    env = os.environ.get("LIFEMAN_MASTER_KEY")
    if env:
        try:
            key = base64.urlsafe_b64decode(env)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"LIFEMAN_MASTER_KEY is not valid base64: {e}") from e
        if len(key) != _KEY_BYTES:
            raise RuntimeError(f"LIFEMAN_MASTER_KEY must decode to {_KEY_BYTES} bytes")
        _cached_key = key
        return key

    path = _key_file()
    if path.exists():
        key = path.read_bytes()
        if len(key) != _KEY_BYTES:
            raise RuntimeError(
                f"{path} has wrong length ({len(key)} bytes, expected {_KEY_BYTES})"
            )
        _cached_key = key
        return key

    path.parent.mkdir(parents=True, exist_ok=True)
    key = AESGCM.generate_key(bit_length=_KEY_BYTES * 8)
    # Write atomically with restrictive perms.
    tmp = path.with_suffix(".key.tmp")
    tmp.write_bytes(key)
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    log.warning(
        "generated new master key at %s — secrets are now bound to this file. "
        "Back it up alongside the DB; without it you cannot decrypt secrets.",
        path,
    )
    _cached_key = key
    return key


def reset_cache_for_tests() -> None:
    """Tests reset the cached key when they swap data_dir."""
    global _cached_key
    _cached_key = None


def encrypt(value: str) -> tuple[bytes, bytes]:
    """Encrypt UTF-8 `value`. Returns (ciphertext, nonce)."""
    key = resolve_master_key()
    nonce = os.urandom(_NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, value.encode("utf-8"), b"")
    return ct, nonce


def decrypt(ciphertext: bytes, nonce: bytes) -> str:
    """Decrypt `ciphertext` produced by `encrypt`. Raises on tampering."""
    key = resolve_master_key()
    return AESGCM(key).decrypt(nonce, ciphertext, b"").decode("utf-8")
