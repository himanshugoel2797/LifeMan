"""Tests for error paths in `lifeman.llm.stream_chat`.

The streaming function talks to an OpenAI-compatible server over HTTP.
We don't want a real Ollama in tests, so we install an `httpx.MockTransport`
by monkeypatching `httpx.AsyncClient` to always pass our transport.

Failure modes covered:
  * Upstream HTTP 4xx/5xx -> LLMError with status + body fragment.
  * Transport-level network error -> wrapped as LLMError.
  * Malformed JSON in an SSE `data:` line -> silently skipped, stream continues.
  * Non-`data:` and blank lines -> ignored.
  * Empty/zero-token response (immediate `[DONE]`) -> yields nothing, no raise.
  * Network error mid-stream -> raises LLMError after partial yields, no
    corruption of caller-visible state (yields seen so far stay valid).
"""

from __future__ import annotations

import json
from typing import AsyncIterator

import httpx
import pytest

from lifeman import llm
from lifeman.llm import LLMError, stream_chat


def _install_transport(monkeypatch: pytest.MonkeyPatch, transport: httpx.AsyncBaseTransport) -> None:
    """Force `httpx.AsyncClient(...)` calls inside llm.py to use our transport."""
    real_cls = httpx.AsyncClient

    class _Patched(real_cls):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(llm.httpx, "AsyncClient", _Patched)


def _sse_response(lines: list[str], status_code: int = 200) -> httpx.Response:
    body = ("\n".join(lines) + "\n").encode()
    return httpx.Response(
        status_code,
        headers={"content-type": "text/event-stream"},
        content=body,
    )


def _sse_chunk(content: str | None = None, finish: str | None = None) -> str:
    choice: dict = {"delta": {}}
    if content is not None:
        choice["delta"]["content"] = content
    if finish is not None:
        choice["finish_reason"] = finish
    return "data: " + json.dumps({"choices": [choice]})


async def _collect(it: AsyncIterator[dict]) -> list[dict]:
    out = []
    async for item in it:
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# HTTP error from upstream
# ---------------------------------------------------------------------------

async def test_http_500_raises_llmerror_with_body(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"upstream exploded")

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    with pytest.raises(LLMError) as ei:
        await _collect(stream_chat([{"role": "user", "content": "hi"}]))
    msg = str(ei.value)
    assert "500" in msg
    assert "upstream exploded" in msg


async def test_http_400_raises_llmerror(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b'{"error":"bad model"}')

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    with pytest.raises(LLMError) as ei:
        await _collect(stream_chat([{"role": "user", "content": "hi"}]))
    assert "400" in str(ei.value)


# ---------------------------------------------------------------------------
# Transport-level network error (connection refused, dns, etc.)
# ---------------------------------------------------------------------------

async def test_connect_error_wrapped_as_llmerror(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    with pytest.raises(LLMError) as ei:
        await _collect(stream_chat([{"role": "user", "content": "hi"}]))
    assert "unreachable" in str(ei.value)


async def test_read_error_wrapped_as_llmerror(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection dropped", request=request)

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    with pytest.raises(LLMError):
        await _collect(stream_chat([{"role": "user", "content": "hi"}]))


# ---------------------------------------------------------------------------
# Malformed SSE / JSON
# ---------------------------------------------------------------------------

async def test_malformed_json_chunk_is_skipped(monkeypatch):
    """A bad JSON `data:` line must not crash the stream."""
    lines = [
        "data: {not json at all",
        _sse_chunk(content="hello"),
        _sse_chunk(finish="stop"),
        "data: [DONE]",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(lines)

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    out = await _collect(stream_chat([{"role": "user", "content": "hi"}]))
    # We got the content delta and a finish marker — the bad line was dropped.
    assert {"content": "hello"} in out
    assert {"finish_reason": "stop"} in out


async def test_blank_and_non_data_lines_ignored(monkeypatch):
    lines = [
        "",
        ": this is a comment",
        "event: ping",
        _sse_chunk(content="ok"),
        "data: [DONE]",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(lines)

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    out = await _collect(stream_chat([{"role": "user", "content": "hi"}]))
    assert out == [{"content": "ok"}]


async def test_chunk_with_no_choices_skipped(monkeypatch):
    """Some servers emit a leading metadata chunk with `choices: []`."""
    lines = [
        "data: " + json.dumps({"choices": []}),
        _sse_chunk(content="x"),
        "data: [DONE]",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(lines)

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    out = await _collect(stream_chat([{"role": "user", "content": "hi"}]))
    assert out == [{"content": "x"}]


# ---------------------------------------------------------------------------
# Empty / zero-token response
# ---------------------------------------------------------------------------

async def test_immediate_done_yields_nothing(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(["data: [DONE]"])

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    out = await _collect(stream_chat([{"role": "user", "content": "hi"}]))
    assert out == []


async def test_empty_body_yields_nothing(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"", headers={"content-type": "text/event-stream"})

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    out = await _collect(stream_chat([{"role": "user", "content": "hi"}]))
    assert out == []


# ---------------------------------------------------------------------------
# Network error mid-stream
# ---------------------------------------------------------------------------

async def test_network_error_mid_stream_raises_after_partial_yields(monkeypatch):
    """If the transport raises while iter_lines is consuming, stream_chat
    must surface it as LLMError — and any items already yielded stay valid."""
    good_chunk = _sse_chunk(content="partial").encode() + b"\n"

    class _FlakyByteStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield good_chunk
            raise httpx.ReadError("connection died mid-stream")

        async def aclose(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_FlakyByteStream(),
        )

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    seen: list[dict] = []
    with pytest.raises(LLMError):
        async for item in stream_chat([{"role": "user", "content": "hi"}]):
            seen.append(item)
    # The partial token yielded before the failure is intact, not corrupted.
    assert seen == [{"content": "partial"}]
