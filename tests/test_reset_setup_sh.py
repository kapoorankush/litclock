"""Tests for scripts/reset-setup.sh (issue #160)."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RESET_SH = REPO_ROOT / "scripts" / "reset-setup.sh"


@pytest.fixture(scope="module")
def reset_sh_content():
    return RESET_SH.read_text()


class TestResetSetupStructure:
    def test_requires_root(self, reset_sh_content):
        assert "$EUID -ne 0" in reset_sh_content

    def test_has_wipe_wifi_flag(self, reset_sh_content):
        assert "--wipe-wifi" in reset_sh_content
        assert "WIPE_WIFI=true" in reset_sh_content

    def test_wifi_wipe_is_gated_by_flag(self, reset_sh_content):
        """Default run must NOT delete WiFi — only --wipe-wifi triggers it."""
        # The wipe block must be inside an `if [[ "$WIPE_WIFI" == "true" ]]` guard.
        wipe_block = reset_sh_content.find("Wiping saved WiFi networks")
        guard = reset_sh_content.rfind('if [[ "$WIPE_WIFI" == "true" ]]', 0, wipe_block)
        assert guard != -1, "WiFi wipe block must be guarded by $WIPE_WIFI check"
        # And the rm -f that actually deletes profiles must be inside the guard.
        rm_idx = reset_sh_content.find('rm -f "$conn"', wipe_block)
        assert rm_idx != -1

    def test_only_wifi_connections_deleted(self, reset_sh_content):
        """Wired ethernet, VPN, bluetooth-PAN profiles live in the same dir
        and must NOT be wiped. Matching by `type=wifi` in the connection file
        is the safety mechanism."""
        assert "type=wifi" in reset_sh_content
        # The rm must be inside a `grep -qE '^type=wifi$'` conditional.
        grep_idx = reset_sh_content.find("grep -qE '^type=wifi")
        rm_idx = reset_sh_content.find('rm -f "$conn"')
        assert grep_idx != -1
        assert rm_idx != -1
        assert grep_idx < rm_idx

    def test_service_stops_are_tolerant(self, reset_sh_content):
        """Each systemctl stop must have `|| true` so a missing service
        doesn't abort the reset. `set -e` is not set, but we still want
        resilience here."""
        for svc in ("litclock.timer", "litclock.service", "litclock-firstboot.service"):
            line = next(
                (ln for ln in reset_sh_content.splitlines() if f"systemctl stop {svc}" in ln),
                None,
            )
            assert line is not None, f"missing stop for {svc}"
            assert "|| true" in line, f"stop {svc} must use `|| true` tolerance"

    def test_weather_cache_cleared(self, reset_sh_content):
        """Stale weather cache from a prior unit system can be served under
        the new unit label (issue #175). Must be cleared."""
        assert 'rm -f "$INSTALL_DIR"/weather-cache*.json' in reset_sh_content

    def test_preserves_env_sh_file(self, reset_sh_content):
        """env.sh should be reset to defaults but NOT deleted — deletion
        would break downstream scripts that read from it. Post-#274 the
        reset writes defaults via atomic_write_env_sh (sidecar-flocked,
        interlocks with src/config.py's atomic_update from the PWA)."""
        assert 'atomic_write_env_sh "$INSTALL_DIR/env.sh"' in reset_sh_content
        # Make sure there's no `rm -f .../env.sh` in the script.
        assert 'rm -f "$INSTALL_DIR/env.sh"' not in reset_sh_content

    def test_reenables_firstboot_service(self, reset_sh_content):
        """After reset the device must boot back into setup mode."""
        assert "systemctl enable litclock-firstboot.service" in reset_sh_content

    def test_clears_weather_location_name(self, reset_sh_content):
        """#389/#380: WEATHER_LOCATION_NAME (added as an env key in PR1) must be
        in the defaults block so a reset clears the prior city — otherwise a
        reset device's Status/splash would show the previous owner's location."""
        defaults_idx = reset_sh_content.find("DEFAULTS=")
        assert defaults_idx != -1
        block = reset_sh_content[defaults_idx : defaults_idx + 400]
        assert "export WEATHER_LOCATION_NAME=" in block


class TestGiftMode:
    """Issue #189 — `--gift-mode` preps the device for shipping."""

    def test_has_gift_mode_flag(self, reset_sh_content):
        assert "--gift-mode" in reset_sh_content
        assert "GIFT_MODE=true" in reset_sh_content

    def test_gift_mode_resets_timezone_to_utc(self, reset_sh_content):
        """#389: the timezone is system state (timedatectl), not env.sh, so the
        config wipe doesn't touch it. A gifted device must not leak the gifter's
        timezone — reset it to UTC so the recipient's first-boot IP-geo sets
        theirs. (Hardware QA T24 confirms timedatectl actually reports UTC — a
        grep can't prove the call works on-device.)"""
        assert "timedatectl set-timezone UTC" in reset_sh_content

    def test_timezone_reset_gated_by_gift_mode(self, reset_sh_content):
        """#389: only gift mode forgets the timezone — a plain reset of your own
        device has no privacy reason to. The timedatectl call must sit inside a
        `$GIFT_MODE == true` guard."""
        tz_idx = reset_sh_content.find("timedatectl set-timezone UTC")
        assert tz_idx != -1
        guard = reset_sh_content.rfind('if [[ "$GIFT_MODE" == "true" ]]', 0, tz_idx)
        assert guard != -1, "timedatectl UTC reset must be guarded by $GIFT_MODE"
        # And it must NOT escape into the always-run config block.
        env_reset = reset_sh_content.find("Resetting configuration")
        assert tz_idx > env_reset, "tz reset should follow (not precede) the env wipe"

    def test_gift_mode_implies_wipe_wifi_and_yes(self, reset_sh_content):
        """Shipping a device with the prep author's WiFi baked in would be
        a real bug — gift-mode must force wipe + skip the prompt."""
        # Find the --gift-mode case block and verify it sets both flags.
        idx = reset_sh_content.find("--gift-mode)")
        assert idx != -1
        # Look at the next ~200 chars for the implications.
        block = reset_sh_content[idx : idx + 200]
        assert "WIPE_WIFI=true" in block
        assert "AUTO_YES=true" in block

    def test_gift_mode_powers_off(self, reset_sh_content):
        """End-of-script gift-mode branch must call poweroff (not reboot) —
        poweroff is what makes the welcome splash persist on the bistable e-ink."""
        idx = reset_sh_content.find('if [[ "$GIFT_MODE" == "true" ]]')
        assert idx != -1, "gift-mode end-of-script branch missing"
        elif_idx = reset_sh_content.find("elif", idx)
        block = reset_sh_content[idx:elif_idx]
        assert "poweroff" in block

    def test_gift_mode_disables_ssh_before_poweroff(self, reset_sh_content):
        """#528: gift mode must force SSH off before shipping — an owner who
        ever enabled SSH (QA, recovery) would otherwise hand the recipient a
        device with SSH + default creds listening on their network. Every
        layer: the SOCKET (Bookworm socket-activates sshd — disabling only
        ssh.service leaves port 22 open, caught by hardware QA 2026-07-16),
        the classic service, raspi-config posture, and the boot-partition
        re-enable flags (sshswitch re-enables SSH if a bare `ssh` file
        exists on /boot or /boot/firmware)."""
        # rfind: the end-of-script branch is the LAST $GIFT_MODE test in the
        # file (the first one is the early marker-write block).
        idx = reset_sh_content.rfind('if [[ "$GIFT_MODE" == "true" ]]')
        elif_idx = reset_sh_content.find("elif", idx)
        block = reset_sh_content[idx:elif_idx]
        # The socket is the load-bearing unit on current images — a
        # service-only disable ships a device with port 22 still open.
        assert "ssh.socket" in block, "must disable ssh.socket (Bookworm socket-activation)"
        assert "systemctl disable --now ssh.socket ssh.service" in block
        assert "raspi-config nonint do_ssh 1" in block
        assert "/boot/firmware/ssh" in block and "/boot/ssh" in block
        # And it must happen before the poweroff COMMAND (rfind: the word
        # also appears in comments earlier in the branch).
        assert block.find("systemctl disable --now ssh") < block.rfind("\n    poweroff")

    def test_gift_mode_ssh_disable_after_env_wipe_failure_gate(self, reset_sh_content):
        """#528: on a FAILED gift prep the script exits non-zero and the
        device stays on — the owner may need SSH to fix it. The SSH disable
        must therefore sit AFTER the ENV_WIPE_FAILED fatal gate so the
        failure path never locks the owner out."""
        idx = reset_sh_content.rfind('if [[ "$GIFT_MODE" == "true" ]]')
        block = reset_sh_content[idx : reset_sh_content.find("elif", idx)]
        gate_idx = block.find('"$ENV_WIPE_FAILED" == "true"')
        ssh_idx = block.find("systemctl disable --now ssh")
        assert gate_idx != -1 and ssh_idx != -1
        assert gate_idx < ssh_idx

    def test_gift_mode_marker_written_before_shutdown_service_stop(self, reset_sh_content):
        """CRITICAL ordering invariant: the .welcome-mode marker must be written
        BEFORE `systemctl stop litclock-shutdown.service`. That stop fires the
        service's ExecStop (shutdown-splash.sh), which branches on the marker.
        If the marker is written later, ExecStop has already painted
        'Powered Off' and won't re-fire on the subsequent poweroff (the service
        is already inactive). Feature would be a no-op on real hardware."""
        marker_idx = reset_sh_content.find('touch "$CONFIG_DIR/.welcome-mode"')
        stop_idx = reset_sh_content.find("systemctl stop litclock-shutdown.service")
        assert marker_idx != -1, "gift-mode marker `touch` not found"
        assert stop_idx != -1, "shutdown-service stop not found"
        assert marker_idx < stop_idx, (
            "marker must be written before `systemctl stop litclock-shutdown.service` "
            "so ExecStop picks up the gift-mode branch"
        )

    def test_gift_mode_aborts_poweroff_on_env_wipe_failure(self, reset_sh_content):
        """#393: the env.sh wipe is the load-bearing privacy step for a gift —
        it clears the gifter's WEATHER_LATITUDE/LONGITUDE/LOCATION_NAME. If it
        fails (lock timeout rc=75 or a write error), stale coordinates survive
        into the recipient's first boot and PR2's handoff can start a wrong-time
        clock off the leftover latitude. So in gift mode a failed wipe is FATAL:
        the Step 3 failure path sets ENV_WIPE_FAILED, and the end-of-script gift
        branch must refuse to power off (poweroff is the 'ready to ship' signal)
        and exit non-zero when the wipe failed. Plain non-gift resets stay
        best-effort and ignore the flag."""
        # The env-wipe failure path must set the flag.
        assert "ENV_WIPE_FAILED=true" in reset_sh_content
        # The end-of-script gift branch must gate on the flag before poweroff.
        gift_idx = reset_sh_content.rfind('if [[ "$GIFT_MODE" == "true" ]]')
        assert gift_idx != -1, "end-of-script gift branch missing"
        flag_check_idx = reset_sh_content.find('"$ENV_WIPE_FAILED" == "true"', gift_idx)
        assert flag_check_idx != -1, "gift branch must check ENV_WIPE_FAILED"
        poweroff_idx = reset_sh_content.find("poweroff", gift_idx)
        assert poweroff_idx != -1, "gift branch poweroff missing"
        assert flag_check_idx < poweroff_idx, (
            "the ENV_WIPE_FAILED gate must precede poweroff so a failed wipe "
            "aborts before the device is declared ready to ship"
        )
        # The abort must exit non-zero (a stale device must not ship silently).
        abort_block = reset_sh_content[flag_check_idx:poweroff_idx]
        assert "exit 1" in abort_block, "failed-wipe abort must exit non-zero, not fall through to poweroff"

    def test_message_file_flag_parsed(self, reset_sh_content):
        """#280: --message-file FILE flag must be parsed. The PWA's
        Prepare-for-Gifting endpoint hands the script a file path containing
        the personalized welcome — reading from a file (not an inline arg)
        keeps the message out of the process list / journal."""
        assert "--message-file" in reset_sh_content
        assert "GIFT_MESSAGE_FILE=" in reset_sh_content

    def test_welcome_message_written_before_shutdown_service_stop(self, reset_sh_content):
        """#280: same ordering invariant as the .welcome-mode marker —
        .welcome-message must be written BEFORE the shutdown service stops,
        otherwise shutdown-splash.sh's ExecStop has already painted the
        default greeting and won't re-read the file on the subsequent
        poweroff."""
        msg_write_idx = reset_sh_content.find('"$CONFIG_DIR/.welcome-message"')
        stop_idx = reset_sh_content.find("systemctl stop litclock-shutdown.service")
        assert msg_write_idx != -1, ".welcome-message write not found"
        assert msg_write_idx < stop_idx, (
            ".welcome-message must be written before `systemctl stop litclock-shutdown.service`"
        )

    def test_welcome_message_size_bounded(self, reset_sh_content):
        """#280 + #319: the message file copy must be size-bounded so a
        hostile or unbounded input file can't fill /etc/litclock. M3's
        validator caps GIFT_MODE_MESSAGE at 80 chars (#319 lowered from
        280 once the renderer learned to wrap); reset-setup.sh enforces
        the same at write-time via `os.read(fd, 80)` defense-in-depth
        inside the O_NOFOLLOW Python block (#316)."""
        gift_block_start = reset_sh_content.find('if [[ "$GIFT_MODE" == "true" ]]; then')
        gift_block_end = reset_sh_content.find('echo "=', gift_block_start)
        gift_block = reset_sh_content[gift_block_start:gift_block_end]
        assert "os.read(fd, 80)" in gift_block, (
            "welcome-message write must enforce 80-char ceiling (matches "
            "GIFT_MODE_MESSAGE_MAX_LEN in src/config.py post-#319)"
        )

    def test_welcome_message_rejects_symlinks(self, reset_sh_content):
        """#280 + #316 /review: source file (handed in via --message-file)
        must be opened with O_NOFOLLOW. The naive `[[ ! -L ... ]] && head`
        is racy — between the test and the read, a pi-level adversary can
        rename(2) a symlink over the path; since this script runs as root,
        the read would then follow the symlink and exfiltrate /etc/shadow
        et al. to the e-ink display. Defense: O_NOFOLLOW from Python."""
        gift_block_start = reset_sh_content.find('if [[ "$GIFT_MODE" == "true" ]]; then')
        gift_block_end = reset_sh_content.find('echo "=', gift_block_start)
        gift_block = reset_sh_content[gift_block_start:gift_block_end]
        assert "O_NOFOLLOW" in gift_block, (
            "--message-file source must be opened with O_NOFOLLOW — the older `[[ ! -L ... ]]` "
            "check is TOCTOU-racy under root, opening a pi→root file-disclosure primitive "
            "(#316 /review CRITICAL finding)"
        )

    def test_no_message_file_clears_stale_welcome_message(self, reset_sh_content):
        """#280: if a previous --gift-mode run set a personalized message and
        the next run doesn't pass --message-file, the stale message must NOT
        leak into the new gift-mode session. Explicit absence = default text."""
        gift_block_start = reset_sh_content.find('if [[ "$GIFT_MODE" == "true" ]]; then')
        gift_block_end = reset_sh_content.find('echo "=', gift_block_start)
        gift_block = reset_sh_content[gift_block_start:gift_block_end]
        assert "rm -f" in gift_block and ".welcome-message" in gift_block, (
            "absent --message-file must clear any prior .welcome-message"
        )


class TestRebootHintFile:
    """Issue #282 — --reboot must signal shutdown-splash.sh to paint
    'Restarting...' instead of 'Powered Off'. The hint write is hardened
    against symlink TOCTOU + cancel/abort cleanup per /review of PR #304."""

    HINT_PATH = "/run/litclock/shutdown-action"
    HINT_TMP_PATTERN = ".litclock-hint.XXXXXX"
    HINT_WRITE_GUARD = 'if [[ "$DO_REBOOT" == "true" ]]'

    def _hint_block(self, content: str) -> str:
        """Slice the content to just the DO_REBOOT-guarded hint write block.
        Anchored on the `# Issue #282:` comment header (unique) and the
        `# Step 1:` services-stop marker so we don't accidentally pick up
        the end-of-script `elif [[ $DO_REBOOT ]]` reboot branch."""
        start = content.find("# Issue #282:")
        assert start != -1, "`# Issue #282:` hint-block header missing"
        end = content.find("# Step 1:", start)
        assert end != -1, "could not find end of hint block (Step 1 marker)"
        block = content[start:end]
        assert self.HINT_WRITE_GUARD in block, "DO_REBOOT guard missing inside hint block"
        return block

    def test_writes_hint_file_when_reboot_flag_set(self, reset_sh_content):
        assert self.HINT_PATH in reset_sh_content
        block = self._hint_block(reset_sh_content)
        # Must produce the literal bytes `reboot\n` somewhere in the block.
        assert "printf 'reboot\\n'" in block or "echo 'reboot'" in block or 'echo "reboot"' in block

    def test_hint_write_gated_by_reboot_flag(self, reset_sh_content):
        """Hint write must be guarded by `if [[ "$DO_REBOOT" == "true" ]]`
        — writing unconditionally would mislabel a non-reboot path."""
        block = self._hint_block(reset_sh_content)
        assert self.HINT_WRITE_GUARD in block

    def test_hint_written_before_shutdown_service_stop(self, reset_sh_content):
        """The hint write block must precede `systemctl stop litclock-shutdown.service`
        — ExecStop fires from that stop and reads the hint."""
        # Anchor on the unique #282 comment header (the `if [[ $DO_REBOOT ]]`
        # string also appears in the end-of-script reboot branch).
        block_idx = reset_sh_content.find("# Issue #282:")
        stop_idx = reset_sh_content.find("systemctl stop litclock-shutdown.service")
        assert block_idx != -1, "#282 hint-block header missing"
        assert stop_idx != -1, "shutdown-service stop missing"
        assert block_idx < stop_idx, "hint write must come before the shutdown-service stop"

    def test_hint_written_after_user_confirmation(self, reset_sh_content):
        """The hint write must come AFTER the y/N prompt block — otherwise a
        cancelling user (`n`) leaves a stale 'reboot' hint in /run that
        misleads a later unrelated shutdown until the next real reboot."""
        guard_idx = reset_sh_content.rfind(self.HINT_WRITE_GUARD)
        prompt_exit_idx = reset_sh_content.find('echo "Cancelled."')
        assert prompt_exit_idx != -1, "y/N cancellation handler missing"
        assert guard_idx > prompt_exit_idx, (
            "hint write must be AFTER the prompt-cancel `exit 0` so a "
            "cancelling user doesn't leave a stale hint in /run"
        )

    def test_hint_uses_atomic_rename(self, reset_sh_content):
        """Direct `>` redirect into pi-owned /run/litclock/ would follow
        attacker-planted symlinks (CRITICAL TOCTOU). Atomic write is via
        a root-owned /run/ tmpfile + `mv -T` (rename(2) replaces the
        destination without traversing pre-existing symlinks)."""
        block = self._hint_block(reset_sh_content)
        # Must use mktemp -p /run (the root-owned dir, not pi-owned /run/litclock/).
        assert "mktemp -p /run " in block, "hint write must allocate tmp via mktemp -p /run"
        # Must use mv -T (atomic rename, no symlink follow at destination).
        assert "mv -T" in block, "hint write must finalize with mv -T (rename(2))"
        # Must NOT contain a direct `> /run/litclock/shutdown-action` redirect.
        assert "> /run/litclock/shutdown-action" not in reset_sh_content, (
            "direct `>` redirect into pi-owned /run/litclock/ is the symlink-TOCTOU "
            "primitive — use mv -T from a /run/ tmpfile instead"
        )

    def test_hint_block_registers_exit_trap(self, reset_sh_content):
        """Script abort or Ctrl-C between hint write and `systemctl reboot`
        must clean up the hint, otherwise it persists across the script and
        misleads the next unrelated stop of litclock-shutdown.service."""
        block = self._hint_block(reset_sh_content)
        assert "trap " in block and "EXIT" in block, "EXIT trap missing in hint write block"
        assert "rm -f /run/litclock/shutdown-action" in block, "EXIT trap must rm -f the hint file"

    def test_does_not_mkdir_run_litclock_as_root(self, reset_sh_content):
        """`mkdir -p /run/litclock` as root would create the dir as
        root:root if tmpfiles.d hasn't run, breaking later pi-user
        heartbeat/status writes that expect pi:pi ownership. Drop it —
        if the dir is missing the rename fails, splash falls back to
        list-jobs detection (pre-PR behavior)."""
        assert "mkdir -p /run/litclock" not in reset_sh_content, (
            "do not mkdir /run/litclock as root — it's provisioned by tmpfiles.d "
            "as pi:pi; root mkdir creates wrong ownership"
        )

    def test_uses_systemctl_reboot_not_bare_reboot(self, reset_sh_content):
        """Use `systemctl reboot` directly (cleaner systemd integration;
        bare `/sbin/reboot` forwards to it on Bookworm anyway)."""
        assert "systemctl reboot" in reset_sh_content
        import re

        bare_reboot = re.search(r"(?m)^\s*reboot\s*$", reset_sh_content)
        assert bare_reboot is None, (
            f"bare `reboot` invocation at offset {bare_reboot.start() if bare_reboot else None} "
            "— use `systemctl reboot` instead"
        )


class TestResetSetupExecution:
    def test_default_run_preserves_wifi_profiles(self, script_sandbox, tmp_path):
        """Without --wipe-wifi, NM connection files should survive."""
        # Simulate NM connections dir with one wifi + one ethernet + one VPN.
        nm_dir = tmp_path / "nm"
        nm_dir.mkdir()
        (nm_dir / "home.nmconnection").write_text("[connection]\ntype=wifi\n")
        (nm_dir / "eth.nmconnection").write_text("[connection]\ntype=ethernet\n")
        (nm_dir / "vpn.nmconnection").write_text("[connection]\ntype=vpn\n")

        # We can't easily sandbox /etc/NetworkManager or /etc/litclock without
        # writing a wrapper, so this test asserts the grep-based filter works
        # directly against fixture files.
        import subprocess

        for conn in nm_dir.glob("*.nmconnection"):
            r = subprocess.run(
                ["grep", "-qE", "^type=wifi$", str(conn)],
                capture_output=True,
            )
            if conn.name == "home.nmconnection":
                assert r.returncode == 0, "wifi profile should match"
            else:
                assert r.returncode != 0, f"{conn.name} should NOT match"


def test_clears_handoff_complete_marker():
    """EPIC #383 PR2 (#388): a reset returns the device to fresh-setup state, so
    the lingering .handoff-complete must be cleared too — otherwise the
    post-WiFi handoff splash would be skipped on re-provision (handoff is active
    only when .setup-complete exists AND .handoff-complete is absent)."""
    src = RESET_SH.read_text()
    assert 'rm -f "$CONFIG_DIR/.handoff-complete"' in src


def test_defaults_include_weather_location_mode_and_ip_country():
    """#337 A3 + /review testing-gap: gift-mode reset must include the new
    MODE + IP_COUNTRY defaults. Without these, a gift-recipient whose
    first-boot IP-geo fails would inherit the gifter's stale MODE=specific
    AND no IP_COUNTRY baseline — on-boot reresolve would never fire."""
    from pathlib import Path

    content = (Path(__file__).parent.parent / "scripts/reset-setup.sh").read_text()
    assert "export WEATHER_LOCATION_MODE=auto" in content, "#337 A3: reset-setup.sh DEFAULTS must include MODE=auto"
    assert "export WEATHER_IP_COUNTRY=" in content, (
        "#337 A3: reset-setup.sh DEFAULTS must include WEATHER_IP_COUNTRY= (empty)"
    )


# ── #387: prepare-for-gift pi->root hardening ────────────────────────────────


class TestPrivilegeHardening387:
    """litclock-prepare-for-gift.service runs reset-setup.sh as root and pi can
    `systemctl start` it via sudoers/020, so the script + everything it executes
    as root must live outside the pi-writable repo."""

    SERVICE = REPO_ROOT / "systemd" / "litclock-prepare-for-gift.service"
    PI_GEN = REPO_ROOT / "pi-gen" / "stage3" / "03-install-services" / "00-run.sh"
    INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
    UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"

    def test_service_execstart_is_root_owned_copy(self):
        body = self.SERVICE.read_text()
        assert "ExecStart=/usr/local/lib/litclock/reset-setup.sh" in body, (
            "prepare-for-gift.service must exec the ROOT-OWNED reset-setup.sh copy (#387)"
        )
        assert "ExecStart=/home/pi/litclock/scripts/reset-setup.sh" not in body, (
            "must NOT exec the pi-writable repo copy as root (#387 pi->root)"
        )

    def test_gift_message_uses_system_python_not_venv(self, reset_sh_content):
        # Running the pi-writable venv interpreter as root is a pi->root vector;
        # the stdlib-only heredoc uses the root-owned system python instead.
        assert "/usr/bin/python3 - " in reset_sh_content, "gift-message processing must use the system python3 (#387)"
        assert '"$INSTALL_DIR/venv/bin/python3" - "$GIFT_MESSAGE_FILE"' not in reset_sh_content, (
            "must NOT run the pi-writable venv interpreter as root (#387)"
        )

    def test_sources_state_lib_relative_to_self(self, reset_sh_content):
        # So the root-owned copy sources the root-owned lib/state.sh beside it.
        assert '"$_THIS_SCRIPT_DIR/lib/state.sh"' in reset_sh_content, (
            "reset-setup must source lib/state.sh relative to its own dir so the "
            "installed root-owned copy sources the root-owned lib (#387)"
        )

    def test_install_paths_ship_reset_setup_and_state_root_owned(self):
        for src, name in ((self.PI_GEN, "pi-gen"), (self.INSTALL_SH, "install.sh"), (self.UPDATE_SH, "update.sh")):
            body = src.read_text()
            assert "reset-setup.sh" in body and "/usr/local/lib/litclock" in body, (
                f"{name} must install reset-setup.sh root-owned to /usr/local/lib/litclock (#387)"
            )
            assert "/usr/local/lib/litclock/lib" in body, f"{name} must install the root-owned lib/state.sh dir (#387)"
            assert "lib/state.sh" in body, f"{name} must install state.sh alongside (#387)"


class TestFactoryResetStrictEnvWipe:
    """#510 review (Codex): the PWA Factory reset must be fail-closed on a
    config-wipe failure. Unlike a plain reset (best-effort) or gift mode (aborts
    before poweroff), the factory path passes --strict-env-wipe so a Step 3 env.sh
    wipe failure aborts BEFORE the destructive WiFi wipe + reboot — never silently
    reboots the owner into a stale-config setup believing everything was erased."""

    def test_has_strict_env_wipe_flag(self, reset_sh_content):
        assert "--strict-env-wipe) STRICT_ENV_WIPE=true" in reset_sh_content

    def test_strict_guard_precedes_wifi_wipe_and_reboot(self, reset_sh_content):
        guard_idx = reset_sh_content.find('"$STRICT_ENV_WIPE" == "true" && "$ENV_WIPE_FAILED" == "true"')
        assert guard_idx != -1, "strict-env-wipe fail-closed guard missing"
        # Guard aborts non-zero right after the check.
        exit_idx = reset_sh_content.find("exit 1", guard_idx)
        assert exit_idx != -1 and (exit_idx - guard_idx) < 500, "strict guard must exit 1"
        # The destructive WiFi wipe (Step 7) and the end-of-script reboot must come
        # AFTER the guard so a failed wipe leaves WiFi up + no reboot.
        wifi_idx = reset_sh_content.find("Step 7", guard_idx)
        reboot_idx = reset_sh_content.find("systemctl reboot", guard_idx)
        assert wifi_idx != -1 and guard_idx < wifi_idx, "guard must precede the WiFi wipe"
        assert reboot_idx != -1 and guard_idx < reboot_idx, "guard must precede the reboot"

    def test_plain_reset_stays_best_effort(self, reset_sh_content):
        """Default STRICT_ENV_WIPE=false — a plain/dev reset must NOT abort on an
        env-wipe failure (behavior unchanged for the shell/dev path)."""
        assert "STRICT_ENV_WIPE=false" in reset_sh_content

    def test_reset_unit_passes_strict_env_wipe(self):
        unit = (REPO_ROOT / "systemd" / "litclock-reset.service").read_text()
        assert "--strict-env-wipe" in unit, "litclock-reset.service must pass --strict-env-wipe"
        assert "--wipe-wifi" in unit and "--reboot" in unit
