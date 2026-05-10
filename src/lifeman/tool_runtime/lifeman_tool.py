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


def notify(message: str, urgency: str = "ambient", channel: str = "web") -> dict:
    """Post a notification to the user."""
    return _c().call("notify", message=message, urgency=urgency, channel=channel)


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
