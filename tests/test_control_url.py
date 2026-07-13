"""Tests for the single source of truth for the Control PWA port + URL (#343)."""

import importlib
import sys
from pathlib import Path

import pytest

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import control_url  # noqa: E402


def _reload_with_port(monkeypatch, value):
    """Reload control_url with LITCLOCK_CONTROL_PORT set (or unset if None).
    CONTROL_PORT is resolved at import time, so an override needs a reload."""
    if value is None:
        monkeypatch.delenv("LITCLOCK_CONTROL_PORT", raising=False)
    else:
        monkeypatch.setenv("LITCLOCK_CONTROL_PORT", str(value))
    return importlib.reload(control_url)


class TestControlBaseUrl:
    def test_default_port_is_80(self, monkeypatch):
        mod = _reload_with_port(monkeypatch, None)
        assert mod.CONTROL_PORT == 80

    def test_port_80_omits_the_port(self, monkeypatch):
        # The whole point of #343: a user never sees a port to type.
        mod = _reload_with_port(monkeypatch, None)
        assert mod.control_base_url("litclock.local") == "http://litclock.local"
        assert mod.control_base_url("192.168.2.5") == "http://192.168.2.5"
        assert ":" not in mod.control_base_url("192.168.2.5").split("//", 1)[1]

    def test_non_default_port_is_explicit(self, monkeypatch):
        # A dev override / the historical 8443 must render the port so the URL
        # still reaches the server.
        mod = _reload_with_port(monkeypatch, 8443)
        assert mod.CONTROL_PORT == 8443
        assert mod.control_base_url("192.168.2.5") == "http://192.168.2.5:8443"

    def test_plain_http_scheme_always(self, monkeypatch):
        # control_server has no TLS listener (#257) — never https.
        mod = _reload_with_port(monkeypatch, None)
        assert mod.control_base_url("litclock.local").startswith("http://")
        assert "https://" not in mod.control_base_url("litclock.local")

    def test_ipv6_literal_is_bracketed(self, monkeypatch):
        # Defensive (/review): a bare IPv6 host must be bracketed so the port
        # separator isn't ambiguous. Not reachable today (AF_INET only) but this
        # is the central URL builder now.
        mod = _reload_with_port(monkeypatch, 8443)
        assert mod.control_base_url("::1") == "http://[::1]:8443"
        mod = _reload_with_port(monkeypatch, None)
        assert mod.control_base_url("fd00::1") == "http://[fd00::1]"
        # Already-bracketed input is not double-wrapped.
        assert mod.control_base_url("[fd00::1]") == "http://[fd00::1]"

    @pytest.fixture(autouse=True)
    def _restore(self, monkeypatch):
        # Leave the module at its real default for any later importer.
        yield
        monkeypatch.delenv("LITCLOCK_CONTROL_PORT", raising=False)
        importlib.reload(control_url)


class TestNoStaleHardcodedPort:
    """Guard: the control-surface code must route the URL through the shared
    helper, not re-hardcode a port. Catches a refactor silently reintroducing
    `:8443` / `:80` in the QR or handoff builders (#343)."""

    def _read(self, rel):
        return (Path(__file__).resolve().parents[1] / rel).read_text()

    def test_literary_clock_qr_uses_helper(self):
        src = self._read("src/literary_clock.py")
        assert "control_base_url" in src
        assert ':8443"' not in src  # no hardcoded port in the QR path

    def test_handoff_uses_helper(self):
        src = self._read("src/control_server/handoff.py")
        assert "control_base_url" in src
        assert ':{CONTROL_PORT}"' not in src

    def test_status_js_derives_port_from_origin(self):
        src = self._read("src/control_server/static/js/status.js")
        # Derives from window.location.port; no hardcoded MDNS_PORT constant.
        assert "window.location.port" in src
        assert "MDNS_PORT" not in src

    def test_qr_dev_tools_are_not_stale(self):
        # /review Low: the QR-scanability validator + PWA-load audit are dev
        # tools, but they must not validate/probe the old :8443 port.
        assert ":8443" not in self._read("tools/control-pwa/validate_qr_layout.py")
        assert ":8443" not in self._read("scripts/diag-pwa-load-audit.sh")


class TestPort80Deploy:
    """The sysctl drop-in that lets pi bind port 80 must exist, set the right
    knob, and be installed by every deploy path + ordered before the unit
    binds (#343)."""

    _CONF = "sysctl.d/30-litclock-unprivileged-ports.conf"

    def _read(self, rel):
        return (Path(__file__).resolve().parents[1] / rel).read_text()

    def test_sysctl_dropin_sets_port_floor_to_80(self):
        conf = self._read(self._CONF)
        assert "net.ipv4.ip_unprivileged_port_start = 80" in conf

    def test_installed_by_all_three_paths(self):
        # pi-gen (fresh flash), install.sh (manual), update.sh (OTA) must each
        # drop the file into /etc/sysctl.d, or an upgrader can't bind 80.
        name = "30-litclock-unprivileged-ports.conf"
        assert name in self._read("pi-gen/stage3/02-configure-system/00-run.sh")
        assert name in self._read("scripts/install.sh")
        assert name in self._read("scripts/update.sh")

    def test_update_applies_sysctl_live(self):
        # OTA can't wait for a reboot — update.sh must apply the knob before it
        # restarts control_server on the new port.
        up = self._read("scripts/update.sh")
        assert "ip_unprivileged_port_start=80" in up

    def test_unit_orders_after_sysinit(self):
        # systemd-sysctl runs before sysinit.target, so After=sysinit.target
        # guarantees the knob is applied before this unit binds 80.
        unit = self._read("systemd/litclock-control.service")
        assert "After=" in unit and "sysinit.target" in unit
