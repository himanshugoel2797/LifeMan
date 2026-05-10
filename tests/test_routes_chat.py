"""Tests for `lifeman.routes.chat`.

Covers the parts of the chat router that are exercisable without an
external process:

  * Session CRUD (create / list / get / patch / archive).
  * `POST /api/chat/sessions/{id}/messages` for archived and `build_chat`
    sessions (pure rejection paths — no model needed).
  * The live-chat SSE generator with a monkeypatched `stream_chat` so
    no Ollama server is touched. Asserts event ordering, error handling,
    and that mid-stream client disconnect doesn't crash the server.

Out of scope (require external services / processes):
  * `/llm/status`, `/llm/pull`            — need a live Ollama.
  * `WebSocket /sessions/{id}/terminal`   — needs a Claude CLI under PTY.
  * `/sessions/{id}/workspace*` integration end-to-end (file-system
    inspection of the build-chat workspace lives in build_chat tests).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

# Reuse the pattern from test_e2e_http rather than importing the fixture
# (importing would re-register it twice). We define a local fixture here
# so test discovery is self-contained.


@pytest_asyncio.fixture
async def http_client(tmp_path: Path):
    from lifeman import db as db_mod
    from lifeman.config import settings
    from lifeman.secrets import crypto as secrets_crypto

    prev_path = settings.db_path
    prev_data_dir = settings.data_dir
    prev_sandbox = settings.sandbox_enabled
    settings.db_path = tmp_path / "test.db"
    settings.data_dir = tmp_path
    settings.sandbox_enabled = False
    secrets_crypto.reset_cache_for_tests()

    if db_mod._db is not None:
        await db_mod._db.close()
        db_mod._db = None
    await db_mod.get_db()

    from lifeman.outputs.registry import install_builtin_channels
    from lifeman.inputs import install_handlers as install_input_handlers
    from lifeman.memory import install_handlers as install_memory_handlers
    from lifeman.observations import install_handlers as install_observation_handlers
    install_builtin_channels()
    install_input_handlers()
    install_memory_handlers()
    install_observation_handlers()

    from lifeman.main import app

    headers = {"Authorization": f"Bearer {settings.token}"}
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=headers,
    ) as client:
        try:
            yield client
        finally:
            await db_mod.close_db()
            settings.db_path = prev_path
            settings.data_dir = prev_data_dir
            settings.sandbox_enabled = prev_sandbox
            secrets_crypto.reset_cache_for_tests()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """Parse an SSE response body into a list of (event, data) pairs."""
    out: list[tuple[str, dict]] = []
    event = "message"
    data_lines: list[str] = []
    for raw in text.splitlines():
        if raw == "":
            if data_lines:
                payload = "\n".join(data_lines)
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    parsed = {"_raw": payload}
                out.append((event, parsed))
            event = "message"
            data_lines = []
            continue
        if raw.startswith("event:"):
            event = raw[len("event:"):].strip()
        elif raw.startswith("data:"):
            data_lines.append(raw[len("data:"):].lstrip())
    if data_lines:
        try:
            parsed = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            parsed = {"_raw": "\n".join(data_lines)}
        out.append((event, parsed))
    return out


async def _make_session(client: httpx.AsyncClient, surface: str = "live_chat") -> str:
    r = await client.post("/api/chat/sessions", json={"surface": surface, "title": "t"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_assigns_default_title_when_blank(http_client):
    r = await http_client.post("/api/chat/sessions", json={"surface": "live_chat"})
    assert r.status_code == 200
    body = r.json()
    assert body["surface"] == "live_chat"
    assert body["title"].startswith("Live chat")
    assert body["message_count"] == 0
    assert body["archived_at"] is None


@pytest.mark.asyncio
async def test_create_session_unknown_surface_400(http_client):
    r = await http_client.post("/api/chat/sessions", json={"surface": "wat"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_get_session_404_for_unknown(http_client):
    r = await http_client.get("/api/chat/sessions/nonexistent")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_sessions_filters_by_surface_and_archive(http_client):
    live = await _make_session(http_client, "live_chat")
    build = await _make_session(http_client, "build_chat")

    only_live = await http_client.get("/api/chat/sessions", params={"surface": "live_chat"})
    ids = [s["id"] for s in only_live.json()]
    assert live in ids and build not in ids

    only_build = await http_client.get("/api/chat/sessions", params={"surface": "build_chat"})
    ids = [s["id"] for s in only_build.json()]
    assert build in ids and live not in ids

    # Archive the live session and confirm it disappears from the unfiltered list.
    arch = await http_client.delete(f"/api/chat/sessions/{live}")
    assert arch.status_code == 200
    after = await http_client.get("/api/chat/sessions")
    assert all(s["id"] != live for s in after.json())


@pytest.mark.asyncio
async def test_patch_session_updates_title(http_client):
    sid = await _make_session(http_client)
    r = await http_client.patch(f"/api/chat/sessions/{sid}", json={"title": "renamed"})
    assert r.status_code == 200
    assert r.json()["title"] == "renamed"

    # No-op patch (title=None) leaves it alone.
    r2 = await http_client.patch(f"/api/chat/sessions/{sid}", json={})
    assert r2.json()["title"] == "renamed"


@pytest.mark.asyncio
async def test_list_messages_for_empty_session_is_empty(http_client):
    sid = await _make_session(http_client)
    r = await http_client.get(f"/api/chat/sessions/{sid}/messages")
    assert r.status_code == 200
    assert r.json() == []


# ---------------------------------------------------------------------------
# POST /messages — error paths that don't need the model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_message_to_unknown_session_404(http_client):
    r = await http_client.post(
        "/api/chat/sessions/ghost/messages", json={"content": "hi"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_post_message_to_archived_session_400(http_client):
    sid = await _make_session(http_client)
    await http_client.delete(f"/api/chat/sessions/{sid}")
    r = await http_client.post(
        f"/api/chat/sessions/{sid}/messages", json={"content": "hi"},
    )
    assert r.status_code == 400
    assert "archived" in r.json()["detail"]


@pytest.mark.asyncio
async def test_post_message_to_build_chat_session_409(http_client):
    """build_chat is interactive over a WebSocket — POST must be rejected
    *before* writing the user message, so no orphan row is left behind."""
    sid = await _make_session(http_client, "build_chat")
    r = await http_client.post(
        f"/api/chat/sessions/{sid}/messages", json={"content": "hi"},
    )
    assert r.status_code == 409
    assert "/terminal" in r.json()["detail"]

    # No user message persisted.
    msgs = await http_client.get(f"/api/chat/sessions/{sid}/messages")
    assert msgs.json() == []


# ---------------------------------------------------------------------------
# Live-chat SSE streaming with stream_chat monkeypatched.
# ---------------------------------------------------------------------------


def _fake_stream(deltas: list[dict]):
    """Build an async generator factory for monkeypatching `stream_chat`."""
    async def _gen(messages, tools=None, **kw):
        for d in deltas:
            yield d
    return _gen


@pytest.mark.asyncio
async def test_live_chat_streams_delta_then_done(http_client, monkeypatch):
    from lifeman.routes import chat as chat_mod

    deltas = [
        {"content": "Hello"},
        {"content": ", "},
        {"content": "world!"},
        {"finish_reason": "stop"},
    ]
    monkeypatch.setattr(chat_mod, "stream_chat", _fake_stream(deltas))

    sid = await _make_session(http_client)
    async with http_client.stream(
        "POST", f"/api/chat/sessions/{sid}/messages",
        json={"content": "hi there"},
    ) as r:
        assert r.status_code == 200
        body = await r.aread()
    events = _parse_sse(body.decode())

    types = [e for e, _ in events]
    assert types[:3] == ["delta", "delta", "delta"]
    assert types[-1] == "done"
    assert "".join(d["text"] for e, d in events if e == "delta") == "Hello, world!"

    # Persisted: user + assistant messages.
    msgs = (await http_client.get(f"/api/chat/sessions/{sid}/messages")).json()
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant"]
    assert msgs[0]["content"] == "hi there"
    assert msgs[1]["content"] == "Hello, world!"

    done_payload = events[-1][1]
    assert done_payload["message_id"] == msgs[1]["id"]


@pytest.mark.asyncio
async def test_live_chat_llm_error_emits_error_then_done(http_client, monkeypatch):
    from lifeman.routes import chat as chat_mod
    from lifeman.llm import LLMError

    async def _boom(messages, tools=None, **kw):
        if False:
            yield {}  # make this an async generator
        raise LLMError("backend unreachable")

    monkeypatch.setattr(chat_mod, "stream_chat", _boom)

    sid = await _make_session(http_client)
    async with http_client.stream(
        "POST", f"/api/chat/sessions/{sid}/messages",
        json={"content": "hello"},
    ) as r:
        assert r.status_code == 200
        body = await r.aread()
    events = _parse_sse(body.decode())
    types = [e for e, _ in events]

    assert "error" in types
    assert types[-1] == "done"
    err_payload = next(d for e, d in events if e == "error")
    assert "backend unreachable" in err_payload["message"]


@pytest.mark.asyncio
async def test_live_chat_unexpected_exception_emits_error_then_done(http_client, monkeypatch):
    """Non-LLMError exceptions take the generic crash path; the generator
    still emits `error` followed by `done` so the browser leaves 'thinking'."""
    from lifeman.routes import chat as chat_mod

    async def _boom(messages, tools=None, **kw):
        if False:
            yield {}
        raise RuntimeError("kaboom")

    monkeypatch.setattr(chat_mod, "stream_chat", _boom)

    sid = await _make_session(http_client)
    async with http_client.stream(
        "POST", f"/api/chat/sessions/{sid}/messages",
        json={"content": "hello"},
    ) as r:
        body = await r.aread()
    events = _parse_sse(body.decode())
    types = [e for e, _ in events]
    assert types[-2:] == ["error", "done"]
    err = next(d for e, d in events if e == "error")
    assert "RuntimeError" in err["message"] and "kaboom" in err["message"]


@pytest.mark.asyncio
async def test_live_chat_disconnect_mid_stream_does_not_crash(http_client, monkeypatch):
    """If the client drops mid-stream, the generator should bail out cleanly
    (the route's `request.is_disconnected()` check sits at the top of the
    loop). Subsequent requests on the same session must still work."""
    from lifeman.routes import chat as chat_mod

    async def _slow(messages, tools=None, **kw):
        # Emit one chunk fast, then sleep long enough for us to disconnect.
        yield {"content": "partial"}
        await asyncio.sleep(0.05)
        for i in range(20):
            yield {"content": f" chunk{i}"}
            await asyncio.sleep(0.05)
        yield {"finish_reason": "stop"}

    monkeypatch.setattr(chat_mod, "stream_chat", _slow)

    sid = await _make_session(http_client)

    # Open the SSE stream, read just a tiny prefix, then close abruptly.
    async with http_client.stream(
        "POST", f"/api/chat/sessions/{sid}/messages",
        json={"content": "hi"},
    ) as r:
        assert r.status_code == 200
        # Pull a small slice then break out of the context to disconnect.
        async for _chunk in r.aiter_raw():
            break

    # Give the server a moment to notice the disconnect & clean up.
    await asyncio.sleep(0.1)

    # A fresh request on a new session still succeeds — server is healthy.
    sid2 = await _make_session(http_client)
    fast = [{"content": "ok"}, {"finish_reason": "stop"}]
    monkeypatch.setattr(chat_mod, "stream_chat", _fake_stream(fast))
    async with http_client.stream(
        "POST", f"/api/chat/sessions/{sid2}/messages",
        json={"content": "again"},
    ) as r:
        body = await r.aread()
    events = _parse_sse(body.decode())
    assert events[-1][0] == "done"
