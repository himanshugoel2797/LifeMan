"""Tests for the manifest.network → sandbox flag wiring.

Doesn't shell out to bwrap (that requires user namespaces and a Linux
kernel feature set we can't assume in CI). Instead exercises
`_build_bwrap_cmd` and the `lifeman_tool.network_allowed` helper to
confirm the policy plumbing is intact.
"""

from __future__ import annotations

import os
from pathlib import Path

from lifeman.sandbox import _build_bwrap_cmd


def test_no_network_omits_share_net(tmp_path):
    cmd = _build_bwrap_cmd(tmp_path, tmp_path, None)
    assert "--share-net" not in cmd
    assert not any(c == "LIFEMAN_NETWORK_HOSTS" for c in cmd)


def test_declared_network_adds_share_net_and_env(tmp_path):
    cmd = _build_bwrap_cmd(
        tmp_path, tmp_path, None,
        network_hosts=["api.example.com", "*.cdn.example"],
    )
    assert "--share-net" in cmd
    # The env var follows immediately after `--setenv LIFEMAN_NETWORK_HOSTS`.
    idx = cmd.index("LIFEMAN_NETWORK_HOSTS")
    assert cmd[idx + 1] == "api.example.com,*.cdn.example"


def test_empty_list_treated_as_no_network(tmp_path):
    cmd = _build_bwrap_cmd(tmp_path, tmp_path, None, network_hosts=[])
    assert "--share-net" not in cmd


def test_networked_sandbox_binds_system_ca_paths(tmp_path):
    """Networked tools must see the host CA store; otherwise every TLS
    handshake fails with `CERTIFICATE_VERIFY_FAILED`. We bind the standard
    distro paths (/etc/ssl on Debian-shaped, /etc/pki on RHEL-shaped) when
    `--share-net` is on. Each is conditional on existence so the test is
    accurate against whichever distro it runs on."""
    cmd = _build_bwrap_cmd(
        tmp_path, tmp_path, None, network_hosts=["api.example.com"],
    )
    # At least one of these should be bound on any real system that has
    # certificate authorities installed; assert that any path that exists
    # on the host is in the command.
    for ca_path in ("/etc/ssl", "/etc/pki", "/etc/ca-certificates"):
        if Path(ca_path).exists():
            assert ca_path in cmd, f"{ca_path} exists on host but not bound"

    # And nothing CA-related should be bound when network is off.
    no_net = _build_bwrap_cmd(tmp_path, tmp_path, None)
    assert "/etc/ssl" not in no_net
    assert "/etc/pki" not in no_net


def test_network_allowed_helper_against_env(monkeypatch):
    # Import here so the helper picks up the patched env each call.
    from lifeman.tool_runtime.lifeman_tool import network_allowed, network_hosts

    monkeypatch.setenv("LIFEMAN_NETWORK_HOSTS", "api.example.com,db.local")
    assert network_hosts() == ["api.example.com", "db.local"]
    assert network_allowed("api.example.com")
    assert not network_allowed("evil.example.com")

    monkeypatch.setenv("LIFEMAN_NETWORK_HOSTS", "*")
    assert network_allowed("anything.example")

    monkeypatch.delenv("LIFEMAN_NETWORK_HOSTS", raising=False)
    assert not network_allowed("api.example.com")
