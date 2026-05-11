"""External input sources turned into ``input_events``.

Two kinds of subscription:

* ``webhook`` — exposes a receiver endpoint the external service POSTs to.
  Whatever body it sends becomes the ``raw_payload`` of an input_event.
  Auth is a per-subscription secret token included in the URL query
  (``?token=…``) or ``Authorization: Bearer …``; stored hashed.

* ``json_poll`` — the kernel polls a URL on the subscription's interval,
  hashes the response body, and emits an input_event only when the hash
  changes. ETag / Last-Modified are honoured so polite hosts aren't
  spammed.

Both kinds funnel through ``lifeman.inputs.ingest_input`` so the rest of
the system (routing, audit, dispatch) doesn't need to know an input
came from a subscription. The provenance is preserved in
``input_events.source`` as ``subscription:<id>``.

Adding a new kind: implement an ``_poll_<kind>`` coroutine that takes
the row dict and returns either a ``_PollResult`` or ``None``, then
register it in ``_POLLERS``. The webhook receiver lives on the routes
side and doesn't touch this module's poll path.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import httpx

from lifeman.db import get_db

log = logging.getLogger("lifeman.input_subscriptions")


SUPPORTED_KINDS = ("webhook", "json_poll")
DEFAULT_POLL_INTERVAL = 300         # seconds
MIN_POLL_INTERVAL = 30
TICK_INTERVAL = 60.0                # how often the loop wakes to check due polls
POLL_TIMEOUT_SECONDS = 15.0


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubscriptionRow:
    id: str
    kind: str
    name: str
    config: dict
    interval_seconds: int
    enabled: bool
    last_polled_at: str | None
    last_status: str | None
    last_error: str | None
    created_at: str


def _row_to_subscription(r: dict) -> SubscriptionRow:
    return SubscriptionRow(
        id=r["id"],
        kind=r["kind"],
        name=r["name"],
        config=json.loads(r["config_json"] or "{}"),
        interval_seconds=int(r["interval_seconds"]),
        enabled=bool(r["enabled"]),
        last_polled_at=r.get("last_polled_at"),
        last_status=r.get("last_status"),
        last_error=r.get("last_error"),
        created_at=r["created_at"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_subscription(
    *,
    kind: str,
    name: str,
    config: dict | None = None,
    interval_seconds: int = DEFAULT_POLL_INTERVAL,
) -> tuple[SubscriptionRow, str | None]:
    """Create a subscription. Returns (row, plaintext_secret).

    For webhook kinds the kernel mints a one-time-visible secret and stores
    only its hash; the caller embeds the plaintext in the URL they hand to
    the external service. For poll kinds the secret is ``None``.
    """
    if kind not in SUPPORTED_KINDS:
        raise ValueError(
            f"unsupported subscription kind {kind!r}; expected one of "
            f"{SUPPORTED_KINDS}"
        )
    if interval_seconds < MIN_POLL_INTERVAL:
        raise ValueError(
            f"interval_seconds must be >= {MIN_POLL_INTERVAL}",
        )
    if kind == "json_poll":
        cfg = config or {}
        if not isinstance(cfg.get("url"), str) or not cfg["url"]:
            raise ValueError("json_poll subscription requires config.url")

    sid = str(uuid.uuid4())[:12]
    secret_plaintext: str | None = None
    secret_hash: str | None = None
    if kind == "webhook":
        secret_plaintext = secrets.token_urlsafe(32)
        secret_hash = _hash_token(secret_plaintext)

    now = _now()
    db = await get_db()
    await db.execute(
        """INSERT INTO input_subscriptions
             (id, kind, name, config_json, interval_seconds, enabled,
              secret_hash, created_at)
           VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
        (
            sid, kind, name, json.dumps(config or {}),
            interval_seconds, secret_hash, now,
        ),
    )
    await db.commit()
    row = await _fetch_row(sid)
    assert row is not None
    return _row_to_subscription(row), secret_plaintext


async def _fetch_row(sid: str) -> dict | None:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM input_subscriptions WHERE id = ?", (sid,),
    )
    return dict(rows[0]) if rows else None


async def get_subscription(sid: str) -> SubscriptionRow | None:
    row = await _fetch_row(sid)
    return _row_to_subscription(row) if row else None


async def list_subscriptions() -> list[SubscriptionRow]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM input_subscriptions ORDER BY created_at DESC"
    )
    return [_row_to_subscription(dict(r)) for r in rows]


async def update_subscription(
    sid: str,
    *,
    name: str | None = None,
    config: dict | None = None,
    interval_seconds: int | None = None,
    enabled: bool | None = None,
) -> SubscriptionRow | None:
    """Partial update. Returns the new row or None if not found."""
    if interval_seconds is not None and interval_seconds < MIN_POLL_INTERVAL:
        raise ValueError(f"interval_seconds must be >= {MIN_POLL_INTERVAL}")
    sets, vals = [], []
    if name is not None:
        sets.append("name = ?"); vals.append(name)
    if config is not None:
        sets.append("config_json = ?"); vals.append(json.dumps(config))
    if interval_seconds is not None:
        sets.append("interval_seconds = ?"); vals.append(interval_seconds)
    if enabled is not None:
        sets.append("enabled = ?"); vals.append(1 if enabled else 0)
    if not sets:
        return await get_subscription(sid)
    vals.append(sid)
    db = await get_db()
    await db.execute(
        f"UPDATE input_subscriptions SET {', '.join(sets)} WHERE id = ?", vals,
    )
    await db.commit()
    return await get_subscription(sid)


async def delete_subscription(sid: str) -> bool:
    db = await get_db()
    cur = await db.execute(
        "DELETE FROM input_subscriptions WHERE id = ?", (sid,),
    )
    await db.commit()
    return bool(cur.rowcount)


async def verify_webhook_secret(sid: str, presented_token: str) -> bool:
    """Constant-time check that ``presented_token`` matches the stored hash
    for this subscription. False for missing/poll subscriptions, disabled
    rows, or wrong tokens."""
    if not presented_token:
        return False
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT secret_hash, enabled, kind FROM input_subscriptions WHERE id = ?",
        (sid,),
    )
    if not rows:
        return False
    r = dict(rows[0])
    if not r["enabled"] or r["kind"] != "webhook" or not r["secret_hash"]:
        return False
    return secrets.compare_digest(_hash_token(presented_token), r["secret_hash"])


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------


@dataclass
class _PollResult:
    """What a poller returns after a single fetch.

    ``unchanged`` means the response was identical to the previous poll (by
    ETag or content hash) — no input_event is emitted. ``etag`` and
    ``content_hash`` are stored on the row for next-poll comparison.
    """
    raw_payload: str | None
    unchanged: bool = False
    etag: str | None = None
    content_hash: str | None = None
    error: str | None = None


Poller = Callable[[dict], Awaitable[_PollResult]]


async def _poll_json(row: dict) -> _PollResult:
    """GET the configured URL, compare hash/etag to last poll, emit on change."""
    cfg = json.loads(row["config_json"] or "{}")
    url = cfg.get("url")
    if not url:
        return _PollResult(raw_payload=None, error="config.url missing")
    headers: dict[str, str] = dict(cfg.get("headers") or {})
    if row.get("last_etag"):
        headers["If-None-Match"] = row["last_etag"]
    try:
        async with httpx.AsyncClient(timeout=POLL_TIMEOUT_SECONDS) as client:
            r = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        return _PollResult(raw_payload=None, error=f"{type(e).__name__}: {e}")

    if r.status_code == 304:
        return _PollResult(
            raw_payload=None, unchanged=True,
            etag=row.get("last_etag"),
        )
    if not (200 <= r.status_code < 300):
        return _PollResult(raw_payload=None, error=f"HTTP {r.status_code}")

    body = r.text
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    etag = r.headers.get("etag")
    if content_hash == row.get("last_hash"):
        return _PollResult(
            raw_payload=None, unchanged=True,
            etag=etag or row.get("last_etag"), content_hash=content_hash,
        )
    return _PollResult(
        raw_payload=body, etag=etag, content_hash=content_hash,
    )


_POLLERS: dict[str, Poller] = {
    "json_poll": _poll_json,
}


async def _emit_input_event(sub: dict, raw_payload: str) -> None:
    """Hand the payload to lifeman.inputs so the router takes over."""
    from lifeman.inputs import ingest_input
    cfg = json.loads(sub["config_json"] or "{}")
    await ingest_input(
        surface=cfg.get("surface", "api"),
        raw_payload=raw_payload,
        intent_hint=cfg.get("intent_hint"),
        source=f"subscription:{sub['id']}",
        sensitivity=cfg.get("sensitivity", "personal"),
        reason=f"poll of subscription {sub['name']!r}",
        context={"subscription_id": sub["id"], "kind": sub["kind"]},
    )


async def poll_once(sub_id: str) -> _PollResult:
    """Run a single poll for ``sub_id``. Returns the result for inspection."""
    row = await _fetch_row(sub_id)
    if row is None:
        return _PollResult(raw_payload=None, error="subscription not found")
    if not row["enabled"]:
        return _PollResult(raw_payload=None, unchanged=True)
    poller = _POLLERS.get(row["kind"])
    if poller is None:
        return _PollResult(
            raw_payload=None, error=f"no poller for kind {row['kind']!r}",
        )
    result = await poller(row)
    await _record_poll(row["id"], result)
    if result.raw_payload is not None and not result.unchanged and not result.error:
        await _emit_input_event(row, result.raw_payload)
    return result


async def _record_poll(sid: str, result: _PollResult) -> None:
    status = "error" if result.error else ("unchanged" if result.unchanged else "ok")
    db = await get_db()
    await db.execute(
        """UPDATE input_subscriptions
              SET last_polled_at = ?, last_etag = ?, last_hash = ?,
                  last_status = ?, last_error = ?
            WHERE id = ?""",
        (
            _now(), result.etag, result.content_hash,
            status, result.error, sid,
        ),
    )
    await db.commit()


async def _due_subscriptions(now_utc: datetime) -> list[dict]:
    """Subscriptions whose next poll is due. Webhooks are excluded (they
    receive, they don't poll)."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM input_subscriptions WHERE enabled = 1 AND kind != 'webhook'"
    )
    due: list[dict] = []
    for r in rows:
        r = dict(r)
        if r["last_polled_at"] is None:
            due.append(r)
            continue
        try:
            last = datetime.fromisoformat(r["last_polled_at"])
        except ValueError:
            due.append(r)
            continue
        if last + timedelta(seconds=r["interval_seconds"]) <= now_utc:
            due.append(r)
    return due


_task: asyncio.Task | None = None


async def start() -> None:
    """Launch the poll loop. Idempotent."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop())
    log.info("input-subscription poller started")


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    _task = None


async def _loop() -> None:
    """Wake every TICK_INTERVAL, poll any due subscriptions in parallel."""
    while True:
        try:
            now = datetime.now(timezone.utc)
            due = await _due_subscriptions(now)
            if due:
                await asyncio.gather(
                    *(poll_once(r["id"]) for r in due),
                    return_exceptions=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("input-subscription tick crashed; continuing")
        await asyncio.sleep(TICK_INTERVAL)
