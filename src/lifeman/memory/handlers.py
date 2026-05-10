"""Built-in memory handlers.

- `memory_store` — write the candidate to the `memories` table.
- `discard`      — explicit no-op when the router decides "not memory-worthy".
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from lifeman.db import get_db
from lifeman.routing.event import HandlerManifest
from lifeman.routing.handlers import BuiltinHandler, HandlerRegistry

log = logging.getLogger("lifeman.memory.handlers")

registry = HandlerRegistry()


async def _store(event: dict) -> dict:
    db = await get_db()
    mem_id = str(uuid.uuid4())[:12]
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO memories
             (id, content, type, tags_json, sensitivity, source, created_at, classified_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            mem_id,
            event.get("content", ""),
            event.get("type_hint") or "episodic",
            json.dumps(event.get("tags") or []),
            event.get("sensitivity", "personal"),
            event.get("source", ""),
            now,
            event.get("classified_by") or "router",
        ),
    )
    await db.commit()
    return {"ok": True, "delivery_id": mem_id}


async def _discard(event: dict) -> dict:
    log.info("memory candidate %s discarded", event.get("event_id"))
    return {"ok": True, "delivery_id": event.get("event_id")}


def install_builtin_handlers() -> None:
    registry.register(BuiltinHandler(
        manifest=HandlerManifest(
            name="memory_store", handler_type="store",
            sensitivity_tolerance="private",
        ),
        methods={"store": _store},
    ))
    registry.register(BuiltinHandler(
        manifest=HandlerManifest(
            name="discard", handler_type="noop",
            sensitivity_tolerance="private",
        ),
        methods={"store": _discard},
    ))
    log.info("installed built-in memory handlers: memory_store, discard")
