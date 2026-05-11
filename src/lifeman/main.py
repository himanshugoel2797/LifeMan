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
    from lifeman.outputs.channels.devices import install_device_channels
    from lifeman.inputs import install_handlers as install_input_handlers
    from lifeman.memory import install_handlers as install_memory_handlers
    from lifeman.observations import install_handlers as install_observation_handlers
    install_builtin_channels()
    await install_device_channels()
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
    """Gate non-loopback peers.

    The UI is unauthenticated by design — the page itself templates the
    master bearer token into ``window.LIFEMAN_TOKEN`` for the browser to
    fetch with. Exposing any non-API path over the network would leak it.

    With ``LIFEMAN_ALLOW_NETWORK=true`` (the post-pairing mode), we still
    reject every non-loopback request to the UI surface and the static
    files, but we let API routes through so paired devices can reach
    them. ``require_auth`` then gates each call: master tokens are still
    rejected over the wire, only paired device tokens succeed.
    """
    client = request.client
    if client is None or client.host in _LOOPBACK_HOSTS:
        return await call_next(request)
    if settings.allow_network and request.url.path.startswith("/api/"):
        return await call_next(request)
    return JSONResponse(
        {"error": "non-loopback access denied"},
        status_code=403,
    )


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
    import os
    import uvicorn
    # ``LIFEMAN_ALLOW_NETWORK=true`` on its own should be enough to expose the
    # API to paired devices on the LAN — without this, the host stays at its
    # loopback default and the flag is inert. If the user explicitly set
    # ``LIFEMAN_HOST`` we honor it; otherwise we bind to all interfaces.
    if settings.allow_network and "LIFEMAN_HOST" not in os.environ:
        settings.host = "0.0.0.0"
    _enforce_loopback_only(settings.host)
    uvicorn.run(
        "lifeman.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _enforce_loopback_only(host: str) -> None:
    """Refuse to bind to a non-loopback address unless explicitly allowed.

    The UI surface is unauthenticated by design (browser sessions don't carry
    bearer tokens, and the page itself templates the master token in for JS).
    On a non-loopback bind, that would expose the audit log, schedule list,
    secrets metadata, chat history, and the bearer token itself to anyone on
    the network.

    Setting ``LIFEMAN_ALLOW_NETWORK=true`` flips on the device-token model:
    the kernel will bind to any host, the per-request middleware rejects
    non-loopback access to the UI surface, and the API surface accepts only
    paired device tokens (the master token is still rejected over the wire).
    """
    if host in _LOOPBACK_HOSTS:
        return
    if settings.allow_network:
        return
    raise SystemExit(
        f"Refusing to bind to non-loopback host {host!r}: the UI is currently "
        "unauthenticated and exposing it on a network would leak the audit log, "
        "secrets metadata, and the API bearer token. Set LIFEMAN_HOST=127.0.0.1 "
        "(or 'localhost' / '::1'), or set LIFEMAN_ALLOW_NETWORK=true after "
        "pairing a device — that locks the UI to loopback while letting paired "
        "devices reach the API over the network."
    )


if __name__ == "__main__":
    cli()
