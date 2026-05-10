"""FastAPI application entry point."""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from lifeman.config import settings
from lifeman.db import get_db, close_db
from lifeman import scheduler
from lifeman.routes import api_router, ui_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("lifeman")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.get_tools_dir().mkdir(parents=True, exist_ok=True)

    db = await get_db()
    log.info("Database ready at %s", settings.get_db_path())

    await scheduler.start()
    log.info("Scheduler started")

    # Print connection info
    log.info("=" * 60)
    log.info("lifeman kernel running")
    log.info("  UI:    http://%s:%d/", settings.host, settings.port)
    log.info("  API:   http://%s:%d/api/", settings.host, settings.port)
    log.info("  Token: %s", settings.token)
    log.info("=" * 60)

    yield

    # Shutdown
    await scheduler.stop()
    await close_db()
    log.info("Shut down cleanly")


app = FastAPI(
    title="lifeman",
    description="Personal Companion System — Phase 1 Kernel",
    version="0.1.0",
    lifespan=lifespan,
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
    import uvicorn
    uvicorn.run(
        "lifeman.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    cli()
