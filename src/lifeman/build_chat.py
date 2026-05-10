"""Wrap the Claude Code CLI as a build-chat backend.

Each lifeman build session corresponds to one Claude Code session. We manage
the Claude session id ourselves so resume works deterministically across
multiple turns. Output is parsed as `--output-format stream-json`, and we
yield assistant text deltas plus structured tool-use / result events.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

from lifeman.config import settings

log = logging.getLogger(__name__)


BUILD_SYSTEM_PROMPT = """\
You are the build-chat assistant for lifeman, a personal companion AI kernel.

Your job is to help the user design and produce new tools that plug into the
lifeman tool registry. A tool consists of:

  - a name (snake_case), description, and category
  - a JSON manifest declaring capabilities (reads, writes, network, compute_limits)
  - a Python `run.py` that reads JSON args from stdin and writes a JSON result to stdout
  - JSON schemas for args and result

Working directory layout:
  - The current working directory is a per-session scratch space.
  - Place finished artifacts as: ./out/<tool_name>/run.py, manifest.json, schema_input.json, schema_output.json
  - When the user asks to register the tool, tell them to click the
    "Register from workspace" button in the lifeman UI.

Be concise. Discuss the design first, then write files. Prefer minimal
dependencies; the runtime is Python 3.12 in a sandbox without network unless
the manifest declares a `network` allowlist.
"""


class BuildChatError(RuntimeError):
    pass


def workspace_for(session_id: str) -> Path:
    root = settings.get_build_workspace_dir()
    root.mkdir(parents=True, exist_ok=True)
    ws = root / session_id
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "out").mkdir(exist_ok=True)
    return ws


def cleanup_workspace(session_id: str) -> None:
    ws = settings.get_build_workspace_dir() / session_id
    if ws.exists():
        shutil.rmtree(ws, ignore_errors=True)


async def stream_build_turn(
    session_id: str,
    external_id: str | None,
    user_message: str,
) -> AsyncIterator[dict]:
    """Run one Claude Code turn and stream parsed events.

    Yields:
        {"type": "external_id", "value": "..."}        # new claude session id (first turn only)
        {"type": "delta", "text": "..."}               # assistant text delta
        {"type": "tool_use", "name": "...", "input": {...}}
        {"type": "tool_result", "name": "...", "ok": True, "summary": "..."}
        {"type": "done", "result": "..."}
        {"type": "error", "message": "..."}
    """
    cli = settings.claude_cli
    if not shutil.which(cli) and not Path(cli).exists():
        yield {
            "type": "error",
            "message": (
                f"Claude CLI '{cli}' not found on PATH. Install Claude Code or set "
                f"LIFEMAN_CLAUDE_CLI to its absolute path."
            ),
        }
        return

    workspace = workspace_for(session_id)

    # Decide session id: reuse if we have one, else create a fresh uuid for first turn
    is_first = external_id is None
    claude_session_id = external_id or str(uuid4())

    args: list[str] = [
        cli,
        "--print",
        "--output-format", "stream-json",
        "--verbose",
        "--append-system-prompt", BUILD_SYSTEM_PROMPT,
    ]
    if is_first:
        args += ["--session-id", claude_session_id]
    else:
        args += ["--resume", claude_session_id]
    args += [user_message]

    log.info("build_chat: spawning claude (session=%s, first=%s, cwd=%s)", claude_session_id, is_first, workspace)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workspace),
        )
    except FileNotFoundError as e:
        yield {"type": "error", "message": f"failed to spawn claude: {e}"}
        return

    if is_first:
        yield {"type": "external_id", "value": claude_session_id}

    # Stream stdout line by line and parse JSON events.
    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            log.debug("build_chat: non-JSON line: %s", line[:200])
            continue
        async for parsed in _parse_event(evt):
            yield parsed

    rc = await proc.wait()
    if rc != 0:
        stderr = b""
        if proc.stderr is not None:
            try:
                stderr = await proc.stderr.read()
            except Exception:
                pass
        yield {
            "type": "error",
            "message": f"claude exited {rc}: {stderr.decode(errors='replace')[:400]}",
        }


async def _parse_event(evt: dict) -> AsyncIterator[dict]:
    """Translate one Claude Code stream-json event into our event vocabulary."""
    etype = evt.get("type")

    if etype == "assistant":
        msg = evt.get("message") or {}
        for block in msg.get("content") or []:
            btype = block.get("type")
            if btype == "text":
                text = block.get("text") or ""
                if text:
                    yield {"type": "delta", "text": text}
            elif btype == "tool_use":
                yield {
                    "type": "tool_use",
                    "name": block.get("name", ""),
                    "input": block.get("input") or {},
                }

    elif etype == "user":
        # tool_result blocks come back as user-role messages
        msg = evt.get("message") or {}
        for block in msg.get("content") or []:
            if block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    summary = "".join(
                        (c.get("text") or "") for c in content if isinstance(c, dict)
                    )
                else:
                    summary = str(content) if content is not None else ""
                yield {
                    "type": "tool_result",
                    "ok": not block.get("is_error", False),
                    "summary": summary[:400],
                }

    elif etype == "result":
        # Final terminator; payload contains the full assistant result text
        yield {"type": "done", "result": evt.get("result", "")}

    elif etype == "system":
        # init / metadata; ignore
        return

    elif etype == "error":
        yield {"type": "error", "message": evt.get("message", "claude error")}


def list_workspace_tools(session_id: str) -> list[dict]:
    """Inspect the workspace's `out/` dir for finished tool artifacts."""
    ws = settings.get_build_workspace_dir() / session_id / "out"
    if not ws.exists():
        return []
    tools = []
    for tool_dir in sorted(ws.iterdir()):
        if not tool_dir.is_dir():
            continue
        run_py = tool_dir / "run.py"
        manifest_path = tool_dir / "manifest.json"
        if not run_py.exists():
            continue
        manifest = {}
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text())
            except json.JSONDecodeError:
                manifest = {"_parse_error": True}
        tools.append({
            "name": tool_dir.name,
            "path": str(tool_dir),
            "manifest": manifest,
            "code_size": run_py.stat().st_size,
        })
    return tools


def read_workspace_tool(session_id: str, name: str) -> dict | None:
    """Read a finished tool's files for registration. Returns None if missing."""
    ws = settings.get_build_workspace_dir() / session_id / "out" / name
    run_py = ws / "run.py"
    if not run_py.exists():
        return None

    code = run_py.read_text()
    manifest_path = ws / "manifest.json"
    schema_in_path = ws / "schema_input.json"
    schema_out_path = ws / "schema_output.json"

    manifest: dict = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError:
            manifest = {}

    description = manifest.pop("description", "") if isinstance(manifest, dict) else ""
    category = manifest.pop("category", "general") if isinstance(manifest, dict) else "general"

    def _safe_load(p: Path) -> dict:
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {}

    return {
        "name": name,
        "description": description,
        "category": category,
        "manifest": manifest,
        "schema_input": _safe_load(schema_in_path),
        "schema_output": _safe_load(schema_out_path),
        "code": code,
    }
