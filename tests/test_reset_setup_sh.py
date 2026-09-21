"""Tests for scripts/reset-setup.sh (issue litclock-dev#160)."""

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RESET_SH = REPO_ROOT / "scripts" / "reset-setup.sh"


STATE_SH = REPO_ROOT / "scripts" / "lib" / "state.sh"

from tests.state_sh_lifts import extract_history_helper  # noqa: E402


@pytest.fixture(scope="module")
def reset_sh_content():
    return RESET_SH.read_text()


def _run_defaults_assignment(reset_sh_content, gift_language_code):
    """EXECUTE reset-setup.sh's own `DEFAULTS=...` line and return the body.

    litclock-dev#840 moved the env.sh block into `env_sh_defaults()` in
    lib/state.sh, so reset-setup.sh no longer contains any of the keys these
    tests are about — a source grep would observe nothing. Lift the assignment
    VERBATIM, source the real helper, and read what the script would hand to
    `atomic_write_env_sh`.

    Lifting by text (not re-typing the call) is what keeps this honest: a
    substring check for `env_sh_defaults "$GIFT_LANGUAGE_CODE"` passes just as
    well when the quotes are single, and the single-quoted form would seed the
    literal string `$GIFT_LANGUAGE_CODE` on every gifted device.
    """
    m = re.search(r"^\s*DEFAULTS=.*$", reset_sh_content, re.M)
    assert m, "reset-setup.sh has no DEFAULTS assignment"
    assert len(re.findall(r"^\s*DEFAULTS=", reset_sh_content, re.M)) == 1, (
        "expected exactly one DEFAULTS assignment; the lift below would otherwise be ambiguous"
    )
    harness = (
        f'. "{STATE_SH}"\n'
        f'GIFT_LANGUAGE_CODE="{gift_language_code}"\n'
        f"{m.group(0)}\n"
        'printf %s "$DEFAULTS"'
    )
    proc = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"DEFAULTS assignment failed: {proc.stderr}"

    # FULL EQUALITY, not "it mentions env_sh_defaults" (PR litclock-dev#858 review, item B).
    # litclock-dev#783's guard compared every discovered write's key set AND comment
    # status against the sample, per caller. Checking only that the right-hand
    # side names the helper is satisfied by `$(env_sh_defaults | grep -v
    # RENDER_LEAD)` — a pipeline that ships a body missing a documented knob,
    # which is exactly the drift class litclock-dev#840 exists to end. Equality against
    # the helper's own output subsumes the key set, the comment status, the key
    # ORDER and the trailing newline in one assertion.
    from tests.test_first_boot_flow import env_sh_defaults

    expected = env_sh_defaults(gift_language_code)
    assert proc.stdout == expected, (
        "reset-setup.sh's DEFAULTS is not env_sh_defaults() output. It must be the helper's body "
        "verbatim — a filter, a re-ordering or an appended line is the hand-maintained drift "
        f"litclock-dev#840 removed.\n--- got ---\n{proc.stdout!r}\n--- want ---\n{expected!r}"
    )
    return proc.stdout


def _extract_state_helper_gate(reset_sh_content):
    """The `for _fn in ...` state.sh-helper guard, verbatim.

    Anchored on `for _fn in` ALONE, deliberately. Anchoring on the full
    `for _fn in atomic_write_env_sh env_sh_defaults; do` would make a mutant
    that NARROWS the list (dropping env_sh_defaults — the precise regression
    these tests exist for) fail on a missing span instead of on behaviour,
    which reports a broken test rather than a broken guard.
    """
    start = reset_sh_content.index("for _fn in ")
    end = reset_sh_content.index("\ndone\n", start) + len("\ndone\n")
    span = reset_sh_content[start:end]
    assert "exit 1" in span, "span lost the abort under test"
    return span


class TestTooOldStateSh:
    """litclock-dev#840 / PR litclock-dev#858 review item A — a state.sh that defines the
    writer but not `env_sh_defaults` must stop this script DEAD.

    Reachable, not hypothetical: `update.sh` installs the root-owned copy of
    reset-setup.sh in its privilege-helper loop and the root-owned
    lib/state.sh AFTERWARDS, so an interrupted or half-failed update leaves a
    NEW script beside an OLD state.sh.

    Why it is this script that needs the guard for CORRECTNESS: reset-setup.sh
    has no `set -e`. Without the gate, `env_sh_defaults` prints "command not
    found", the command substitution yields the empty string,
    `atomic_write_env_sh` writes it SUCCESSFULLY, and Step 3 reports a green
    "done" over a one-byte env.sh — every knob and the gift language gone, on a
    device usually about to be shipped to someone.
    """

    def _run(self, reset_sh_content, tmp_path, *, define_defaults):
        install = tmp_path / "install"
        install.mkdir()
        defaults_fn = f'. "{STATE_SH}"\n' if define_defaults else ""
        m = re.search(r"^\s*DEFAULTS=.*$", reset_sh_content, re.M)
        harness = f"""
RED=""; GREEN=""; YELLOW=""; NC=""
_THIS_SCRIPT_DIR={shlex.quote(str(tmp_path))}
INSTALL_DIR={shlex.quote(str(install))}
GIFT_LANGUAGE_CODE=""
# An OLD lib/state.sh: the writer exists, the defaults helper does not.
atomic_write_env_sh() {{ printf "%s" "$2" > "$1"; return 0; }}
{defaults_fn}
{_extract_state_helper_gate(reset_sh_content)}
echo -n "Resetting configuration... "
{m.group(0)}
if atomic_write_env_sh "$INSTALL_DIR/env.sh" "$DEFAULTS"; then
    echo "done"
else
    echo "FAILED"
fi
"""
        r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=10)
        return r, install / "env.sh"

    def test_an_old_state_sh_aborts_before_writing_anything(self, reset_sh_content, tmp_path):
        r, env_sh = self._run(reset_sh_content, tmp_path, define_defaults=False)
        assert r.returncode == 1, f"expected the gate to abort, got rc={r.returncode}\n{r.stdout}{r.stderr}"
        assert not env_sh.exists(), (
            f"an env.sh was written with a too-old state.sh: {env_sh.read_text()!r}. Without the "
            "gate this file is ONE BYTE and the banner still says done."
        )
        assert "done" not in r.stdout, f"the green success banner printed on a failed reset: {r.stdout!r}"
        assert "env_sh_defaults is not defined" in r.stderr, (
            f"the abort must name the missing helper, not just die: {r.stderr!r}"
        )
        assert "too old" in r.stderr, "the abort must say what is wrong with state.sh"

    def test_a_current_state_sh_still_writes_the_full_body(self, reset_sh_content, tmp_path):
        """The inverse, so the gate cannot pass by refusing everything."""
        r, env_sh = self._run(reset_sh_content, tmp_path, define_defaults=True)
        assert r.returncode == 0, f"{r.stdout}{r.stderr}"
        from tests.test_first_boot_flow import env_sh_defaults

        assert env_sh.read_text() == env_sh_defaults("")


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
        seeded EMPTY so a reset clears the prior city — otherwise a reset
        device's Status/splash would show the previous owner's location.

        EXECUTED since litclock-dev#840 (the body lives in lib/state.sh now);
        the old version sliced 400 characters after `DEFAULTS=` out of the
        source, which today contains no keys at all."""
        body = _run_defaults_assignment(reset_sh_content, "")
        assert "export WEATHER_LOCATION_NAME=\n" in body


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


def test_defaults_include_weather_location_mode_and_ip_country(reset_sh_content):
    """litclock-dev#337 A3 + /review testing-gap: a gift-mode reset must seed the MODE +
    IP_COUNTRY defaults. Without these, a gift-recipient whose first-boot
    IP-geo fails would inherit the gifter's stale MODE=specific AND no
    IP_COUNTRY baseline — on-boot reresolve would never fire.

    EXECUTED since litclock-dev#840: the body now comes from
    `env_sh_defaults()` in lib/state.sh, so a grep of reset-setup.sh sees no
    such literal. Run the script's own DEFAULTS assignment and read the body
    it actually produces."""
    body = _run_defaults_assignment(reset_sh_content, "")
    assert "export WEATHER_LOCATION_MODE=auto\n" in body, "litclock-dev#337 A3: the reset body must seed MODE=auto"
    assert "export WEATHER_IP_COUNTRY=\n" in body, (
        "litclock-dev#337 A3: the reset body must seed WEATHER_IP_COUNTRY= (empty)")


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


# litclock-dev#764: reset-setup.sh calls disable_ssh_for_handoff from both
# terminal branches. Every harness below that splices in _terminal_branch must
# define it, or the run emits "command not found" on stderr and CONTINUES —
# swallowed, because these harnesses deliberately omit `set -e` (mirroring the
# script). That elided the SSH-off security gate from every behavioural test of
# both handoff arms while they all stayed green.
#
# One definition, three call sites: the rationale drifting between verbatim
# copies 470 lines apart is how the next reader concludes the stub is decorative.
# The stub alone proves nothing either way — what makes it load-bearing is the
# assertion at each site: positive (the gate ran, before poweroff) on the two
# handoff arms, negative (it did NOT run) on every path that leaves the device
# with its current owner.
_SSH_GATE_STUB = 'disable_ssh_for_handoff() { echo "STUB_SSH_GATE"; }\n'

# litclock-dev#833: both re-arms of litclock-shutdown.service (Step 1's litclock-dev#727
# one and the plain-arm one) go through `timeout N systemctl start ...`. An
# external `timeout` binary cannot see a shell function, so any harness that
# executes Step 1 or rearm_shutdown_splash with `systemctl` shadowed MUST also
# shadow `timeout`, or the re-arm reaches the REAL systemctl. Kept in every
# harness that executes lifted script code, even where the lifted span holds
# no `timeout` today — the cost is one line, the failure mode is silent.
_TIMEOUT_STUB = 'timeout() { shift; "$@"; }\n'


_extract_history_helper = extract_history_helper


def _extract_handoff_history_fn(content: str) -> str:
    """reset-setup.sh's clear_shell_history_for_handoff, verbatim, with its
    fail-closed parts asserted (the litclock-dev#662 rule)."""
    assert content.count("clear_shell_history_for_handoff() {") == 1
    start = content.index("clear_shell_history_for_handoff() {")
    end = content.index("\n}\n", start) + len("\n}\n")
    fn = content[start:end]
    for required, why in (
        ('clear_and_lock_bash_history "$_PI_BASH_HISTORY" "$_ROOT_BASH_HISTORY"', "the shared wipe+lock call"),
        ("exit 1", "the fail-closed abort"),
        ("do NOT hand this device on", "the operator warning"),
        ("NOT powering off", "the refusal to power off"),
    ):
        assert required in fn, f"clear_shell_history_for_handoff lost {why} ({required!r})"
    return fn


def _history_fixture(tmp_path):
    """Two history files with an owner's commands in them, at paths the lifted
    handoff arms are pointed at by redefining the script's two fixed
    variables (same convention as the cloning harness)."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    pi, root = home / "pi.bash_history", home / "root.bash_history"
    # A test that pre-stages a survivor (a non-empty directory at the path)
    # keeps it; the fixture only supplies the ordinary owner's-history shape.
    if not pi.exists():
        pi.write_text("nmcli dev wifi connect HomeNet password hunter2\n", encoding="utf-8")
    if not root.exists():
        root.write_text("sudo -i\n", encoding="utf-8")
    decl = f"_PI_BASH_HISTORY={shlex.quote(str(pi))}\n_ROOT_BASH_HISTORY={shlex.quote(str(root))}\n"
    return pi, root, decl


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
        # litclock-dev#868: the history wipe is a THIRD span, hoisted between
        # Step 7 and the "Reset Complete!" banner for both handoff arms. Its
        # condition is inside the lifted text, so the non-handoff arms exercise
        # the guard rather than an absence.
        hist = content.find(
            'if [[ "$ENV_WIPE_FAILED" != "true" && ( "$GIFT_MODE" == "true" || "$DO_POWEROFF" == "true" ) ]]; then'
        )
        assert hist != -1, "litclock-dev#868: the hoisted handoff history-wipe guard is missing"
        assert hoist_end <= hist < anchor, (
            "the history wipe must sit after the rotation guard and before the terminal branch"
        )
        hist_end = content.index("\nfi\n", hist) + len("\nfi\n")
        hist_block = content[hist:hist_end]
        assert "clear_shell_history_for_handoff" in hist_block, (
            f"lifted history guard has no call in it: {hist_block!r}"
        )
        assert "NetworkManager" not in hist_block
        block = hoist_block + "\n" + hist_block + "\n" + content[anchor:]
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
            f"{_TIMEOUT_STUB}"
            # See _SSH_GATE_STUB. What makes it load-bearing here is the pair of
            # positive assertions below: the gate ran, and it ran before poweroff.
            f"{_SSH_GATE_STUB}"
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
            # litclock-dev#868: the handoff arms wipe+lock the shell history.
            # Real helper, real function, paths redirected into tmp_path/home.
            f"{_history_fixture(tmp_path)[2]}"
            f"{_extract_history_helper()}\n"
            f"{_extract_handoff_history_fn(content)}\n"
            f"{extra}\n"
            f"{self._rotation_fn(content)}\n"
            f"{self._terminal_branch(content)}"
        )
        result = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=30)
        # litclock-dev#833: if a re-arm ever reaches the real systemctl (the
        # `timeout` stub gone missing) it fails as non-root and prints this.
        assert "WARNING: could not re-arm" not in result.stdout, result.stdout
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
        # The gate is NOT conditioned on WIPE_WIFI — `--keep-wifi --poweroff` takes
        # the same poweroff arm and disables SSH there too, which the bench QA doc
        # calls out as the sub-step that ends your session with no hotspot to fall
        # back on. Without this the arm's SSH-off coverage existed only under
        # wipe_wifi=true, so gating the call on the wipe would have stayed green.
        assert "STUB_SSH_GATE" in result.stdout, (
            "--keep-wifi --poweroff is still a poweroff, and the poweroff arm disables SSH"
        )
        assert result.stdout.index("STUB_SSH_GATE") < result.stdout.index("STUB_POWEROFF"), (
            "the SSH gate must run before poweroff on this arm too"
        )
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
        hist_pi, _, hist_decl = _history_fixture(tmp_path)
        default_line = next(ln for ln in content.splitlines() if ln.startswith("WIPE_WIFI="))
        parse_start = content.index("while [[ $# -gt 0 ]]; do")
        parse_end = content.index("done", parse_start) + len("done")

        program = (
            "set -u\n"
            'poweroff() { echo "STUB_POWEROFF"; }\n'
            'systemctl() { echo "STUB_SYSTEMCTL $*"; }\n'
            f"{_TIMEOUT_STUB}"
            # See _SSH_GATE_STUB. This harness runs the parser with NO flags, so it
            # falls to the "Reboot to enter setup mode" branch. The stub is what lets
            # the NEGATIVE assertion below observe that the gate stayed away — without
            # it, a regression that started disabling SSH on the plain same-owner
            # reset would be invisible rather than red.
            f"{_SSH_GATE_STUB}"
            "AUTO_YES=false\nDO_REBOOT=false\nDO_POWEROFF=false\n"
            "STRICT_ENV_WIPE=false\nGIFT_MODE=false\nGIFT_MESSAGE_FILE=''\nENV_WIPE_FAILED=false\n"
            f"{default_line}\n"
            f"{content[parse_start:parse_end]}\n"
            f"CONFIG_DIR={config}\n"
            f"LITCLOCK_STATE_DIR={state}\n"
            f"{self._state_dir_line(content)}\n"
            'RED=""\nGREEN=""\nYELLOW=""\nNC=""\n'
            f"{hist_decl}"
            f"{_extract_history_helper()}\n"
            f"{_extract_handoff_history_fn(content)}\n"
            f"{self._rotation_fn(content)}\n"
            f"{self._terminal_branch(content)}"
        )
        # No arguments: the plain `sudo reset-setup.sh` a person actually types.
        result = subprocess.run(["bash", "-c", program, "bash"], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        # litclock-dev#868: the plain reset keeps the operator's history — it hands
        # control back to them, not to a new owner.
        assert hist_pi.is_file() and "hunter2" in hist_pi.read_text(encoding="utf-8"), (
            "the plain no-flag reset must NOT wipe the shell history — it is the same-owner path"
        )
        assert "WARNING: could not re-arm" not in result.stdout, result.stdout  # litclock-dev#833: see _run
        assert not pw.exists(), (
            "a no-argument factory reset did not rotate the setup network's password. "
            "The parser default and the terminal branch each pass in isolation, so only "
            "this end-to-end path catches the two disagreeing."
        )
        # The same end-to-end reasoning for the OTHER half of the handoff contract:
        # the operator-facing help says only --poweroff and --gift-mode disable SSH,
        # and this is the invocation a person actually types on a clock they are
        # keeping. Real parser + real terminal branch, so a default flip that routed
        # the plain reset into the poweroff arm would show up here.
        assert "STUB_SSH_GATE" not in result.stdout, (
            "the plain no-flag reset must NOT disable SSH — it is the same-owner path"
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
        # litclock-dev#764 /review: SSH-off is a HANDOFF action, so it must fire on
        # the poweroff arm and on NEITHER of the others. Only the presence half was
        # asserted before, and presence-only is one-directional: adding the call to
        # the reboot arm — a plain reset that leaves the clock with its owner, now
        # silently stripped of remote access — left the whole suite green. Measured,
        # both arms.
        if label == "poweroff":
            assert "STUB_SSH_GATE" in result.stdout, "the poweroff arm is a handoff and must disable SSH"
            assert result.stdout.index("STUB_SSH_GATE") < result.stdout.index("STUB_POWEROFF"), (
                "the SSH gate must run before poweroff"
            )
        else:
            assert "STUB_SSH_GATE" not in result.stdout, (
                f"the {label} arm is NOT a handoff — the clock stays with its owner, who "
                "needs SSH. Disabling it here strands them on their own device."
            )
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
            f"{_TIMEOUT_STUB}"
            # See _SSH_GATE_STUB. Load-bearing here for the OPPOSITE reason: this
            # harness asserts the gate is ABSENT after a failed rotation, and without
            # the stub a regression that DID call it would emit "command not found"
            # and the negative assertion would still pass.
            f"{_SSH_GATE_STUB}"
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
            "CURRENT owner, who needs remote access to diagnose the card. The structural "
            "half is test_it_runs_after_the_fail_closed_gates plus the exact call-site "
            "count — this is the executing half."
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
            # litclock-dev#847 item 1 (litclock-dev#854 review) — the runtime-render
            # validation memo: a result token, an rc, a reason and a SHA,
            # describing THIS device's freetype. Not a credential, and
            # per-device state the next owner must not inherit. Listed here
            # deliberately rather than loosening the scan (this guard caught
            # the addition, which is what it is for).
            "runtime-render-validation.json",
    # litclock-dev#871 Stage A: the self-test pass record, per-device like the memo.
    "runtime-render-selftest.json",
        # litclock-dev#871 Stage A: the self-test pass record, per-device like the memo.
        "runtime-render-selftest.json",
            # litclock-dev#871 Stage A: the self-test pass record, per-device like the memo.
            "runtime-render-selftest.json",
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
# location. "Counterpart" is whichever repo this one is NOT: here that is the
# development checkout at ~/litclock-dev, and in the development repo it is
# this public checkout at ~/litclock. The default below is the documented
# maintainer layout FOR THIS REPO; porting this file across without flipping
# it is what the guards below catch — and that is not hypothetical, it
# happened on the v0.226.0 port (litclock-dev#765).
#
# The override name is repo-relative, so it must be set PER REPO (a direnv
# .envrc, not ~/.bashrc and not a shared workflow `env:` block): one exported
# value cannot mean "the other repo" in two repos. Deliberately NOT reading
# LITCLOCK_PUBLIC_CHECKOUT here: in this repository that variable's documented
# value is this repository's own path, so honouring it would resolve the
# counterpart to ourselves — the development copy reads it as the
# pre-litclock-dev#765 name, and there it does name the counterpart.
_COUNTERPART_ENV_KEYS = ("LITCLOCK_COUNTERPART_CHECKOUT",)
_DEFAULT_COUNTERPART_CHECKOUT = "/home/ankush/litclock-dev"


def _resolve_counterpart_checkout(env) -> str:
    """The counterpart checkout root, or "" for "there is no counterpart".

    Set-but-EMPTY is an explicit opt-out, not a fall-through. That is the
    standard CI-yaml way to unset a variable, and the previous `or` chain
    quietly ignored it — on a machine where the default path happens to exist
    (the maintainer's), asking for no counterpart still ran the check against
    the real one. Empty now means what it looks like it means.

    A non-absolute override is rejected outright rather than resolved: relative
    to the repo root, ``scripts/reset-setup.sh`` IS this repo's own copy, which
    is the vacuous self-comparison all over again (/review litclock-dev#711).
    """
    for key in _COUNTERPART_ENV_KEYS:
        if key not in env:
            continue
        value = env[key].strip()
        if not value:
            return ""
        if not os.path.isabs(value):
            raise ValueError(f"{key} must be absolute, got {value!r}")
        return value
    return _DEFAULT_COUNTERPART_CHECKOUT


_COUNTERPART_CHECKOUT = _resolve_counterpart_checkout(os.environ)
_COUNTERPART_RESET_SH = Path(_COUNTERPART_CHECKOUT or "/nonexistent") / "scripts" / "reset-setup.sh"


# The guard that makes the vacuous case impossible rather than merely unlikely.
# A cross-repo check pointed at this repo's OWN file asserts `X == X` and can
# never go red, so it reports the property as verified while protecting nothing.
# That is exactly what a port of this file introduces when the default above
# still names the repo it came from, and it is invisible: the suite stays green
# and `skipif` never fires, because the path does exist.
#
# `samefile`, not a resolved-path comparison: two paths can resolve differently
# and still be the same inode (a hard-linked clone, a bind mount), which is the
# self-comparison wearing a different name. Identity is the actual question.
#
# Takes the path as an argument rather than reading the module constant: the
# whole `is_file() and not self` conjunction is the load-bearing part, and a
# test that can only reach the inner predicate leaves dropping the outer term a
# silent, green mutation (measured — it was).
#
# `is_file`, not `exists`: a directory at that path exists and is not us, so it
# reported AVAILABLE and the parity test then died on IsADirectoryError instead
# of skipping with a diagnosis.
#
# The except clause is wider than OSError on purpose. Path.resolve raises
# RuntimeError on a symlink loop and ValueError on an embedded NUL — neither an
# OSError — and both are unreachable today only because is_file() short-circuits
# first. That ordering is not something the next refactor should have to know:
# a raise here is a COLLECTION error that takes the whole file down, every
# CI-side gate in it included.
def _counterpart_available(counterpart: Path, ours: Path = RESET_SH) -> bool:
    # `ours` is injectable only so the tests can build both sides inside tmp_path
    # — a hard-linked pair cannot be constructed against the real repo without
    # writing into it, and the hardlink case is the one `samefile` exists for.
    try:
        if not counterpart.is_file():
            return False
        return not counterpart.samefile(ours)
    except (OSError, ValueError, RuntimeError):
        return False


_COUNTERPART_AVAILABLE = _counterpart_available(_COUNTERPART_RESET_SH)


class TestSshHandoffGate:
    """litclock-dev#528, back-ported to dev by litclock-dev#657.

    The function and its header comment are BYTE-IDENTICAL to the counterpart
    repository's — checked below, so the two cannot drift on the one
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

        The maintainer-local test below still compares against the counterpart
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
        the counterpart's WORKING TREE — whatever branch and uncommitted state is
        checked out — and it skips into a green suite wherever the checkout is
        absent. The golden test above is the CI-side floor; this one is the
        maintainer-local cross-check that the OTHER repo still agrees.
        """
        # The skipif is the only other consumer of the guard, and a decorator
        # cannot be asserted on. litclock-dev#764 /review measured the gap:
        # reverting `not _COUNTERPART_AVAILABLE` to a bare `not ....exists()`
        # alongside a self-naming default reproduces litclock-dev#765 exactly —
        # this test passes vacuously AND the guard's own test still passes,
        # because it never touches the wiring. Re-asserting here is what makes
        # the decorator load-bearing.
        assert _counterpart_available(self.COUNTERPART_RESET_SH), (
            "the skipif must gate on the self-comparison guard, not on a bare exists()"
        )
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

    def test_the_shipped_counterpart_path_does_not_name_this_repo(self):
        """NEVER skipped — this is the one that goes red on a bad port.

        litclock-dev#765 /review: the self-comparison guard downgrades the
        defect from a vacuous PASS to a silent SKIP, which is safer but still
        unsignalled. On a ported copy whose default was not flipped that skip is
        the permanent steady state: the cross-repo parity check never runs
        again, on any machine, wearing the CI skip reason, and nothing says so.

        A pure path comparison needs no second checkout, so it runs on CI in
        BOTH repositories and fails the port that forgot to flip the literal —
        or an override exported once for both repos, which is the same mistake
        by another route.
        """
        # SKIP, not fail, for the explicit opt-out (/review). A set-but-empty
        # LITCLOCK_COUNTERPART_CHECKOUT (the only override this repo reads —
        # see _COUNTERPART_ENV_KEYS) is a DOCUMENTED, supported configuration —
        # `test_the_counterpart_resolution_rejects_what_it_says_it_rejects`
        # asserts exactly that behaviour — so exercising it must not turn the
        # whole suite red. Failing here made pass/fail depend on an ambient
        # environment variable, which is the test-isolation problem this file
        # takes trouble to avoid elsewhere.
        #
        # The hard assertion below is the one the docstring is about, and it
        # still never skips: it is the self-naming path that goes red on a bad
        # port.
        if _COUNTERPART_CHECKOUT == "":
            pytest.skip(
                "counterpart explicitly opted out via "
                f"{' / '.join(_COUNTERPART_ENV_KEYS)}; unset to run the cross-repo parity check"
            )
        assert Path(_COUNTERPART_CHECKOUT).resolve() != REPO_ROOT.resolve(), (
            f"the counterpart checkout resolves to THIS repository ({REPO_ROOT}). The "
            "cross-repo parity check would compare the file to itself, or skip forever "
            "pretending to be CI. Flip the default in _DEFAULT_COUNTERPART_CHECKOUT, or "
            "set the override per-repo rather than once for both."
        )

    def test_the_counterpart_resolution_rejects_what_it_says_it_rejects(self):
        """litclock-dev#764 /review: the resolution block reasons at length about
        relative paths and set-but-empty vars, and had zero coverage — deleting
        the `isabs` raise (the /review litclock-dev#711 protection) went unnoticed, and the
        empty-var behaviour did not match the prose above it.
        """
        assert _resolve_counterpart_checkout({}) == _DEFAULT_COUNTERPART_CHECKOUT

        for key in _COUNTERPART_ENV_KEYS:
            assert _resolve_counterpart_checkout({key: "/some/where"}) == "/some/where"
            assert _resolve_counterpart_checkout({key: ""}) == "", (
                f"a set-but-empty {key} is an explicit opt-out, not a fall-through to "
                "the maintainer's default — that default exists on the one machine "
                "where opting out matters"
            )
            with pytest.raises(ValueError):
                _resolve_counterpart_checkout({key: "scripts/reset-setup.sh"})

        # The property this repo's header comment calls deliberate, pinned:
        # LITCLOCK_PUBLIC_CHECKOUT names THIS repository, so honouring it would
        # resolve the counterpart to ourselves (the litclock-dev#765 self-
        # comparison). The development copy reads it as the pre-litclock-dev#765 name; a
        # hand-merge that copies its two-key tuple across is exactly what this
        # assertion catches — the port review measured that the previous
        # precedence check stayed green with the key re-added.
        assert "LITCLOCK_PUBLIC_CHECKOUT" not in _COUNTERPART_ENV_KEYS, (
            "this repository must not read LITCLOCK_PUBLIC_CHECKOUT (see the header "
            "comment above _COUNTERPART_ENV_KEYS)"
        )
        assert _resolve_counterpart_checkout(
            {"LITCLOCK_PUBLIC_CHECKOUT": "/b"}
        ) == _DEFAULT_COUNTERPART_CHECKOUT, (
            "LITCLOCK_PUBLIC_CHECKOUT is ignored here; only the default may answer"
        )

    def test_the_self_comparison_guard_is_not_decorative(self, tmp_path):
        """litclock-dev#765: the parity test above is skipped on CI, so nothing in
        a normal run exercises the guard that decides WHY it skips. Without this,
        deleting the self-comparison term leaves the suite green everywhere — and
        reintroduces the exact defect the port hit: a counterpart path that names
        this repo makes the assertion `X == X`, which passes vacuously while
        `skipif` never fires, because the file does exist.

        Driven through the real availability predicate, not a reimplementation of
        it, and through the whole `is_file() and not self` conjunction rather than
        the inner comparison alone — dropping the outer term was a mutation that
        survived a version of this test that only reached the inner half.
        """
        assert not _counterpart_available(RESET_SH), (
            "pointing at our own file must report UNAVAILABLE (skip), never available"
        )

        indirect = REPO_ROOT / ".." / REPO_ROOT.name / "scripts" / "reset-setup.sh"
        assert not _counterpart_available(indirect), (
            "a `..`-laden path that resolves to our own file is the same self-comparison"
        )

        # ...and the symlink half, which the `..` case does NOT cover: plain string
        # normalisation handles `..` and would pass the case above while letting a
        # symlinked checkout run the parity test against itself (measured).
        link = tmp_path / "linked-repo"
        link.symlink_to(REPO_ROOT, target_is_directory=True)
        assert not _counterpart_available(link / "scripts" / "reset-setup.sh"), (
            "a symlink to this repo is this repo — compare file IDENTITY, not path text"
        )

        assert not _counterpart_available(Path("/nonexistent/repo/scripts/reset-setup.sh")), (
            "an absent counterpart is unavailable too (the CI case)"
        )

        a_directory = tmp_path / "scripts"
        a_directory.mkdir()
        assert not _counterpart_available(a_directory), (
            "a directory is not a readable counterpart — it exists and is not us, so an "
            "exists()-based guard called it available and the parity test then died on "
            "IsADirectoryError instead of skipping with a diagnosis"
        )

        loop = tmp_path / "loop"
        loop.symlink_to(tmp_path / "loop2")
        (tmp_path / "loop2").symlink_to(loop)
        assert not _counterpart_available(loop), "a symlink loop must fail closed, not raise"
        assert not _counterpart_available(Path("/nul\x00byte")), (
            "ValueError from an embedded NUL must fail closed too — a raise here is a "
            "COLLECTION error that takes down every test in this file, CI gates included"
        )

        # The hardlink case, which is why this is `samefile` and not a resolved-path
        # comparison: two paths can resolve differently and still be the same inode
        # (a `cp -al` clone, a bind mount). Measured — swapping samefile back for
        # resolve()-inequality survives every OTHER case in this test.
        ours = tmp_path / "ours" / "scripts" / "reset-setup.sh"
        ours.parent.mkdir(parents=True)
        ours.write_text(RESET_SH.read_text(), encoding="utf-8")
        hardlinked = tmp_path / "hardlinked" / "scripts" / "reset-setup.sh"
        hardlinked.parent.mkdir(parents=True)
        os.link(ours, hardlinked)
        assert hardlinked.resolve() != ours.resolve(), "precondition: two distinct paths"
        assert not _counterpart_available(hardlinked, ours=ours), (
            "a hard link is the same file under another name — comparing resolved paths "
            "calls it a different repo and the parity check goes vacuous again"
        )

        genuine = tmp_path / "counterpart" / "scripts" / "reset-setup.sh"
        genuine.parent.mkdir(parents=True)
        genuine.write_text(RESET_SH.read_text(), encoding="utf-8")
        assert _counterpart_available(genuine), (
            "a real second checkout MUST be available — a guard that reports "
            "unavailable for everything would skip the parity check into oblivion"
        )

    def test_the_guard_fails_closed_on_every_error_class(self, monkeypatch, tmp_path):
        """The contract is "any failure means unavailable", and the except clause
        that delivers it is wider than OSError for a measured reason: Path.resolve
        raises RuntimeError on a symlink loop and ValueError on an embedded NUL,
        neither of which is an OSError. Today both are unreachable through this
        entry point because is_file() short-circuits first — which makes the
        widening look like dead defensiveness and invites the next refactor to
        narrow it. A raise here is not one red test: it is a COLLECTION error that
        takes down all of this file, every CI-side gate in it included.
        """
        ours = tmp_path / "ours" / "scripts" / "reset-setup.sh"
        ours.parent.mkdir(parents=True)
        ours.write_text("#!/bin/bash\n", encoding="utf-8")
        counterpart = tmp_path / "theirs" / "scripts" / "reset-setup.sh"
        counterpart.parent.mkdir(parents=True)
        counterpart.write_text("#!/bin/bash\n", encoding="utf-8")
        assert _counterpart_available(counterpart, ours=ours), "precondition: available"

        for exc in (OSError, ValueError, RuntimeError):

            def _raise(self, other, _exc=exc):
                raise _exc("synthetic")

            monkeypatch.setattr(Path, "samefile", _raise)
            assert not _counterpart_available(counterpart, ours=ours), (
                f"{exc.__name__} from the identity check must fail closed, not propagate"
            )

    def test_no_dev_image_exception_survives(self, reset_sh_content):
        """The removed exception, pinned as removed — by SHAPE, not by name.

        The first version of this listed the three names the PR had just
        deleted, which are exactly the three nobody would reuse. Measured
        against a CI-like run (counterpart checkout absent, so the parity test
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

    def test_it_is_called_from_exactly_those_two_places(self, reset_sh_content):
        """The exclusivity property, pinned where the harnesses cannot see it.

        litclock-dev#764 /review, measured: `_terminal_branch` lifts the span
        starting AT the hoisted non-gift rotation guard, so a
        `disable_ssh_for_handoff` inserted one line ABOVE that anchor is outside
        every behavioural harness's field of view. That placement is the worst
        one there is — SSH off unconditionally, on every reset, BEFORE the
        rotation's fail-closed `exit 1` — and it left all 135 tests green.

        A whole-file call-site count is what closes it, the same shape the
        rotation already uses (`len(calls) == 2`). Presence-in-two-spans cannot:
        it is one-directional and says nothing about a third site anywhere else.
        """
        import re as _re

        calls = [
            (lineno, ln)
            for lineno, ln in enumerate(reset_sh_content.splitlines(), 1)
            if not ln.lstrip().startswith("#")
            and _re.search(r'(?<![A-Za-z0-9_"])disable_ssh_for_handoff(?![A-Za-z0-9_(])', ln)
        ]
        assert len(calls) == 2, (
            "disable_ssh_for_handoff must be called from EXACTLY two places — the gift "
            "arm and the litclock-dev#627 poweroff arm. Any other site turns a handoff "
            "action into an unconditional one, and the behavioural harnesses cannot see "
            f"a call placed above the lifted terminal span; found {len(calls)}: {calls}"
        )

        # ...and both of them are the terminal arms, not merely two of something.
        tail_at = reset_sh_content.index("Reset Complete!")
        reboot_at = reset_sh_content.index('elif [[ "$DO_REBOOT"', tail_at)
        # Offsets from the ENUMERATED line number, not from searching the text
        # (/review). Both call sites are the byte-identical line
        # `    disable_ssh_for_handoff`, so `reset_sh_content.index(ln)` returned
        # the FIRST site's offset for BOTH iterations and this window was
        # asserted twice against the same position — the second site was
        # unchecked. Mutation-verified at the time: moving the poweroff-arm call
        # into the plain no-poweroff/no-reboot `else` arm, where the device is
        # NOT being handed on and the owner still needs SSH, kept this green.
        _line_starts = []
        _acc = 0
        for _ln in reset_sh_content.splitlines():
            _line_starts.append(_acc)
            _acc += len(_ln) + 1

        for lineno, ln in calls:
            offset = _line_starts[lineno - 1]
            assert tail_at < offset < reboot_at, (
                f"line {lineno} calls the gate outside the gift/poweroff arms: {ln!r}. "
                "Ahead of the fail-closed gates it strips the CURRENT owner's access on "
                "the exact path where they still need it; on the reboot or plain arm it "
                "strips it from a device that is not being handed on at all."
            )

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


REARM_FN = "rearm_shutdown_splash"
REARM_CALL_RE = re.compile(rf'^[ \t]*{REARM_FN}[ \t]+"', re.M)  # a CALL line, never the `name() {{` definition


def _rearm_helper(body: str) -> str:
    """Lift the litclock-dev#833 helper WITH its timeout constant, verifying
    the load-bearing parts the way _rotation_fn does (the litclock-dev#662 rule)."""
    const = next((ln for ln in body.splitlines() if ln.startswith("SHUTDOWN_REARM_TIMEOUT_S=")), None)
    assert const, "SHUTDOWN_REARM_TIMEOUT_S must be defined with the other top-of-file constants"
    start = body.find(f"{REARM_FN}() {{")
    assert start != -1, f"{REARM_FN}() is missing"
    end = body.index("\n}\n", start) + 3
    fn = body[start:end]
    assert "systemctl start litclock-shutdown.service" in fn, "helper lost the start"
    assert "WARNING: could not re-arm the shutdown splash" in fn, "helper lost the warning"
    return const + "\n" + fn


def _rearm_calls(code: str) -> list[int]:
    """Offsets of every CALL of the helper in (comment-stripped) code."""
    return [m.start() for m in REARM_CALL_RE.finditer(code)]


def _lift_services_stop_span(body: str) -> str:
    """Lift Step 1's services-stop block — the litclock-dev#727 re-arm + stop
    edge, and since litclock-dev#833 the plain-arm re-arm right after it —
    and nothing past it. Callers must prepend _rearm_helper() to execute it."""
    anchor = 'echo -n "Stopping litclock services... "'
    assert body.count(anchor) == 1, "stop-block anchor must be unique"
    start = body.index(anchor)
    end_anchor = "systemctl stop litclock-handoff-fallback.service"
    # End at the anchor line's own newline (/review litclock-dev#731: a longer
    # overshoot leaned on a comment's length and could swallow a real,
    # unstubbed pkill into the executed span).
    end = body.index(end_anchor, start) + len(end_anchor)
    span = body[start : body.index("\n", end) + 1]
    assert len(_rearm_calls(_noncomment(span))) == 2, "span lost a re-arm call — extraction drifted"
    assert "systemctl stop litclock-shutdown.service" in span, "span lost the stop edge — extraction drifted"
    assert "pkill" not in span, "span overgrew into unstubbed host commands"
    return span


class TestSplashStopEdgeRearm:
    """litclock-dev#727: the shutdown/welcome splash is the ExecStop of a
    RemainAfterExit oneshot — a stop edge that exists once per boot. An
    earlier failed reset spends it, and the same-boot retry (the EXPECTED
    path after a failure) painted nothing: a successfully-reset gift box
    shipped showing "Reset did not finish / Do NOT pass it on". The fix
    re-arms the edge with a start before every stop."""

    def test_start_precedes_stop_in_executable_lines(self, reset_sh_content):
        code = _noncomment(reset_sh_content)
        calls = _rearm_calls(code)
        assert calls, "the re-arm call vanished (litclock-dev#727)"
        start_idx = calls[0]
        stop_idx = code.find("systemctl stop litclock-shutdown.service")
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
        rearm_idx = _rearm_calls(code)[0]
        assert decision_idx != -1
        assert decision_idx < rearm_idx, (
            "splash-suppress decision must precede the re-armed stop edge"
        )

    def test_rearm_start_is_blocking(self, reset_sh_content):
        # /review litclock-dev#731 green-while-broken door: this repo's own rule is
        # "systemctl start from inside a service needs --no-block" — but
        # HERE a queued (unstarted) start job is simply REPLACED by the stop
        # on the next line, so --no-block would silently recreate the
        # no-paint retry while every ordering test stays green. The re-arm
        # must stay blocking. Since litclock-dev#833 the start lives in the
        # shared helper, so the helper body is what to check.
        line = next(
            ln for ln in _noncomment(_rearm_helper(reset_sh_content)).splitlines()
            if "systemctl start litclock-shutdown.service" in ln
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
        rearm = _rearm_calls(code)[0]
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
        helper = _rearm_helper(reset_sh_content)
        line = next(
            (ln for ln in _noncomment(helper).splitlines() if "systemctl start litclock-shutdown.service" in ln),
            "",
        )
        assert line.strip().startswith('timeout "$SHUTDOWN_REARM_TIMEOUT_S"'), (
            "the re-arm start must be wrapped in `timeout` on the named constant — it runs "
            "inside litclock-reset.service on the PWA arm"
        )
        const = helper.splitlines()[0]
        seconds = int(const.split("=", 1)[1])
        assert 1 <= seconds <= 30, f"{const}: must be well inside litclock-reset.service's TimeoutStartSec=60"

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
        span = _lift_services_stop_span(body)
        rec = tmp_path / "rec"
        script = (
            f"REC={shlex.quote(str(rec))}\n"
            'systemctl() { echo "$@" >> "$REC"; }\n'
            f"{_TIMEOUT_STUB}"
            # Not the plain arm: this test is about Step 1's own start→stop.
            "DO_REBOOT=true\nDO_POWEROFF=false\nGIFT_MODE=false\n"
            f"{_rearm_helper(body)}\n" + span
        )
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert "WARNING: could not re-arm" not in result.stdout, result.stdout  # litclock-dev#833
        calls = rec.read_text().splitlines()
        start_pos = calls.index("start litclock-shutdown.service")
        stop_pos = calls.index("stop litclock-shutdown.service")
        assert start_pos < stop_pos, f"re-arm ordering broken at runtime: {calls}"
        failed_pos = calls.index("stop litclock-reset-failed.service")
        assert failed_pos < start_pos, f"failure painter must stop before the re-arm: {calls}"


class TestPlainArmRearmsShutdownSplash:
    """litclock-dev#833: Step 1's re-arm-then-stop (litclock-dev#727, under the litclock-dev#718
    suppress marker) CONSUMES litclock-shutdown.service's once-per-boot stop
    edge. The three terminal arms are fine — the script itself is the next
    thing to happen. The plain arm exits and tells the operator to reboot,
    and that reboot found the unit inactive: no shutdown-splash.sh, and the
    stale quote rode e-ink persistence across the power-off (bench,
    2026-09-13). The plain arm must leave the unit ARMED — and the re-arm
    sits in Step 1, right after the stop, NOT at the end of the script:
    every fail-closed `exit 1` and the Step 7 SIGHUP (WiFi wipe over SSH)
    would otherwise skip a tail re-arm and reproduce the symptom through
    the abort door (/review of the first cut)."""

    UNIT = "litclock-shutdown.service"
    PLAIN_GATE = 'if [[ "$DO_REBOOT" != "true" && "$DO_POWEROFF" != "true" && "$GIFT_MODE" != "true" ]]; then'

    # ── source order ──

    def test_plain_rearm_follows_the_marker_clear_inside_step_1(self, reset_sh_content):
        code = _noncomment(reset_sh_content)
        calls = _rearm_calls(code)
        assert len(calls) == 2, (
            f"expected Step 1's litclock-dev#727 re-arm and the litclock-dev#833 "
            f"plain-arm re-arm, found {len(calls)}")
        stop_idx = code.index(f"systemctl stop {self.UNIT}")
        clear_idx = code.index("rm -f /run/litclock-splash-suppress", stop_idx)
        assert calls[0] < stop_idx < clear_idx < calls[1], (
            "litclock-dev#833: the plain-arm re-arm must come AFTER Step 1's stop edge and "
            "AFTER the suppress-marker clear (or the re-armed edge would be muted)"
        )
        gate_idx = code.rfind(self.PLAIN_GATE, 0, calls[1])
        assert gate_idx != -1 and "\n" not in code[gate_idx + len(self.PLAIN_GATE) : calls[1]].strip("\n"), (
            "the plain-arm re-arm must be gated exactly like the marker touch: not reboot, "
            "not poweroff, not gift"
        )

    def test_plain_rearm_precedes_every_abort_path(self, reset_sh_content):
        """The placement argument: rotation's fail-closed exits and the Step 7
        WiFi wipe (whose SSH drop SIGHUPs the script) all come later, so
        every one of them leaves the unit armed."""
        code = _noncomment(reset_sh_content)
        rearm = _rearm_calls(code)[1]
        rotation_guard = code.index('if [[ "$GIFT_MODE" != "true" && "$WIPE_WIFI" == "true" ]]; then')
        wifi_wipe = code.index("Wiping saved WiFi networks")
        assert rearm < rotation_guard < wifi_wipe, "the re-arm must precede the rotation and the WiFi wipe"
        assert f"systemctl stop {self.UNIT}" not in code[rearm:], "a later stop would consume the edge again"
        terminal = TestHotspotPasswordResetSemantics._terminal_branch(reset_sh_content)
        assert not _rearm_calls(_noncomment(terminal)), "no re-arm belongs in the terminal chain any more"

    def test_helper_shape(self, reset_sh_content):
        """Blocking, bounded on the named constant, stderr NOT discarded (it is
        the only diagnostic the WARNING can carry), and the WARNING tail is
        the caller's argument."""
        body = _noncomment(_rearm_helper(reset_sh_content))
        start = next(ln for ln in body.splitlines() if f"systemctl start {self.UNIT}" in ln)
        assert "--no-block" not in start
        assert start.strip().startswith('timeout "$SHUTDOWN_REARM_TIMEOUT_S"')
        assert "2>/dev/null" not in start, "/review F4: do not hide systemctl's stderr"
        assert re.search(r'WARNING: could not re-arm the shutdown splash; \$1"', body), body
        assert "$SHUTDOWN_REARM_TIMEOUT_S" in start

    # ── executed ──

    def _run(
        self,
        tmp_path,
        *,
        gift="false",
        poweroff="false",
        reboot="false",
        wipe_wifi="true",
        unremovable_password=False,
        fail_second_start=False,
    ):
        """EXECUTE Step 1's real stop block (with the real helper) and then
        the real hoisted rotation + terminal if/elif/else chain in one bash
        program, recording every systemctl and rm call in order."""
        content = RESET_SH.read_text()
        state = tmp_path / "state"
        state.mkdir()
        if unremovable_password:
            (state / "hotspot-password").mkdir()
            (state / "hotspot-password" / "occupant").write_text("blocks rmdir\n", encoding="utf-8")
        else:
            (state / "hotspot-password").write_text("clockwis\n", encoding="utf-8")
        config = tmp_path / "config"
        config.mkdir()
        rec = tmp_path / "rec"
        semantics = TestHotspotPasswordResetSemantics
        # C1: a systemctl that fails ONLY the second `start` of the unit — the
        # plain-arm re-arm — so the WARN path executes for real.
        fail_arm = (
            f'    if [[ "$1 $2" == "start {self.UNIT}" ]]; then STARTS=$((STARTS + 1)); '
            "if [[ $STARTS -eq 2 ]]; then return 1; fi; fi\n"
            if fail_second_start
            else ""
        )
        program = (
            "set -u  # NOT -e: reset-setup.sh deliberately omits it\n"
            f"REC={shlex.quote(str(rec))}\nSTARTS=0\n"
            'systemctl() { echo "$@" >> "$REC"\n' + fail_arm + "    return 0\n}\n"
            f"{_TIMEOUT_STUB}"
            # C3: record rm, and never touch the host's /run; forward everything
            # else so the rotation's real rm still runs against tmp_path.
            'rm() { echo "rm $*" >> "$REC"; case " $* " in *" /run/"*) return 0 ;; esac; command rm "$@"; }\n'
            'poweroff() { echo "STUB_POWEROFF"; }\n'
            f"{_SSH_GATE_STUB}"
            f"GIFT_MODE={gift}\nDO_POWEROFF={poweroff}\nDO_REBOOT={reboot}\nWIPE_WIFI={wipe_wifi}\n"
            "ENV_WIPE_FAILED=false\nSTRICT_ENV_WIPE=false\n"
            f"CONFIG_DIR={config}\nLITCLOCK_STATE_DIR={state}\n"
            f"{semantics._state_dir_line(content)}\n"
            'RED=""\nGREEN=""\nYELLOW=""\nNC=""\n'
            f"{_rearm_helper(content)}\n"
            f"{_lift_services_stop_span(content)}\n"
            f"{semantics._rotation_fn(content)}\n"
            f"{semantics._terminal_branch(content)}"
        )
        result = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=30)
        calls = rec.read_text().splitlines() if rec.exists() else []
        # Step 1 ran for real: the litclock-dev#727 re-arm and its stop edge are both in the log.
        assert f"start {self.UNIT}" in calls and f"stop {self.UNIT}" in calls, calls
        if not fail_second_start:
            # The `timeout` stub gone missing would send the re-arm to the real
            # binary, which fails as non-root and prints exactly this.
            assert "WARNING: could not re-arm" not in result.stdout, result.stdout
        return calls, result

    def _unit_calls(self, calls):
        return [c for c in calls if c.endswith(f" {self.UNIT}")]

    @pytest.mark.parametrize("wipe_wifi", ["true", "false"], ids=["default", "keep-wifi"])
    def test_plain_run_leaves_the_unit_armed(self, tmp_path, wipe_wifi):
        """The default finish AND `--keep-wifi` (which only flips WIPE_WIFI and
        still takes the plain arm) both end with the unit STARTED and nothing
        stopping it afterwards."""
        calls, result = self._run(tmp_path, wipe_wifi=wipe_wifi)
        assert result.returncode == 0, f"{result.stdout}{result.stderr}"
        assert "Reboot to enter setup mode" in result.stdout
        start, stop = f"start {self.UNIT}", f"stop {self.UNIT}"
        assert self._unit_calls(calls) == [start, stop, start], calls
        # C3: the suppress marker is cleared BEFORE the re-arm — the whole
        # correctness argument for the placement. Nothing else may stop the
        # unit after the re-arm, either.
        clear = calls.index("rm -f /run/litclock-splash-suppress")
        rearm = len(calls) - 1 - calls[::-1].index(start)
        assert calls.index(stop) < clear < rearm, calls
        assert stop not in calls[rearm:], calls

    @pytest.mark.parametrize(
        "kwargs",
        [{"reboot": "true"}, {"poweroff": "true"}, {"gift": "true"}],
        ids=["reboot", "poweroff", "gift"],
    )
    def test_terminal_arms_do_not_re_arm(self, tmp_path, kwargs):
        """The re-arm is confined to the plain arm. On the terminal arms Step
        1's stop edge already painted the right screen under the right marker
        (the welcome splash on gift), and a second armed edge at the script's
        own poweroff would paint "Powered Off" OVER it."""
        calls, result = self._run(tmp_path, **kwargs)
        assert result.returncode == 0, f"{result.stdout}{result.stderr}"
        assert self._unit_calls(calls) == [f"start {self.UNIT}", f"stop {self.UNIT}"], calls

    def test_an_aborted_plain_run_still_leaves_the_unit_armed(self, tmp_path):
        """Placement A, executed: the rotation fails closed (`exit 1`) AFTER
        Step 1, so the script never reaches its closing banner — and the unit
        is armed anyway, because the re-arm already happened."""
        calls, result = self._run(tmp_path, unremovable_password=True)
        assert result.returncode != 0, "the rotation must fail closed"
        assert "Reboot to enter setup mode" not in result.stdout, "the abort must not reach the banner"
        start, stop = f"start {self.UNIT}", f"stop {self.UNIT}"
        assert self._unit_calls(calls) == [start, stop, start], (
            f"litclock-dev#833: an abort after Step 1 must find the unit already re-armed: {calls}"
        )

    def test_a_failed_rearm_warns_and_the_run_continues(self, tmp_path):
        """C1: the WARN path, executed. A failed re-arm must not abort the
        reset (fail-open — the reset itself is more important than its
        splash) but must say so with THIS caller's consequence."""
        calls, result = self._run(tmp_path, fail_second_start=True)
        assert result.returncode == 0, f"{result.stdout}{result.stderr}"
        assert self._unit_calls(calls)[-1] == f"start {self.UNIT}", calls
        warn = [ln for ln in result.stdout.splitlines() if "WARNING: could not re-arm" in ln]
        # Exactly one warning, carrying THIS caller's tail. (It lands after
        # Step 1's `echo -n "Stopping litclock services... "` on the same
        # line, in production too — hence endswith, not equality.)
        assert len(warn) == 1, result.stdout
        assert warn[0].endswith(
            "WARNING: could not re-arm the shutdown splash; the reboot this run asks for may not paint one."
        ), result.stdout
        assert "this run may not paint its final screen" not in result.stdout, "Step 1's re-arm must not have warned"
        assert "Reboot to enter setup mode" in result.stdout, "the closing banner must still print"


class TestNonGiftResetClearsWelcomeMode:
    """litclock-dev#833 /review F2: .welcome-mode is written by gift mode and
    consumed only by first-boot.sh, so a gift prep that aborted at the litclock-dev#393
    env-wipe gate leaves it. shutdown-splash.sh ranks it ABOVE reboot
    detection — and the plain arm now leaves a live stop edge for the
    operator's reboot, which would paint "Welcome to LitClock" on a device
    nobody is gifting. Every non-gift run clears it, before Step 1's stop."""

    GUARDED = (
        'if [[ "$GIFT_MODE" != "true" ]]; then\n'
        '    rm -f "$CONFIG_DIR/.gift-language"\n'
        '    rm -f "$CONFIG_DIR/.welcome-mode"\n'
        "fi\n"
    )

    def test_cleared_before_the_stop_edge(self, reset_sh_content):
        code = _noncomment(reset_sh_content)
        idx = code.find(self.GUARDED)
        assert idx != -1, "the non-gift .gift-language/.welcome-mode clear block is missing or reshaped"
        assert idx < code.index("systemctl stop litclock-shutdown.service"), "must precede Step 1's stop edge"
        ladder = (REPO_ROOT / "scripts" / "shutdown-splash.sh").read_text()
        assert "/etc/litclock/.welcome-mode" in _noncomment(ladder), "the ladder no longer reads the marker?"

    @pytest.mark.parametrize("gift", ["false", "true"])
    def test_executed(self, tmp_path, gift):
        config = tmp_path / "config"
        config.mkdir()
        (config / ".welcome-mode").write_text("")
        (config / ".gift-language").write_text("de\n")
        code = _noncomment(RESET_SH.read_text())
        idx = code.index(self.GUARDED)
        program = f"set -u\nGIFT_MODE={gift}\nCONFIG_DIR={config}\n" + code[idx : idx + len(self.GUARDED)]
        r = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        expect_present = gift == "true"
        assert (config / ".welcome-mode").exists() is expect_present
        assert (config / ".gift-language").exists() is expect_present


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
        """The validated gift code must reach LITCLOCK_LANGUAGE in the written body.

        EXECUTED since litclock-dev#840. This was a substring grep for
        `export LITCLOCK_LANGUAGE=$GIFT_LANGUAGE_CODE`, which the refactor
        deletes outright — the variable is now an ARGUMENT to env_sh_defaults.
        A grep for the new shape would be just as weak: it is satisfied whether
        the call is `env_sh_defaults "$GIFT_LANGUAGE_CODE"` or
        `env_sh_defaults '$GIFT_LANGUAGE_CODE'`, and the single-quoted form
        would ship the literal text to every gifted device."""
        assert "export LITCLOCK_LANGUAGE=es\n" in _run_defaults_assignment(reset_sh_content, "es")

    def test_plain_reset_clears_stale_marker(self, reset_sh_content):
        """Trap (c): a NON-gift reset must remove an abandoned gift's
        marker. The cleanup must be gated on NOT gift mode (gift runs
        manage the marker in their own arm)."""
        guarded = (
            'if [[ "$GIFT_MODE" != "true" ]]; then\n'
            '    rm -f "$CONFIG_DIR/.gift-language"\n'
            '    rm -f "$CONFIG_DIR/.welcome-mode"\n'  # litclock-dev#833
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
        """The seeding mechanism is now the ARGUMENT passed to env_sh_defaults
        (litclock-dev#840); before it was the DEFAULTS quote flip
        (single->double). Either way a substring grep is satisfied by the
        broken form too — `env_sh_defaults '$GIFT_LANGUAGE_CODE'` greps the
        same as the working double-quoted call and seeds the literal string
        `$GIFT_LANGUAGE_CODE` onto every gifted device. So EXECUTE the
        assignment and assert the expansion (guard-observation-window class)."""
        for code, expected in (("es", "export LITCLOCK_LANGUAGE=es\n"), ("", "export LITCLOCK_LANGUAGE=\n")):
            body = _run_defaults_assignment(reset_sh_content, code)
            assert expected in body, (code, body)
            assert "$GIFT_LANGUAGE_CODE" not in body

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


class TestTheRuntimeValidationMemoIsCleared:
    """litclock-dev#847 item 1 (litclock-dev#854 review) — the runtime-render validation
    memo records that THIS device's freetype could not reproduce the expected
    measurement (or that no tick ever had the budget to try). It is per-device
    state: a reset must not hand it to the next owner, and a clone master must not
    ship it to every recipient, where it would report a verdict about hardware
    they have never run on.

    Asserted on the EXECUTED lines. The comment beside the removal names the
    file, so a raw substring is satisfied by prose with the `rm` gone — the
    trap this repo has measured on nine separate guards.
    """

    def test_the_memo_is_removed(self, reset_sh_content):
        executed = "\n".join(
            ln for ln in reset_sh_content.splitlines() if not ln.lstrip().startswith("#")
        )
        assert 'rm -f "$STATE_DIR/runtime-render-validation.json"' in executed, (
            "the runtime-render validation memo survives a factory reset; the next owner (or "
            "every clone) inherits a failure verdict about someone else's device"
        )

    def test_it_sits_with_the_other_state_removals(self, reset_sh_content):
        """Next to the reset-failed marker, which is the same class of
        per-device bookkeeping and the same best-effort treatment — so the two
        cannot drift apart into different failure policies."""
        executed = "\n".join(
            ln for ln in reset_sh_content.splitlines() if not ln.lstrip().startswith("#")
        )
        a = executed.index('rm -f "$STATE_DIR/reset-failed"')
        b = executed.index('rm -f "$STATE_DIR/runtime-render-validation.json"')
        assert abs(a - b) < 200, executed[min(a, b): max(a, b) + 80]


class TestHandoffHistoryWipe:
    """litclock-dev#868 — gift mode and the --poweroff factory reset wipe and
    LOCK the previous owner's shell history; the plain reset and --reboot do
    not. EXECUTED through the same lifted terminal branch as the rotation
    tests (real helper from lib/state.sh, real function from this script),
    with the two fixed history paths redirected into tmp_path/home.
    """

    _sem = TestHotspotPasswordResetSemantics

    @staticmethod
    def _hist_paths(tmp_path):
        home = tmp_path / "home"
        return home / "pi.bash_history", home / "root.bash_history"

    def _run(self, content, tmp_path, **kw):
        return self._sem()._run(content, tmp_path, **kw)

    @pytest.mark.parametrize(
        "kw",
        [
            pytest.param(dict(gift_mode="true", wipe_wifi="false"), id="gift"),
            pytest.param(dict(gift_mode="false", do_poweroff="true", wipe_wifi="true"), id="pwa-factory-reset"),
            pytest.param(dict(gift_mode="false", do_poweroff="true", wipe_wifi="false"), id="keep-wifi-poweroff"),
        ],
    )
    def test_handoff_arms_wipe_and_lock_the_history_before_ssh_off(self, reset_sh_content, tmp_path, kw):
        pi, root = self._hist_paths(tmp_path)
        _, result, _ = self._run(reset_sh_content, tmp_path, **kw)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "STUB_POWEROFF" in result.stdout
        for p in (pi, root):
            assert p.is_dir() and not p.is_symlink(), f"{p} is not the write-back lock directory"
            assert not any(p.iterdir()), f"{p} is not empty"
        assert "hunter2" not in "".join(q.read_text() for q in (tmp_path / "home").rglob("*") if q.is_file())
        out = result.stdout
        assert "Clearing shell history before handoff... done" in out
        assert out.index("Clearing shell history") < out.index("STUB_SSH_GATE") < out.index("STUB_POWEROFF"), (
            "the history wipe must run BEFORE SSH-off (a failure has to leave the owner a shell) and before poweroff"
        )

    @pytest.mark.parametrize(
        "kw",
        [
            pytest.param(dict(gift_mode="false", do_reboot="true", wipe_wifi="true"), id="reboot"),
            pytest.param(dict(gift_mode="false", wipe_wifi="true"), id="plain"),
        ],
    )
    def test_non_handoff_arms_keep_the_history(self, reset_sh_content, tmp_path, kw):
        """The litclock-dev#718 distinction: these hand control back to an
        operator at a console, not to a new owner."""
        pi, root = self._hist_paths(tmp_path)
        _, result, _ = self._run(reset_sh_content, tmp_path, **kw)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Clearing shell history" not in result.stdout
        assert pi.is_file() and "hunter2" in pi.read_text(encoding="utf-8")
        assert root.is_file() and "sudo -i" in root.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "kw",
        [
            pytest.param(dict(gift_mode="true", wipe_wifi="false"), id="gift"),
            pytest.param(dict(gift_mode="false", do_poweroff="true", wipe_wifi="true"), id="pwa-factory-reset"),
        ],
    )
    def test_a_surviving_history_is_fatal_and_keeps_the_owner_a_shell(self, reset_sh_content, tmp_path, kw):
        """A NON-EMPTY directory at the path (what an earlier aborted run plus a
        stray file looks like) cannot be removed by rmdir or rm -f, so its
        contents survive. Decision (litclock-dev#868): fatal, like a failed rotation —
        no poweroff, no SSH-off, red banner, exit 1."""
        pi, root = self._hist_paths(tmp_path)
        (tmp_path / "home").mkdir(exist_ok=True)
        pi.mkdir()
        (pi / "stray").write_text("hunter2\n", encoding="utf-8")
        _, result, _ = self._run(reset_sh_content, tmp_path, **kw)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "Shell history SURVIVED" in result.stdout
        assert "do NOT hand this device on" in result.stdout
        # The per-path diagnostic, pinned (litclock-dev#873 testing specialist: deleting
        # both diagnostic blocks left every test green).
        assert "Could not clear the shell history at" in result.stdout and str(pi) in result.stdout
        assert "STUB_POWEROFF" not in result.stdout, "must refuse to power off with the old owner's history on it"
        assert "STUB_SSH_GATE" not in result.stdout, "SSH-off must not run — the owner still needs a shell to fix this"
        assert (pi / "stray").is_file(), "the harness's survivor was not the thing that failed"
        # The other path was still processed: report, not short-circuit — and
        # the banner tells the owner that lock stays until the next boot.
        assert root.is_dir()
        assert "Locks already placed at" in result.stdout and str(root) in result.stdout

    @pytest.mark.parametrize(
        "kw",
        [
            pytest.param(dict(gift_mode="true", wipe_wifi="false"), id="gift"),
            pytest.param(dict(gift_mode="false", do_poweroff="true", wipe_wifi="true"), id="pwa-factory-reset"),
        ],
    )
    def test_a_history_that_cannot_be_locked_is_fatal_and_names_the_path(self, reset_sh_content, tmp_path, kw):
        """The UNLOCKED shape (the litclock-dev#834 write-back case): the path
        is empty but the lock cannot be placed. Staged with a parent that does
        not exist — mirrors the cloning file's test."""
        pi, root = self._hist_paths(tmp_path)
        unlockable = tmp_path / "no-such-dir" / ".bash_history"
        extra = f"_PI_BASH_HISTORY={shlex.quote(str(unlockable))}\n"
        _, result, _ = self._run(reset_sh_content, tmp_path, extra=extra, **kw)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "Could not lock" in result.stdout and str(unlockable) in result.stdout
        assert "STUB_SSH_GATE" not in result.stdout and "STUB_POWEROFF" not in result.stdout
        assert not unlockable.exists()
        assert root.is_dir() and "Locks already placed at" in result.stdout and str(root) in result.stdout
        del pi

    def test_state_sh_gate_requires_the_history_helper(self, reset_sh_content):
        """A new reset-setup.sh beside an old lib/state.sh must refuse before
        Step 1 rather than reach the handoff arm and fail with
        `command not found` — which, with no `set -e`, would print FAILED and
        exit 1 only because the function is written fail-closed."""
        m = re.search(r"^for _fn in (.+); do$", reset_sh_content, re.M)
        assert m, "the lib/state.sh helper gate is missing"
        assert "clear_and_lock_bash_history" in m.group(1).split()

    def test_history_wipe_is_one_hoisted_call_above_the_wifi_wipe(self, reset_sh_content):
        """Order is the contract (litclock-dev#873 review + red team): non-gift rotation
        belts -> history wipe (both handoff arms, fail-closed) -> Step 7 WiFi
        wipe -> "Reset Complete!" banner -> terminal arms (gift gates, SSH-off,
        poweroff). A failure must never print the banner, the lock must exist
        before the step that can SIGHUP the run, and the terminal arms must not
        carry a second call."""
        content = reset_sh_content
        calls = [
            (n, ln)
            for n, ln in enumerate(content.splitlines(), 1)
            if not ln.lstrip().startswith("#")
            and re.search(r'(?<![A-Za-z0-9_"])clear_shell_history_for_handoff(?![A-Za-z0-9_(])', ln)
        ]
        assert len(calls) == 1, f"expected exactly one history-wipe call site, found {calls}"
        rotation_guard = content.index('if [[ "$GIFT_MODE" != "true" && "$WIPE_WIFI" == "true" ]]; then')
        wifi_wipe = content.index("# Step 7:", rotation_guard)
        banner = content.index("Reset Complete!")
        call_at = content.index(
            'if [[ "$ENV_WIPE_FAILED" != "true" && ( "$GIFT_MODE" == "true" || "$DO_POWEROFF" == "true" ) ]]; then'
        )
        assert rotation_guard < call_at < wifi_wipe < banner, (
            "history wipe must sit ABOVE Step 7 (a SIGHUP'd bash saves its history — litclock-dev#873 red team) "
            "and precede the banner"
        )
        terminal = content[content.rfind('if [[ "$GIFT_MODE" == "true" ]]; then') :]
        assert "clear_shell_history_for_handoff" not in [
            ln.strip() for ln in terminal.splitlines() if not ln.lstrip().startswith("#")
        ], "the terminal arms must not carry a second call"
        gift = terminal[: terminal.index('elif [[ "$DO_POWEROFF" == "true" ]]')]
        assert "SURVIVED the reset" in gift and "disable_ssh_for_handoff" in gift

    def test_a_gift_prep_that_will_abort_on_env_wipe_does_not_touch_the_history(self, reset_sh_content, tmp_path):
        """The device stays with its owner on that path (the litclock-dev#393 gate refuses
        to power off), so no lock is placed and their history file is intact."""
        pi, root = self._hist_paths(tmp_path)
        _, result, _ = self._run(
            reset_sh_content, tmp_path, gift_mode="true", wipe_wifi="false", extra="ENV_WIPE_FAILED=true"
        )
        assert result.returncode == 1
        assert "Gift prep FAILED" in result.stdout
        assert "Clearing shell history" not in result.stdout
        assert pi.is_file() and "hunter2" in pi.read_text(encoding="utf-8")
        assert root.is_file()
