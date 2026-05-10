"""Local LLM client.

Talks to Ollama (or any OpenAI-compatible server) via `/v1/chat/completions`.
Streams deltas and surfaces tool-call fragments as they arrive.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

import httpx

from lifeman.config import settings


class LLMError(RuntimeError):
    pass


async def stream_chat(
    messages: list[dict],
    tools: list[dict] | None = None,
    model: str | None = None,
    temperature: float = 0.7,
) -> AsyncIterator[dict]:
    """Stream OpenAI-style chat completion deltas from the LLM backend.

    Yields dicts shaped like the `delta` field of an OpenAI streaming chunk:
        {"content": "..."}                              # token
        {"tool_calls": [{"index": 0, "id": "...", ...}]} # tool-call fragment
        {"finish_reason": "stop" | "tool_calls"}        # final marker
        {"usage": {"prompt_tokens": N, "completion_tokens": M,
                    "total_tokens": K, "model": "..."}}  # if backend reports it
    """
    chosen_model = model or settings.llm_model
    payload = {
        "model": chosen_model,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
        # Ask OpenAI-compatible servers to include usage in the final chunk.
        # Ollama honours this from 0.5+; older builds ignore it silently.
        "stream_options": {"include_usage": True},
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    url = settings.llm_base_url.rstrip("/") + "/v1/chat/completions"
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
        try:
            async with client.stream("POST", url, json=payload) as r:
                if r.status_code >= 400:
                    body = await r.aread()
                    raise LLMError(f"LLM server {r.status_code}: {body[:400].decode(errors='replace')}")
                async for raw in r.aiter_lines():
                    if not raw or not raw.startswith("data:"):
                        continue
                    data = raw[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    # `usage` may arrive in a trailing chunk that has no
                    # `choices`. Surface it before falling through.
                    usage = chunk.get("usage")
                    if usage:
                        yield {
                            "usage": {
                                "prompt_tokens": usage.get("prompt_tokens"),
                                "completion_tokens": usage.get("completion_tokens"),
                                "total_tokens": usage.get("total_tokens"),
                                "model": chunk.get("model") or chosen_model,
                            }
                        }
                    choices = chunk.get("choices") or []
                    if not choices:
                        if usage:
                            # Final usage-only chunk — end of stream.
                            return
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    finish = choice.get("finish_reason")
                    if delta:
                        yield delta
                    if finish:
                        yield {"finish_reason": finish}
                        return
        except httpx.HTTPError as e:
            raise LLMError(f"LLM server unreachable at {url}: {e}") from e


def merge_tool_call_deltas(
    accum: list[dict], delta_calls: list[dict]
) -> list[dict]:
    """Fold streaming tool_call deltas into a stable list.

    OpenAI streams tool calls as fragments keyed by `index`. We assemble
    them into complete `{id, type, function: {name, arguments}}` objects.
    """
    for frag in delta_calls:
        idx = frag.get("index", 0)
        while len(accum) <= idx:
            accum.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
        slot = accum[idx]
        if "id" in frag and frag["id"]:
            slot["id"] = frag["id"]
        if "type" in frag and frag["type"]:
            slot["type"] = frag["type"]
        fn_frag = frag.get("function") or {}
        fn = slot["function"]
        if "name" in fn_frag and fn_frag["name"] is not None:
            fn["name"] = (fn["name"] or "") + fn_frag["name"]
        if "arguments" in fn_frag and fn_frag["arguments"] is not None:
            fn["arguments"] = (fn["arguments"] or "") + fn_frag["arguments"]
    return accum
