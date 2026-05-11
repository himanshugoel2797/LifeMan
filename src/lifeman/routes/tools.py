"""Tool registry, invocation, and management routes."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import jsonschema
from fastapi import APIRouter, Depends, HTTPException

from lifeman import audit
from lifeman.auth import require_auth
from lifeman.config import settings
from lifeman.db import get_db
from lifeman.models import (
    IdResponse,
    InvokeRequest,
    InvokeResponse,
    Invocation,
    ToolCreate,
    ToolDetail,
    ToolSummary,
)

log = logging.getLogger("lifeman.routes.tools")

# Cap stored invocation results at 1 MB serialised. A runaway tool returning
# multi-MB blobs would silently bloat the SQLite DB; we keep just enough to
# debug what happened and replace the payload with a truncation marker.
_RESULT_JSON_MAX_BYTES = 1_000_000


def _cap_result_json(result: dict) -> str:
    """Serialise `result` to JSON, swapping in a truncation marker if too big."""
    body = json.dumps(result, default=str)
    if len(body) <= _RESULT_JSON_MAX_BYTES:
        return body
    return json.dumps({
        "_truncated": True,
        "_original_bytes": len(body),
        "_limit_bytes": _RESULT_JSON_MAX_BYTES,
        "preview": body[:1024],
        # Preserve the error key if present so the invocation row still
        # reports the right top-level status.
        **({"error": result["error"]} if isinstance(result, dict)
           and isinstance(result.get("error"), str) else {}),
    })
from lifeman.sandbox import run_tool
from lifeman.sse import bus
from lifeman.tool_socket import ToolSocket

router = APIRouter()

# Strong references for fire-and-forget background invocations. Without this,
# Python may GC the task before _runner finishes, killing the invocation
# mid-flight. See asyncio.create_task() docs: "Save a reference to the result."
_pending_tasks: set[asyncio.Task] = set()


@router.post("", response_model=IdResponse)
async def register_tool(body: ToolCreate, _: str = Depends(require_auth)):
    """Register a new tool with manifest and code."""
    db = await get_db()
    tool_id = str(uuid.uuid4())[:12]
    now = datetime.now(timezone.utc).isoformat()

    # Check name uniqueness
    existing = await db.execute_fetchall("SELECT id FROM tools WHERE name = ?", (body.name,))
    if existing:
        raise HTTPException(400, f"Tool with name '{body.name}' already exists")

    await db.execute(
        "INSERT INTO tools (id, name, description, category, version, installed_at) VALUES (?, ?, ?, ?, 1, ?)",
        (tool_id, body.name, body.description, body.category, now),
    )
    await db.execute(
        """INSERT INTO tool_manifests (tool_id, manifest_json, schema_input_json, schema_output_json, code, version)
           VALUES (?, ?, ?, ?, ?, 1)""",
        (
            tool_id,
            body.manifest.model_dump_json(),
            json.dumps(body.schema_input),
            json.dumps(body.schema_output),
            body.code,
        ),
    )
    await db.commit()

    # Write tool code to disk
    tool_dir = settings.get_tools_dir() / tool_id
    tool_dir.mkdir(parents=True, exist_ok=True)
    (tool_dir / "run.py").write_text(body.code)
    (tool_dir / "manifest.json").write_text(body.manifest.model_dump_json(indent=2))

    await audit.log(
        source="user",
        action="register_tool",
        target=body.name,
        args_summary=f"category={body.category}",
        reason="tool registration",
    )
    await bus.publish("tool_registered", {"id": tool_id, "name": body.name})

    return IdResponse(id=tool_id)


@router.get("", response_model=list[ToolSummary])
async def list_tools(
    category: str | None = None,
    _: str = Depends(require_auth),
):
    db = await get_db()
    if category:
        rows = await db.execute_fetchall(
            "SELECT * FROM tools WHERE category = ? AND deprecated_at IS NULL ORDER BY name",
            (category,),
        )
    else:
        rows = await db.execute_fetchall(
            "SELECT * FROM tools WHERE deprecated_at IS NULL ORDER BY name"
        )

    # Batch the two follow-up lookups so we don't fire 2N queries.
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    inv_counts = {
        r["tool"]: r["cnt"]
        for r in await db.execute_fetchall(
            "SELECT tool, COUNT(*) AS cnt FROM invocations "
            "WHERE started_at > ? GROUP BY tool",
            (week_ago,),
        )
    }
    latest_manifests = {
        r["tool_id"]: r["manifest_json"]
        for r in await db.execute_fetchall(
            "SELECT m.tool_id, m.manifest_json FROM tool_manifests m "
            "INNER JOIN (SELECT tool_id, MAX(version) AS v "
            "FROM tool_manifests GROUP BY tool_id) latest "
            "ON m.tool_id = latest.tool_id AND m.version = latest.v"
        )
    }

    result = []
    for r in rows:
        r = dict(r)
        manifest_summary: dict = {}
        mj = latest_manifests.get(r["id"])
        if mj:
            try:
                m = json.loads(mj)
                manifest_summary = {
                    "reads": len(m.get("reads", [])),
                    "writes": len(m.get("writes", [])),
                    "network": len(m.get("network", [])),
                }
            except json.JSONDecodeError:
                pass
        result.append(ToolSummary(
            id=r["id"],
            name=r["name"],
            description=r["description"],
            category=r["category"],
            manifest_summary=manifest_summary,
            invocations_last_week=inv_counts.get(r["name"], 0),
        ))
    return result


@router.get("/invocations")
async def list_invocations(
    tool: str | None = None,
    source: str | None = None,
    session_id: str | None = None,
    schedule_id: str | None = None,
    since: str | None = None,
    status: str | None = None,
    limit: int = 50,
    _: str = Depends(require_auth),
):
    """Cross-cutting view of every invocation, regardless of trigger source.

    Filterable by tool, source (user/llm/schedule/tool), session, schedule,
    status (running/ok/error), or `since` (ISO timestamp). The Activity UI
    page calls this with a poll-then-SSE pattern.
    """
    db = await get_db()
    clauses, vals = [], []
    if tool:
        clauses.append("tool = ?"); vals.append(tool)
    if source:
        clauses.append("source = ?"); vals.append(source)
    if session_id:
        clauses.append("session_id = ?"); vals.append(session_id)
    if schedule_id:
        clauses.append("schedule_id = ?"); vals.append(schedule_id)
    if status:
        clauses.append("status = ?"); vals.append(status)
    if since:
        clauses.append("started_at > ?"); vals.append(since)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    vals.append(min(int(limit), 200))
    rows = await db.execute_fetchall(
        f"SELECT * FROM invocations {where} ORDER BY started_at DESC LIMIT ?",
        vals,
    )
    out = []
    for r in rows:
        r = dict(r)
        try:
            args_obj = json.loads(r["args_json"]) if r.get("args_json") else {}
        except json.JSONDecodeError:
            args_obj = {}
        try:
            result_obj = json.loads(r["result_json"]) if r.get("result_json") else None
        except json.JSONDecodeError:
            result_obj = None
        out.append({
            "id": r["id"],
            "tool": r["tool"],
            "source": r["source"],
            "status": r.get("status", "completed"),
            "args": args_obj,
            "result": result_obj,
            "error": r.get("error"),
            "reason": r.get("reason", ""),
            "started_at": r["started_at"],
            "finished_at": r.get("finished_at"),
            "session_id": r.get("session_id"),
            "schedule_id": r.get("schedule_id"),
            "parent_invocation_id": r.get("parent_invocation_id"),
        })
    return out


@router.get("/{tool_id}", response_model=ToolDetail)
async def get_tool(tool_id: str, _: str = Depends(require_auth)):
    db = await get_db()
    row = await db.execute_fetchall("SELECT * FROM tools WHERE id = ?", (tool_id,))
    if not row:
        raise HTTPException(404, "Tool not found")
    r = dict(row[0])

    manifest_rows = await db.execute_fetchall(
        "SELECT * FROM tool_manifests WHERE tool_id = ? ORDER BY version DESC LIMIT 1",
        (tool_id,),
    )
    manifest_data = {}
    schema_input = {}
    schema_output = {}
    code = ""
    if manifest_rows:
        m = dict(manifest_rows[0])
        manifest_data = json.loads(m["manifest_json"])
        schema_input = json.loads(m["schema_input_json"])
        schema_output = json.loads(m["schema_output_json"])
        code = m["code"]

    return ToolDetail(
        id=r["id"],
        name=r["name"],
        description=r["description"],
        category=r["category"],
        version=r["version"],
        installed_at=r["installed_at"],
        deprecated_at=r.get("deprecated_at"),
        manifest=manifest_data,
        schema_input=schema_input,
        schema_output=schema_output,
        code=code,
    )


@router.post("/{tool_id}/invoke", response_model=InvokeResponse)
async def invoke_tool(tool_id: str, body: InvokeRequest, _: str = Depends(require_auth)):
    """Invoke a tool by ID."""
    db = await get_db()
    row = await db.execute_fetchall("SELECT * FROM tools WHERE id = ?", (tool_id,))
    if not row:
        raise HTTPException(404, "Tool not found")
    tool_name = dict(row[0])["name"]

    invocation_id, result = await _execute_tool(
        tool_name, body.args, source="user", reason=body.reason,
    )
    error = result.get("error")

    return InvokeResponse(
        invocation_id=invocation_id,
        status="completed",
        result=result if not error else None,
        error=error,
    )


@router.post("/invoke", response_model=InvokeResponse)
async def invoke_tool_by_name(body: InvokeRequest, _: str = Depends(require_auth)):
    """Invoke a tool by name."""
    invocation_id, result = await _execute_tool(
        body.tool, body.args, source="user", reason=body.reason,
    )
    error = result.get("error")

    return InvokeResponse(
        invocation_id=invocation_id,
        status="completed",
        result=result if not error else None,
        error=error,
    )


@router.post("/invoke_async", response_model=InvokeResponse)
async def invoke_tool_async(body: InvokeRequest, _: str = Depends(require_auth)):
    """Spawn a tool invocation in the background; return its id immediately.

    The caller polls `GET /api/tools/invocations/{id}` (or the `get_invocation`
    MCP tool) for the final status. Use this when a tool takes longer than the
    caller wants to block on — long-running scrapes, ML inference, slow
    network fetches — or when the caller wants to schedule work and forget.
    """
    invocation_id = await _spawn_invocation(
        body.tool, body.args, source="user", reason=body.reason,
    )
    return InvokeResponse(invocation_id=invocation_id, status="running")


@router.get("/invocations/{invocation_id}", response_model=Invocation)
async def get_invocation(invocation_id: str, _: str = Depends(require_auth)):
    db = await get_db()
    row = await db.execute_fetchall("SELECT * FROM invocations WHERE id = ?", (invocation_id,))
    if not row:
        raise HTTPException(404, "Invocation not found")
    r = dict(row[0])
    return Invocation(
        id=r["id"],
        tool=r["tool"],
        args=json.loads(r["args_json"]),
        source=r["source"],
        result=json.loads(r["result_json"]) if r["result_json"] else None,
        error=r["error"],
        started_at=r["started_at"],
        finished_at=r["finished_at"],
        schedule_id=r.get("schedule_id"),
        session_id=r.get("session_id"),
        parent_invocation_id=r.get("parent_invocation_id"),
        status=r.get("status") or "completed",
        reason=r.get("reason") or "",
    )


@router.post("/{tool_id}/deprecate")
async def deprecate_tool(tool_id: str, _: str = Depends(require_auth)):
    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute("UPDATE tools SET deprecated_at = ? WHERE id = ?", (now, tool_id))
    await db.commit()
    await audit.log(source="user", action="deprecate_tool", target=tool_id)
    return {"ok": True}


async def _spawn_invocation(
    tool_name: str,
    args: dict,
    *,
    source: str = "user",
    reason: str = "",
    session_id: str | None = None,
    parent_invocation_id: str | None = None,
) -> str:
    """Pre-insert a `running` invocation row, then run `_execute_tool` in the
    background and update the row when it completes.

    Returns the invocation id immediately. Pollers see status='running' until
    the background task transitions it to ok/error.

    Why pre-insert before backgrounding? So a fast `get_invocation` call right
    after this returns sees the row (rather than a 404). `_execute_tool`
    detects the pre-existing row and reuses its id instead of double-creating.
    """
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM tools WHERE name = ?", (tool_name,),
    )
    if not rows:
        # Mirror the sync path: create an error row immediately so the
        # caller's polling lands on a real, terminal record.
        inv_id = str(uuid.uuid4())[:12]
        now = datetime.now(timezone.utc).isoformat()
        err = f"Tool '{tool_name}' not found"
        await db.execute(
            """INSERT INTO invocations
               (id, tool, args_json, source, started_at, finished_at,
                session_id, parent_invocation_id, reason, status, error,
                result_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'error', ?, ?)""",
            (
                inv_id, tool_name, json.dumps(args), source, now, now,
                session_id, parent_invocation_id, reason, err,
                json.dumps({"error": err}),
            ),
        )
        await db.commit()
        return inv_id

    inv_id = str(uuid.uuid4())[:12]
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO invocations
           (id, tool, args_json, source, started_at, session_id,
            parent_invocation_id, reason, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running')""",
        (
            inv_id, tool_name, json.dumps(args), source, now,
            session_id, parent_invocation_id, reason,
        ),
    )
    await db.commit()
    await bus.publish("invocation_started", {
        "id": inv_id, "tool": tool_name, "source": source,
        "session_id": session_id, "parent_invocation_id": parent_invocation_id,
        "reason": reason, "started_at": now,
    })

    async def _runner():
        try:
            await _execute_tool(
                tool_name, args, source=source, reason=reason,
                session_id=session_id,
                parent_invocation_id=parent_invocation_id,
                _existing_invocation_id=inv_id,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("async invocation %s crashed outside _execute_tool", inv_id)
            err = f"{type(e).__name__}: {e}"
            finished = datetime.now(timezone.utc).isoformat()
            try:
                _db = await get_db()
                await _db.execute(
                    "UPDATE invocations SET error = ?, finished_at = ?, "
                    "status = 'error', result_json = ? WHERE id = ? "
                    "AND finished_at IS NULL",
                    (err, finished, json.dumps({"error": err}), inv_id),
                )
                await _db.commit()
            except Exception:  # noqa: BLE001
                log.exception("failed to record crash for invocation %s", inv_id)

    task = asyncio.create_task(_runner())
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)
    return inv_id


async def _execute_tool(
    tool_name: str,
    args: dict,
    source: str = "user",
    reason: str = "",
    schedule_id: str | None = None,
    session_id: str | None = None,
    parent_invocation_id: str | None = None,
    fire_id: str | None = None,
    _existing_invocation_id: str | None = None,
) -> tuple[str, dict]:
    """Core tool execution logic used by API, scheduler, chat, and tool-side API.

    Returns ``(invocation_id, result_dict)``. The invocation_id is always a
    real row id; the result is the tool's output (or ``{"error": ...}``).

    When `_existing_invocation_id` is set, the function takes over a row that
    `_spawn_invocation` pre-inserted: skips both the row-creation step and the
    `invocation_started` SSE publish, then updates the same row when it
    finishes. Validation failures still record an error transition on that
    row so async callers see the same shape as sync ones.
    """
    db = await get_db()

    # Look up tool
    rows = await db.execute_fetchall("SELECT * FROM tools WHERE name = ?", (tool_name,))
    if not rows:
        return (
            _existing_invocation_id or "",
            {"error": f"Tool '{tool_name}' not found"},
        )
    tool = dict(rows[0])

    # Load manifest + input schema from the latest version.
    timeout = 30.0
    network_hosts: list[str] = []
    schema_input: dict = {}
    manifest_rows = await db.execute_fetchall(
        "SELECT manifest_json, schema_input_json FROM tool_manifests "
        "WHERE tool_id = ? ORDER BY version DESC LIMIT 1",
        (tool["id"],),
    )
    if manifest_rows:
        manifest = json.loads(manifest_rows[0]["manifest_json"])
        timeout = manifest.get("compute_limits", {}).get("timeout", 30.0)
        raw_net = manifest.get("network", [])
        if isinstance(raw_net, list):
            network_hosts = [str(h) for h in raw_net if h]
        try:
            schema_input = json.loads(manifest_rows[0]["schema_input_json"] or "{}")
        except json.JSONDecodeError:
            schema_input = {}

    # Validate args BEFORE creating the invocation row, so a bad-args call
    # doesn't leave an orphaned `running` row or fire a misleading start event.
    validation_error: str | None = None
    if isinstance(schema_input, dict) and schema_input:
        try:
            jsonschema.validate(instance=args, schema=schema_input)
        except jsonschema.ValidationError as e:
            path = "/".join(str(p) for p in e.absolute_path) or "(root)"
            validation_error = f"args failed schema_input at {path}: {e.message}"
        except jsonschema.SchemaError as e:
            validation_error = f"schema_input itself is invalid: {e.message}"

    # Create invocation record. If validation failed, record it as a finished
    # error in a single INSERT so observers never see a transient `running`.
    # When called from `_spawn_invocation`, a row already exists — reuse it.
    inv_id = _existing_invocation_id or str(uuid.uuid4())[:12]
    now = datetime.now(timezone.utc).isoformat()
    if validation_error is not None:
        if _existing_invocation_id is not None:
            await db.execute(
                """UPDATE invocations
                     SET error = ?, finished_at = ?, status = 'error',
                         result_json = ?
                   WHERE id = ?""",
                (validation_error, now,
                 json.dumps({"error": validation_error}), inv_id),
            )
        else:
            await db.execute(
                """INSERT INTO invocations
                   (id, tool, args_json, source, started_at, finished_at,
                    schedule_id, session_id, parent_invocation_id, reason,
                    status, error, result_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'error', ?, ?)""",
                (
                    inv_id, tool_name, json.dumps(args), source, now, now,
                    schedule_id, session_id, parent_invocation_id, reason,
                    validation_error, json.dumps({"error": validation_error}),
                ),
            )
        await db.commit()
        await audit.log(
            source=source, action="invoke_tool", target=tool_name,
            args_summary=json.dumps(args)[:200],
            result_summary=validation_error[:200],
            reason=reason,
        )
        await bus.publish("invocation_completed", {
            "id": inv_id, "tool": tool_name, "source": source, "status": "error",
            "session_id": session_id, "schedule_id": schedule_id,
            "parent_invocation_id": parent_invocation_id,
            "finished_at": now, "error": validation_error, "result": None,
        })
        return inv_id, {"error": validation_error}

    if _existing_invocation_id is None:
        await db.execute(
            """INSERT INTO invocations
               (id, tool, args_json, source, started_at, schedule_id, session_id,
                parent_invocation_id, reason, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')""",
            (
                inv_id, tool_name, json.dumps(args), source, now, schedule_id,
                session_id, parent_invocation_id, reason,
            ),
        )
        await db.commit()
        await bus.publish("invocation_started", {
            "id": inv_id, "tool": tool_name, "source": source,
            "session_id": session_id, "schedule_id": schedule_id,
            "parent_invocation_id": parent_invocation_id,
            "reason": reason, "started_at": now,
        })
    elif schedule_id is not None:
        # The pre-inserted row didn't know about schedule_id; backfill it.
        await db.execute(
            "UPDATE invocations SET schedule_id = ? WHERE id = ?",
            (schedule_id, inv_id),
        )
        await db.commit()

    # Run in sandbox, behind a per-invocation tool-side API socket.
    tool_dir = settings.get_tools_dir() / tool["id"]
    outputs_emitted = 0
    if not (tool_dir / "run.py").exists():
        result = {"error": "Tool code not found on disk"}
    else:
        async with ToolSocket(
            invocation_id=inv_id,
            tool_name=tool_name,
            source=source,
            session_id=session_id,
        ) as ts:
            result = await run_tool(
                tool_dir, args, timeout=timeout,
                socket_path=str(ts.socket_path),
                network_hosts=network_hosts,
                fire_id=fire_id,
            )
            outputs_emitted = ts.outputs_emitted

    # Auto-emit a completion output for human-facing invocations that didn't
    # surface anything themselves. Without this, a tool that just returns a
    # dict (e.g. {"greeting": "hello"}) is invisible — the result lives on the
    # invocation detail page but never reaches a notification channel.
    # Fires for `user` (manual UI/API invokes) and `schedule:*` (scheduled
    # fires the user explicitly set up); tool-to-tool calls stay quiet.
    auto_emit = source == "user" or source == "schedule"
    if auto_emit and outputs_emitted == 0 and not result.get("error"):
        try:
            from lifeman.outputs import emit_output
            from lifeman.outputs.models import StructuredContent

            body = json.dumps(result, default=str)
            if len(body) > 400:
                body = body[:397] + "..."
            await emit_output(
                content=StructuredContent(title=tool_name, body=body),
                category="completion",
                urgency="soft",
                reason=f"auto: invocation {inv_id} returned no output",
                source_tool=f"tool:{tool_name}",
            )
        except Exception:  # noqa: BLE001
            log.exception("auto-emit completion output failed for %s", inv_id)

    # Update invocation
    finished = datetime.now(timezone.utc).isoformat()
    error = result.get("error")
    status = "error" if error else "ok"
    stored_result_json = _cap_result_json(result)
    await db.execute(
        "UPDATE invocations SET result_json = ?, error = ?, finished_at = ?, status = ? WHERE id = ?",
        (stored_result_json, error, finished, status, inv_id),
    )
    await db.commit()

    # Audit
    await audit.log(
        source=source,
        action="invoke_tool",
        target=tool_name,
        args_summary=json.dumps(args)[:200],
        result_summary=json.dumps(result)[:200] if result else "",
        reason=reason,
    )

    await bus.publish("invocation_completed", {
        "id": inv_id, "tool": tool_name, "source": source, "status": status,
        "session_id": session_id, "schedule_id": schedule_id,
        "parent_invocation_id": parent_invocation_id,
        "finished_at": finished,
        "error": error,
        "result": result if not error else None,
    })

    return inv_id, result
