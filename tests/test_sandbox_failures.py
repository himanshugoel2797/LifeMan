"""Failure-mode tests for the tool runtime.

These exercise `lifeman.sandbox.run_tool` directly with `sandbox_enabled=False`,
so they run real subprocesses but do not require bwrap / user namespaces.

Coverage:
    - non-zero exit reported with stderr captured
    - non-JSON stdout returned as `output` (not a crash)
    - timeout: process killed and timeout error reported
    - uncaught exception in tool reported as error (with traceback in stderr)
    - tool that writes only to stderr (and exits 0) handled gracefully
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lifeman.config import settings
from lifeman.sandbox import run_tool


@pytest.fixture(autouse=True)
def _disable_sandbox():
    """Force the in-process direct runner — no bwrap required."""
    prev = settings.sandbox_enabled
    settings.sandbox_enabled = False
    try:
        yield
    finally:
        settings.sandbox_enabled = prev


def _write_tool(tmp_path: Path, code: str) -> Path:
    """Materialise a tool dir with a `run.py` and return its path."""
    tool_dir = tmp_path / "tool"
    tool_dir.mkdir()
    (tool_dir / "run.py").write_text(code)
    return tool_dir


# ---------------------------------------------------------------------------
# Non-zero exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nonzero_exit_reported_with_stderr(tmp_path):
    code = (
        "import sys\n"
        "sys.stderr.write('boom on stderr')\n"
        "sys.exit(7)\n"
    )
    tool_dir = _write_tool(tmp_path, code)

    result = await run_tool(tool_dir, args={}, timeout=5.0)

    assert "error" in result
    assert "exited with code 7" in result["error"]
    assert "boom on stderr" in result.get("stderr", "")


# ---------------------------------------------------------------------------
# Malformed JSON on stdout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_json_stdout_returned_as_output(tmp_path):
    """A tool that exits 0 but emits non-JSON shouldn't crash the runtime;
    the raw stdout is surfaced under `output` (truncated to 4000 chars)."""
    code = (
        "import sys\n"
        "sys.stdout.write('this is not JSON at all { broken')\n"
    )
    tool_dir = _write_tool(tmp_path, code)

    result = await run_tool(tool_dir, args={}, timeout=5.0)

    assert "error" not in result
    assert "output" in result
    assert "not JSON" in result["output"]


@pytest.mark.asyncio
async def test_non_json_stdout_truncated(tmp_path):
    """Outputs larger than 4000 chars are clipped, not echoed in full."""
    code = (
        "import sys\n"
        "sys.stdout.write('x' * 10000)\n"
    )
    tool_dir = _write_tool(tmp_path, code)

    result = await run_tool(tool_dir, args={}, timeout=5.0)

    assert "output" in result
    assert len(result["output"]) == 4000


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hanging_tool_killed_on_timeout(tmp_path):
    """A tool that sleeps past the deadline must be killed and the runtime
    must return a timeout error rather than blocking forever."""
    code = (
        "import time\n"
        "time.sleep(30)\n"
    )
    tool_dir = _write_tool(tmp_path, code)

    # Small timeout — the timeout argument is plumbed all the way through.
    result = await run_tool(tool_dir, args={}, timeout=0.5)

    assert "error" in result
    assert "timed out" in result["error"]
    assert "0.5" in result["error"]


@pytest.mark.asyncio
async def test_busy_loop_also_killed(tmp_path):
    """Same as above but with a CPU-bound loop (no sleep). Confirms the
    timeout path doesn't depend on the child being interruptible at a
    syscall boundary — `proc.kill()` sends SIGKILL."""
    code = (
        "while True:\n"
        "    pass\n"
    )
    tool_dir = _write_tool(tmp_path, code)

    result = await run_tool(tool_dir, args={}, timeout=0.5)

    assert "error" in result
    assert "timed out" in result["error"]


# ---------------------------------------------------------------------------
# Uncaught exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uncaught_exception_reported(tmp_path):
    """An unhandled exception → Python exits non-zero, traceback hits stderr;
    the runtime's non-zero-exit branch surfaces both."""
    code = (
        "raise RuntimeError('intentional crash for test')\n"
    )
    tool_dir = _write_tool(tmp_path, code)

    result = await run_tool(tool_dir, args={}, timeout=5.0)

    assert "error" in result
    assert "exited with code" in result["error"]
    stderr = result.get("stderr", "")
    assert "RuntimeError" in stderr
    assert "intentional crash for test" in stderr


# ---------------------------------------------------------------------------
# Stderr-only output (exit 0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stderr_only_with_zero_exit_handled(tmp_path):
    """Tool writes only to stderr and exits 0. stdout is empty, so the
    JSON parse fails and we fall through to the `output` path with an
    empty string. The point: no crash."""
    code = (
        "import sys\n"
        "sys.stderr.write('chatty diagnostic noise\\n')\n"
        "sys.exit(0)\n"
    )
    tool_dir = _write_tool(tmp_path, code)

    result = await run_tool(tool_dir, args={}, timeout=5.0)

    # Either branch is acceptable behaviour — the contract is "don't crash".
    # In practice json.loads('') raises and the runtime falls into the
    # `output` branch with an empty string.
    assert "error" not in result
    assert result.get("output", "") == ""


# ---------------------------------------------------------------------------
# Sanity: a well-behaved tool still works under the same harness.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_round_trip(tmp_path):
    """Establishes that the harness itself isn't broken — a JSON-emitting
    tool returns a parsed dict, the same as a real registered tool."""
    code = (
        "import json, sys\n"
        "args = json.loads(sys.stdin.read())\n"
        "sys.stdout.write(json.dumps({'echoed': args.get('msg')}))\n"
    )
    tool_dir = _write_tool(tmp_path, code)

    result = await run_tool(tool_dir, args={"msg": "hi"}, timeout=5.0)

    assert result == {"echoed": "hi"}
