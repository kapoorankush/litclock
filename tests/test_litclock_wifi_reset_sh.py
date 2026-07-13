"""Tests for scripts/litclock-wifi-reset.sh (#245 M5 D11/D12).

Drives the helper as a subprocess with stubbed `nmcli` + `systemctl`
binaries via env overrides. Verifies the locked sequence:

    1. systemctl stop litclock-control.service
    2. nmcli -t -f UUID,TYPE connection show → iterate wifi UUIDs
    3. nmcli connection delete <uuid> for each
    4. rm /etc/litclock/.setup-complete
    5. systemctl restart litclock-firstboot.service

D12 lock: ALL wifi profiles wiped (not just active) so a secondary
saved network can't auto-reconnect and skip firstboot.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "litclock-wifi-reset.sh"


@pytest.fixture
def harness(tmp_path):
    """Build env with nmcli + systemctl stubs that record every invocation.

    Each stub appends its argv (one line per call) to a log file we can
    assert against.
    """
    setup_complete = tmp_path / ".setup-complete"
    setup_complete.write_text("")
    # EPIC #383 PR2 (#388): wifi-reset must clear the handoff marker too.
    handoff_complete = tmp_path / ".handoff-complete"
    handoff_complete.write_text("")

    # nmcli stub. Behavior:
    #   `nmcli -t -f UUID,TYPE connection show` → emit two wifi rows + 1 ethernet
    #   `nmcli connection delete <uuid>` → log + exit 0
    nmcli_log = tmp_path / "nmcli-calls.log"
    nmcli_stub = tmp_path / "nmcli-stub.sh"
    nmcli_stub.write_text(
        f"""#!/bin/bash
echo "$@" >> "{nmcli_log}"
# Stub the connection-show output. Match the script's exact arg shape.
if [ "$1" = "-t" ] && [ "$2" = "-f" ] && [ "$3" = "UUID,TYPE" ] && [ "$4" = "connection" ] && [ "$5" = "show" ]; then
    echo "uuid-wifi-1:802-11-wireless"
    echo "uuid-wifi-2:802-11-wireless"
    echo "uuid-eth-1:802-3-ethernet"
    echo "uuid-hotspot:802-11-wireless"
    exit 0
fi
exit 0
"""
    )
    nmcli_stub.chmod(0o755)

    # systemctl stub.
    systemctl_log = tmp_path / "systemctl-calls.log"
    systemctl_stub = tmp_path / "systemctl-stub.sh"
    systemctl_stub.write_text(
        f"""#!/bin/bash
echo "$@" >> "{systemctl_log}"
exit 0
"""
    )
    systemctl_stub.chmod(0o755)

    env = {
        **os.environ,
        "LITCLOCK_NMCLI": str(nmcli_stub),
        "LITCLOCK_SYSTEMCTL": str(systemctl_stub),
        "LITCLOCK_SETUP_COMPLETE_FILE": str(setup_complete),
        "LITCLOCK_HANDOFF_COMPLETE_FILE": str(handoff_complete),
    }
    return {
        "tmp": tmp_path,
        "setup_complete": setup_complete,
        "handoff_complete": handoff_complete,
        "nmcli_log": nmcli_log,
        "systemctl_log": systemctl_log,
        "env": env,
    }


def _run(env):
    return subprocess.run(
        [str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestHappyPath:
    def test_stops_litclock_control_first(self, harness):
        result = _run(harness["env"])
        assert result.returncode == 0, result.stderr
        log = harness["systemctl_log"].read_text().splitlines()
        # First systemctl call is `stop litclock-control.service`.
        assert log[0] == "stop litclock-control.service"

    def test_wipes_all_wifi_uuids_by_uuid(self, harness):
        """D12 — iterate wifi-type UUIDs and delete each. Hotspot
        connection (also 802-11-wireless type) is wiped too; firstboot
        recreates it cleanly."""
        result = _run(harness["env"])
        assert result.returncode == 0
        log = harness["nmcli_log"].read_text().splitlines()
        # Should have one connection-show call + 3 delete calls
        # (uuid-wifi-1, uuid-wifi-2, uuid-hotspot — not uuid-eth-1).
        delete_calls = [line for line in log if line.startswith("connection delete")]
        assert "connection delete uuid-wifi-1" in delete_calls
        assert "connection delete uuid-wifi-2" in delete_calls
        assert "connection delete uuid-hotspot" in delete_calls
        # The ethernet UUID must NOT be deleted.
        assert "connection delete uuid-eth-1" not in delete_calls

    def test_removes_setup_complete(self, harness):
        result = _run(harness["env"])
        assert result.returncode == 0
        assert not harness["setup_complete"].exists(), "setup-complete must be removed so firstboot.service re-fires"

    def test_removes_handoff_complete(self, harness):
        """EPIC #383 PR2 (#388): a WiFi change can mean a new timezone, so the
        handoff (IP-geo re-resolve + browser-tz fallback) must re-run on
        re-provision. A lingering .handoff-complete would skip it and risk a
        wrong-time clock."""
        result = _run(harness["env"])
        assert result.returncode == 0
        assert not harness["handoff_complete"].exists(), "handoff-complete must be cleared so the handoff re-runs"

    def test_restarts_firstboot_service_last(self, harness):
        result = _run(harness["env"])
        assert result.returncode == 0
        log = harness["systemctl_log"].read_text().splitlines()
        # Last systemctl call is `restart --no-block litclock-firstboot.service`.
        # --no-block is critical — without it `systemctl restart` blocks until
        # firstboot's full AP-mode + captive-portal + credential flow completes
        # (minutes), and wifi-reset.service's TimeoutStartSec=60 SIGTERMs the
        # helper before it can return.
        assert log[-1] == "restart --no-block litclock-firstboot.service"

    def test_missing_nmcli_aborts_before_state_mutation(self, harness, tmp_path):
        """/review finding: if nmcli is missing or broken, the helper must
        fail-fast BEFORE removing .setup-complete + restarting firstboot.
        Otherwise NetworkManager's saved profiles stay on disk and the
        Pi appears stuck after Reset-WiFi (no hotspot, no LAN drop)."""
        env = harness["env"].copy()
        env["LITCLOCK_NMCLI"] = str(tmp_path / "does-not-exist")
        result = subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 1, "must exit non-zero when nmcli missing"
        # State mutations must NOT have happened.
        assert harness["setup_complete"].exists(), "nmcli missing → .setup-complete must remain (no state mutation)"
        # No firstboot restart should have been attempted.
        log = harness["systemctl_log"].read_text() if harness["systemctl_log"].exists() else ""
        assert "restart litclock-firstboot" not in log

    def test_no_active_wifi_connections_completes_cleanly(self, harness, tmp_path):
        """If nmcli has no wifi connections at all, the script must
        still continue through to setup-complete removal + firstboot
        restart."""
        # Replace the stub with one that returns only ethernet rows.
        nmcli_stub = tmp_path / "nmcli-stub.sh"
        nmcli_stub.write_text(
            f"""#!/bin/bash
echo "$@" >> "{harness["nmcli_log"]}"
if [ "$5" = "show" ]; then
    echo "uuid-eth-1:802-3-ethernet"
fi
exit 0
"""
        )
        nmcli_stub.chmod(0o755)
        env = harness["env"].copy()
        env["LITCLOCK_NMCLI"] = str(nmcli_stub)
        result = subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0
        assert not harness["setup_complete"].exists()


class TestSourceInvariants:
    """Static checks — independent of nmcli/systemctl availability."""

    def test_filters_by_wifi_type(self):
        """D12 — must filter `nmcli connection show` by TYPE=802-11-wireless,
        NOT just blindly delete everything."""
        source = SCRIPT.read_text()
        assert "802-11-wireless" in source

    def test_uses_uuid_not_ssid(self):
        """F12 — delete by UUID. nmcli connection ID is not guaranteed
        to equal SSID; uniquely identifying by UUID avoids ambiguity."""
        source = SCRIPT.read_text()
        # The iteration must read the UUID column.
        assert "UUID,TYPE" in source
        # And the delete invocation references the bound `uuid` variable.
        assert 'connection delete "$uuid"' in source

    def test_stops_control_service_before_wifi_wipe(self):
        """Ordering invariant — control_server must stop BEFORE we drop
        the LAN, otherwise it lingers as a zombie process bound to the
        dead interface."""
        source = SCRIPT.read_text()
        stop_idx = source.find("stop litclock-control.service")
        nmcli_idx = source.find("connection delete")
        assert stop_idx > 0 and nmcli_idx > 0
        assert stop_idx < nmcli_idx

    def test_restart_firstboot_after_setup_complete_removal(self):
        """If we restart firstboot BEFORE removing .setup-complete, the
        unit's ConditionPathExists condition trips and it does nothing."""
        source = SCRIPT.read_text()
        rm_idx = source.find("rm -f")
        restart_idx = source.find("restart litclock-firstboot")
        assert rm_idx > 0 and restart_idx > 0
        assert rm_idx < restart_idx
