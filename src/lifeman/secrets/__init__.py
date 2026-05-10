"""Encrypted secret storage for tools.

Threat model
============
Local personal system; the SQLite DB may be backed up to disks or sync
folders the user doesn't fully control. Tool code is version-controlled
and may be reviewed by the build chat. So:

- **Values encrypted at rest** with AES-256-GCM, per-secret nonce.
- **Master key** lives outside the DB. Resolution order:
    1. `LIFEMAN_MASTER_KEY` env var (base64 urlsafe, 32 bytes).
    2. `~/.lifeman/master.key` (0600 file). Auto-generated on first
       boot if neither source is set; the secrets are then bound to
       that file — back it up alongside the DB or you lose them.
- **Per-tool permission gate.** A tool reading a secret needs either
  membership in the secret's `allowed_tools` list, or a standing
  `secret:read:<name>` permission, or the user must approve the request
  inline (re-uses the existing `await_permission` flow).
- **Audit on every access.** Tool name, secret name, granted/denied,
  reason — but never the value.
- **LLM cannot read values.** The chat surface exposes `list_secrets`
  (names + descriptions) only. Tools are the only path to values, and
  only after the permission gate.

Public surface:
    put_secret(name, value, description="", allowed_tools=None, sensitivity="private")
    get_secret_value(name, *, accessor, reason)        — for trusted callers
    get_secret_for_tool(tool_name, name, reason)        — with permission flow
    list_secrets()                                       — names + descriptions
    delete_secret(name)
    access_log(name, limit)
"""

from __future__ import annotations

from lifeman.secrets.store import (
    SecretAccessDenied,
    SecretMetadata,
    SecretNotFound,
    access_log,
    delete_secret,
    get_secret_for_tool,
    get_secret_value,
    list_secrets,
    put_secret,
)

__all__ = [
    "put_secret",
    "get_secret_value",
    "get_secret_for_tool",
    "list_secrets",
    "delete_secret",
    "access_log",
    "SecretMetadata",
    "SecretNotFound",
    "SecretAccessDenied",
]
