"""Tests for scripts/reset-setup.sh (issue litclock-dev#160)."""

import os
import re
import shlex
import subprocess
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
        the new unit label (issue litclock-dev#175). Must be cleared."""
        assert 'rm -f "$INSTALL_DIR"/weather-cache*.json' in reset_sh_content

    def test_preserves_env_sh_file(self, reset_sh_content):
        """env.sh should be reset to defaults but NOT deleted — deletion
        would break downstream scripts that read from it. Post-litclock-dev#274 the
        reset writes defaults via atomic_write_env_sh (sidecar-flocked,
        interlocks with src/config.py's atomic_update from the PWA)."""
        assert 'atomic_write_env_sh "$INSTALL_DIR/env.sh"' in reset_sh_content
        # Make sure there's no `rm -f .../env.sh` in the script.
        assert 'rm -f "$INSTALL_DIR/env.sh"' not in reset_sh_content

    def test_reenables_firstboot_service(self, reset_sh_content):
        """After reset the device must boot back into setup mode."""
        assert "systemctl enable litclock-firstboot.service" in reset_sh_content

    def test_clears_weather_location_name(self, reset_sh_content):
        """litclock-dev#389/litclock-dev#380: WEATHER_LOCATION_NAME (added as an env key in PR1) must be
        in the defaults block so a reset clears the prior city — otherwise a
        reset device's Status/splash would show the previous owner's location."""
        defaults_idx = reset_sh_content.find("DEFAULTS=")
        assert defaults_idx != -1
        block = reset_sh_content[defaults_idx : defaults_idx + 400]
        assert "export WEATHER_LOCATION_NAME=" in block


class TestGiftMode:
    """Issue litclock-dev#189 — `--gift-mode` preps the device for shipping."""

    def test_has_gift_mode_flag(self, reset_sh_content):
        assert "--gift-mode" in reset_sh_content
        assert "GIFT_MODE=true" in reset_sh_content

    def test_gift_mode_resets_timezone_to_utc(self, reset_sh_content):
        """litclock-dev#389: the timezone is system state (timedatectl), not env.sh, so the
        config wipe doesn't touch it. A gifted device must not leak the gifter's
        timezone — reset it to UTC so the recipient's first-boot IP-geo sets
        theirs. (Hardware QA T24 confirms timedatectl actually reports UTC — a
        grep can't prove the call works on-device.)"""
        assert "timedatectl set-timezone UTC" in reset_sh_content

    def test_timezone_reset_gated_by_gift_mode(self, reset_sh_content):
        """litclock-dev#389: only gift mode forgets the timezone — a plain reset of your own
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
        # rfind → the END-OF-SCRIPT gift branch (there's an earlier
        # marker-write `if [[ "$GIFT_MODE" ...]]`, and litclock-dev#627 added a
        # DO_POWEROFF elif to the hint block between them).
        idx = reset_sh_content.rfind('if [[ "$GIFT_MODE" == "true" ]]')
        assert idx != -1, "gift-mode end-of-script branch missing"
        elif_idx = reset_sh_content.find("elif", idx)
        block = reset_sh_content[idx:elif_idx]
        assert "poweroff" in block

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
        """litclock-dev#393: the env.sh wipe is the load-bearing privacy step for a gift —
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
        """litclock-dev#280: --message-file FILE flag must be parsed. The PWA's
        Prepare-for-Gifting endpoint hands the script a file path containing
        the personalized welcome — reading from a file (not an inline arg)
        keeps the message out of the process list / journal."""
        assert "--message-file" in reset_sh_content
        assert "GIFT_MESSAGE_FILE=" in reset_sh_content

    def test_welcome_message_written_before_shutdown_service_stop(self, reset_sh_content):
        """litclock-dev#280: same ordering invariant as the .welcome-mode marker —
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
        """litclock-dev#280 + litclock-dev#319: the message file copy must be size-bounded so a
        hostile or unbounded input file can't fill /etc/litclock. M3's
        validator caps GIFT_MODE_MESSAGE at 80 chars (litclock-dev#319 lowered from
        280 once the renderer learned to wrap); reset-setup.sh enforces
        the same at write-time via `os.read(fd, 80)` defense-in-depth
        inside the O_NOFOLLOW Python block (litclock-dev#316)."""
        gift_block_start = reset_sh_content.find('if [[ "$GIFT_MODE" == "true" ]]; then')
        gift_block_end = reset_sh_content.find('echo "=', gift_block_start)
        gift_block = reset_sh_content[gift_block_start:gift_block_end]
        assert "os.read(fd, 80)" in gift_block, (
            "welcome-message write must enforce 80-char ceiling (matches "
            "GIFT_MODE_MESSAGE_MAX_LEN in src/config.py post-litclock-dev#319)"
        )

    def test_welcome_message_rejects_symlinks(self, reset_sh_content):
        """litclock-dev#280 + litclock-dev#316 /review: source file (handed in via --message-file)
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
            "(litclock-dev#316 /review CRITICAL finding)"
        )

    def test_no_message_file_clears_stale_welcome_message(self, reset_sh_content):
        """litclock-dev#280: if a previous --gift-mode run set a personalized message and
        the next run doesn't pass --message-file, the stale message must NOT
        leak into the new gift-mode session. Explicit absence = default text."""
        gift_block_start = reset_sh_content.find('if [[ "$GIFT_MODE" == "true" ]]; then')
        gift_block_end = reset_sh_content.find('echo "=', gift_block_start)
        gift_block = reset_sh_content[gift_block_start:gift_block_end]
        assert "rm -f" in gift_block and ".welcome-message" in gift_block, (
            "absent --message-file must clear any prior .welcome-message"
        )


class TestRebootHintFile:
    """Issue litclock-dev#282 — --reboot must signal shutdown-splash.sh to paint
    'Restarting...' instead of 'Powered Off'. The hint write is hardened
    against symlink TOCTOU + cancel/abort cleanup per /review of PR litclock-dev#304."""

    HINT_PATH = "/run/litclock/shutdown-action"
    HINT_TMP_PATTERN = ".litclock-hint.XXXXXX"
    HINT_WRITE_GUARD = 'if [[ "$DO_REBOOT" == "true" ]]'

    def _hint_block(self, content: str) -> str:
        """Slice the content to just the DO_REBOOT-guarded hint write block.
        Anchored on the `# Issue litclock-dev#282:` comment header (unique) and the
        `# Step 1:` services-stop marker so we don't accidentally pick up
        the end-of-script `elif [[ $DO_REBOOT ]]` reboot branch."""
        start = content.find("# Issue litclock-dev#282:")
        assert start != -1, "`# Issue litclock-dev#282:` hint-block header missing"
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
        # Anchor on the unique litclock-dev#282 comment header (the `if [[ $DO_REBOOT ]]`
        # string also appears in the end-of-script reboot branch).
        block_idx = reset_sh_content.find("# Issue litclock-dev#282:")
        stop_idx = reset_sh_content.find("systemctl stop litclock-shutdown.service")
        assert block_idx != -1, "litclock-dev#282 hint-block header missing"
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
    """EPIC litclock-dev#383 PR2 (litclock-dev#388): a reset returns the device to fresh-setup state, so
    the lingering .handoff-complete must be cleared too — otherwise the
    post-WiFi handoff splash would be skipped on re-provision (handoff is active
    only when .setup-complete exists AND .handoff-complete is absent)."""
    src = RESET_SH.read_text()
    assert 'rm -f "$CONFIG_DIR/.handoff-complete"' in src


def test_defaults_include_weather_location_mode_and_ip_country():
    """litclock-dev#337 A3 + /review testing-gap: gift-mode reset must include the new
    MODE + IP_COUNTRY defaults. Without these, a gift-recipient whose
    first-boot IP-geo fails would inherit the gifter's stale MODE=specific
    AND no IP_COUNTRY baseline — on-boot reresolve would never fire."""
    from pathlib import Path

    content = (Path(__file__).parent.parent / "scripts/reset-setup.sh").read_text()
    assert "export WEATHER_LOCATION_MODE=auto" in content, (
        "litclock-dev#337 A3: reset-setup.sh DEFAULTS must include "
        "MODE=auto")
    assert "export WEATHER_IP_COUNTRY=" in content, (
        "litclock-dev#337 A3: reset-setup.sh DEFAULTS must include WEATHER_IP_COUNTRY= (empty)"
    )


# ── litclock-dev#387: prepare-for-gift pi->root hardening ────────────────────────────────


class TestPrivilegeHardening387:
    """litclock-prepare-for-gift.service runs reset-setup.sh as root and pi can
    `systemctl start` it via sudoers/020, so the script + everything it executes
    as root must live outside the pi-writable repo."""

    SERVICE = REPO_ROOT / "systemd" / "litclock-prepare-for-gift.service"
    PI_GEN = REPO_ROOT / "pi-gen" / "stage3" / "03-install-services" / "00-run.sh"
    UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"

    def test_service_execstart_is_root_owned_copy(self):
        body = self.SERVICE.read_text()
        assert "ExecStart=/usr/local/lib/litclock/reset-setup.sh" in body, (
            "prepare-for-gift.service must exec the ROOT-OWNED reset-setup.sh copy (litclock-dev#387)"
        )
        assert "ExecStart=/home/pi/litclock/scripts/reset-setup.sh" not in body, (
            "must NOT exec the pi-writable repo copy as root (litclock-dev#387 pi->root)"
        )

    def test_gift_message_uses_system_python_not_venv(self, reset_sh_content):
        # Running the pi-writable venv interpreter as root is a pi->root vector;
        # the stdlib-only heredoc uses the root-owned system python instead.
        assert "/usr/bin/python3 - " in reset_sh_content, (
            "gift-message processing must use the system python3 "
            "(litclock-dev#387)")
        assert '"$INSTALL_DIR/venv/bin/python3" - "$GIFT_MESSAGE_FILE"' not in reset_sh_content, (
            "must NOT run the pi-writable venv interpreter as root (litclock-dev#387)"
        )

    def test_sources_state_lib_relative_to_self(self, reset_sh_content):
        # So the root-owned copy sources the root-owned lib/state.sh beside it.
        assert '"$_THIS_SCRIPT_DIR/lib/state.sh"' in reset_sh_content, (
            "reset-setup must source lib/state.sh relative to its own dir so the "
            "installed root-owned copy sources the root-owned lib (litclock-dev#387)"
        )

    def test_install_paths_ship_reset_setup_and_state_root_owned(self):
        for src, name in ((self.PI_GEN, "pi-gen"), (self.UPDATE_SH, "update.sh")):
            body = src.read_text()
            assert "reset-setup.sh" in body and "/usr/local/lib/litclock" in body, (
                f"{name} must install reset-setup.sh root-owned to /usr/local/lib/litclock "
                "(litclock-dev#387)"
            )
            assert "/usr/local/lib/litclock/lib" in body, (
                f"{name} must install the root-owned lib/state.sh dir "
                "(litclock-dev#387)")
            assert "lib/state.sh" in body, f"{name} must install state.sh alongside (litclock-dev#387)"


class TestFactoryResetStrictEnvWipe:
    """litclock-dev#510 review (Codex): the PWA Factory reset must be fail-closed on a
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
        # The destructive WiFi wipe (Step 7) and BOTH terminal actions must come
        # AFTER the guard so a failed wipe leaves WiFi up + never powers off /
        # reboots from a stale-config state. litclock-dev#627: the factory path
        # now ends at `poweroff`, so pin the guard-before-poweroff invariant too
        # — not just guard-before-reboot (the now-dev-only branch).
        wifi_idx = reset_sh_content.find("Step 7", guard_idx)
        reboot_idx = reset_sh_content.find("systemctl reboot", guard_idx)
        poweroff_idx = reset_sh_content.find("\n    poweroff", guard_idx)
        assert wifi_idx != -1 and guard_idx < wifi_idx, "guard must precede the WiFi wipe"
        assert reboot_idx != -1 and guard_idx < reboot_idx, "guard must precede the reboot"
        assert poweroff_idx != -1 and guard_idx < poweroff_idx, "guard must precede the poweroff (litclock-dev#627)"

    def test_plain_reset_stays_best_effort(self, reset_sh_content):
        """Default STRICT_ENV_WIPE=false — a plain/dev reset must NOT abort on an
        env-wipe failure (behavior unchanged for the shell/dev path)."""
        assert "STRICT_ENV_WIPE=false" in reset_sh_content

    def test_reset_unit_passes_strict_env_wipe(self):
        unit = (REPO_ROOT / "systemd" / "litclock-reset.service").read_text()
        assert "--strict-env-wipe" in unit, "litclock-reset.service must pass --strict-env-wipe"
        # litclock-dev#627: factory reset POWERS OFF, not reboots. --reboot must
        # NOT be present, or a mover would find the clock back on in a hotspot.
        assert "--wipe-wifi" in unit and "--poweroff" in unit
        assert "--reboot" not in unit, "factory reset must power off (litclock-dev#627), not reboot"


class TestPowerOffMode:
    """litclock-dev#627 — Factory reset powers OFF instead of rebooting. After a
    full wipe the next power-on runs first-boot regardless, so rebooting into a
    live hotspot is wrong when the owner is packing the clock up to move or
    handing it on. --poweroff powers off with NO gift welcome splash."""

    def _shutdown_branch(self, content: str) -> str:
        # The end-of-script terminal-action if/elif chain, from the gift branch
        # (which powers off) through the final else.
        start = content.rfind('if [[ "$GIFT_MODE" == "true" ]]')
        assert start != -1, "end-of-script shutdown branch not found"
        return content[start:]

    def test_poweroff_flag_parsed(self, reset_sh_content):
        assert "--poweroff) DO_POWEROFF=true" in reset_sh_content

    def test_reboot_and_poweroff_are_mutually_exclusive(self, reset_sh_content):
        guard = 'if [[ "$DO_REBOOT" == "true" && "$DO_POWEROFF" == "true" ]]'
        idx = reset_sh_content.find(guard)
        assert idx != -1, "mutual-exclusion guard missing"
        # Must fail CLOSED, not warn-and-fall-through (branch order would then
        # silently pick the terminal action).
        exit_idx = reset_sh_content.find("exit 1", idx)
        assert exit_idx != -1 and (exit_idx - idx) < 200, "mutual-exclusion guard must exit 1"

    def test_poweroff_implies_strict_env_wipe(self, reset_sh_content):
        # /review: a bare `--poweroff` must not power off best-effort — it sets
        # STRICT_ENV_WIPE so a failed config wipe aborts before power-off.
        assert "--poweroff) DO_POWEROFF=true; STRICT_ENV_WIPE=true" in reset_sh_content

    def test_end_of_script_has_a_poweroff_branch_before_reboot(self, reset_sh_content):
        branch = self._shutdown_branch(reset_sh_content)
        po_idx = branch.find('elif [[ "$DO_POWEROFF" == "true" ]]')
        assert po_idx != -1, "no --poweroff terminal branch"
        # It must run `poweroff` (not reboot) before the DO_REBOOT branch.
        reboot_idx = branch.find('elif [[ "$DO_REBOOT" == "true" ]]')
        assert reboot_idx != -1 and po_idx < reboot_idx, "poweroff branch must precede the reboot branch"
        segment = branch[po_idx:reboot_idx]
        assert "poweroff" in segment, "poweroff branch must call poweroff"
        assert "systemctl reboot" not in segment, "poweroff branch must NOT reboot"

    def test_poweroff_clears_the_stale_reboot_hint_never_writes_one(self, reset_sh_content):
        # The reboot-hint write (litclock-dev#282) steers shutdown-splash to 'Restarting…'.
        # --poweroff must NEVER write it, and must actively CLEAR any stale hint
        # (litclock-dev#627 /review) so a factory-reset power-off always paints
        # 'Powered Off'.
        start = reset_sh_content.find("# Issue litclock-dev#282:")
        end = reset_sh_content.find("# Step 1:", start)
        hint_block = reset_sh_content[start:end]
        # The reboot writer stays gated on DO_REBOOT.
        reboot_part, _, poweroff_part = hint_block.partition('elif [[ "$DO_POWEROFF" == "true" ]]')
        assert 'if [[ "$DO_REBOOT" == "true" ]]' in reboot_part
        assert "printf 'reboot" in reboot_part
        # The poweroff branch CLEARS the hint and does NOT write 'reboot'.
        assert poweroff_part, "poweroff branch missing from the hint block"
        assert "rm -f /run/litclock/shutdown-action" in poweroff_part
        assert "printf 'reboot" not in poweroff_part

    def test_poweroff_does_not_write_the_gift_welcome_marker(self, reset_sh_content):
        # The welcome-splash marker is written only under GIFT_MODE. A --poweroff
        # (non-gift) reset must NOT paint a 'welcome' message — it's a
        # relocation / non-gift handoff.
        marker_guard = 'if [[ "$GIFT_MODE" == "true" ]]'
        # The FIRST such guard is the early marker write (pre service-stop).
        first = reset_sh_content.find(marker_guard)
        assert first != -1
        # DO_POWEROFF must not appear inside the marker-write block.
        block = reset_sh_content[first : first + 600]
        assert "DO_POWEROFF" not in block


class TestHotspotPasswordResetSemantics:
    """litclock-dev#620 — the persisted hotspot password survives a plain reset
    and a WiFi reset ON PURPOSE (the owner's phone has the network saved, and a
    changed password is a trap -- how badly it traps an Android user was
    measured twice with disagreeing results, so litclock-dev#648 treats the
    "no user-discoverable recovery" version as unproven; the argument for
    persisting does not depend on it),
    but gift mode MUST rotate it: the recipient loses nothing, and the gifter
    must not retain a working key to the recipient's setup hotspot.

    These EXECUTE the real block rather than grepping for it — a structural
    assertion that never runs the code is not a guard (the litclock-dev#638 lesson). The
    harness also sources the script's OWN `STATE_DIR=` line instead of
    inventing one, so dropping that assignment fails the test rather than
    silently degrading `rm -f` to a no-op path.
    """

    @staticmethod
    def _rotation_fn(content):
        """Lift `rotate_hotspot_password_for_handoff()` whole.

        litclock-dev#662: the previous helper lifted an ad-hoc echo..done span and
        asserted NOTHING about what it lifted, so deleting the fail-closed gate
        left the span still extractable and the tests still green. Verify the
        span's load-bearing parts, the way the prepare-for-cloning harness does.
        """
        start = content.find("rotate_hotspot_password_for_handoff() {")
        assert start != -1, "rotate_hotspot_password_for_handoff() is missing"
        end = content.find("\n}\n", start)
        assert end != -1, "could not find the end of rotate_hotspot_password_for_handoff()"
        fn = content[start : end + 3]
        for required, why in (
            ("rm -f", "the removal itself"),
            ('-L "$STATE_DIR/hotspot-password"', "the dangling-symlink survivor check (litclock-dev#663)"),
            ("compgen -G", "the orphaned-staging-file sweep"),
            ("exit 1", "the fail-closed abort"),
            ("do NOT pass this device on", "the operator warning"),
        ):
            assert required in fn, f"rotation function lost {why} ({required!r})"
        return fn

    @staticmethod
    def _terminal_branch(content):
        """Lift the REAL terminal if/elif/else chain, not a reconstruction.

        litclock-dev#662: the old harness substituted the literal
        `: # non-gift: rotation must not run` whenever gift_mode was false, so
        the negative test executed no script code at all and could not fail.
        The branch condition has to be INSIDE the lifted span for a
        parametrised test to mean anything.

        Anchored on the LAST `if [[ "$GIFT_MODE" == "true" ]]` — there are three
        in this script and the first two are the marker writer and the wipe
        summary, so `.find()` would silently lift the wrong one.
        """
        hoist = content.find('if [[ "$GIFT_MODE" != "true" && "$WIPE_WIFI" == "true" ]]; then')
        assert hoist != -1, (
            "litclock-dev#666: the hoisted non-gift rotation guard is missing. It decides "
            "rotation for every non-gift path, so it MUST be inside the lifted span or a "
            "parametrised test proves nothing (the litclock-dev#662 rule)."
        )
        anchor = content.rfind('if [[ "$GIFT_MODE" == "true" ]]; then')
        assert anchor != -1, "terminal GIFT_MODE branch is missing"
        assert hoist < anchor, "the non-gift rotation must be hoisted ahead of the terminal branch"

        # TWO spans joined, not one contiguous slice. Since /review moved the
        # rotation above Step 7 (so a dying SSH session cannot leave WiFi gone
        # with the old key intact), everything between them includes Step 7's
        # real `rm` of /etc/NetworkManager/system-connections/* — which a test
        # must never execute. Take the decision block and the terminal branch,
        # and nothing in between.
        hoist_end = content.index("\nfi\n", hoist) + len("\nfi\n")
        hoist_block = content[hoist:hoist_end]
        assert "rotate_hotspot_password_for_handoff" in hoist_block, (
            f"lifted rotation guard has no call in it: {hoist_block!r}"
        )
        assert "NetworkManager" not in hoist_block, "lifted span reaches Step 7's WiFi wipe"
        block = hoist_block + "\n" + content[anchor:]
        for required, why in (
            ('elif [[ "$DO_POWEROFF" == "true" ]]', "the --poweroff arm"),
            ('elif [[ "$DO_REBOOT" == "true" ]]', "the --reboot arm"),
            ("poweroff", "the terminal poweroff"),
        ):
            assert required in block, f"terminal branch lost {why} ({required!r})"
        # Count CALL lines, not substring hits — the surrounding comments name the
        # function too, so `.count()` on the raw text reads 3 for 2 calls.
        # The call is now `if ! rotate_hotspot_password_for_handoff; then` —
        # the litclock-dev#719 belt checks the call's own status, because with no password
        # file on disk a `command not found` left nothing for the outcome
        # check to see. Count NAME OCCURRENCES on executed lines, not one
        # blessed spelling (/review litclock-dev#720: the bare-line counter went to zero
        # the moment the call was wrapped).
        calls = [
            ln
            for ln in block.splitlines()
            if not ln.lstrip().startswith("#")
            and re.search(r'(?<![A-Za-z0-9_"])rotate_hotspot_password_for_handoff(?![A-Za-z0-9_(])', ln)
        ]
        assert len(calls) == 2, (
            "litclock-dev#666: exactly two call sites — the hoisted non-gift one (covering "
            "power-off, reboot AND plain finish) and the gift arm's, which sits after the "
            f"litclock-dev#393 abort gate; found {len(calls)} call site(s)"
        )
        return block

    @staticmethod
    def _state_dir_line(content):
        line = next((ln for ln in content.splitlines() if ln.startswith("STATE_DIR=")), None)
        assert line, "reset-setup.sh must define STATE_DIR (the rotation depends on it)"
        return line

    def _run(self, content, tmp_path, gift_mode, do_poweroff="false", do_reboot="false", wipe_wifi="false", extra=""):
        """Execute the real function + the real terminal branch.

        `poweroff`/`systemctl` are shadowed by shell functions so the dispatch
        runs to completion without taking the machine down, and so a test can
        assert WHICH terminal action was reached.
        """
        import subprocess

        state = tmp_path / "state"
        state.mkdir()
        pw = state / "hotspot-password"
        pw.write_text("clockwis\n", encoding="utf-8")
        # A staging file must EXIST for any "staging secrets were swept" assertion
        # to mean anything — without one the glob is empty before the script runs
        # and the assertion passes with the sweep deleted.
        (state / ".hotspot-password.XYZ").write_text("oldsecret\n", encoding="utf-8")
        config = tmp_path / "config"
        config.mkdir()
        program = (
            "set -u  # NOT -e: reset-setup.sh deliberately omits it\n"
            'poweroff() { echo "STUB_POWEROFF"; }\n'
            'systemctl() { echo "STUB_SYSTEMCTL $*"; }\n'
            # The terminal branch calls disable_ssh_for_handoff. It was authored
            # here (#52/#53) and back-ported to the development repo by
            # litclock-dev#657, so BOTH repos have it today — the parity test
            # below passes against the counterpart, which proves it. What the
            # development repo never gained is this stub, so its harness has the
            # same latent gap (filed there). Without the stub every run emitted
            # "command not found" on stderr — swallowed, because the harness
            # deliberately omits `set -e` — which elided the security gate from
            # every behavioural test while they still passed. Re-lost when this
            # file was taken wholesale in the v0.226.0 port.
            'disable_ssh_for_handoff() { echo "STUB_SSH_GATE"; }\n'
            f"GIFT_MODE={gift_mode}\n"
            f"DO_POWEROFF={do_poweroff}\n"
            f"DO_REBOOT={do_reboot}\n"
            f"WIPE_WIFI={wipe_wifi}\n"
            "ENV_WIPE_FAILED=false\n"
            "STRICT_ENV_WIPE=false\n"
            f"CONFIG_DIR={config}\n"
            f"LITCLOCK_STATE_DIR={state}\n"
            f"{self._state_dir_line(content)}\n"
            'RED=""\nGREEN=""\nYELLOW=""\nNC=""\n'
            f"{extra}\n"
            f"{self._rotation_fn(content)}\n"
            f"{self._terminal_branch(content)}"
        )
        result = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=30)
        return pw, result, state

    def test_gift_mode_rotates_the_password(self, reset_sh_content, tmp_path):
        """Forced with wipe_wifi=false on purpose: the gift arm's call is
        UNCONDITIONAL, and passing wipe_wifi=true here would assert a coupling
        that does not exist (--gift-mode sets WIPE_WIFI itself, so the test
        could not tell an unconditional call from a guarded one).
        """
        pw, result, _ = self._run(reset_sh_content, tmp_path, "true", wipe_wifi="false")
        assert result.returncode == 0, result.stderr
        assert "STUB_POWEROFF" in result.stdout, "gift mode must reach poweroff"
        assert "STUB_SSH_GATE" in result.stdout, "the SSH gate must run at all on this arm"
        assert result.stdout.index("STUB_SSH_GATE") < result.stdout.index("STUB_POWEROFF"), (
            "the SSH gate must run before poweroff"
        )
        assert not pw.exists(), "gift mode must rotate the hotspot password for the new owner"

    def test_pwa_factory_reset_rotates_the_password(self, reset_sh_content, tmp_path):
        """litclock-dev#660 — the PWA "Factory reset" card runs
        `reset-setup.sh --wipe-wifi --strict-env-wipe --poweroff --yes` via
        litclock-reset.service.

        WiFi is wiped, so the next power-on comes up in the setup hotspot — and
        before litclock-dev#660 it came up broadcasting LitClock-Setup with the PREVIOUS
        owner's permanent key, surviving every reset the new owner later
        performed. v0.223.0 had no such leak because the key regenerated every
        provisioning cycle, which makes this a REGRESSION introduced by litclock-dev#620
        rather than a pre-existing gap.
        """
        pw, result, state = self._run(reset_sh_content, tmp_path, "false", do_poweroff="true", wipe_wifi="true")
        assert result.returncode == 0, result.stderr
        assert "STUB_POWEROFF" in result.stdout, "the --poweroff arm must reach poweroff"
        assert "STUB_SSH_GATE" in result.stdout, "the SSH gate must run at all on this arm"
        assert result.stdout.index("STUB_SSH_GATE") < result.stdout.index("STUB_POWEROFF"), (
            "the SSH gate must run before poweroff on this arm too (litclock-dev#636)"
        )
        assert not pw.exists(), (
            "litclock-dev#660: --wipe-wifi --poweroff comes back up in the setup hotspot, "
            "so it MUST clear the persisted setup-WiFi key"
        )
        assert not list(state.glob(".hotspot-password.*")), "staging secrets must be swept here too"

    def test_keep_wifi_with_poweroff_keeps_the_password(self, reset_sh_content, tmp_path):
        """`--keep-wifi --poweroff` is the "same owner, moved house" path.

        Renamed from test_hand_run_poweroff_without_wipe_wifi_keeps_the_password
        (/review): since litclock-dev#666 a bare `--poweroff` DOES wipe, because
        the wipe is the default and --poweroff does not opt out. The old name
        described a CLI invocation that no longer exists, so it asserted correct
        behaviour under a scenario nobody can reach — the state is now only
        produced by --keep-wifi.

        The behaviour under test is unchanged and still right: the WiFi survives,
        so the clock boots straight back onto its own network and never starts a
        setup network — nothing for a rotated key to protect, and rotating would
        strand the owner's phone for nothing. The bench QA doc pins this as
        "same owner, moved house" and asserts the password is UNCHANGED.
        """
        pw, result, _ = self._run(reset_sh_content, tmp_path, "false", do_poweroff="true", wipe_wifi="false")
        assert result.returncode == 0, result.stderr
        assert "STUB_POWEROFF" in result.stdout, "the --poweroff arm must still reach poweroff"
        assert pw.exists(), "--keep-wifi --poweroff is the same-owner path and must PRESERVE the key"
        assert pw.read_text(encoding="utf-8").strip() == "clockwis"

    def test_the_real_cli_default_reaches_rotation_end_to_end(self, reset_sh_content, tmp_path):
        """Integration, because the two halves were only ever tested apart
        (/review). The flag parser proves WIPE_WIFI defaults true; the terminal
        tests prove WIPE_WIFI=true rotates. Neither proves the DEFAULT rotates,
        because the harness injects WIPE_WIFI itself — so flipping the
        production default could have left both suites green.

        This runs the real flag-parsing loop with no arguments and feeds its
        result straight into the real terminal branch.
        """
        import subprocess

        state = tmp_path / "state"
        state.mkdir()
        pw = state / "hotspot-password"
        pw.write_text("clockwis\n", encoding="utf-8")
        config = tmp_path / "config"
        config.mkdir()

        content = reset_sh_content
        default_line = next(ln for ln in content.splitlines() if ln.startswith("WIPE_WIFI="))
        parse_start = content.index("while [[ $# -gt 0 ]]; do")
        parse_end = content.index("done", parse_start) + len("done")

        program = (
            "set -u\n"
            'poweroff() { echo "STUB_POWEROFF"; }\n'
            'systemctl() { echo "STUB_SYSTEMCTL $*"; }\n'
            # The terminal branch calls disable_ssh_for_handoff. It was authored
            # here (#52/#53) and back-ported to the development repo by
            # litclock-dev#657, so BOTH repos have it today — the parity test
            # below passes against the counterpart, which proves it. What the
            # development repo never gained is this stub, so its harness has the
            # same latent gap (filed there). Without the stub every run emitted
            # "command not found" on stderr — swallowed, because the harness
            # deliberately omits `set -e` — which elided the security gate from
            # every behavioural test while they still passed. Re-lost when this
            # file was taken wholesale in the v0.226.0 port.
            # (Defensive here: this arm runs the parser with no flags, so it falls
            # to the "Reboot to enter setup mode" branch and never reaches the
            # gate. Kept so the harness stays uniform if that ever changes.)
            'disable_ssh_for_handoff() { echo "STUB_SSH_GATE"; }\n'
            "AUTO_YES=false\nDO_REBOOT=false\nDO_POWEROFF=false\n"
            "STRICT_ENV_WIPE=false\nGIFT_MODE=false\nGIFT_MESSAGE_FILE=''\nENV_WIPE_FAILED=false\n"
            f"{default_line}\n"
            f"{content[parse_start:parse_end]}\n"
            f"CONFIG_DIR={config}\n"
            f"LITCLOCK_STATE_DIR={state}\n"
            f"{self._state_dir_line(content)}\n"
            'RED=""\nGREEN=""\nYELLOW=""\nNC=""\n'
            f"{self._rotation_fn(content)}\n"
            f"{self._terminal_branch(content)}"
        )
        # No arguments: the plain `sudo reset-setup.sh` a person actually types.
        result = subprocess.run(["bash", "-c", program, "bash"], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert not pw.exists(), (
            "a no-argument factory reset did not rotate the setup network's password. "
            "The parser default and the terminal branch each pass in isolation, so only "
            "this end-to-end path catches the two disagreeing."
        )

    def test_gift_mode_also_sweeps_orphaned_staging_files(self, reset_sh_content, tmp_path):
        """A SIGKILL between mkstemp and os.replace leaves a 0600 staging file
        holding a real past password; an exact-name rm would ship it."""
        import subprocess

        state = tmp_path / "state"
        state.mkdir()
        (state / "hotspot-password").write_text("clockwis\n", encoding="utf-8")
        (state / ".hotspot-password.XYZ").write_text("oldsecret\n", encoding="utf-8")
        program = (
            "set -u  # NOT -e: reset-setup.sh deliberately omits it\n"
            f"LITCLOCK_STATE_DIR={state}\n{self._state_dir_line(reset_sh_content)}\n"
            'RED=""\nGREEN=""\nNC=""\n'
            f"{self._rotation_fn(reset_sh_content)}\n"
            "rotate_hotspot_password_for_handoff\n"
        )
        r = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        assert not list(state.glob(".hotspot-password.*")), "orphaned staging secrets must be swept"

    @pytest.mark.parametrize(
        "shape",
        ["regular_file", "dangling_symlink", "directory"],
        ids=["regular-file", "dangling-symlink", "directory"],
    )
    def test_survivor_check_sees_every_kind_of_surviving_entry(self, reset_sh_content, tmp_path, shape):
        """litclock-dev#663 parity: prepare-for-cloning.sh got a five-shape
        survivor test and this copy got none, which is how the missing `-L`
        survived here after being fixed there.

        A dangling symlink is the sharp case: `-e` FOLLOWS the link and is false
        for a broken one, so an `-e`-only check reports success for an entry
        that is demonstrably still there.
        """
        import subprocess

        state = tmp_path / "state"
        state.mkdir()
        pw = state / "hotspot-password"
        if shape == "regular_file":
            pw.write_text("clockwis\n", encoding="utf-8")
        elif shape == "dangling_symlink":
            pw.symlink_to(tmp_path / "does-not-exist")
        else:
            pw.mkdir()
            (pw / "occupant").write_text("blocks rmdir\n", encoding="utf-8")

        # Make the unlink fail for root and non-root alike where possible; a
        # non-empty directory does this without relying on permissions.
        if shape != "directory":
            state.chmod(0o500)
        try:
            program = (
                "set -u\n"
                f"LITCLOCK_STATE_DIR={state}\n{self._state_dir_line(reset_sh_content)}\n"
                'RED=""\nGREEN=""\nNC=""\n'
                f"{self._rotation_fn(reset_sh_content)}\n"
                "rotate_hotspot_password_for_handoff\n"
            )
            r = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=30)
            if shape != "directory" and os.access(str(state), os.W_OK):
                # litclock-dev#662: root can unlink a file or a dangling symlink
                # regardless of directory permissions, so those two shapes are
                # genuinely unblockable here. The "directory" shape is NOT
                # skipped — `rm -f` fails on a directory for root and non-root
                # alike — so the fail-closed gate keeps executing coverage even
                # in a root container. test_at_least_one_shape_is_root_proof
                # pins that, because "all shapes skipped" would look identical
                # to "all shapes passed".
                pytest.skip("running as root — unlink cannot be blocked for this shape")
            assert r.returncode != 0, (
                f"a surviving {shape} at the password path must fail the rotation closed, "
                "not print done — the invariant is that NO entry survives, whatever it points at"
            )
            assert "could not be removed from" in r.stdout + r.stderr
        finally:
            state.chmod(0o700)

    def test_at_least_one_shape_is_root_proof(self, reset_sh_content, tmp_path):
        """The fail-closed gate must keep executing coverage in a root container.

        litclock-dev#662: two guards in this suite self-skip when the runner can
        defeat the permission trick, and they were the only executing coverage
        of the loudest failure paths. CI is ubuntu-latest so they run there, but
        in a root container they vanish SILENTLY — and an all-skipped run reads
        exactly like an all-passed one.

        A non-empty directory at the password path makes `rm -f` fail for root
        and non-root alike, so this asserts the gate fires with no permission
        trick and no skip, whoever is running it.
        """
        import subprocess

        state = tmp_path / "state"
        state.mkdir()
        occupied = state / "hotspot-password"
        occupied.mkdir()
        (occupied / "keep").write_text("not empty\n", encoding="utf-8")

        program = (
            "set -u\n"
            f"LITCLOCK_STATE_DIR={state}\n{self._state_dir_line(reset_sh_content)}\n"
            'RED=""\nGREEN=""\nNC=""\n'
            f"{self._rotation_fn(reset_sh_content)}\n"
            "rotate_hotspot_password_for_handoff\n"
        )
        r = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=30)

        assert r.returncode != 0, (
            "the rotation reported success while an entry survived at the password path — and this "
            "shape needs no permission trick, so the gate has no executing coverage anywhere"
        )
        assert "could not be removed from" in r.stdout + r.stderr

    def test_gift_mode_FAILS_CLOSED_when_the_password_cannot_be_removed(self, reset_sh_content, tmp_path):
        """`rm -f` returns 0 for a missing file but not for a read-only mount —
        the Pi's most common degradation. Without verification the operator
        reads 'done' and ships a device whose key the gifter still knows."""
        import subprocess

        state = tmp_path / "state"
        state.mkdir()
        # litclock-dev#662: a non-empty DIRECTORY at the password path makes
        # `rm -f` fail for root and non-root alike, so this guard no longer
        # self-skips in a root container — where it was previously the ONLY
        # executing coverage of the fail-closed abort.
        pw = state / "hotspot-password"
        pw.mkdir()
        (pw / "occupant").write_text("blocks rmdir\n", encoding="utf-8")
        program = (
            "set -u  # NOT -e: reset-setup.sh deliberately omits it\n"
            f"LITCLOCK_STATE_DIR={state}\n{self._state_dir_line(reset_sh_content)}\n"
            'RED=""\nGREEN=""\nNC=""\n'
            f"{self._rotation_fn(reset_sh_content)}\n"
            "rotate_hotspot_password_for_handoff\n"
        )
        r = subprocess.run(
            ["bash", "-c", program],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "LC_ALL": "C"},
        )
        assert r.returncode != 0, "a failed rotation must abort handoff prep, not print done"
        assert "could not be removed from" in r.stdout + r.stderr
        # litclock-dev#663: rm's own diagnosis must reach the operator —
        # "Is a directory" / "Read-only file system" / "Operation not permitted"
        # need different remedies and "could not remove" distinguishes none.
        assert "Is a directory" in r.stdout or "directory" in r.stdout.lower(), (
            f"rm's cause must be surfaced, got: {r.stdout!r}"
        )

    def test_rotation_call_is_after_the_abort_gate(self, reset_sh_content):
        """A gift prep that ABORTS leaves the device with its current owner —
        rotating there would drop that owner into the trap litclock-dev#620 removes.

        Anchors on the CALL inside the terminal branch, not on a string that
        also appears in the function definition further up the file. The
        original version of this test used `.index("Regenerating hotspot
        password")`, which after litclock-dev#660's refactor resolved to the function body
        and inverted the comparison.
        """
        block = self._terminal_branch(reset_sh_content)
        gate = block.index('if [[ "$ENV_WIPE_FAILED" == "true" ]]')
        # The GIFT call specifically. Since litclock-dev#666 the span also holds
        # the hoisted non-gift call, which precedes the gate by design and is
        # guarded on GIFT_MODE != true, so it can never fire on this path.
        rotate = block.index("if ! rotate_hotspot_password_for_handoff; then", gate)
        assert gate < rotate, "gift-mode rotation must come AFTER the litclock-dev#393 env-wipe abort gate"

    def test_keep_wifi_preserves_the_password_on_a_plain_reset(self, reset_sh_content, tmp_path):
        """`--keep-wifi` is the ONLY path that preserves either password now
        (litclock-dev#666, owner decision).

        It preserves BOTH, and that is coherent rather than a special case: if
        the WiFi survives, the clock returns to its own network and never raises
        a setup hotspot, so there is no setup network for a rotated key to
        protect and rotating would strand the owner's phone for nothing. This is
        the old "same owner, moved house" case, now reached deliberately by a
        flag instead of by accident through a flag combination.

        litclock-dev#662: this previously executed NO script code — the harness
        substituted a shell no-op whenever gift_mode was false, so it wrote the
        file, ran nothing, and asserted the file existed.
        """
        pw, result, _ = self._run(reset_sh_content, tmp_path, "false", wipe_wifi="false")
        assert result.returncode == 0, result.stderr
        assert "STUB_POWEROFF" not in result.stdout, "a plain reset must not power off"
        assert pw.exists(), "--keep-wifi must PRESERVE the hotspot password"
        assert pw.read_text(encoding="utf-8").strip() == "clockwis"

    def test_keep_wifi_preserves_the_password_on_the_reboot_path(self, reset_sh_content, tmp_path):
        """Same rule with `--reboot`: the clock stays put and comes straight
        back up on its own network."""
        pw, result, _ = self._run(reset_sh_content, tmp_path, "false", do_reboot="true", wipe_wifi="false")
        assert result.returncode == 0, result.stderr
        assert "STUB_SYSTEMCTL reboot" in result.stdout, "the --reboot arm must reach systemctl reboot"
        assert pw.exists(), "--keep-wifi --reboot must PRESERVE the hotspot password"
        assert pw.read_text(encoding="utf-8").strip() == "clockwis"

    @pytest.mark.parametrize(
        "label,kwargs,expect_in_stdout",
        [
            ("plain", {}, None),
            ("reboot", {"do_reboot": "true"}, "STUB_SYSTEMCTL reboot"),
            ("poweroff", {"do_poweroff": "true"}, "STUB_POWEROFF"),
        ],
    )
    def test_the_default_reset_rotates_on_every_terminal_path(
        self, reset_sh_content, tmp_path, label, kwargs, expect_in_stdout
    ):
        """litclock-dev#666/litclock-dev#664, the owner's rule: a factory reset removes BOTH
        saved passwords.

        Parametrised across all three terminal exits on purpose. The previous
        design rotated only in the power-off arm, so a reset that rebooted, or
        one that just finished and left the operator to power-cycle, raised the
        same setup hotspot on the next boot carrying the PREVIOUS owner's
        permanent key. Which exit the reset happened to take is not something a
        credential's lifetime should depend on.

        WIPE_WIFI defaults TRUE now, so these are the no-flag, `--reboot` and
        `--poweroff` invocations respectively.
        """
        pw, result, _ = self._run(reset_sh_content, tmp_path, "false", wipe_wifi="true", **kwargs)
        assert result.returncode == 0, result.stderr
        if expect_in_stdout:
            assert expect_in_stdout in result.stdout, f"the {label} arm was not reached"
        assert not pw.exists(), (
            f"the {label} reset did NOT rotate the hotspot key. The WiFi is gone, so the next "
            "boot raises a setup hotspot — with the previous owner's permanent key on it."
        )

    def test_survivor_check_detects_a_dangling_symlink_regardless_of_uid(self, reset_sh_content, tmp_path):
        """Root-proof coverage of the litclock-dev#663 `-L` clause.

        The parametrised shape test blocks the unlink with a 0500 parent, which
        root ignores — so under root the dangling-symlink case skips and `-L`
        has NO executing coverage at all. Here the survivor CONDITION is
        evaluated directly against a dangling symlink, which needs no failed
        unlink and therefore behaves identically for root and non-root.
        """
        import subprocess

        state = tmp_path / "state"
        state.mkdir()
        (state / "hotspot-password").symlink_to(tmp_path / "does-not-exist")
        fn = self._rotation_fn(reset_sh_content)
        program = (
            f"STATE_DIR={state}\n"
            'if [[ -e "$STATE_DIR/hotspot-password" || -L "$STATE_DIR/hotspot-password" ]]; '
            "then echo SURVIVOR; else echo CLEAR; fi\n"
        )
        r = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=30)
        assert "SURVIVOR" in r.stdout, "sanity: a dangling symlink must read as a survivor"
        # And the shipped function must use that same two-clause form.
        assert '-L "$STATE_DIR/hotspot-password"' in fn, (
            "litclock-dev#663: `-e` alone FOLLOWS symlinks and is false for a dangling one, "
            "so a failed unlink of one would report success"
        )
        bare_e_only = '[[ -e "$STATE_DIR/hotspot-password" ]] ||' in fn
        assert not bare_e_only, "the survivor check regressed to -e only"

    def test_a_surviving_staging_file_alone_fails_closed(self, reset_sh_content, tmp_path):
        """The compgen half of the survivor check had no behavioural coverage.

        The sweep test uses a writable state dir, so `rm` succeeds and the
        survivor branch never fires — it exercises rm's glob, not the check.
        Here only a STAGING entry survives, and it is a non-empty directory so
        the unlink fails for root and non-root alike.
        """
        import subprocess

        state = tmp_path / "state"
        state.mkdir()
        staging = state / ".hotspot-password.XYZ"
        staging.mkdir()
        (staging / "occupant").write_text("blocks rmdir\n", encoding="utf-8")
        program = (
            "set -u\n"
            f"LITCLOCK_STATE_DIR={state}\n{self._state_dir_line(reset_sh_content)}\n"
            'RED=""\nGREEN=""\nNC=""\n'
            f"{self._rotation_fn(reset_sh_content)}\n"
            "rotate_hotspot_password_for_handoff\n"
        )
        r = subprocess.run(
            ["bash", "-c", program],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "LC_ALL": "C"},
        )
        assert r.returncode != 0, "a surviving staging file holds a real past PSK and must fail the rotation closed"
        assert "could not be removed from" in r.stdout + r.stderr

    @pytest.mark.parametrize(
        ("gift", "poweroff", "wipe"),
        [("true", "false", "false"), ("false", "true", "true")],
        ids=["gift-mode", "pwa-factory-reset"],
    )
    def test_a_failed_rotation_never_reaches_poweroff(self, reset_sh_content, tmp_path, gift, poweroff, wipe):
        """THE safety contract, and it had no test.

        Every other fail-closed test calls the function as the last line of its
        harness program, so the script's exit status IS the function's and the
        assertion cannot distinguish `exit 1` from `return 1`. In production
        there is no `set -e`, so a `return 1` would fall through to
        `echo "Powering off..."; poweroff` and ship the previous owner's key
        immediately after printing FAILED.

        This runs the REAL terminal branch with an unremovable password and
        asserts the device never powers off.
        """
        import subprocess

        state = tmp_path / "state"
        state.mkdir()
        pw = state / "hotspot-password"
        pw.mkdir()
        (pw / "occupant").write_text("blocks rmdir\n", encoding="utf-8")
        config = tmp_path / "config"
        config.mkdir()
        program = (
            "set -u\n"
            'poweroff() { echo "STUB_POWEROFF"; }\n'
            'systemctl() { echo "STUB_SYSTEMCTL $*"; }\n'
            # The terminal branch calls disable_ssh_for_handoff. It was authored
            # here (#52/#53) and back-ported to the development repo by
            # litclock-dev#657, so BOTH repos have it today — the parity test
            # below passes against the counterpart, which proves it. What the
            # development repo never gained is this stub, so its harness has the
            # same latent gap (filed there). Without the stub every run emitted
            # "command not found" on stderr — swallowed, because the harness
            # deliberately omits `set -e` — which elided the security gate from
            # every behavioural test while they still passed. Re-lost when this
            # file was taken wholesale in the v0.226.0 port.
            'disable_ssh_for_handoff() { echo "STUB_SSH_GATE"; }\n'
            f"GIFT_MODE={gift}\nDO_POWEROFF={poweroff}\nDO_REBOOT=false\nWIPE_WIFI={wipe}\n"
            "ENV_WIPE_FAILED=false\n"
            f"CONFIG_DIR={config}\nLITCLOCK_STATE_DIR={state}\n"
            f"{self._state_dir_line(reset_sh_content)}\n"
            'RED=""\nGREEN=""\nYELLOW=""\nNC=""\n'
            f"{self._rotation_fn(reset_sh_content)}\n"
            f"{self._terminal_branch(reset_sh_content)}"
        )
        r = subprocess.run(
            ["bash", "-c", program],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "LC_ALL": "C"},
        )
        assert r.returncode != 0, "a failed rotation must abort the script"
        assert "STUB_POWEROFF" not in r.stdout, (
            "the device must NOT power off after a failed rotation — powering off "
            "here ships the previous owner's key, which is the whole point of failing closed"
        )
        assert "STUB_SSH_GATE" not in r.stdout, (
            "and it must NOT disable SSH either: the abort leaves the device with its "
            "CURRENT owner, who needs remote access to diagnose the card. This is the "
            "behavioural half of the ordering the parametrised structural test pins."
        )
        assert "could not be removed from" in r.stdout + r.stderr

    def test_reset_setup_has_no_other_state_dir_deletion(self, reset_sh_content):
        """litclock-dev#662: `test_wifi_reset_does_not_wipe_the_state_dir` scans
        litclock-wifi-reset.sh only, so reset-setup.sh's OWN fourteen-odd `rm`
        calls were never checked against the litclock-dev#620 preserve-across-reset promise.

        The rotation function is excised before scanning — it is the one place
        that is SUPPOSED to delete the key.
        """
        import re as _re

        # Deletions of a NAMED non-secret marker under STATE_DIR that are part
        # of the reset's own bookkeeping. Each entry is a full filename, not a
        # pattern: a glob here would let a future `rm -f "$STATE_DIR"/*` through,
        # which is the whole class this guard exists to stop.
        BOOKKEEPING_MARKERS = (
            # litclock-dev#665 — cleared at the START of a reset so the failure
            # marker means "the most recent attempt failed" rather than "one
            # failed once, ever". Not a credential.
            "reset-failed",
        )

        fn = self._rotation_fn(reset_sh_content)
        rest = reset_sh_content.replace(fn, "")
        for lineno, line in enumerate(rest.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if any(f"$STATE_DIR/{marker}" in line for marker in BOOKKEEPING_MARKERS):
                # Still guarded: the line must name exactly one known marker and
                # nothing else, so it cannot smuggle the password along with it.
                assert "hotspot-password" not in line, line
                continue
            # Not just `rm`: `: > file` truncates, `mv` relocates, `shred -u`
            # and `find -delete` remove. All destroy the litclock-dev#620 promise equally.
            destroys = _re.search(r"\b(rm|shred|unlink|mv|truncate|find)\b", line) or _re.search(
                r">\s*\"?\$STATE_DIR", line
            )
            if destroys and _re.search(r"STATE_DIR|/var/lib/litclock|hotspot-password", line):
                raise AssertionError(
                    f"line {lineno} deletes hotspot state outside the rotation function, "
                    f"breaking the litclock-dev#620 survives-a-plain-reset promise: {line!r}"
                )

    def test_state_dir_is_overridable_like_the_other_scripts(self, reset_sh_content):
        assert 'STATE_DIR="${LITCLOCK_STATE_DIR:-/var/lib/litclock}"' in reset_sh_content

    def test_wifi_reset_does_not_wipe_the_state_dir(self):
        """A substring check for 'hotspot-password' would stay green if the
        script ever did `rm -rf $STATE_DIR` — which destroys the same
        invariant, on the exact moved-house scenario litclock-dev#620 is about."""
        import re as _re

        wifi_reset = (REPO_ROOT / "scripts" / "litclock-wifi-reset.sh").read_text()
        for lineno, line in enumerate(wifi_reset.splitlines(), 1):
            # Not just `rm`: `: > file` truncates, `mv` relocates, `shred -u`
            # and `find -delete` remove. All destroy the litclock-dev#620 promise equally.
            destroys = _re.search(r"\b(rm|shred|unlink|mv|truncate|find)\b", line) or _re.search(
                r">\s*\"?\$STATE_DIR", line
            )
            if destroys and _re.search(r"STATE_DIR|/var/lib/litclock|hotspot-password", line):
                raise AssertionError(
                    f"line {lineno} deletes state a WiFi reset must preserve (litclock-dev#620): {line!r}")

    def test_sd_cloning_rotates_the_password(self):
        """prepare-for-cloning.sh clones ONE card into MANY for other people.
        Without this, every clone ships the same permanent WPA2 key."""
        clone = (REPO_ROOT / "scripts" / "prepare-for-cloning.sh").read_text()
        assert "hotspot-password" in clone, (
            "prepare-for-cloning.sh must clear the persisted setup-hotspot password (litclock-dev#620) — "
            "otherwise every cloned card broadcasts LitClock-Setup with the SAME key"
        )
        assert ".hotspot-password.*" in clone, "must also sweep orphaned staging files"


class TestRotationOrderingIsPinned:
    """/review: the ordering this change depends on was correct but unguarded.

    Moving the rotation above the litclock-dev#510 gate turned the suite red only with
    `STRICT_ENV_WIPE: unbound variable` — an accident of the harness, not an
    assertion. Once the harness defined it, the mutation went 75/75 green.
    """

    @staticmethod
    def _idx(content, needle, start=0):
        i = content.find(needle, start)
        assert i != -1, f"anchor vanished: {needle!r}"
        return i

    def test_strict_env_wipe_gate_precedes_the_rotation(self, reset_sh_content):
        """Rotation is irreversible, so a --strict-env-wipe abort must come
        first. Otherwise a failed config wipe rotates the key and THEN refuses
        to finish, leaving the owner a device whose setup password changed for
        a reset that did not happen."""
        gate = self._idx(reset_sh_content, 'if [[ "$STRICT_ENV_WIPE" == "true" && "$ENV_WIPE_FAILED" == "true" ]]')
        rotate = self._idx(reset_sh_content, 'if [[ "$GIFT_MODE" != "true" && "$WIPE_WIFI" == "true" ]]')
        assert gate < rotate, "the litclock-dev#510 abort gate must precede the hotspot-key rotation"

    def test_rotation_precedes_the_wifi_wipe(self, reset_sh_content):
        """Step 7 deletes the WiFi keyfiles — the one step that can take the
        script's own network with it. On the new default path an operator runs
        this over SSH; if the session dies during the wipe the script is
        SIGHUP'd, and with the rotation AFTER Step 7 that leaves WiFi gone and
        the old setup key intact: the litclock-dev#660 leak, silently, on the
        path litclock-dev#666 made the default."""
        rotate = self._idx(reset_sh_content, 'if [[ "$GIFT_MODE" != "true" && "$WIPE_WIFI" == "true" ]]')
        step7 = self._idx(reset_sh_content, "# Step 7: Optionally wipe saved WiFi networks")
        assert rotate < step7, (
            "the key rotation must happen BEFORE the WiFi wipe — the wipe can kill the "
            "SSH session the script is running under, and everything after it is lost"
        )


class TestKeepWifiIsCommandLineOnly:
    """litclock-dev#666/litclock-dev#664 (owner decision). A factory reset clears BOTH saved
    passwords; `--keep-wifi` is the opt-out, and it is deliberately a
    COMMAND-LINE-ONLY affordance.

    Someone typing it into a shell has self-identified as technical and knows
    they are keeping the device on its network. The PWA has no way to express
    it, and must not acquire one by accident — the tapped path is the one a
    non-technical owner uses to hand the clock on, and it is the path that must
    never quietly become the lenient one.
    """

    RESET_SH = os.path.join(os.path.dirname(__file__), "..", "scripts", "reset-setup.sh")
    UNIT = os.path.join(os.path.dirname(__file__), "..", "systemd", "litclock-reset.service")

    def _parse_flags(self, argv):
        """Execute the REAL flag-parsing loop and report the resulting state."""
        import subprocess

        content = open(self.RESET_SH).read()
        start = content.index("while [[ $# -gt 0 ]]; do")
        end = content.index("done", start) + len("done")
        loop = content[start:end]
        assert "--keep-wifi" in loop, "lifted span does not contain the flag under test"

        program = (
            "set -u\n"
            "AUTO_YES=false\nDO_REBOOT=false\nDO_POWEROFF=false\n"
            "STRICT_ENV_WIPE=false\nGIFT_MODE=false\nGIFT_MESSAGE_FILE=''\n"
            f"{self._wipe_wifi_default(content)}\n"
            f"{loop}\n"
            'echo "WIPE_WIFI=$WIPE_WIFI GIFT_MODE=$GIFT_MODE"\n'
        )
        return subprocess.run(
            ["bash", "-c", program, "bash", *argv], capture_output=True, text=True, timeout=30
        )

    @staticmethod
    def _wipe_wifi_default(content):
        """The real default line, lifted — not restated. Restating it would let
        the production default flip to false with this suite green."""
        line = next((ln for ln in content.splitlines() if ln.startswith("WIPE_WIFI=")), None)
        assert line, "reset-setup.sh must define a WIPE_WIFI default"
        return line

    def test_the_wipe_is_the_default(self):
        r = self._parse_flags([])
        assert r.returncode == 0, r.stderr
        assert "WIPE_WIFI=true" in r.stdout, (
            "a factory reset with no flags must clear both saved passwords; "
            f"got {r.stdout!r}"
        )

    def test_keep_wifi_opts_out(self):
        r = self._parse_flags(["--keep-wifi"])
        assert r.returncode == 0, r.stderr
        assert "WIPE_WIFI=false" in r.stdout, f"--keep-wifi must preserve WiFi; got {r.stdout!r}"

    def test_wipe_wifi_is_still_accepted(self):
        """litclock-reset.service passes it explicitly and older runbooks use
        it. Rejecting it would break the PWA path for no gain."""
        r = self._parse_flags(["--wipe-wifi"])
        assert r.returncode == 0, f"--wipe-wifi must remain accepted: {r.stdout}{r.stderr}"
        assert "WIPE_WIFI=true" in r.stdout

    def test_gift_mode_still_forces_the_wipe(self):
        r = self._parse_flags(["--gift-mode"])
        assert r.returncode == 0, r.stderr
        assert "WIPE_WIFI=true GIFT_MODE=true" in r.stdout

    def test_keep_wifi_cannot_be_reached_from_the_pwa(self):
        """The guard the whole opt-out depends on. litclock-reset.service is
        what the PWA's Factory reset card starts; if `--keep-wifi` ever appears
        in its ExecStart, the tapped path silently stops clearing the key and
        litclock-dev#660 comes straight back."""
        unit = open(self.UNIT).read()
        exec_lines = [ln for ln in unit.splitlines() if ln.strip().startswith("ExecStart")]
        assert exec_lines, "litclock-reset.service has no ExecStart"
        for ln in exec_lines:
            assert "--keep-wifi" not in ln, (
                "the PWA factory-reset path must NEVER pass --keep-wifi — it is a "
                f"command-line-only escape for technical users: {ln.strip()}"
            )

    def test_no_shipped_caller_passes_keep_wifi(self, tmp_path):
        """Wider than the unit: any systemd unit, script, template or server
        route that invokes reset-setup.sh on a user's behalf is the same hazard.

        /review: the first version ignored grep's exit code, scanned only three
        directories and had no positive control, so a broken invocation would
        have reported "no offenders" forever. It now searches the whole repo and
        proves the search works before trusting an empty result.
        """
        import subprocess

        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

        def scan(where):
            r = subprocess.run(
                ["grep", "-rn", "--exclude-dir=.git", "--exclude-dir=node_modules", "--", "--keep-wifi", where],
                capture_output=True, text=True,
            )
            assert r.returncode in (0, 1), f"grep failed (rc={r.returncode}): {r.stderr}"
            return r.stdout.splitlines()

        # Positive control: the search must be capable of finding one.
        planted = tmp_path / "fake-caller.service"
        planted.write_text("ExecStart=/x/reset-setup.sh --keep-wifi\n", encoding="utf-8")
        assert scan(str(tmp_path)), "the scan cannot find a planted caller — an empty result proves nothing"

        allowed = ("scripts/reset-setup.sh", "tests/", "docs/", "README.md", "CHANGELOG.md", "CLAUDE.md")
        offenders = [
            ln for ln in scan(root)
            if not any(a in ln.split(":", 1)[0].replace(root + "/", "") for a in allowed)
        ]
        assert not offenders, f"a shipped caller passes --keep-wifi on a user's behalf: {offenders}"


class TestGiftModeRejectsKeepWifi:
    """/review found this: --gift-mode sets WIPE_WIFI itself, so combining it
    with --keep-wifi made the LAST flag on the command line win.

        --gift-mode --keep-wifi   ->  WIPE_WIFI=false   <-- ships the gifter's PSK
        --keep-wifi --gift-mode   ->  WIPE_WIFI=true

    The first ordering skips the WiFi wipe and hands the recipient a device
    carrying the gifter's home credentials, while the script header and README
    both promise gift mode implies a wipe. Created by --keep-wifi, so closed by
    it. Rejected rather than silently overridden, and BOTH orderings, which is
    why the guard tracks whether the flag was typed rather than its effect.
    """

    def _run_flags(self, argv):
        import subprocess

        content = open(TestKeepWifiIsCommandLineOnly.RESET_SH).read()
        defaults = [ln for ln in content.splitlines() if ln.startswith(("WIPE_WIFI=", "KEEP_WIFI_REQUESTED="))]
        assert len(defaults) == 2, f"expected both flag defaults, got {defaults}"
        start = content.index("while [[ $# -gt 0 ]]; do")
        end = content.index("done", start) + len("done")
        gs = content.index('if [[ "$DO_REBOOT" == "true" && "$DO_POWEROFF"')
        ge = content.index("# Write gift-mode marker")
        program = (
            "set -u\nAUTO_YES=false\nDO_REBOOT=false\nDO_POWEROFF=false\nSTRICT_ENV_WIPE=false\n"
            "GIFT_MODE=false\nGIFT_MESSAGE_FILE=''\nRED=''\nNC=''\n"
            + "\n".join(defaults) + "\n" + content[start:end] + "\n" + content[gs:ge] + "\n"
            'echo "OK WIPE_WIFI=$WIPE_WIFI"\n'
        )
        return subprocess.run(["bash", "-c", program, "bash", *argv], capture_output=True, text=True, timeout=30)

    @pytest.mark.parametrize("argv", [["--gift-mode", "--keep-wifi"], ["--keep-wifi", "--gift-mode"]])
    def test_both_orderings_are_rejected(self, argv):
        r = self._run_flags(argv)
        assert r.returncode != 0, f"{argv} was accepted: {r.stdout}"
        assert "mutually exclusive" in r.stdout, f"the refusal must say why: {r.stdout}"

    @pytest.mark.parametrize("argv,expected", [(["--gift-mode"], "true"), (["--keep-wifi"], "false"), ([], "true")])
    def test_valid_combinations_still_work(self, argv, expected):
        """Guard the guard: an over-broad refusal would break gift prep."""
        r = self._run_flags(argv)
        assert r.returncode == 0, f"{argv} should be accepted: {r.stdout}{r.stderr}"
        assert f"WIPE_WIFI={expected}" in r.stdout, f"{argv} gave {r.stdout!r}"


class TestFailureBannerMatchesThePath:
    """/review: the fail-closed banner was handoff-only, so after
    litclock-dev#666 made rotation run on every reset it told an operator doing
    an ordinary `--reboot` reset not to pass on a device they were keeping.

    Fail-closed is right either way; the copy has to match the path that
    reached it.
    """

    def _run(self, reset_sh_content, tmp_path, **env):
        import subprocess

        state = tmp_path / "state"
        state.mkdir()
        # A directory at the password path cannot be unlinked, so the rotation
        # fails closed for root and non-root alike.
        (state / "hotspot-password").mkdir()
        preamble = "".join(f"{k}={v}\n" for k, v in env.items())
        program = (
            "set -u\n"
            f"LITCLOCK_STATE_DIR={state}\n"
            f"{TestHotspotPasswordResetSemantics._state_dir_line(reset_sh_content)}\n"
            'RED=""\nGREEN=""\nYELLOW=""\nNC=""\n'
            f"{preamble}"
            f"{TestHotspotPasswordResetSemantics._rotation_fn(reset_sh_content)}\n"
            "rotate_hotspot_password_for_handoff\n"
        )
        return subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=30)

    def test_a_handoff_says_do_not_pass_it_on(self, reset_sh_content, tmp_path):
        r = self._run(reset_sh_content, tmp_path, GIFT_MODE="true", DO_POWEROFF="false")
        assert r.returncode != 0, "the rotation must fail closed"
        assert "do NOT pass this device on" in r.stdout, r.stdout

    def test_an_ordinary_reset_does_not(self, reset_sh_content, tmp_path):
        """The whole point: this path keeps the device with its owner."""
        r = self._run(reset_sh_content, tmp_path, GIFT_MODE="false", DO_POWEROFF="false")
        assert r.returncode != 0, "the rotation must still fail closed"
        assert "do NOT pass this device on" not in r.stdout, (
            f"an ordinary reset must not tell the owner to stop passing on a device "
            f"they are keeping: {r.stdout}"
        )
        assert "Reset FAILED" in r.stdout, r.stdout
        assert "has NOT been rebooted or powered off" in r.stdout, r.stdout


def test_the_reset_failed_marker_clear_verifies_itself(tmp_path):
    """litclock-dev#665 /review: `rm -f` returns 0 for a path it did not remove
    — a directory, a symlink to nowhere, a read-only mount. Without verifying,
    the clear is a no-op that reports success and the device keeps warning its
    owner not to pass on a clock that is fine.

    Warn rather than abort is deliberate: a stale warning is the fail-safe
    direction, whereas aborting a factory reset over bookkeeping would strand
    the owner with no reset at all.
    """
    import re as _re
    import subprocess

    body = (REPO_ROOT / "scripts" / "reset-setup.sh").read_text()
    start = body.index('rm -f "$STATE_DIR/reset-failed"')
    end = body.index("echo -n \"Stopping litclock services", start)
    span = body[start:end]
    assert "reset-failed" in span and _re.search(r"\[ -e .*reset-failed", span), (
        "the clear no longer verifies its own result"
    )

    state = tmp_path / "state"
    state.mkdir()
    # A non-empty directory at the marker path: `rm -f` fails on it for root and
    # non-root alike, so this test needs no permission trick.
    blocked = state / "reset-failed"
    blocked.mkdir()
    (blocked / "occupant").write_text("blocks rm\n", encoding="utf-8")

    program = f'set -u\nSTATE_DIR={state}\n{span}\n'
    r = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=30)

    assert r.returncode == 0, f"the clear aborted the reset over bookkeeping: {r.stderr}"
    assert "could not clear" in r.stdout + r.stderr, (
        "an unremovable marker was silently accepted, so the device keeps reporting a failed reset"
    )
    assert blocked.exists(), "the harness did not actually reproduce an unremovable marker"


# litclock-dev#708: one definition, env-overridable for a non-standard clone
# location. "Counterpart" is whichever repo this one is NOT: in the development
# repo that is the public checkout, and here it is the development checkout
# (~/litclock-archive — the naming is inverted, see CLAUDE.md). The default
# below is the documented maintainer layout FOR THIS REPO; porting this file
# across without flipping it is what the self-comparison guard catches.
# `or`, not a get() default: a SET-BUT-EMPTY var (the standard CI-yaml way to
# "unset") would otherwise yield the relative path scripts/reset-setup.sh —
# which from the repo root is THIS repo's own copy, turning the cross-repo
# check into a vacuous self-comparison that passes instead of skipping
# (/review litclock-dev#711). Non-absolute overrides are rejected for the same
# reason. LITCLOCK_PUBLIC_CHECKOUT is still read so a shared CI/dev config
# setting either name keeps working.
# Deliberately NOT falling back to LITCLOCK_PUBLIC_CHECKOUT: in this repository
# that variable's documented value is this repository's own path, so honouring it
# resolves the counterpart to ourselves. The self-comparison guard below would
# then turn the check into a silent skip on precisely the machine that has both
# clones — lost coverage in a green suite, which is the failure this guard exists
# to prevent.
_COUNTERPART_CHECKOUT = (
    os.environ.get("LITCLOCK_COUNTERPART_CHECKOUT") or "/home/ankush/litclock-archive"
)
if not os.path.isabs(_COUNTERPART_CHECKOUT):
    raise ValueError(
        f"LITCLOCK_COUNTERPART_CHECKOUT must be absolute, got {_COUNTERPART_CHECKOUT!r}")
_COUNTERPART_RESET_SH = Path(_COUNTERPART_CHECKOUT) / "scripts" / "reset-setup.sh"

# The guard that makes the vacuous case impossible rather than merely unlikely.
# A cross-repo check pointed at this repo's OWN file asserts `X == X` and can
# never go red, so it reports the property as verified while protecting
# nothing. That is exactly what a port of this file introduces when the
# default above still names the repo it came from, and it is invisible: the
# suite stays green. Resolve both sides and compare the real paths — symlinks
# and `..` segments included — so the check either compares two repos or
# declares itself unavailable.
def _is_self_comparison():
    try:
        return _COUNTERPART_RESET_SH.resolve() == RESET_SH.resolve()
    except OSError:
        return False


_COUNTERPART_AVAILABLE = _COUNTERPART_RESET_SH.exists() and not _is_self_comparison()


class TestSshHandoffGate:
    """litclock-dev#528, back-ported to dev by litclock-dev#657.

    The function and its header comment are BYTE-IDENTICAL to public's — the
    file is checked for that below, so the two repos cannot drift on the one
    step that decides whether a device is handed on with SSH reachable.

    An earlier version of this made the gate inert on dev images via an
    /etc/litclock/.dev-image marker, so the bench would keep SSH through a
    factory reset. The owner removed the exception (2026-08-20): SSH is
    recoverable on a dev image in two ways that cost nothing — touch a blank
    `ssh` file in the boot partition of the SD card, or use the console on the
    monitor-attached bench Pi — so the distinction bought a divergence and
    protected nothing. Deleting it also deleted the marker, its pi-gen writer
    and a build-workflow variable.
    """

    # Module-note: the path lives ONCE, in _COUNTERPART_RESET_SH at module scope —
    # litclock-dev#708 property 1 was this literal appearing twice (a class
    # attribute cannot be referenced from its own decorator), so changing one
    # copy silently turned the test into a skip or pointed it at the wrong file.
    COUNTERPART_RESET_SH = _COUNTERPART_RESET_SH

    GOLDEN = Path(__file__).parent / "fixtures" / "disable_ssh_for_handoff.golden"

    @staticmethod
    def _commands(span):
        """Comment lines stripped.

        Three mutants survived the first probe because of them: the call sites
        carry a comment reading "see disable_ssh_for_handoff above", so
        DELETING the call left both the presence and the ordering assertions
        green. An assertion a comment can satisfy is not an assertion about
        behaviour.
        """
        return "\n".join(ln for ln in span.splitlines() if not ln.lstrip().startswith("#"))

    @staticmethod
    def _function_header(content):
        """The comment block immediately above the definition."""
        d = content.index("disable_ssh_for_handoff() {")
        return content[content.rindex("\n\n", 0, d) + 2 : d]

    @classmethod
    def _function_body(cls, content):
        """Just the function, not the rest of the file after it.

        `assert "exit 1" in <everything from the def onward>` was satisfied by
        an unrelated `exit 1` further down, so gutting the still-listening
        refusal into a warning stayed green.
        """
        start = content.index("disable_ssh_for_handoff() {")
        return content[start : content.index("\n}\n", start)]

    def _lift(self, content):
        """The function, executable on its own with every privileged verb stubbed.

        `rm` is stubbed for a reason that is not tidiness. The function really
        runs `rm -f /boot/ssh /boot/firmware/ssh …`, and the lift really
        executes it: as an ordinary user that fails into `|| true` and is
        invisible, but under `sudo pytest`, in a root container, or on a Pi it
        DELETES THE OPERATOR'S BOOT-PARTITION SSH FLAGS — the exact recovery
        path CLAUDE.md now documents for getting back in after a reset. Public's
        equivalent helper already stubs all three; dev's did not (/review).
        """
        start = content.index("disable_ssh_for_handoff() {")
        end = content.index("\n}\n", start) + len("\n}\n")
        return (
            "RED=''\nGREEN=''\nYELLOW=''\nNC=''\n"
            "systemctl() { echo \"STUB systemctl $*\"; }\n"
            "raspi-config() { echo \"STUB raspi-config $*\"; }\n"
            "rm() { echo \"STUB rm $*\"; }\n"
            "ss() { return 0; }\n"
            + content[start:end]
        )

    def test_the_function_exists_at_all(self, reset_sh_content):
        """The point of the back-port: dev had no copy, so there was no
        dev-first path for a fix that needs to call it (#57)."""
        assert "disable_ssh_for_handoff() {" in reset_sh_content

    @classmethod
    def _header_plus_body(cls, content):
        """Header comment block + function, as one span — the golden's unit."""
        return cls._function_header(content) + cls._function_body(content) + "\n}\n"

    def test_it_matches_the_vendored_golden_copy(self, reset_sh_content):
        """litclock-dev#708: the parity property, enforced IN CI.

        The maintainer-local test below still compares against the public
        checkout, but it skips wherever that checkout is absent — which is
        every CI run, so the property the litclock-dev#657 back-port exists to
        protect had no CI-side enforcement at all. The golden fixture is the
        committed statement of what the function must say; any edit to the
        function goes red HERE until the fixture is refreshed, which is
        deliberate — refreshing it is the moment to remember the same edit is
        owed to the other repo.

        Refresh (after a deliberate change, in the same commit; run from the
        repo root):

            python3 tests/fixtures/refresh_ssh_gate_golden.py
        """
        golden = self.GOLDEN.read_text()
        # Shape floor (/review litclock-dev#711): the test and the refresh recipe share
        # their anchors, so an anchor-degrading edit followed by a mechanical
        # refresh could vendor a truncated golden and re-green with silently
        # narrowed coverage. These pin the golden's gross shape independently.
        assert golden.startswith("# litclock-dev#528"), "golden lost its header half"
        assert "disable_ssh_for_handoff() {" in golden and "exit 1" in golden
        assert golden.endswith("}\n"), "golden lost its function close"
        assert self._header_plus_body(reset_sh_content) == golden, (
            "disable_ssh_for_handoff (or its header comment) no longer matches "
            "tests/fixtures/disable_ssh_for_handoff.golden. If the change is deliberate, "
            "refresh it IN THE SAME COMMIT: python3 tests/fixtures/refresh_ssh_gate_golden.py — "
            "and remember the same change is owed to the other repo (litclock-dev#657/litclock-dev#708)."
        )

    @pytest.mark.skipif(
        not _COUNTERPART_AVAILABLE,
        reason="counterpart checkout not present (CI), or it resolves to this repo's own copy "
        "— a self-comparison would pass vacuously; cross-repo parity is a maintainer-local "
        "check (set LITCLOCK_COUNTERPART_CHECKOUT for a non-standard clone location)",
    )
    def test_it_is_byte_identical_to_the_counterpart_copy(self, reset_sh_content):
        """The whole reason litclock-dev#657 exists is that this function lived
        on one side only. Keeping the two textually identical is what makes the
        next port an insertion rather than a merge — and it is the property the
        owner asked for when the marker was removed.

        Caveats this test carries knowingly (litclock-dev#708): it reads
        public's WORKING TREE — whatever branch and uncommitted state is
        checked out — and it skips into a green suite wherever the checkout is
        absent. The golden test above is the CI-side floor; this one is the
        maintainer-local cross-check that the OTHER repo still agrees.
        """
        counterpart = self.COUNTERPART_RESET_SH.read_text()
        # header+body as ONE span, same unit as the golden (/review litclock-dev#711: the
        # body-only + header-only pair left a gap where a whitespace change
        # BETWEEN them, or a header drift under a stale extraction, could
        # differ while both piecewise asserts held — and the class docstring
        # claims byte-identity of the whole thing).
        assert self._header_plus_body(reset_sh_content) == self._header_plus_body(counterpart), (
            "this repo and its counterpart copies of disable_ssh_for_handoff (function "
            "or its header comment) have drifted"
        )

    def test_no_dev_image_exception_survives(self, reset_sh_content):
        """The removed exception, pinned as removed — by SHAPE, not by name.

        The first version of this listed the three names the PR had just
        deleted, which are exactly the three nobody would reuse. Measured
        against a CI-like run (public checkout absent, so the parity test
        skips), a reintroduced exception under ANY new name survived it: a
        marker at a different path, an env var called something else, even
        `[[ "$(hostname)" == "litclock-dev" ]]`.

        So the assertion is structural instead. The first thing the function
        does must be the disable itself — nothing may return, exit, or branch
        away before it. That holds whatever the reintroducer calls their knob.
        """
        body = self._function_body(reset_sh_content)
        first = body[: body.index('echo -n "Disabling SSH before handoff... "')]
        commands = [ln.strip() for ln in first.splitlines()[1:] if ln.strip() and not ln.strip().startswith("#")]
        assert commands == [], (
            "nothing may run before the SSH disable — an early return/exit here is how an "
            "image-kind exception comes back (litclock-dev#657). Found: " + repr(commands)
        )
        # The retired names, still worth naming: they are what a copy-paste
        # revert would bring back.
        for token in ("/etc/litclock/.dev-image", "LITCLOCK_FORCE_SSH_GATE", "LITCLOCK_DEV_IMAGE"):
            assert token not in reset_sh_content, f"{token} is back — the gate must not branch on image kind"

    def test_both_terminal_arms_call_it(self, reset_sh_content):
        """Gift mode and the litclock-dev#627 poweroff reset both hand the device on."""
        tail = reset_sh_content[reset_sh_content.index("Reset Complete!") :]
        gift = self._commands(tail[: tail.index('elif [[ "$DO_POWEROFF" == "true" ]]')])
        poweroff = self._commands(
            tail[tail.index('elif [[ "$DO_POWEROFF" == "true" ]]') : tail.index('elif [[ "$DO_REBOOT"')]
        )
        assert "disable_ssh_for_handoff" in gift, "gift mode must disable SSH before handing the device on"
        assert "disable_ssh_for_handoff" in poweroff, "the poweroff reset is a handoff too (litclock-dev#627)"

    def test_it_runs_after_the_fail_closed_gates(self, reset_sh_content):
        """On a failed prep the device stays with its CURRENT owner, who may
        still need SSH to fix it. Stripping their access on that path is the one
        ordering that must not happen."""
        tail = reset_sh_content[reset_sh_content.index("Reset Complete!") :]
        gift = self._commands(tail[: tail.index('elif [[ "$DO_POWEROFF" == "true" ]]')])
        assert gift.index("rotate_hotspot_password_for_handoff") < gift.index("disable_ssh_for_handoff")

    def test_it_actually_disables_ssh_when_run(self, reset_sh_content):
        """Executed, not grepped — and now with no branch that can skip it."""
        import subprocess

        program = self._lift(reset_sh_content) + "\ndisable_ssh_for_handoff\n"
        r = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0
        assert "STUB systemctl disable --now ssh.socket" in r.stdout
        assert "STUB systemctl disable --now ssh.service" in r.stdout
        assert "STUB raspi-config nonint do_ssh 1" in r.stdout

    def test_the_boot_partition_flags_are_cleared(self, reset_sh_content):
        """sshswitch.service turns SSH back on at boot if a bare `ssh` file
        exists, so disabling the unit alone is not enough.

        This is also the owner's documented way BACK IN on a dev image: put the
        file there from any machine that can read the SD card."""
        fn = self._commands(self._function_body(reset_sh_content))
        for flag in ("/boot/ssh", "/boot/ssh.txt", "/boot/firmware/ssh", "/boot/firmware/ssh.txt"):
            assert flag in fn, f"{flag} must be cleared — sshswitch.service re-enables SSH from any of them"

    def test_the_usage_text_warns_that_the_handoff_arms_disable_ssh(self, reset_sh_content):
        """The operator-facing half.

        litclock-dev#657 /review: three docs still promised SSH survives a
        reset — `docs/recovery.md`, `docs/script-reference.md` (which did not
        even list `--poweroff`), and the script's own `--help`. A gate whose
        consequence is undocumented is a gate that reads as a fault the first
        time someone meets it, and the recovery path is not guessable.
        """
        usage = reset_sh_content[reset_sh_content.index("Usage: sudo $0") :]
        usage = usage[: usage.index("exit 1")]
        assert "--poweroff" in usage, "the flag must be listed at all"
        assert "DISABLE SSH" in usage, "and its SSH consequence stated"
        assert "boot partition" in usage, "and the way back in named — it is not guessable"

    def test_the_recovery_doc_no_longer_promises_ssh_survives_a_reset(self):
        doc = (REPO_ROOT / "docs" / "recovery.md").read_text()
        assert "or run a factory reset" in doc, "recovery.md must name the reset as a thing that disables SSH"

    def test_it_verifies_port_22_rather_than_trusting_the_disables(self, reset_sh_content):
        """Every disable above it is `|| true`, and socket activation means the
        service state does not prove the port is shut."""
        fn = self._commands(self._function_body(reset_sh_content))
        assert "ss -H -ltn" in fn
        assert "grep -qx 22" in fn, "match port 22 EXACTLY — :2222 must not count as a hit"
        # Scoped to the FUNCTION: `in <everything after the def>` was satisfied
        # by an unrelated `exit 1` later in the file, so gutting this refusal
        # into a warning stayed green (/review probe).
        assert "exit 1" in fn, "a still-listening port 22 must refuse the handoff, not warn"


class TestNoDevImageMarkerAnywhere:
    """litclock-dev#657: the marker, its pi-gen writer and the build-workflow
    variable are all gone. Pinned because each was individually plausible."""

    def test_pi_gen_does_not_write_a_dev_image_marker(self):
        finalize = (REPO_ROOT / "pi-gen" / "stage3" / "04-finalize" / "00-run.sh").read_text()
        assert ".dev-image" not in finalize
        assert "LITCLOCK_DEV_IMAGE" not in finalize

    def test_the_build_workflow_does_not_stamp_an_image_kind(self):
        wf = (REPO_ROOT / ".github" / "workflows" / "build-image.yml").read_text()
        assert "LITCLOCK_DEV_IMAGE" not in wf


# ───────────────────── litclock-dev#719: definition order + rotation belt ───


def _stripped_lines(content):
    return [ln for ln in content.splitlines() if not ln.lstrip().startswith("#")]


def test_every_function_is_defined_before_its_first_call(reset_sh_content):
    """litclock-dev#719: bash resolves function names at EXECUTION time. The
    litclock-dev#666 reordering left rotate_hotspot_password_for_handoff called 160
    lines before its definition — every default factory reset hit `command
    not found`, kept the permanent setup key, and printed Reset Complete.
    The lifted-span harness is structurally blind to this (spans execute
    with stubs prepended), so the ordering itself is pinned, for EVERY
    function this script defines — not a blacklist of the one that broke."""
    lines = _stripped_lines(reset_sh_content)
    defs = {}
    for i, ln in enumerate(lines):
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{", ln.strip())
        if m:
            defs[m.group(1)] = i
    assert defs, "no functions found — the scan anchor broke"
    assert "rotate_hotspot_password_for_handoff" in defs, "the litclock-dev#719 function vanished"
    offenders = []
    for name, def_idx in defs.items():
        # ANY bare-word occurrence on an executed line counts as a call —
        # `if ! name`, `name || true`, `$(name)`, `name &` all resolve at
        # execution time exactly like a bare call, and the first version of
        # this regex (line-start only) was blind to every one of them
        # (/review litclock-dev#720, Codex). The def line itself is excluded by index.
        call_re = re.compile(rf'(?<![A-Za-z0-9_"]){name}(?![A-Za-z0-9_])')
        for i, ln in enumerate(lines):
            if i == def_idx:
                continue
            if call_re.search(ln):
                if i < def_idx:
                    offenders.append(f"{name}: called at stripped-line {i}, defined at {def_idx}")
                break
    assert offenders == [], (
        "function called before its definition — bash resolves at execution time, so this is "
        "`command not found` on the device (litclock-dev#719):\n  " + "\n  ".join(offenders)
    )


def _extract_rotation_call_site() -> str:
    """The non-gift call site WITH its condition and the litclock-dev#719 belt, verbatim."""
    body = RESET_SH.read_text()
    start = body.index('if [[ "$GIFT_MODE" != "true" && "$WIPE_WIFI" == "true" ]]; then')
    end = body.index("\nfi\n", start) + len("\nfi")
    span = body[start:end]
    assert "SURVIVED the reset" in span, "span lost the litclock-dev#719 belt"
    assert "rotate_hotspot_password_for_handoff" in span
    return span


class TestRotationCallSiteBelt:
    """The belt must catch the OUTCOME, so it fires even when the call itself
    is `command not found` — the exact litclock-dev#719 state, reproduced: the span runs
    WITHOUT any definition of the function."""

    def _run(self, tmp_path, define_rotate):
        state = tmp_path / "state"
        state.mkdir()
        (state / "hotspot-password").write_text("biY7vvkuF\n", encoding="utf-8")
        stubs = {
            True: 'rotate_hotspot_password_for_handoff() { rm -f "$STATE_DIR/hotspot-password"; }\n',
            "noop": "rotate_hotspot_password_for_handoff() { :; }\n",
            False: "",
        }
        stub = stubs[define_rotate]
        script = f"""RED=''
NC=''
GIFT_MODE=false
WIPE_WIFI=true
DO_POWEROFF=false
STATE_DIR={shlex.quote(str(state))}
{stub}{_extract_rotation_call_site()}
echo REACHED-PAST-ROTATION
"""
        # LC_ALL=C: the missing-function test asserts bash's English
        # "command not found"; bash localizes it (/review litclock-dev#720 round 3).
        env = dict(os.environ, LC_ALL="C")
        return (
            subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30, env=env),
            state,
        )

    def test_missing_function_cannot_reach_reset_complete(self, tmp_path):
        r, state = self._run(tmp_path, define_rotate=False)
        assert r.returncode == 1, f"the litclock-dev#719 state sailed through: {r.stdout}{r.stderr}"
        assert "command not found" in r.stderr, "harness did not reproduce the litclock-dev#719 state"
        assert "FAILED to run" in r.stdout and "still on this device" in r.stdout, (
            "the call-status check must catch a vanished function even when no password file "
            "exists for the outcome belt to see (/review litclock-dev#720, Codex)"
        )
        assert (state / "hotspot-password").exists(), "harness invariant"
        assert "REACHED-PAST-ROTATION" not in r.stdout

    def test_noop_rotation_trips_the_outcome_belt(self, tmp_path):
        """/review litclock-dev#720 round 3: with the rc-guard firing first in the
        missing-function case, nothing drove the SURVIVED branch — deleting
        its exit 1 stayed green, the unguarded-guard class litclock-dev#719 itself
        demonstrated. A rotate that returns 0 but deletes nothing must trip
        the outcome belt."""
        r, state = self._run(tmp_path, define_rotate="noop")
        assert r.returncode == 1, f"a no-op rotation sailed through: {r.stdout}{r.stderr}"
        assert "SURVIVED the reset" in r.stdout
        assert (state / "hotspot-password").exists()
        assert "REACHED-PAST-ROTATION" not in r.stdout

    def test_working_rotation_passes_the_belt(self, tmp_path):
        r, state = self._run(tmp_path, define_rotate=True)
        assert r.returncode == 0, f"{r.stdout}{r.stderr}"
        assert not (state / "hotspot-password").exists()
        assert "REACHED-PAST-ROTATION" in r.stdout


# ───────────────────── litclock-dev#727 + litclock-dev#718: splash stop edge ────────────


def _noncomment(text: str) -> str:
    """Comments must not satisfy behavior assertions (memory: three
    instances in one session) — strip full-line comments before grepping."""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


class TestSplashStopEdgeRearm:
    """litclock-dev#727: the shutdown/welcome splash is the ExecStop of a
    RemainAfterExit oneshot — a stop edge that exists once per boot. An
    earlier failed reset spends it, and the same-boot retry (the EXPECTED
    path after a failure) painted nothing: a successfully-reset gift box
    shipped showing "Reset did not finish / Do NOT pass it on". The fix
    re-arms the edge with a start before every stop."""

    def test_start_precedes_stop_in_executable_lines(self, reset_sh_content):
        code = _noncomment(reset_sh_content)
        start_idx = code.find("systemctl start litclock-shutdown.service")
        stop_idx = code.find("systemctl stop litclock-shutdown.service")
        assert start_idx != -1, "the re-arm start vanished (litclock-dev#727)"
        assert stop_idx != -1, "the shutdown-service stop vanished"
        assert start_idx < stop_idx, (
            "the re-arm must come BEFORE the stop — a start after the stop "
            "re-activates the unit but paints nothing this run"
        )

    def test_rearm_is_after_the_splash_decision_block(self, reset_sh_content):
        # The hint/suppress decisions must be on disk before the stop edge
        # fires ([[learning-lifted-spans-blind-to-definition-order]] class:
        # ordering is part of the contract, not an accident).
        code = _noncomment(reset_sh_content)
        decision_idx = code.find("touch /run/litclock-splash-suppress")
        rearm_idx = code.find("systemctl start litclock-shutdown.service")
        assert decision_idx != -1 and rearm_idx != -1
        assert decision_idx < rearm_idx, (
            "splash-suppress decision must precede the re-armed stop edge"
        )

    def test_rearm_start_is_blocking(self, reset_sh_content):
        # /review litclock-dev#731 green-while-broken door: this repo's own rule is
        # "systemctl start from inside a service needs --no-block" — but
        # HERE a queued (unstarted) start job is simply REPLACED by the stop
        # on the next line, so --no-block would silently recreate the
        # no-paint retry while every ordering test stays green. The re-arm
        # must stay blocking.
        code = _noncomment(reset_sh_content)
        line = next(
            ln for ln in code.splitlines() if "systemctl start litclock-shutdown.service" in ln
        )
        assert "--no-block" not in line, (
            "the re-arm went --no-block — the immediate stop replaces the queued "
            "start and the retry paints nothing (litclock-dev#727 reborn)"
        )

    def test_failure_painter_stopped_before_the_rearm(self, reset_sh_content):
        # /review litclock-dev#731: a retry landing inside the litclock-dev#725 failure
        # painter's window would race two eink processes on SPI; losing that
        # race keeps "Do NOT pass it on" on a successfully reset device.
        code = _noncomment(reset_sh_content)
        failed_stop = code.find("systemctl stop litclock-reset-failed.service")
        rearm = code.find("systemctl start litclock-shutdown.service")
        assert failed_stop != -1, "failure-painter stop missing from Step 1"
        assert failed_stop < rearm, "failure painter must be stopped before the re-armed edge"

    def test_suppress_marker_consumed_after_the_stop_edge(self, reset_sh_content):
        # The marker protects the ONE stop just requested, never the rest of
        # the boot (first-boot.sh's own principle; /review litclock-dev#731 both passes).
        code = _noncomment(reset_sh_content)
        stop_idx = code.find("systemctl stop litclock-shutdown.service")
        post = code[stop_idx:]
        assert "rm -f /run/litclock-splash-suppress" in post, (
            "the suppress marker must be cleared after the stop edge consumes it"
        )

    def test_rearm_start_is_time_bounded(self, reset_sh_content):
        # On the PWA arm this script runs INSIDE litclock-reset.service;
        # an unbounded systemctl start that waits on a job would hang the
        # reset past its TimeoutStartSec=60.
        code = _noncomment(reset_sh_content)
        line = next(
            (ln for ln in code.splitlines() if "systemctl start litclock-shutdown.service" in ln),
            "",
        )
        assert line.strip().startswith("timeout "), (
            "the re-arm start must be wrapped in `timeout` — it runs inside "
            "litclock-reset.service on the PWA arm"
        )

    def test_shutdown_unit_shape_supports_the_rearm(self):
        # The re-arm depends on all three: oneshot + RemainAfterExit=yes
        # (stop edge exists at all) + ExecStart=/bin/true (start is instant
        # and side-effect-free). Changing any of them silently breaks litclock-dev#727.
        unit = (REPO_ROOT / "systemd" / "litclock-shutdown.service").read_text()
        code = _noncomment(unit)
        assert "Type=oneshot" in code
        assert "RemainAfterExit=yes" in code
        assert "ExecStart=/bin/true" in code

    def test_executed_order_start_then_stop(self, tmp_path):
        """EXECUTE the lifted services-stop span with systemctl recorded:
        the call sequence must contain start(litclock-shutdown) then
        stop(litclock-shutdown)."""
        body = RESET_SH.read_text()
        anchor = 'echo -n "Stopping litclock services... "'
        assert body.count(anchor) == 1, "stop-block anchor must be unique"
        start = body.index(anchor)
        end_anchor = "systemctl stop litclock-handoff-fallback.service"
        # End at the anchor line's own newline (/review litclock-dev#731: a longer
        # overshoot leaned on a comment's length and could swallow a real,
        # unstubbed pkill into the executed span).
        end = body.index(end_anchor, start) + len(end_anchor)
        span = body[start : body.index("\n", end) + 1]
        assert "systemctl start litclock-shutdown.service" in span, (
            "span lost the re-arm — extraction drifted"
        )
        assert "pkill" not in span, "span overgrew into unstubbed host commands"
        rec = tmp_path / "rec"
        script = (
            f"REC={shlex.quote(str(rec))}\n"
            'systemctl() { echo "$@" >> "$REC"; }\n'
            'timeout() { shift; "$@"; }\n'
            + span
        )
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        calls = rec.read_text().splitlines()
        start_pos = calls.index("start litclock-shutdown.service")
        stop_pos = calls.index("stop litclock-shutdown.service")
        assert start_pos < stop_pos, f"re-arm ordering broken at runtime: {calls}"
        failed_pos = calls.index("stop litclock-reset-failed.service")
        assert failed_pos < start_pos, f"failure painter must stop before the re-arm: {calls}"


class TestPlainArmSplashSuppress:
    """litclock-dev#718 (owner decision 2026-08-23): the CLI plain arm — no
    --reboot, no --poweroff, not gift — painted "Powered Off" on a running
    device. It now suppresses the splash (last-painted content persists;
    the console's closing text carries the instruction). The PWA path is
    unaffected: it always dispatches --poweroff."""

    SUPPRESS = "/run/litclock-splash-suppress"

    def _run_arms(self, tmp_path, *, reboot="false", poweroff="false", gift="false") -> list[str]:
        tmp_path = Path(tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        body = RESET_SH.read_text()
        start = body.index("rm -f /run/litclock-splash-suppress")
        end_marker = "# Step 1:"
        end = body.index(end_marker, start)
        span = body[start:end]
        assert 'touch /run/litclock-splash-suppress' in span, "span lost the suppress write"
        assert '"$GIFT_MODE" != "true"' in span, "span lost the gift exclusion"
        rec = tmp_path / "rec"
        hint_tmp = tmp_path / "hint"
        script = (
            f"REC={shlex.quote(str(rec))}\n"
            f"DO_REBOOT={reboot}\nDO_POWEROFF={poweroff}\nGIFT_MODE={gift}\n"
            'rm() { echo "rm $*" >> "$REC"; }\n'
            'touch() { echo "touch $*" >> "$REC"; }\n'
            f'mktemp() {{ printf "%s\\n" {shlex.quote(str(hint_tmp))}; }}\n'
            'mv() { echo "mv $*" >> "$REC"; }\n'
            "chmod() { :; }\n"
            "trap() { :; }\n"
            + span
        )
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return rec.read_text().splitlines() if rec.exists() else []

    def test_stale_suppress_cleared_on_every_run(self, tmp_path):
        # A stale marker outranks even the gift welcome in the splash
        # ladder — every arm must clear it first (the litclock-dev#672
        # stale-marker class).
        for kwargs in ({"reboot": "true"}, {"poweroff": "true"}, {"gift": "true"}, {}):
            calls = self._run_arms(tmp_path / f"c{len(kwargs)}{list(kwargs.keys())}".replace("'", ""), **kwargs)
            assert f"rm -f {self.SUPPRESS}" in calls, f"stale clear missing for {kwargs or 'plain'}"

    def test_plain_arm_writes_suppress(self, tmp_path):
        calls = self._run_arms(tmp_path)
        assert f"touch {self.SUPPRESS}" in calls

    def test_gift_arm_never_writes_suppress(self, tmp_path):
        # Suppress outranks welcome: writing it on the gift arm would ship
        # a box with no welcome splash — the exact litclock-dev#727 outcome
        # by another door.
        calls = self._run_arms(tmp_path, gift="true")
        assert f"touch {self.SUPPRESS}" not in calls

    def test_reboot_and_poweroff_arms_never_write_suppress(self, tmp_path):
        for kwargs in ({"reboot": "true"}, {"poweroff": "true"}):
            calls = self._run_arms(tmp_path / list(kwargs)[0], **kwargs)
            assert f"touch {self.SUPPRESS}" not in calls, f"suppress leaked into {kwargs}"

    def test_pwa_reset_unit_still_takes_the_poweroff_arm(self):
        # The owner constraint: the PWA path must never reach a non-poweroff
        # arm. Any future change routing it elsewhere must carry litclock-dev#718.
        unit = (REPO_ROOT / "systemd" / "litclock-reset.service").read_text()
        exec_line = next(ln for ln in _noncomment(unit).splitlines() if ln.startswith("ExecStart="))
        assert "--poweroff" in exec_line


class TestGiftLanguageFile:
    """litclock-dev#532 pickers 5b: --language-file → root-owned
    .gift-language marker + env.sh LITCLOCK_LANGUAGE seed, mirroring the
    --message-file pattern including the litclock-dev#316 O_NOFOLLOW defense."""

    @staticmethod
    def _gift_block(content: str) -> str:
        start = content.find('if [[ "$GIFT_MODE" == "true" ]]; then')
        end = content.find('echo "=', start)
        return content[start:end]

    @staticmethod
    def _language_py(content: str) -> str:
        """Extract the language python heredoc for EXECUTION — a content
        grep alone is satisfied by comments (mutate-the-code lesson)."""
        anchor = content.find('"$GIFT_LANGUAGE_FILE" "$CONFIG_DIR/.gift-language"')
        assert anchor != -1, "language heredoc invocation not found"
        py_start = content.index("<<'PY'", anchor) + len("<<'PY'\n")
        py_end = content.index("\nPY\n", py_start)
        return content[py_start:py_end]

    def test_language_file_flag_parsed(self, reset_sh_content):
        assert "--language-file" in reset_sh_content
        assert "GIFT_LANGUAGE_FILE=" in reset_sh_content

    def test_marker_written_before_shutdown_service_stop(self, reset_sh_content):
        """Write-before-stops: once bash surfaces are catalog-routed, the
        ExecStop welcome splash consults the marker — writing after the
        stops would be too late (same invariant as .welcome-message)."""
        write_idx = reset_sh_content.find('"$CONFIG_DIR/.gift-language"')
        stop_idx = reset_sh_content.find("systemctl stop litclock-shutdown.service")
        assert write_idx != -1
        assert write_idx < stop_idx

    def test_language_read_uses_o_nofollow_both_sides(self, reset_sh_content):
        py = self._language_py(reset_sh_content)
        code_lines = [ln for ln in py.splitlines() if not ln.lstrip().startswith("#")]
        code = "\n".join(code_lines)
        assert code.count("O_NOFOLLOW") >= 2, (
            "both the pi-writable source read AND the marker write must be O_NOFOLLOW"
        )

    def test_no_language_file_clears_stale_marker(self, reset_sh_content):
        block = self._gift_block(reset_sh_content)
        assert 'rm -f "$CONFIG_DIR/.gift-language"' in block

    def test_env_defaults_seed_language_from_validated_code(self, reset_sh_content):
        assert "export LITCLOCK_LANGUAGE=$GIFT_LANGUAGE_CODE" in reset_sh_content

    def test_plain_reset_clears_stale_marker(self, reset_sh_content):
        """Trap (c): a NON-gift reset must remove an abandoned gift's
        marker. The cleanup must be gated on NOT gift mode (gift runs
        manage the marker in their own arm)."""
        guarded = (
            'if [[ "$GIFT_MODE" != "true" ]]; then\n'
            '    rm -f "$CONFIG_DIR/.gift-language"\n'
            "fi"
        )
        assert guarded in reset_sh_content, (
            "expected the exact non-gift-guarded rm block (other "
            "GIFT_MODE!=true guards exist — the anchor must be the full "
            "guarded sequence, not the first guard in the file)"
        )
        # Codex 5b /review P3 ordering: the cleanup must run BEFORE the
        # service stops (the ExecStop splash may consult the marker once
        # catalog-routed) and AFTER the confirmation prompt (a cancelled
        # reset must change nothing).
        rm_idx = reset_sh_content.index(guarded)
        stop_idx = reset_sh_content.index("systemctl stop litclock-shutdown.service")
        confirm_idx = reset_sh_content.index('read -p "Continue? (y/N)')
        assert confirm_idx < rm_idx < stop_idx

    # ── executed behavior of the validation heredoc ──

    def _run_py(self, reset_sh_content, tmp_path, src_content=None, symlink_to=None):
        import subprocess
        import sys

        py = self._language_py(reset_sh_content)
        src = tmp_path / "gift-language"
        dst = tmp_path / "marker"
        if symlink_to is not None:
            src.symlink_to(symlink_to)
        elif src_content is not None:
            src.write_text(src_content, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-", str(src), str(dst)],
            input=py,
            capture_output=True,
            text=True,
        )
        return proc, dst

    def test_valid_code_writes_marker_and_prints_code(self, reset_sh_content, tmp_path):
        proc, dst = self._run_py(reset_sh_content, tmp_path, src_content="es\n")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "es"
        assert dst.read_text(encoding="utf-8") == "es"

    def test_symlinked_source_rejected(self, reset_sh_content, tmp_path):
        target = tmp_path / "secret"
        target.write_text("es", encoding="utf-8")
        proc, dst = self._run_py(reset_sh_content, tmp_path, symlink_to=target)
        assert proc.returncode != 0
        assert not dst.exists()

    def test_shell_metacharacters_rejected(self, reset_sh_content, tmp_path):
        """The code is interpolated into the env.sh DEFAULTS as root —
        anything outside the lowercase BCP-47 shape must exit nonzero
        BEFORE any write."""
        for evil in ("es;curl x|sh", "es\nrm -rf /", "$(reboot)", "ES", "-es", ""):
            proc, dst = self._run_py(reset_sh_content, tmp_path, src_content=evil)
            assert proc.returncode != 0, f"accepted {evil!r}"
            assert not dst.exists(), f"marker written for {evil!r}"

    def test_surrounding_whitespace_is_stripped_not_persisted(self, reset_sh_content, tmp_path):
        """Unlike the config.py validator (raw exact match — its persisted
        value IS the raw payload), the script strips BEFORE validating and
        persists the STRIPPED value, so there is no validated≠persisted
        gap. This is deliberate leniency for the CLI path where
        `echo es > file` writes a trailing newline."""
        for src in ("es\n", "es ", "  es  "):
            proc, dst = self._run_py(reset_sh_content, tmp_path, src_content=src)
            assert proc.returncode == 0, (src, proc.stderr)
            assert proc.stdout.strip() == "es"
            assert dst.read_text(encoding="utf-8") == "es"

    def test_oversized_input_rejected(self, reset_sh_content, tmp_path):
        """os.read(fd, 64) truncation must not turn a huge input into a
        valid-looking prefix that passes the shape gate."""
        proc, dst = self._run_py(reset_sh_content, tmp_path, src_content="a" * 4096)
        assert proc.returncode != 0
        assert not dst.exists()

    def test_missing_source_rejected(self, reset_sh_content, tmp_path):
        proc, dst = self._run_py(reset_sh_content, tmp_path)
        assert proc.returncode != 0
        assert not dst.exists()

    def test_fifo_source_rejected_fast(self, reset_sh_content, tmp_path):
        """A pi-placed FIFO at the pi-owned staging path must fail fast,
        not block root's open until the unit's 60s timeout (the repo's
        O_NONBLOCK-alongside-O_NOFOLLOW rule)."""
        import subprocess
        import sys

        py = self._language_py(reset_sh_content)
        src = tmp_path / "gift-language"
        dst = tmp_path / "marker"
        os.mkfifo(src)
        proc = subprocess.run(
            [sys.executable, "-", str(src), str(dst)],
            input=py,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode != 0
        assert not dst.exists()

    # ── executed control flow (a content grep survives a quote revert /
    #    branch deletion — the tests below run the real bash) ──

    @staticmethod
    def _lift_gift_language_span(content: str) -> str:
        """The whole `if [[ -n "$GIFT_LANGUAGE_FILE" ]] ... fi` arm,
        including the heredoc and both failure branches."""
        start = content.find('if [[ -n "$GIFT_LANGUAGE_FILE" ]]; then')
        assert start != -1
        end_marker = '\n    else\n        rm -f "$CONFIG_DIR/.gift-language"\n    fi\n'
        end = content.index(end_marker, start) + len(end_marker)
        return content[start:end]

    def _run_span(self, reset_sh_content, tmp_path, language_file, stale_marker=False):
        import subprocess

        span = self._lift_gift_language_span(reset_sh_content)
        if stale_marker:
            (tmp_path / ".gift-language").write_text("de", encoding="utf-8")
        script = (
            f'GIFT_LANGUAGE_FILE="{language_file}"\n'
            f'CONFIG_DIR="{tmp_path}"\n'
            "GIFT_LANGUAGE_CODE=\n"
            f"{span}\n"
            'echo "CODE=[$GIFT_LANGUAGE_CODE]"\n'
        )
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=15)

    def test_span_valid_source_sets_code_and_marker(self, reset_sh_content, tmp_path):
        src = tmp_path / "src"
        src.write_text("es", encoding="utf-8")
        proc = self._run_span(reset_sh_content, tmp_path, src)
        assert "CODE=[es]" in proc.stdout, proc.stderr
        assert (tmp_path / ".gift-language").read_text(encoding="utf-8") == "es"

    def test_span_invalid_source_clears_preexisting_stale_marker(self, reset_sh_content, tmp_path):
        """The heredoc-FAILURE branch must rm a stale marker — deleting
        that rm ships the PREVIOUS gift's language to the new recipient."""
        src = tmp_path / "src"
        src.write_text("NOT A CODE", encoding="utf-8")
        proc = self._run_span(reset_sh_content, tmp_path, src, stale_marker=True)
        assert "CODE=[]" in proc.stdout, proc.stderr
        assert not (tmp_path / ".gift-language").exists()

    def test_span_no_language_file_clears_preexisting_stale_marker(self, reset_sh_content, tmp_path):
        proc = self._run_span(reset_sh_content, tmp_path, "", stale_marker=True)
        assert "CODE=[]" in proc.stdout, proc.stderr
        assert not (tmp_path / ".gift-language").exists()

    def test_defaults_interpolates_validated_code(self, reset_sh_content):
        """The seeding mechanism IS the DEFAULTS quote flip (single→double);
        a substring grep is satisfied either way, so EXECUTE the assignment
        and assert the expansion (guard-observation-window class)."""
        import re as _re
        import subprocess

        m = _re.search(r'DEFAULTS="export OPENWEATHERMAP_APIKEY=\n.*?\n"\n', reset_sh_content, _re.S)
        assert m, "DEFAULTS double-quoted assignment not found"
        span = m.group(0)
        for code, expected in (("es", "export LITCLOCK_LANGUAGE=es\n"), ("", "export LITCLOCK_LANGUAGE=\n")):
            proc = subprocess.run(
                ["bash", "-c", f'GIFT_LANGUAGE_CODE="{code}"\n{span}\nprintf %s "$DEFAULTS"'],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert expected in proc.stdout, (code, proc.stdout, proc.stderr)
            assert "$GIFT_LANGUAGE_CODE" not in proc.stdout

    def test_symlinked_destination_rejected(self, reset_sh_content, tmp_path):
        """Write-side O_NOFOLLOW, EXECUTED: a planted symlink at the marker
        path must not be followed, and the victim must stay untouched."""
        import subprocess
        import sys

        victim = tmp_path / "victim"
        victim.write_text("untouched", encoding="utf-8")
        src = tmp_path / "gift-language"
        src.write_text("es", encoding="utf-8")
        dst = tmp_path / "marker"
        dst.symlink_to(victim)
        py = self._language_py(reset_sh_content)
        proc = subprocess.run(
            [sys.executable, "-", str(src), str(dst)],
            input=py,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode != 0
        assert victim.read_text(encoding="utf-8") == "untouched"
