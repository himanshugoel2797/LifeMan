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
    category: str = "completion",
    urgency: str = "soft",
    reason: str = "",
    expires_at: str | None = None,
    context: dict | None = None,
) -> dict:
    """Sugar over `emit_output` for plain-text notifications.

    The router decides which channel(s) carry the event — never specify a
    channel. Use `emit_output` directly for structured content or actions.

    Pick `category` to match the *intent* of the message; that determines
    where the user actually sees it under the default routing rules:

      - "completion"          → transient toast (default). One-shot "done"
                                messages. Most `notify` calls want this.
      - "progress"            → transient toast. Long-running task updates.
      - "query"               → transient toast. System asks a question;
                                normally pair with `actions` via emit_output.
      - "alert"               → toast AND sticks in the persistent panel.
                                "Something needs attention soon."
      - "reminder"            → sticks in the persistent panel until
                                dismissed. No toast.
      - "intervention" + urgency="persistent"
                              → sticks in the persistent panel and toasts.
                                Used for nudges that should not be missed.
      - "status"              → digest only. Silent in real time. Use for
                                ambient state changes a human doesn't need
                                to react to right now.
      - "permission_request"  → toast only. Reserved for capability prompts.
      - any category, urgency="urgent"
                              → fans out to every installed channel.

    `urgency` is orthogonal: `ambient` (no interruption), `soft` (one
    transient surface), `persistent` (stays until acknowledged), `urgent`
    (escalate everywhere). The default `soft` matches a single toast.

    Unmatched (category, urgency) combinations either ask the LLM router
    to pick channels or fall back to the digest — so non-canonical values
    may be silent in real time. Stick to the categories above unless you
    know what you're doing.
    """
    if not isinstance(message, str):
        raise TypeError(
            f"notify(message=...) must be a string, got {type(message).__name__}; "
            "use emit_output() for structured content"
        )
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

    See `notify` for what each `category` value means in terms of where
    the event surfaces (toast, persistent panel, digest, etc.). Note the
    defaults differ from `notify`: `emit_output` defaults to
    `status`/`ambient`, which is **digest-only** — pass an explicit
    `category` (e.g. `completion`, `alert`, `reminder`) when you want
    real-time visibility.
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


# ---------------------------------------------------------------------------
# Per-tool state — a small JSON KV store namespaced by your tool name.
# Use it for caches, last-seen markers, run counters, scheduling cursors.
# Values must be JSON-serialisable; max 64 KB per value. For larger
# blobs, write a dedicated storage tool.
# ---------------------------------------------------------------------------

class _StateMissing:
    """Sentinel for `state_get(default=...)` to distinguish "missing" from None."""
    __slots__ = ()
    def __repr__(self) -> str: return "<state_missing>"


_STATE_MISSING = _StateMissing()


def state_get(key: str, default: Any = _STATE_MISSING) -> Any:
    """Read a value from this tool's state. Missing key → `default` if
    given, else None. (Stored `null` is indistinguishable from missing
    over the wire — pass `default=` if you need that distinction.)"""
    res = _c().call("state_get", key=key)
    if res is None and default is not _STATE_MISSING:
        return default
    return res


def state_set(key: str, value: Any, reason: str = "") -> dict:
    """Write a value to this tool's state. Replaces any existing value
    at the same key. Value must be JSON-serialisable; max 64 KB."""
    return _c().call("state_set", key=key, value=value, reason=reason)


def state_delete(key: str, reason: str = "") -> dict:
    """Delete a key from this tool's state. Returns `{ok: True, deleted: 0|1}`."""
    return _c().call("state_delete", key=key, reason=reason)


def state_list(prefix: str | None = None) -> list[dict]:
    """List `{key, updated_at}` entries for this tool. Optional `prefix`
    is a literal string match (LIKE wildcards are escaped)."""
    return _c().call("state_list", prefix=prefix)


# ---------------------------------------------------------------------------
# Local LLM access — gated by capability `llm:invoke`. First call in a
# tool's lifetime prompts the user; allow-always converts to a standing
# grant. Sync return; the helper accumulates streamed deltas before
# returning. Don't pass secrets in `messages` — the LLM is local but
# routed system context still sees them.
# ---------------------------------------------------------------------------

def llm_chat(
    messages: list[dict],
    model: str | None = None,
    temperature: float = 0.7,
    tools: list[dict] | None = None,
    reason: str = "",
) -> dict:
    """Run one chat completion against the local LLM.

    `messages` is OpenAI-shaped: `[{role, content}, ...]`. `tools` is
    OpenAI tool specs (the LLM can request tool calls; you decide
    whether to honour them by `invoke()`-ing the corresponding tool).

    Returns:
        `{content, tool_calls, finish_reason}` on success, or
        `{permission_required: True, capability: "llm:invoke"}` if the
        user has not granted access. `{error: "..."}` if the LLM server
        is unreachable or returns a non-2xx.
    """
    return _c().call(
        "llm_chat",
        messages=messages,
        model=model,
        temperature=temperature,
        tools=tools,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Network policy
# ---------------------------------------------------------------------------

def network_mode() -> str | None:
    """Return the declared network mode: "unrestricted", "local_only", or None."""
    return os.environ.get("LIFEMAN_NETWORK_MODE") or None


def fire_id() -> str | None:
    """Return the scheduler's per-fire id when this invocation was scheduler-fired.

    Use as a dedup key for external side effects: persist `(fire_id, action)`
    in `state_set` and skip on replay. None means this invocation didn't come
    from a scheduled fire (chat tool call, direct API invoke, etc.).
    """
    return os.environ.get("LIFEMAN_FIRE_ID") or None


def network_hosts() -> list[str]:
    """Return the host allowlist this tool declared in `manifest.network`.

    Empty list means the tool was sandboxed without network access. A
    non-empty list means the tool has network reachability — the entries
    are the hosts the tool author committed to talking to. The sandbox
    does not (yet) enforce the allowlist at the syscall level, so a
    well-behaved tool should self-restrict (e.g. set HTTP_PROXY, validate
    URLs against this list before calling out).
    """
    raw = os.environ.get("LIFEMAN_NETWORK_HOSTS", "")
    return [h.strip() for h in raw.split(",") if h.strip()]


def network_allowed(host: str) -> bool:
    """True if `host` is covered by the tool's network allowlist.

    Matches exact host strings and the "*" wildcard. Use this to gate
    requests in tool-side code:

        if not network_allowed(parsed.hostname or ""):
            raise RuntimeError(f"{parsed.hostname} not in declared allowlist")
    """
    allow = network_hosts()
    if not allow:
        return False
    if "*" in allow:
        return True
    return host in allow


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
