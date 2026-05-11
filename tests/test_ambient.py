"""Tests for the ambient cycle.

Covers `run_one_cycle` directly with a mocked LLM so we don't need Ollama.
Verifies the three observable shapes:

* silent tick — model decides nothing's worth surfacing, returns without
  tool calls.
* tick that emits an output — model calls `emit_output`, the output row
  shows up in the DB.
* tick that's gated by user_state — DND/asleep flag short-circuits the
  cycle and audits the skip.
* tick bounded by ambient_max_iterations.
"""

from __future__ import annotations

import pytest

from lifeman import ambient
from lifeman import user_state


@pytest.fixture(autouse=True)
def _isolate_user_state():
    """Each test starts with an empty user_state provider registry."""
    user_state.clear_providers()
    yield
    user_state.clear_providers()


@pytest.mark.asyncio
async def test_silent_tick_returns_done_without_tool_calls(temp_db, monkeypatch):
    async def fake_stream(messages, tools=None, model=None, temperature=0.7):
        # Model decides nothing's worth surfacing.
        yield {"content": ""}
        yield {"finish_reason": "stop"}

    import lifeman.llm as llm_mod
    monkeypatch.setattr(llm_mod, "stream_chat", fake_stream)

    summary = await ambient.run_one_cycle()
    assert summary["finished"] == "done"
    assert summary["tool_calls"] == 0
    assert summary["iterations"] == 1
    assert summary["skipped"] is False
    assert summary["tick_id"]  # non-empty


@pytest.mark.asyncio
async def test_tick_that_emits_output_persists_row(temp_db, monkeypatch):
    """The model calls emit_output via a tool call; the output_events
    table should reflect it after the tick."""
    import json

    sent_tool_call = False

    async def fake_stream(messages, tools=None, model=None, temperature=0.7):
        nonlocal sent_tool_call
        if not sent_tool_call:
            sent_tool_call = True
            yield {"tool_calls": [{
                "index": 0,
                "id": "call-1",
                "function": {
                    "name": "emit_output",
                    "arguments": json.dumps({
                        "content": "drink water",
                        "category": "reminder",
                        "urgency": "soft",
                        "reason": "noticed user hasn't logged water in 3h",
                    }),
                },
            }]}
            yield {"finish_reason": "tool_calls"}
        else:
            yield {"content": ""}
            yield {"finish_reason": "stop"}

    import lifeman.llm as llm_mod
    monkeypatch.setattr(llm_mod, "stream_chat", fake_stream)

    # Install builtin output channels so emit_output has somewhere to route.
    from lifeman.outputs.registry import install_builtin_channels
    install_builtin_channels()

    summary = await ambient.run_one_cycle()
    assert summary["finished"] == "done"
    assert summary["tool_calls"] >= 1

    rows = await temp_db.execute_fetchall(
        "SELECT id, category, urgency FROM output_events"
    )
    assert len(rows) == 1
    assert rows[0]["category"] == "reminder"
    assert rows[0]["urgency"] == "soft"


@pytest.mark.asyncio
async def test_dnd_short_circuits_tick(temp_db, monkeypatch):
    async def dnd_provider():
        return {"do_not_disturb": True}

    user_state.register_provider("dnd_test", dnd_provider)

    called = False

    async def fake_stream(messages, tools=None, model=None, temperature=0.7):
        nonlocal called
        called = True
        yield {"finish_reason": "stop"}

    import lifeman.llm as llm_mod
    monkeypatch.setattr(llm_mod, "stream_chat", fake_stream)

    summary = await ambient.run_one_cycle()
    assert summary["skipped"] is True
    assert summary["skip_reason"] == "do_not_disturb"
    assert not called, "LLM must not run under DND"

    # And the skip must show up in the audit log.
    audit_rows = await temp_db.execute_fetchall(
        "SELECT action FROM audit_log WHERE source = 'ambient'"
    )
    assert any(r["action"] == "ambient_tick_skipped" for r in audit_rows)


@pytest.mark.asyncio
async def test_asleep_short_circuits_tick(temp_db, monkeypatch):
    async def asleep_provider():
        return {"asleep": True}

    user_state.register_provider("asleep_test", asleep_provider)

    async def fake_stream(messages, tools=None, model=None, temperature=0.7):
        yield {"finish_reason": "stop"}

    import lifeman.llm as llm_mod
    monkeypatch.setattr(llm_mod, "stream_chat", fake_stream)

    summary = await ambient.run_one_cycle()
    assert summary["skipped"] is True
    assert summary["skip_reason"] == "asleep"


@pytest.mark.asyncio
async def test_max_iterations_caps_runaway_model(temp_db, monkeypatch):
    """A model that keeps emitting tool calls must stop at the configured
    iteration ceiling — otherwise an ambient tick could spin forever."""
    import json
    from lifeman.config import settings

    async def fake_stream(messages, tools=None, model=None, temperature=0.7):
        # Always ask for another tool call → would loop forever without a cap.
        yield {"tool_calls": [{
            "index": 0,
            "id": "call-x",
            "function": {
                "name": "now",
                "arguments": json.dumps({}),
            },
        }]}
        yield {"finish_reason": "tool_calls"}

    import lifeman.llm as llm_mod
    monkeypatch.setattr(llm_mod, "stream_chat", fake_stream)

    prev = settings.ambient_max_iterations
    settings.ambient_max_iterations = 3
    try:
        summary = await ambient.run_one_cycle()
    finally:
        settings.ambient_max_iterations = prev

    assert summary["iterations"] == 3
    assert summary["finished"] == "max_iterations"


@pytest.mark.asyncio
async def test_llm_error_records_audit_and_returns(temp_db, monkeypatch):
    from lifeman.llm import LLMError

    async def boom(messages, tools=None, model=None, temperature=0.7):
        # An async generator that raises on first iteration.
        raise LLMError("connection refused")
        yield  # pragma: no cover

    import lifeman.llm as llm_mod
    monkeypatch.setattr(llm_mod, "stream_chat", boom)

    summary = await ambient.run_one_cycle()
    assert summary["finished"].startswith("llm_error")
    audit_rows = await temp_db.execute_fetchall(
        "SELECT action, result_summary FROM audit_log WHERE source = 'ambient'"
    )
    assert any(r["action"] == "ambient_tick" for r in audit_rows)
