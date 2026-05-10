"""End-to-end bwrap sandbox tests.

Skipped automatically when `bwrap` (or working user namespaces) aren't
available — these are the real-distro guarantees we want pinned, not the
argv-stringology that `test_sandbox_network.py` covers.

What we actually verify here:
  * `_run_sandboxed` end-to-end: the seccomp-fd plumbing doesn't deadlock,
    the tool's stdin/stdout round-trips, and JSON comes back.
  * The sandbox actually contains the tool: a tool that tries to read
    `/etc/shadow` does NOT succeed (root-only file masked by bwrap mounts).
  * No-network tools cannot reach the network namespace (socket creation
    or DNS lookup fails).
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from lifeman.config import settings
from lifeman.sandbox import run_tool


def _bwrap_actually_works() -> bool:
    """Bubblewrap requires CAP_SYS_ADMIN-style userns features. On many CI
    runners and inside unprivileged containers `bwrap --version` succeeds
    but `bwrap --bind / / true` fails with `setting up uid map`. Probe the
    real path so the suite skips cleanly there instead of erroring."""
    if not shutil.which("bwrap"):
        return False
    try:
        r = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "true"],
            capture_output=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0


bwrap_required = pytest.mark.skipif(
    not _bwrap_actually_works(),
    reason="bwrap not installed or user namespaces unavailable",
)


@pytest.fixture
def sandboxed(tmp_path, monkeypatch):
    """Force the real sandbox path. Without this, tests inherit the global
    `settings.sandbox_enabled = False` from conftest and silently fall back
    to `_run_direct`, which would defeat the entire point."""
    monkeypatch.setattr(settings, "sandbox_enabled", True)
    return tmp_path


def _write_tool(root: Path, body: str) -> Path:
    tool_dir = root / "tool"
    tool_dir.mkdir(parents=True, exist_ok=True)
    (tool_dir / "run.py").write_text(body)
    return tool_dir


@bwrap_required
@pytest.mark.asyncio
async def test_sandboxed_tool_round_trip(sandboxed):
    """Smoke: stdin→stdout JSON round-trip survives the bwrap + seccomp-fd
    plumbing. If the seccomp pipe deadlocked or stdin wasn't connected, this
    would hang or error; we cap with a generous timeout."""
    tool_dir = _write_tool(sandboxed, """
import json, sys
args = json.loads(sys.stdin.read())
print(json.dumps({"echoed": args.get("payload")}))
""")
    result = await asyncio.wait_for(
        run_tool(tool_dir, {"payload": "hello-bwrap"}, timeout=15.0),
        timeout=20.0,
    )
    assert result == {"echoed": "hello-bwrap"}


@bwrap_required
@pytest.mark.asyncio
async def test_sandboxed_tool_cannot_read_host_secrets(sandboxed):
    """The container should mask /etc/shadow (and similar). If a future
    refactor accidentally bind-mounts the entire host filesystem read-only,
    this test fails — that's the regression guard."""
    tool_dir = _write_tool(sandboxed, """
import json, sys
try:
    with open("/etc/shadow", "rb") as f:
        f.read(1)
    print(json.dumps({"read": True}))
except Exception as e:
    print(json.dumps({"read": False, "err": type(e).__name__}))
""")
    result = await asyncio.wait_for(
        run_tool(tool_dir, {}, timeout=15.0),
        timeout=20.0,
    )
    assert result.get("read") is False, (
        f"sandbox did not contain /etc/shadow read: {result}"
    )


@bwrap_required
@pytest.mark.asyncio
async def test_sandboxed_tool_without_network_cannot_open_socket(sandboxed):
    """No `network_hosts` → tool runs in a fresh net namespace with no
    interfaces beyond loopback (no default route). Asserting "TCP connect
    to 1.1.1.1 fails" is the smallest meaningful containment check that
    doesn't depend on DNS availability inside the sandbox."""
    tool_dir = _write_tool(sandboxed, """
import json, socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(2.0)
try:
    s.connect(("1.1.1.1", 80))
    out = {"connected": True}
except OSError as e:
    out = {"connected": False, "errno": e.errno}
finally:
    s.close()
print(json.dumps(out))
""")
    result = await asyncio.wait_for(
        run_tool(tool_dir, {}, timeout=15.0),
        timeout=20.0,
    )
    assert result.get("connected") is False, (
        f"network namespace not isolated: {result}"
    )
