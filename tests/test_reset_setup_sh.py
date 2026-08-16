"""Tests for scripts/reset-setup.sh (issue #160)."""

import os
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
        # rfind → the END-OF-SCRIPT gift branch (there's an earlier
        # marker-write `if [[ "$GIFT_MODE" ...]]`, and litclock-dev#627 added a
        # DO_POWEROFF elif to the hint block between them).
        idx = reset_sh_content.rfind('if [[ "$GIFT_MODE" == "true" ]]')
        assert idx != -1, "gift-mode end-of-script branch missing"
        elif_idx = reset_sh_content.find("elif", idx)
        block = reset_sh_content[idx:elif_idx]
        assert "poweroff" in block

    @staticmethod
    def _ssh_gate_body(reset_sh_content):
        """Extract disable_ssh_for_handoff()'s body (litclock-dev#636 moved the
        litclock-dev#528 gate into a shared function so gift mode and the non-gift
        factory-reset poweroff enforce the same posture)."""
        idx = reset_sh_content.find("disable_ssh_for_handoff() {")
        assert idx != -1, "disable_ssh_for_handoff() definition missing"
        end = reset_sh_content.find("\n}", idx)
        assert end != -1
        return reset_sh_content[idx:end]

    @staticmethod
    def _gift_block(reset_sh_content):
        # rfind: the end-of-script branch is the LAST $GIFT_MODE test in the
        # file (the first one is the early marker-write block).
        idx = reset_sh_content.rfind('if [[ "$GIFT_MODE" == "true" ]]')
        assert idx != -1, "gift-mode end-of-script branch missing"
        return reset_sh_content[idx : reset_sh_content.find("elif", idx)]

    @staticmethod
    def _poweroff_block(reset_sh_content):
        # rfind: the LAST $DO_POWEROFF test is the end-of-script arm (an
        # earlier one lives in the reboot-hint block), mirroring _gift_block.
        idx = reset_sh_content.rfind('elif [[ "$DO_POWEROFF" == "true" ]]')
        assert idx != -1, "poweroff end-of-script branch missing"
        return reset_sh_content[idx : reset_sh_content.find("elif", idx + 1)]

    def test_ssh_gate_disables_every_layer(self, reset_sh_content):
        """litclock-dev#528: the handoff gate must force SSH off across every layer: the
        SOCKET (Bookworm socket-activates sshd — disabling only ssh.service
        leaves port 22 open, caught by hardware QA 2026-07-16), the classic
        service, raspi-config posture, and the boot-partition re-enable
        flags (sshswitch re-enables SSH if a bare `ssh` file exists on
        /boot or /boot/firmware)."""
        body = self._ssh_gate_body(reset_sh_content)
        # The socket is the load-bearing unit on current images — a
        # service-only disable ships a device with port 22 still open.
        # Disabled in a SEPARATE call from the service so a missing unit on
        # an older image can't abort the other disable (/review).
        assert "systemctl disable --now ssh.socket" in body, "must disable ssh.socket separately"
        assert "systemctl disable --now ssh.service" in body, "must disable ssh.service separately"
        assert "raspi-config nonint do_ssh 1" in body
        assert "/boot/firmware/ssh" in body and "/boot/ssh" in body

    def test_ssh_gate_verifies_port_22_closed(self, reset_sh_content):
        """litclock-dev#528 /review: the SSH-off step is a security gate, not best-effort.
        After disabling, it must verify port 22 is actually closed (the
        disables are all `|| true`, and socket-activation means unit state
        alone doesn't prove the port is shut) and refuse to power off
        (exit) if sshd still listens."""
        body = self._ssh_gate_body(reset_sh_content)
        assert "ss -H -ltn" in body, "must probe listening sockets to verify SSH is off"
        # Exact port match, not a substring that would false-hit :2222 etc.
        assert "grep -qx 22" in body
        verify_idx = body.find("ss -H -ltn")
        disable_idx = body.find("systemctl disable --now ssh.socket")
        exit_idx = body.find("exit 1", verify_idx)
        assert disable_idx < verify_idx < exit_idx

    def test_gift_mode_calls_ssh_gate_before_poweroff(self, reset_sh_content):
        """litclock-dev#528: gift mode must run the gate, after the ENV_WIPE_FAILED fatal
        gate (on a FAILED prep the device stays on and the owner may need
        SSH to fix it) and before the poweroff command."""
        block = self._gift_block(reset_sh_content)
        gate_idx = block.find('"$ENV_WIPE_FAILED" == "true"')
        call_idx = block.find("\n    disable_ssh_for_handoff")
        poweroff_idx = block.rfind("\n    poweroff")
        assert gate_idx != -1 and call_idx != -1 and poweroff_idx != -1
        assert gate_idx < call_idx < poweroff_idx

    @pytest.mark.parametrize("arm", ["gift", "poweroff"], ids=["gift-mode", "pwa-factory-reset"])
    def test_rotation_runs_before_disabling_ssh_on_both_handoff_arms(self, reset_sh_content, arm):
        """litclock-dev#620 + litclock-dev#528 interact, and the order is load-bearing.

        The hotspot-password rotation fails CLOSED: a read-only card or ownership
        drift on the state dir makes it print "do NOT pass this device on" and
        exit 1, which leaves the clock with its CURRENT owner.
        `disable_ssh_for_handoff` does not return — it is the point of no remote
        access. Run SSH-off first and that abort path strips the owner's only
        remote way in on the exact branch where they still need it to diagnose
        the card. Rotation first; SSH-off last before poweroff.

        This pairing exists ONLY on this repo: litclock-dev has no SSH gate at
        all (authored here, in #52/#53), so upstream cannot pin the ordering and
        every port that touches this file must re-establish it by hand.

        Parametrised over BOTH arms as of litclock-dev#660. The gift arm was
        pinned when the two features first met; the --poweroff arm grew the same
        pairing only when #660 added rotation to it, and nothing covered it.
        """
        import re as _re

        block = self._gift_block(reset_sh_content) if arm == "gift" else self._poweroff_block(reset_sh_content)
        # Anchor on the CALL LINE, not a bare substring. The comments in both
        # arms name the function, so `block.find(name)` matches prose that sits
        # above the call and stays true even with the call moved below the SSH
        # gate — verified: that mutation kept this test green until this fix.
        _call = _re.search(r"(?m)^\s*rotate_hotspot_password_for_handoff\s*$", block)
        rotate_idx = _call.start() if _call else -1
        ssh_idx = block.find("\n    disable_ssh_for_handoff")
        assert rotate_idx != -1, f"litclock-dev#660 rotation call missing from the {arm} arm"
        assert ssh_idx != -1, f"litclock-dev#528 SSH gate missing from the {arm} arm"
        assert rotate_idx < ssh_idx, (
            f"in the {arm} arm, SSH must be disabled AFTER the hotspot-password rotation — "
            "the rotation can exit 1 and leave the device with its current owner, who then needs SSH"
        )

    def test_poweroff_mode_calls_ssh_gate_before_poweroff(self, reset_sh_content):
        """litclock-dev#636: the non-gift factory-reset copy invites "move or
        pass the clock on", so the poweroff path must hand over the same
        SSH posture a recipient gets from a gift or fresh flash: off. Its
        env-wipe safety mirrors gift mode structurally — --poweroff implies
        --strict-env-wipe, whose failure aborts long before this branch."""
        content = reset_sh_content
        idx = content.rfind('elif [[ "$DO_POWEROFF" == "true" ]]')
        assert idx != -1, "poweroff end-of-script branch missing"
        block = content[idx : content.find("elif", idx + 1)]
        # Anchor on the CALL (line-leading), not a bare substring: a later
        # comment mentioning the function, or the call being commented out,
        # must not keep this test green with the gate gone (matches the
        # gift-mode test's anchoring).
        call_idx = block.find("\n    disable_ssh_for_handoff")
        poweroff_idx = block.rfind("\n    poweroff")
        assert call_idx != -1, "poweroff branch must run the SSH handoff gate"
        assert call_idx < poweroff_idx

    def test_reboot_and_plain_paths_leave_ssh_alone(self, reset_sh_content):
        """The device stays the owner's on --reboot and the no-flag hint path
        — no SSH change there (litclock-dev#636 scope: handoff paths only)."""
        content = reset_sh_content
        idx = content.rfind('elif [[ "$DO_REBOOT" == "true" ]]')
        assert idx != -1
        tail = content[idx:]
        assert "disable_ssh_for_handoff" not in tail

    # --- Behavioral gate tests (litclock-dev#636 /review) ------------------
    # The structural tests above prove the gate is WIRED; these RUN it. A
    # security gate that is only pattern-matched is unverified: the whole
    # point is that port-22-still-open aborts the poweroff, and that only the
    # exact port 22 (not :2222) triggers it, and that an ss that can't verify
    # never silently passes. Extract the function and execute it under bash
    # with systemctl/raspi-config/rm and a scripted `ss` stubbed on PATH.

    @staticmethod
    def _run_ssh_gate(tmp_path, ss_script):
        """Execute disable_ssh_for_handoff() with a stubbed environment.

        ss_script is the body of a fake `ss` on PATH. Returns the completed
        process (rc 0 = gate passed/proceeded, rc 1 = gate aborted).
        """
        import subprocess

        content = RESET_SH.read_text()
        start = content.find("disable_ssh_for_handoff() {")
        # Match the function's closing brace on its own line — a bare find("}")
        # would truncate at the first ${VAR:-default} or awk block.
        end = content.find("\n}", start)
        assert start != -1 and end != -1, "could not extract disable_ssh_for_handoff()"
        func = content[start : end + 2]

        stubs = tmp_path / "bin"
        stubs.mkdir()
        for name in ("systemctl", "raspi-config", "rm"):
            (stubs / name).write_text("#!/bin/sh\nexit 0\n")
        (stubs / "ss").write_text("#!/bin/sh\n" + ss_script)
        for f in stubs.iterdir():
            f.chmod(0o755)

        # `local` is only valid inside a function, so the extracted body must
        # run as a function call, not top-level. Colors are referenced inside.
        harness = f"RED=''; GREEN=''; YELLOW=''; NC=''\n{func}\ndisable_ssh_for_handoff\n"
        return subprocess.run(
            ["bash", "-c", harness],
            capture_output=True,
            text=True,
            env={"PATH": f"{stubs}:/usr/bin:/bin"},
            timeout=10,
        )

    def test_gate_aborts_when_port_22_still_listening(self, tmp_path):
        """The load-bearing promise: sshd still on :22 → exit 1 (poweroff
        never reached)."""
        proc = self._run_ssh_gate(tmp_path, 'echo "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*"\n')
        assert proc.returncode == 1, proc.stderr
        assert "still listening" in proc.stdout.lower() or "still listening" in proc.stderr.lower()

    def test_gate_proceeds_when_only_high_ports_listen(self, tmp_path):
        """grep -qx 22 must not false-hit :2222 / :220 — a device with only
        those open must pass the gate (rc 0)."""
        ss = 'echo "LISTEN 0 128 0.0.0.0:2222 0.0.0.0:*"\necho "LISTEN 0 128 0.0.0.0:220 0.0.0.0:*"\n'
        proc = self._run_ssh_gate(tmp_path, ss)
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_gate_proceeds_when_no_ports_listen(self, tmp_path):
        """Port 22 verified closed → gate passes."""
        proc = self._run_ssh_gate(tmp_path, "exit 0\n")  # no output = nothing listening
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_gate_does_not_fail_open_when_ss_errors(self, tmp_path):
        """litclock-dev#636 /review: an `ss` that ERRORS (nonzero, no output)
        must NOT read as 'verified closed'. It warns and proceeds (can't
        verify), exactly like an absent ss — never a silent pass that a
        pipe-to-grep would have produced."""
        proc = self._run_ssh_gate(tmp_path, "exit 3\n")  # ss present but fails
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "could not verify" in proc.stdout.lower() or "could not verify" in proc.stderr.lower()

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
    assert "export WEATHER_LOCATION_MODE=auto" in content, (
        "litclock-dev#337 A3: reset-setup.sh DEFAULTS must include MODE=auto"
    )
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
        assert "/usr/bin/python3 - " in reset_sh_content, (
            "gift-message processing must use the system python3 (litclock-dev#387)"
        )
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
        for src, name in ((self.PI_GEN, "pi-gen"), (self.UPDATE_SH, "update.sh")):
            body = src.read_text()
            assert "reset-setup.sh" in body and "/usr/local/lib/litclock" in body, (
                f"{name} must install reset-setup.sh root-owned to /usr/local/lib/litclock (litclock-dev#387)"
            )
            assert "/usr/local/lib/litclock/lib" in body, (
                f"{name} must install the root-owned lib/state.sh dir (litclock-dev#387)"
            )
            assert "lib/state.sh" in body, f"{name} must install state.sh alongside (litclock-dev#387)"


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
        start = reset_sh_content.find("# Issue #282:")
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
    changed password is a trap with no user-discoverable recovery on Android),
    but gift mode MUST rotate it: the recipient loses nothing, and the gifter
    must not retain a working key to the recipient's setup hotspot.

    These EXECUTE the real block rather than grepping for it — a structural
    assertion that never runs the code is not a guard (the #638 lesson). The
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
        anchor = content.rfind('if [[ "$GIFT_MODE" == "true" ]]; then')
        assert anchor != -1, "terminal GIFT_MODE branch is missing"
        block = content[anchor:]
        for required, why in (
            ('elif [[ "$DO_POWEROFF" == "true" ]]', "the --poweroff arm"),
            ('elif [[ "$DO_REBOOT" == "true" ]]', "the --reboot arm"),
            ("poweroff", "the terminal poweroff"),
        ):
            assert required in block, f"terminal branch lost {why} ({required!r})"
        # Count CALL lines, not substring hits — the surrounding comments name the
        # function too, so `.count()` on the raw text reads 3 for 2 calls.
        calls = [
            ln
            for ln in block.splitlines()
            if ln.strip() == "rotate_hotspot_password_for_handoff" and not ln.lstrip().startswith("#")
        ]
        assert len(calls) == 2, (
            "litclock-dev#660: BOTH handoff arms (gift, and --poweroff when the WiFi "
            f"went with it) must rotate the key; found {len(calls)} call site(s)"
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
            # This repo's terminal branch calls disable_ssh_for_handoff, which
            # upstream does not have (it was authored here, in #52/#53). Without
            # a stub every harness run emitted "command not found" on stderr —
            # swallowed, because the harness deliberately omits `set -e` — which
            # both polluted assertion messages and elided this repo's security
            # gate from every behavioural test.
            'disable_ssh_for_handoff() { echo "STUB_SSH_GATE"; }\n'
            f"GIFT_MODE={gift_mode}\n"
            f"DO_POWEROFF={do_poweroff}\n"
            f"DO_REBOOT={do_reboot}\n"
            f"WIPE_WIFI={wipe_wifi}\n"
            "ENV_WIPE_FAILED=false\n"
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
        assert result.stdout.index("STUB_SSH_GATE") < result.stdout.index("STUB_POWEROFF"), (
            "the SSH gate must run before poweroff"
        )
        assert not pw.exists(), "gift mode must rotate the hotspot password for the new owner"

    def test_pwa_factory_reset_rotates_the_password(self, reset_sh_content, tmp_path):
        """litclock-dev#660 — the PWA "Factory reset" card runs
        `reset-setup.sh --wipe-wifi --strict-env-wipe --poweroff --yes` via
        litclock-reset.service.

        WiFi is wiped, so the next power-on comes up in the setup hotspot — and
        before #660 it came up broadcasting LitClock-Setup with the PREVIOUS
        owner's permanent key, surviving every reset the new owner later
        performed. v0.223.0 had no such leak because the key regenerated every
        provisioning cycle, which makes this a REGRESSION introduced by #620
        rather than a pre-existing gap.
        """
        pw, result, state = self._run(reset_sh_content, tmp_path, "false", do_poweroff="true", wipe_wifi="true")
        assert result.returncode == 0, result.stderr
        assert "STUB_POWEROFF" in result.stdout, "the --poweroff arm must reach poweroff"
        assert result.stdout.index("STUB_SSH_GATE") < result.stdout.index("STUB_POWEROFF"), (
            "the SSH gate must run before poweroff on this arm too (litclock-dev#636)"
        )
        assert not pw.exists(), (
            "litclock-dev#660: --wipe-wifi --poweroff comes back up in the setup hotspot, "
            "so it MUST clear the persisted setup-WiFi key"
        )
        assert not list(state.glob(".hotspot-password.*")), "staging secrets must be swept here too"

    def test_hand_run_poweroff_without_wipe_wifi_keeps_the_password(self, reset_sh_content, tmp_path):
        """The other half of litclock-dev#660, and the reason the discriminator is
        WIPE_WIFI rather than the terminal action.

        A hand-run `sudo reset-setup.sh --poweroff` does NOT set WIPE_WIFI. The
        clock powers off, then boots straight back onto its saved network and
        never raises a hotspot at all — so there is no setup network for a stale
        key to protect, and rotating would strand the owner's phone for nothing.
        The bench QA doc tests exactly this as "same owner, moved house" and
        asserts the password is UNCHANGED.

        Keying #660 on --poweroff instead of --wipe-wifi would have traded the
        leak for a regression against a deliberately QA'd behaviour.
        """
        pw, result, _ = self._run(reset_sh_content, tmp_path, "false", do_poweroff="true", wipe_wifi="false")
        assert result.returncode == 0, result.stderr
        assert "STUB_POWEROFF" in result.stdout, "the --poweroff arm must still reach poweroff"
        assert pw.exists(), "a --poweroff WITHOUT --wipe-wifi is the same-owner path and must PRESERVE the key"
        assert pw.read_text(encoding="utf-8").strip() == "clockwis"

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
                pytest.skip("running as root — unlink cannot be blocked for this shape")
            assert r.returncode != 0, (
                f"a surviving {shape} at the password path must fail the rotation closed, "
                "not print done — the invariant is that NO entry survives, whatever it points at"
            )
            assert "do NOT pass this device on" in r.stdout + r.stderr
        finally:
            state.chmod(0o700)

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
        assert "do NOT pass this device on" in r.stdout or "do NOT pass this device on" in r.stderr
        # litclock-dev#663: rm's own diagnosis must reach the operator —
        # "Is a directory" / "Read-only file system" / "Operation not permitted"
        # need different remedies and "could not remove" distinguishes none.
        assert "Is a directory" in r.stdout or "directory" in r.stdout.lower(), (
            f"rm's cause must be surfaced, got: {r.stdout!r}"
        )

    def test_rotation_call_is_after_the_abort_gate(self, reset_sh_content):
        """A gift prep that ABORTS leaves the device with its current owner —
        rotating there would drop that owner into the trap #620 removes.

        Anchors on the CALL inside the terminal branch, not on a string that
        also appears in the function definition further up the file. The
        original version of this test used `.index("Regenerating hotspot
        password")`, which after #660's refactor resolved to the function body
        and inverted the comparison.
        """
        block = self._terminal_branch(reset_sh_content)
        gate = block.index('if [[ "$ENV_WIPE_FAILED" == "true" ]]')
        rotate = block.index("    rotate_hotspot_password_for_handoff")
        assert gate < rotate, "rotation must come AFTER the #393 env-wipe abort gate"

    def test_plain_reset_keeps_the_password(self, reset_sh_content, tmp_path):
        """The motivating #620 case: the same owner re-provisioning their own
        clock must not be handed a stale-credential dead end.

        litclock-dev#662: this previously executed NO script code — the harness
        substituted a shell no-op for the whole block whenever gift_mode was
        false, so it wrote the file, ran nothing, and asserted the file existed.
        It now runs the real terminal branch with GIFT_MODE=false and no
        terminal-action flag, i.e. the plain `sudo reset-setup.sh` path.
        """
        pw, result, _ = self._run(reset_sh_content, tmp_path, "false")
        assert result.returncode == 0, result.stderr
        assert "STUB_POWEROFF" not in result.stdout, "a plain reset must not power off"
        assert pw.exists(), "a non-gift reset must PRESERVE the hotspot password"
        assert pw.read_text(encoding="utf-8").strip() == "clockwis"

    def test_reboot_path_keeps_the_password(self, reset_sh_content, tmp_path):
        """`--reboot` is the same-owner path too: the clock stays put and comes
        straight back up, so rotating would strand the owner's saved network."""
        pw, result, _ = self._run(reset_sh_content, tmp_path, "false", do_reboot="true")
        assert result.returncode == 0, result.stderr
        assert "STUB_SYSTEMCTL reboot" in result.stdout, "the --reboot arm must reach systemctl reboot"
        assert pw.exists(), "a --reboot reset must PRESERVE the hotspot password"
        assert pw.read_text(encoding="utf-8").strip() == "clockwis"

    def test_wipe_wifi_with_reboot_keeps_the_password(self, reset_sh_content, tmp_path):
        """`--wipe-wifi --reboot` is docs/recovery.md's "full fresh-start", and it
        must PRESERVE the key.

        This is the case that pins the real litclock-dev#660 discriminator. A WiFi
        wipe on its own is NOT the handoff signal: litclock-dev#620's promise is
        that the password "survives a plain factory reset AND a WiFi reset ...
        the motivating user is the same person re-provisioning their own clock",
        and that user's phone is about to rejoin LitClock-Setup. Rotating here
        would be the litclock-dev#620 bug reached through a different flag.

        The signal is wipe AND power-off — WiFi gone (so a hotspot WILL be
        raised) and the device leaving rather than coming back.
        """
        pw, result, _ = self._run(
            reset_sh_content, tmp_path, "false", do_reboot="true", wipe_wifi="true"
        )
        assert result.returncode == 0, result.stderr
        assert "STUB_SYSTEMCTL reboot" in result.stdout, "the --reboot arm must reach systemctl reboot"
        assert pw.exists(), (
            "--wipe-wifi --reboot is the same owner re-provisioning their own clock "
            "(docs/recovery.md 'full fresh-start') and MUST preserve the key"
        )
        assert pw.read_text(encoding="utf-8").strip() == "clockwis"

    def test_bare_wipe_wifi_keeps_the_password(self, reset_sh_content, tmp_path):
        """Same rule with no terminal action at all — the device stays up."""
        pw, result, _ = self._run(reset_sh_content, tmp_path, "false", wipe_wifi="true")
        assert result.returncode == 0, result.stderr
        assert "STUB_POWEROFF" not in result.stdout
        assert pw.exists(), "a bare --wipe-wifi must preserve the key"

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
            ["bash", "-c", program], capture_output=True, text=True, timeout=30,
            env={**os.environ, "LC_ALL": "C"},
        )
        assert r.returncode != 0, (
            "a surviving staging file holds a real past PSK and must fail the rotation closed"
        )
        assert "do NOT pass this device on" in r.stdout + r.stderr

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
            # This repo's terminal branch calls disable_ssh_for_handoff, which
            # upstream does not have (it was authored here, in #52/#53). Without
            # a stub every harness run emitted "command not found" on stderr —
            # swallowed, because the harness deliberately omits `set -e` — which
            # both polluted assertion messages and elided this repo's security
            # gate from every behavioural test.
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
        assert "do NOT pass this device on" in r.stdout + r.stderr

    def test_reset_setup_has_no_other_state_dir_deletion(self, reset_sh_content):
        """litclock-dev#662: `test_wifi_reset_does_not_wipe_the_state_dir` scans
        litclock-wifi-reset.sh only, so reset-setup.sh's OWN fourteen-odd `rm`
        calls were never checked against the #620 preserve-across-reset promise.

        The rotation function is excised before scanning — it is the one place
        that is SUPPOSED to delete the key.
        """
        import re as _re

        fn = self._rotation_fn(reset_sh_content)
        rest = reset_sh_content.replace(fn, "")
        for lineno, line in enumerate(rest.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            # Not just `rm`: `: > file` truncates, `mv` relocates, `shred -u`
            # and `find -delete` remove. All destroy the #620 promise equally.
            destroys = _re.search(r"\b(rm|shred|unlink|mv|truncate|find)\b", line) or _re.search(
                r">\s*\"?\$STATE_DIR", line
            )
            if destroys and _re.search(r"STATE_DIR|/var/lib/litclock|hotspot-password", line):
                raise AssertionError(
                    f"line {lineno} deletes hotspot state outside the rotation function, "
                    f"breaking the #620 survives-a-plain-reset promise: {line!r}"
                )

    def test_state_dir_is_overridable_like_the_other_scripts(self, reset_sh_content):
        assert 'STATE_DIR="${LITCLOCK_STATE_DIR:-/var/lib/litclock}"' in reset_sh_content

    def test_wifi_reset_does_not_wipe_the_state_dir(self):
        """A substring check for 'hotspot-password' would stay green if the
        script ever did `rm -rf $STATE_DIR` — which destroys the same
        invariant, on the exact moved-house scenario #620 is about."""
        import re as _re

        wifi_reset = (REPO_ROOT / "scripts" / "litclock-wifi-reset.sh").read_text()
        for lineno, line in enumerate(wifi_reset.splitlines(), 1):
            # Not just `rm`: `: > file` truncates, `mv` relocates, `shred -u`
            # and `find -delete` remove. All destroy the #620 promise equally.
            destroys = _re.search(r"\b(rm|shred|unlink|mv|truncate|find)\b", line) or _re.search(
                r">\s*\"?\$STATE_DIR", line
            )
            if destroys and _re.search(r"STATE_DIR|/var/lib/litclock|hotspot-password", line):
                raise AssertionError(f"line {lineno} deletes state a WiFi reset must preserve (#620): {line!r}")

    def test_sd_cloning_rotates_the_password(self):
        """prepare-for-cloning.sh clones ONE card into MANY for other people.
        Without this, every clone ships the same permanent WPA2 key."""
        clone = (REPO_ROOT / "scripts" / "prepare-for-cloning.sh").read_text()
        assert "hotspot-password" in clone, (
            "prepare-for-cloning.sh must clear the persisted setup-hotspot password (#620) — "
            "otherwise every cloned card broadcasts LitClock-Setup with the SAME key"
        )
        assert ".hotspot-password.*" in clone, "must also sweep orphaned staging files"
