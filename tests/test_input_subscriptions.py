"""Tests for `lifeman.input_subscriptions`.

Covers the two kinds end-to-end against the real DB / inputs router,
with httpx mocked so no real network calls happen.

* CRUD: create / list / patch / delete; webhook returns a one-time secret.
* json_poll: first poll emits an input_event; identical second poll
  emits nothing; modified third poll emits again. 304 / ETag honoured.
* webhook auth: verify_webhook_secret enforces the stored hash.
* Invalid configs are rejected at create time.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lifeman import input_subscriptions as subs


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class _FakeClient:
    def __init__(self, responses) -> None:
        # `responses` is a list-of-FakeResponse, popped per call.
        self._responses = list(responses)
        self.requests: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get(self, url, headers=None):
        self.requests.append((url, dict(headers or {})))
        return self._responses.pop(0)


@pytest.fixture(autouse=True)
def _install_input_handlers():
    """ingest_input dispatches to handlers; install the builtins for tests."""
    from lifeman.inputs import install_handlers
    install_handlers()
    yield


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_webhook_returns_one_time_secret(temp_db):
    row, secret = await subs.create_subscription(
        kind="webhook", name="github prs",
    )
    assert row.kind == "webhook"
    assert row.enabled is True
    assert secret is not None and len(secret) > 20

    # The DB stores only the hash, never the plaintext.
    rows = await temp_db.execute_fetchall(
        "SELECT secret_hash FROM input_subscriptions WHERE id = ?", (row.id,),
    )
    assert rows[0]["secret_hash"] and rows[0]["secret_hash"] != secret


@pytest.mark.asyncio
async def test_create_json_poll_requires_url(temp_db):
    with pytest.raises(ValueError, match="config.url"):
        await subs.create_subscription(
            kind="json_poll", name="weather", config={},
        )


@pytest.mark.asyncio
async def test_create_rejects_unknown_kind(temp_db):
    with pytest.raises(ValueError, match="unsupported"):
        await subs.create_subscription(kind="rss", name="news")


@pytest.mark.asyncio
async def test_create_rejects_short_interval(temp_db):
    with pytest.raises(ValueError, match="interval_seconds"):
        await subs.create_subscription(
            kind="json_poll", name="x",
            config={"url": "https://example.com/x"},
            interval_seconds=5,
        )


@pytest.mark.asyncio
async def test_list_returns_created_subscriptions(temp_db):
    await subs.create_subscription(kind="webhook", name="a")
    await subs.create_subscription(
        kind="json_poll", name="b",
        config={"url": "https://example.com/b"},
    )
    listed = await subs.list_subscriptions()
    names = {s.name for s in listed}
    assert names == {"a", "b"}


@pytest.mark.asyncio
async def test_update_changes_interval_and_disable(temp_db):
    row, _ = await subs.create_subscription(
        kind="json_poll", name="poll",
        config={"url": "https://example.com/x"},
    )
    updated = await subs.update_subscription(
        row.id, interval_seconds=600, enabled=False,
    )
    assert updated.interval_seconds == 600
    assert updated.enabled is False


@pytest.mark.asyncio
async def test_delete_subscription(temp_db):
    row, _ = await subs.create_subscription(kind="webhook", name="x")
    assert await subs.delete_subscription(row.id) is True
    assert await subs.get_subscription(row.id) is None
    assert await subs.delete_subscription(row.id) is False


# ---------------------------------------------------------------------------
# Webhook secret verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_secret_verifies_correct_token(temp_db):
    row, secret = await subs.create_subscription(
        kind="webhook", name="x",
    )
    assert await subs.verify_webhook_secret(row.id, secret) is True
    assert await subs.verify_webhook_secret(row.id, "wrong") is False
    assert await subs.verify_webhook_secret("nonexistent", secret) is False


@pytest.mark.asyncio
async def test_webhook_secret_rejected_when_disabled(temp_db):
    row, secret = await subs.create_subscription(
        kind="webhook", name="x",
    )
    await subs.update_subscription(row.id, enabled=False)
    assert await subs.verify_webhook_secret(row.id, secret) is False


@pytest.mark.asyncio
async def test_poll_subscription_not_callable_for_webhook(temp_db):
    row, _ = await subs.create_subscription(kind="webhook", name="x")
    result = await subs.poll_once(row.id)
    assert result.error and "no poller" in result.error


# ---------------------------------------------------------------------------
# json_poll
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_json_poll_emits_on_first_fetch(temp_db):
    row, _ = await subs.create_subscription(
        kind="json_poll", name="weather",
        config={"url": "https://example.com/wx", "intent_hint": "weather"},
    )
    body = '{"temp": 21.5}'
    fake = _FakeClient([_FakeResponse(200, text=body, headers={"etag": "v1"})])
    with patch("lifeman.input_subscriptions.httpx.AsyncClient", return_value=fake):
        result = await subs.poll_once(row.id)
    assert result.raw_payload == body
    assert result.unchanged is False
    assert result.etag == "v1"

    # An input_event row should be present with source=subscription:<id>.
    rows = await temp_db.execute_fetchall(
        "SELECT raw_payload, source, intent_hint FROM input_events "
        "WHERE source = ?", (f"subscription:{row.id}",),
    )
    assert len(rows) == 1
    assert rows[0]["raw_payload"] == body
    assert rows[0]["intent_hint"] == "weather"


@pytest.mark.asyncio
async def test_json_poll_skips_when_body_unchanged(temp_db):
    row, _ = await subs.create_subscription(
        kind="json_poll", name="x",
        config={"url": "https://example.com/x"},
    )
    body = '{"foo": 1}'
    fake = _FakeClient([
        _FakeResponse(200, text=body),
        _FakeResponse(200, text=body),
    ])
    with patch("lifeman.input_subscriptions.httpx.AsyncClient", return_value=fake):
        first = await subs.poll_once(row.id)
        second = await subs.poll_once(row.id)
    assert first.raw_payload == body
    assert second.unchanged is True
    assert second.raw_payload is None

    rows = await temp_db.execute_fetchall(
        "SELECT id FROM input_events WHERE source = ?",
        (f"subscription:{row.id}",),
    )
    assert len(rows) == 1, "second poll must not emit a duplicate event"


@pytest.mark.asyncio
async def test_json_poll_honours_304_not_modified(temp_db):
    row, _ = await subs.create_subscription(
        kind="json_poll", name="x",
        config={"url": "https://example.com/x"},
    )
    fake = _FakeClient([
        _FakeResponse(200, text="initial", headers={"etag": "v1"}),
        _FakeResponse(304, text="", headers={"etag": "v1"}),
    ])
    with patch("lifeman.input_subscriptions.httpx.AsyncClient", return_value=fake):
        first = await subs.poll_once(row.id)
        second = await subs.poll_once(row.id)
    assert first.etag == "v1"
    assert second.unchanged is True

    # Second poll must have sent If-None-Match: v1.
    assert any(
        h.get("If-None-Match") == "v1" for _u, h in fake.requests
    )


@pytest.mark.asyncio
async def test_json_poll_records_error_on_5xx(temp_db):
    row, _ = await subs.create_subscription(
        kind="json_poll", name="x",
        config={"url": "https://example.com/x"},
    )
    fake = _FakeClient([_FakeResponse(503, text="boom")])
    with patch("lifeman.input_subscriptions.httpx.AsyncClient", return_value=fake):
        result = await subs.poll_once(row.id)
    assert result.error == "HTTP 503"

    refreshed = await subs.get_subscription(row.id)
    assert refreshed.last_status == "error"
    assert "503" in (refreshed.last_error or "")


@pytest.mark.asyncio
async def test_json_poll_emits_again_when_body_changes(temp_db):
    row, _ = await subs.create_subscription(
        kind="json_poll", name="x",
        config={"url": "https://example.com/x"},
    )
    fake = _FakeClient([
        _FakeResponse(200, text='{"v": 1}'),
        _FakeResponse(200, text='{"v": 2}'),
    ])
    with patch("lifeman.input_subscriptions.httpx.AsyncClient", return_value=fake):
        await subs.poll_once(row.id)
        await subs.poll_once(row.id)
    rows = await temp_db.execute_fetchall(
        "SELECT raw_payload FROM input_events WHERE source = ? "
        "ORDER BY emitted_at",
        (f"subscription:{row.id}",),
    )
    assert len(rows) == 2
    assert rows[0]["raw_payload"] != rows[1]["raw_payload"]
