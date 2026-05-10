"""Ollama process supervisor.

Lifeman uses Ollama (https://ollama.com) as its local LLM backend. Ollama
exposes an OpenAI-compatible `/v1/chat/completions` endpoint that the chat
loop in `lifeman.llm` already targets.

This module:
  * probes whether an Ollama server is already up at the configured URL,
  * launches `ollama serve` as a managed child process if not (and the user
    hasn't disabled autostart),
  * waits until the server reports healthy before yielding control,
  * exposes thin async wrappers for `/api/tags` (list models) and
    `/api/pull` (download a model on demand).

If the `ollama` binary isn't on PATH, startup logs a warning and continues.
The chat loop will surface a clear "LLM unavailable" error to the user.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import AsyncIterator

import httpx

from lifeman.config import settings

log = logging.getLogger("lifeman.ollama")


# --------------------------------------------------------------------------- #
# Module state — we manage at most one child process per lifeman instance.
# --------------------------------------------------------------------------- #
_proc: asyncio.subprocess.Process | None = None
_log_pump: asyncio.Task | None = None
_owns_process: bool = False  # True only if we spawned ollama ourselves


def _api_url(path: str) -> str:
    return settings.llm_base_url.rstrip("/") + path


async def health_check(timeout: float = 2.0) -> bool:
    """Return True if Ollama responds on the configured URL."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(_api_url("/api/tags"))
            return r.status_code == 200
    except httpx.HTTPError:
        return False


async def list_models() -> list[dict]:
    """List models pulled on the Ollama server. Empty list on error."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(_api_url("/api/tags"))
            r.raise_for_status()
            return r.json().get("models", [])
    except httpx.HTTPError as e:
        log.warning("list_models failed: %s", e)
        return []


async def stream_pull(model: str) -> AsyncIterator[dict]:
    """Pull a model from Ollama, yielding progress dicts as they arrive."""
    payload = {"name": model, "stream": True}
    async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0)) as client:
        async with client.stream("POST", _api_url("/api/pull"), json=payload) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


# --------------------------------------------------------------------------- #
# Process lifecycle
# --------------------------------------------------------------------------- #

async def start() -> None:
    """Ensure an Ollama server is running. Spawns one if necessary and allowed."""
    global _proc, _log_pump, _owns_process

    if await health_check():
        log.info("Ollama already running at %s — using existing instance", settings.llm_base_url)
        return

    if not settings.ollama_autostart:
        log.warning(
            "Ollama not reachable at %s and autostart disabled — live chat will be unavailable",
            settings.llm_base_url,
        )
        return

    if not shutil.which(settings.ollama_bin):
        log.warning(
            "ollama binary '%s' not found on PATH — install from https://ollama.com or "
            "set LIFEMAN_OLLAMA_AUTOSTART=false to silence this warning",
            settings.ollama_bin,
        )
        return

    log.info("Starting `ollama serve` (timeout=%.0fs)…", settings.ollama_startup_timeout)
    try:
        _proc = await asyncio.create_subprocess_exec(
            settings.ollama_bin, "serve",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as e:
        log.warning("Failed to spawn ollama: %s", e)
        return

    _owns_process = True
    _log_pump = asyncio.create_task(_pump_logs(_proc))

    # Wait for the server to come up. Poll quickly at first, then back off.
    deadline = asyncio.get_event_loop().time() + settings.ollama_startup_timeout
    while asyncio.get_event_loop().time() < deadline:
        if _proc.returncode is not None:
            log.error("ollama serve exited prematurely with code %s", _proc.returncode)
            _owns_process = False
            _proc = None
            return
        if await health_check(timeout=1.0):
            log.info("Ollama is up at %s", settings.llm_base_url)
            await _log_installed_models()
            return
        await asyncio.sleep(0.5)

    log.warning(
        "Ollama did not become healthy within %.0fs; live chat may be unavailable",
        settings.ollama_startup_timeout,
    )


async def stop() -> None:
    """Shut down the managed Ollama process, if we started one."""
    global _proc, _log_pump, _owns_process

    if _proc is None or not _owns_process:
        _proc = None
        _owns_process = False
        return

    log.info("Stopping managed ollama serve…")
    try:
        _proc.terminate()
    except ProcessLookupError:
        pass

    try:
        await asyncio.wait_for(_proc.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        log.warning("ollama serve did not exit on SIGTERM; sending SIGKILL")
        try:
            _proc.kill()
        except ProcessLookupError:
            pass
        await _proc.wait()

    if _log_pump:
        _log_pump.cancel()
        try:
            await _log_pump
        except asyncio.CancelledError:
            pass

    _proc = None
    _log_pump = None
    _owns_process = False


async def _pump_logs(proc: asyncio.subprocess.Process) -> None:
    """Forward Ollama subprocess output to our logger so users see what it's doing."""
    if proc.stdout is None:
        return
    try:
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line:
                log.info("[ollama] %s", line)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("ollama log pump crashed")


async def _log_installed_models() -> None:
    models = await list_models()
    names = [m.get("name", "") for m in models if m.get("name")]
    if names:
        log.info("Ollama has %d model(s) installed: %s", len(names), ", ".join(names[:10]))
        if settings.llm_model not in names:
            log.warning(
                "Configured LLM model '%s' is not installed. Pull it with: ollama pull %s",
                settings.llm_model, settings.llm_model,
            )
    else:
        log.warning(
            "Ollama has no models installed. Pull one with: ollama pull %s",
            settings.llm_model,
        )
