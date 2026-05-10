"""FastAPI application entry point."""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from lifeman.config import settings
from lifeman.db import get_db, close_db
from lifeman import backup, ollama_supervisor, scheduler
from lifeman.routes import api_router, ui_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("lifeman")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    # Belt-and-suspenders for the cli() loopback check: if the app is started
    # via `uvicorn lifeman.main:app --host 0.0.0.0` instead of the packaged
    # entry point, refuse early so the UI never gets exposed to the network.
    _enforce_loopback_only(settings.host)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.get_tools_dir().mkdir(parents=True, exist_ok=True)

    db = await get_db()
    log.info("Database ready at %s", settings.get_db_path())

    from lifeman.outputs.registry import install_builtin_channels
    from lifeman.inputs import install_handlers as install_input_handlers
    from lifeman.memory import install_handlers as install_memory_handlers
    from lifeman.observations import install_handlers as install_observation_handlers
    install_builtin_channels()
    install_input_handlers()
    install_memory_handlers()
    install_observation_handlers()

    await ollama_supervisor.start()

    await scheduler.start()
    log.info("Scheduler started")

    await backup.start_scheduled_backups()

    # If the master key was just generated this boot, surface a one-time
    # output event so the user actually notices (the log warning alone is
    # easy to miss). Without this key, secrets can't be decrypted; the user
    # must back it up alongside the DB.
    from lifeman.secrets.crypto import (
        consume_newly_generated_flag,
        resolve_master_key,
        _key_file,
    )
    resolve_master_key()  # ensure the key file exists; harmless if it already did
    if consume_newly_generated_flag():
        from lifeman.outputs.api import emit_output
        try:
            await emit_output(
                content=(
                    "lifeman generated a new master key at "
                    f"{_key_file()}. Back it up alongside the database — without "
                    "it, every stored secret becomes unrecoverable."
                ),
                category="alert",
                urgency="urgent",
                sensitivity="personal",
                source_tool="lifeman.kernel",
                reason="first-boot master-key generation; user must back up the key file",
            )
        except Exception:
            log.exception("failed to emit master-key backup reminder output")

    # Print connection info
    log.info("=" * 60)
    log.info("lifeman kernel running")
    log.info("  UI:    http://%s:%d/", settings.host, settings.port)
    log.info("  API:   http://%s:%d/api/", settings.host, settings.port)
    log.info("  Token: %s", settings.token)
    log.info("=" * 60)

    yield

    # Shutdown
    await backup.stop_scheduled_backups()
    await scheduler.stop()
    await ollama_supervisor.stop()
    await close_db()
    log.info("Shut down cleanly")


app = FastAPI(
    title="lifeman",
    description="Personal Companion System — Phase 1 Kernel",
    version="0.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def _refuse_non_loopback_clients(request: Request, call_next):
    """Reject any request whose client is not the loopback interface.

    Defence in depth: even if the bind check above were bypassed (a future
    entry point, a misconfigured reverse proxy that forwards directly without
    rewriting the peer), refuse to serve at the request layer too. The UI is
    unauthenticated and templates the bearer token into every page; serving
    it over a network would leak it.
    """
    client = request.client
    if client is not None and client.host not in _LOOPBACK_HOSTS:
        return JSONResponse(
            {"error": "non-loopback access denied"},
            status_code=403,
        )
    return await call_next(request)


# Mount static files
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# API routes (require auth)
app.include_router(api_router)

# UI routes (no auth required for browser access)
app.include_router(ui_router)


def cli():
    """CLI entry point for `lifeman` command."""
    import uvicorn
    _enforce_loopback_only(settings.host)
    uvicorn.run(
        "lifeman.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _enforce_loopback_only(host: str) -> None:
    """Refuse to bind to a non-loopback address.

    The UI surface is unauthenticated by design (browser sessions don't carry
    bearer tokens, and the page itself templates the token in for JS). On a
    non-loopback bind, that would expose the audit log, schedule list,
    secrets metadata, chat history, and the bearer token itself to anyone on
    the network. Until the UI grows a real cookie-based login, refuse.
    """
    if host in _LOOPBACK_HOSTS:
        return
    raise SystemExit(
        f"Refusing to bind to non-loopback host {host!r}: the UI is currently "
        "unauthenticated and exposing it on a network would leak the audit log, "
        "secrets metadata, and the API bearer token. Set LIFEMAN_HOST=127.0.0.1 "
        "(or 'localhost' / '::1'), or implement UI auth before binding wider."
    )


if __name__ == "__main__":
    cli()
