"""Key/value settings the user toggles to shape companion behaviour.

Backed by the ``user_settings`` table. Values are stored as JSON so a new
flag doesn't need a migration. Keys with well-defined semantics today:

* ``do_not_disturb`` (bool) — when True, ambient ticks are skipped and
  the output router can suppress non-urgent events.
* ``sleep_schedule`` (dict) — ``{"start": "HH:MM", "end": "HH:MM"}`` in
  the user's local time; honoured by the ``asleep`` state provider.

Adding a new flag is just calling ``set_setting``; nothing here cares
about the key namespace.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from lifeman.db import get_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def get_setting(key: str, default=None):
    """Return the parsed JSON value for ``key``, or ``default`` if absent
    or the row holds invalid JSON (logged but not raised)."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT value_json FROM user_settings WHERE key = ?", (key,),
    )
    if not rows:
        return default
    try:
        return json.loads(rows[0]["value_json"])
    except json.JSONDecodeError:
        return default


async def set_setting(key: str, value) -> None:
    """Upsert a setting. ``value`` must be JSON-serialisable."""
    payload = json.dumps(value)
    db = await get_db()
    await db.execute(
        """INSERT INTO user_settings (key, value_json, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET
             value_json = excluded.value_json,
             updated_at = excluded.updated_at""",
        (key, payload, _now()),
    )
    await db.commit()


async def delete_setting(key: str) -> bool:
    db = await get_db()
    cur = await db.execute(
        "DELETE FROM user_settings WHERE key = ?", (key,),
    )
    await db.commit()
    return bool(cur.rowcount)


async def list_settings() -> dict:
    """Return all settings as a plain dict. Invalid rows are skipped."""
    db = await get_db()
    rows = await db.execute_fetchall("SELECT key, value_json FROM user_settings")
    out: dict = {}
    for r in rows:
        try:
            out[r["key"]] = json.loads(r["value_json"])
        except json.JSONDecodeError:
            continue
    return out
