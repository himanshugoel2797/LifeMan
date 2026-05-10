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
from lifeman.memory.router import NEEDS_REVIEW_RULE, builtin_route
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

    # If the router flagged needs_review, dispatch with an augmented copy
    # rather than mutating the caller's event in place.
    def _maybe_tag_needs_review(ev, decision):
        if NEEDS_REVIEW_RULE in decision.matched_rules and "needs_review" not in ev.tags:
            return ev.model_copy(update={"tags": list(ev.tags) + ["needs_review"]})
        return ev

    decision, ok, dropped = await engine.run_event(
        event, transform_for_dispatch=_maybe_tag_needs_review,
    )
    if decision.expired:
        return RecordMemoryResponse(event_id=event_id, expired=True)
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


async def get_memory(memory_id: str) -> Memory | None:
    """Fetch a single stored memory by id."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM memories WHERE id = ?", (memory_id,),
    )
    if not rows:
        return None
    r = rows[0]
    return Memory(
        id=r["id"], content=r["content"], type=r["type"],
        tags=json.loads(r["tags_json"]) if r["tags_json"] else [],
        sensitivity=r["sensitivity"], source=r["source"],
        created_at=r["created_at"], classified_by=r["classified_by"],
    )


async def update_memory(
    memory_id: str,
    *,
    content: str | None = None,
    tags: list[str] | None = None,
    reason: str = "",
    actor: str = "user",
) -> bool:
    """Edit content and/or tags of an existing memory. Returns True if updated."""
    if content is None and tags is None:
        return False
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM memories WHERE id = ?", (memory_id,),
    )
    if not rows:
        return False
    sets, vals = [], []
    if content is not None:
        sets.append("content = ?"); vals.append(content)
    if tags is not None:
        sets.append("tags_json = ?"); vals.append(json.dumps(tags))
    vals.append(memory_id)
    await db.execute(
        f"UPDATE memories SET {', '.join(sets)} WHERE id = ?", vals,
    )
    await db.commit()
    await audit.log(
        source=actor, action="update_memory", target=memory_id, reason=reason,
    )
    return True


async def forget(memory_id: str, *, reason: str = "", actor: str = "user") -> bool:
    """Delete a single memory by id. Returns True if a row was removed."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM memories WHERE id = ?", (memory_id,),
    )
    if not rows:
        return False
    await db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    await db.commit()
    await audit.log(
        source=actor, action="forget", target=memory_id, reason=reason,
    )
    return True


async def forget_matching(
    query: str,
    *,
    dry_run: bool = True,
    reason: str = "",
    actor: str = "user",
    limit: int = 200,
) -> list[Memory]:
    """Find or delete memories whose content matches `query` (LIKE substring).

    Defaults to dry-run: returns the candidate list without deleting.
    Pass dry_run=False to actually remove the matches.
    """
    if not query:
        return []
    matches = await recall(query=query, limit=limit)
    if dry_run or not matches:
        return matches
    db = await get_db()
    ids = [m.id for m in matches]
    placeholders = ",".join("?" * len(ids))
    await db.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", ids)
    await db.commit()
    await audit.log(
        source=actor, action="forget_matching",
        target=f"{len(ids)} memories",
        args_summary=query[:200], reason=reason,
    )
    return matches


def install_handlers() -> None:
    install_builtin_handlers()


__all__ = [
    "MEMORY_DOMAIN", "record_memory", "recall",
    "get_memory", "update_memory", "forget", "forget_matching",
    "install_handlers",
]
