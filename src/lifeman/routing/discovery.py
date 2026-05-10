"""Discover router and handler tools by manifest role, for any domain."""

from __future__ import annotations

import json
import logging

from lifeman.db import get_db
from lifeman.routing.domain import RoutingDomain
from lifeman.routing.event import HandlerManifest

log = logging.getLogger("lifeman.routing.discovery")


async def find_router_tool(domain: RoutingDomain) -> str | None:
    """Newest non-deprecated tool whose manifest has the domain's router role.

    Multiple installed routers means the latest install wins, so the build
    chat can ship a new router without uninstalling the old one first.
    """
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT t.name
             FROM tools t
             JOIN tool_manifests m
               ON m.tool_id = t.id
              AND m.version = (SELECT MAX(version) FROM tool_manifests
                                WHERE tool_id = t.id)
            WHERE t.deprecated_at IS NULL
              AND json_extract(m.manifest_json, '$.role') = ?
            ORDER BY t.installed_at DESC
            LIMIT 1""",
        (domain.router_role,),
    )
    return rows[0]["name"] if rows else None


async def find_handler_tools(
    domain: RoutingDomain,
) -> list[tuple[str, HandlerManifest, dict]]:
    """Every installed tool whose manifest declares it a handler in this domain.

    Returns (tool_name, generic_manifest, raw_extension_dict). The raw
    extension dict is the per-domain manifest fragment under
    `manifest.<handler_manifest_key>`; domains that need typed access can
    cast it themselves.
    """
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT t.name, m.manifest_json
             FROM tools t
             JOIN tool_manifests m
               ON m.tool_id = t.id
              AND m.version = (SELECT MAX(version) FROM tool_manifests
                                WHERE tool_id = t.id)
            WHERE t.deprecated_at IS NULL
              AND json_extract(m.manifest_json, '$.role') = ?""",
        (domain.handler_role,),
    )
    out: list[tuple[str, HandlerManifest, dict]] = []
    for r in rows:
        try:
            manifest = json.loads(r["manifest_json"])
            ext = manifest.get(domain.handler_manifest_key) or {}
            generic = HandlerManifest(
                name=r["name"],
                handler_type=ext.get("channel_type") or ext.get("handler_type", "unknown"),
                capabilities=ext.get("capabilities") or {},
                sensitivity_tolerance=ext.get("sensitivity_tolerance", "personal"),
                rate_limit_per_minute=ext.get("rate_limit_per_minute", 0),
                rate_limit_per_hour=ext.get("rate_limit_per_hour", 0),
                config=ext.get("config", {}),
            )
            out.append((r["name"], generic, ext))
        except Exception as e:  # noqa: BLE001
            log.warning(
                "skipping malformed %s manifest for %s: %s",
                domain.handler_role, r["name"], e,
            )
    return out
