"""Built-in observation handlers.

- `archive`   — write the observation to the `observations` table.
- `discard`   — drop (used for noisy/unimportant levels).
- `summarize` — accumulator: kept in `observations` with level='__pending_summary'
                so a downstream tool (built later via the build chat) can drain
                them and emit a single summary observation.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from lifeman.db import get_db
from lifeman.routing.event import HandlerManifest
from lifeman.routing.handlers import BuiltinHandler, HandlerRegistry, make_discard_handler

log = logging.getLogger("lifeman.observations.handlers")

registry = HandlerRegistry()


async def _archive(event: dict) -> dict:
    db = await get_db()
    obs_id = str(uuid.uuid4())[:12]
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO observations
             (id, level, message, component, source, context_json, archived_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            obs_id,
            event.get("level", "info"),
            event.get("message", ""),
            event.get("component", ""),
            event.get("source", ""),
            json.dumps(event.get("context") or {}),
            now,
        ),
    )
    await db.commit()
    return {"ok": True, "delivery_id": obs_id}


async def _summarize(event: dict) -> dict:
    """Stash for later batch-summary. Same table as archive, special level."""
    db = await get_db()
    obs_id = str(uuid.uuid4())[:12]
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO observations
             (id, level, message, component, source, context_json, archived_at)
           VALUES (?, '__pending_summary', ?, ?, ?, ?, ?)""",
        (
            obs_id,
            event.get("message", ""),
            event.get("component", ""),
            event.get("source", ""),
            json.dumps({"original_level": event.get("level"), **(event.get("context") or {})}),
            now,
        ),
    )
    await db.commit()
    return {"ok": True, "delivery_id": obs_id}


def install_builtin_handlers() -> None:
    registry.register(BuiltinHandler(
        manifest=HandlerManifest(
            name="archive", handler_type="store",
            sensitivity_tolerance="private",
        ),
        methods={"archive": _archive},
    ))
    registry.register(make_discard_handler("observations"))
    registry.register(BuiltinHandler(
        manifest=HandlerManifest(
            name="summarize", handler_type="accumulator",
            sensitivity_tolerance="private",
        ),
        methods={"archive": _summarize},
    ))
    log.info("installed built-in observation handlers: archive, discard, summarize")
