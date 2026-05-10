"""Tests for the FastAPI app lifespan and top-level wiring in lifeman.main.

We exercise startup/shutdown via `app.router.lifespan_context` directly to
avoid pulling in `asgi-lifespan`. Ollama is patched out because we don't
want a real subprocess; everything else (DB, scheduler, channel
registration) runs for real against a temp data dir.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport


@pytest_asyncio.fixture
async def lifespan_env(tmp_path: Path):
    """Pin settings to a temp dir and stub Ollama so lifespan can run cleanly."""
    from lifeman import db as db_mod, ollama_supervisor
    from lifeman.config import settings
    from lifeman.secrets import crypto as secrets_crypto

    prev_path = settings.db_path
    prev_data_dir = settings.data_dir
    prev_sandbox = settings.sandbox_enabled
    prev_host = settings.host

    settings.db_path = tmp_path / "test.db"
    settings.data_dir = tmp_path
    settings.sandbox_enabled = False
    settings.host = "127.0.0.1"
    secrets_crypto.reset_cache_for_tests()

    if db_mod._db is not None:
        await db_mod._db.close()
        db_mod._db = None

    # Patch ollama_supervisor.start/stop so the lifespan never tries to
    # exec a real `ollama` binary.
    with patch.object(ollama_supervisor, "start", new=AsyncMock(return_value=None)), \
         patch.object(ollama_supervisor, "stop", new=AsyncMock(return_value=None)):
        try:
            yield settings
        finally:
            await db_mod.close_db()
            settings.db_path = prev_path
            settings.data_dir = prev_data_dir
            settings.sandbox_enabled = prev_sandbox
            settings.host = prev_host
            secrets_crypto.reset_cache_for_tests()


@pytest.mark.asyncio
async def test_lifespan_startup_and_shutdown_clean(lifespan_env):
    """Entering and exiting the lifespan should bring the app up and down."""
    from lifeman import db as db_mod, scheduler
    from lifeman.main import app

    async with app.router.lifespan_context(app):
        # DB was opened during startup.
        assert db_mod._db is not None
        # Scheduler registered itself.
        assert scheduler._task is not None or scheduler._started

    # After shutdown the DB cache should be cleared.
    assert db_mod._db is None


@pytest.mark.asyncio
async def test_lifespan_creates_data_and_tools_dirs(lifespan_env, tmp_path: Path):
    from lifeman.main import app

    tools_dir = lifespan_env.get_tools_dir()
    if tools_dir.exists():
        # Pre-existing from earlier tests; we just need the lifespan not to fail.
        pass

    async with app.router.lifespan_context(app):
        assert lifespan_env.data_dir.exists()
        assert lifespan_env.get_tools_dir().exists()


@pytest.mark.asyncio
async def test_health_status_endpoint_shape(lifespan_env):
    """With the app fully booted via lifespan, /api/system/status returns the
    documented shape (uptime + counts)."""
    from lifeman.config import settings
    from lifeman.main import app

    headers = {"Authorization": f"Bearer {settings.token}"}
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", headers=headers,
        ) as client:
            r = await client.get("/api/system/status")
            assert r.status_code == 200
            body = r.json()
            assert body["uptime"] >= 0
            assert "active_schedules" in body
            assert "pending_permissions" in body


@pytest.mark.asyncio
async def test_non_loopback_client_is_refused_by_middleware(lifespan_env):
    """The defence-in-depth middleware rejects any client.host outside the
    loopback set with a 403 JSON body."""
    from lifeman.main import app

    transport = ASGITransport(app=app, client=("203.0.113.5", 12345))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/system/status")
            assert r.status_code == 403
            assert r.json() == {"error": "non-loopback access denied"}


@pytest.mark.asyncio
async def test_loopback_enforcement_rejects_non_loopback_host():
    """`_enforce_loopback_only` raises SystemExit for any non-loopback host,
    so misconfigured `uvicorn lifeman.main:app --host 0.0.0.0` invocations
    abort instead of leaking the UI."""
    from lifeman.main import _enforce_loopback_only

    # Loopback variants pass.
    for ok in ("127.0.0.1", "localhost", "::1"):
        _enforce_loopback_only(ok)

    with pytest.raises(SystemExit):
        _enforce_loopback_only("0.0.0.0")
    with pytest.raises(SystemExit):
        _enforce_loopback_only("192.168.1.10")


@pytest.mark.asyncio
async def test_static_and_routers_mounted():
    """Smoke-test the app object: static mount, and both routers attached."""
    from lifeman.main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    # Static mount is added with path '/static'.
    assert "/static" in paths
    # API + UI routers contribute many paths; just confirm at least one /api/*.
    assert any(p and p.startswith("/api/") for p in paths)


@pytest.mark.asyncio
async def test_unknown_path_returns_404(lifespan_env):
    """FastAPI's default error handling: unknown paths produce a JSON 404
    rather than an HTML page or a server crash."""
    from lifeman.config import settings
    from lifeman.main import app

    headers = {"Authorization": f"Bearer {settings.token}"}
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", headers=headers,
        ) as client:
            r = await client.get("/api/definitely-not-a-real-route")
            assert r.status_code == 404
            # JSON body, not HTML.
            assert r.headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_double_lifespan_cycles_are_idempotent(lifespan_env):
    """Stopping and starting the lifespan twice in a row should still leave
    things in a clean state — the shutdown path must release the DB so the
    next startup can re-open it."""
    from lifeman import db as db_mod
    from lifeman.main import app

    async with app.router.lifespan_context(app):
        assert db_mod._db is not None
    assert db_mod._db is None

    async with app.router.lifespan_context(app):
        assert db_mod._db is not None
    assert db_mod._db is None
