"""Memory-domain types."""

from __future__ import annotations

from pydantic import BaseModel, Field

from lifeman.routing.event import RoutedEvent


class MemoryEvent(RoutedEvent):
    content: str
    type_hint: str | None = None              # episodic | semantic | identity | summary
    tags: list[str] = Field(default_factory=list)


class RecordMemoryRequest(BaseModel):
    content: str
    type_hint: str | None = None
    tags: list[str] = Field(default_factory=list)
    source: str = ""
    sensitivity: str = "personal"
    expires_at: str | None = None
    context: dict = Field(default_factory=dict)
    reason: str = ""


class RecordMemoryResponse(BaseModel):
    event_id: str
    dispatched: list[str] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)
    expired: bool = False


class Memory(BaseModel):
    id: str
    content: str
    type: str
    tags: list[str] = Field(default_factory=list)
    sensitivity: str = "personal"
    source: str = ""
    created_at: str
    classified_by: str = "router"
