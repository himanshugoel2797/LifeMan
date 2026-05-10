# Tools — `/tools` and `/tools/{tool_id}`

Templates: [tools.html](src/lifeman/templates/tools.html) (list),
[tool_detail.html](src/lifeman/templates/tool_detail.html) (detail).
Routes: [ui.py:45-111](src/lifeman/routes/ui.py#L45-L111).

The registry view. Lists every non-deprecated tool with a one-line
manifest summary; clicking a name opens the detail page where you can
read the source, invoke it manually, and see recent runs.

## List page

Lists active tools (`tools WHERE deprecated_at IS NULL`, alphabetical),
each as a card with:

- **Name** (links to detail)
- **Category** and **version** tags
- **Description** from the registration call
- **Counts** for `manifest.reads`, `manifest.writes`, `manifest.network`
  (the lengths of the declared capability lists — see
  [concepts/tools.md](../concepts/tools.md))
- **Installed date** (truncated to the day)

A collapsed `+ Register a new tool` form at the top accepts a name,
description, category, manifest JSON, input/output schemas, and Python
source, and POSTs to `/api/tools`. The hint under the submit button
reminds you that for non-trivial tools this is the wrong door — most
tools should be authored in the [build chat](build_chat.md) instead.

If `schema_input` is non-empty, the runtime enforces it on every
invocation — bad args fail with a `schema_input` error before the
sandbox is launched. Empty `{}` skips the gate. `schema_output` is
documentation only.

A `tool_registered` SSE event reloads the page so freshly built tools
appear without a manual refresh.

## Detail page

Renders one tool's full record:

- **Header** — name, description, category + version tags, deprecation
  tag if `deprecated_at` is set, install timestamp.
- **Action buttons** — *Invoke* opens an inline form for one-off runs;
  *Deprecate* marks the tool hidden (it stays in the DB and old
  invocations remain visible). Deprecate disappears once the tool is
  already deprecated.
- **Invoke form** — JSON `args` and a free-text `reason`. POSTs to
  `/api/tools/{id}/invoke`; the result lands inline as pretty-printed
  JSON. This is the same path the LLM and the scheduler take, so
  permission prompts that fire here surface in the Permissions page
  exactly like any other invocation.
- **Manifest** — the latest `tool_manifests` row pretty-printed.
- **Code** — the Python source as stored. No editing in the UI; if you
  want to change a tool, register a new version through the API or
  rebuild it via the build chat.
- **Recent Invocations** — most recent 20 rows from `invocations`,
  newest first. Each is a `<details>` element you can expand to see
  args, result, error, and finish timestamp. The first one is open by
  default.

## State machine for a tool

`tools` rows are append-only:

- `installed_at` set on registration; `version` increments when a tool
  with the same name is re-registered.
- `deprecated_at` set when the user clicks Deprecate. The list query
  filters these out, but invocation history still references them by
  name (since `invocations.tool` is the name string, not a foreign
  key).

There is no "edit" or "delete" surface — both are intentional. To
update a tool, re-register; to remove one, deprecate it.
