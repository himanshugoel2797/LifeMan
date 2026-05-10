"""Bubblewrap sandbox runner for tool execution."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from lifeman.config import settings


async def run_tool(
    tool_dir: Path,
    args: dict,
    timeout: float = 30.0,
) -> dict:
    """Run a tool in a bubblewrap sandbox (or directly if sandbox disabled)."""
    input_json = json.dumps(args)

    if not settings.sandbox_enabled or not shutil.which(settings.bwrap_path):
        return await _run_direct(tool_dir, input_json, timeout)

    return await _run_sandboxed(tool_dir, input_json, timeout)


async def _run_direct(tool_dir: Path, input_json: str, timeout: float) -> dict:
    """Run tool directly without sandbox (development mode)."""
    proc = await asyncio.create_subprocess_exec(
        "python3", str(tool_dir / "run.py"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(tool_dir),
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=input_json.encode()),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"error": f"Tool execution timed out after {timeout}s"}

    if proc.returncode != 0:
        return {"error": f"Tool exited with code {proc.returncode}", "stderr": stderr.decode()[:2000]}

    try:
        return json.loads(stdout.decode())
    except json.JSONDecodeError:
        return {"output": stdout.decode()[:4000]}


async def _run_sandboxed(tool_dir: Path, input_json: str, timeout: float) -> dict:
    """Run tool inside bubblewrap sandbox."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cmd = _build_bwrap_cmd(tool_dir, Path(tmpdir))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=input_json.encode()),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"error": f"Tool execution timed out after {timeout}s"}

        if proc.returncode != 0:
            return {"error": f"Tool exited with code {proc.returncode}", "stderr": stderr.decode()[:2000]}

        try:
            return json.loads(stdout.decode())
        except json.JSONDecodeError:
            return {"output": stdout.decode()[:4000]}


def _build_bwrap_cmd(tool_dir: Path, scratch_dir: Path) -> list[str]:
    """Build the bubblewrap command with layered isolation."""
    bwrap = settings.bwrap_path
    cmd = [
        bwrap,
        # Namespace isolation
        "--unshare-all",
        "--die-with-parent",
        # Minimal filesystem
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        # Minimal /dev
        "--dev", "/dev",
        # tmpfs scratch
        "--tmpfs", "/tmp",
        # Tool code read-only
        "--ro-bind", str(tool_dir), "/tool",
        # Scratch space writable
        "--bind", str(scratch_dir), "/scratch",
        # Working directory
        "--chdir", "/tool",
        # Python path for tool deps
        "--setenv", "PYTHONPATH", "/tool",
        "--setenv", "HOME", "/tmp",
        "--setenv", "LIFEMAN_SCRATCH", "/scratch",
        # Run the tool
        "--", "python3", "/tool/run.py",
    ]

    # Bind Python installation if in non-standard location
    python_path = shutil.which("python3")
    if python_path:
        from pathlib import Path as P
        real = P(python_path).resolve().parent.parent
        if str(real) not in ("/usr", "/"):
            cmd[cmd.index("--ro-bind") : cmd.index("--ro-bind")] = []
            # Add the Python prefix
            cmd.insert(cmd.index("--chdir"), "--ro-bind")
            cmd.insert(cmd.index("--chdir"), str(real))
            cmd.insert(cmd.index("--chdir"), str(real))

    return cmd
