"""Pydantic models for the output system.

These mirror the schema in OUTPUT_DESIGN.MD. Categories and urgencies are
free-form strings (not enums) so new values can be added without a code
change to the core — the router falls back to conservative defaults for
unknown values.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# Conventional values — kept as constants for tooling, but not enforced.
CATEGORIES = (
    "status", "completion", "reminder", "query", "intervention",
    "permission_request", "alert", "urgent", "digest", "progress", "presence",
)
URGENCIES = ("ambient", "soft", "persistent", "urgent")
SENSITIVITIES = ("public", "personal", "private")


class StructuredContent(BaseModel):
    title: str
    body: str = ""
    fields: dict[str, str] | None = None
    image_url: str | None = None
    markdown: str | None = None


class Action(BaseModel):
    label: str
    invoke_tool: str
    invoke_args: dict = Field(default_factory=dict)
    confirmation_required: bool = False


class OutputEvent(BaseModel):
    """An event a tool wants surfaced to the user."""

    output_id: str = ""                 # set by core
    source_tool: str = ""               # set by core
    emitted_at: str = ""                # set by core
    content: str | StructuredContent
    category: str = "status"
    urgency: str = "ambient"
    expires_at: str | None = None
    sensitivity: str = "personal"       # public | personal | private
    context: dict = Field(default_factory=dict)
    actions: list[Action] = Field(default_factory=list)
    reason: str = ""

    def short(self) -> str:
        """One-line preview for logs and audit summaries."""
        if isinstance(self.content, StructuredContent):
            return self.content.title or self.content.body[:80]
        return str(self.content)[:120]


class EmitOutputRequest(BaseModel):
    content: str | StructuredContent
    category: str = "status"
    urgency: str = "ambient"
    expires_at: str | None = None
    sensitivity: str = "personal"
    context: dict = Field(default_factory=dict)
    actions: list[Action] = Field(default_factory=list)
    reason: str = ""


class EmitOutputResponse(BaseModel):
    output_id: str
    dispatched: list[str] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)
    expired: bool = False


class CancelOutputResponse(BaseModel):
    ok: bool
    cancelled_channels: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Channel manifest types
# ---------------------------------------------------------------------------

InterruptionLevel = Literal["background", "foreground", "demanding"]


class ChannelCapabilities(BaseModel):
    rich_content: bool = False
    images: bool = False
    actions: bool = False
    persistence: bool = False
    interruption_level: InterruptionLevel = "background"
    typical_latency_ms: int = 100


class ChannelManifest(BaseModel):
    name: str
    channel_type: str
    capabilities: ChannelCapabilities = Field(default_factory=ChannelCapabilities)
    rate_limit_per_minute: int = 0      # 0 = unlimited
    rate_limit_per_hour: int = 0
    sensitivity_tolerance: str = "personal"  # max sensitivity this channel will carry
    config: dict = Field(default_factory=dict)


class DeliveryResult(BaseModel):
    delivered: bool
    delivery_id: str | None = None
    failure_reason: str | None = None
    response: "UserResponse | None" = None


class UserResponse(BaseModel):
    action_label: str
    invoked_tool: str | None = None
    raw_input: str | None = None
    captured_at: str


DeliveryResult.model_rebuild()


# ---------------------------------------------------------------------------
# Routing rule schema
# ---------------------------------------------------------------------------

class RuleMatch(BaseModel):
    category: str | None = None
    urgency: str | None = None
    state: str | None = None             # e.g. "do_not_disturb", "asleep"
    urgency_below: str | None = None
    source_tool: str | None = None


class RuleAction(BaseModel):
    channels: list[str] | Literal["all_available"] = Field(default_factory=list)
    except_urgency: list[str] = Field(default_factory=list)
    defer_to_digest: bool = False
    is_override: bool = False            # state-conditional override


class RoutingRule(BaseModel):
    position: int
    match: RuleMatch
    action: RuleAction
    description: str = ""


# ---------------------------------------------------------------------------
# Routing audit
# ---------------------------------------------------------------------------

class RoutingDecision(BaseModel):
    output_id: str
    matched_rules: list[int] = Field(default_factory=list)
    candidate_channels: list[str] = Field(default_factory=list)
    filtered: dict[str, str] = Field(default_factory=dict)  # channel -> reason
    dispatched: list[str] = Field(default_factory=list)
    expired: bool = False
    notes: str = ""
