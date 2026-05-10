"""Pydantic models for API, manifests, and MCP tool I/O."""

from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Tool models
# ---------------------------------------------------------------------------

class ToolManifest(BaseModel):
    reads: list[str] = Field(default_factory=list, description="Data categories the tool can read")
    writes: list[str] = Field(default_factory=list, description="Data categories the tool can write")
    network: list[str] = Field(
        default_factory=list,
        description=(
            "Allowed egress hosts. Special tokens: '@unrestricted' (any "
            "internet host), '@local' (loopback + RFC1918 only). Empty "
            "list = no network."
        ),
    )
    compute_limits: dict = Field(default_factory=dict, description="CPU/mem/time limits")
    triggers: list[str] = Field(default_factory=list, description="Tools/actions this tool can trigger")
    user_visible: bool = True
    # Output system roles per OUTPUT_DESIGN.MD §"Architecture":
    #   "general" (default), "router" (the active output router),
    #   "output_channel" (delivery target). Multiple tools may share a
    #   role; the latest one installed wins (router) or all participate
    #   (channels).
    role: str = "general"
    # When role == "output_channel", channel-specific manifest fields.
    output_channel: dict | None = None


class ToolCreate(BaseModel):
    name: str
    description: str
    category: str = "general"
    manifest: ToolManifest = Field(default_factory=ToolManifest)
    schema_input: dict = Field(default_factory=dict, description="JSON Schema for args")
    schema_output: dict = Field(default_factory=dict, description="JSON Schema for return")
    code: str = Field(description="Python source code for the tool")


class Tool(BaseModel):
    id: str
    name: str
    description: str
    category: str
    version: int
    installed_at: str
    deprecated_at: str | None = None


class ToolDetail(Tool):
    manifest: ToolManifest
    schema_input: dict = Field(default_factory=dict)
    schema_output: dict = Field(default_factory=dict)
    code: str = ""


class ToolSummary(BaseModel):
    id: str
    name: str
    description: str
    category: str
    manifest_summary: dict = Field(default_factory=dict)
    invocations_last_week: int = 0


# ---------------------------------------------------------------------------
# Permission models
# ---------------------------------------------------------------------------

class PermissionRequestCreate(BaseModel):
    capability: str
    scope: dict = Field(default_factory=dict)
    reason: str


class PermissionGrant(BaseModel):
    id: str
    granter: str
    grantee: str
    capability: str
    scope: dict
    granted_at: str
    expires_at: str | None = None
    revoked_at: str | None = None


class PermissionRequestRecord(BaseModel):
    id: str
    requester: str
    capability: str
    scope: dict
    reason: str
    status: str  # pending | granted_once | granted_always | denied
    requested_at: str
    resolved_at: str | None = None


class PermissionResolve(BaseModel):
    action: str  # allow_once | allow_always | deny


# ---------------------------------------------------------------------------
# Schedule models
# ---------------------------------------------------------------------------

class ScheduleCreate(BaseModel):
    tool: str
    args: dict = Field(default_factory=dict)
    when: str | int | float | dict  # relative duration, seconds, ISO timestamp, or {recur, at}
    context_refs: list[str] = Field(default_factory=list)
    reason: str


class Schedule(BaseModel):
    id: str
    tool: str
    args: dict
    when_spec: str | dict
    context_refs: list[str]
    reason: str
    created_at: str
    fires_at: str
    last_fired: str | None = None
    consecutive_no_ops: int = 0
    total_fires: int = 0
    cancelled_at: str | None = None


class ScheduleUpdate(BaseModel):
    args: dict | None = None
    context_refs: list[str] | None = None


class Reschedule(BaseModel):
    when: str | int | float | dict


# ---------------------------------------------------------------------------
# Invocation models
# ---------------------------------------------------------------------------

class InvokeRequest(BaseModel):
    tool: str
    args: dict = Field(default_factory=dict)
    reason: str


class Invocation(BaseModel):
    id: str
    tool: str
    args: dict
    source: str  # llm | tool | schedule | user
    result: dict | None = None
    error: str | None = None
    started_at: str
    finished_at: str | None = None
    schedule_id: str | None = None
    session_id: str | None = None
    parent_invocation_id: str | None = None
    status: str = "completed"
    reason: str = ""


# ---------------------------------------------------------------------------
# Audit models
# ---------------------------------------------------------------------------

class AuditEntry(BaseModel):
    id: int
    timestamp: str
    source: str
    action: str
    target: str
    args_summary: str
    result_summary: str
    reason: str


class AuditQuery(BaseModel):
    target: str | None = None
    source: str | None = None
    action: str | None = None
    before: str | None = None
    after: str | None = None
    limit: int = 50


# ---------------------------------------------------------------------------
# Build request models
# ---------------------------------------------------------------------------

class BuildRequestCreate(BaseModel):
    description: str
    reason: str
    priority: str = "soon"  # now | soon | whenever


class BuildRequest(BaseModel):
    id: str
    description: str
    reason: str
    priority: str
    status: str  # queued | user_review_needed | approved | completed
    created_at: str
    resolved_at: str | None = None


# ---------------------------------------------------------------------------
# Session models
# ---------------------------------------------------------------------------

class Session(BaseModel):
    id: str
    surface: str  # live_chat | build_chat | scheduled | tool_initiated
    title: str = ""
    external_id: str | None = None
    started_at: str
    last_message_at: str
    message_count: int
    archived_at: str | None = None


class SessionCreate(BaseModel):
    surface: str = "live_chat"  # live_chat | build_chat
    title: str = ""


class SessionUpdate(BaseModel):
    title: str | None = None


# ---------------------------------------------------------------------------
# Chat message models
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    id: str
    session_id: str
    role: str  # user | assistant | tool | system
    content: str
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    created_at: str
    seq: int


class ChatSendRequest(BaseModel):
    content: str


# ---------------------------------------------------------------------------
# System models
# ---------------------------------------------------------------------------

class SystemStatus(BaseModel):
    uptime: float
    active_schedules: int
    pending_permissions: int
    recent_errors: int
    resource_usage: dict = Field(default_factory=dict)


class UserStatus(BaseModel):
    available: bool = True
    last_active: str = ""
    current_focus: str | None = None
    do_not_disturb: bool = False


# ---------------------------------------------------------------------------
# Generic responses
# ---------------------------------------------------------------------------

class OkResponse(BaseModel):
    ok: bool = True


class IdResponse(BaseModel):
    id: str


class ScheduleResponse(BaseModel):
    id: str
    fires_at: str


class InvokeResponse(BaseModel):
    invocation_id: str
    status: str  # running | completed | permission_required
    result: dict | None = None
    error: str | None = None
