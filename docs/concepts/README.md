# Concepts

What each concept in lifeman is and how it fits with the rest. These
docs are meant to be read alongside the [UI page docs](../pages/) — the
pages render concepts; these explain what they actually mean.

For the kernel's internal layout (process model, storage, runtime
surfaces), read [ARCHITECTURE.md](../ARCHITECTURE.md). For per-file
descriptions, [FILES.md](../FILES.md). The docs here are about the
ideas you need to hold to use the system, not the code that
implements them.

| Concept | What it is |
|---------|-----------|
| [tools.md](tools.md) | Sandboxed Python units with manifests; the unit of capability |
| [tool_runtime.md](tool_runtime.md) | Methods sandboxed tools can call back into the core |
| [sandbox.md](sandbox.md) | bubblewrap isolation, seccomp, network policy |
| [permissions.md](permissions.md) | Capability strings, scopes, grants, the prompt flow |
| [secrets.md](secrets.md) | Encrypted store, allow-list / standing-grant / prompt resolution |
| [scheduling.md](scheduling.md) | Schedules, recurrences, context_refs, double-fire avoidance |
| [activities.md](activities.md) | Invocations: every tool run, the cross-cutting feed |
| [routing_domains.md](routing_domains.md) | The four-domain pattern (outputs, inputs, memory, observations) |
| [outputs.md](outputs.md) | Output events, channels, routing rules, sensitivity gates |
| [inputs.md](inputs.md) | Inbound user events and the input router |
| [memory.md](memory.md) | Memory events, memory writers, recall semantics |
| [observations.md](observations.md) | Internal log fabric, archive / summarize / discard policy |
| [build_requests.md](build_requests.md) | The queue the LLM uses to ask for new tools |
| [chat_surfaces.md](chat_surfaces.md) | Live chat (Qwen) vs build chat (Claude Code) |
| [audit_log.md](audit_log.md) | Mutation log, what's logged where, observation carve-out |
| [sse.md](sse.md) | Event bus, replay cursor, dropped-event handling |
| [auth.md](auth.md) | Master token vs device tokens, pairing flow, loopback gate |
