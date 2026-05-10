"""Run Claude Code as a real interactive TUI under a PTY and bridge it to the
browser over a WebSocket.

The build chat used to wrap `claude --print` and translate stream-json events
into structured SSE events. That worked for plain text and tool-use blocks
but couldn't express interactive permission prompts, plan mode, or the
hardcoded `mkdir` directory guard, which all bypass the printable-events
surface. Running the real Claude Code TUI removes the impedance mismatch:
permission UX, slash commands, and plan mode all "just work" because the
user is talking to the real terminal.

Lifecycle:

- The browser opens a WebSocket at /api/chat/sessions/{sid}/terminal.
- We refresh CLAUDE.md, ensure the workspace exists, and spawn `claude` in
  it under a pty. On the first connect we mint a UUID, pass it via
  `--session-id`, and persist it on the lifeman session row as
  `external_id`. On subsequent connects we pass `--resume <external_id>`
  so the same Claude Code session resumes (full transcript and tool state).
- The terminal sends JSON control frames (`{"type": "input"|"resize", ...}`)
  and we send back binary frames containing raw PTY output.

We intentionally do not multiplex one PTY across multiple browser tabs —
each WebSocket gets its own short-lived `claude` process, and `--resume`
carries continuity across reconnects. Two simultaneous tabs would race on
the same Claude Code session id; we let the second tab's spawn fail loudly
in that case rather than silently sharing.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import pty
import shutil
import signal
import struct
import termios
import uuid
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from lifeman.build_chat import BUILD_SYSTEM_PROMPT, refresh_workspace_context, workspace_for
from lifeman.config import settings
from lifeman.db import get_db

log = logging.getLogger(__name__)


# Maximum bytes read from the PTY in one go. PTYs have a small kernel buffer
# (commonly 4 KiB on Linux); 8 KiB is comfortably larger so we drain in one
# read when output is bursty.
_READ_CHUNK = 8192


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """Set the controlling tty's window size via TIOCSWINSZ.

    rows/cols come from the browser (xterm.js fit-addon) and we simply pass
    them through. Defensive lower bound prevents a 0×0 resize from breaking
    Claude's TUI rendering when a buggy client sends garbage.
    """
    rows = max(1, int(rows))
    cols = max(1, int(cols))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def _ensure_external_id(session_id: str) -> tuple[str, bool]:
    """Return (external_id, is_first). Mints + persists a UUID on first call.

    Pre-generating a v4 UUID and forcing it via `--session-id` is acceptable
    because v4 collision risk is astronomically small and the CLI surfaces a
    clear error if the id is already in use. The previous --print wrapper
    avoided this by reading Claude's emitted id, but in the interactive
    terminal the init metadata isn't broken out for us — so we own the id.
    """
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT external_id FROM sessions WHERE id = ?", (session_id,)
    )
    if not rows:
        raise RuntimeError(f"session not found: {session_id}")
    external_id = dict(rows[0]).get("external_id")
    if external_id:
        return external_id, False
    new_id = str(uuid.uuid4())
    await db.execute(
        "UPDATE sessions SET external_id = ? WHERE id = ?",
        (new_id, session_id),
    )
    await db.commit()
    return new_id, True


def _build_args(external_id: str, is_first: bool, workspace: Path) -> list[str]:
    """Compose the argv for spawning claude in this workspace.

    `--add-dir` declares the workspace + `out/` as trusted roots so the CLI's
    directory guard accepts new sub-paths. We pass `--append-system-prompt`
    so lifeman's contract still rides on top of the user's normal Claude
    setup.
    """
    cli = settings.claude_cli
    args = [
        cli,
        "--add-dir", str(workspace),
        "--add-dir", str(workspace / "out"),
        "--append-system-prompt", BUILD_SYSTEM_PROMPT,
    ]
    if is_first:
        args += ["--session-id", external_id]
    else:
        args += ["--resume", external_id]
    return args


async def run_terminal_session(websocket: WebSocket, session_id: str) -> None:
    """Spawn a claude TUI under a PTY and bridge it to the WebSocket.

    The WebSocket should already have been accepted by the caller (the route
    handler does the auth check + accept). On normal exit (claude quits or
    the WS closes) we tear down the PTY and reap the child.
    """
    # Always send a startup banner first so the browser pane has visible
    # evidence the WS reached this code path. If the user sees the banner
    # but no claude output, the failure is in the spawn or claude itself
    # (not in routing/auth/accept). If the user sees no banner at all, the
    # server is still running stale code.
    workspace = workspace_for(session_id)
    await websocket.send_text(f"\r\n[lifeman] starting claude in {workspace}\r\n")

    # Refresh CLAUDE.md so Claude sees the current registry on attach.
    try:
        await refresh_workspace_context(session_id)
    except Exception:
        log.exception("build_terminal: failed to refresh CLAUDE.md")

    cli = settings.claude_cli
    if not shutil.which(cli) and not Path(cli).exists():
        await websocket.send_text(
            f"\r\n[lifeman] Claude CLI '{cli}' not found on PATH. Install Claude Code "
            f"or set LIFEMAN_CLAUDE_CLI to its absolute path.\r\n"
        )
        await websocket.close()
        return

    external_id, is_first = await _ensure_external_id(session_id)
    args = _build_args(external_id, is_first, workspace)
    await websocket.send_text(f"[lifeman] argv: {' '.join(args)}\r\n\r\n")

    log.info(
        "build_terminal: spawning claude (session=%s, resume=%s, cwd=%s)",
        session_id, not is_first, workspace,
    )

    master_fd, slave_fd = pty.openpty()
    _set_winsize(master_fd, rows=40, cols=120)  # provisional; client resizes shortly

    # Spawn claude under the PTY. Two things are subtle here:
    #   1. `start_new_session=True` makes the child its own process group so
    #      Ctrl-C from the TUI doesn't kill lifeman.
    #   2. Without `TIOCSCTTY`, the child has no *controlling terminal*, and
    #      interactive TUIs (which call `tcgetattr`/`tcsetattr` on stdin) get
    #      ENOTTY and exit with no output. setsid alone doesn't acquire one;
    #      we have to ask the kernel explicitly via ioctl. preexec_fn runs in
    #      the forked child after subprocess has dup2'd `slave_fd` to fd 0/1/2,
    #      so calling TIOCSCTTY on fd 0 binds the controlling terminal.
    def _child_setup():  # runs in the forked child only
        try:
            os.setsid()
        except Exception:
            pass
        try:
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        except Exception:
            pass

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=str(workspace),
        preexec_fn=_child_setup,
    )
    os.close(slave_fd)  # only the child needs it; we read from master

    loop = asyncio.get_running_loop()
    pty_done = loop.create_future()

    def _on_pty_readable() -> None:
        try:
            data = os.read(master_fd, _READ_CHUNK)
        except OSError:
            data = b""
        if not data:
            # EOF — child exited. Stop the reader; the writer task will
            # discover this when its next send fails.
            try:
                loop.remove_reader(master_fd)
            except Exception:
                pass
            if not pty_done.done():
                pty_done.set_result(None)
            return
        # Schedule the WS send. The reader is invoked from the loop thread,
        # but WebSocket.send_bytes is a coroutine — fire-and-forget is fine
        # because frames are inherently ordered through the loop.
        loop.create_task(_safe_send_bytes(websocket, data))

    loop.add_reader(master_fd, _on_pty_readable)

    async def _client_pump() -> None:
        """Forward client → PTY: text frames are JSON control messages,
        binary frames are raw input bytes (xterm-attach style)."""
        try:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                text = msg.get("text")
                data = msg.get("bytes")
                if text is not None:
                    _handle_control_frame(text, master_fd)
                elif data:
                    try:
                        os.write(master_fd, data)
                    except OSError:
                        return
        except WebSocketDisconnect:
            return

    pump_task = asyncio.create_task(_client_pump())

    try:
        # The session ends when either the child exits (pty_done) or the
        # client disconnects (pump_task). Wait for whichever fires first;
        # then clean up the other.
        done, pending = await asyncio.wait(
            {pty_done, pump_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
    finally:
        try:
            loop.remove_reader(master_fd)
        except Exception:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                # Try graceful first; if the TUI hung, escalate to SIGKILL
                # after a short grace.
                proc.send_signal(signal.SIGHUP)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()

        # Fast-exit diagnostics. If claude died on its own (PTY EOF before
        # the user closed the tab) and produced little output, the user
        # otherwise sees "disconnected" with no clue why. Surface the exit
        # code over the WS as a final text frame so the failure mode is at
        # least visible. We don't try to capture stderr separately — the
        # PTY merges it into stdout, so anything printed already went out.
        rc = proc.returncode
        if rc is not None and rc != 0:
            log.warning("build_terminal: claude exited rc=%s args=%s", rc, args)
            if websocket.client_state == WebSocketState.CONNECTED:
                with contextlib.suppress(Exception):
                    await websocket.send_text(
                        f"\r\n[lifeman] claude exited with code {rc}. "
                        f"Run `claude` directly in {workspace} to see why.\r\n"
                    )

        if websocket.client_state == WebSocketState.CONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close()


async def _safe_send_bytes(ws: WebSocket, data: bytes) -> None:
    """Send a binary frame, swallowing the disconnect race.

    We can be in a tight window where the client closed the WS between the
    PTY read and our send. Treat that as benign — the outer wait() will see
    the pump task return and tear down anyway.
    """
    if ws.client_state != WebSocketState.CONNECTED:
        return
    try:
        await ws.send_bytes(data)
    except Exception:
        # Avoid spamming the log on a normal disconnect race; the
        # readiness check above catches the common case.
        pass


def _handle_control_frame(raw: str, master_fd: int) -> None:
    """Interpret a JSON control frame from the browser.

    Supported types:
      - {"type": "input", "data": "..."}    typed text (UTF-8)
      - {"type": "resize", "cols": N, "rows": M}    window-size change
    Anything else is ignored. We do not crash on malformed JSON — a flaky
    client should not be able to take the whole pump down.
    """
    import json as _json
    try:
        msg = _json.loads(raw)
    except Exception:
        return
    if not isinstance(msg, dict):
        return
    mtype = msg.get("type")
    if mtype == "input":
        text = msg.get("data") or ""
        if isinstance(text, str) and text:
            try:
                os.write(master_fd, text.encode("utf-8"))
            except OSError:
                pass
    elif mtype == "resize":
        try:
            _set_winsize(master_fd, rows=msg.get("rows", 40), cols=msg.get("cols", 120))
        except Exception:
            pass
