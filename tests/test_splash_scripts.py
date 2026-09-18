"""Tests for scripts/boot-splash.sh and scripts/shutdown-splash.sh (issue litclock-dev#160)."""

import subprocess
import tempfile
from pathlib import Path

import pytest

# The suppress guard, spelled once. Every assertion about it compares the
# WHOLE conjunction: asserting the pieces separately let `&&` -> `||` through,
# and that mutant makes the script exit 0 on every shutdown (/review).
SUPPRESS_GUARD = "if [[ -f /run/litclock-splash-suppress ]] && [[ ! -L /run/litclock-splash-suppress ]]; then"

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOT_SH = REPO_ROOT / "scripts" / "boot-splash.sh"


def _en_catalog():
    import json

    return json.loads((REPO_ROOT / "languages" / "en" / "strings.json").read_text(encoding="utf-8"))
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
        timer-fired litclock.service for SPI/GPIO (issue litclock-dev#269). The on-boot
        clock render is triggered via ExecStartPost in litclock-splash.service
        so systemd's job queue serializes it against timer fires."""
        assert "runtheclock" not in boot_content, (
            "Direct runtheclock.sh invocation must move to ExecStartPost in the unit file "
            "to avoid GPIO contention with timer-fired litclock.service runs."
        )

    def test_does_not_block_on_sleep(self, boot_content):
        """The original 15-second sleep delayed the first-quote render past
        the timer's first tick (issue litclock-dev#269). Match any blocking `sleep` so
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
        # Distinct catalog prefixes per arm; the copy itself lives in the
        # en catalog (litclock-dev#532 slice 2) — assert the VALUES there
        # so the site→key→value chain is observed end to end.
        assert 'PREFIX="shutdown.splash.reboot"' in shutdown_content
        assert 'PREFIX="shutdown.splash.poweroff"' in shutdown_content
        catalog = _en_catalog()
        assert catalog["shutdown.splash.reboot.message"] == "Restarting..."
        assert catalog["shutdown.splash.poweroff.title"] == "Powered Off"

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
        """Welcome splash should greet the recipient and hint at setup —
        copy now lives in the catalog (litclock-dev#532 slice 2)."""
        assert 'PREFIX="shutdown.splash.welcome"' in shutdown_content
        catalog = _en_catalog()
        assert catalog["shutdown.splash.welcome.title"] == "Welcome to LitClock"
        assert "LitClock-Setup" in catalog["shutdown.splash.welcome.message"]

    def test_welcome_message_file_is_consumed_if_present(self, shutdown_content):
        """litclock-dev#280: when /etc/litclock/.welcome-message exists, use its content
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
        # The default fallback is the catalog title now: an empty/missing
        # welcome file leaves TITLE_OVERRIDE empty, so the catalog's
        # shutdown.splash.welcome.title ("Welcome to LitClock") paints.
        assert 'TITLE_OVERRIDE="$WELCOME_TITLE"' in shutdown_content
        assert _en_catalog()["shutdown.splash.welcome.title"] == "Welcome to LitClock"

    def test_suppress_marker_exits_without_painting(self, shutdown_content):
        """litclock-dev#529: the Setup-Incomplete poweroff paints its recovery
        copy and needs it to persist through shutdown. The root-only suppress
        marker must short-circuit the script BEFORE any action resolution
        (welcome marker included) so nothing repaints over it.

        Back-ported from #22 (litclock-dev#657)."""
        assert SUPPRESS_GUARD in shutdown_content, (
            "the whole conjunction is asserted, not its pieces: with `&&` mutated to `||` the script "
            "exits 0 on EVERY shutdown (the marker does not exist, so `! -L` is true) and no splash — "
            "gift welcome included — ever paints again (/review)"
        )
        # Compare against the welcome-mode CHECK, not its first mention (the
        # header comment lists it earlier).
        suppress_idx = shutdown_content.find(SUPPRESS_GUARD)
        welcome_idx = shutdown_content.find("if [[ -f /etc/litclock/.welcome-mode ]]")
        assert welcome_idx != -1
        assert suppress_idx < welcome_idx, "suppress check must precede welcome-mode check"
        # The branch must exit, not fall through to a paint. Search the block
        # with COMMENTS STRIPPED and bounded by its own `fi`, not by a fixed
        # character count: a fixed window makes an added comment fail the test
        # while the code is untouched (hit for real by litclock-dev#861's log
        # line), and — the direction that actually matters — a comment
        # mentioning `exit 0` would satisfy it while the statement was gone.
        block = shutdown_content[suppress_idx:]
        block = block[: block.index("\nfi\n")]
        code = "\n".join(ln.split("#", 1)[0] for ln in block.splitlines())
        assert "exit 0" in code

    def test_suppress_marker_is_root_owned_path_not_hint_dir(self, shutdown_content):
        """litclock-dev#529 security: suppression must NOT be plantable by a
        pi-level process (it could hide the gift welcome, which pi can't
        otherwise touch). The marker therefore lives directly in root-owned
        /run, not in pi-owned /run/litclock/ where the action hint lives, and
        symlinks are rejected."""
        assert "/run/litclock/splash-suppress" not in shutdown_content
        # `! -L` must name the SUPPRESS marker. A loose `"! -L" in window` check
        # let the test pass with the symlink guard pointed at
        # /run/litclock/shutdown-action instead — restoring the exact
        # pi-plantable-symlink hole this test is named for (/review). The
        # sibling at the bottom of this class already used the anchored form.
        assert "[[ ! -L /run/litclock-splash-suppress ]]" in shutdown_content

    def test_suppress_marker_actually_short_circuits_when_executed(self, shutdown_content):
        """Executed, not grepped — the string checks above cannot see
        fall-through.

        The guard fragment is lifted and run against a temp path, so the
        assertion is on behaviour: present marker short-circuits, absent marker
        falls through to action resolution, and a symlink is refused."""
        start = shutdown_content.index(SUPPRESS_GUARD)
        end = shutdown_content.index("\nfi\n", start) + len("\nfi\n")
        frag = shutdown_content[start:end]

        def run(setup):
            root = Path(tempfile.mkdtemp())
            marker = root / "suppress"
            setup(marker)
            script = frag.replace("/run/litclock-splash-suppress", str(marker)) + "\necho REACHED_ACTION_RESOLUTION\n"
            return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

        absent = run(lambda m: None)
        assert "REACHED_ACTION_RESOLUTION" in absent.stdout, (
            "with no marker the script must fall through and paint — an always-suppress "
            "regression silently disables the gift welcome and the Powered Off splash"
        )
        present = run(lambda m: m.write_text(""))
        assert present.returncode == 0 and "REACHED_ACTION_RESOLUTION" not in present.stdout, (
            "with the marker present the script must exit 0 without resolving an action"
        )
        linked = run(lambda m: m.symlink_to("/etc/hostname"))
        assert "REACHED_ACTION_RESOLUTION" in linked.stdout, "a symlinked marker must NOT suppress"

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



class TestShutdownActionProbePrivilege:
    """litclock-dev#862 — the reboot/poweroff probe has to be asked as ROOT.

    Executed end to end against a stubbed PATH, not grepped: every assertion
    here is on the catalog prefix the painter is actually invoked with, which
    is the thing the owner sees on the glass. The source-text tests elsewhere
    in this file cannot see fall-through, and fall-through is the whole bug —
    a failed probe and a real poweroff produced the identical `poweroff`.

    Ground truth, measured on the bench 2026-09-18 ~10s into a shutdown:
    `systemctl list-jobs` as `pi` returns `Failed to connect to bus:
    Connection refused` (dbus.service is gone, and PID 1's private socket is
    `srwx------ root root`), while `sudo -n systemctl list-jobs` answers and
    lists `reboot.target start waiting`.
    """

    REBOOT_JOBS = (
        "JOB  UNIT                       TYPE  STATE\n"
        "1337 reboot.target              start waiting\n"
        "1338 systemd-reboot.service     start waiting\n"
        "1476 litclock-shutdown.service  stop  running\n"
        "\n4 jobs listed.\n"
    )
    POWEROFF_JOBS = (
        "JOB  UNIT                       TYPE  STATE\n"
        "1337 poweroff.target            start waiting\n"
        "1338 systemd-poweroff.service   start waiting\n"
        "\n2 jobs listed.\n"
    )

    @staticmethod
    def _harness(tmp_path, *, root_jobs="", unpriv_jobs="", sudo_available=True,
                 hint=None, welcome=False, sudo_sleep=None):
        """Run the real shutdown-splash.sh with stubbed systemctl/sudo/python.

        Returns (painter_argv, stdout). painter_argv is [] if nothing painted.
        """
        import os
        import stat

        install = tmp_path / "litclock"
        (install / "src").mkdir(parents=True)
        (install / "venv" / "bin").mkdir(parents=True)
        (install / "src" / "eink_display.py").write_text("")

        painter_log = tmp_path / "painter.argv"
        binv = tmp_path / "bin"
        binv.mkdir()

        def _exe(path, body):
            path.write_text(body)
            path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        _exe(install / "venv" / "bin" / "python3",
             f'#!/bin/bash\nprintf "%s\\n" "$@" > {painter_log}\n')
        # `sudo -n <cmd>`: drop the -n, re-exec with a marker the systemctl
        # stub reads as "this call reached PID 1's private socket".
        _exe(binv / "sudo",
             '#!/bin/bash\n'
             '[ -n "$FAKE_SUDO_UNAVAILABLE" ] && { echo "sudo: a password is required" >&2; exit 1; }\n'
             '[ -n "$FAKE_SUDO_SLEEP" ] && sleep "$FAKE_SUDO_SLEEP"\n'
             '[ "$1" = "-n" ] && shift\n'
             'FAKE_AS_ROOT=1 exec "$@"\n')
        _exe(binv / "systemctl",
             '#!/bin/bash\n'
             'if [ "$1" = "list-jobs" ]; then\n'
             '  if [ -n "$FAKE_AS_ROOT" ]; then OUT="$FAKE_ROOT_JOBS"; else OUT="$FAKE_UNPRIV_JOBS"; fi\n'
             '  if [ -z "$OUT" ]; then echo "Failed to connect to bus: Connection refused" >&2; exit 1; fi\n'
             '  printf "%s" "$OUT"; exit 0\n'
             'fi\n'
             'exit 1\n')

        # Redirect the three hardcoded state paths into the sandbox.
        src = SHUTDOWN_SH.read_text()
        hint_path = tmp_path / "shutdown-action"
        welcome_path = tmp_path / "welcome-mode"
        src = src.replace("/run/litclock/shutdown-action", str(hint_path))
        src = src.replace("/etc/litclock/.welcome-mode", str(welcome_path))
        src = src.replace("/run/litclock-splash-suppress", str(tmp_path / "suppress"))
        script = tmp_path / "shutdown-splash.sh"
        script.write_text(src)

        if hint is not None:
            hint_path.write_text(hint + "\n")
        if welcome:
            welcome_path.write_text("")

        env = dict(os.environ)
        env["PATH"] = f"{binv}:{env['PATH']}"
        env["LITCLOCK_DIR"] = str(install)
        env["FAKE_ROOT_JOBS"] = root_jobs
        env["FAKE_UNPRIV_JOBS"] = unpriv_jobs
        if not sudo_available:
            env["FAKE_SUDO_UNAVAILABLE"] = "1"
        if sudo_sleep is not None:
            env["FAKE_SUDO_SLEEP"] = str(sudo_sleep)

        import time as _time

        t0 = _time.monotonic()
        r = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env, timeout=120)
        elapsed = _time.monotonic() - t0
        argv = painter_log.read_text().splitlines() if painter_log.exists() else []
        return argv, r.stdout, elapsed

    def _prefix(self, argv):
        assert "--catalog-prefix" in argv, f"painter was never invoked with a prefix: {argv}"
        return argv[argv.index("--catalog-prefix") + 1]

    def test_reboot_detected_when_only_root_can_reach_pid1(self, tmp_path):
        """THE litclock-dev#862 REGRESSION TEST. This is the live shutdown state ~10s in:
        the unprivileged probe is refused, the root one answers. Before the fix
        the script asked only as `pi`, got nothing, and painted the poweroff
        farewell on a device that was rebooting."""
        argv, out, _ = self._harness(tmp_path, root_jobs=self.REBOOT_JOBS, unpriv_jobs="")
        assert self._prefix(argv) == "shutdown.splash.reboot", (
            "a reboot must paint the reboot splash even when only root can reach PID 1"
        )
        assert "source=list-jobs(root)" in out

    def test_poweroff_when_the_root_probe_shows_no_reboot_job(self, tmp_path):
        """The other half: asking as root must not turn every shutdown into a
        reboot. Without this, the fix above passes with a hardcoded answer."""
        argv, out, _ = self._harness(tmp_path, root_jobs=self.POWEROFF_JOBS, unpriv_jobs="")
        assert self._prefix(argv) == "shutdown.splash.poweroff"
        assert "source=list-jobs(root)" in out

    def test_falls_back_to_the_unprivileged_probe_when_sudo_is_unavailable(self, tmp_path):
        """010_pi-nopasswd is kept deliberately, but the tree must not brick the
        splash if it is ever withdrawn — the unprivileged call still answers
        early in a shutdown, which is where this ran before litclock-dev#856."""
        argv, out, _ = self._harness(
            tmp_path, root_jobs="", unpriv_jobs=self.REBOOT_JOBS, sudo_available=False
        )
        assert self._prefix(argv) == "shutdown.splash.reboot"
        assert "source=list-jobs(unprivileged)" in out

    def test_an_unanswerable_probe_is_distinguishable_from_a_real_poweroff(self, tmp_path):
        """litclock-dev#861. Both still PAINT poweroff — "Restarting…" on a
        device that is really powering off is the worse error, and the PWA
        factory reset depends on this arm. But the journal must tell them
        apart, because that silence is precisely why litclock-dev#862 went unnoticed
        from the day litclock-dev#856 merged."""
        broken_argv, broken_out, _ = self._harness(tmp_path / "a", root_jobs="", unpriv_jobs="")
        real_argv, real_out, _ = self._harness(tmp_path / "b", root_jobs=self.POWEROFF_JOBS)

        assert self._prefix(broken_argv) == "shutdown.splash.poweroff"
        assert self._prefix(real_argv) == "shutdown.splash.poweroff"
        assert "source=list-jobs(UNAVAILABLE)" in broken_out
        assert "source=list-jobs(UNAVAILABLE)" not in real_out, (
            "a genuine poweroff must not be reported as an unanswerable probe"
        )

    def test_hint_file_still_beats_the_probe(self, tmp_path):
        """litclock-dev#282's explicit hint is authoritative — reset-setup.sh
        writes it precisely because the probe is unreliable at its call site.
        The root probe says reboot; the hint must still win."""
        argv, out, _ = self._harness(tmp_path, root_jobs=self.REBOOT_JOBS, hint="poweroff")
        assert self._prefix(argv) == "shutdown.splash.poweroff"
        assert "source=hint-file" in out

    def test_falls_back_when_the_root_probe_answers_nothing(self, tmp_path):
        """The fallback's OTHER trigger. `sudo` being unavailable is covered
        above; this is `sudo` working while the root probe still comes back
        empty — what a partially torn-down PID 1 looks like. Without it the
        elif is only half covered."""
        argv, out, _ = self._harness(
            tmp_path, root_jobs="", unpriv_jobs=self.REBOOT_JOBS, sudo_available=True
        )
        assert self._prefix(argv) == "shutdown.splash.reboot"
        assert "source=list-jobs(unprivileged)" in out

    def test_a_hung_probe_cannot_eat_the_stop_budget(self, tmp_path):
        """Both probes are bounded, and the bound is load-bearing.

        This runs inside an ExecStop with TimeoutStopSec=30 that also has to
        fit a ~7s paint. An unbounded `sudo` that hangs would burn the budget,
        get SIGKILLed before painting, and leave the boot splash on the glass
        through the power-off — exactly the failure litclock-dev#856 exists to
        prevent, reintroduced from inside.

        Executed, not grepped: `sudo` sleeps 30s, and the assertions are that
        the script still finishes fast AND still paints, having fallen through
        to the unprivileged probe."""
        argv, out, elapsed = self._harness(
            tmp_path, root_jobs=self.REBOOT_JOBS, unpriv_jobs=self.REBOOT_JOBS, sudo_sleep=30
        )
        assert elapsed < 20, f"the probe is not bounded: the script took {elapsed:.1f}s"
        assert self._prefix(argv) == "shutdown.splash.reboot", (
            "a hung root probe must fall through to the unprivileged one, not skip the paint"
        )
        assert "source=list-jobs(unprivileged)" in out

    def test_welcome_marker_still_outranks_everything(self, tmp_path):
        """Gift mode must survive the new tier — a recipient's first impression
        is the welcome splash, not a farewell."""
        argv, out, _ = self._harness(tmp_path, root_jobs=self.REBOOT_JOBS, welcome=True)
        assert self._prefix(argv) == "shutdown.splash.welcome"
        assert "source=welcome-marker" in out

class TestShutdownActionHint:
    """Issue litclock-dev#282 — explicit /run/litclock/shutdown-action hint takes
    precedence over the racy `systemctl list-jobs` detection so callers
    that stop litclock-shutdown.service mid-script (reset-setup.sh
    --reboot) get the right splash."""

    def test_reads_hint_file(self, shutdown_content):
        assert "/run/litclock/shutdown-action" in shutdown_content

    def test_hint_checked_before_list_jobs(self, shutdown_content):
        """Priority order: welcome-mode → hint file → list-jobs → poweroff.
        Hint file must beat list-jobs (which is the racy signal litclock-dev#282 fixes)."""
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
    """/review of PR litclock-dev#304 — hint resolver runs as User=pi but reads from
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


class TestShutdownSplashArgAssemblyExecuted:
    """EXECUTED coverage of the SPLASH_ARGS assembly (litclock-dev#532
    slice 2): each arm must hand eink_display the right mix of catalog
    prefix and literal overrides — a content grep can't see the
    positional-prepend or the conditional flag appends."""

    @staticmethod
    def _span():
        body = SHUTDOWN_SH.read_text(encoding="utf-8")
        start = body.index('case "$SHUTDOWN_ACTION" in')
        end = body.index("\nfi\n", body.index("SPLASH_ARGS")) + len("\nfi\n")
        return body[start:end]

    def _run(self, tmp_path, action, welcome_message=None):
        import subprocess

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "eink_display.py").write_text("", encoding="utf-8")
        rec = tmp_path / "argv.rec"
        recorder = tmp_path / "python.sh"
        recorder.write_text(
            "#!/bin/bash\n" + f'for a in "$@"; do printf "%s\\n" "$a"; done > "{rec}"\n',
            encoding="utf-8",
        )
        recorder.chmod(0o755)
        etc = tmp_path / "etc"
        etc.mkdir()
        welcome_path = etc / ".welcome-message"
        if welcome_message is not None:
            welcome_path.write_text(welcome_message, encoding="utf-8")
        span = self._span().replace("/etc/litclock/.welcome-message", str(welcome_path))
        script = (
            f'INSTALL_DIR="{tmp_path}"\n'
            f'PYTHON="{recorder}"\n'
            f'SHUTDOWN_ACTION="{action}"\n'
            "RANDOM=0\n"
            f"{span}\n"
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20)
        argv = rec.read_text(encoding="utf-8").splitlines() if rec.exists() else []
        return proc, argv

    def test_reboot_arm_prefix_plus_quote_submessage(self, tmp_path):
        _, argv = self._run(tmp_path, "reboot")
        assert argv[:4] == ["src/eink_display.py", "status", "--catalog-prefix", "shutdown.splash.reboot"]
        assert "--submessage" in argv
        quote = argv[argv.index("--submessage") + 1]
        assert quote.startswith('"'), quote  # a curated literary quote
        assert "--message" not in argv  # Restarting... comes from the catalog

    def test_poweroff_arm_quote_rides_the_message(self, tmp_path):
        _, argv = self._run(tmp_path, "poweroff")
        assert argv[2:4] == ["--catalog-prefix", "shutdown.splash.poweroff"]
        assert "--message" in argv
        assert "--submessage" not in argv  # "LitClock" comes from the catalog

    def test_welcome_arm_default_title_comes_from_catalog(self, tmp_path):
        _, argv = self._run(tmp_path, "welcome")
        # No custom message → no positional title → the catalog's
        # "Welcome to LitClock" resolves inside eink_display.
        assert argv[:4] == ["src/eink_display.py", "status", "--catalog-prefix", "shutdown.splash.welcome"]

    def test_welcome_arm_custom_message_overrides_the_title(self, tmp_path):
        _, argv = self._run(tmp_path, "welcome", welcome_message="Happy Birthday Mom!")
        assert argv[2:4] == ["--catalog-prefix", "shutdown.splash.welcome"]
        # Title rides LAST behind the -- separator (leading-dash defense).
        assert argv[-2:] == ["--", "Happy Birthday Mom!"]

    def test_welcome_arm_leading_dash_title_survives(self, tmp_path):
        """A gifter typing "-Mom-" must not kill the paint: without the --
        separator argparse treats it as an unknown option (probed live)."""
        _, argv = self._run(tmp_path, "welcome", welcome_message="-Mom-")
        assert argv[-2:] == ["--", "-Mom-"]
