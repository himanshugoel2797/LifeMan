"""Memory writes — third concrete instance of `lifeman.routing`.

Tools call `record_memory(...)` instead of writing directly into a memory
store. The router (a tool, replaceable by the build chat) classifies
"is this memory-worthy, and if so where/how", and dispatches to one or
more memory-writing handler tools (or `discard` to drop).

Public surface:
    record_memory(...)  → records the candidate, classifies, stores.
    recall(...)         → reads back from the memories store.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from lifeman import audit
from lifeman.db import get_db
from lifeman.memory.handlers import install_builtin_handlers, registry as mem_registry
from lifeman.memory.models import (
    Memory,
    MemoryEvent,
    RecordMemoryResponse,
)
from lifeman.memory.router import builtin_route
from lifeman.routing.domain import RoutingDomain
from lifeman.routing.engine import create_engine

log = logging.getLogger("lifeman.memory")


MEMORY_DOMAIN = RoutingDomain(
    name="memory",
    router_role="memory_router",
    handler_role="memory_writer",
    handler_manifest_key="memory_writer",
    handler_methods=("store",),
    audit_table="memory_routing_audit",
    dispatch_table="memory_dispatches",
)


engine = create_engine(
    domain=MEMORY_DOMAIN,
    builtin_registry=mem_registry,
    builtin_router=builtin_route,
    fallback_handler="memory_store",   # safe default: always store
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def record_memory(
    *,
    content: str,
    type_hint: str | None = None,
    tags: list[str] | None = None,
    source: str = "",
    sensitivity: str = "personal",
    expires_at: str | None = None,
    context: dict | None = None,
    reason: str = "",
) -> RecordMemoryResponse:
    """Emit a memory candidate; the classifier/router decides what to do."""
    event_id = str(uuid.uuid4())[:12]
    emitted_at = datetime.now(timezone.utc).isoformat()
    event = MemoryEvent(
        event_id=event_id,
        emitted_at=emitted_at,
        source=source,
        sensitivity=sensitivity,
        expires_at=expires_at,
        context=context or {},
        reason=reason,
        content=content,
        type_hint=type_hint,
        tags=tags or [],
    )

    db = await get_db()
    await db.execute(
        """INSERT INTO memory_events
             (id, content, type_hint, tags_json, source, sensitivity,
              expires_at, context_json, reason, emitted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id, content, type_hint, json.dumps(event.tags), source, sensitivity,
            expires_at, json.dumps(event.context), reason, emitted_at,
        ),
    )
    await db.commit()

    decision = await engine.decide(event, state={})
    await engine.persist_audit(decision)
    if decision.expired:
        return RecordMemoryResponse(event_id=event_id, expired=True)

    ok, dropped = await engine.dispatch_all(event, decision)
    await audit.log(
        source=source or "system",
        action="record_memory",
        target=event_id,
        args_summary=f"{type_hint or '?'} → {','.join(ok) or 'none'}",
        reason=reason,
    )
    return RecordMemoryResponse(event_id=event_id, dispatched=ok, dropped=dropped)


async def recall(
    query: str | None = None,
    type: list[str] | None = None,
    tags: list[str] | None = None,
    before: str | None = None,
    after: str | None = None,
    limit: int = 25,
) -> list[Memory]:
    """Read back from the memories store. Plain LIKE-search for now."""
    db = await get_db()
    clauses, vals = [], []
    if query:
        clauses.append("content LIKE ?")
        vals.append(f"%{query}%")
    if type:
        clauses.append("type IN (" + ",".join("?" * len(type)) + ")")
        vals.extend(type)
    if before:
        clauses.append("created_at < ?"); vals.append(before)
    if after:
        clauses.append("created_at > ?"); vals.append(after)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    vals.append(min(int(limit), 200))
    rows = await db.execute_fetchall(
        f"SELECT * FROM memories {where} ORDER BY created_at DESC LIMIT ?",
        vals,
    )
    out: list[Memory] = []
    for r in rows:
        m_tags = json.loads(r["tags_json"]) if r["tags_json"] else []
        # AND semantics: every requested tag must be present on the memory.
        if tags and not all(t in m_tags for t in tags):
            continue
        out.append(Memory(
            id=r["id"], content=r["content"], type=r["type"],
            tags=m_tags, sensitivity=r["sensitivity"], source=r["source"],
            created_at=r["created_at"], classified_by=r["classified_by"],
        ))
    return out


def install_handlers() -> None:
    install_builtin_handlers()


__all__ = ["MEMORY_DOMAIN", "record_memory", "recall", "install_handlers"]
