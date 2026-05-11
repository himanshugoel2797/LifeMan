"""Ambient LLM cycle: a periodic, tool-call-capable reasoning loop.

This is the kernel's only proactive primitive. Every N minutes
(``settings.ambient_interval_minutes``) the cycle runs:

* A small seed prompt nudges the LLM to look around — recent
  interactions, scheduled jobs, recent observations, stored memory —
  and decide whether anything warrants reaching out.
* The full live-chat tool surface is available, so the LLM can both
  investigate (recall, list_scheduled, recent_interactions, observe)
  and act (emit_output, schedule, cancel) in the same tick.
* The cycle is NOT a chat session: messages live in-memory and are
  *not* persisted to the ``messages`` table. Ambient reasoning isn't
  conversation — persisting it would pollute live-chat history and
  the next ambient tick would re-read its own previous tick.
* Each cycle is bounded by ``settings.ambient_max_iterations`` to keep
  a confused LLM from burning the host. Each cycle is audited.

Gated by :func:`lifeman.user_state.get_state` — if ``do_not_disturb``
or ``asleep`` is set, the cycle short-circuits with an audit row.

Lifespan: started/stopped by ``main.py`` alongside the scheduler.
Disabled by default — flip ``LIFEMAN_AMBIENT_ENABLED=true`` to turn on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time as _time
import uuid

from lifeman import audit
from lifeman.config import settings

log = logging.getLogger("lifeman.ambient")


_task: asyncio.Task | None = None


DEFAULT_SYSTEM_PROMPT = (
    "You are the background reasoning loop of a personal companion. You "
    "wake up on a timer — the user did NOT invoke you. Your job is to "
    "survey state (recall recent memories, list scheduled jobs, look at "
    "recent interactions, check recent observations) and decide whether "
    "anything concrete warrants reaching out to the user RIGHT NOW.\n\n"
    "Most ticks should end silently. Bias hard toward NOT emitting. "
    "Only call emit_output when there's a specific, useful thing the "
    "user benefits from being told at this moment — not summaries of "
    "what you looked at, not status updates, not 'I checked and "
    "everything is fine'. If you do emit, emit exactly once with a "
    "concrete message; set category and urgency deliberately.\n\n"
    "Do NOT chat with yourself. Do NOT narrate your reasoning. When "
    "you have nothing to say, produce no tool calls and no text."
)

DEFAULT_USER_PROMPT = (
    "Survey recent state and decide if anything warrants the user's "
    "attention right now. End silently if not."
)


def _build_user_prompt(state: dict) -> str:
    """Default user prompt enriched with the live user-state context.

    The LLM sees what the system has inferred (time of day, recent
    activity, busy status, device reachability) without needing to call
    tools for the basics. Saves a round-trip and grounds the decision
    in real signals."""
    bits: list[str] = []
    if state.get("period"):
        bits.append(f"period={state['period']}")
    if state.get("weekday"):
        bits.append(f"weekday={state['weekday']}")
    if state.get("activity"):
        bits.append(f"activity={state['activity']}")
    if state.get("idle_minutes") is not None:
        bits.append(f"idle_minutes={state['idle_minutes']}")
    if state.get("device_online") is not None:
        bits.append(
            f"device_online={'true' if state['device_online'] else 'false'}"
        )
    context = "; ".join(bits) if bits else "(no signals)"
    return (
        f"{DEFAULT_USER_PROMPT}\n\n"
        f"Inferred user-state right now: {context}.\n"
        "Use this to decide: if the user is long-idle, prefer recording "
        "an observation over emitting an interrupt."
    )


def _skip_reason(state: dict) -> str | None:
    """Decide whether to skip a tick based on user state.

    Skip when:
    * do_not_disturb is on (user's explicit override),
    * the user is currently busy (an inferred busy window from an input
      is active), or
    * activity has been long_idle past the configured threshold (no
      point thinking if there's been nobody around for an hour).
    """
    from lifeman.config import settings

    if state.get("do_not_disturb"):
        return "do_not_disturb"
    if state.get("busy"):
        return "busy"
    idle = state.get("idle_minutes")
    if isinstance(idle, int) and idle >= settings.ambient_skip_after_idle_minutes:
        return f"long_idle:{idle}m"
    return None


async def start() -> None:
    """Launch the recurring ambient cycle if enabled."""
    global _task
    if _task is not None and not _task.done():
        return
    if not settings.ambient_enabled or settings.ambient_interval_minutes <= 0:
        log.info("ambient cycle disabled")
        return
    interval_s = settings.ambient_interval_minutes * 60.0
    _task = asyncio.create_task(_loop(interval_s))
    log.info("ambient cycle started; interval=%.0fs", interval_s)


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    _task = None
    log.info("ambient cycle stopped")


async def _loop(interval_s: float) -> None:
    """Sleep interval, fire one cycle, repeat. Survives individual failures."""
    # First sleep so a hot-restart loop doesn't fire immediately.
    await asyncio.sleep(interval_s)
    while True:
        try:
            await run_one_cycle()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("ambient cycle iteration crashed; continuing")
        await asyncio.sleep(interval_s)


async def run_one_cycle(
    *,
    system_prompt: str | None = None,
    user_prompt: str | None = None,
) -> dict:
    """Run a single ambient cycle. Returns a summary for inspection.

    Summary shape::

        {
            "tick_id":  str,    # short uuid; "" when skipped
            "iterations": int,  # model-turn rounds completed
            "tool_calls": int,  # total tool calls dispatched
            "finished": str,    # "done" | "max_iterations" | "llm_error:..."
            "skipped":  bool,
            "skip_reason": str | None,
        }

    Tests and the (future) "force a tick" admin route both use this.
    """
    # Lazy imports so a misconfigured LLM/chat module doesn't break the
    # outputs system that imports this transitively.
    from lifeman.chat_tools import dispatch as dispatch_tool, tool_specs
    from lifeman.llm import LLMError, merge_tool_call_deltas, stream_chat
    from lifeman.usage import record_usage
    from lifeman.user_state import get_state

    state = await get_state()
    skip_reason = _skip_reason(state)
    if skip_reason is not None:
        await audit.log(
            source="ambient", action="ambient_tick_skipped",
            args_summary=skip_reason, reason="user state suppresses ambient tick",
        )
        return {
            "tick_id": "", "iterations": 0, "tool_calls": 0,
            "finished": "skipped", "skipped": True, "skip_reason": skip_reason,
        }

    tick_id = str(uuid.uuid4())[:12]
    sys_p = system_prompt or DEFAULT_SYSTEM_PROMPT
    usr_p = user_prompt or _build_user_prompt(state)
    messages: list[dict] = [
        {"role": "system", "content": sys_p},
        {"role": "user", "content": usr_p},
    ]
    specs = tool_specs()
    max_iter = settings.ambient_max_iterations
    tool_calls_total = 0
    iterations_done = 0
    finished_reason = "done"

    try:
        for iteration in range(max_iter):
            iterations_done = iteration + 1
            text_buf: list[str] = []
            tool_calls_accum: list[dict] = []
            finish: str | None = None
            usage: dict | None = None
            started_ms = _time.monotonic() * 1000
            async for delta in stream_chat(messages, tools=specs):
                if "content" in delta and delta["content"]:
                    text_buf.append(delta["content"])
                if "tool_calls" in delta and delta["tool_calls"]:
                    merge_tool_call_deltas(tool_calls_accum, delta["tool_calls"])
                if "finish_reason" in delta:
                    finish = delta["finish_reason"]
                if "usage" in delta:
                    usage = delta["usage"]
            await record_usage(
                usage, surface="ambient",
                latency_ms=int(_time.monotonic() * 1000 - started_ms),
            )
            assistant_msg: dict = {
                "role": "assistant", "content": "".join(text_buf) or "",
            }
            if tool_calls_accum:
                assistant_msg["tool_calls"] = tool_calls_accum
            messages.append(assistant_msg)

            if not (finish == "tool_calls" or tool_calls_accum):
                break

            for call in tool_calls_accum:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                raw_args = fn.get("arguments") or "{}"
                # Ambient ticks aren't a chat session — no session_id to thread.
                result = await dispatch_tool(name, raw_args, session_id=None)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or name,
                    "content": json.dumps(result),
                })
                tool_calls_total += 1
        else:
            finished_reason = "max_iterations"
    except LLMError as e:
        log.warning("ambient tick %s: LLM unavailable: %s", tick_id, e)
        finished_reason = f"llm_error:{type(e).__name__}"

    await audit.log(
        source="ambient", action="ambient_tick", target=tick_id,
        args_summary=f"iterations={iterations_done} tool_calls={tool_calls_total}",
        result_summary=finished_reason[:200],
    )
    return {
        "tick_id": tick_id,
        "iterations": iterations_done,
        "tool_calls": tool_calls_total,
        "finished": finished_reason,
        "skipped": False,
        "skip_reason": None,
    }
