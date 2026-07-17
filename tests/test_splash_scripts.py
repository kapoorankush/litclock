"""Tests for scripts/boot-splash.sh and scripts/shutdown-splash.sh (issue #160)."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOT_SH = REPO_ROOT / "scripts" / "boot-splash.sh"
SHUTDOWN_SH = REPO_ROOT / "scripts" / "shutdown-splash.sh"


@pytest.fixture(scope="module")
def boot_content():
    return BOOT_SH.read_text()


@pytest.fixture(scope="module")
def shutdown_content():
    return SHUTDOWN_SH.read_text()


# ── boot-splash.sh ────────────────────────────────────────────────────


class TestBootSplash:
    def test_uses_timeout_wrapper(self, boot_content):
        """The eink_display invocation must be wrapped in `timeout` —
        a hung SPI driver could otherwise block boot indefinitely."""
        assert "timeout 20" in boot_content

    def test_does_not_invoke_runtheclock_directly(self, boot_content):
        """boot-splash must NOT call runtheclock.sh directly — that races the
        timer-fired litclock.service for SPI/GPIO (issue #269). The on-boot
        clock render is triggered via ExecStartPost in litclock-splash.service
        so systemd's job queue serializes it against timer fires."""
        assert "runtheclock" not in boot_content, (
            "Direct runtheclock.sh invocation must move to ExecStartPost in the unit file "
            "to avoid GPIO contention with timer-fired litclock.service runs."
        )

    def test_does_not_block_on_sleep(self, boot_content):
        """The original 15-second sleep delayed the first-quote render past
        the timer's first tick (issue #269). Match any blocking `sleep` so
        a future regression with `sleep 30` or `sleep $DELAY` still fails."""
        import re

        assert not re.search(r"(?m)^\s*sleep\s+\S+", boot_content), (
            "boot-splash.sh must not contain any blocking sleep — the on-boot "
            "clock render is triggered via ExecStartPost without a delay"
        )

    def test_eink_failure_does_not_abort_boot(self, boot_content):
        """`|| true` on the eink invocation prevents a display failure from
        propagating an error code that would mark the boot service as failed."""
        eink_line = next(
            (ln for ln in boot_content.splitlines() if "eink_display.py status" in ln),
            None,
        )
        assert eink_line is not None
        assert "|| true" in eink_line

    def test_uses_venv_python(self, boot_content):
        """Boot scripts must use the venv python3, not system python3 — system
        python lacks Pillow/waveshare drivers."""
        assert '"$INSTALL_DIR/venv/bin/python3"' in boot_content or "venv/bin/python3" in boot_content


# ── shutdown-splash.sh ────────────────────────────────────────────────


class TestShutdownSplash:
    def test_detects_reboot_vs_shutdown(self, shutdown_content):
        """Different message bank for reboot vs shutdown — reboot is transient,
        shutdown persists on the e-ink screen indefinitely."""
        assert "reboot.target" in shutdown_content
        assert "list-jobs" in shutdown_content
        # Distinct titles
        assert "Restarting" in shutdown_content
        assert "Powered Off" in shutdown_content

    def test_random_quote_selection(self, shutdown_content):
        """Quote arrays should be indexed by $RANDOM so each shutdown shows
        a different one."""
        assert "RANDOM" in shutdown_content

    def test_three_quote_banks(self, shutdown_content):
        """Reboot, shutdown, and gift-mode each have their own QUOTES=() array."""
        assert shutdown_content.count("QUOTES=(") == 3

    def test_gift_mode_marker_check(self, shutdown_content):
        """Gift-mode marker check must come before reboot/shutdown detection
        so the welcome splash takes precedence."""
        assert "/etc/litclock/.welcome-mode" in shutdown_content
        marker_idx = shutdown_content.find("/etc/litclock/.welcome-mode")
        reboot_idx = shutdown_content.find("reboot.target")
        assert marker_idx < reboot_idx, "marker check must precede reboot detection"

    def test_gift_mode_welcome_content(self, shutdown_content):
        """Welcome splash should greet the recipient and hint at setup."""
        assert "Welcome to LitClock" in shutdown_content
        assert "LitClock-Setup" in shutdown_content

    def test_welcome_message_file_is_consumed_if_present(self, shutdown_content):
        """#280: when /etc/litclock/.welcome-message exists, use its content
        as the TITLE so the gifter's personalized welcome lands on the e-ink.
        Falls back to 'Welcome to LitClock' when missing/empty."""
        assert "/etc/litclock/.welcome-message" in shutdown_content
        # Source must reject symlinks (avoid attacker-pointed targets).
        msg_idx = shutdown_content.find("/etc/litclock/.welcome-message")
        block = shutdown_content[msg_idx : msg_idx + 800]
        assert "! -L" in block, "welcome-message read must reject symlinks"
        # Must be bounded so a giant file can't block shutdown.
        assert "head -c" in block, "welcome-message read must be size-bounded"
        assert "timeout " in block, "welcome-message read must be time-bounded"
        # The default fallback must still be present — empty/missing file
        # falls through to the default greeting.
        assert ":-Welcome to LitClock}" in shutdown_content

    def test_suppress_marker_exits_without_painting(self, shutdown_content):
        """#529: the Setup-Incomplete poweroff paints its recovery copy and
        needs it to persist through shutdown. The root-only suppress marker
        must short-circuit the script BEFORE any action resolution (welcome
        marker included) so nothing repaints over it."""
        assert "/run/litclock-splash-suppress" in shutdown_content
        # Compare against the welcome-mode CHECK, not its first mention (the
        # header comment lists it earlier).
        suppress_idx = shutdown_content.find("if [[ -f /run/litclock-splash-suppress ]]")
        welcome_idx = shutdown_content.find("if [[ -f /etc/litclock/.welcome-mode ]]")
        assert suppress_idx != -1 and welcome_idx != -1
        assert suppress_idx < welcome_idx, "suppress check must precede welcome-mode check"
        # The branch must exit, not fall through to a paint.
        block = shutdown_content[suppress_idx : suppress_idx + 200]
        assert "exit 0" in block

    def test_suppress_marker_is_root_owned_path_not_hint_dir(self, shutdown_content):
        """#529 security: suppression must NOT be plantable by a pi-level
        process (it could hide the gift welcome, which pi can't otherwise
        touch). The marker therefore lives directly in root-owned /run,
        not in pi-owned /run/litclock/ where the action hint lives, and
        symlinks are rejected."""
        assert "/run/litclock/splash-suppress" not in shutdown_content
        line = shutdown_content[shutdown_content.find("if [[ -f /run/litclock-splash-suppress ]]") :][:200]
        assert "! -L" in line, "suppress marker check must reject symlinks"

    def test_uses_venv_python(self, shutdown_content):
        assert "venv/bin/python3" in shutdown_content

    def test_eink_failure_does_not_block_shutdown(self, shutdown_content):
        """A failing eink call must not delay shutdown.
        The actual call is split across lines with backslash continuation,
        so we check for `|| true` anywhere in the script."""
        assert "eink_display.py" in shutdown_content
        assert "|| true" in shutdown_content


class TestShutdownReboootDetection:
    """Verify the reboot-vs-shutdown detection parses real systemctl output."""

    REBOOT_OUTPUT = """\
JOB UNIT                         TYPE  STATE
1   reboot.target                start waiting
2   systemd-reboot.service       start running
2 jobs listed.
"""

    SHUTDOWN_OUTPUT = """\
JOB UNIT                         TYPE  STATE
1   poweroff.target              start waiting
2   systemd-poweroff.service     start running
2 jobs listed.
"""

    def test_detection_matches_reboot(self):
        """`grep -q reboot.target` must find the reboot job."""
        import subprocess

        r = subprocess.run(
            ["grep", "-q", "reboot.target"],
            input=self.REBOOT_OUTPUT,
            text=True,
        )
        assert r.returncode == 0

    def test_detection_skips_shutdown(self):
        import subprocess

        r = subprocess.run(
            ["grep", "-q", "reboot.target"],
            input=self.SHUTDOWN_OUTPUT,
            text=True,
        )
        assert r.returncode != 0


class TestShutdownActionHint:
    """Issue #282 — explicit /run/litclock/shutdown-action hint takes
    precedence over the racy `systemctl list-jobs` detection so callers
    that stop litclock-shutdown.service mid-script (reset-setup.sh
    --reboot) get the right splash."""

    def test_reads_hint_file(self, shutdown_content):
        assert "/run/litclock/shutdown-action" in shutdown_content

    def test_hint_checked_before_list_jobs(self, shutdown_content):
        """Priority order: welcome-mode → hint file → list-jobs → poweroff.
        Hint file must beat list-jobs (which is the racy signal #282 fixes)."""
        hint_idx = shutdown_content.find("/run/litclock/shutdown-action")
        list_jobs_idx = shutdown_content.find("list-jobs")
        assert hint_idx != -1 and list_jobs_idx != -1
        assert hint_idx < list_jobs_idx, "hint file must be checked before list-jobs fallback"

    def test_welcome_mode_still_takes_top_priority(self, shutdown_content):
        """Gift mode's persistent /etc/litclock/.welcome-mode marker must
        beat the transient hint file — a Pi being prepped for shipping
        could in principle have a stale hint file from earlier debugging."""
        welcome_idx = shutdown_content.find("/etc/litclock/.welcome-mode")
        hint_idx = shutdown_content.find("/run/litclock/shutdown-action")
        assert welcome_idx != -1 and hint_idx != -1
        assert welcome_idx < hint_idx

    def test_resolves_action_into_single_variable(self, shutdown_content):
        """Single SHUTDOWN_ACTION variable + case statement is the cleanest
        form and what the test suite assumes for ordering invariants."""
        assert "SHUTDOWN_ACTION" in shutdown_content
        assert "case " in shutdown_content


class TestShutdownActionHintHardening:
    """/review of PR #304 — hint resolver runs as User=pi but reads from
    pi-owned /run/litclock/, so a hostile pi-level process can plant
    symlinks, FIFOs, or arbitrary content. Defenses below."""

    def test_rejects_symlink_hint(self, shutdown_content):
        """If pi pre-plants /run/litclock/shutdown-action as a symlink
        (e.g., to /etc/passwd), reading it would serve the wrong content
        AND leak whatever's in the target. `[[ ! -L ]]` rejects symlinks
        before any read."""
        assert "! -L /run/litclock/shutdown-action" in shutdown_content, (
            "shutdown-action must be checked with `! -L` to reject symlinks"
        )

    def test_bounded_read_with_timeout_and_head(self, shutdown_content):
        """An unbounded `$(< file)` on a pi-controlled path can block
        TimeoutStopSec=30 (FIFO planted by pi) or consume memory (huge
        file). `timeout 1 head -c 32` caps both."""
        assert "timeout 1" in shutdown_content, "hint read must use `timeout 1` to cap FIFOs"
        assert "head -c 32" in shutdown_content, "hint read must use `head -c 32` to cap size"
        # The old unbounded `$(< /run/litclock/shutdown-action)` must be gone.
        assert "$(< /run/litclock/shutdown-action)" not in shutdown_content, (
            "unbounded `$(< file)` read of attacker-controlled path is unsafe"
        )

    def test_strips_whitespace_from_hint(self, shutdown_content):
        """`reboot\\r` (CRLF), `reboot\\n  ` (trailing whitespace from a
        future writer that's slightly different), etc. must all match
        `reboot)`. `tr -d '[:space:]'` normalizes."""
        assert "tr -d '[:space:]'" in shutdown_content, (
            "hint read must strip whitespace to handle CRLF + trailing-space writers"
        )

    def test_allowlists_hint_content(self, shutdown_content):
        """Only `reboot` and `poweroff` are valid hint values. Anything
        else (junk, attacker-spoofed `xxx`) must fall through to the
        list-jobs detector — otherwise pi can suppress a real reboot
        signal by writing garbage to the hint file."""
        # The case-arm allowlist must explicitly enumerate `reboot|poweroff`.
        import re

        # Look for `reboot|poweroff)` (or `poweroff|reboot)`) inside a case statement.
        m = re.search(r"\b(reboot\|poweroff|poweroff\|reboot)\)", shutdown_content)
        assert m is not None, "shutdown-splash must allowlist hint content via `reboot|poweroff)` case arm"

    def test_invalid_hint_falls_through_to_list_jobs(self, shutdown_content):
        """If hint is rejected (symlink, junk content, missing), splash must
        still try list-jobs — otherwise pi can force the splash to skip the
        legitimate reboot.target detection by planting an invalid hint."""
        # Verify the post-hint fallback structure: there must be a check on
        # SHUTDOWN_ACTION emptiness that gates the list-jobs branch.
        assert 'if [[ -z "$SHUTDOWN_ACTION" ]]' in shutdown_content, (
            "fallback check `[[ -z $SHUTDOWN_ACTION ]]` must gate the list-jobs branch"
        )
