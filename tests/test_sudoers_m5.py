"""Sudoers structural guards for #245 M5.

We can't run `visudo -c -f` in CI (requires the visudo binary + isn't
generally available on dev machines), so this test does the lighter-but-
still-useful check: verify the M5 entries are present and the file shape
hasn't drifted.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SUDOERS = REPO_ROOT / "sudoers" / "020_litclock-control"


class TestSudoersEntries:
    def test_litclock_update_service_entry_present(self):
        body = SUDOERS.read_text()
        assert "/usr/bin/systemctl start --no-block litclock-update.service" in body

    def test_litclock_wifi_reset_service_entry_present(self):
        body = SUDOERS.read_text()
        assert "/usr/bin/systemctl start --no-block litclock-wifi-reset.service" in body

    def test_litclock_prepare_for_gift_service_entry_present(self):
        """#280: /api/system/prepare-for-gift triggers the gift-prep unit via
        systemctl, same pattern as wifi-reset. Without this entry, the
        endpoint returns a 500 on every invocation."""
        body = SUDOERS.read_text()
        assert "/usr/bin/systemctl start --no-block litclock-prepare-for-gift.service" in body

    def test_litclock_reset_service_entry_present(self):
        """#510: /api/system/reset triggers the factory-reset unit via systemctl,
        same pattern as wifi-reset/gift. Without this entry the endpoint 500s."""
        body = SUDOERS.read_text()
        assert "/usr/bin/systemctl start --no-block litclock-reset.service" in body

    def test_reset_route_argv_matches_sudoers(self):
        """Parity: the exact argv reset() dispatches (minus sudo argv[0], which sudo
        strips) must be an authorized Cmnd in 020 — catches a unit-name/path drift
        between the route and the sudoers file in CI, not on hardware."""
        from control_server.routes.system import RESET_UNIT, SYSTEMCTL

        argv = ["sudo", SYSTEMCTL, "start", "--no-block", RESET_UNIT]
        assert argv[0] == "sudo", argv
        cmnd = " ".join(argv[1:])
        assert cmnd in SUDOERS.read_text()

    def test_pre_shutdown_stop_entry_present(self):
        """#362: /api/system/reboot + /api/system/poweroff now synchronously
        stop litclock.timer + litclock.service before invoking the destructive
        systemctl --no-block call so a timer-queued render can't paint over
        the 'Powered Off' splash. Without this exact sudoers entry the
        pre-stop subprocess.run returns non-zero and the race re-opens
        (degraded to pre-fix behavior, log + proceed)."""
        body = SUDOERS.read_text()
        assert "/usr/bin/systemctl stop litclock.timer litclock.service" in body

    def test_no_per_command_helper_path_entry(self):
        """D11 — the helper script invocation goes via systemctl, NOT via a
        direct sudoers entry for the .sh path. A direct entry would make
        the privilege surface broader than necessary; the systemd unit is
        the trust boundary. #280: same principle for reset-setup.sh — the
        gift-prep unit owns the elevated work, not a sudoers entry for the
        .sh path directly."""
        body = SUDOERS.read_text()
        assert "/usr/local/bin/litclock-wifi-reset.sh" not in body
        assert "/home/pi/litclock/scripts/reset-setup.sh" not in body

    def test_gift_tz_reset_entry_matches_route_argv(self):
        """#396: the sudoers entry must match the EXACT argv prepare_for_gift
        runs — sudo matches commands verbatim, so a drift (path change, extra
        flag) would silently no-op the privileged call once 010_pi-nopasswd is
        dropped (#387). Derive the expected command from _gift_reset_argv()
        rather than a duplicated literal so the source of truth is the code, not
        a string that can rot. sudo strips argv[0], so the sudoers Cmnd is the
        argv minus the leading 'sudo'. This is the parity class MEMORY flags as
        recurring (#388 sudo-boundary no-op)."""
        from control_server.routes.system import _gift_reset_argv

        argv = _gift_reset_argv()
        assert argv[0] == "sudo", argv
        expected_cmnd = " ".join(argv[1:])  # "/usr/bin/timedatectl set-timezone UTC"
        body = SUDOERS.read_text()
        # Match the whole comma-separated Cmnd, not a loose substring, so a
        # broader accidental allowance (e.g. trailing args) can't pass.
        cmnds = {c.strip() for c in body.split("NOPASSWD:", 1)[-1].split(",")}
        assert expected_cmnd in cmnds, f"{expected_cmnd!r} not an exact sudoers Cmnd; got {sorted(cmnds)}"

    def test_m4_entries_preserved(self):
        body = SUDOERS.read_text()
        assert "/usr/bin/systemctl reboot --no-block" in body
        assert "/usr/bin/systemctl poweroff --no-block" in body


class TestTmpfilesEntry:
    def test_var_lib_litclock_dir_owned_by_pi(self):
        """F5 — /var/lib/litclock must be created/normalized to pi:pi 0755
        on every boot via systemd-tmpfiles --create."""
        path = REPO_ROOT / "systemd" / "tmpfiles.d" / "litclock.conf"
        body = path.read_text()
        assert "d /var/lib/litclock 0755 pi pi -" in body

    def test_run_litclock_dir_preserved(self):
        """Pre-M5 entry must still be present (M2's status file +
        M5's update.status both live here)."""
        path = REPO_ROOT / "systemd" / "tmpfiles.d" / "litclock.conf"
        body = path.read_text()
        assert "d /run/litclock 0755 pi pi -" in body


class TestUpdateServiceTimeoutBumped:
    def test_timeout_start_sec_is_600(self):
        """F1 — TimeoutStartSec was 120; M5 review caught that real
        Pi-Zero-2W updates can exceed that under CPU pressure. Lock at
        600 so a regression to a tighter value fails CI."""
        path = REPO_ROOT / "systemd" / "litclock-update.service"
        body = path.read_text()
        assert "TimeoutStartSec=600" in body
        assert "TimeoutStartSec=120" not in body


class TestWifiResetServiceUnit:
    def test_unit_file_present(self):
        path = REPO_ROOT / "systemd" / "litclock-wifi-reset.service"
        assert path.exists()

    def test_conflicts_with_update_service(self):
        """D11 — Conflicts=litclock-update.service is the systemd-native
        interlock against running a wifi-reset mid-update. The Flask-side
        gate in /api/wifi/reset is racy without this."""
        path = REPO_ROOT / "systemd" / "litclock-wifi-reset.service"
        body = path.read_text()
        assert "Conflicts=litclock-update.service" in body

    def test_runs_as_root(self):
        """The helper has to stop a service we own + delete NM connections
        + remove root-owned /etc/litclock/.setup-complete. Running as root
        keeps the privilege surface minimal (no nested sudo needed inside
        the helper script)."""
        path = REPO_ROOT / "systemd" / "litclock-wifi-reset.service"
        body = path.read_text()
        assert "User=root" in body

    def test_execstart_invokes_helper_path(self):
        path = REPO_ROOT / "systemd" / "litclock-wifi-reset.service"
        body = path.read_text()
        assert "ExecStart=/usr/local/bin/litclock-wifi-reset.sh" in body


class TestPrepareForGiftServiceUnit:
    """#280: gift-prep unit, structurally a sibling of wifi-reset."""

    UNIT = REPO_ROOT / "systemd" / "litclock-prepare-for-gift.service"

    def test_unit_file_present(self):
        assert self.UNIT.exists(), "systemd/litclock-prepare-for-gift.service must ship in-repo"

    def test_conflicts_with_update_service(self):
        """Same interlock as wifi-reset: never run mid-update. reset-setup.sh
        wipes env.sh and powers off — running concurrently with an update is
        a deterministic appliance brick."""
        body = self.UNIT.read_text()
        assert "Conflicts=litclock-update.service" in body

    def test_runs_as_root(self):
        """reset-setup.sh enforces EUID==0 (touches /etc/litclock/, stops
        services, deletes NM connections, overwrites env.sh, invokes
        poweroff). Running as root keeps the privilege surface to the single
        sudoers allowance for `systemctl start --no-block …`."""
        body = self.UNIT.read_text()
        assert "User=root" in body

    def test_execstart_invokes_reset_setup_with_gift_mode(self):
        body = self.UNIT.read_text()
        # #387: the ROOT-OWNED copy, not the pi-writable repo path (pi can
        # `systemctl start` this unit via 020, so a pi-writable ExecStart is
        # a pi->root vector).
        assert "/usr/local/lib/litclock/reset-setup.sh" in body
        assert "/home/pi/litclock/scripts/reset-setup.sh" not in body
        assert "--gift-mode" in body
        # Message file is the trust boundary — script reads from this path
        # rather than accepting the message on the command line, so the
        # sudoers entry doesn't need to grant arbitrary args.
        assert "--message-file /run/litclock/gift-message" in body

    def test_timeout_generous(self):
        """reset-setup.sh on Pi Zero 2W finishes its swap + invoke-poweroff
        in ~10s. 60s gives headroom without leaving a stuck unit pinning
        systemd's job queue forever."""
        body = self.UNIT.read_text()
        assert "TimeoutStartSec=60" in body


# ─── M5 jq dependency (needed for status-file helper) ───────────────────────


class TestJqAptDependency:
    """Hardware QA on test Pi 2026-04-30 caught: M5's update_status helper
    (scripts/lib/update_status.sh) requires jq for atomic JSON writes, but
    jq wasn't in the project's apt deps. Without it, the PWA's phase
    reading-list never animates because the status file is never written.

    These tests pin jq in all three install paths (DIY install, image
    build, in-place update) so the regression can't sneak back in.
    """

    def test_install_sh_includes_jq(self):
        path = REPO_ROOT / "scripts" / "install.sh"
        body = path.read_text()
        assert " jq" in body, (
            "scripts/install.sh must apt-install jq (M5 status-file helper requires it for atomic JSON writes)"
        )

    def test_pi_gen_image_build_includes_jq(self):
        path = REPO_ROOT / "pi-gen" / "stage3" / "00-install-deps" / "01-run.sh"
        body = path.read_text()
        assert " jq" in body, (
            "pi-gen/stage3/00-install-deps/01-run.sh must apt-install jq "
            "so fresh images can write the M5 update-status file"
        )

    def test_update_sh_ensures_jq_for_existing_pis(self):
        path = REPO_ROOT / "scripts" / "update.sh"
        body = path.read_text()
        # Existing pre-M5 Pis don't have jq in their apt set. update.sh
        # must self-install it on next fire so the upgraded code's
        # status-file helper works.
        assert "command -v jq" in body
        assert "apt-get install -y" in body and "jq" in body
