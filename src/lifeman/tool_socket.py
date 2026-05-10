"""Per-invocation Unix-socket gateway between sandboxed tools and the core.

Each tool invocation gets a fresh socket. The sandbox bind-mounts it, and
the tool calls in via `lifeman_tool.py` (also bind-mounted). All requests
are attributed to the calling invocation — every nested invoke becomes a
child invocation, so the activity feed shows the full call tree.

Wire format: line-delimited JSON. One request per line, one response per
line. Methods: now, invoke, notify, request_permission, audit, log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lifeman import audit
from lifeman.db import get_db
from lifeman.permissions_runtime import await_permission, find_matching_grant
from lifeman.sse import bus

log = logging.getLogger("lifeman.tool_socket")

# Paths inside the sandbox: the runtime dir is read-only; the socket is a
# single bind-mount alongside it so it doesn't need a writable parent.
SANDBOX_RUNTIME_PATH = "/lifeman-runtime"
SANDBOX_SOCKET_PATH = "/lifeman-tool.sock"


class ToolSocket:
    """Async-context manager that owns the per-invocation socket lifecycle."""

    def __init__(
        self,
        invocation_id: str,
        tool_name: str,
        source: str,
        session_id: str | None,
    ) -> None:
        self.invocation_id = invocation_id
        self.tool_name = tool_name
        self.source = source
        self.session_id = session_id
        self._tmpdir: Path | None = None
        self._server: asyncio.AbstractServer | None = None
        self.socket_path: Path | None = None
        # Tracks whether the tool produced any user-visible output during this
        # invocation. _execute_tool reads this after the run to decide whether
        # to auto-emit a completion notification for user-initiated calls —
        # otherwise a tool that only returns a dict produces nothing visible.
        self.outputs_emitted: int = 0

    async def __aenter__(self) -> "ToolSocket":
        self._tmpdir = Path(tempfile.mkdtemp(prefix="lifeman-tool-"))
        self.socket_path = self._tmpdir / "tool.sock"
        # Raise the per-line read limit above the asyncio default (64 KiB).
        # state_set caps values at 64 KB serialised; a value sent at exactly
        # the limit plus envelope overhead must still arrive intact so the
        # cap check can return a clean error rather than the connection
        # dying at the transport layer.
        self._server = await asyncio.start_unix_server(
            self._handle, str(self.socket_path), limit=1024 * 1024,
        )
        # Make sure the sandbox can actually open the socket regardless of umask.
        os.chmod(self.socket_path, 0o666)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)

    # -----------------------------------------------------------------------
    # Connection handler
    # -----------------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while not reader.at_eof():
                line = await reader.readline()
                if not line:
                    break
                try:
                    req = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError as e:
                    self._send(writer, {"error": f"invalid JSON: {e}"})
                    continue
                method = req.get("method", "")
                params = req.get("params") or {}
                if not isinstance(params, dict):
                    self._send(writer, {"error": "params must be an object"})
                    continue
                try:
                    resp = await self._dispatch(method, params)
                except Exception as e:  # noqa: BLE001
                    log.exception("tool socket dispatch crashed")
                    resp = {"error": f"{type(e).__name__}: {e}"}
                self._send(writer, resp)
        except (ConnectionResetError, BrokenPipeError):
            return
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _send(writer: asyncio.StreamWriter, payload: dict) -> None:
        writer.write((json.dumps(payload) + "\n").encode("utf-8"))

    # -----------------------------------------------------------------------
    # Method dispatch — every method returns {"result": ...} or {"error": ...}
    # -----------------------------------------------------------------------

    async def _dispatch(self, method: str, params: dict) -> dict:
        if method == "now":
            return {"result": datetime.now(timezone.utc).isoformat()}

        if method == "log":
            log.info("[tool %s/%s] %s", self.tool_name, self.invocation_id,
                     str(params.get("message", ""))[:500])
            return {"result": True}

        if method == "invoke":
            from lifeman.routes.tools import _execute_tool

            target = params.get("tool")
            if not target:
                return {"error": "missing 'tool'"}
            granted = await self._check_invoke_capability(
                target,
                params.get("args") or {},
                params.get("reason", ""),
            )
            if not granted:
                return {"result": {
                    "permission_required": True,
                    "capability": f"invoke:{target}",
                }}
            result = await _execute_tool(
                target,
                params.get("args") or {},
                source="tool",
                reason=params.get("reason", "tool_initiated"),
                parent_invocation_id=self.invocation_id,
                session_id=self.session_id,
            )
            result.pop("_invocation_id", None)
            return {"result": result}

        if method == "notify":
            from lifeman.outputs import emit_output

            res = await emit_output(
                content=str(params.get("message", ""))[:1000],
                category=params.get("category", "status"),
                urgency=params.get("urgency", "ambient"),
                expires_at=params.get("expires_at"),
                context=params.get("context") or {},
                reason=params.get("reason", ""),
                source_tool=f"tool:{self.tool_name}",
            )
            self.outputs_emitted += 1
            return {"result": res.model_dump()}

        if method == "emit_output":
            from lifeman.outputs import emit_output

            res = await emit_output(
                content=params.get("content", ""),
                category=params.get("category", "status"),
                urgency=params.get("urgency", "ambient"),
                expires_at=params.get("expires_at"),
                sensitivity=params.get("sensitivity", "personal"),
                context=params.get("context") or {},
                actions=params.get("actions") or [],
                reason=params.get("reason", ""),
                source_tool=f"tool:{self.tool_name}",
            )
            self.outputs_emitted += 1
            return {"result": res.model_dump()}

        if method == "cancel_output":
            from lifeman.outputs import cancel_output

            output_id = params.get("output_id")
            if not output_id:
                return {"error": "missing 'output_id'"}
            res = await cancel_output(
                output_id,
                reason=params.get("reason", ""),
                source_tool=f"tool:{self.tool_name}",
            )
            return {"result": res.model_dump()}

        if method == "report_response":
            from lifeman.outputs import report_response

            output_id = params.get("output_id")
            label = params.get("action_label")
            if not output_id or not label:
                return {"error": "missing 'output_id' or 'action_label'"}
            res = await report_response(
                output_id=output_id,
                action_label=label,
                raw_input=params.get("raw_input"),
                channel=params.get("channel", ""),
                source_tool=f"tool:{self.tool_name}",
            )
            return {"result": res}

        if method == "request_permission":
            db = await get_db()
            pid = str(uuid.uuid4())[:12]
            now = datetime.now(timezone.utc).isoformat()
            cap = params.get("capability", "")
            reason = params.get("reason", "")
            scope = params.get("scope") or {}
            await db.execute(
                """INSERT INTO permission_requests
                   (id, requester, capability, scope_json, reason, status, requested_at, invocation_id)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    pid,
                    f"tool:{self.tool_name}",
                    cap,
                    json.dumps(scope),
                    reason,
                    now,
                    self.invocation_id,
                ),
            )
            await db.commit()
            await bus.publish("permission_requested", {"id": pid, "capability": cap, "from": self.tool_name})
            await audit.log(
                source=f"tool:{self.tool_name}",
                action="request_permission",
                target=cap,
                reason=reason,
            )
            timeout = float(params.get("timeout", 120.0))
            status = await await_permission(pid, timeout=timeout)
            return {"result": {"id": pid, "status": status}}

        if method == "sse_publish":
            # Output channel tools publish UI events through here. Restricted
            # to a single namespace so a misbehaving tool can't impersonate
            # other system events.
            event_type = str(params.get("event_type", ""))
            if not event_type.startswith("output."):
                return {"error": "sse_publish event_type must start with 'output.'"}
            data = params.get("data") or {}
            if not isinstance(data, dict):
                return {"error": "sse_publish data must be an object"}
            await bus.publish(event_type, {
                "source_tool": self.tool_name,
                **data,
            })
            return {"result": True}

        if method == "list_output_channels":
            # Used by the router tool to discover what's installed.
            from lifeman.outputs.tool_backed import all_available_channels
            channels = await all_available_channels()
            return {"result": [c.model_dump() for c in channels]}

        if method == "record_memory":
            from lifeman.memory import record_memory
            res = await record_memory(
                content=str(params.get("content", ""))[:8000],
                type_hint=params.get("type_hint"),
                tags=params.get("tags") or [],
                source=f"tool:{self.tool_name}",
                sensitivity=params.get("sensitivity", "personal"),
                expires_at=params.get("expires_at"),
                context=params.get("context") or {},
                reason=params.get("reason", ""),
            )
            return {"result": res.model_dump()}

        if method == "recall":
            from lifeman.memory import recall
            mems = await recall(
                query=params.get("query"),
                type=params.get("type"),
                tags=params.get("tags"),
                before=params.get("before"),
                after=params.get("after"),
                limit=int(params.get("limit", 10)),
            )
            return {"result": [m.model_dump() for m in mems]}

        if method == "observe":
            from lifeman.observations import observe
            res = await observe(
                message=str(params.get("message", ""))[:2000],
                level=params.get("level", "info"),
                component=params.get("component", ""),
                source=f"tool:{self.tool_name}",
                expires_at=params.get("expires_at"),
                context=params.get("context") or {},
                reason=params.get("reason", ""),
            )
            return {"result": res.model_dump()}

        if method == "ingest_input":
            from lifeman.inputs import ingest_input
            res = await ingest_input(
                surface=params.get("surface", "api"),
                raw_payload=str(params.get("raw_payload", "")),
                intent_hint=params.get("intent_hint"),
                source=f"tool:{self.tool_name}",
                sensitivity=params.get("sensitivity", "personal"),
                expires_at=params.get("expires_at"),
                context=params.get("context") or {},
                reason=params.get("reason", ""),
            )
            return {"result": res.model_dump()}

        if method == "secret_get":
            from lifeman.secrets import (
                SecretAccessDenied,
                SecretNotFound,
                get_secret_for_tool,
            )
            name = params.get("name")
            if not name:
                return {"error": "missing 'name'"}
            reason = params.get("reason", "")
            timeout = float(params.get("timeout", 120.0))
            try:
                value = await get_secret_for_tool(
                    self.tool_name, name, reason,
                    invocation_id=self.invocation_id,
                    permission_timeout=timeout,
                )
            except SecretNotFound:
                return {"result": {"error": f"secret {name!r} not found"}}
            except SecretAccessDenied as e:
                return {"result": {
                    "permission_required": True,
                    "capability": f"secret:read:{name}",
                    "denied_reason": str(e),
                }}
            return {"result": {"value": value}}

        if method == "secret_has":
            # Existence check — never reveals the value, no permission needed.
            from lifeman.secrets import list_secrets
            name = params.get("name")
            if not name:
                return {"error": "missing 'name'"}
            for s in await list_secrets():
                if s.name == name:
                    return {"result": True}
            return {"result": False}

        if method == "list_secret_names":
            # Names + descriptions (not values). Useful for tools that
            # adapt behaviour based on which secrets are present.
            from lifeman.secrets import list_secrets
            return {"result": [
                {"name": s.name, "description": s.description,
                 "allowed_tools": s.allowed_tools, "sensitivity": s.sensitivity}
                for s in await list_secrets()
            ]}

        if method == "state_get":
            key = params.get("key")
            if not isinstance(key, str) or not key:
                return {"error": "missing 'key'"}
            db = await get_db()
            rows = await db.execute_fetchall(
                "SELECT value_json FROM tool_state WHERE tool_name = ? AND key = ?",
                (self.tool_name, key),
            )
            if not rows:
                return {"result": None}
            try:
                return {"result": json.loads(rows[0]["value_json"])}
            except json.JSONDecodeError:
                return {"error": "stored value is not valid JSON"}

        if method == "state_set":
            key = params.get("key")
            if not isinstance(key, str) or not key:
                return {"error": "missing 'key'"}
            if "value" not in params:
                return {"error": "missing 'value'"}
            try:
                value_json = json.dumps(params["value"])
            except (TypeError, ValueError) as e:
                return {"error": f"value not JSON-serialisable: {e}"}
            # 64 KB cap per value — keeps the DB lean and forces tools that
            # want to stash big blobs to use a real storage tool instead.
            if len(value_json) > 65536:
                return {"error": "value too large (max 64 KB serialised)"}
            db = await get_db()
            now = datetime.now(timezone.utc).isoformat()
            await db.execute(
                """INSERT INTO tool_state (tool_name, key, value_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(tool_name, key) DO UPDATE SET
                     value_json = excluded.value_json,
                     updated_at = excluded.updated_at""",
                (self.tool_name, key, value_json, now),
            )
            await db.commit()
            return {"result": {"ok": True, "updated_at": now}}

        if method == "state_delete":
            key = params.get("key")
            if not isinstance(key, str) or not key:
                return {"error": "missing 'key'"}
            db = await get_db()
            cur = await db.execute(
                "DELETE FROM tool_state WHERE tool_name = ? AND key = ?",
                (self.tool_name, key),
            )
            await db.commit()
            return {"result": {"ok": True, "deleted": cur.rowcount or 0}}

        if method == "state_list":
            db = await get_db()
            prefix = params.get("prefix")
            if prefix is not None and not isinstance(prefix, str):
                return {"error": "'prefix' must be a string"}
            if prefix:
                # Escape the LIKE wildcards the caller might have included so
                # `prefix` is treated as a literal string match, not a pattern.
                escaped = (
                    prefix.replace("\\", "\\\\")
                          .replace("%", "\\%")
                          .replace("_", "\\_")
                )
                rows = await db.execute_fetchall(
                    "SELECT key, updated_at FROM tool_state "
                    "WHERE tool_name = ? AND key LIKE ? ESCAPE '\\' ORDER BY key",
                    (self.tool_name, escaped + "%"),
                )
            else:
                rows = await db.execute_fetchall(
                    "SELECT key, updated_at FROM tool_state "
                    "WHERE tool_name = ? ORDER BY key",
                    (self.tool_name,),
                )
            return {"result": [dict(r) for r in rows]}

        if method == "llm_chat":
            from lifeman.llm import LLMError, merge_tool_call_deltas, stream_chat

            messages = params.get("messages")
            if not isinstance(messages, list) or not messages:
                return {"error": "'messages' must be a non-empty list"}
            tools_spec = params.get("tools")
            if tools_spec is not None and not isinstance(tools_spec, list):
                return {"error": "'tools' must be a list of tool specs"}
            try:
                temperature = float(params.get("temperature", 0.7))
            except (TypeError, ValueError):
                return {"error": "'temperature' must be a number"}
            model = params.get("model")
            if model is not None and not isinstance(model, str):
                return {"error": "'model' must be a string"}
            reason = params.get("reason", "")

            granted = await self._check_capability(
                "llm:invoke",
                {"model": model or "", "message_count": len(messages)},
                reason,
            )
            if not granted:
                return {"result": {
                    "permission_required": True,
                    "capability": "llm:invoke",
                }}

            content_parts: list[str] = []
            tool_calls: list[dict] = []
            finish_reason: str | None = None
            try:
                async for delta in stream_chat(
                    messages,
                    tools=tools_spec,
                    model=model,
                    temperature=temperature,
                ):
                    if "content" in delta and delta["content"]:
                        content_parts.append(delta["content"])
                    if "tool_calls" in delta and delta["tool_calls"]:
                        merge_tool_call_deltas(tool_calls, delta["tool_calls"])
                    if "finish_reason" in delta:
                        finish_reason = delta["finish_reason"]
            except LLMError as e:
                return {"result": {"error": str(e)}}

            await audit.log(
                source=f"tool:{self.tool_name}",
                action="llm_chat",
                target=model or "default",
                reason=reason,
            )
            return {"result": {
                "content": "".join(content_parts),
                "tool_calls": tool_calls,
                "finish_reason": finish_reason,
            }}

        if method == "audit":
            db = await get_db()
            limit = int(params.get("limit", 20))
            rows = await db.execute_fetchall(
                "SELECT timestamp, source, action, target, reason FROM audit_log "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return {"result": [dict(r) for r in rows]}

        return {"error": f"unknown method {method!r}"}

    async def _check_capability(
        self,
        capability: str,
        request_scope: dict,
        reason: str,
        *,
        timeout: float = 120.0,
    ) -> bool:
        """Generic standing-grant-or-prompt check for an arbitrary capability.

        Used by gates that don't carry the structured `target/args` shape
        of `invoke:<tool>` (e.g. `llm:invoke`). The flow mirrors
        `_check_invoke_capability`: look up a matching grant, otherwise
        open a permission request and block until the user resolves it.
        """
        grantee = f"tool:{self.tool_name}"
        if await find_matching_grant(grantee, capability, request_scope):
            return True
        db = await get_db()
        pid = str(uuid.uuid4())[:12]
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """INSERT INTO permission_requests
               (id, requester, capability, scope_json, reason, status, requested_at, invocation_id)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (pid, grantee, capability, json.dumps(request_scope), reason, now, self.invocation_id),
        )
        await db.commit()
        await bus.publish("permission_requested", {"id": pid, "capability": capability, "from": self.tool_name})
        await audit.log(
            source=grantee,
            action="request_permission",
            target=capability,
            reason=reason,
        )
        status = await await_permission(pid, timeout=timeout)
        return status in ("granted_once", "granted_always")

    async def _check_invoke_capability(
        self,
        target: str,
        args: dict,
        reason: str,
    ) -> bool:
        """Tools must hold `invoke:<target>` to invoke another tool.

        Honours scope (args_match, expires_at) via find_matching_grant so a
        narrowly-scoped grant doesn't accidentally cover a broader request.
        The request scope carries the actual `args` so grants like
        `args_match: {dry_run: true}` are checked against the real call.
        """
        db = await get_db()
        cap = f"invoke:{target}"
        grantee = f"tool:{self.tool_name}"
        request_scope = {"target": target, "args": args if isinstance(args, dict) else {}}
        if await find_matching_grant(grantee, cap, request_scope):
            return True
        # No standing grant — request one and wait for the user. Persist the
        # actual args alongside the target so the resolve UI can see what's
        # being granted and offer to scope the grant to those args.
        pid = str(uuid.uuid4())[:12]
        now = datetime.now(timezone.utc).isoformat()
        scope_json = json.dumps(request_scope)
        await db.execute(
            """INSERT INTO permission_requests
               (id, requester, capability, scope_json, reason, status, requested_at, invocation_id)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (pid, grantee, cap, scope_json, reason, now, self.invocation_id),
        )
        await db.commit()
        await bus.publish("permission_requested", {"id": pid, "capability": cap, "from": self.tool_name})
        status = await await_permission(pid, timeout=120.0)
        return status in ("granted_once", "granted_always")
