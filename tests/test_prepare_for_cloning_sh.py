"""Tests for scripts/prepare-for-cloning.sh (issue #160)."""

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PREPARE_SH = REPO_ROOT / "scripts" / "prepare-for-cloning.sh"


@pytest.fixture(scope="module")
def prepare_sh_content():
    return PREPARE_SH.read_text()


class TestPrepareForCloningStructure:
    def test_requires_root(self, prepare_sh_content):
        assert "$EUID -ne 0" in prepare_sh_content

    def test_uses_set_e(self, prepare_sh_content):
        """Unlike update.sh, this is a fresh-card prep script — bail on any
        failure rather than leaving the card half-wiped."""
        preamble = prepare_sh_content[:500]
        assert "\nset -e\n" in preamble or preamble.startswith("set -e\n")

    def test_removes_setup_complete_flag(self, prepare_sh_content):
        """Without this, cloned cards would think setup is already done."""
        assert 'rm -f "$CONFIG_DIR/.setup-complete"' in prepare_sh_content

    def test_regenerates_env_sh_with_defaults(self, prepare_sh_content):
        """env.sh credentials must be scrubbed before cloning. Cloner should
        overwrite the file with defaults, not delete it. Post-#274 the
        write goes through atomic_write_env_sh (sidecar-flocked)."""
        assert 'atomic_write_env_sh "$INSTALL_DIR/env.sh"' in prepare_sh_content
        assert "OPENWEATHERMAP_APIKEY=" in prepare_sh_content
        # Must not leave the real key
        assert 'rm -f "$INSTALL_DIR/env.sh"' not in prepare_sh_content

    def test_reenables_firstboot_service(self, prepare_sh_content):
        """So the cloned card goes through setup on first boot."""
        assert "systemctl enable litclock-firstboot.service" in prepare_sh_content

    def test_clears_weather_cache(self, prepare_sh_content):
        """Cache from the cloner's location would confuse the recipient."""
        assert 'rm -f "$INSTALL_DIR"/weather-cache*.json' in prepare_sh_content

    def test_clears_bash_history(self, prepare_sh_content):
        """Opsec: strip the cloner's shell history before distribution."""
        assert "rm -f /home/pi/.bash_history" in prepare_sh_content

    def test_clears_ssl_certs(self, prepare_sh_content):
        """SSL cert contains litclock.local — fine to share, but regenerating
        on the recipient's Pi gives them a unique keypair."""
        assert 'rm -rf "$INSTALL_DIR/.certs"' in prepare_sh_content

    def test_wifi_wipe_is_opt_in(self, prepare_sh_content):
        """WiFi wipe is prompted interactively (y/N) — default is keep.
        This matters because many cloners want to keep their test WiFi
        for the recipient to connect over."""
        # The script uses `read -p "Clear saved WiFi networks? (y/N)"`.
        assert "Clear saved WiFi networks?" in prepare_sh_content
        assert "(y/N)" in prepare_sh_content


def test_defaults_include_weather_location_mode_and_ip_country():
    """#337 A3 + /review testing-gap: prepare-for-cloning.sh must include
    the new MODE + IP_COUNTRY defaults. Without these, a cloned image's
    first boot would inherit cloner's MODE=specific (if set) with stale
    coords for a location 1000 miles away from the cloned device's WiFi."""
    from pathlib import Path

    content = (Path(__file__).parent.parent / "scripts/prepare-for-cloning.sh").read_text()
    assert "export WEATHER_LOCATION_MODE=auto" in content, (
        "#337 A3: prepare-for-cloning.sh DEFAULTS must include MODE=auto"
    )
    assert "export WEATHER_IP_COUNTRY=" in content, (
        "#337 A3: prepare-for-cloning.sh DEFAULTS must include WEATHER_IP_COUNTRY= (empty)"
    )


# ─── The hotspot-password step, EXECUTED (litclock-dev#649) ───────────────────────────
#
# The rest of this file greps the script's text, which is the right shape for
# most of it. It is the wrong shape for litclock-dev#649: the defect was that `set -e`
# terminated the script on the `rm` line, so the RED "Do NOT clone this card"
# warning below it was unreachable. Every string those greps look for was
# present the whole time, and the script still exited 1 — so a test asserting
# the exit code or the presence of the warning TEXT passes with the bug live.
#
# The script requires root and performs a dozen destructive system operations,
# so it cannot be run whole in CI. These tests instead lift the real step out
# of the real file (verifying the span they lifted) and execute it against a
# tmp STATE_DIR, so the warning is asserted where it actually has to appear:
# on stdout, in the failure case.

_STEP_START = 'echo -n "Clearing setup-hotspot password... "'
_STEP_END = 'echo -e "${GREEN}done${NC}"'


def _extract_hotspot_password_step() -> str:
    """The verbatim block from the shipped script, span-verified.

    An index()-anchored splice that silently grabs the wrong region is its own
    failure mode — the assertions below make a moved or renamed step fail loudly
    here instead of quietly testing some other part of the file.
    """
    body = PREPARE_SH.read_text()

    start = body.index(_STEP_START)
    end = body.index(_STEP_END, start) + len(_STEP_END)
    step = body[start:end]

    # These three carry the load. The span cannot run LONG by construction
    # (index() takes the first _STEP_END at or after start), so the real risk
    # is a span cut SHORT by a new `done` echo landing between the anchors --
    # the script already has eleven of them. Each assertion below fails in
    # exactly that case, verified by inserting one.
    assert "rm -f" in step, "extracted span does not contain the removal this step is about"
    assert "Do NOT clone this card" in step, "extracted span does not contain the warning under test"
    assert "exit 1" in step, "extracted span does not contain the abort under test"
    # The start anchor must be unique, or `start` itself could point at the
    # wrong step and every assertion above would still hold at the new site.
    assert body.count(_STEP_START) == 1, "start anchor is no longer unique; the span may be lifted from elsewhere"
    return step


_SHELL_OPTION_RE = re.compile(r"^\s*(?:set\s+-\S+|shopt\s+-[su]\s+\S+|IFS=.*)$", re.M)


def _script_shell_environment() -> str:
    """The script's shell options and colour vars, LIFTED not hand-copied.

    `set -e` is the single load-bearing element of the whole litclock-dev#649
    regression test: drop it from the preamble and the un-fixed step happily
    prints the warning and exits 1, so the test goes green on the bug. Nothing
    coupled the harness to the script, so a future `set -euo pipefail` or a
    `shopt -s failglob` (this step relies on an unmatched glob reaching
    `rm -f` as a literal) would leave the harness silently testing the old
    environment.
    """
    body = PREPARE_SH.read_text()

    options = [m.strip() for m in _SHELL_OPTION_RE.findall(body)]
    assert options, "script no longer sets any shell options; the litclock-dev#649 regression test needs `set -e`"
    assert any(o.startswith("set -e") for o in options), (
        "the litclock-dev#649 warning is only unreachable UNDER `set -e` -- without it this suite proves nothing"
    )

    colours = []
    for name in ("RED", "GREEN", "NC"):
        line = re.search(rf"^{name}='[^']*'", body, re.M)
        assert line, f"colour var {name} is no longer declared where the harness can lift it"
        colours.append(line.group(0))

    return "\n".join(options + colours)


def _run_step(state_dir: Path):
    """Execute the real step in the real script's shell environment."""
    script = f"""{_script_shell_environment()}
STATE_DIR={shlex.quote(str(state_dir))}
{_extract_hotspot_password_step()}
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)


def test_step_reports_success_when_the_password_is_removable(tmp_path):
    """Positive control. Without it, a step that aborted for an unrelated
    reason would satisfy the failure test below for the wrong reason."""
    (tmp_path / "hotspot-password").write_text("s3cret\n")
    (tmp_path / ".hotspot-password.tmp123").write_text("older\n")

    result = _run_step(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "done" in result.stdout
    assert not (tmp_path / "hotspot-password").exists()
    assert not list(tmp_path.glob(".hotspot-password.*"))


def test_step_prints_the_do_not_clone_warning_when_removal_fails(tmp_path):
    """litclock-dev#649. The exit code was always right; the DIAGNOSTIC was lost.

    An operator who sees a truncated line and a stopped script has no way to
    know the one thing that matters — that this card must not be cloned,
    because every copy would carry a key the preparer knows.

    Forced failure via a non-empty DIRECTORY at the password path: `rm -f`
    refuses it ("Is a directory") for root and non-root alike, so the fixture
    holds regardless of who runs the suite. `chattr +i` reproduces it on
    hardware; the realistic cause is an SD card remounting read-only.
    """
    victim = tmp_path / "hotspot-password"
    victim.mkdir()
    (victim / "occupied").write_text("x")

    result = _run_step(tmp_path)

    assert result.returncode == 1, f"step must still abort: {result.stdout}{result.stderr}"
    assert "FAILED" in result.stdout
    assert "Do NOT clone this card" in result.stdout, (
        "the warning that exists to stop a key-reuse mistake must actually reach the operator; "
        "under `set -e` an unguarded `rm` kills the script before this line can print"
    )
    assert "every copy would share a key you know" in result.stdout
    # The step's OWN success signal. The full-script banner lives outside the
    # lifted span, so asserting on it would be satisfied by every outcome.
    assert "done" not in result.stdout


def test_step_also_catches_an_orphaned_staging_file(tmp_path):
    """The glob half of the gate. A power cut between mkstemp and os.replace
    leaves `.hotspot-password.*` holding a real past password, and a card
    cloned with one of those still ships a working key."""
    orphan = tmp_path / ".hotspot-password.tmpXYZ"
    orphan.mkdir()
    (orphan / "occupied").write_text("x")

    result = _run_step(tmp_path)

    assert result.returncode == 1
    assert "Do NOT clone this card" in result.stdout


def _extract_survivor_condition() -> str:
    """The `if` condition from the step, verbatim and span-verified."""
    body = PREPARE_SH.read_text()
    start = body.index('if [[ -e "$STATE_DIR/hotspot-password"')
    end = body.index("; then", start)
    cond = body[start + len("if ") : end]

    assert "-e" in cond and "-L" in cond and "compgen -G" in cond, f"extracted span is not the survivor check: {cond!r}"
    assert "echo" not in cond, "span ran past the condition into the failure branch"
    assert body.count("; then", start, end + len("; then")) == 1, "span crossed a nested `then`"
    return cond


@pytest.mark.parametrize(
    ("name", "make", "expect_present"),
    [
        ("nothing at all", lambda d: None, False),
        ("a real password file", lambda d: (d / "hotspot-password").write_text("s3cret\n"), True),
        ("an orphaned staging file", lambda d: (d / ".hotspot-password.tmp1").write_text("old\n"), True),
        # `-e` follows symlinks and is FALSE for a dangling one, so without the
        # `-L` this case reports "removed" for an entry that is still there.
        ("a dangling symlink", lambda d: (d / "hotspot-password").symlink_to(d / "gone"), True),
        ("a dangling staging symlink", lambda d: (d / ".hotspot-password.t2").symlink_to(d / "gone"), True),
    ],
)
def test_survivor_check_sees_every_kind_of_surviving_entry(tmp_path, name, make, expect_present):
    """The step calls this condition "the real gate", so it has to be one.

    Tested directly rather than through a forced `rm` failure: forcing one
    needs either a read-only dir (no effect as root) or chattr (filesystem
    dependent), and a euid-gated skip would make a green suite locally mean
    nothing on the machine that matters.
    """
    make(tmp_path)
    script = f"""STATE_DIR={shlex.quote(str(tmp_path))}
if {_extract_survivor_condition()}; then echo PRESENT; else echo ABSENT; fi
"""
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stderr
    verdict = result.stdout.strip()
    assert verdict == ("PRESENT" if expect_present else "ABSENT"), (
        f"survivor check said {verdict} for {name}; a surviving entry means the removal did not "
        f"do what it claimed, whatever it points at"
    )


def test_state_dir_is_the_shared_override_with_the_documented_default():
    """The executed-step tests inject their own STATE_DIR, so they would not
    notice the real assignment being broken or removed. Pin it separately —
    same gap the reset-setup tests close by sourcing the script's own line.

    The default must stay in lockstep with reset-setup.sh and
    src/wifi_provision.py, which is what makes one override reach all three.
    """
    import wifi_provision

    body = PREPARE_SH.read_text()
    assert 'STATE_DIR="${LITCLOCK_STATE_DIR:-/var/lib/litclock}"' in body, (
        "prepare-for-cloning.sh must keep the shared LITCLOCK_STATE_DIR override with the "
        "/var/lib/litclock default; the hotspot-password step clears nothing if this drifts"
    )
    assert str(wifi_provision.STATE_DIR) == "/var/lib/litclock"


def test_step_succeeds_on_a_card_with_no_password_at_all(tmp_path):
    """The ordinary clean-card path, and the only case that actually executes
    the `rm` with an UNMATCHED glob.

    Nothing in this script or scripts/lib/state.sh sets `nullglob`, so
    `"$STATE_DIR"/.hotspot-password.*` reaches `rm -f` as a literal and `-f`
    swallows it. Correct today, and exactly what a future `shopt -s failglob`
    would break. It is also the state of a card on a re-run of the script.
    """
    result = _run_step(tmp_path)

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    assert "done" in result.stdout
    assert "Do NOT clone this card" not in result.stdout


def test_step_succeeds_when_the_state_dir_does_not_exist(tmp_path):
    """A device that never entered setup has no state dir. Same unmatched-glob
    path, plus a nonexistent parent."""
    result = _run_step(tmp_path / "never-created")

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    assert "done" in result.stdout


def test_failure_tells_the_operator_what_actually_went_wrong(tmp_path):
    """ "Could not remove" does not tell them which remedy applies.

    A read-only remount means the card is dying; an ownership drift means it
    is fixable in place. litclock-dev#649's whole thesis is that the operator was told
    nothing at the moment it mattered, so rm's own diagnosis is surfaced
    instead of discarded to /dev/null.
    """
    victim = tmp_path / "hotspot-password"
    victim.mkdir()
    (victim / "occupied").write_text("x")

    result = _run_step(tmp_path)

    assert result.returncode == 1
    assert "Is a directory" in result.stdout, f"rm's cause must reach the operator, not /dev/null: {result.stdout!r}"


class TestNoBootBeforeImaging:
    """litclock-dev#660 — the prepared master must not be bootable between
    Step 8 (which deletes the persisted setup-WiFi key) and imaging.

    Step 8 is only meaningful if nothing re-creates the key afterwards. But this
    same script re-enables litclock-firstboot.service and Step 1 removes
    .setup-complete -- both correct, because a CLONED card must run first-boot --
    so a single boot of the MASTER runs create_hotspot() ->
    _load_or_create_hotspot_password(), which mints and fsyncs a fresh permanent
    key straight back. Every clone would then carry it, which is precisely what
    Step 8 exists to prevent, and silently: the success banner has already printed.
    """

    @staticmethod
    def _tail(content):
        """Everything after the success banner — where the terminal action lives."""
        idx = content.rfind("SD Card Ready for Cloning!")
        assert idx != -1, "success banner missing"
        return content[idx:]

    def test_powers_off_by_default(self, prepare_sh_content):
        tail = self._tail(prepare_sh_content)
        assert re.search(r"(?m)^\s*poweroff( \|\||$)", tail), (
            "litclock-dev#660: clone prep must power the Pi off so the card cannot "
            "boot before imaging (gift mode powers off for the same reason)"
        )

    def test_poweroff_is_the_last_action_and_follows_the_key_removal(self, prepare_sh_content):
        """Ordering is the whole point: powering off BEFORE the key is deleted
        would leave the key on the card."""
        rm_idx = prepare_sh_content.index('rm -f "$STATE_DIR/hotspot-password"')
        # Anchor on the CALL. A bare rindex("poweroff") also matches the
        # "--no-poweroff was used" echo further down, so it stayed true even
        # with the real call deleted entirely.
        calls = [m.start() for m in re.finditer(r"(?m)^\s*poweroff( \|\||$)", prepare_sh_content)]
        assert len(calls) == 1, f"expected exactly one poweroff call, found {len(calls)}"
        assert rm_idx < calls[0], "the key removal must happen before the poweroff"

    def test_poweroff_is_gated_so_it_can_be_opted_out(self, prepare_sh_content):
        """CI and bench iteration need a way out, but it must be explicit.

        Checks the guard IMMEDIATELY enclosing the poweroff, not merely that a
        POWEROFF_WHEN_DONE conditional appears somewhere earlier -- the tail also
        contains one around the "Next steps" copy, and an earlier-anywhere check
        stayed green when the real guard was deleted.
        """
        # Assert the case ARM, not the string — the comment at the top of the
        # script and the usage echo both contain "--no-poweroff", so a bare
        # substring check stayed green with the whole parser deleted.
        assert re.search(r"--no-poweroff\)\s*POWEROFF_WHEN_DONE=false", prepare_sh_content), (
            "the --no-poweroff case arm must set POWEROFF_WHEN_DONE=false"
        )
        assert "POWEROFF_WHEN_DONE=true" in prepare_sh_content, "power-off must be the DEFAULT"
        lines = self._tail(prepare_sh_content).splitlines()
        po = next(i for i, ln in enumerate(lines) if re.match(r"^\s*poweroff( \|\||$)", ln))
        assert lines[po].startswith("    "), "poweroff must be indented inside a conditional, not top-level"
        # Walk back to the nearest enclosing `if` at column 0.
        opener = next(
            (lines[i] for i in range(po - 1, -1, -1) if lines[i].startswith("if ") or lines[i] == "fi"),
            None,
        )
        assert opener is not None and 'if [[ "$POWEROFF_WHEN_DONE" == "true" ]]' in opener, (
            f"poweroff's enclosing conditional must be the POWEROFF_WHEN_DONE guard, got: {opener!r}"
        )

    def test_no_poweroff_path_warns_about_the_reboot_hazard(self, prepare_sh_content):
        """With --no-poweroff the hazard is live again, so the operator has to be
        told in the same breath -- a silent opt-out would be worse than no flag.

        Scoped to the opt-out branch specifically. The default branch carries a
        near-identical warning, so a tail-wide search stayed green even with the
        opt-out branch's warning gutted.
        """
        tail = self._tail(prepare_sh_content)
        start = tail.index("else")
        optout = tail[start : tail.index("fi", start)]
        assert "no-poweroff was used" in optout, "the opt-out branch must say the Pi is still running"
        assert re.search(r"re-creates", optout), (
            "the opt-out branch must name the consequence of booting before imaging"
        )
        assert "litclock-dev#660" in optout, "point the operator at the issue that explains why"

    def test_stale_manual_shutdown_instruction_is_gone_from_the_default_path(self, prepare_sh_content):
        """The old copy told the operator to `sudo shutdown -h now` themselves.
        On the default path the script has already done it, and leaving the
        instruction would imply the Pi is still up."""
        tail = self._tail(prepare_sh_content)
        default_branch = tail[: tail.index("else")] if "else" in tail else tail
        assert "sudo shutdown -h now" not in default_branch, (
            "the default (power-off) path must not tell the operator to shut down by hand"
        )

    @staticmethod
    def _run_flag_parser(argv):
        """Execute the REAL flag-parsing prologue.

        litclock-dev#660 review: every other test in this class is a static
        assertion on the file text, so renaming POWEROFF_WHEN_DONE or setting it
        to `0` instead of `false` in the `--no-poweroff` arm left all of them
        green while --no-poweroff silently powered off a bench or CI Pi.
        """
        import subprocess

        content = PREPARE_SH.read_text()
        start = content.index("POWEROFF_WHEN_DONE=true")
        loop = content.index("while [[ $# -gt 0 ]]")
        m = re.search(r"(?m)^done$", content[loop:])
        assert m, "could not find the end of the argument loop"
        prologue = content[start : loop + m.end()]
        program = f'{prologue}\nprintf "RESULT=%s\\n" "$POWEROFF_WHEN_DONE"\n'
        return subprocess.run(
            ["bash", "-c", program, "bash", *argv],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "LC_ALL": "C"},
        )

    def test_flag_parser_defaults_to_powering_off(self):
        r = self._run_flag_parser([])
        assert r.returncode == 0, r.stderr
        assert "RESULT=true" in r.stdout, f"default must be power-off, got {r.stdout!r}"

    def test_flag_parser_honours_no_poweroff(self):
        r = self._run_flag_parser(["--no-poweroff"])
        assert r.returncode == 0, r.stderr
        assert "RESULT=false" in r.stdout, (
            "--no-poweroff must set POWEROFF_WHEN_DONE to the literal string the "
            f"guard compares against, got {r.stdout!r}"
        )

    def test_flag_parser_rejects_unknown_flags(self):
        r = self._run_flag_parser(["--wat"])
        assert r.returncode != 0, "an unknown flag must not be silently ignored"
        assert "--no-poweroff" in r.stdout, "usage text must document the real flag"
