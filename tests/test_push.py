"""Direct unit tests for `lifeman.push.send_wake_push`.

The route-level tests in test_push_endpoint mock send_wake_push wholesale —
this module exercises the actual httpx call so the status-code → return-value
mapping has coverage. Each test stubs out httpx.AsyncClient so no real
network IO happens.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from lifeman import push


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeClient:
    """Drop-in replacement for httpx.AsyncClient that records the POST args."""

    def __init__(self, response: _FakeResponse | Exception) -> None:
        self._response = response
        self.last_post: dict | None = None

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def post(self, url, *, content=None, headers=None) -> _FakeResponse:
        self.last_post = {"url": url, "content": content, "headers": headers}
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


@pytest.mark.asyncio
async def test_send_wake_push_returns_ok_for_2xx():
    fake = _FakeClient(_FakeResponse(202))
    with patch("lifeman.push.httpx.AsyncClient", return_value=fake):
        result = await push.send_wake_push(
            device_id="dev-1", transport="unifiedpush",
            endpoint="https://ntfy.sh/upABC", output_id="out-1",
        )
    assert result == "ok"
    assert fake.last_post is not None
    assert fake.last_post["url"] == "https://ntfy.sh/upABC"
    assert fake.last_post["headers"]["Content-Type"] == "application/json"
    # Body must mention the output id so the device can drain /pending fast.
    body = fake.last_post["content"]
    assert "output_id" in body and "out-1" in body


@pytest.mark.asyncio
async def test_send_wake_push_returns_gone_for_410():
    fake = _FakeClient(_FakeResponse(410))
    with patch("lifeman.push.httpx.AsyncClient", return_value=fake):
        result = await push.send_wake_push(
            device_id="dev-1", transport="unifiedpush",
            endpoint="https://ntfy.sh/dead", output_id=None,
        )
    assert result == "gone"


@pytest.mark.asyncio
async def test_send_wake_push_returns_error_for_5xx():
    fake = _FakeClient(_FakeResponse(503))
    with patch("lifeman.push.httpx.AsyncClient", return_value=fake):
        result = await push.send_wake_push(
            device_id="dev-1", transport="unifiedpush",
            endpoint="https://ntfy.sh/upABC", output_id="out-1",
        )
    assert result == "error"


@pytest.mark.asyncio
async def test_send_wake_push_returns_error_on_network_failure():
    fake = _FakeClient(httpx.ConnectError("boom"))
    with patch("lifeman.push.httpx.AsyncClient", return_value=fake):
        result = await push.send_wake_push(
            device_id="dev-1", transport="unifiedpush",
            endpoint="https://ntfy.sh/down", output_id="out-1",
        )
    assert result == "error"


@pytest.mark.asyncio
async def test_send_wake_push_rejects_unknown_transport():
    """No HTTP call should fire for a transport the kernel doesn't speak."""
    httpx_mock = AsyncMock()
    with patch("lifeman.push.httpx.AsyncClient", new=httpx_mock):
        result = await push.send_wake_push(
            device_id="dev-1", transport="carrier-pigeon",
            endpoint="https://x.example/abc", output_id="out-1",
        )
    assert result == "error"
    httpx_mock.assert_not_called()


@pytest.mark.asyncio
async def test_send_wake_push_omits_output_id_when_none():
    fake = _FakeClient(_FakeResponse(200))
    with patch("lifeman.push.httpx.AsyncClient", return_value=fake):
        await push.send_wake_push(
            device_id="dev-1", transport="unifiedpush",
            endpoint="https://ntfy.sh/upABC", output_id=None,
        )
    # Body should be the empty object — no output_id leaks to a third-party
    # distributor when the caller didn't supply one.
    assert fake.last_post is not None
    assert fake.last_post["content"] == "{}"
