"""Observation-domain types."""

from __future__ import annotations

from pydantic import BaseModel, Field

from lifeman.routing.event import RoutedEvent


class ObservationEvent(RoutedEvent):
    message: str
    level: str = "info"          # debug | info | warn | error
    component: str = ""


class ObserveRequest(BaseModel):
    message: str
    level: str = "info"
    component: str = ""
    source: str = ""
    sensitivity: str = "personal"
    expires_at: str | None = None
    context: dict = Field(default_factory=dict)
    reason: str = ""


class ObserveResponse(BaseModel):
    event_id: str
    dispatched: list[str] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)
    expired: bool = False
