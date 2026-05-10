"""Input-domain types."""

from __future__ import annotations

from pydantic import BaseModel, Field

from lifeman.routing.event import RoutedEvent


class InputEvent(RoutedEvent):
    surface: str                            # voice | chat | notification_click | watch | api
    raw_payload: str = ""
    intent_hint: str | None = None          # caller suggestion: "invoke" | "chat" | "command" | None


class IngestInputRequest(BaseModel):
    surface: str
    raw_payload: str
    intent_hint: str | None = None
    source: str = ""
    sensitivity: str = "personal"
    expires_at: str | None = None
    context: dict = Field(default_factory=dict)
    reason: str = ""


class IngestInputResponse(BaseModel):
    event_id: str
    dispatched: list[str] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)
    expired: bool = False


class IngestBatchRequest(BaseModel):
    """A bag of input events from one client.

    Used by device clients (CLIENT_DESIGN.MD) to amortise HTTP overhead
    when their outbox has accumulated multiple observations — sensors,
    foreground-app changes, notification fan-out — between uploads.
    Each event is processed independently; partial failures are reported
    per-event in the response.
    """
    events: list[IngestInputRequest] = Field(default_factory=list)


class IngestBatchItemResult(BaseModel):
    """Per-event result inside a batch response.

    ``ok=True`` and ``response`` set means the event was ingested; the
    embedded ``IngestInputResponse`` has the dispatch/dropped detail.
    ``ok=False`` and ``error`` set means this single event failed; the
    rest of the batch was still attempted.
    """
    ok: bool
    response: IngestInputResponse | None = None
    error: str | None = None


class IngestBatchResponse(BaseModel):
    results: list[IngestBatchItemResult]
