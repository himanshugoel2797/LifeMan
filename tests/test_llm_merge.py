"""Tests for `merge_tool_call_deltas`.

Streaming LLM backends emit tool-call fragments incrementally. The
merge function must concatenate `name` and `arguments` strings rather
than replacing on every fragment.
"""

from __future__ import annotations

from lifeman.llm import merge_tool_call_deltas


def test_merge_concatenates_name_fragments():
    accum: list[dict] = []
    merge_tool_call_deltas(accum, [{"index": 0, "function": {"name": "sched"}}])
    merge_tool_call_deltas(accum, [{"index": 0, "function": {"name": "ule_tool"}}])
    assert accum[0]["function"]["name"] == "schedule_tool"


def test_merge_concatenates_arguments_fragments():
    accum: list[dict] = []
    merge_tool_call_deltas(accum, [{"index": 0, "function": {"arguments": '{"a"'}}])
    merge_tool_call_deltas(accum, [{"index": 0, "function": {"arguments": ': 1, "b": 2}'}}])
    assert accum[0]["function"]["arguments"] == '{"a": 1, "b": 2}'


def test_merge_assembles_id_from_first_fragment():
    accum: list[dict] = []
    merge_tool_call_deltas(accum, [{"index": 0, "id": "call_abc", "function": {"name": "f"}}])
    merge_tool_call_deltas(accum, [{"index": 0, "function": {"arguments": "{}"}}])
    assert accum[0]["id"] == "call_abc"
    assert accum[0]["function"]["arguments"] == "{}"


def test_merge_grows_accum_to_index():
    """A fragment for index=2 must allocate slots 0 and 1 too."""
    accum: list[dict] = []
    merge_tool_call_deltas(accum, [{"index": 2, "function": {"name": "third"}}])
    assert len(accum) == 3
    assert accum[2]["function"]["name"] == "third"
    assert accum[0]["function"]["name"] == ""
    assert accum[1]["function"]["name"] == ""


def test_merge_handles_multiple_indices_in_one_delta():
    accum: list[dict] = []
    merge_tool_call_deltas(accum, [
        {"index": 0, "function": {"name": "a"}},
        {"index": 1, "function": {"name": "b"}},
    ])
    assert accum[0]["function"]["name"] == "a"
    assert accum[1]["function"]["name"] == "b"


def test_merge_ignores_explicit_none_fragments():
    """Some servers send `{"name": null}` between real fragments. That must
    not corrupt the accumulated value."""
    accum: list[dict] = []
    merge_tool_call_deltas(accum, [{"index": 0, "function": {"name": "good"}}])
    merge_tool_call_deltas(accum, [{"index": 0, "function": {"name": None}}])
    assert accum[0]["function"]["name"] == "good"
