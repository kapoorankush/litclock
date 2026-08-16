"""Tests for scripts/prepare-for-cloning.sh (issue #160)."""

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
    """"Could not remove" does not tell them which remedy applies.

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
    assert "Is a directory" in result.stdout, (
        f"rm's cause must reach the operator, not /dev/null: {result.stdout!r}"
    )
