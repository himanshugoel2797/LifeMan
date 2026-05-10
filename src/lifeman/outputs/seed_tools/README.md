# Seed output tools

Reference implementations the build chat (or you) can register via
`POST /api/tools` to take over routing or add new channels. They are NOT
auto-installed — the in-process default router and built-in channels stay
active until you replace them.

To install one:

    curl -X POST http://localhost:8390/api/tools \
      -H "Authorization: Bearer $LIFEMAN_TOKEN" \
      -d @router_seed.json

Once installed, the next `emit_output` call will use it.

## Files

- `router_seed.py` — drop-in replacement for the in-process default router.
  Same rule set, expressed as a sandboxed tool. Edit the rules to change
  routing behaviour.
- `channel_console.py` — example minimal channel tool that just logs
  delivered events to the core log. Useful as a template when adding new
  channels.

## I/O contracts

See `lifeman.outputs.tool_backed` for the exact JSON shapes the router and
channel tools must accept and return. Both contracts are stable; the core
treats `manifest.role` as the dispatcher.

## Roles (namespaced per domain)

The output system uses two manifest roles:

- `manifest.role = "output_router"` — the routing brain. The newest
  installed one wins.
- `manifest.role = "output_channel"` — a delivery target. All installed
  channels are eligible at routing time.

Other domains (input routing, memory writes, log routing) follow the same
shape with their own namespaced roles (e.g. `input_router`,
`memory_writer`). See `lifeman.routing` for the framework.
