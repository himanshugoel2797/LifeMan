"""Lifecycle / failure-mode tests for the secret store.

Covers things `test_secrets.py` doesn't:
- Master-key file vanishes between writes (regenerate-and-orphan behaviour).
- Master-key file is corrupt (wrong length) at startup.
- Decryption with a swapped master key (manual "rotation" — no API exists).
- Many secrets share one key without nonce reuse / cross-talk.
"""

from __future__ import annotations

import os

import pytest

from lifeman.config import settings
from lifeman.db import get_db
from lifeman.secrets import (
    get_secret_value,
    list_secrets,
    put_secret,
)
from lifeman.secrets import crypto as secrets_crypto


# ---------------------------------------------------------------------------
# Master-key file lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_master_key_orphans_existing_secrets(temp_db):
    """If the key file is deleted after a secret is written, a fresh key
    gets generated on next access and the old ciphertext can no longer be
    decrypted. We assert the *actual* behaviour: silent regenerate +
    decryption failure (NOT a startup-time error)."""
    await put_secret("k", "v1")
    key_path = settings.data_dir / "master.key"
    assert key_path.exists()
    original = key_path.read_bytes()

    # Simulate operator losing the key file.
    key_path.unlink()
    secrets_crypto.reset_cache_for_tests()

    # Reading triggers re-resolution -> a brand-new key is generated.
    new_key = secrets_crypto.resolve_master_key()
    assert key_path.exists()
    assert new_key != original
    assert secrets_crypto.consume_newly_generated_flag() is True

    # Old ciphertext is now undecryptable — AES-GCM auth tag fails.
    with pytest.raises(Exception):
        await get_secret_value("k", accessor="user", reason="t")


@pytest.mark.asyncio
async def test_corrupt_master_key_file_raises(temp_db):
    """A key file with wrong length is a hard error, not silent regeneration."""
    secrets_crypto.resolve_master_key()  # create it
    key_path = settings.data_dir / "master.key"
    key_path.write_bytes(b"too-short")
    secrets_crypto.reset_cache_for_tests()

    with pytest.raises(RuntimeError, match="wrong length"):
        secrets_crypto.resolve_master_key()


@pytest.mark.asyncio
async def test_invalid_env_master_key_raises(temp_db, monkeypatch):
    """Bad base64 in LIFEMAN_MASTER_KEY -> RuntimeError, never silent fallback."""
    monkeypatch.setenv("LIFEMAN_MASTER_KEY", "!!!not base64!!!")
    secrets_crypto.reset_cache_for_tests()
    with pytest.raises(RuntimeError, match="not valid base64"):
        secrets_crypto.resolve_master_key()


@pytest.mark.asyncio
async def test_env_master_key_wrong_length_raises(temp_db, monkeypatch):
    import base64
    monkeypatch.setenv(
        "LIFEMAN_MASTER_KEY",
        base64.urlsafe_b64encode(b"only-sixteen-byt").decode(),
    )
    secrets_crypto.reset_cache_for_tests()
    with pytest.raises(RuntimeError, match="32 bytes"):
        secrets_crypto.resolve_master_key()


# ---------------------------------------------------------------------------
# "Rotation" — there is no rotation API; we simulate by swapping the key.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_swapped_master_key_cannot_decrypt_old_ciphertext(temp_db):
    """No rotation API exists in `lifeman.secrets.crypto` (no re-encrypt
    helper, no key-id column on the secrets table). This test pins the
    current behaviour: swapping the key out renders prior secrets
    permanently unreadable. If/when a rotation API lands, replace this
    with a real round-trip-after-rotation test."""
    await put_secret("k", "v1")
    assert await get_secret_value("k", accessor="user", reason="t") == "v1"

    # Swap the on-disk key for a fresh one.
    key_path = settings.data_dir / "master.key"
    key_path.write_bytes(os.urandom(32))
    secrets_crypto.reset_cache_for_tests()

    with pytest.raises(Exception):
        await get_secret_value("k", accessor="user", reason="t")


@pytest.mark.skip(reason="No rotation API: crypto.py exposes no re-encrypt-all "
                         "or key-id mechanism. Tracked as future work.")
def test_rotation_reencrypts_existing_secrets():
    pass


# ---------------------------------------------------------------------------
# Many-secrets, one-key — nonce-reuse regression
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_many_secrets_unique_nonces_and_independent_ciphertexts(temp_db):
    """Encrypting many values under the same key must produce distinct
    nonces and distinct ciphertexts even for identical plaintexts. AES-GCM
    nonce reuse with the same key is catastrophic, so guard against any
    future change that swaps `os.urandom` for a counter or constant."""
    n = 20
    for i in range(n):
        await put_secret(f"s{i}", "same-plaintext")

    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT name, encrypted_value, nonce FROM secrets ORDER BY name"
    )
    assert len(rows) == n
    nonces = {bytes(r["nonce"]) for r in rows}
    cts = {bytes(r["encrypted_value"]) for r in rows}
    assert len(nonces) == n, "nonce reuse detected — AES-GCM key is now compromised"
    assert len(cts) == n, "identical ciphertext for identical plaintext — bad randomness"
    for nonce in nonces:
        assert len(nonce) == 12

    # All still decrypt correctly.
    for i in range(n):
        assert (
            await get_secret_value(f"s{i}", accessor="user", reason="t")
            == "same-plaintext"
        )


@pytest.mark.asyncio
async def test_overwrite_same_secret_rotates_nonce(temp_db):
    """Updating an existing secret must pick a fresh nonce — otherwise the
    update path reuses (key, nonce) which leaks the XOR of plaintexts."""
    await put_secret("k", "first-value")
    db = await get_db()
    n1 = bytes((await db.execute_fetchall(
        "SELECT nonce FROM secrets WHERE name = 'k'"))[0]["nonce"])

    await put_secret("k", "second-value")
    n2 = bytes((await db.execute_fetchall(
        "SELECT nonce FROM secrets WHERE name = 'k'"))[0]["nonce"])

    assert n1 != n2
    assert await get_secret_value("k", accessor="user", reason="t") == "second-value"


@pytest.mark.asyncio
async def test_list_after_orphan_still_lists_metadata(temp_db):
    """Losing the master key bricks values but should not break metadata
    listing — operators need to see what's stranded."""
    await put_secret("a", "v")
    await put_secret("b", "v")
    (settings.data_dir / "master.key").unlink()
    secrets_crypto.reset_cache_for_tests()

    items = await list_secrets()
    assert {i.name for i in items} == {"a", "b"}
