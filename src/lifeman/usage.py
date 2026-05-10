"""LLM usage recording — single insert helper used by every call site.

The `usage` dict shape is the same one `lifeman.llm.stream_chat` yields:
    {"prompt_tokens": int, "completion_tokens": int,
     "total_tokens": int, "model": str}

A no-op when the backend didn't report usage (None or empty dict). Failures
are logged at warning level; usage accounting is never load-bearing on the
calling path.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from lifeman.db import get_db

log = logging.getLogger("lifeman.usage")


async def record_usage(
    usage: dict | None,
    *,
    surface: str,
    session_id: str | None = None,
    latency_ms: int | None = None,
) -> None:
    if not usage:
        return
    try:
        db = await get_db()
        await db.execute(
            """INSERT INTO llm_usage
                 (surface, session_id, model, prompt_tokens, completion_tokens,
                  total_tokens, latency_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                surface,
                session_id,
                usage.get("model") or "",
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                int(usage.get("total_tokens") or 0),
                latency_ms,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        log.exception("failed to record llm_usage row")
