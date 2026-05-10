"""Generic event + decision shapes shared across all routing domains."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RoutedEvent(BaseModel):
    """Base class for any event that flows through the routing engine.

    Domains subclass this and add their own typed fields (categories,
    urgencies, source-channel identifiers, etc.). The framework only
    consults the fields defined here.
    """

    event_id: str = ""              # set by producer or engine
    source: str = ""                # which tool / surface emitted it
    emitted_at: str = ""            # ISO timestamp; set by producer or engine
    expires_at: str | None = None   # routing engine drops the event past this
    sensitivity: str = "personal"   # public | personal | private
    context: dict = Field(default_factory=dict)
    reason: str = ""                # always required at the call-site

    def to_payload(self) -> dict:
        """JSON shape passed to router and handler tools."""
        return self.model_dump()

    def short(self) -> str:
        """One-line preview for audit summaries."""
        return f"{self.__class__.__name__}({self.event_id})"


class HandlerManifest(BaseModel):
    """Generic shape every handler tool's manifest fragment must satisfy.

    Each domain may extend this — outputs uses additional capability flags
    (rich_content, actions, persistence, …) on top.
    """

    name: str
    handler_type: str
    capabilities: dict = Field(default_factory=dict)
    sensitivity_tolerance: str = "personal"
    rate_limit_per_minute: int = 0
    rate_limit_per_hour: int = 0
    config: dict = Field(default_factory=dict)


class RoutingDecision(BaseModel):
    """The contract every router tool must return, regardless of domain."""

    event_id: str
    matched_rules: list[int] = Field(default_factory=list)
    candidate_handlers: list[str] = Field(default_factory=list)
    filtered: dict[str, str] = Field(default_factory=dict)
    dispatched: list[str] = Field(default_factory=list)
    expired: bool = False
    notes: str = ""
