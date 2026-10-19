"""Tests for scripts/litclock-wifi-reset.sh (litclock-dev#245 M5 D11/D12).

Drives the helper as a subprocess with stubbed `nmcli` + `systemctl`
binaries via env overrides. Verifies the locked sequence:

    0. systemctl enable --no-reload litclock-firstboot.service (refuse on
       failure; litclock-dev#938)
    1. systemctl stop litclock-control.service
    2. rm /etc/litclock/.setup-complete and .handoff-complete (before the
       wipe since the litclock-dev#938 review)
    3. nmcli -t -f UUID,TYPE connection show → iterate wifi UUIDs, then
       nmcli connection delete <uuid> for each
    4. systemctl restart litclock-firstboot.service

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
    # EPIC litclock-dev#383 PR2 (litclock-dev#388): wifi-reset must clear the handoff marker too.
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
        # First systemctl call that changes anything is `stop
        # litclock-control.service`; only the firstboot enable precedes it
        # (litclock-dev#938, which must come before every state change).
        assert log[:2] == ["enable --no-reload litclock-firstboot.service", "stop litclock-control.service"]

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
        """EPIC litclock-dev#383 PR2 (litclock-dev#388): a WiFi change can mean a new timezone, so the
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

    def test_enables_firstboot_before_any_state_change(self, harness):
        """litclock-dev#938: a successful setup disables litclock-firstboot, so
        the reset must re-enable it, or a setup left unfinished (30-min timeout
        -> power-off -> "unplug and plug back in") boots to no hotspot and no
        WiFi. It must come FIRST: a power cut after the WiFi wipe is the same
        brick if the enable has not happened yet."""
        result = _run(harness["env"])
        assert result.returncode == 0
        log = harness["systemctl_log"].read_text().splitlines()
        assert log.index("enable --no-reload litclock-firstboot.service") == 0, log

    def test_markers_go_before_any_wifi_profile_is_deleted(self, harness, tmp_path):
        """litclock-dev#938 review: a power cut AFTER the WiFi wipe but BEFORE
        .setup-complete was removed left an enabled first-boot that saw the
        marker, skipped, and raised no hotspot, with no WiFi left to fall back
        on. With the markers removed first, a cut anywhere after the enable
        boots into provisioning (hotspot, or the pre-connected path on WiFi
        that is still saved). One log shared by both stubs records, for each
        call, whether the marker still existed at that moment."""
        shared = tmp_path / "ordered.log"
        sc = harness["setup_complete"]
        hc = harness["handoff_complete"]
        for name in ("systemctl", "nmcli"):
            stub = tmp_path / f"{name}-ordered.sh"
            show = '[ "$5" = "show" ] && echo "uuid-wifi-1:802-11-wireless"\n' if name == "nmcli" else ""
            stub.write_text(
                f"""#!/bin/bash
echo "{name} $* marker=$([ -e "{sc}" ] && echo y || echo n) handoff=$([ -e "{hc}" ] && echo y || echo n)" >> "{shared}"
{show}exit 0
"""
            )
            stub.chmod(0o755)
        env = harness["env"].copy()
        env["LITCLOCK_SYSTEMCTL"] = str(tmp_path / "systemctl-ordered.sh")
        env["LITCLOCK_NMCLI"] = str(tmp_path / "nmcli-ordered.sh")
        result = subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        lines = shared.read_text().splitlines()
        deletes = [line for line in lines if line.startswith("nmcli connection delete")]
        assert deletes, lines
        # Both markers: a cut after the wipe with .handoff-complete still
        # present would skip the handoff, the wrong-time-clock risk of litclock-dev#388.
        assert all("marker=n handoff=n" in line for line in deletes), lines
        assert lines[0].startswith("systemctl enable --no-reload litclock-firstboot.service"), lines

    def test_setup_complete_rm_failure_aborts_before_wipe(self, harness, tmp_path):
        """litclock-dev#938 review: Step 2 now runs before the WiFi wipe so that
        a marker that cannot be removed stops the reset while the WiFi is still
        saved. Log-and-continue here, or Step 2 moved back below the wipe,
        would bring back "no WiFi, but marked set up"."""
        if os.geteuid() == 0:
            pytest.skip("root ignores directory permissions")
        ro = tmp_path / "ro"
        ro.mkdir()
        marker = ro / ".setup-complete"
        marker.write_text("")
        ro.chmod(0o555)
        env = harness["env"].copy()
        env["LITCLOCK_SETUP_COMPLETE_FILE"] = str(marker)
        try:
            result = subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
        finally:
            ro.chmod(0o755)
        assert result.returncode == 1, result.stdout
        assert marker.exists()
        nmcli = harness["nmcli_log"].read_text() if harness["nmcli_log"].exists() else ""
        assert "connection delete" not in nmcli
        assert not any(line.startswith("restart") for line in harness["systemctl_log"].read_text().splitlines())

    def test_a_reset_during_a_running_setup_changes_nothing(self, harness, tmp_path):
        """litclock-dev#938 review: a second reset fired while first-boot is
        already provisioning (marker gone, unit activating) used to delete every
        802-11-wireless profile, the live LitClock-Setup hotspot included, and
        restart first-boot mid-setup. The PWA's token-restore path explicitly
        allows a double-fire, so it must be a no-op."""
        harness["setup_complete"].unlink()
        stub = tmp_path / "systemctl-provisioning.sh"
        stub.write_text(
            f"""#!/bin/bash
if [ "$1" = is-active ]; then echo activating; exit 3; fi
echo "$@" >> "{harness["systemctl_log"]}"
exit 0
"""
        )
        stub.chmod(0o755)
        env = harness["env"].copy()
        env["LITCLOCK_SYSTEMCTL"] = str(stub)
        result = subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout
        assert not harness["systemctl_log"].exists(), harness["systemctl_log"].read_text()
        nmcli = harness["nmcli_log"].read_text() if harness["nmcli_log"].exists() else ""
        assert nmcli == ""
        assert harness["handoff_complete"].exists()

    @pytest.mark.parametrize("state", ["inactive", "failed", "active"])
    def test_marker_gone_but_firstboot_idle_still_resets(self, harness, tmp_path, state):
        """The double-fire guard must need BOTH conditions. Marker gone with
        first-boot NOT running (failed, inactive, or an earlier reset that died
        before its restart) is exactly the state that needs a reset: skipping
        it there would leave a clock with no setup and no hotspot."""
        harness["setup_complete"].unlink()
        stub = tmp_path / "systemctl-idle.sh"
        stub.write_text(
            f"""#!/bin/bash
if [ "$1" = is-active ]; then echo {state}; exit 3; fi
echo "$@" >> "{harness["systemctl_log"]}"
exit 0
"""
        )
        stub.chmod(0o755)
        env = harness["env"].copy()
        env["LITCLOCK_SYSTEMCTL"] = str(stub)
        result = subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout
        log = harness["systemctl_log"].read_text().splitlines()
        assert log[0] == "enable --no-reload litclock-firstboot.service", log
        assert log[-1] == "restart --no-block litclock-firstboot.service", log
        assert "connection delete" in harness["nmcli_log"].read_text()

    def test_enable_failure_refuses_the_reset(self, harness, tmp_path):
        """litclock-dev#938: if the enable fails, nothing may change — no WiFi
        profile deleted, .setup-complete kept, control left running, no restart.
        A reset that cannot promise a retry after the next boot is worse than
        no reset at all."""
        failing = tmp_path / "systemctl-enable-fails.sh"
        failing.write_text(
            f"""#!/bin/bash
echo "$@" >> "{harness["systemctl_log"]}"
[ "$1" = "enable" ] && exit 1
exit 0
"""
        )
        failing.chmod(0o755)
        env = harness["env"].copy()
        env["LITCLOCK_SYSTEMCTL"] = str(failing)
        result = subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 1
        assert "refusing to reset" in result.stdout
        assert harness["setup_complete"].exists()
        assert harness["handoff_complete"].exists()
        assert harness["systemctl_log"].read_text().splitlines() == ["enable --no-reload litclock-firstboot.service"]
        nmcli = harness["nmcli_log"].read_text() if harness["nmcli_log"].exists() else ""
        assert "delete" not in nmcli

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
        # Parsed lines, not a substring: the real call is `restart --no-block
        # litclock-firstboot.service`, which the old `"restart litclock-firstboot"
        # not in log` could never match (litclock-dev#938 review). The pre-flight also runs
        # before the Step 0 enable, so nothing at all reaches systemctl.
        assert log == "", log
        assert harness["handoff_complete"].exists()

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
        rm_idx = source.find('rm -f "$SETUP_COMPLETE_FILE"')
        # The systemctl call itself, not the error message that names it.
        restart_idx = source.find('"$SYSTEMCTL" restart --no-block litclock-firstboot.service')
        assert rm_idx > 0 and restart_idx > 0
        assert rm_idx < restart_idx


class TestUnitFilesKeepTheEnablePossible:
    """litclock-dev#938 review: the reset now REFUSES unless `systemctl enable
    litclock-firstboot.service` succeeds on the device, which the stubbed tests
    cannot see. Two things it needs: first-boot must stay installable, and the
    reset must run as root, since a non-root caller cannot enable units (polkit
    would refuse). Sandboxing directives on the reset unit are deliberately NOT
    banned: on a running system `systemctl enable` asks PID 1 over D-Bus and
    PID 1 writes the symlink, so the caller's ProtectSystem= does not reach it."""

    def test_firstboot_unit_has_an_install_section(self):
        unit = (REPO_ROOT / "systemd" / "litclock-firstboot.service").read_text()
        assert "[Install]" in unit and "WantedBy=" in unit

    def test_reset_unit_runs_as_root(self):
        # The EFFECTIVE value: systemd takes the last User= in [Service], and a
        # system unit with none runs as root. A substring match would pass a
        # later `User=pi` and fail a harmless deletion of `User=root`.
        unit = (REPO_ROOT / "systemd" / "litclock-wifi-reset.service").read_text()
        section, users = None, []
        for raw in unit.splitlines():
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line
            elif section == "[Service]" and line.startswith("User="):
                users.append(line.split("=", 1)[1].strip())
        assert not users or users[-1] == "root", (
            f"a non-root reset cannot run `systemctl enable` (litclock-dev#938): {users}"
        )


def _stop_post_command() -> str:
    """The shell command in litclock-wifi-reset.service's ExecStopPost= line, as
    /bin/sh will receive it: systemd turns `$$` into a literal `$` and expands any
    other `$VAR`/`%` itself, so a lone `$` or `%` here would mean something other
    than what this test runs (litclock-dev#938 review)."""
    unit = (REPO_ROOT / "systemd" / "litclock-wifi-reset.service").read_text()
    lines = [line for line in unit.splitlines() if line.startswith("ExecStopPost=")]
    assert len(lines) == 1, lines
    prefix = "ExecStopPost=-/bin/sh -c '"
    assert lines[0].startswith(prefix) and lines[0].endswith("'"), lines[0]
    command = lines[0][len(prefix) : -1]
    assert "%" not in command, "systemd would expand a % specifier"
    assert "$" not in command.replace("$$", ""), "a lone $ is expanded by systemd, not the shell"
    return command.replace("$$", "$")


class TestStopPostFailsafe:
    """litclock-dev#938 review: a reset that dies partway (the 60s timeout on a
    hung nmcli, an exit after the control server was stopped, a Conflicts= or
    manual stop) used to leave a frozen quote, no PWA, no hotspot, and
    wifi-watchdog standing down because the marker was gone. ExecStopPost= runs
    after ExecStart ends for any reason. These run the unit's ACTUAL command line
    against a systemctl stub that knows first-boot's state, with the exit
    variables systemd passes: a stop reports SERVICE_RESULT=signal (measured on
    systemd 255) or success with EXIT_CODE=killed (systemd.exec(5)), so only
    EXIT_CODE=exited + EXIT_STATUS=0 means the reset really finished.
    first-boot is RemainAfterExit=yes, so after any setup this boot it is
    `active` and `start` would be a no-op: the failsafe must
    `restart` it, but never while it is `activating` (a setup is running)."""

    def _run(self, tmp_path, *, marker: bool, firstboot: str, exit_code: str, exit_status: str, result: str):
        sc = tmp_path / ".setup-complete"
        if marker:
            sc.write_text("")
        log = tmp_path / "calls.log"
        stubs = tmp_path / "bin"
        stubs.mkdir()
        stub = stubs / "systemctl"
        stub.write_text(
            f"""#!/bin/bash
if [ "$1" = is-active ]; then echo "{firstboot}"; [ "{firstboot}" = active ]; exit $?; fi
echo "$*" >> "{log}"
exit 0
"""
        )
        stub.chmod(0o755)
        command = _stop_post_command()
        assert command.count("/etc/litclock/.setup-complete") == 1, command
        command = command.replace("/etc/litclock/.setup-complete", str(sc))
        env = {
            **os.environ,
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "SERVICE_RESULT": result,
            "EXIT_CODE": exit_code,
            "EXIT_STATUS": exit_status,
        }
        out = subprocess.run(["/bin/sh", "-c", command], env=env, capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return log.read_text().splitlines() if log.exists() else []

    def test_a_finished_reset_is_left_alone(self, tmp_path):
        # first-boot "active" (already completed this boot), so only the exit
        # gate can keep this empty: "activating" would mask a missing gate.
        calls = self._run(
            tmp_path,
            marker=False,
            firstboot="active",
            exit_code="exited",
            exit_status="0",
            result="success",
        )
        assert calls == []

    def test_timeout_reruns_a_firstboot_that_already_completed_this_boot(self, tmp_path):
        calls = self._run(
            tmp_path,
            marker=False,
            firstboot="active",
            exit_code="killed",
            exit_status="TERM",
            result="timeout",
        )
        assert calls == ["restart --no-block litclock-firstboot.service"]

    @pytest.mark.parametrize("result", ["signal", "success"])
    def test_a_sigterm_stop_counts_as_unfinished(self, tmp_path, result):
        # Conflicts=litclock-update.service or a manual stop: EXIT_CODE=killed,
        # EXIT_STATUS=TERM, with SERVICE_RESULT=signal (measured on systemd 255)
        # or success (systemd.exec(5)). The gate must not read SERVICE_RESULT,
        # so both have to act; "success" is the case a SERVICE_RESULT gate misses.
        calls = self._run(
            tmp_path,
            marker=False,
            firstboot="inactive",
            exit_code="killed",
            exit_status="TERM",
            result=result,
        )
        assert calls == ["restart --no-block litclock-firstboot.service"]

    def test_a_running_setup_is_never_interrupted(self, tmp_path):
        calls = self._run(
            tmp_path,
            marker=False,
            firstboot="activating",
            exit_code="exited",
            exit_status="1",
            result="exit-code",
        )
        assert calls == []

    def test_marker_present_restores_the_pwa(self, tmp_path):
        calls = self._run(
            tmp_path,
            marker=True,
            firstboot="active",
            exit_code="exited",
            exit_status="1",
            result="exit-code",
        )
        assert calls == ["start --no-block litclock-control.service"]
