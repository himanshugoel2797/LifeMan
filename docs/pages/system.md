# System — `/system`

Template: [system.html](src/lifeman/templates/system.html). Route:
`system_page` in [ui.py](src/lifeman/routes/ui.py).

Read-only summary of operational state: LLM token usage and the
encrypted backup queue. Mutations go through `/api/system/...`.

## LLM usage

Token counts recorded by [`lifeman.usage.record_usage`](src/lifeman/usage.py)
on every chat turn and router LLM fallback. Three views stacked:

- **Stat tiles** — calls + tokens for the last 24h and lifetime.
- **By-surface table** — per-source rollup (`live_chat`,
  `output_router`, …), 24h alongside lifetime so you can spot the
  surface that suddenly started chewing through context.
- **Recent calls** (collapsed) — the 25 newest rows with model,
  prompt/completion split, and latency.

API: `GET /api/system/usage?surface=&session_id=&since=&limit=` returns
the same rows plus `totals`.

## Backups

Encrypted SQLite snapshots produced by
[`lifeman.backup`](src/lifeman/backup.py). The header shows the
configured cadence and retention; the table lists existing files
newest-first.

- **Create backup now** — `POST /api/system/backups`. Surfaces a toast
  with the new filename and reloads.
- **Restore** — `POST /api/system/backups/restore` with
  `{name, confirm: true}`. The dialog warns that the live DB will be
  overwritten; the previous file is kept at `data.db.pre-restore` for
  one rollback.

Backups encrypt with the master key (same one used for secrets). If
you lose the key file, the backup is unrecoverable — back the key up
separately.

## What this page doesn't yet show

- A "restore-from-elsewhere" upload flow (you'd drop a file into the
  backup dir manually for now).
- Per-model cost / dollar estimates (we record token counts, not
  prices).
- A "purge usage rows older than N" knob.
