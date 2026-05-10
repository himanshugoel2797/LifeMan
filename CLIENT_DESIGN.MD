# Client App Design

How users interface with lifeman from devices other than the loopback web UI.
The kernel stays unchanged: clients are *sensors and surfaces*, not logic
owners. They ingest device-side observations into `/api/inputs` and render
delivered events from the output system back to the user.

## Core principle

Clients carry no business logic. They have three responsibilities:

1. **Observe.** Collect sensor data, app usage, foreground state,
   notifications, screen captures, location — whatever the device exposes.
2. **Forward.** Wrap each observation as an input event and POST it to
   `/api/inputs`. The kernel's input router decides what it means.
3. **Render.** Receive output events delivered to a per-device channel and
   surface them via the platform's native notification / UI primitives.

Everything else — classification, routing, intervention, recall, scheduling
— stays in the kernel. The client is dumb on purpose; reasoning happens once,
server-side, where it's auditable and shared across surfaces.

## Stack choice

- **.NET 9 with .NET MAUI** for the Android phone app and the Windows
  desktop app, single shared codebase (~70% shared, the rest per-platform
  via `#if ANDROID` / `#if WINDOWS` or partial-class platform splits).
- **C#** throughout. No Kotlin in v1.
- **WearOS deferred** — see [WearOS strategy](#wearos-strategy) below.

Why MAUI:
- Native UI on each platform, single project structure.
- Microsoft maintains .NET for Android (formerly Xamarin.Android) bindings
  for the full Android API surface, including the niche bits we need
  (UsageStatsManager, AccessibilityService, NotificationListenerService,
  MediaProjection, SensorManager).
- Windows side gets WinUI 3 + full Win32 interop via `System.Runtime.
  InteropServices` and `CsWin32`. No bridge layer.
- Sideload-friendly: no Play Store / Microsoft Store constraints on
  signing, target SDK pressure, or policy review.

What MAUI doesn't help with:
- WearOS — Microsoft killed Xamarin.Wear bindings with Xamarin EOL.
- iOS — possible but out of scope for v1.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                  Lifeman.Client (shared C#)                      │
│                                                                  │
│   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐        │
│   │  Collector   │   │   Outbox     │   │   Output     │        │
│   │  Registry    │──▶│  (SQLite)    │──▶│   Renderer   │        │
│   │              │   │              │   │              │        │
│   └──────────────┘   └──────┬───────┘   └──────────────┘        │
│                             │                   ▲                │
│                       ┌─────┴────────┐    ┌────┴────────┐       │
│                       │  Uploader    │    │  SSE / FCM  │       │
│                       │  + retry     │    │  receiver   │       │
│                       └──────┬───────┘    └────┬────────┘       │
│                              │                  │                │
└──────────────────────────────┼──────────────────┼────────────────┘
                               │                  │
                               ▼                  ▲
                      ┌────────────────────────────────┐
                      │      lifeman kernel             │
                      │ POST /api/inputs                │
                      │ POST /api/outputs/{id}/respond  │
                      │ GET  /events  (SSE)             │
                      └────────────────────────────────┘
       │                                                       │
       │  per-platform native code (collectors, renderers)     │
       ▼                                                       ▼
┌────────────────────────┐                ┌────────────────────────┐
│  Android (.NET for     │                │  Windows (WinUI 3 +    │
│  Android)              │                │  CsWin32)              │
│ ─ SensorManager        │                │ ─ Foreground window    │
│ ─ UsageStatsManager    │                │ ─ Process list (WMI)   │
│ ─ AccessibilityService │                │ ─ Idle time            │
│ ─ NotifListenerService │                │ ─ Sensors (if any)     │
│ ─ MediaProjection      │                │ ─ ETW (later)          │
│ ─ Location (FLP)       │                │ ─ Win32 notifications  │
│ ─ Health Connect       │                │                        │
└────────────────────────┘                └────────────────────────┘
```

### Shared core (`Lifeman.Client`)

- **Collector registry.** Each collector implements
  `ICollector { string Surface { get; } IAsyncEnumerable<InputEvent>
  Stream(CancellationToken ct); }`. The host process subscribes to each
  collector's stream and writes events to the outbox. Collectors are
  platform-specific but the interface lives in shared code.
- **Outbox.** Local SQLite table `outbox(id, surface, payload_json,
  emitted_at, attempts, last_error)`. Size-bounded; oldest events dropped
  when over the cap (default 100MB).
- **Uploader.** Drains the outbox, POSTs batches to `/api/inputs`, marks
  rows uploaded. Exponential backoff on errors. Adaptive batch size: 1 row
  per request on Wi-Fi, up to 50 batched on metered/cellular to amortise
  radio wakes.
- **SSE / FCM receiver.** Long-lived `EventSource` (Wi-Fi & desktop) or
  push (cellular, future). Drives the output renderer.
- **Pairing state.** Stores the device token (encrypted at rest using the
  platform keystore — Android Keystore / Windows DPAPI), server URL,
  collector enablement, and per-collector sample rates.

### Platform-specific layers

Live under MAUI's per-target folders (`Platforms/Android/`,
`Platforms/Windows/`). Each implements one or more `ICollector` plus a
platform-specific output renderer.

## Observation surfaces

### Android

| Surface | Source | Permission | Volume |
|---|---|---|---|
| `phone.sensor.<name>` | `SensorManager` | none | high (downsampled) |
| `phone.location` | Fused Location Provider | `ACCESS_FINE_LOCATION` | medium |
| `phone.app_usage` | `UsageStatsManager` + AccessibilityService | `PACKAGE_USAGE_STATS`, accessibility bind | high |
| `phone.foreground_app` | AccessibilityService `onAccessibilityEvent` | accessibility bind | medium |
| `phone.notification` | `NotificationListenerService` | listener bind | medium |
| `phone.screen` | screen on/off broadcasts | none | low |
| `phone.battery` | `BatteryManager` | none | low |
| `phone.network` | `ConnectivityManager` | `ACCESS_NETWORK_STATE` | low |
| `phone.calendar` | `CalendarContract` (opt-in) | `READ_CALENDAR` | low |
| `phone.health.<metric>` | Health Connect | per-record-type | low |
| `phone.screen_capture` | `MediaProjection` | runtime prompt + FGS | very high |

Sensor data is **downsampled in the collector**, not in the kernel. Raw
accelerometer at 200Hz is useless to the LLM; what matters is "user is
walking" or "device picked up." Each sensor collector ships with a
downsampler (windowed mean, peak detection, activity classifier) and emits
1–60 events/min depending on the signal.

Foreground-app and notification streams are noisy by nature — the kernel's
input router is the gate. Don't filter client-side beyond obvious noise
(skip duplicate consecutive foreground events from the same package).

Screen capture is opt-in per session and capped: sample 1 frame every N
seconds (configurable per-device, default 30s), JPEG-compressed, base64'd
into the input payload. Not for OCR — for "what's roughly on screen" cues
the LLM can use as context. If OCR is needed, do it server-side on
delivered frames.

### Windows

| Surface | Source | Permission | Volume |
|---|---|---|---|
| `desktop.active_window` | `GetForegroundWindow` + `GetWindowText` | none | medium |
| `desktop.process_list` | `Process.GetProcesses()` + WMI | none | low (5-min poll) |
| `desktop.idle_time` | `GetLastInputInfo` | none | low |
| `desktop.screen_capture` | Windows Graphics Capture API | none (UAC-clean) | high |
| `desktop.notification` | `UserNotificationListener` | runtime prompt | medium |
| `desktop.power` | `SystemEvents.PowerModeChanged` | none | low |
| `desktop.network` | `NetworkChange` events | none | low |

No accessibility analog needed on Windows — `UIAutomation` gives you the
same surface (window title, control values, automation tree) without a
special permission, but it's expensive to poll continuously. Use it
on-demand if a tool requests deeper context.

Windows is simpler than Android in almost every way. The hard part is
running as a real background service (Windows Service vs scheduled task vs
auto-start in the user session). v1: auto-start in user session, no
service install, because services run as SYSTEM and lose access to
per-user state.

### WearOS strategy

v1: nothing on the watch. Health Connect on the phone receives heart rate,
steps, sleep stages, activity sessions, calories, SpO2 from any WearOS 3+
watch that has Health Connect sync enabled. The Android client reads
Health Connect and ingests those as `phone.health.<metric>` events.

v2 (only if raw IMU / real-time signals are needed): a ~200-line Kotlin
Wear service that subscribes to the watch's `SensorManager` and posts to
the paired phone via Google's Data Layer API. The phone client receives,
re-tags as `watch.sensor.<name>`, and uploads. No standalone watch ↔
server connection.

Why this split:
- Health Connect covers the practical sensor surface for behavior /
  wellness use cases.
- Raw biosignal streams are unusual to actually need; defer the cost of a
  watch app until usage proves it.
- A watch app means another sideload-and-permission flow per device, plus
  WearOS-specific lifecycle handling (always-on, ambient mode, complications).

## Server communication

### Outbound: input events

Every observation becomes an `InputEventCreate`:

```http
POST /api/inputs HTTP/1.1
Authorization: Bearer <device_token>
Content-Type: application/json

{
  "surface": "phone.foreground_app",
  "raw_payload": "{\"package\":\"com.slack\",\"activity\":\"...\",\"timestamp\":\"2026-05-10T13:00:00Z\"}",
  "intent_hint": null,
  "source": "device:hgoel-pixel-7",
  "reason": "foreground app changed"
}
```

`surface` is the routing key the input router uses to pick a handler.
`raw_payload` is opaque JSON — the kernel doesn't validate its shape; the
matched handler does.

Batching: when the outbox has more than one ready event, the client POSTs
to `/api/inputs/batch` (new endpoint — see [Server changes](#server-side-additions))
with `{events: [...]}` to avoid per-event HTTP overhead.

### Inbound: output delivery

Each device registers as an output channel on first pair-up:

- Channel name: `device:<device_id>` (e.g. `device:hgoel-pixel-7`).
- Channel manifest declares the device's capabilities: `images`,
  `actions`, `persistence`, `interruption_level`. A phone is
  `actions=true, persistence=true, interruption_level=foreground`; a
  desktop is similar; future watch is `images=false, actions=true,
  persistence=false`.

Delivery uses one of two transports:

1. **SSE** (default). Client maintains a long-lived `GET /events?token=...`
   connection and listens for `output.deliver:<device_id>` events.
   Cheap on Wi-Fi & desktop; battery-expensive on cellular.
2. **FCM push** (later). Server posts a wake-only message to FCM; client
   pulls from `/api/outputs/pending?since=...`. Battery-friendly on
   cellular. Requires running a Google project; deliberately deferred.

The client's output renderer maps the received event to a platform
notification:

- Android: `NotificationCompat.Builder` with action buttons matching the
  event's `actions` field. Click → POST `/api/outputs/{id}/respond` with
  the chosen `action_label`.
- Windows: `AppNotification` (toast) with `AppNotificationButton`s,
  same flow.

### Response actions

When the user clicks an action, the client POSTs to
`/api/outputs/{id}/respond` with `{action_label, raw_input?, channel:
"device:<id>"}`. The existing `report_response` flow takes over from
there.

### Auth

See [server-side auth](#server-side-additions) — that's the next doc.
Summary:
- Pairing flow produces a per-device token at first install.
- Token sent on every request as `Authorization: Bearer ...`.
- Tokens revocable from the server UI; revoke ⇒ client gets 401 ⇒
  re-pair flow.

## Permission model

### Android (sideloaded)

Granted once at provisioning. The client UI walks the user through each
manually, or — preferred — generates an ADB command list the user runs
once with the device tethered:

```bash
adb shell pm grant dev.lifeman.client android.permission.PACKAGE_USAGE_STATS
adb shell appops set dev.lifeman.client GET_USAGE_STATS allow
adb shell settings put secure enabled_accessibility_services \
  dev.lifeman.client/.collectors.A11yCollector
adb shell settings put secure enabled_notification_listeners \
  dev.lifeman.client/.collectors.NotificationCollector
adb shell pm grant dev.lifeman.client android.permission.ACCESS_FINE_LOCATION
adb shell pm grant dev.lifeman.client android.permission.POST_NOTIFICATIONS
```

The provisioning UI on the lifeman server shows these commands pre-filled
with the chosen package id.

Permissions that the runtime *cannot* grant via ADB and must still be
prompted:
- `MediaProjection` consent — system-mandated per session.
- Some OEM-specific battery optimisation exclusions.
- Some OEM-specific autostart permissions (Xiaomi, Huawei).

The client periodically self-audits each declared collector's permission
state and emits an `observation` event when a critical permission goes
missing, so the kernel can surface it in the audit log.

### Windows

No special permissions. The first run prompts for:
- Notification permission (`AppNotificationManager`).
- Whether to install an auto-start shortcut in
  `shell:startup`.

### Foreground services (Android)

The Android client runs a single foreground service of type
`dataSync | mediaProjection | specialUse`, with a persistent notification
("lifeman is observing"). The notification is mandatory by OS design from
Android 13+ for FGS; we surface it as a feature, not hide it.

## Local storage & offline

Each client keeps a SQLite database under app-private storage:

- `config`: device token (encrypted), server URL, enabled collectors,
  per-collector sample rates.
- `outbox`: pending input events.
- `received`: dedup table of output IDs the client has surfaced (so SSE
  replay on reconnect doesn't double-notify). Bounded to 30 days.
- `health`: per-collector last-success timestamp, error counters; used by
  the self-audit observation emitter.

The outbox is the offline buffer: lose connectivity for hours, events
accumulate; reconnect, drain. Hard cap on disk usage; oldest non-critical
events drop first. Critical events (`urgency=urgent` outbound, the rare
case) are kept regardless.

## Provisioning flow

1. **On the server, in the loopback browser:** open `/system/devices`,
   click *Pair new device*. Server generates a one-time pairing code
   (8 chars, ~5 min TTL) plus a QR encoding `lifeman://pair?host=<server-url>&code=<code>`.
2. **Install the client:**
   - Android: install the sideloaded APK, open it.
   - Windows: install MSIX or extract portable zip, run.
3. **Client first-run UI:**
   - "Scan QR" (Android, uses camera permission just for this) or
     "Enter pair URL" (Windows / Android fallback).
   - Client POSTs `/api/auth/pair` with `{code, device_name, platform,
     model, capabilities: [...]}`.
   - Server validates the code, issues a long-lived device token, marks
     the code consumed, inserts an `output_channels` row for
     `device:<device_id>`.
4. **Permission walkthrough.** Client shows OS-level permissions still
   needed (the ADB commands or in-app deep links). User completes them.
5. **First collector heartbeat.** Each enabled collector emits a single
   "hello" event so the kernel + UI can confirm the device is live.
6. **Server `/system/devices` shows the new entry** with capabilities,
   last-seen, and revoke button.

The pairing code is consumable once; if it expires unused, the user
generates a new one. Pairing codes never grant access by themselves —
they're trade-up tokens for a real device token.

## Distribution & updates

- **Signing.** Self-signed keystore. Key stored offline; back it up
  alongside the master DB key.
- **Distribution channel.** `/api/system/client-updates/<platform>` on
  the lifeman server returns `{version, sha256, download_url}`. The
  download_url can be the server itself (`/api/system/client-updates/<platform>/download`)
  or any static host.
- **Update check.** Client polls weekly + on every reconnect. If a newer
  version is available, downloads in the background, surfaces a
  `category=alert urgency=soft` output event prompting the user to
  install. No silent install on sideloaded Android — the OS always
  shows the install dialog.
- **Rollback.** Keep the prior APK locally; if the new version
  crash-loops three times in 24h, the OS-side prompt isn't enough — for
  v2 we may add an "uninstall + reinstall previous" recovery; v1 is
  manual.

## Out of scope for v1

- **iOS / macOS clients.** Add when explicitly needed.
- **Voice in/out.** Punted until output channels mature.
- **Camera input.** Privacy-sensitive; no current use case.
- **Always-on mic.** Same.
- **Web-browser-history capture.** Per-browser extensions are a separate
  project.
- **Multiple servers.** One device pairs with exactly one lifeman
  instance.
- **End-to-end encryption between client & server.** Tailscale / VPN
  carries the transport security. Adding app-layer E2E doesn't buy much
  when both ends are devices we own and the server is single-user.
- **Cross-device sync.** Each device pairs independently; the kernel is
  the only shared state.

## Risks

- **Battery drain.** Continuous sensor sampling + FGS can chew 10–20%/day
  if not throttled. Adaptive sampling per collector; aggressive backoff
  on `BATTERY_LOW` broadcasts.
- **Permission drift.** Android OS updates and aggressive battery
  optimisations revoke permissions or kill the FGS silently, especially
  on Xiaomi / Samsung. Self-audit observation events are the canary; the
  user has to fix it manually.
- **Accessibility-service trust.** The a11y service can read every app's
  screen content. The threat model has to assume the device is fully
  trusted (single-user, sideloaded, paired with our own server). If the
  device is shared, lifeman shouldn't be on it.
- **Sideload signing-key compromise.** Anyone with the key can ship a
  fake "update" via DNS hijacking. Mitigation: pin the server's update
  endpoint to a Tailscale-only host, or include a static public key in
  the client and sign update manifests separately from the APK.
- **MediaProjection prompt fatigue.** Every screen-record session shows a
  system dialog. Cannot bypass without a system signature. If it
  matters, accept the friction or skip the feature.
- **Data volume.** Sensors + screenshots can produce 1–5 GB/day at high
  sample rates. The kernel's `/system` page should grow a "data volume
  per device per day" tile so this is visible early.
- **OS lifecycle changes.** Android deprecates and changes APIs
  aggressively (notification listener access, FGS types, target-SDK
  bumps). Sideload avoids Play Store deadlines but the OS itself will
  still break things every couple of years.

## Next steps

In rough order:

1. **Server-side auth changes** — per-device tokens, pairing flow,
   loopback-vs-network split. Blocker for everything else. *(Next doc.)*
2. **Server-side input batch endpoint** — `POST /api/inputs/batch`,
   `/api/outputs/pending?since=`, `/api/system/devices` UI.
3. **Bootstrap the .NET MAUI project** with just the outbox, uploader,
   pairing flow, and one collector (`phone.battery` — easiest, no
   special permissions).
4. **Add collectors incrementally**, ordered by signal value:
   foreground-app → notifications → location → sensors → screen capture.
5. **Windows client** parallel to the Android collector work, since most
   of the shared code is exercised by either.
6. **Health Connect collector** as the WearOS proxy.
7. **FCM push transport** when SSE proves expensive on cellular.

Defer Kotlin watch app until specifically motivated.
