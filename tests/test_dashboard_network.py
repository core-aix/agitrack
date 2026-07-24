"""Where the dashboard/backtrace web server binds, what URL it advertises, and how it hands
out ports — the remote-terminal story.

On the user's own machine the server stays on loopback. In a remote shell (SSH/Mosh)
loopback is reachable only from the remote box, so it binds every interface and advertises
the remote's own address; when a firewall blocks that, the printed message must be a
copy-pasteable `ssh -L` command. Ports are handed out consecutively so a second instance
lands on a neighbouring, predictable URL instead of a random ephemeral one.
"""

import socket

import pytest

from agitrack.metrics.server import (
    ALL_INTERFACES,
    BIND_HOST_ENV,
    DEFAULT_HOST,
    advertised_host,
    bind_scanning,
    dashboard_url,
    default_bind_host,
    exposure_note,
    is_remote_session,
    remote_access_help,
    remote_browser_hint,
    ssh_forward_command,
    ssh_target,
)

_SSH_VARS = ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Neither remote nor overridden by default — the suite itself may be running over SSH,
    so every test states the environment it means to exercise."""
    for var in (*_SSH_VARS, BIND_HOST_ENV):
        monkeypatch.delenv(var, raising=False)


def _remote(monkeypatch, server_ip="10.1.2.3"):
    monkeypatch.setenv("SSH_CONNECTION", f"192.168.0.9 51000 {server_ip} 22")


# --- choosing the bind address -------------------------------------------------


def test_local_shell_binds_loopback_only():
    assert is_remote_session() is False
    assert default_bind_host() == DEFAULT_HOST


@pytest.mark.parametrize("var", _SSH_VARS)
def test_any_ssh_marker_makes_the_session_remote(monkeypatch, var):
    monkeypatch.setenv(var, "1.2.3.4 5 6.7.8.9 22" if var != "SSH_TTY" else "/dev/pts/0")
    assert is_remote_session() is True
    assert default_bind_host() == ALL_INTERFACES


def test_host_env_overrides_both_directions(monkeypatch):
    # Opting a remote session back out of network exposure...
    _remote(monkeypatch)
    monkeypatch.setenv(BIND_HOST_ENV, "127.0.0.1")
    assert default_bind_host() == "127.0.0.1"
    # ...and pinning a single NIC rather than the wildcard.
    monkeypatch.setenv(BIND_HOST_ENV, "10.0.0.5")
    assert default_bind_host() == "10.0.0.5"


def test_headless_local_box_is_not_treated_as_remote(monkeypatch):
    # No DISPLAY means a browser can't be opened here, but it does NOT mean the user is
    # elsewhere — putting the dashboard on the network for a console user is not implied.
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert default_bind_host() == DEFAULT_HOST


# --- advertising an address the user can actually type -------------------------


def test_wildcard_bind_advertises_the_ip_the_ssh_client_reached(monkeypatch):
    _remote(monkeypatch, server_ip="10.4.5.6")
    assert advertised_host(ALL_INTERFACES) == "10.4.5.6"
    assert dashboard_url(ALL_INTERFACES, 8765) == "http://10.4.5.6:8765/"


def test_concrete_bind_host_is_advertised_verbatim(monkeypatch):
    _remote(monkeypatch, server_ip="10.4.5.6")  # ignored: the bind address is already specific
    assert advertised_host("127.0.0.1") == "127.0.0.1"
    assert dashboard_url("127.0.0.1", 9000) == "http://127.0.0.1:9000/"


def test_wildcard_bind_without_ssh_info_still_yields_a_usable_host(monkeypatch):
    # Wildcard bind but no SSH_CONNECTION (e.g. an explicit AGITRACK_DASHBOARD_HOST=0.0.0.0):
    # fall back to this host's own address, never the meaningless "0.0.0.0".
    monkeypatch.setenv(BIND_HOST_ENV, ALL_INTERFACES)
    host = advertised_host(ALL_INTERFACES)
    assert host not in ("0.0.0.0", "::", "*", "")


def test_ipv6_wildcard_is_also_resolved(monkeypatch):
    _remote(monkeypatch, server_ip="10.9.9.9")
    assert advertised_host("::") == "10.9.9.9"


# --- the copy-paste SSH forwarding message -------------------------------------


def test_ssh_forward_command_is_complete_and_copy_pasteable(monkeypatch):
    _remote(monkeypatch, server_ip="10.4.5.6")
    command = ssh_forward_command(8765)
    # Every part filled in: no <placeholders> left for the user to work out.
    assert "<" not in command
    assert command.endswith("@10.4.5.6")
    assert "-L 8765:localhost:8765" in command
    assert command.startswith("ssh -N ")
    assert ssh_target().endswith("@10.4.5.6")


def test_remote_access_help_gives_both_routes_verbatim(monkeypatch):
    _remote(monkeypatch, server_ip="10.4.5.6")
    text = remote_access_help("http://10.4.5.6:8765/", 8765)
    # Route 1: the direct URL, for when the firewall allows it.
    assert "http://10.4.5.6:8765/" in text
    # Route 2: the exact command to run, said to belong on the USER's machine, plus the
    # localhost URL that command creates — nothing left to figure out.
    assert ssh_forward_command(8765) in text
    assert "ON YOUR OWN MACHINE" in text
    assert "http://localhost:8765/" in text


def test_remote_access_help_names_the_actual_bound_port(monkeypatch):
    # A second instance on 8766 must forward 8766 — a hardcoded 8765 would silently
    # tunnel the wrong dashboard.
    _remote(monkeypatch)
    text = remote_access_help("http://10.1.2.3:8766/", 8766)
    assert "-L 8766:localhost:8766" in text
    assert "http://localhost:8766/" in text
    assert "8765" not in text


def test_loopback_bind_is_told_to_tunnel_and_offered_no_dead_link(monkeypatch):
    # Loopback-bound (a headless local box, or AGITRACK_DASHBOARD_HOST=127.0.0.1 over SSH):
    # the tunnel is the ONLY way in, so offering the 127.0.0.1 URL as something to open
    # "from your own machine" would just send the user to a page that cannot load.
    _remote(monkeypatch)
    text = remote_access_help("http://127.0.0.1:8765/", 8765, bind_host="127.0.0.1")
    assert "loopback" in text
    assert "firewall" not in text
    assert ssh_forward_command(8765) in text
    assert "http://localhost:8765/" in text


def test_bind_host_is_inferred_from_the_url_when_not_recorded(monkeypatch):
    # Handshakes written by an older daemon carry no "host"; the advertised URL still says
    # which case it is.
    _remote(monkeypatch)
    assert "loopback" in remote_access_help("http://127.0.0.1:8765/", 8765)
    assert "loopback" not in remote_access_help("http://10.1.2.3:8765/", 8765)


def test_one_line_hint_still_carries_the_forward_command(monkeypatch):
    # The TUI popup variant is compact but must not drop the actionable part.
    _remote(monkeypatch)
    hint = remote_browser_hint("http://10.1.2.3:8765/", 8765)
    assert "\n" not in hint
    assert ssh_forward_command(8765) in hint


def test_exposure_note_only_warns_when_actually_exposed():
    assert exposure_note("127.0.0.1") == ""
    note = exposure_note(ALL_INTERFACES)
    assert "all network interfaces" in note
    assert BIND_HOST_ENV in note  # and how to turn it off
    assert note.endswith("\n")


# --- consecutive port allocation -----------------------------------------------


class _Listener:
    """A bare TCP listener standing in for the dashboard server."""

    def __init__(self, address):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(address)
        self.sock.listen(1)
        self.server_address = self.sock.getsockname()

    def close(self):
        self.sock.close()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_ports_are_allocated_consecutively_for_multiple_instances():
    # The reported annoyance: instance 1 got 8765 and every later one got a random
    # ephemeral port. They must march 8765, 8766, 8767 instead.
    base = _free_port()
    servers = []
    try:
        for expected in range(base, base + 3):
            server = bind_scanning(_Listener, "127.0.0.1", base)
            servers.append(server)
            assert server.server_address[1] == expected
    finally:
        for server in servers:
            server.close()


def test_scan_skips_a_port_held_by_something_else():
    base = _free_port()
    blocker = _Listener(("127.0.0.1", base))
    try:
        server = bind_scanning(_Listener, "127.0.0.1", base)
        try:
            assert server.server_address[1] == base + 1
        finally:
            server.close()
    finally:
        blocker.close()


def test_port_zero_still_means_let_the_os_choose():
    server = bind_scanning(_Listener, "127.0.0.1", 0)
    try:
        assert server.server_address[1] > 0
    finally:
        server.close()


def test_exhausted_span_falls_back_to_an_ephemeral_port():
    # Better a working dashboard on an odd port than no dashboard at all.
    base = _free_port()
    blocker = _Listener(("127.0.0.1", base))
    try:
        server = bind_scanning(_Listener, "127.0.0.1", base, span=1)  # only the taken port tried
        try:
            assert server.server_address[1] not in (0, base)
        finally:
            server.close()
    finally:
        blocker.close()


def test_scan_never_runs_off_the_end_of_the_port_range():
    # A preferred port near 65535 must not produce out-of-range candidates.
    server = bind_scanning(_Listener, "127.0.0.1", 65535, span=64)
    try:
        assert 0 < server.server_address[1] <= 65535
    finally:
        server.close()
