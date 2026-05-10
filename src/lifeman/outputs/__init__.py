"""Output system: structured events + router + extensible channels.

Tools call `emit_output(...)` instead of picking a delivery channel directly.
The router decides — based on category, urgency, user state, preferences and
installed channels — which channels surface the event.

Public surface:
    emit_output(...)        — record an event and route it.
    cancel_output(id, ...)  — recall a delivered event from every channel.
    report_response(...)    — channel-side callback when a user acts on an event.

See OUTPUT_DESIGN.MD for the design rationale and full schema.
"""

from __future__ import annotations

from lifeman.outputs.api import (
    cancel_output,
    emit_output,
    report_response,
)

__all__ = ["emit_output", "cancel_output", "report_response"]
