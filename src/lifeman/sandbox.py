"""Bubblewrap sandbox runner for tool execution."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from lifeman.config import settings
from lifeman.tool_socket import SANDBOX_RUNTIME_PATH, SANDBOX_SOCKET_PATH

log = logging.getLogger("lifeman.sandbox")


def _runtime_dir() -> Path:
    """Directory containing the sandbox-side `lifeman_tool.py` helper."""
    return Path(__file__).parent / "tool_runtime"


# Sandbox-side uid: nobody. Inside the user namespace, the tool sees this uid;
# outside it maps back to the calling user. Combined with --cap-drop ALL and
# --new-session this gives a real "unprivileged user" rather than running as
# the calling user inside the namespace.
_SANDBOX_UID = 65534
_SANDBOX_GID = 65534


_seccomp_warning_logged = False


def _classify_network(network_hosts: list[str] | None) -> tuple[str | None, list[str]]:
    """Split a `manifest.network` list into (mode, host_allowlist).

    `@unrestricted` and `@local` are mode tokens; everything else is a host
    allowlist entry. The strongest mode wins (`@unrestricted` > `@local`).
    """
    if not network_hosts:
        return None, []
    mode: str | None = None
    hosts: list[str] = []
    for entry in network_hosts:
        if entry == "@unrestricted":
            mode = "unrestricted"
        elif entry == "@local" and mode != "unrestricted":
            mode = "local_only"
        elif entry and not entry.startswith("@"):
            hosts.append(entry)
    return mode, hosts


def _build_seccomp_filter() -> bytes | None:
    """Return a serialised BPF seccomp filter, or None if unavailable.

    Uses libseccomp's Python bindings (`seccomp` or `pyseccomp`) when
    installed. Allows a generous baseline and denies a small set of
    historically-dangerous syscalls (mount/keyctl/etc.). The filter is
    intentionally permissive — it's a defence-in-depth backstop on top of
    namespace isolation, not the primary access-control layer.
    """
    global _seccomp_warning_logged
    try:
        import seccomp as _seccomp  # type: ignore[import-not-found]
    except ImportError:
        try:
            import pyseccomp as _seccomp  # type: ignore[import-not-found]
        except ImportError:
            if not _seccomp_warning_logged:
                log.warning(
                    "sandbox: neither 'seccomp' nor 'pyseccomp' is installed; "
                    "no seccomp filter will be applied. The bubblewrap namespace "
                    "isolation still applies; install libseccomp Python bindings "
                    "for defence-in-depth."
                )
                _seccomp_warning_logged = True
            return None

    # Allow everything by default, then deny syscalls a sandboxed personal
    # tool has no business issuing. This is conservative — broaden the
    # denylist as concrete attack scenarios are identified.
    f = _seccomp.SyscallFilter(_seccomp.ALLOW)
    deny_syscalls = (
        "mount", "umount", "umount2", "pivot_root", "chroot",
        "init_module", "finit_module", "delete_module",
        "kexec_load", "kexec_file_load",
        "reboot", "swapon", "swapoff",
        "ptrace", "process_vm_readv", "process_vm_writev",
        "keyctl", "add_key", "request_key",
        "bpf",
        "perf_event_open",
        "userfaultfd",
    )
    for name in deny_syscalls:
        try:
            f.add_rule(_seccomp.ERRNO(1), name)  # 1 == EPERM
        except (ValueError, OSError):
            # Syscall name unknown to this libseccomp build — skip it.
            continue

    # Export to BPF bytes via a temp file (avoids pipe-buffer blocking and
    # works regardless of how libseccomp writes — a few syscalls or one big
    # write).
    with tempfile.NamedTemporaryFile() as tf:
        f.export_bpf(tf.fileno())
        tf.flush()
        tf.seek(0)
        return tf.read()


async def run_tool(
    tool_dir: Path,
    args: dict,
    timeout: float = 30.0,
    socket_path: str | None = None,
    network_hosts: list[str] | None = None,
    fire_id: str | None = None,
) -> dict:
    """Run a tool in a bubblewrap sandbox (or directly if sandbox disabled).

    `socket_path` (when set) is a Unix-domain-socket the tool can use to call
    back into core via the `lifeman_tool` helper. The caller — usually
    `_execute_tool` — owns the socket lifecycle.

    `network_hosts` — if non-empty, give the tool access to the host's
    network namespace. Special tokens `@unrestricted` and `@local` set
    `LIFEMAN_NETWORK_MODE` (the latter is advisory until an egress proxy
    is wired in); other entries are exposed via `LIFEMAN_NETWORK_HOSTS`
    so responsible tools can self-restrict. Empty/None = no network.
    """
    input_json = json.dumps(args)
    network_hosts = network_hosts or []

    if not settings.sandbox_enabled or not shutil.which(settings.bwrap_path):
        return await _run_direct(tool_dir, input_json, timeout, socket_path, network_hosts, fire_id)

    return await _run_sandboxed(tool_dir, input_json, timeout, socket_path, network_hosts, fire_id)


async def _run_direct(
    tool_dir: Path,
    input_json: str,
    timeout: float,
    socket_path: str | None,
    network_hosts: list[str] | None = None,
    fire_id: str | None = None,
) -> dict:
    """Run tool directly without sandbox (development mode)."""
    env = os.environ.copy()
    runtime = _runtime_dir()
    env["PYTHONPATH"] = f"{runtime}:{env.get('PYTHONPATH', '')}".rstrip(":")
    if socket_path:
        env["LIFEMAN_TOOL_SOCKET"] = socket_path
    mode, hosts = _classify_network(network_hosts)
    if mode:
        env["LIFEMAN_NETWORK_MODE"] = mode
    if hosts:
        env["LIFEMAN_NETWORK_HOSTS"] = ",".join(hosts)
    if fire_id:
        env["LIFEMAN_FIRE_ID"] = fire_id
    proc = await asyncio.create_subprocess_exec(
        "python3", str(tool_dir / "run.py"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(tool_dir),
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=input_json.encode()),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"error": f"Tool execution timed out after {timeout}s"}

    if proc.returncode != 0:
        return {"error": f"Tool exited with code {proc.returncode}", "stderr": stderr.decode()[:2000]}

    try:
        return json.loads(stdout.decode())
    except json.JSONDecodeError:
        return {"output": stdout.decode()[:4000]}


async def _run_sandboxed(
    tool_dir: Path,
    input_json: str,
    timeout: float,
    socket_path: str | None,
    network_hosts: list[str] | None = None,
    fire_id: str | None = None,
) -> dict:
    """Run tool inside bubblewrap sandbox."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # If libseccomp is available, materialise the filter and pass it via a
        # dedicated FD that bwrap inherits (--seccomp <FD>). The fd is opened
        # inheritable; bwrap reads + closes it before exec.
        seccomp_bpf = _build_seccomp_filter()
        seccomp_fd = -1
        pass_fds: tuple[int, ...] = ()
        if seccomp_bpf:
            r, w = os.pipe()
            os.write(w, seccomp_bpf)
            os.close(w)
            os.set_inheritable(r, True)
            seccomp_fd = r
            pass_fds = (r,)

        try:
            cmd = _build_bwrap_cmd(
                tool_dir, Path(tmpdir), socket_path, seccomp_fd,
                network_hosts=network_hosts,
                fire_id=fire_id,
            )
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                pass_fds=pass_fds,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=input_json.encode()),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return {"error": f"Tool execution timed out after {timeout}s"}
        finally:
            if seccomp_fd >= 0:
                try:
                    os.close(seccomp_fd)
                except OSError:
                    pass

        if proc.returncode != 0:
            return {"error": f"Tool exited with code {proc.returncode}", "stderr": stderr.decode()[:2000]}

        try:
            return json.loads(stdout.decode())
        except json.JSONDecodeError:
            return {"output": stdout.decode()[:4000]}


def _build_bwrap_cmd(
    tool_dir: Path,
    scratch_dir: Path,
    socket_path: str | None,
    seccomp_fd: int = -1,
    *,
    network_hosts: list[str] | None = None,
    fire_id: str | None = None,
) -> list[str]:
    """Build the bubblewrap command with layered isolation."""
    bwrap = settings.bwrap_path
    runtime = _runtime_dir()

    # Bind paths first so reordering or extending the binds list later doesn't
    # silently break the command's structure. Each bind is conditional on the
    # source existing — this keeps the sandbox usable on systems with a
    # different filesystem layout (NixOS, Alpine, distroless containers) where
    # /bin or /lib may not exist as standalone trees.
    candidate_binds: list[tuple[str, str, str]] = [
        ("--ro-bind", "/usr", "/usr"),
        ("--ro-bind", "/bin", "/bin"),
        ("--ro-bind", "/lib", "/lib"),
        ("--ro-bind", "/lib64", "/lib64"),
        ("--ro-bind", "/etc/alternatives", "/etc/alternatives"),
        ("--ro-bind", "/nix", "/nix"),  # NixOS systems
    ]
    binds: list[tuple[str, str, str]] = [
        b for b in candidate_binds if Path(b[1]).exists()
    ]
    binds += [
        ("--ro-bind", str(tool_dir), "/tool"),
        ("--bind", str(scratch_dir), "/scratch"),
        ("--ro-bind", str(runtime), SANDBOX_RUNTIME_PATH),
    ]

    # Bind a non-standard Python prefix (e.g. a venv or pyenv install) so
    # `python3` inside the sandbox finds its stdlib. Skip when the prefix is
    # already covered by /usr or /.
    python_path = shutil.which("python3")
    if python_path:
        prefix = str(Path(python_path).resolve().parent.parent)
        if prefix not in ("/usr", "/") and Path(prefix).exists():
            binds.append(("--ro-bind", prefix, prefix))

    network_hosts = network_hosts or []

    cmd: list[str] = [
        bwrap,
        # Namespace isolation
        "--unshare-all",
        "--die-with-parent",
    ]
    # Per-tool network policy: if the manifest declares hosts, retain the
    # host's network namespace so the tool can reach the network. Empty
    # declaration → keep the network namespace unshared and the tool gets
    # nothing. Allowlist enforcement at the syscall level is future work
    # (egress proxy); the LIFEMAN_NETWORK_HOSTS env var below carries the
    # declared list to the tool so a responsible tool author can self-restrict.
    if network_hosts:
        cmd.append("--share-net")
    cmd += [
        # Detach from controlling terminal — defends against TIOCSTI input
        # injection back to the user's shell.
        "--new-session",
        # Drop every Linux capability inside the namespace. Belt-and-suspenders
        # alongside the userns drop below: even if some host binary happened to
        # be setuid, it can't exercise privileged operations.
        "--cap-drop", "ALL",
        # Inside the unshared user namespace, the calling user is initially
        # mapped to uid 0. Drop to nobody so the tool sees an unprivileged uid
        # — fulfils the design's "process under unprivileged user" promise
        # without requiring a real OS-level account.
        "--uid", str(_SANDBOX_UID),
        "--gid", str(_SANDBOX_GID),
    ]
    for flag, src, dst in binds:
        cmd += [flag, src, dst]

    # /lib64 is sometimes a symlink to usr/lib64 on Debian-shaped systems and
    # sometimes a real directory on others (e.g. RHEL). If the bind above
    # didn't cover it, fall back to the symlink shape.
    if not Path("/lib64").is_dir():
        cmd += ["--symlink", "usr/lib64", "/lib64"]

    cmd += [
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--chdir", "/tool",
        # Python path includes the helper dir so tools can `import lifeman_tool`.
        "--setenv", "PYTHONPATH", f"/tool:{SANDBOX_RUNTIME_PATH}",
        "--setenv", "HOME", "/tmp",
        "--setenv", "LIFEMAN_SCRATCH", "/scratch",
    ]
    if socket_path:
        # Unix sockets work across mount/network namespaces, so a plain bind
        # mount is enough for the tool to reach the core via lifeman_tool.
        cmd += [
            "--bind", socket_path, SANDBOX_SOCKET_PATH,
            "--setenv", "LIFEMAN_TOOL_SOCKET", SANDBOX_SOCKET_PATH,
        ]
    if network_hosts:
        # Bind the host's resolver config so DNS works inside the sandbox.
        # This is only meaningful when --share-net is set above; without it,
        # there's no network namespace to resolve in.
        if Path("/etc/resolv.conf").exists():
            cmd += ["--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf"]
        if Path("/etc/hosts").exists():
            cmd += ["--ro-bind", "/etc/hosts", "/etc/hosts"]
        # Expose the system CA bundle so TLS verification works. Python's
        # ssl module reads from a small set of well-known paths (varying by
        # distro) plus `SSL_CERT_FILE` / `SSL_CERT_DIR` env vars. Without
        # any of these visible, urllib raises CERTIFICATE_VERIFY_FAILED for
        # every HTTPS request — even though `--share-net` made the network
        # reachable. We bind the directories that hold the certs (not just
        # the symlinked .crt) because most Debian-shaped systems use a
        # symlink chain that points into /etc/ssl/certs and /usr/share/...
        # /usr is already bound above, so binding /etc/ssl + /etc/pki
        # covers the remaining roots.
        for ca_path in ("/etc/ssl", "/etc/pki", "/etc/ca-certificates"):
            if Path(ca_path).exists():
                cmd += ["--ro-bind", ca_path, ca_path]
        mode, hosts = _classify_network(network_hosts)
        if mode:
            cmd += ["--setenv", "LIFEMAN_NETWORK_MODE", mode]
        if hosts:
            cmd += ["--setenv", "LIFEMAN_NETWORK_HOSTS", ",".join(hosts)]
    if fire_id:
        cmd += ["--setenv", "LIFEMAN_FIRE_ID", fire_id]
    if seccomp_fd >= 0:
        cmd += ["--seccomp", str(seccomp_fd)]
    cmd += ["--", "python3", "/tool/run.py"]
    return cmd
