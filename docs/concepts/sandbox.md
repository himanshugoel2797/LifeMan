# Sandbox

Every tool invocation runs in a bubblewrap subprocess (or, if bwrap
isn't installed, falls back to a direct subprocess — see
[REVIEW.md](../REVIEW.md) for the security note on that fallback).
The sandbox isolates filesystem, network, IPC, PID, and user
namespaces, and applies a seccomp denylist if `libseccomp`'s Python
bindings are present.

Everything below is in [sandbox.py](src/lifeman/sandbox.py).

## Layers, in order of importance

1. **Mount namespace** — `--unshare-all`, then explicit `--ro-bind`
   for the tool's directory and the runtime helpers. The tool sees a
   minimal filesystem view; the rest of the host is gone.
2. **User namespace** — the tool runs as uid `65534` (`nobody`) inside
   the namespace. `--cap-drop ALL` strips capabilities. Outside, the
   namespace maps back to the calling user.
3. **Network namespace** — by default unshared (no network at all). If
   the manifest's `network` list is non-empty, the sandbox keeps the
   host network and exposes the allowlist to the tool via
   `LIFEMAN_NETWORK_HOSTS`. The tool is expected to self-restrict;
   syscall-level egress proxying is future work.
4. **PID, IPC, UTS namespaces** — unshared. The tool can't see other
   processes, post sysv IPC messages, or change hostname.
5. **`/dev`** — minimal. `/dev/null`, `/dev/urandom` only.
6. **`/proc`, `/sys`** — not mounted.
7. **Tmpfs scratch** — `/tmp` is a tmpfs inside the sandbox.
8. **Seccomp** — denylist of historically-dangerous syscalls
   (`mount`, `umount`, `pivot_root`, `init_module`, `delete_module`,
   `kexec_*`, `reboot`, `swap*`, `ptrace`, `process_vm_*`, `keyctl`,
   `bpf`, `perf_event_open`, `userfaultfd`, `add_key`,
   `request_key`). Anything not on that list is allowed. Defence in
   depth, not the primary boundary.

## Runtime socket

A Unix socket lives in a per-invocation tmpdir on the host. The
sandbox bind-mounts that socket inside the namespace at
`SANDBOX_SOCKET_PATH` (a fixed location under `/lifeman-runtime/`)
so the tool can find it without configuration.

Unix sockets work across mount and network namespaces, so this is the
only intentional way data crosses the namespace boundary. Everything
the tool does that touches state goes over that socket and is subject
to the runtime's capability checks.

## Stdio contract

Tools read JSON args from stdin and write a JSON return on stdout.
`run_tool` parses stdout. Anything on stderr is logged but doesn't
appear in the result. Timeouts are configurable per invocation via
`compute_limits.timeout` in the manifest; default is 30 seconds.

## Disabling the sandbox

`LIFEMAN_SANDBOX_ENABLED=false`, or running on a host without bwrap
installed, falls back to a direct `python3 run.py` subprocess in the
tool's directory. **No isolation.** Useful for local dev only.

## Why not just allowlist syscalls?

A tight syscall allowlist would break Python's startup before the
tool's code ever runs. Python imports a lot. The current design
treats namespace isolation as the primary gate (which it is —
mount/network/user namespaces are kernel-enforced), with seccomp as
defence in depth against specific known-bad calls.
