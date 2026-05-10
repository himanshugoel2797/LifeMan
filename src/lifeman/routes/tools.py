"""Tool registry, invocation, and management routes."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

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
    Tool,
    ToolCreate,
    ToolDetail,
    ToolSummary,
)
from lifeman.sandbox import run_tool
from lifeman.sse import bus

router = APIRouter()


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

    result = []
    for r in rows:
        r = dict(r)
        # Count recent invocations
        week_ago = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=7)).isoformat()
        inv_count = await db.execute_fetchall(
            "SELECT COUNT(*) as cnt FROM invocations WHERE tool = ? AND started_at > ?",
            (r["name"], week_ago),
        )
        count = inv_count[0]["cnt"] if inv_count else 0

        # Get manifest summary
        manifest_rows = await db.execute_fetchall(
            "SELECT manifest_json FROM tool_manifests WHERE tool_id = ? ORDER BY version DESC LIMIT 1",
            (r["id"],),
        )
        manifest_summary = {}
        if manifest_rows:
            m = json.loads(manifest_rows[0]["manifest_json"])
            manifest_summary = {
                "reads": len(m.get("reads", [])),
                "writes": len(m.get("writes", [])),
                "network": len(m.get("network", [])),
            }

        result.append(ToolSummary(
            id=r["id"],
            name=r["name"],
            description=r["description"],
            category=r["category"],
            manifest_summary=manifest_summary,
            invocations_last_week=count,
        ))
    return result


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

    result = await _execute_tool(tool_name, body.args, source="user", reason=body.reason)
    invocation_id = result.pop("_invocation_id", "")
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
    result = await _execute_tool(body.tool, body.args, source="user", reason=body.reason)
    invocation_id = result.pop("_invocation_id", "")
    error = result.get("error")

    return InvokeResponse(
        invocation_id=invocation_id,
        status="completed",
        result=result if not error else None,
        error=error,
    )


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
    )


@router.post("/{tool_id}/deprecate")
async def deprecate_tool(tool_id: str, _: str = Depends(require_auth)):
    db = await get_db()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute("UPDATE tools SET deprecated_at = ? WHERE id = ?", (now, tool_id))
    await db.commit()
    await audit.log(source="user", action="deprecate_tool", target=tool_id)
    return {"ok": True}


async def _execute_tool(
    tool_name: str,
    args: dict,
    source: str = "user",
    reason: str = "",
    schedule_id: str | None = None,
) -> dict:
    """Core tool execution logic used by API and scheduler."""
    db = await get_db()

    # Look up tool
    rows = await db.execute_fetchall("SELECT * FROM tools WHERE name = ?", (tool_name,))
    if not rows:
        return {"error": f"Tool '{tool_name}' not found", "_invocation_id": ""}
    tool = dict(rows[0])

    # Create invocation record
    inv_id = str(uuid.uuid4())[:12]
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO invocations (id, tool, args_json, source, started_at, schedule_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (inv_id, tool_name, json.dumps(args), source, now, schedule_id),
    )
    await db.commit()

    # Run in sandbox
    tool_dir = settings.get_tools_dir() / tool["id"]
    if not (tool_dir / "run.py").exists():
        result = {"error": "Tool code not found on disk"}
    else:
        timeout = 30.0
        manifest_rows = await db.execute_fetchall(
            "SELECT manifest_json FROM tool_manifests WHERE tool_id = ? ORDER BY version DESC LIMIT 1",
            (tool["id"],),
        )
        if manifest_rows:
            manifest = json.loads(manifest_rows[0]["manifest_json"])
            timeout = manifest.get("compute_limits", {}).get("timeout", 30.0)
        result = await run_tool(tool_dir, args, timeout=timeout)

    # Update invocation
    finished = datetime.now(timezone.utc).isoformat()
    error = result.get("error")
    await db.execute(
        "UPDATE invocations SET result_json = ?, error = ?, finished_at = ? WHERE id = ?",
        (json.dumps(result), error, finished, inv_id),
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

    result["_invocation_id"] = inv_id
    return result
