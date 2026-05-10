"""Sandbox-side client for the lifeman tool-side API.

This module is bind-mounted read-only into every sandbox at /lifeman/. Tools
import it and call back into the core for capabilities they need.

    from lifeman_tool import invoke, notify, request_permission, now

    res = invoke("memory_recall", {"query": "lunch"}, reason="check what user ate")
    if res.get("permission_required"):
        request_permission(res["capability"], reason="needed for lunch reminder")

The transport is line-delimited JSON over a Unix socket. Calls block on the
core's response (tools are short-lived processes — async overhead isn't
worth it).
"""

from __future__ import annotations

import json
import os
import socket
import threading
from typing import Any

_SOCKET_ENV = "LIFEMAN_TOOL_SOCKET"
_DEFAULT_PATH = "/lifeman-tool.sock"


class LifemanToolError(RuntimeError):
    pass


class _Client:
    def __init__(self) -> None:
        path = os.environ.get(_SOCKET_ENV, _DEFAULT_PATH)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._sock.connect(path)
        except (FileNotFoundError, ConnectionRefusedError) as e:
            raise LifemanToolError(
                f"could not connect to lifeman tool socket at {path!r}: {e}"
            ) from e
        self._fp = self._sock.makefile("rwb", buffering=0)
        self._lock = threading.Lock()

    def call(self, method: str, **params: Any) -> Any:
        with self._lock:
            payload = json.dumps({"method": method, "params": params}) + "\n"
            self._fp.write(payload.encode("utf-8"))
            line = self._fp.readline()
        if not line:
            raise LifemanToolError("lifeman socket closed without responding")
        try:
            resp = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise LifemanToolError(f"invalid response from core: {e}") from e
        if "error" in resp:
            raise LifemanToolError(resp["error"])
        return resp.get("result")

    def close(self) -> None:
        try:
            self._fp.close()
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass


_client: _Client | None = None
_client_lock = threading.Lock()


def _c() -> _Client:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = _Client()
    return _client


def now() -> str:
    """Current UTC ISO timestamp from the core. Authoritative — don't guess."""
    return _c().call("now")


def invoke(tool: str, args: dict | None = None, reason: str = "") -> dict:
    """Invoke another registered tool through the core.

    Returns the called tool's result dict, OR `{"permission_required": True,
    "capability": "..."}` if your tool lacks `invoke:<tool>` and the user
    declined to grant it. Use request_permission to ask for the capability.
    """
    return _c().call("invoke", tool=tool, args=args or {}, reason=reason)


def notify(
    message: str,
    category: str = "status",
    urgency: str = "ambient",
    reason: str = "",
    expires_at: str | None = None,
    context: dict | None = None,
) -> dict:
    """Sugar over `emit_output` for plain-text notifications.

    The router decides which channel(s) carry the event — never specify a
    channel. Use `emit_output` directly for structured content or actions.
    """
    return _c().call(
        "notify",
        message=message,
        category=category,
        urgency=urgency,
        reason=reason,
        expires_at=expires_at,
        context=context or {},
    )


def emit_output(
    content: str | dict,
    category: str = "status",
    urgency: str = "ambient",
    reason: str = "",
    expires_at: str | None = None,
    sensitivity: str = "personal",
    context: dict | None = None,
    actions: list[dict] | None = None,
) -> dict:
    """Emit a structured output event for the user.

    `content` may be a plain string or a dict {title, body, fields,
    image_url, markdown}. `actions` is a list of {label, invoke_tool,
    invoke_args, confirmation_required}. The router chooses channels —
    don't pick channels here.
    """
    return _c().call(
        "emit_output",
        content=content,
        category=category,
        urgency=urgency,
        reason=reason,
        expires_at=expires_at,
        sensitivity=sensitivity,
        context=context or {},
        actions=actions or [],
    )


def cancel_output(output_id: str, reason: str = "") -> dict:
    """Recall a previously-emitted output event from every channel."""
    return _c().call("cancel_output", output_id=output_id, reason=reason)


def report_response(
    output_id: str,
    action_label: str,
    channel: str,
    raw_input: str | None = None,
) -> dict:
    """Channel-side: report a user response to an emitted event."""
    return _c().call(
        "report_response",
        output_id=output_id,
        action_label=action_label,
        channel=channel,
        raw_input=raw_input,
    )


def request_permission(
    capability: str,
    scope: dict | None = None,
    reason: str = "",
    timeout: float = 120.0,
) -> dict:
    """Request a capability. Blocks until the user resolves it or `timeout`.

    Returned dict has `status` ∈ {granted_once, granted_always, denied, pending}.
    `pending` means the request timed out — treat as denial for safety.
    """
    return _c().call(
        "request_permission",
        capability=capability,
        scope=scope or {},
        reason=reason,
        timeout=timeout,
    )


def audit(limit: int = 20) -> list[dict]:
    """Read recent audit-log entries (visible to all callers; no PII filter)."""
    return _c().call("audit", limit=limit)


def log(message: str) -> None:
    """Send a debug line to the core's logger, prefixed with this tool's id."""
    _c().call("log", message=message)


def sse_publish(event_type: str, data: dict) -> None:
    """Output-channel tools: publish an SSE event for the web UI.

    `event_type` must start with `output.` (the core enforces this so a
    channel can't impersonate other system events).
    """
    _c().call("sse_publish", event_type=event_type, data=data)


def list_output_channels() -> list[dict]:
    """Router tools: enumerate currently-installed output channels."""
    return _c().call("list_output_channels")


# ---------------------------------------------------------------------------
# Memory, observations, inputs (other routing domains)
# ---------------------------------------------------------------------------

def record_memory(
    content: str,
    type_hint: str | None = None,
    tags: list[str] | None = None,
    sensitivity: str = "personal",
    reason: str = "",
    expires_at: str | None = None,
    context: dict | None = None,
) -> dict:
    """Emit a memory candidate. The memory router decides whether/how to store."""
    return _c().call(
        "record_memory",
        content=content, type_hint=type_hint, tags=tags or [],
        sensitivity=sensitivity, reason=reason,
        expires_at=expires_at, context=context or {},
    )


def recall(
    query: str | None = None,
    type: list[str] | None = None,
    tags: list[str] | None = None,
    before: str | None = None,
    after: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Read back from the memory store."""
    return _c().call(
        "recall",
        query=query, type=type, tags=tags,
        before=before, after=after, limit=limit,
    )


def observe(
    message: str,
    level: str = "info",
    component: str = "",
    reason: str = "",
    expires_at: str | None = None,
    context: dict | None = None,
) -> dict:
    """Emit a structured observation. The observation router routes it
    (archive / summarize / discard). Replaces ad-hoc print/log calls."""
    return _c().call(
        "observe",
        message=message, level=level, component=component, reason=reason,
        expires_at=expires_at, context=context or {},
    )


def secret(name: str, reason: str = "", timeout: float = 120.0) -> str:
    """Fetch a secret value. Blocks if the user must approve the request.

    Resolution:
      1. If your tool is in the secret's `allowed_tools` list, returns
         immediately.
      2. If your tool holds a standing `secret:read:<name>` permission,
         returns immediately.
      3. Otherwise the user is prompted; this call blocks up to `timeout`
         seconds. If the user denies (or doesn't respond in time) a
         `LifemanToolError` is raised.

    Returns the decrypted value as a string. Never log the result.
    """
    res = _c().call("secret_get", name=name, reason=reason, timeout=timeout)
    if isinstance(res, dict):
        if res.get("permission_required"):
            raise LifemanToolError(
                f"access to secret {name!r} denied: {res.get('denied_reason', 'no reason')}"
            )
        if "error" in res:
            raise LifemanToolError(res["error"])
        return res["value"]
    raise LifemanToolError(f"unexpected secret_get response: {res!r}")


def secret_exists(name: str) -> bool:
    """Check whether a secret with this name exists. Doesn't read the value
    and doesn't require permission."""
    return bool(_c().call("secret_has", name=name))


def list_secret_names() -> list[dict]:
    """List secret names + descriptions. Values are never included."""
    return _c().call("list_secret_names")


def ingest_input(
    surface: str,
    raw_payload: str,
    intent_hint: str | None = None,
    sensitivity: str = "personal",
    reason: str = "",
    expires_at: str | None = None,
    context: dict | None = None,
) -> dict:
    """Inject a unit of input as if it came from a user surface. The input
    router will dispatch it (typically to the LLM, or to direct_invoke)."""
    return _c().call(
        "ingest_input",
        surface=surface, raw_payload=raw_payload, intent_hint=intent_hint,
        sensitivity=sensitivity, reason=reason,
        expires_at=expires_at, context=context or {},
    )
