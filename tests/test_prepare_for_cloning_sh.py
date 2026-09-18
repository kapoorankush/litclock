"""Tests for scripts/prepare-for-cloning.sh (issue litclock-dev#160)."""

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PREPARE_SH = REPO_ROOT / "scripts" / "prepare-for-cloning.sh"
STATE_SH = REPO_ROOT / "scripts" / "lib" / "state.sh"


@pytest.fixture(scope="module")
def prepare_sh_content():
    return PREPARE_SH.read_text()


class TestPrepareForCloningStructure:
    def test_requires_root(self, prepare_sh_content):
        assert "$EUID -ne 0" in prepare_sh_content

    def test_uses_set_e(self, prepare_sh_content):
        """Unlike update.sh, this is a fresh-card prep script — bail on any
        failure rather than leaving the card half-wiped. Pinned as "the FIRST
        executed statement", not "within the first N bytes": the header grew
        past a byte window in litclock-dev#834 while `set -e` stayed exactly
        where it must be."""
        executed = [ln for ln in prepare_sh_content.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
        assert executed[0] == "set -e", f"the first executed statement is {executed[0]!r}, not `set -e`"

    def test_removes_setup_complete_flag(self, prepare_sh_content):
        """Without this, cloned cards would think setup is already done."""
        assert 'rm -f "$CONFIG_DIR/.setup-complete"' in prepare_sh_content

    def test_regenerates_env_sh_with_defaults(self, prepare_sh_content):
        """env.sh credentials must be scrubbed before cloning. Cloner should
        overwrite the file with defaults, not delete it. Post-litclock-dev#274 the
        write goes through atomic_write_env_sh (sidecar-flocked); since
        litclock-dev#840 the body comes from `env_sh_defaults()` in
        lib/state.sh, so the key is asserted on the EVALUATED body rather than
        grepped out of this script."""
        assert 'atomic_write_env_sh "$INSTALL_DIR/env.sh"' in prepare_sh_content
        assert "# export OPENWEATHERMAP_APIKEY=\n" in _shipped_defaults()
        # Must not leave the real key
        assert 'rm -f "$INSTALL_DIR/env.sh"' not in prepare_sh_content

    def test_reenables_firstboot_service(self, prepare_sh_content):
        """So the cloned card goes through setup on first boot."""
        assert "systemctl enable litclock-firstboot.service" in prepare_sh_content

    def test_clears_weather_cache(self, prepare_sh_content):
        """Cache from the cloner's location would confuse the recipient."""
        assert 'rm -f "$INSTALL_DIR"/weather-cache*.json' in prepare_sh_content

    def test_clears_bash_history(self, prepare_sh_content):
        """Opsec: strip the cloner's shell history before distribution. Since
        litclock-dev#834 the paths are fixed variables (same convention as
        _NM_PROFILE_DIR: not env-overridable, injected by the executed tests
        below) and the step both removes and LOCKS them."""
        assert '_PI_BASH_HISTORY="/home/pi/.bash_history"' in prepare_sh_content
        assert '_ROOT_BASH_HISTORY="/root/.bash_history"' in prepare_sh_content
        assert 'for _h in "$_PI_BASH_HISTORY" "$_ROOT_BASH_HISTORY"; do' in prepare_sh_content

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


class TestTooOldStateSh:
    """PR litclock-dev#858 review item A — the same gate reset-setup.sh needs, here for the
    MESSAGE rather than for correctness.

    `set -e` (line 26) would abort this script on the "command not found"
    anyway, so it is not silently wrong the way reset-setup.sh is. But an
    abrupt bash error mid-Step-2 is the wrong report from a card-prep tool
    whose entire contract is saying DO NOT CLONE loudly, and relying on a
    `set -e` two hundred lines away is luck rather than design.
    """

    def _gate(self, prepare_sh_content):
        # Anchored on `for _fn in` ALONE — see the note on the reset-setup
        # analogue: a mutant that narrows the checked list must fail on
        # BEHAVIOUR, not on the harness failing to find its span.
        start = prepare_sh_content.index("for _fn in ")
        end = prepare_sh_content.index("\ndone\n", start) + len("\ndone\n")
        span = prepare_sh_content[start:end]
        assert "exit 1" in span, "span lost the abort under test"
        return span

    def test_an_old_state_sh_aborts_with_the_do_not_clone_banner(self, prepare_sh_content, tmp_path):
        harness = f"""
set -e
RED=""; GREEN=""; YELLOW=""; NC=""
_THIS_SCRIPT_DIR={shlex.quote(str(tmp_path))}
# An OLD lib/state.sh: the writer exists, the defaults helper does not.
atomic_write_env_sh() {{ printf "%s" "$2" > "$1"; return 0; }}
{self._gate(prepare_sh_content)}
echo REACHED_STEP_1
"""
        r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=10)
        assert r.returncode == 1, f"expected the gate to abort, got rc={r.returncode}\n{r.stdout}{r.stderr}"
        assert "REACHED_STEP_1" not in r.stdout, "the gate must abort BEFORE any step runs"
        assert "env_sh_defaults is not defined" in r.stderr, r.stderr
        assert "Do NOT clone this card" in r.stderr, (
            f"a card-prep abort must carry the do-not-clone banner, not a bare error: {r.stderr!r}"
        )
        assert "nothing has been wiped" in r.stderr, (
            "the operator must be told the card is untouched — otherwise the safe response to this "
            "message is indistinguishable from the response to a mid-run failure"
        )

    def test_a_current_state_sh_passes_the_gate(self, prepare_sh_content, tmp_path):
        """The inverse, so the gate cannot pass by refusing everything."""
        harness = f"""
set -e
RED=""; NC=""
_THIS_SCRIPT_DIR={shlex.quote(str(tmp_path))}
. "{STATE_SH}"
{self._gate(prepare_sh_content)}
echo REACHED_STEP_1
"""
        r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=10)
        assert r.returncode == 0, f"{r.stdout}{r.stderr}"
        assert "REACHED_STEP_1" in r.stdout


def test_the_clone_seed_carries_no_language(prepare_sh_content):
    """litclock-dev#840 — a clone must NOT inherit the cloner's language.

    `env_sh_defaults` takes an optional language argument (reset-setup passes
    the gift language); this caller must pass NONE, so the recipient's first
    boot keeps Accept-Language negotiation alive — the litclock-dev#743 empty-seed
    contract. EXECUTED: a grep for `env_sh_defaults` alone cannot tell a bare
    call from one that forwards a variable.
    """
    assert "export LITCLOCK_LANGUAGE=\n" in _shipped_defaults(), (
        "a prepared card seeds a non-empty LITCLOCK_LANGUAGE — every clone would boot in the "
        "cloner's language instead of negotiating (litclock-dev#840 / litclock-dev#743)"
    )


def test_defaults_include_weather_location_mode_and_ip_country():
    """litclock-dev#337 A3 + /review testing-gap: a prepared card must seed the MODE +
    IP_COUNTRY defaults. Without these, a cloned image's first boot would
    inherit the cloner's MODE=specific (if set) with stale coords for a
    location 1000 miles away from the cloned device's WiFi.

    EXECUTED since litclock-dev#840: the body comes from `env_sh_defaults()`
    in lib/state.sh, so a grep of prepare-for-cloning.sh sees no such literal.
    `_shipped_defaults()` runs the script's own assignment."""
    shipped = _shipped_defaults()
    assert "export WEATHER_LOCATION_MODE=auto\n" in shipped, "litclock-dev#337 A3: the wipe must seed MODE=auto"
    assert "export WEATHER_IP_COUNTRY=\n" in shipped, (
        "litclock-dev#337 A3: the wipe must seed WEATHER_IP_COUNTRY= (empty)")


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


# `set +X` as well as `set -X` (litclock-dev#662). Matching only the minus form
# meant the lifted environment could not reproduce a script that turns an option
# back OFF — the ordinary way to scope errexit — so the harness silently tested
# an environment the script does not have, and the runtime probe below had
# nothing to catch. Mutation-verified: adding `set +e` to the script left the
# whole suite green until this pattern learned to see it.
_SHELL_OPTION_RE = re.compile(r"^\s*(?:set\s+[-+]\S+|shopt\s+-[su]\s+\S+|IFS=.*)$", re.M)


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
    # Only options that are genuinely in effect AT THE STEP. Scanning the whole
    # file hoists a `set +e` from far below — scoped inside an unrelated
    # function, or in a heredoc — above the step, which turns every litclock-dev#649 test
    # red with a message that misdescribes the cause (/review, mutation-proven).
    step_at = body.find(_STEP_START)
    assert step_at != -1, "the step anchor moved; the option scan would cover the wrong region"
    body = body[:step_at]

    options = [m.strip() for m in _SHELL_OPTION_RE.findall(body)]
    assert options, "script no longer sets any shell options; the litclock-dev#649 regression test needs `set -e`"
    # Presence, not effect — _ERREXIT_PROBE is what proves errexit is actually
    # ON when the step runs. This stays as the earlier, clearer failure.
    assert any(o.startswith("set -e") for o in options), (
        "the litclock-dev#649 warning is only unreachable UNDER `set -e` -- without it this suite proves nothing"
    )

    colours = []
    for name in ("RED", "GREEN", "NC"):
        line = re.search(rf"^{name}='[^']*'", body, re.M)
        assert line, f"colour var {name} is no longer declared where the harness can lift it"
        colours.append(line.group(0))

    return "\n".join(options + colours)


# litclock-dev#662: the harness asserted that a `set -e` line EXISTS among the
# lifted options, which is presence, not effect. _SHELL_OPTION_RE collects every
# matching line and concatenates them, so adding a `set +e` anywhere above the
# step — the ordinary way to scope errexit off — emits `set -e` then `set +e`,
# leaves errexit OFF, lets the unfixed step happily print the warning, and the
# assertion still passes because `set +e` is not counted. The guard would be
# disarmed by precisely the drift it exists to detect.
#
# So probe the shell at RUNTIME, immediately before the lifted span. `$-` holds
# the active option letters; exit 99 is chosen to be distinguishable from every
# exit code the step itself produces.
_ERREXIT_PROBE_MARKER = "harness: errexit is NOT active"
_ERREXIT_PROBE = f'[[ $- == *e* ]] || {{ echo "{_ERREXIT_PROBE_MARKER}" >&2; exit 99; }}'


def _build_step_script(state_dir: Path) -> str:
    """The exact program _run_step executes. Factored out so a test can assert
    the probe is really in it, and really ahead of the step."""
    return f"""{_script_shell_environment()}
STATE_DIR={shlex.quote(str(state_dir))}
{_ERREXIT_PROBE}
{_extract_hotspot_password_step()}
"""


def _run_step(state_dir: Path):
    """Execute the real step in the real script's shell environment."""
    script = _build_step_script(state_dir)
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    # Both conditions: 99 alone would misreport a genuine `exit 99` from the
    # lifted step as a harness fault, with a message that actively points the
    # reader at the wrong thing (/review, Codex).
    assert not (result.returncode == 99 and _ERREXIT_PROBE_MARKER in result.stderr), (
        "the lifted shell environment does not actually have errexit on, so every litclock-dev#649 assertion "
        "below proves nothing: " + result.stderr
    )
    return result


def test_the_probe_is_actually_wired_into_the_harness():
    """The probe is worthless if it is not spliced in, and deleting that one
    line brings the litclock-dev#662 defect straight back with every test still green
    (/review — mutation-proven). Pin the wiring, not just the constant.
    """
    script = _build_step_script(Path("/tmp/does-not-matter"))
    assert _ERREXIT_PROBE in script, "the errexit probe is no longer part of the harness script"
    step = _extract_hotspot_password_step()
    assert script.index(_ERREXIT_PROBE) < script.index(step), (
        "the probe runs after the step, so it cannot protect it"
    )


def test_the_harness_probe_catches_a_disarmed_errexit():
    """The probe's own guard. If it stops detecting `set +e`, the disarming it
    exists to catch goes back to being invisible."""
    script = f"set -e\nset +e\n{_ERREXIT_PROBE}\necho reached\n"
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 99, "the probe did not notice errexit was turned back off"
    assert "reached" not in result.stdout

    ok = subprocess.run(
        ["bash", "-c", f"set -e\n{_ERREXIT_PROBE}\necho reached\n"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ok.returncode == 0 and "reached" in ok.stdout, "the probe fires on a correctly-armed shell"


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


def _executed_branch(marker: str, end_marker: str = "# Step 4") -> str:
    """The named branch with COMMENT LINES STRIPPED.

    Asserting against raw script text is satisfied by prose: the comment
    explaining a message quotes the message, so deleting the `echo` leaves the
    assertion green. That is the same defect this suite exists to catch, one
    level up (litclock-dev#662, and again here in litclock-dev#653 /review).
    """
    body = PREPARE_SH.read_text()
    start = body.index(marker)
    span = body[start : body.index(end_marker, start)]
    return "\n".join(ln for ln in span.splitlines() if not ln.lstrip().startswith("#"))


def _extract_profile_glob_assignment() -> str:
    """The `_SETUP_NET_PROFILE_GLOB=` line, lifted verbatim (litclock-dev#653)."""
    body = PREPARE_SH.read_text()
    line = next(ln for ln in body.splitlines() if ln.startswith("_SETUP_NET_PROFILE_GLOB="))
    assert "$_NM_PROFILE_DIR" in line, f"the glob no longer derives from the profile dir: {line!r}"
    return line


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
        # litclock-dev#653: NetworkManager's keyfile writer disambiguates a colliding
        # connection id with a numeric suffix, and writes through a
        # write-temp-then-rename — so a failed teardown delete (the premise of
        # the whole fix) leaves one of these behind, each holding the real PSK.
        (
            "a suffixed NM profile",
            lambda d: (d / "system-connections").mkdir(exist_ok=True)
            or (d / "system-connections" / "litclock-hotspot-1.nmconnection").write_text("psk=x\n"),
            True,
        ),
        (
            "an orphaned NM write-temp",
            lambda d: (d / "system-connections").mkdir(exist_ok=True)
            or (d / "system-connections" / "litclock-hotspot.nmconnection.7QK2Zx").write_text("psk=x\n"),
            True,
        ),
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
    profiles = tmp_path / "system-connections"
    profiles.mkdir(exist_ok=True)
    # The condition also reads the NM profile glob (litclock-dev#653). It must be defined
    # here: an EMPTY glob makes `compgen -G ""` succeed, which would report a
    # survivor for every case including "nothing at all".
    # The glob assignment is LIFTED from the script, not restated here: a
    # harness that supplies its own pattern cannot notice the script's pattern
    # narrowing back to one filename, which is the defect under test.
    script = f"""STATE_DIR={shlex.quote(str(tmp_path))}
_NM_PROFILE_DIR={shlex.quote(str(profiles))}
{_extract_profile_glob_assignment()}
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
    # litclock-dev#662: NOT `str(wifi_provision.STATE_DIR) == "/var/lib/litclock"`.
    # That value is `os.environ.get("LITCLOCK_STATE_DIR", ...)` read at import
    # time, so any runner with that variable exported fails this for a reason
    # that has nothing to do with the script. Assert the DEFAULT the module
    # declares, which is the thing that must match the shell literal above.
    assert wifi_provision.STATE_DIR_DEFAULT == "/var/lib/litclock", (
        "the Python and shell defaults for the state dir have drifted"
    )
    # ...and the constant must still be what STATE_DIR actually resolves from,
    # or it can be correct while the effective default has drifted (/review).
    assert wifi_provision.STATE_DIR == os.environ.get(
        "LITCLOCK_STATE_DIR", wifi_provision.STATE_DIR_DEFAULT
    ), "STATE_DIR is no longer derived from STATE_DIR_DEFAULT"


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
        # Anchored on `\nelse\n`, not `index("else")`: this is a NEGATIVE
        # assertion, so a substring anchor truncated by any word containing
        # "else" would pass vacuously rather than fail.
        default_branch = self._default_arm(prepare_sh_content)
        assert "sudo shutdown -h now" not in default_branch, (
            "the default (power-off) path must not tell the operator to shut down by hand"
        )

    _ECHO_RE = re.compile(r"""^\s*echo\s+(?:-e\s+)?["'](.*)["']\s*(?:\#.*)?$""")

    @classmethod
    def _printed(cls, block):
        """Only the payloads of `echo` lines -- what the operator actually SEES.

        Three mutation probes drove this, each one surviving the previous
        version. Dropping full-line comments was not enough: the rationale
        comment above these echoes says "a frozen clock", so deleting "frozen"
        from the visible line stayed green; moving the word into a TRAILING
        comment on a code line stayed green after that; and parking the whole
        message in a `_DEV659_MSG="..."` variable that is never echoed stayed
        green after that. Keeping only echo payloads closes all three with one
        rule, and matches what these assertions are actually about.
        """
        return "\n".join(m.group(1) for ln in block.splitlines() for m in [cls._ECHO_RE.match(ln)] if m)

    @staticmethod
    def _banner_tail(content):
        return content[content.rfind("SD Card Ready for Cloning!") :]

    @classmethod
    def _default_arm(cls, content):
        """The power-off arm of the closing banner, anchored on `\nelse\n`.

        `tail.index("else")` -- what the sibling negative assertion used to
        use -- is a SUBSTRING search, so any word containing "else" earlier in
        the arm truncates the span. On a NEGATIVE assertion that is silent: the
        test passes because it is looking at almost nothing. Anchoring on the
        line makes the same edit fail loudly instead.
        """
        return cls._banner_tail(content).split("\nelse\n")[0]

    @classmethod
    def _optout_branch(cls, content):
        """What the `--no-poweroff` arm prints.

        The end of the branch is anchored with `(?m)^fi$` rather than the
        sibling test's `content.index("fi", ...)`, which is a substring search
        any word containing "fi" ("confirm", "first") would truncate. That
        sibling is a POSITIVE assertion, so truncation makes it fail loudly and
        it is left as it is.
        """
        tail = cls._banner_tail(content)
        start = tail.index("\nelse\n")
        end = re.search(r"(?m)^fi$", tail[start:])
        assert end, "could not find the end of the poweroff conditional"
        return cls._printed(tail[start : start + end.start()])

    def test_no_poweroff_path_says_the_frozen_panel_is_expected(self, prepare_sh_content):
        """litclock-dev#659 -- the state reads exactly like a brick.

        With --no-poweroff the script leaves the panel frozen on the last quote
        and port 80 refusing connections, having stopped litclock.timer and
        litclock-control.service several steps earlier. SSH is up, the disk is
        rw and nothing failed, so the only thing separating "intended end state"
        from the litclock-dev#531 lgpio wedge is someone having said so. Saying
        it in the closing banner is the only place the operator is guaranteed to
        be looking at the moment the state is created; a checklist entry only
        helps a reader who has the checklist open. Cost 20 minutes on
        2026-08-15 before this line existed.

        Scoped to the opt-out arm rather than the whole tail, because the
        SUCCESSFUL default path halts and leaves no panel to misread. Its
        FAILURE path does leave one, and carries the same sentence -- pinned
        separately by test_poweroff_failure_arm_also_says_it below. An earlier
        draft of this said the default branch never needs it, which was wrong:
        `poweroff` can fail, and that arm exits 1 with the Pi still running.
        """
        optout = self._optout_branch(prepare_sh_content)
        assert re.search(r"(?i)frozen", optout), "the opt-out branch must say the display is frozen"
        assert re.search(r"(?i)port 80", optout), "and that port 80 is refusing connections"
        assert "litclock-dev#659" in optout, "point at the issue that explains why this looks like a fault"
        # WHICH units and markers it names is cross-checked against the script
        # and the unit files in test_every_unit_the_banner_names_is_one_it_can_
        # account_for; asserting the names here as well would only re-report the
        # same regression twice.

    @classmethod
    def _poweroff_failure_block(cls, content):
        """What the `poweroff || { ... }` recovery arm prints."""
        start = content.index("poweroff || {")
        end = content.index("\n    }", start)
        return cls._printed(content[start:end])

    def test_poweroff_failure_arm_also_says_it(self, prepare_sh_content):
        """The one default-path state that CAN look bricked.

        `poweroff` failing is a realistic state on the degrading card Step 8
        exists to catch, and the arm exits 1 with the Pi still running -- which
        means the frozen panel and the refused port are live on the default
        path too. It is less ambiguous than the opt-out arm, because the
        operator is being told in red that the Pi is up, but the symptom
        outlasts the terminal in exactly the same way.
        """
        block = self._poweroff_failure_block(prepare_sh_content)
        assert re.search(r"(?i)frozen", block), "the poweroff-failure arm must say the display is frozen"
        assert re.search(r"(?i)port 80", block), "and that port 80 is refusing connections"
        assert "litclock-dev#659" in block

    def test_the_successful_default_path_stays_quiet_about_the_panel(self, prepare_sh_content):
        """It halts, so describing a frozen panel there would describe a state
        that never exists -- and would train the operator to skim the line."""
        default_arm = self._default_arm(prepare_sh_content)
        assert "litclock-dev#659" not in default_arm, (
            "the successful default arm powers off; it must not claim a frozen panel"
        )
        assert not re.search(r"(?i)frozen", self._printed(default_arm)), (
            "nor say the display is frozen, on a path that halts the Pi"
        )

    _UNIT_RE = re.compile(r"\b(litclock[\w.-]*\.(?:service|timer))\b")
    _CLEARED_RE = re.compile(r'rm -f "\$CONFIG_DIR/(\.[a-z-]+)"')

    def test_every_unit_the_banner_names_is_one_it_can_account_for(self, prepare_sh_content):
        """Cross-checked against the units and the markers, not grepped for.

        The copy invites the operator to reason about specific units, so each
        one it names must be gated on a marker THIS script clears -- otherwise
        the sentence sends someone to a unit that would start fine, which is a
        second dead end rather than an answer.

        Deriving the named set from the banner rather than hardcoding it is the
        point: an earlier version asserted membership for two hardcoded strings,
        so adding `litclock-splash.service` -- which the script never touches --
        to the copy left every test green.
        """
        cleared = set(self._CLEARED_RE.findall(prepare_sh_content))
        assert {".setup-complete", ".handoff-complete"} <= cleared, (
            f"the banner's explanation rests on both markers being cleared; found {sorted(cleared)}"
        )
        gated = {}
        for unit in sorted((REPO_ROOT / "systemd").glob("*.service")):
            m = re.search(r"(?m)^ConditionPathExists=/etc/litclock/(\S+)", unit.read_text())
            if m:
                gated[unit.name] = m.group(1)
        for arm in (self._optout_branch(prepare_sh_content), self._poweroff_failure_block(prepare_sh_content)):
            named = set(self._UNIT_RE.findall(arm))
            assert named, "the banner must name the units it is explaining"
            for unit in sorted(named):
                assert gated.get(unit) in cleared, (
                    f"{unit} is named in the banner but is not gated on a marker this script clears "
                    f"(its gate is {gated.get(unit)!r}); restarting it would work, so the copy is wrong"
                )

    def test_the_banner_names_the_markers_not_only_the_units(self, prepare_sh_content):
        """The durable cause, and the one an operator can actually check.

        Stopping the units is the transient half -- `systemctl start` undoes it.
        The markers are what make a restart a no-op, and an explanation that
        omits them invites exactly the futile restart it was meant to prevent.
        """
        for arm in (self._optout_branch(prepare_sh_content), self._poweroff_failure_block(prepare_sh_content)):
            assert ".setup-complete" in arm and ".handoff-complete" in arm, (
                "name both markers -- they are why restarting the units changes nothing"
            )

    def test_the_do_not_boot_warning_is_read_before_the_reassurance(self, prepare_sh_content):
        """Ordering, and it is a safety ordering rather than a style one.

        The litclock-dev#660 block is "do NOT let it boot again -- a boot re-creates the
        key". The litclock-dev#659 block ends "there is nothing to debug". An operator
        who skims and takes the reassurance first is measurably more likely to
        reach for a power cycle, which is the exact action litclock-dev#660 says
        destroys the master (the script's own header comment at the top makes
        that coupling explicit). So the warning goes first, and stays first.
        """
        for arm in (self._optout_branch(prepare_sh_content), self._poweroff_failure_block(prepare_sh_content)):
            assert arm.index("litclock-dev#660") < arm.index("litclock-dev#659"), (
                "the do-not-boot warning must be read before the it-is-not-broken reassurance"
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


# --- litclock-dev#673: the fresh-setup paths must clear the same markers -----

RESET_SH = REPO_ROOT / "scripts" / "reset-setup.sh"
SYSTEMD_DIR = REPO_ROOT / "systemd"

# systemd treats all of these as "this path must exist for the unit to run".
# Condition* skips the unit, Assert* fails it; either way a stale marker
# satisfies the gate, which is the litclock-dev#673 defect.
_GATE_KEYS = frozenset(
    {
        "ConditionPathExists",
        "ConditionPathExistsGlob",
        "ConditionFileNotEmpty",
        "AssertPathExists",
        "AssertPathExistsGlob",
        "AssertFileNotEmpty",
    }
)


def _cleared_config_markers(script_text):
    """Marker basenames the script `rm -f`s out of $CONFIG_DIR.

    Comment lines are dropped first. Without that, commenting out the removal
    left every parity assertion green -- mutation-verified during the
    litclock-dev#673 review, and exactly the litclock-dev#662 failure shape.

    Still text-level, so it does not distinguish an unconditional removal from
    one inside an `if` (reset-setup.sh clears .welcome-message conditionally).
    That is fine here: every caller intersects the result with the unit-gate
    set, and no gate marker is removed conditionally in either script. The
    behavioural guard in TestMarkerRemovalActuallyRuns is what proves the
    removal executes; this helper only answers "which markers are named".
    """
    live = "\n".join(ln for ln in script_text.splitlines() if not ln.lstrip().startswith("#"))
    return set(re.findall(r'rm -f "\$CONFIG_DIR/(\.[A-Za-z0-9._-]+)"', live))


def _positive_unit_gate_markers():
    """/etc/litclock markers that a unit REQUIRES in order to start.

    Scope is systemd `Condition*`/`Assert*` gates ONLY. Shell and Python read
    these markers too -- scripts/nm-dispatcher/99-litclock-ip-change gates its
    re-render on .handoff-complete, and src/control_server/handoff.py treats
    "setup-complete present, handoff-complete absent" as the handoff window --
    and none of that is visible here. What this derivation covers is the class
    that made litclock-dev#673 recipient-visible: a unit that will not start
    while a marker is missing, and therefore WILL start when a stale one rides
    a clone.

    Derived from the shipped unit files rather than restated as a literal, so a
    new gate added to any unit fails this suite until both reset paths clear it.

    Parsing is deliberately permissive about spelling, because the first version
    of this helper accepted only `ConditionPathExists=` at the start of a line
    and five systemd-valid alternatives slipped past it silently (whitespace
    around the `=`, the Assert* family, the *Glob and FileNotEmpty variants, and
    the `|` trigger prefix). A derivation that silently under-returns makes
    every assertion built on it weaker without ever failing.

    Excluded: `!` negation, which gates on the marker's ABSENCE. Comment lines
    (`#` and systemd's `;`) are skipped so the handoff-fallback timer's prose
    reference to ConditionPathExists is not read as a directive. All unit types
    are scanned, not just *.service -- systemd accepts Condition* anywhere.
    """
    markers = set()
    for unit in sorted(SYSTEMD_DIR.iterdir()):
        if not unit.is_file():
            continue
        for raw in unit.read_text().splitlines():
            line = raw.strip()
            if not line or line[0] in "#;" or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() not in _GATE_KEYS:
                continue
            value = value.strip()
            # `|` marks a triggering condition, and may precede the `!`.
            while value[:1] == "|":
                value = value[1:].strip()
            if value.startswith("!"):
                continue
            if value.startswith("/etc/litclock/"):
                markers.add(value[len("/etc/litclock/") :])
    return markers


def _marker_removal_span(content):
    """The Step 1 lines that actually delete the markers, for execution.

    Anchored on the operator-visible echo and the `done` that closes it, so a
    removal moved out of the step (below the poweroff, say) leaves the span and
    the behavioural test goes red.
    """
    start = content.index('echo -n "Removing setup-state markers... "')
    end = content.index('echo -e "${GREEN}done${NC}"', start)
    return content[start:end]


class TestMarkerRemovalActuallyRuns:
    """litclock-dev#673 / litclock-dev#662: the static assertions below could
    not tell a live `rm` from a commented-out one. Mutation-verified: commenting
    the removal, wrapping it in `&& false`, or inserting `exit 0` above it all
    left the text-only suite at 34 passed. This class lifts the real span into
    bash and looks at the filesystem, so all three go red.
    """

    @staticmethod
    def _run_removal(content, tmp_path, present):
        cfg = tmp_path / "etc-litclock"
        cfg.mkdir()
        for name in present:
            (cfg / name).write_text("")
        program = f'set -e\nCONFIG_DIR={shlex.quote(str(cfg))}\nGREEN=""\nRED=""\nNC=""\n' + _marker_removal_span(
            content
        )
        r = subprocess.run(
            ["bash", "-c", program],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "LC_ALL": "C"},
        )
        return r, cfg

    def test_span_is_not_empty(self, prepare_sh_content):
        """Guard the guard: an empty span would make every assertion below pass
        without running anything."""
        span = _marker_removal_span(prepare_sh_content)
        assert "rm -f" in span, f"marker-removal span carries no removal: {span!r}"

    def test_both_gate_markers_are_actually_deleted(self, prepare_sh_content, tmp_path):
        r, cfg = self._run_removal(prepare_sh_content, tmp_path, [".setup-complete", ".handoff-complete"])
        assert r.returncode == 0, f"removal step failed: {r.stderr}"
        assert not (cfg / ".handoff-complete").exists(), (
            "litclock-dev#673: .handoff-complete survived the removal step; "
            "litclock.service is gated on it, so the clone paints quotes during "
            "the recipient's setup"
        )
        assert not (cfg / ".setup-complete").exists(), ".setup-complete survived the removal step"

    def test_removal_is_a_noop_when_the_markers_are_absent(self, prepare_sh_content, tmp_path):
        """`set -e` is active in this script, so the removal must not abort a
        prep run on an already-clean card."""
        r, _ = self._run_removal(prepare_sh_content, tmp_path, [])
        assert r.returncode == 0, f"removal step must tolerate absent markers: {r.stderr}"

    def test_a_marker_that_survives_is_reported_not_silently_skipped(self, prepare_sh_content, tmp_path):
        """litclock-dev#649's lesson, applied to this step. Under `set -e` a failing `rm`
        used to kill the script on that line: `done` never printed, nothing said
        why, and the run died BEFORE env.sh, WiFi, certs and the hotspot
        password were cleared. A read-only $CONFIG_DIR makes unlink fail the way
        a card remounting read-only does.
        """
        cfg = tmp_path / "etc-litclock"
        cfg.mkdir()
        (cfg / ".setup-complete").write_text("")
        (cfg / ".handoff-complete").write_text("")
        cfg.chmod(0o500)  # dir not writable -> unlink denied, files still there
        try:
            program = f'set -e\nCONFIG_DIR={shlex.quote(str(cfg))}\nGREEN=""\nRED=""\nNC=""\n' + _marker_removal_span(
                prepare_sh_content
            )
            r = subprocess.run(
                ["bash", "-c", program],
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, "LC_ALL": "C"},
            )
        finally:
            cfg.chmod(0o700)
        if (cfg / ".handoff-complete").exists():
            assert r.returncode != 0, "a surviving marker must abort the prep run"
            assert "Do NOT clone this card" in r.stdout, (
                "a surviving setup-state marker must be named on stdout -- a clone "
                f"made from this card skips part of setup. Got: {r.stdout!r}"
            )
            assert ".handoff-complete" in r.stdout, (
                "the warning must say WHICH marker survived; with two markers there "
                "is a partial-failure state and the operator cannot infer it"
            )
        else:
            pytest.skip("filesystem allowed the unlink despite a read-only dir (running as root?)")

    def test_removal_does_not_take_unrelated_state_with_it(self, prepare_sh_content, tmp_path):
        """Negative assertion: an over-broad `rm -f "$CONFIG_DIR/".*` would
        satisfy every other test here. .welcome-mode is gift-mode state that
        reset-setup.sh writes and clone prep has no business deleting."""
        r, cfg = self._run_removal(
            prepare_sh_content, tmp_path, [".setup-complete", ".handoff-complete", ".welcome-mode"]
        )
        assert r.returncode == 0, r.stderr
        assert (cfg / ".welcome-mode").exists(), "clone prep deleted .welcome-mode, which is not a setup-state marker"


class TestFreshSetupMarkerParity:
    """litclock-dev#673: prepare-for-cloning.sh cleared .setup-complete but not
    .handoff-complete, so every card cloned from a prepared master shipped with
    litclock.service's ConditionPathExists already satisfied by the previous
    owner's marker and painted quotes during the recipient's setup."""

    def test_gate_derivation_finds_the_real_markers(self):
        """Guard the guard: if the derivation silently returned nothing, every
        parity assertion below would pass vacuously."""
        gates = _positive_unit_gate_markers()
        assert ".handoff-complete" in gates and ".setup-complete" in gates, (
            f"unit-gate derivation looks broken, got {gates!r}"
        )

    def test_both_reset_paths_clear_every_unit_gate_marker(self, prepare_sh_content):
        gates = _positive_unit_gate_markers()
        for name, text in (
            ("prepare-for-cloning.sh", prepare_sh_content),
            ("reset-setup.sh", RESET_SH.read_text()),
        ):
            missing = gates - _cleared_config_markers(text)
            assert not missing, (
                f"{name} returns the device to a fresh-setup state but does not "
                f"clear {sorted(missing)}, which a systemd unit gates on"
            )

    def test_setup_complete_is_cleared_before_handoff_complete(self, prepare_sh_content):
        """The order is load-bearing, not cosmetic. scripts/update.sh hard-exits
        when .setup-complete is missing, so clearing it first shuts the door on a
        concurrent updater re-touching .handoff-complete during the prep window
        -- which is unbounded, since Step 3 waits on an interactive prompt.
        Swapping these two lines reopens that window on the master card itself.
        """
        setup_idx = prepare_sh_content.index('rm -f "$CONFIG_DIR/.setup-complete"')
        handoff_idx = prepare_sh_content.index('rm -f "$CONFIG_DIR/.handoff-complete"')
        assert setup_idx < handoff_idx, (
            ".setup-complete must be cleared BEFORE .handoff-complete: it is what "
            "stops update.sh, which would otherwise re-create .handoff-complete"
        )

    def test_setup_state_writers_are_stopped_before_the_markers_are_cleared(self, prepare_sh_content):
        """litclock-dev#673 /review. Both markers are re-creatable: the PWA
        writes .handoff-complete via src/control_server/handoff.py on the Done
        tap and on any Settings save, and scripts/update.sh re-touches it as the
        EPIC litclock-dev#383 PR2 migration. Clearing them with their writers still up lets
        the defect reinstate itself after this script has printed success.
        reset-setup.sh has always stopped its writers first.
        """
        rm_idx = prepare_sh_content.index('rm -f "$CONFIG_DIR/.handoff-complete"')
        for unit in (
            "litclock-control.service",
            "litclock-update.service",
            "litclock-update.timer",
        ):
            stop = f"systemctl stop {unit}"
            assert stop in prepare_sh_content, f"clone prep never stops {unit}"
            assert prepare_sh_content.index(stop) < rm_idx, (
                f"{unit} must be stopped BEFORE the markers are cleared, or it "
                "can re-create .handoff-complete after this script reports success"
            )


# --- litclock-dev#653 ------------------------------------------------------


def test_the_profile_dir_variable_is_defined_before_its_first_use():
    """This script has `set -e` but NOT `set -u`, so an undefined variable
    expands to the empty string rather than aborting. `_NM_PROFILE_DIR` is used
    in Step 3's `rm -f "$_NM_PROFILE_DIR"/*`, which runs long before Step 8 —
    a definition placed at first-mention would have made that `rm -f /*`, as
    root. Caught before it shipped; the ordering is the safety property, so it
    is pinned rather than trusted.
    """
    body = PREPARE_SH.read_text()
    assert "\nset -u" not in body, (
        "the script gained `set -u`, which changes this hazard — re-read the reasoning below"
    )
    # And it must NOT be environment-overridable: this one feeds an unbounded
    # `rm -f "$DIR"/*` as root, unlike STATE_DIR which only ever has fixed
    # filenames appended (/review, security).
    assert "LITCLOCK_NM_PROFILE_DIR" not in body, (
        "the profile directory became environment-overridable; a wrong value wipes a whole "
        "directory as root"
    )
    definition = body.index('_NM_PROFILE_DIR="/etc/NetworkManager/system-connections"')
    first_use = min(
        i for i in (body.find('"$_NM_PROFILE_DIR"'), body.find("$_NM_PROFILE_DIR/")) if i != -1
    )
    assert definition < first_use, (
        "_NM_PROFILE_DIR is used before it is defined; without `set -u` that expands to empty and "
        "Step 3 becomes `rm -f /*` running as root"
    )


def test_the_hotspot_profile_is_cleared_regardless_of_the_wifi_prompt():
    """litclock-dev#653. `nmcli device wifi hotspot` writes a PERSISTENT profile
    containing the PSK, teardown deletes it best-effort (`check=False`), and the
    WiFi wipe is opt-in and DEFAULTS TO KEEP. So the realistic path ships a card
    with a working key for LitClock-Setup while the step prints `done` — and
    since litclock-dev#620 that key is permanent.

    It is OUR profile, not the operator's network, so the "keep my test WiFi"
    rationale does not cover it: the removal must not sit inside the y/N branch.
    """
    body = PREPARE_SH.read_text()
    prompt_at = body.index("Clear saved WiFi networks?")
    branch_end = body.index("# Step 4", prompt_at)
    optional_branch = body[prompt_at:branch_end]
    assert "litclock-hotspot" not in optional_branch, (
        "the hotspot profile is removed only inside the opt-in WiFi wipe, whose default is KEEP"
    )

    step = _extract_hotspot_password_step()
    assert "litclock-hotspot" in step, "the unconditional step does not clear the hotspot profile"


def test_the_survivor_check_covers_the_profile_too():
    """The check is the gate, so it must gate on every place the key lives. A
    check covering only the state file printed `done` for a card still carrying
    the profile."""
    step = _extract_hotspot_password_step()
    guard = step[step.index("if [["):]
    assert "_SETUP_NET_PROFILE" in guard, "the survivor check does not look at the NM profile"


def test_the_optional_wifi_wipe_verifies_itself():
    """It was `rm ... || true` then an unconditional `done`, so an operator who
    explicitly ASKED to wipe could be told it worked while home WiFi PSKs
    survived — the same "reported success it did not achieve" shape as litclock-dev#649,
    arriving by the opposite route."""
    branch = _executed_branch("Clear saved WiFi networks?")
    assert "find" in branch and "-mindepth 1" in branch, (
        "the WiFi wipe still reports done without checking"
    )
    assert "Do NOT clone this card" in branch, "the failure branch does not warn against cloning"
    assert "exit 1" in branch, "the WiFi wipe failure does not stop the prep"


def test_the_step_fails_closed_on_a_surviving_hotspot_profile(tmp_path):
    """Executed, not grepped: plant a profile the removal cannot take and assert
    the step reports FAILED rather than done."""
    import subprocess

    state = tmp_path / "state"
    state.mkdir()
    profiles = tmp_path / "system-connections"
    profiles.mkdir()
    # A non-empty directory at the profile path: `rm -f` fails on it for root
    # and non-root alike, so this needs no permission trick.
    blocked = profiles / "litclock-hotspot.nmconnection"
    blocked.mkdir()
    (blocked / "occupant").write_text("blocks rm\n", encoding="utf-8")

    script = f"""{_script_shell_environment()}
STATE_DIR={shlex.quote(str(state))}
_NM_PROFILE_DIR={shlex.quote(str(profiles))}
nmcli() {{ return 0; }}
{_ERREXIT_PROBE}
{_extract_hotspot_password_step()}
"""
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)

    assert result.returncode == 1, f"the step did not fail closed: {result.stdout}{result.stderr}"
    assert "FAILED" in result.stdout
    assert "Do NOT clone this card" in result.stdout
    assert blocked.exists(), "the harness did not actually reproduce a surviving profile"


def test_the_step_removes_the_whole_profile_FAMILY(tmp_path):
    """litclock-dev#653 /review: NM disambiguates a colliding connection id with a
    numeric suffix and writes through a temp-then-rename, so a failed teardown
    delete — the premise of this fix — leaves `litclock-hotspot-1.nmconnection`
    or `litclock-hotspot.nmconnection.XXXXXX` behind, each holding the real PSK.
    An exact-filename removal walked past both and printed `done`.
    """
    import subprocess

    state = tmp_path / "state"
    state.mkdir()
    profiles = tmp_path / "system-connections"
    profiles.mkdir()
    family = [
        profiles / "litclock-hotspot.nmconnection",
        profiles / "litclock-hotspot-1.nmconnection",
        profiles / "litclock-hotspot.nmconnection.7QK2Zx",
    ]
    for member in family:
        member.write_text("[wifi-security]\npsk=PERMANENT-KEY\n", encoding="utf-8")
    survivor = profiles / "home-wifi.nmconnection"
    survivor.write_text("[wifi-security]\npsk=the-operators-network\n", encoding="utf-8")

    script = f"""{_script_shell_environment()}
STATE_DIR={shlex.quote(str(state))}
_NM_PROFILE_DIR={shlex.quote(str(profiles))}
nmcli() {{ return 0; }}
{_ERREXIT_PROBE}
{_extract_hotspot_password_step()}
"""
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    for member in family:
        assert not member.exists(), f"{member.name} survived, carrying the permanent key onto every clone"
    assert survivor.exists(), "the step deleted the operator's own saved WiFi"


def test_the_step_actually_removes_a_removable_profile(tmp_path):
    """The positive control the failure test cannot give.

    test_the_step_fails_closed_on_a_surviving_hotspot_profile plants an
    UNREMOVABLE profile, so it reports FAILED whether or not the removal was
    ever attempted — deleting the profile from the `rm` line left it green.
    This plants a removable one and asserts it is gone.
    """
    import subprocess

    state = tmp_path / "state"
    state.mkdir()
    (state / "hotspot-password").write_text("s3cret12\n", encoding="utf-8")
    profiles = tmp_path / "system-connections"
    profiles.mkdir()
    profile = profiles / "litclock-hotspot.nmconnection"
    profile.write_text("[wifi-security]\npsk=s3cret12\n", encoding="utf-8")
    survivor = profiles / "home-wifi.nmconnection"
    survivor.write_text("[wifi-security]\npsk=the-operators-network\n", encoding="utf-8")

    script = f"""{_script_shell_environment()}
STATE_DIR={shlex.quote(str(state))}
_NM_PROFILE_DIR={shlex.quote(str(profiles))}
nmcli() {{ echo "NMCLI $*"; return 0; }}
{_ERREXIT_PROBE}
{_extract_hotspot_password_step()}
"""
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    assert not profile.exists(), "the setup-hotspot profile survived; every clone carries a working key"
    assert not (state / "hotspot-password").exists()
    # ...and it must not take the operator's own networks with it: that is what
    # the opt-in Step 3 wipe is for, and the default there is KEEP.
    assert survivor.exists(), "the unconditional step deleted the operator's saved WiFi"


def test_the_step_also_deletes_the_live_networkmanager_connection(tmp_path):
    """Removing the FILE is not the whole job: NetworkManager holds the profile
    in memory, so a running hotspot can rewrite it. The runtime delete is the
    other half, and removing it left the file-level assertions green."""
    import subprocess

    state = tmp_path / "state"
    state.mkdir()
    profiles = tmp_path / "system-connections"
    profiles.mkdir()
    calls = tmp_path / "nmcli-calls"

    script = f"""{_script_shell_environment()}
STATE_DIR={shlex.quote(str(state))}
_NM_PROFILE_DIR={shlex.quote(str(profiles))}
nmcli() {{ echo "$*" >> {shlex.quote(str(calls))}; return 0; }}
{_ERREXIT_PROBE}
{_extract_hotspot_password_step()}
"""
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"{result.stdout}{result.stderr}"

    assert calls.exists(), "the step never invoked nmcli, so NetworkManager still holds the profile"
    invoked = calls.read_text(encoding="utf-8")
    assert "connection delete litclock-hotspot" in invoked, invoked


def test_the_journal_is_rotated_before_it_is_vacuumed():
    """litclock-dev#654 (half 2), found again by /review of litclock-dev#653.

    `--vacuum-time` operates only on ARCHIVED journal files — man journalctl:
    "it will not remove active journal files". The documented cloning flow is
    provision, verify, then prepare, all in ONE boot, so the setup PSK that
    reached the journal through sudo's command-audit line sits in the ACTIVE
    file, which a bare vacuum cannot see. Rotating first archives everything
    written so far.
    """
    body = PREPARE_SH.read_text()
    executed = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    assert "journalctl --rotate" in executed, (
        "the journal is vacuumed without rotating, so the ACTIVE file — where this boot's setup "
        "key is — survives onto every clone"
    )
    rotate_at = executed.index("journalctl --rotate")
    vacuum_at = executed.index("--vacuum-time")
    assert rotate_at < vacuum_at, "the rotate must precede the vacuum, or it archives nothing in time"
    assert "--vacuum-time=1d" not in executed, (
        "a one-day retention is the wrong instrument for a step whose job is 'this card must carry "
        "nothing about this device'"
    )


def test_the_optional_wipe_drops_networkmanagers_in_memory_copies():
    """Removing files alone is the half-measure Step 8 calls out: NM holds these
    connections in memory — the operator's own network is ACTIVE while Step 3
    runs — and a live daemon can write a profile back after the file is gone.
    reset-setup.sh warns about this rather than fixing it; a step that prints a
    VERIFIED `done` has to actually do it (/review, security).
    """
    branch = _executed_branch("Clear saved WiFi networks?")
    assert "nmcli connection delete" in branch, (
        "Step 3 removes profile files without telling NetworkManager, so the daemon can re-persist them"
    )
    assert branch.index("nmcli connection delete") < branch.index('rm -f "$_NM_PROFILE_DIR"'), (
        "delete through NM before removing the files, or the daemon rewrites what was just removed"
    )


def test_the_optional_wipe_can_see_dotfiles():
    """`compgen -G "$DIR/*"` skips dotfiles, so a `.`-prefixed keyfile or an
    editor swap file holding a PSK passed the gate."""
    branch = _executed_branch("Clear saved WiFi networks?")
    assert "find" in branch and "-mindepth 1" in branch, (
        "the survivor check globs, so it cannot see a dotfile carrying a PSK"
    )


def test_the_wipe_failure_says_the_setup_key_is_still_there():
    """Aborting in Step 3 stops before Step 8, so the setup-hotspot key has NOT
    been cleared and the card is already part-way prepared — markers gone,
    env.sh scrubbed. An operator told only "do not clone" knows neither."""
    branch = _executed_branch("Clear saved WiFi networks?")
    # The unique wipe-FAILURE phrase, not "part-way prepared" — that string now
    # also appears in the network-session refusal inside this same branch, so
    # asserting it here was satisfiable with the failure line deleted
    # (/review litclock-dev#710 round 2; greppable-constants prefix-freeness).
    assert "has NOT been cleared yet, and this card is already" in branch, (
        "the wipe failure no longer says the setup key is still on the card"
    )


# ───────────────────── litclock-dev#701: network-session refusal ────────────
#
# The opt-in WiFi wipe deletes EVERY NM connection, wired included, so a run
# over SSH loses its own link mid-loop and SIGHUP kills the script at Step 3 —
# before Step 8 removes the setup-hotspot key. The fix is a pair: refuse the
# wipe on a network session (fail closed), and an unfinished-run marker so a
# half-finished run is visible to the NEXT run instead of only to an operator
# who noticed a banner never printed.
#
# Same harness rules as the rest of this file: spans are LIFTED verbatim and
# EXECUTED (the branch condition lives inside the lifted span, litclock-dev#662), and
# assertions run against comment-stripped text where grepping is unavoidable
# (a comment can satisfy a raw-text assertion, litclock-dev#653 /review).

_YELLOW_DECL = "YELLOW='\\033[1;33m'"


def _extract_is_network_session() -> str:
    """The `_is_network_session()` function, lifted verbatim."""
    body = PREPARE_SH.read_text()
    start = body.index("_is_network_session() {")
    end = body.index("\n}", start) + len("\n}")
    fn = body[start:end]
    assert "SSH_CONNECTION" in fn and "sshd" in fn, "span is not the detector"
    assert "return 1" in fn, "span lost the local-console fallthrough"
    assert body.count("_is_network_session() {") == 1
    return fn


def _fake_ps(bin_dir: Path, chain: dict) -> None:
    """A `ps` that answers -o comm=/-o ppid= from a fixed table.

    Unknown pids (the live test shell's own $$) map to the chain entry point
    `START`, so the walk enters the synthetic ancestry deterministically.
    """
    lines = ["#!/bin/bash", 'mode=$2; pid=$4', 'case "$mode" in']
    lines.append("comm=)")
    lines.append('  case "$pid" in')
    for pid, (comm, _ppid) in chain.items():
        lines.append(f'    {pid}) echo {comm};;')
    lines.append(f'    *) echo {chain["START"][0]};;')
    lines.append("  esac;;")
    lines.append("ppid=)")
    lines.append('  case "$pid" in')
    # procps pads ppid= output with leading spaces and REJECTS a padded -p
    # argument ("error: improper list"), so the detector's whitespace strip is
    # load-bearing; a fake emitting clean values never exercised it
    # (/review litclock-dev#710 round 2).
    for pid, (_comm, ppid) in chain.items():
        lines.append(f'    {pid}) echo "      {ppid}";;')
    lines.append(f'    *) echo "      {chain["START"][1]}";;')
    lines.append("  esac;;")
    lines.append("esac")
    ps = bin_dir / "ps"
    ps.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ps.chmod(0o755)


_SSH_CHAIN = {"START": ("bash", "400"), "400": ("sudo", "300"), "300": ("sshd-session", "1")}
_CONSOLE_CHAIN = {"START": ("bash", "400"), "400": ("sudo", "300"), "300": ("login", "1")}


def _run_detector(tmp_path, chain, env_extra=""):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _fake_ps(bin_dir, chain)
    script = f"""unset SSH_CONNECTION SSH_TTY
PATH={shlex.quote(str(bin_dir))}:$PATH
{env_extra}
{_extract_is_network_session()}
if _is_network_session; then echo DETECTED-NETWORK; else echo DETECTED-LOCAL; fi
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)


class TestNetworkSessionDetector:
    def test_env_evidence_alone_detects(self, tmp_path):
        """SSH_CONNECTION survives `sudo -E`; it must short-circuit even when
        the ancestry looks local."""
        r = _run_detector(tmp_path, _CONSOLE_CHAIN, env_extra='export SSH_CONNECTION="10.0.0.2 51022 10.0.0.9 22"')
        assert r.returncode == 0, r.stderr
        assert "DETECTED-NETWORK" in r.stdout

    def test_ancestry_walk_detects_sshd_past_sudo_env_reset(self, tmp_path):
        """The realistic path: `sudo ./prepare-for-cloning.sh` over SSH, where
        env_reset stripped SSH_CONNECTION. Only the ancestry walk can see it.
        The chain uses `sshd-session` (OpenSSH >= 9.8) to pin the sshd* glob."""
        r = _run_detector(tmp_path, _SSH_CHAIN)
        assert r.returncode == 0, r.stderr
        assert "DETECTED-NETWORK" in r.stdout

    def test_console_session_is_not_flagged(self, tmp_path):
        """A local console login (getty/login ancestry, no SSH env) must NOT be
        refused — it is exactly the session the wipe cannot drop."""
        r = _run_detector(tmp_path, _CONSOLE_CHAIN)
        assert r.returncode == 0, r.stderr
        assert "DETECTED-LOCAL" in r.stdout


def _extract_wipe_branch() -> str:
    """Step 3's whole if/else, lifted verbatim WITH its condition (litclock-dev#662)."""
    body = PREPARE_SH.read_text()
    start = body.index('if [[ $REPLY =~ ^[Yy]$ ]]; then')
    end = body.index('echo "Keeping WiFi credentials."', start)
    end = body.index("fi", end) + len("fi")
    span = body[start:end]
    assert "REFUSED" in span, "span does not contain the refusal under test"
    assert "nmcli connection delete" in span, "span cut short of the wipe"
    assert body.count('if [[ $REPLY =~ ^[Yy]$ ]]; then') == 1, "condition anchor no longer unique"
    return span


def _run_wipe_branch(tmp_path, chain, reply="y"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _fake_ps(bin_dir, chain)
    profiles = tmp_path / "system-connections"
    profiles.mkdir(exist_ok=True)
    (profiles / "home-wifi.nmconnection").write_text("psk=secret\n", encoding="utf-8")
    # The wipe branch rewrites $_WPA_SUPPLICANT_CONF — parameterized in the
    # script precisely so this EXECUTED span cannot touch the host's real
    # /etc/wpa_supplicant/wpa_supplicant.conf when the suite runs as root
    # (/review litclock-dev#710 round 2).
    wpa_conf = tmp_path / "wpa_supplicant.conf"
    wpa_conf.write_text("network={ psk=old }\n", encoding="utf-8")
    script = f"""{_script_shell_environment()}
{_YELLOW_DECL}
unset SSH_CONNECTION SSH_TTY
PATH={shlex.quote(str(bin_dir))}:$PATH
_NM_PROFILE_DIR={shlex.quote(str(profiles))}
_WPA_SUPPLICANT_CONF={shlex.quote(str(wpa_conf))}
nmcli() {{ return 0; }}
REPLY={reply}
{_extract_is_network_session()}
{_ERREXIT_PROBE}
{_extract_wipe_branch()}
echo REACHED-END
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30), profiles


class TestWipeRefusedOnNetworkSession:
    def test_y_over_ssh_ancestry_is_refused_before_any_deletion(self, tmp_path):
        result, profiles = _run_wipe_branch(tmp_path, _SSH_CHAIN)
        assert result.returncode == 1, f"{result.stdout}{result.stderr}"
        assert "REFUSED" in result.stdout
        assert "Do NOT clone" in result.stdout, "the refusal does not warn against cloning the part-prepared card"
        assert "local console" in result.stdout, "the refusal does not say where to run instead"
        assert (profiles / "home-wifi.nmconnection").exists(), (
            "the refusal must fire BEFORE the deletion loop — a dropped session mid-loop is the bug itself"
        )
        assert "REACHED-END" not in result.stdout

    def test_y_on_console_still_wipes(self, tmp_path):
        """Positive control: the refusal must not break the console path the
        QA checklist row 2 exercises."""
        result, profiles = _run_wipe_branch(tmp_path, _CONSOLE_CHAIN)
        assert result.returncode == 0, f"{result.stdout}{result.stderr}"
        assert "REFUSED" not in result.stdout
        assert "done" in result.stdout
        assert not (profiles / "home-wifi.nmconnection").exists()
        assert "REACHED-END" in result.stdout
        # The legacy wpa_supplicant template must have replaced the PSK-bearing
        # file — via the parameterized path, never the host's real one.
        wpa_conf = tmp_path / "wpa_supplicant.conf"
        assert "psk=old" not in wpa_conf.read_text()
        assert "update_config=1" in wpa_conf.read_text()

    def test_n_over_ssh_is_untouched(self, tmp_path):
        """The default path never deletes anything, so it needs no refusal —
        an SSH operator answering N must sail through."""
        result, profiles = _run_wipe_branch(tmp_path, _SSH_CHAIN, reply="n")
        assert result.returncode == 0, f"{result.stdout}{result.stderr}"
        assert "Keeping WiFi credentials." in result.stdout
        assert (profiles / "home-wifi.nmconnection").exists()
        assert "REACHED-END" in result.stdout


def _extract_marker_write() -> str:
    body = PREPARE_SH.read_text()
    start = body.index('if ! mkdir -p "$STATE_DIR"')
    # `\nfi\n`, not `fi` — a bare substring search lands on the `fi` inside
    # words like "first" and silently truncates the span (caught live here).
    end = body.index("\nfi\n", start) + len("\nfi")
    span = body[start:end]
    assert "_UNFINISHED_MARKER" in span and "exit 1" in span
    assert span.rstrip().endswith("fi"), "span did not close its if"
    return span


def _extract_marker_removal() -> str:
    body = PREPARE_SH.read_text()
    # rindex: since litclock-dev#855 F1 the Step 2 abort also retires the
    # marker (it changed nothing, so its re-run must not be warned about). The
    # TAIL retirement — the one with the false-warning caveat — is this one.
    assert body.count('rm -f "$_UNFINISHED_MARKER"') == 2, "expected exactly the Step 2 and tail retirements"
    start = body.rindex('rm -f "$_UNFINISHED_MARKER"')
    end = body.index("\nfi\n", start) + len("\nfi")
    span = body[start:end]
    assert "falsely" in span, "span lost the false-warning caveat"
    assert span.rstrip().endswith("fi"), "span did not close its if"
    return span


class TestUnfinishedRunMarker:
    MARKER = "clone-prep-unfinished"

    def _env(self, state_dir):
        return (
            f"{_script_shell_environment()}\n{_YELLOW_DECL}\n"
            f"STATE_DIR={shlex.quote(str(state_dir))}\n"
            f'_UNFINISHED_MARKER="$STATE_DIR/{self.MARKER}"\n{_ERREXIT_PROBE}\n'
        )

    def test_write_creates_the_marker(self, tmp_path):
        state = tmp_path / "state"
        script = self._env(state) + _extract_marker_write()
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"{r.stdout}{r.stderr}"
        assert (state / self.MARKER).exists()

    def test_unwritable_state_dir_aborts_before_any_mutation(self, tmp_path):
        """A read-only card must be caught HERE, not discovered at Step 8. A
        regular file where the parent dir should be blocks mkdir -p for root
        and non-root alike."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory\n", encoding="utf-8")
        state = blocker / "state"
        script = self._env(state) + _extract_marker_write()
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert r.returncode == 1, f"{r.stdout}{r.stderr}"
        assert "Nothing has been changed" in r.stdout

    def test_removal_retires_the_marker(self, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        (state / self.MARKER).write_text("", encoding="utf-8")
        script = self._env(state) + _extract_marker_removal()
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"{r.stdout}{r.stderr}"
        assert not (state / self.MARKER).exists()
        assert "falsely" not in r.stdout

    def test_surviving_marker_warns_but_does_not_abort(self, tmp_path):
        """After the last verified step the card IS prepared; a stuck marker is
        the benign direction and must not turn success into a false failure."""
        state = tmp_path / "state"
        state.mkdir()
        stuck = state / self.MARKER
        stuck.mkdir()
        (stuck / "occupant").write_text("x", encoding="utf-8")
        script = self._env(state) + _extract_marker_removal()
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"{r.stdout}{r.stderr}"
        assert "falsely" in r.stdout

    def test_marker_write_precedes_step_1_and_removal_precedes_the_banner(self):
        """Ordering is the whole property: the marker must bracket every
        mutation, or a death inside the gap is invisible to the next run."""
        body = PREPARE_SH.read_text()
        executed = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
        write_at = executed.index('touch "$_UNFINISHED_MARKER"')
        step1_at = executed.index("Stopping setup-state writers")
        removal_at = executed.rindex('rm -f "$_UNFINISHED_MARKER"')
        banner_at = executed.index("SD Card Ready for Cloning!")
        # The other retirement is the Step 2 abort's (litclock-dev#855 F1). It
        # is INSIDE the abort arm, so it can only run on a path that changed
        # nothing — never on the way to the banner.
        step2_retire_at = executed.index('rm -f "$_UNFINISHED_MARKER"')
        assert step2_retire_at < executed.index("_abort_env_credentials \"$_ENV_WHY\"") < banner_at, (
            "the early marker retirement is no longer inside the Step 2 abort arm"
        )
        # Anchor on the END of Step 8 — its survivor check — not its opening
        # echo: anchored on the opening, moving the removal to before the
        # rm/verify left every test green while a death during Step 8 (the one
        # step the marker most exists to witness) left no marker
        # (/review litclock-dev#710 round 2: the guard's window must contain its subject).
        step8_verify_at = executed.index('compgen -G "$_SETUP_NET_PROFILE_GLOB"')
        # #57 changed the tail contract: the banner prints FIRST (while
        # an SSH operator can still see it), then the SSH gate runs — its
        # exit 1 must leave the marker in place for the next, console, run —
        # and only then does the marker retire, last thing before poweroff.
        gate_at = executed.rindex("disable_ssh_for_handoff")
        poweroff_at = executed.index("Powering off now so the card cannot boot")
        assert write_at < step1_at, "marker written after mutations begin"
        assert step8_verify_at < banner_at < gate_at < removal_at < poweroff_at, (
            "tail order must be: Step 8 verify -> banner -> SSH gate -> marker retirement -> "
            "poweroff; an SSH-gate refusal must still leave the marker (#57)"
        )

    def test_a_stale_marker_is_reported_at_startup(self):
        """The re-run warning must appear BEFORE the confirm prompt, while the
        operator can still stop. Comment-stripped: prose quoting the warning
        must not satisfy this (litclock-dev#653 /review)."""
        body = PREPARE_SH.read_text()
        executed = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
        warn_at = executed.index("did NOT finish")
        confirm_at = executed.index("Are you sure you want to continue?")
        assert warn_at < confirm_at, "the stale-marker warning prints after the operator already confirmed"

    def test_early_advisory_appears_before_the_confirm(self):
        executed = "\n".join(
            ln for ln in PREPARE_SH.read_text().splitlines() if not ln.lstrip().startswith("#")
        )
        advisory_at = executed.index("be REFUSED from here")
        confirm_at = executed.index("Are you sure you want to continue?")
        assert advisory_at < confirm_at, (
            "the network-session advisory must print before the confirm, while stopping is free"
        )


class TestDetectorFailsClosed:
    """/review of litclock-dev#710 (Codex finding 1): the first draft's `|| break` turned
    any ps hiccup into "local", which over SSH is exactly the wipe-then-SIGHUP
    bug. The detector now has a third verdict — 2, "could not determine" — and
    the wipe is refused on 0 AND 2; only a walk that reached init earns a 1."""

    def test_missing_ps_is_unknown_not_local(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        # A PATH with no ps at all (but bash builtins still work).
        script = f"""unset SSH_CONNECTION SSH_TTY
PATH={shlex.quote(str(bin_dir))}
{_extract_is_network_session()}
_rc=0
_is_network_session || _rc=$?
echo "VERDICT=$_rc"
"""
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert "VERDICT=2" in r.stdout, f"missing ps must be UNKNOWN, got: {r.stdout}{r.stderr}"

    def test_ps_that_errors_midwalk_is_unknown(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        ps = bin_dir / "ps"
        ps.write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
        ps.chmod(0o755)
        script = f"""unset SSH_CONNECTION SSH_TTY
PATH={shlex.quote(str(bin_dir))}:$PATH
{_extract_is_network_session()}
_rc=0
_is_network_session || _rc=$?
echo "VERDICT=$_rc"
"""
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert "VERDICT=2" in r.stdout, f"a failing ps must be UNKNOWN, got: {r.stdout}{r.stderr}"

    def test_ppid_lookup_failure_midwalk_is_unknown_not_local(self, tmp_path):
        """/review litclock-dev#710 round 2: with `ps | tr`, the `||` saw TR's status, so a
        ppid lookup that failed mid-walk — an ancestor exiting between the comm
        read and the ppid read, an ordinary race — produced an empty pid, the
        loop exited, and the verdict was "confirmed local". Over SSH with env
        stripped that re-opens the original wipe-then-SIGHUP bug. A ps that
        answers comm= but fails ppid= must yield UNKNOWN."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        ps = bin_dir / "ps"
        ps.write_text(
            '#!/bin/bash\ncase "$2" in comm=) echo bash;; ppid=) exit 1;; esac\n',
            encoding="utf-8",
        )
        ps.chmod(0o755)
        script = f"""unset SSH_CONNECTION SSH_TTY
PATH={shlex.quote(str(bin_dir))}:$PATH
{_extract_is_network_session()}
_rc=0
_is_network_session || _rc=$?
echo "VERDICT=$_rc"
"""
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert "VERDICT=2" in r.stdout, f"a failing ppid lookup must be UNKNOWN, got: {r.stdout}{r.stderr}"

    def test_pid_cycle_hits_the_hop_cap_not_an_infinite_loop(self, tmp_path):
        # A ppid table that cycles: 400 -> 300 -> 400 -> ...
        cycle = {"START": ("bash", "400"), "400": ("bash", "300"), "300": ("bash", "400")}
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _fake_ps(bin_dir, cycle)
        script = f"""unset SSH_CONNECTION SSH_TTY
PATH={shlex.quote(str(bin_dir))}:$PATH
{_extract_is_network_session()}
_rc=0
_is_network_session || _rc=$?
echo "VERDICT=$_rc"
"""
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert "VERDICT=2" in r.stdout, f"a pid cycle must be UNKNOWN, got: {r.stdout}{r.stderr}"

    def test_unknown_verdict_refuses_the_wipe(self, tmp_path):
        """The verdict has to reach the branch: rc 2 must refuse, with copy
        naming the cause, and delete nothing."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        ps = bin_dir / "ps"
        ps.write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
        ps.chmod(0o755)
        profiles = tmp_path / "system-connections"
        profiles.mkdir()
        (profiles / "home-wifi.nmconnection").write_text("psk=secret\n", encoding="utf-8")
        script = f"""{_script_shell_environment()}
{_YELLOW_DECL}
unset SSH_CONNECTION SSH_TTY
PATH={shlex.quote(str(bin_dir))}:$PATH
_NM_PROFILE_DIR={shlex.quote(str(profiles))}
nmcli() {{ return 0; }}
REPLY=y
{_extract_is_network_session()}
{_ERREXIT_PROBE}
{_extract_wipe_branch()}
echo REACHED-END
"""
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert r.returncode == 1, f"{r.stdout}{r.stderr}"
        assert "could not determine" in r.stdout.lower()
        assert "Do NOT clone" in r.stdout
        assert (profiles / "home-wifi.nmconnection").exists()
        assert "REACHED-END" not in r.stdout


def _extract_preconfirm_region() -> str:
    """The advisory + stale-marker block, lifted verbatim WITH its conditions.

    /review litclock-dev#710 round 2 (litclock-dev#662's lesson, again): the earlier tests checked
    these warnings' text POSITIONS only, so inverting either condition — the
    SSH advisory never firing over SSH, the stale warning firing only when
    there is NO marker — stayed green. Conditions must be inside the span.
    """
    body = PREPARE_SH.read_text()
    start = body.index("_NET_SESSION_RC=0")
    end = body.index('read -p "Are you sure', start)
    span = body[start : body.rindex("\n", 0, end)]
    assert "be REFUSED from here" in span, "span lost the advisory"
    assert "did NOT finish" in span, "span lost the stale-marker warning"
    assert span.count("if ") >= 2, "span lost its conditions"
    return span


def _run_preconfirm(tmp_path, chain, marker: bool, ssh_env: bool):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _fake_ps(bin_dir, chain)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    if marker:
        (state / "clone-prep-unfinished").write_text("", encoding="utf-8")
    env_line = 'export SSH_CONNECTION="10.0.0.2 51022 10.0.0.9 22"' if ssh_env else ""
    script = f"""{_script_shell_environment()}
{_YELLOW_DECL}
unset SSH_CONNECTION SSH_TTY
{env_line}
PATH={shlex.quote(str(bin_dir))}:$PATH
STATE_DIR={shlex.quote(str(state))}
_UNFINISHED_MARKER="$STATE_DIR/clone-prep-unfinished"
{_extract_is_network_session()}
{_ERREXIT_PROBE}
{_extract_preconfirm_region()}
echo REACHED-CONFIRM
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)


class TestPreConfirmWarningsExecute:
    def test_ssh_session_shows_the_advisory(self, tmp_path):
        r = _run_preconfirm(tmp_path, _CONSOLE_CHAIN, marker=False, ssh_env=True)
        assert r.returncode == 0, f"{r.stdout}{r.stderr}"
        assert "be REFUSED from here" in r.stdout
        assert "did NOT finish" not in r.stdout
        assert "REACHED-CONFIRM" in r.stdout

    def test_console_session_shows_no_advisory(self, tmp_path):
        r = _run_preconfirm(tmp_path, _CONSOLE_CHAIN, marker=False, ssh_env=False)
        assert r.returncode == 0, f"{r.stdout}{r.stderr}"
        assert "be REFUSED from here" not in r.stdout
        assert "did NOT finish" not in r.stdout
        assert "REACHED-CONFIRM" in r.stdout

    def test_stale_marker_warns_before_the_confirm(self, tmp_path):
        r = _run_preconfirm(tmp_path, _CONSOLE_CHAIN, marker=True, ssh_env=False)
        assert r.returncode == 0, f"{r.stdout}{r.stderr}"
        assert "did NOT finish" in r.stdout
        assert "sudo rm -f" in r.stdout, "the stale warning lost its clear-a-known-stale-marker advice"
        assert "REACHED-CONFIRM" in r.stdout

    def test_unknown_verdict_shows_the_could_not_determine_note(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        ps = bin_dir / "ps"
        ps.write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
        ps.chmod(0o755)
        state = tmp_path / "state"
        state.mkdir()
        script = f"""{_script_shell_environment()}
{_YELLOW_DECL}
unset SSH_CONNECTION SSH_TTY
PATH={shlex.quote(str(bin_dir))}:$PATH
STATE_DIR={shlex.quote(str(state))}
_UNFINISHED_MARKER="$STATE_DIR/clone-prep-unfinished"
{_extract_is_network_session()}
{_ERREXIT_PROBE}
{_extract_preconfirm_region()}
echo REACHED-CONFIRM
"""
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"{r.stdout}{r.stderr}"
        assert "Could not determine" in r.stdout
        assert "REACHED-CONFIRM" in r.stdout


# ───────────────────── #57: SSH posture on the clone-prep handoff ─────


GOLDEN_SSH_GATE = Path(__file__).parent / "fixtures" / "disable_ssh_for_handoff.golden"


def _extract_clone_prep_gate_fn() -> str:
    """prepare-for-cloning.sh's copy of disable_ssh_for_handoff, verbatim."""
    body = PREPARE_SH.read_text()
    start = body.index("disable_ssh_for_handoff() {")
    end = body.index("\n}\n", start) + len("\n}\n")
    assert body.count("disable_ssh_for_handoff() {") == 1
    return body[start:end]


def _extract_poweroff_block() -> str:
    """The final POWEROFF_WHEN_DONE block, verbatim — the pty test must prove
    the REAL poweroff is reached after the redirect, not a stand-in touch
    (/review litclock-dev#713, Codex finding 3)."""
    body = PREPARE_SH.read_text()
    start = body.rindex('if [[ "$POWEROFF_WHEN_DONE" == "true" ]]; then')
    end = body.index("\nfi\n", start) + len("\nfi")
    span = body[start:end]
    assert "poweroff" in span and "Power-off FAILED" in span, "span is not the final poweroff block"
    return span


def _extract_tail_region() -> str:
    """Banner through poweroff-arm gate + marker retirement, verbatim with
    conditions inside the span (litclock-dev#662)."""
    body = PREPARE_SH.read_text()
    start = body.index('echo -e "${GREEN}  SD Card Ready for Cloning!${NC}"')
    end = body.index('# litclock-dev#660 — power off LAST', start)
    span = body[start:end]
    assert "disable_ssh_for_handoff" in span and 'rm -f "$_UNFINISHED_MARKER"' in span
    assert "trap '' HUP" in span, "span lost the HUP guard"
    return span


class TestClonePrepSshGate:
    def test_function_body_matches_the_golden(self):
        """#57 adds a THIRD copy of the gate; the golden fixture
        (litclock-dev#708) is what keeps three copies from being three
        truths. Body only — the header comment is deliberately clone-prep's
        own, since reset-setup's rationale text is specific to its arms."""
        golden = GOLDEN_SSH_GATE.read_text()
        golden_fn = golden[golden.index("disable_ssh_for_handoff() {") :]
        assert _extract_clone_prep_gate_fn() == golden_fn, (
            "prepare-for-cloning.sh's disable_ssh_for_handoff has drifted from the golden — "
            "refresh via tests/fixtures/refresh_ssh_gate_golden.py (which reads reset-setup.sh) "
            "and port the same change to ALL copies (litclock-dev#657/litclock-dev#708, #57)"
        )

    def _run_tail(self, tmp_path, poweroff="true", ss_line="", net_rc="0"):
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        (state / "clone-prep-unfinished").write_text("", encoding="utf-8")
        # `rm` stubbed EXCEPT for the marker: the real gate rm targets
        # /boot/ssh flags (which must not be touched by a test run — same
        # rationale as reset-setup's _lift), while the marker retirement must
        # really happen. The stub forwards marker paths to the real rm.
        script = f"""{_script_shell_environment()}
{_YELLOW_DECL}
STATE_DIR={shlex.quote(str(state))}
_UNFINISHED_MARKER="$STATE_DIR/clone-prep-unfinished"
_NET_SESSION_RC={net_rc}
POWEROFF_WHEN_DONE={poweroff}
systemctl() {{ echo "STUB systemctl $*"; }}
raspi-config() {{ echo "STUB raspi-config $*"; }}
rm() {{
    local _a
    for _a in "$@"; do
        case "$_a" in /boot/*|/etc/*) echo "STUB-REFUSED rm $*"; return 0;; esac
    done
    case "$2" in "$_UNFINISHED_MARKER") command rm "$@";; *) echo "STUB rm $*";; esac
}}
ss() {{ printf '%s\\n' {shlex.quote(ss_line)}; }}
{_extract_clone_prep_gate_fn()}
{_ERREXIT_PROBE}
{_extract_tail_region()}
echo REACHED-POWEROFF-BLOCK
"""
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30), state

    def test_poweroff_arm_disables_ssh_then_retires_the_marker(self, tmp_path):
        """Console verdict (1): output stays on the terminal and the whole
        chain is visible. The network-verdict output contract is different —
        see the two tests below."""
        r, state = self._run_tail(tmp_path, poweroff="true", net_rc="1")
        assert r.returncode == 0, f"{r.stdout}{r.stderr}"
        assert "Disabling SSH before handoff" in r.stdout
        assert "THIS SESSION WILL DROP" not in r.stdout, "a console run must not warn about a drop that cannot happen"
        assert not (state / "clone-prep-unfinished").exists(), "marker must retire after a successful gate"
        assert "REACHED-POWEROFF-BLOCK" in r.stdout

    def test_network_session_gets_the_drop_warning_then_goes_quiet(self, tmp_path):
        """Network verdict (0): the drop warning prints BEFORE the redirect —
        the last thing the operator sees — and everything after goes to
        /dev/null (the pty may be dead; an EIO'd echo under set -e would kill
        the tail, /review litclock-dev#713). The work still happens: marker retired,
        poweroff block reached."""
        r, state = self._run_tail(tmp_path, poweroff="true", net_rc="0")
        assert r.returncode == 0, f"{r.stdout}{r.stderr}"
        assert "THIS SESSION WILL DROP" in r.stdout
        assert "Disabling SSH before handoff" not in r.stdout, (
            "gate output after the redirect must not reach a possibly-dead pty"
        )
        assert not (state / "clone-prep-unfinished").exists()
        assert "REACHED-POWEROFF-BLOCK" not in r.stdout

    def test_gate_refusal_keeps_the_marker(self, tmp_path):
        """Port 22 still listening → the gate exits 1 (do NOT hand over) —
        invisible over a dropped session, so the marker must survive for the
        next, console, run to report."""
        r, state = self._run_tail(
            tmp_path, poweroff="true", ss_line="LISTEN 0 128 0.0.0.0:22 0.0.0.0:*", net_rc="1"
        )
        assert r.returncode == 1, f"the gate did not fail closed: {r.stdout}{r.stderr}"
        assert "SSH still listening" in r.stdout
        assert (state / "clone-prep-unfinished").exists(), (
            "a refused handoff must leave the unfinished marker — the refusal is invisible over "
            "a dropped SSH session (#57)"
        )
        assert "REACHED-POWEROFF-BLOCK" not in r.stdout

    def test_no_poweroff_arm_keeps_ssh_and_says_so(self, tmp_path):
        r, state = self._run_tail(tmp_path, poweroff="false", net_rc="1")
        assert r.returncode == 0, f"{r.stdout}{r.stderr}"
        assert "Disabling SSH before handoff" not in r.stdout, (
            "--no-poweroff is the inspection path; stripping the inspector's access is the wrong trade"
        )
        assert not (state / "clone-prep-unfinished").exists()
        assert "REACHED-POWEROFF-BLOCK" in r.stdout

    def test_no_poweroff_banner_warns_the_posture_rides_the_card(self):
        body = PREPARE_SH.read_text()
        executed = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
        arm = executed[executed.index("--no-poweroff was used") - 600 : executed.index("--no-poweroff was used") + 200]
        assert "SSH was NOT disabled" in arm, (
            "the inspection arm must say the master's SSH posture will ride every clone (#57)"
        )

    def test_hup_guard_precedes_the_gate_call(self):
        body = PREPARE_SH.read_text()
        executed = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
        gate_at = executed.rindex("disable_ssh_for_handoff")
        trap_at = executed.rindex("trap '' HUP")
        assert trap_at < gate_at, (
            "disabling sshd kills this script's own session; without the HUP guard the script dies "
            "before poweroff and the next boot re-mints the setup key (litclock-dev#660)"
        )

    def test_tail_survives_its_own_session_dying_at_the_gate(self, tmp_path):
        """/review litclock-dev#713: `trap '' HUP` alone is NOT enough. Once sshd dies the
        script's stdout is a closed pty, every echo fails with EIO, and under
        `set -e` the first echo after the drop — the gate's own "done" —
        killed the script before the marker retired or poweroff ran,
        recreating the exact litclock-dev#660 hazard the trap exists to
        prevent. Reproduced on a real pty; the fix redirects the tail to
        /dev/null on any session that may drop. This test runs the REAL
        lifted spans on a REAL pty and kills the master mid-gate."""
        import pty as _pty

        state = tmp_path / "state"
        state.mkdir()
        (state / "clone-prep-unfinished").write_text("", encoding="utf-8")
        reached = state / "reached"
        script = f"""{_script_shell_environment()}
{_YELLOW_DECL}
STATE_DIR={shlex.quote(str(state))}
_UNFINISHED_MARKER="$STATE_DIR/clone-prep-unfinished"
_NET_SESSION_RC=0
POWEROFF_WHEN_DONE=true
systemctl() {{ sleep 1.2; }}
raspi-config() {{ :; }}
rm() {{
    local _a
    for _a in "$@"; do
        case "$_a" in /boot/*|/etc/*) return 0;; esac
    done
    case "$2" in "$_UNFINISHED_MARKER") command rm "$@";; *) :;; esac
}}
ss() {{ :; }}
poweroff() {{ touch {shlex.quote(str(reached))}; }}
{_extract_clone_prep_gate_fn()}
{_ERREXIT_PROBE}
{_extract_tail_region()}
{_extract_poweroff_block()}
"""
        master, slave = _pty.openpty()
        proc = subprocess.Popen(
            ["bash", "-c", script], stdout=slave, stderr=slave, stdin=slave, close_fds=True
        )
        os.close(slave)
        # Sync on OUTPUT, not a sleep (/review litclock-dev#713): a bare sleep on a loaded
        # box could close the master before bash even reaches `trap '' HUP`
        # and fail spuriously. The drop warning prints just BEFORE the trap;
        # seeing it bounds the remaining race to two builtins (trap, exec),
        # which the short sleep below covers with orders of magnitude to
        # spare — versus the whole pre-banner script before.
        import time as _time

        seen = b""
        deadline = _time.monotonic() + 20
        while b"THIS SESSION WILL DROP" not in seen:
            assert _time.monotonic() < deadline, f"never saw the drop warning: {seen!r}"
            try:
                seen += os.read(master, 4096)
            except OSError:
                break
        _time.sleep(0.2)  # let bash cross the exec redirect into the gate
        os.close(master)  # the SSH session dies
        rc = proc.wait(timeout=30)
        assert rc == 0, "the tail died with its pty — poweroff would never run (litclock-dev#660)"
        assert not (state / "clone-prep-unfinished").exists(), "marker never retired after the drop"
        assert reached.exists(), (
            "the REAL poweroff was never invoked after the drop — the block past the redirect died"
        )

# ── litclock-dev#821 / litclock-dev#839: the env.sh credential gate ───────────

_ENV_GATE_START = 'echo -n "Verifying env.sh carries no owner credentials... "'
_ENV_GATE_END = 'echo -e "${GREEN}done${NC}"'

# Every key the Step 2 wipe writes EMPTY. litclock-dev#821's gate listed the first five
# by hand and missed the last three (litclock-dev#839); the script now DERIVES the list
# from $DEFAULTS, and this is the independent statement of what that
# derivation must produce.
_OWNER_KEYS = frozenset(
    {
        "OPENWEATHERMAP_APIKEY",
        "WEATHER_LATITUDE",
        "WEATHER_LONGITUDE",
        "WEATHER_LOCATION_NAME",
        "WEATHER_IP_COUNTRY",
        "WEATHER_LAST_IP_GEO_AT",
        "LITCLOCK_LANGUAGE",
        "GIFT_MODE_MESSAGE",
    }
)


def _extract_env_abort_fn() -> str:
    """Both shared aborts, verbatim: the generic `_abort_do_not_clone` and the
    env.sh arm that delegates to it. Lifted as one span because they are
    adjacent and the delegating one cannot run alone."""
    body = PREPARE_SH.read_text()
    start = body.index("_abort_do_not_clone() {")
    end = body.index("\n}", body.index("_abort_env_credentials() {")) + len("\n}")
    fn = body[start:end]
    assert "Do NOT clone this card" in fn and "exit 1" in fn, "the shared abort lost its banner or its exit"
    assert body.count("_abort_env_credentials() {") == 1, "the env abort is defined more than once"
    assert body.count("_abort_do_not_clone() {") == 1, "the generic abort is defined more than once"
    assert start < body.index("_abort_env_credentials() {") < end, "the two aborts are no longer one span"
    return fn


def _extract_defaults_and_owner_keys() -> str:
    """`DEFAULTS=...` plus the `_ENV_OWNER_KEYS=$(...)` derivation, verbatim,
    prefixed with a source of the real lib/state.sh.

    NOT comment-stripped: the defaults body holds `# export ...` lines, and
    stripping them would test a wipe that ships a different file.

    litclock-dev#840 — the body itself now comes from `env_sh_defaults()` in
    lib/state.sh, so the lifted span is one line plus the sed. Sourcing the
    REAL helper (rather than pasting a copy here) is deliberate: it is what
    makes `_ENV_OWNER_KEYS` a live derivation from the shipped block, which is
    the litclock-dev#839 property this whole section exists to protect.
    """
    body = PREPARE_SH.read_text()
    assert len(re.findall(r"^DEFAULTS=", body, re.M)) == 1, (
        "DEFAULTS is assigned more than once; the harness may lift the wrong one"
    )
    start = re.search(r"^DEFAULTS=", body, re.M).start()
    derive_at = body.index("_ENV_OWNER_KEYS=$(", start)
    end = body.index("/p')\n", derive_at) + len("/p')")
    span = body[start:end]
    assert "sed -nE" in span, "span lost the derivation"
    assert "env_sh_defaults" in span, "span lost the call to the shared defaults helper (litclock-dev#840)"
    return f'. "{STATE_SH}"\n{span}'


def _extract_env_gate() -> str:
    """The shipped gate, span-verified like the hotspot step above."""
    body = PREPARE_SH.read_text()
    start = body.index(_ENV_GATE_START)
    end = body.index(_ENV_GATE_END, start) + len(_ENV_GATE_END)
    gate = body[start:end]
    assert "_abort_env_credentials" in gate, "extracted span lost the abort under test"
    assert "_ENV_OWNER_KEYS" in gate, "extracted span lost the derived key list"
    assert "grep -qE" in gate, "extracted span lost the re-read"
    return gate


def _shipped_defaults() -> str:
    """The env.sh Step 2 actually writes, obtained by RUNNING the assignment."""
    r = subprocess.run(
        ["bash", "-c", _extract_defaults_and_owner_keys() + '\nprintf "%s" "$DEFAULTS"'],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert "export WEATHER_LATITUDE=\n" in r.stdout, "the lifted DEFAULTS did not evaluate to the shipped file"

    # FULL EQUALITY, not "it mentions env_sh_defaults" (PR litclock-dev#858 review, item B).
    # This is the highest-fanout seed path there is — every SD card cut for
    # "Friends & Family" is born from it — and a substring check on the
    # assignment is satisfied by `$(env_sh_defaults | grep -v RENDER_LEAD)`,
    # which ships every clone an env.sh missing a documented knob. Equality
    # against the helper subsumes key set, comment status, order and the
    # trailing newline.
    from tests.test_first_boot_flow import env_sh_defaults

    expected = env_sh_defaults()
    assert r.stdout == expected, (
        "prepare-for-cloning.sh's DEFAULTS is not env_sh_defaults() output. It must be the "
        "helper's body verbatim — a filter, a re-ordering or an appended line is the "
        f"hand-maintained drift litclock-dev#840 removed.\n--- got ---\n{r.stdout!r}\n"
        f"--- want ---\n{expected!r}"
    )
    return r.stdout


def _leaked(key: str, value: str) -> str:
    """The shipped defaults with ONE key's whole line replaced by a live value.

    Replace the WHOLE line: OPENWEATHERMAP_APIKEY ships commented out, so a
    naive `export KEY=` -> `export KEY=value` substitution matches the tail of
    the comment and produces `# export KEY=value`, still commented, which
    correctly does NOT trip the gate — a fixture testing nothing.
    """
    out, hit = [], False
    for ln in _shipped_defaults().splitlines():
        if ln.lstrip("# ").startswith(f"export {key}="):
            out.append(f"export {key}={value}")
            hit = True
        else:
            out.append(ln)
    assert hit, f"fixture did not find {key} in the shipped defaults"
    return "\n".join(out) + "\n"


def _run_env_gate(tmp_path, *, env_sh: str | None, owner_keys: str | None = None):
    """Execute the REAL gate against a real env.sh, under the real shell opts.

    ``env_sh=None`` leaves NO env.sh on the card. ``owner_keys`` overrides the
    derived list AFTER the real derivation ran — the fail-closed test needs it
    empty, which the shipped DEFAULTS can never produce.
    """
    install = tmp_path / "litclock"
    install.mkdir(exist_ok=True)
    if env_sh is not None:
        (install / "env.sh").write_text(env_sh)
    override = f"_ENV_OWNER_KEYS={shlex.quote(owner_keys)}\n" if owner_keys is not None else ""
    script = f"""{_script_shell_environment()}
INSTALL_DIR={shlex.quote(str(install))}
{_extract_env_abort_fn()}
{_extract_defaults_and_owner_keys()}
{override}{_ERREXIT_PROBE}
{_extract_env_gate()}
echo "REACHED_BANNER"
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)


class TestEnvCredentialGate:
    """litclock-dev#821 — a bad env.sh must not reach the success banner.

    Since litclock-dev#839 this gate is the RE-READ half only: a write that
    REPORTS failure aborts at Step 2 (TestStepTwoAbortsBeforeTheWifiPrompt
    below). What is left for the gate is a write that returned 0 and produced
    a bad file anyway.
    """

    def test_a_clean_env_passes_and_reaches_the_banner(self, tmp_path):
        r = _run_env_gate(tmp_path, env_sh=_shipped_defaults())
        assert r.returncode == 0, r.stdout + r.stderr
        assert "REACHED_BANNER" in r.stdout, "a clean card must be allowed through"

    @pytest.mark.parametrize(
        "key,value",
        [
            ("OPENWEATHERMAP_APIKEY", "deadbeefcafe"),
            ("WEATHER_LATITUDE", "30.2672"),
            ("WEATHER_LONGITUDE", "-97.7431"),
            ("WEATHER_LOCATION_NAME", "Austin, Texas"),
            ("GIFT_MODE_MESSAGE", "Happy birthday"),
            # The three litclock-dev#821's hand-written list missed (litclock-dev#839).
            ("WEATHER_IP_COUNTRY", "US"),
            ("WEATHER_LAST_IP_GEO_AT", "2026-09-13T11:32:05Z"),
            ("LITCLOCK_LANGUAGE", "de"),
        ],
    )
    def test_a_surviving_secret_aborts_even_when_the_write_reported_success(self, tmp_path, key, value):
        """The silent half: `atomic_write_env_sh` returned 0 but the file is
        wrong. A return-code check alone cannot see this, which is why the gate
        re-reads the file — Step 8's idiom."""
        leaked = _leaked(key, value)
        assert f"\nexport {key}={value}\n" in "\n" + leaked, "fixture did not actually inject the secret"
        r = _run_env_gate(tmp_path, env_sh=leaked)
        assert r.returncode == 1, f"{key} survived the wipe and the gate let it through\n{r.stdout}"
        assert key in r.stdout, f"the abort must NAME the surviving key; got {r.stdout!r}"
        assert "Do NOT clone this card" in r.stdout
        assert "REACHED_BANNER" not in r.stdout

    def test_a_commented_apikey_is_not_a_leak(self, tmp_path):
        """The shipped defaults comment OPENWEATHERMAP_APIKEY out entirely, so a
        commented line must not trip the gate — otherwise it fails every run."""
        shipped = _shipped_defaults()
        assert "# export OPENWEATHERMAP_APIKEY=" in shipped, "the shipped defaults no longer comment the key out"
        r = _run_env_gate(tmp_path, env_sh=shipped)
        assert r.returncode == 0, "a commented-out key is the SHIPPED state, not a leak"

    def test_an_absent_env_sh_passes_the_gate(self, tmp_path):
        """litclock-dev#844 item 4 — the branch an operator least expects.

        An absent env.sh at the gate is NOT a wiped-away file: Step 2 leaves an
        absent env.sh absent (its `if [[ -f ]]` guards the write, it never
        creates one), nothing between Step 2 and the gate touches env.sh (Step 5
        removes `*.log` and `weather-cache*.json` only), and first-boot.sh
        seeds a fresh env.sh on the clone. So absent means "no owner data", and
        aborting on it would refuse a card that is safe.
        """
        r = _run_env_gate(tmp_path, env_sh=None)
        assert not (tmp_path / "litclock" / "env.sh").exists(), "fixture left an env.sh behind"
        assert r.returncode == 0, f"an absent env.sh must pass the gate\n{r.stdout}{r.stderr}"
        assert "REACHED_BANNER" in r.stdout
        assert "FAILED" not in r.stdout

    def test_an_empty_key_list_fails_closed(self, tmp_path):
        """litclock-dev#773's rule, applied here: nothing verified is a failure. With the
        derived list empty the loop would check nothing and print `done` over a
        file full of owner data."""
        r = _run_env_gate(tmp_path, env_sh=_leaked("OPENWEATHERMAP_APIKEY", "deadbeef"), owner_keys="")
        assert r.returncode == 1, f"an empty key list let a leaking env.sh through\n{r.stdout}"
        assert "no keys were verified" in r.stdout
        assert "REACHED_BANNER" not in r.stdout

    def test_the_gate_keys_are_derived_from_the_wipe(self):
        """The list is computed from $DEFAULTS by the shipped sed, so it cannot
        drift from the wipe the way litclock-dev#821's hand-written list did. Two
        checks: the derivation yields exactly the eight owner keys, AND those
        are exactly the keys DEFAULTS writes empty (computed here independently
        of the sed), so a DEFAULTS edit that adds an owner key is picked up and
        a sed regression that drops one is caught."""
        r = subprocess.run(
            ["bash", "-c", _extract_defaults_and_owner_keys() + '\nprintf "%s\\n" "$_ENV_OWNER_KEYS"'],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        derived = frozenset(ln for ln in r.stdout.split("\n") if ln)
        assert derived == _OWNER_KEYS, f"derived {sorted(derived)}"
        empties = frozenset(
            m.group(1)
            for m in re.finditer(r"^#?\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=\s*$", _shipped_defaults(), re.M)
        )
        assert derived == empties, f"the sed and an independent scan of DEFAULTS disagree: {sorted(derived ^ empties)}"

    def test_the_gate_precedes_both_the_banner_and_the_poweroff(self):
        """Position is the whole point: after either one, the operator is gone."""
        body = PREPARE_SH.read_text()
        gate_at = body.index(_ENV_GATE_START)
        # The ECHO, not the first mention: "SD Card Ready for Cloning!" also
        # appears in two comments near the top of the file, so index() on the
        # bare phrase returns offset ~1194 and the assertion compares against
        # the wrong thing. Caught by running it.
        banner_at = body.index('echo -e "${GREEN}  SD Card Ready for Cloning!${NC}"')
        assert gate_at < banner_at, "the credential gate must run before the success banner"
        # The EXECUTED call, searched from the gate on the comment-stripped
        # text. The first version did `body.find("poweroff", banner_at)`
        # inside `if != -1`: a search that starts AT the banner can only
        # find something after it, and a miss passed silently, so the
        # assertion could not go red (v0.227.0 port review, testing pass).
        executed = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
        gate_exec_at = executed.index(_ENV_GATE_START)
        poweroff_at = executed.index("poweroff || {")
        assert gate_exec_at < poweroff_at, "the credential gate must run before the power-off"

    def test_the_failure_arm_aborts_instead_of_raising_a_flag(self):
        """The two regressions, in order: litclock-dev#821's `true  # explicit success`
        (a swallowed failure) and litclock-dev#821's own fix, a flag read after Step 8
        (a failure acted on too late, litclock-dev#839). Executed lines only — the
        comments still tell both stories."""
        body = PREPARE_SH.read_text()
        assert "true  # explicit success for `set -e`" not in body, (
            "the env.sh wipe failure arm is swallowing the failure again (litclock-dev#821)"
        )
        executed = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
        assert "ENV_WIPE_FAILED" not in executed, (
            "the failure arm is raising a flag for a later gate again instead of aborting at Step 2 (litclock-dev#839)"
        )
        # The WiFi prompt is the first executed line of Step 3 (the `# Step 3`
        # heading is a comment and is gone from `executed`).
        step2_at = executed.index(_STEP2_START)
        step2 = executed[step2_at : executed.index('read -p "Clear saved WiFi networks?', step2_at)]
        assert "_abort_env_credentials" in step2, "Step 2's failure arm does not call the shared abort"


# ── litclock-dev#839: Step 2 aborts BEFORE the WiFi prompt, Steps 2-8 executed ─
#
# The defect was ORDER: the abort existed but ran after Step 8, so on a failed
# wipe the script still deleted every WiFi profile, stopped the clock, wiped
# the logs and removed the setup-WiFi key — on a card it then refused to clone.
# A test of Step 2 alone cannot see order, so this harness lifts Steps 2
# through the gate and runs them for real against a tmp card: every mutation
# site is parameterised (INSTALL_DIR, STATE_DIR, _NM_PROFILE_DIR,
# _WPA_SUPPLICANT_CONF, the two history paths) and the system commands are
# recorded stubs. The one literal host path in the span, Step 5's
# `rm -f /tmp/litclock-*`, is stripped — and the strip is asserted exact.

_STEP2_START = 'echo -n "Clearing configuration (env.sh)... "'
_TMP_LITTER_LINE = "rm -f /tmp/litclock-* 2>/dev/null || true"


def _extract_steps_2_through_gate() -> str:
    body = PREPARE_SH.read_text()
    assert body.count(_STEP2_START) == 1, "Step 2 anchor is no longer unique"
    start = body.index(_STEP2_START)
    gate_at = body.index(_ENV_GATE_START, start)
    end = body.index(_ENV_GATE_END, gate_at) + len(_ENV_GATE_END)
    span = body[start:end]
    for needle in (
        "Removing setup-state markers",
        "Clear saved WiFi networks?",
        "Clearing setup-hotspot password",
        "nmcli connection delete",
        "Clearing bash history",
    ):
        assert needle in span, f"span cut short of {needle!r}"
    lines = span.splitlines()
    kept = [ln for ln in lines if ln.strip() != _TMP_LITTER_LINE]
    assert len(lines) - len(kept) == 1, "expected to strip exactly one /tmp litter line from the span"
    return "\n".join(kept)


_ENV_SH_BEFORE = "export OPENWEATHERMAP_APIKEY=deadbeefcafe\nexport WEATHER_LOCATION_NAME=Austin\n"


def _run_steps_2_through_gate(tmp_path, *, write_rc: int, with_env_sh: bool = True):
    """Run Steps 2..gate. ``write_rc`` is what the stubbed atomic_write_env_sh
    returns; 0 makes it write its argument, the way the real helper does.
    ``with_env_sh=False`` stages a card with no env.sh at all."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_ps(bin_dir, _CONSOLE_CHAIN)  # a console login, so the y-wipe is not refused
    install = tmp_path / "litclock"
    install.mkdir()
    if with_env_sh:
        (install / "env.sh").write_text(_ENV_SH_BEFORE)
    (install / "first-boot.log").write_text("x\n")
    (install / ".certs").mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "hotspot-password").write_text("setup-key\n")
    # litclock-dev#855 review item A: the setup-state markers are removed in
    # Step 2b, INSIDE this span, so an aborted run can be checked for them.
    config = tmp_path / "etc-litclock"
    config.mkdir()
    (config / ".setup-complete").write_text("")
    (config / ".handoff-complete").write_text("")
    profiles = tmp_path / "system-connections"
    profiles.mkdir()
    (profiles / "home-wifi.nmconnection").write_text("psk=secret\n")
    (profiles / "litclock-hotspot.nmconnection").write_text("psk=setup-key\n")
    wpa = tmp_path / "wpa_supplicant.conf"
    wpa.write_text("network={ psk=old }\n")
    home = tmp_path / "home"
    home.mkdir()
    pi_hist = home / "pi.bash_history"
    root_hist = home / "root.bash_history"
    pi_hist.write_text("nmcli dev wifi connect x password y\n")
    root_hist.write_text("sudo -i\n")
    prompts = tmp_path / "prompts.rec"
    nm_rec = tmp_path / "nmcli.rec"
    if write_rc == 0:
        writer = 'atomic_write_env_sh() { printf "%s" "$2" > "$1"; }'
    else:
        writer = f"atomic_write_env_sh() {{ return {write_rc}; }}"
    script = f"""{_script_shell_environment()}
{_YELLOW_DECL}
unset SSH_CONNECTION SSH_TTY
PATH={shlex.quote(str(bin_dir))}:$PATH
INSTALL_DIR={shlex.quote(str(install))}
STATE_DIR={shlex.quote(str(state))}
CONFIG_DIR={shlex.quote(str(config))}
_NM_PROFILE_DIR={shlex.quote(str(profiles))}
_WPA_SUPPLICANT_CONF={shlex.quote(str(wpa))}
_PI_BASH_HISTORY={shlex.quote(str(pi_hist))}
_ROOT_BASH_HISTORY={shlex.quote(str(root_hist))}
read() {{
    # Only the y/N PROMPT is answered; Step 3's `while read -r _con` loop must
    # still reach the builtin, or it spins forever on the stub's constant.
    if [[ "$1" == "-p" ]]; then REPLY=y; printf '%s\\n' "$*" >> {shlex.quote(str(prompts))}; else builtin read "$@"; fi
}}
nmcli() {{ printf 'nmcli %s\\n' "$*" >> {shlex.quote(str(nm_rec))}; [[ "$1" == "-t" ]] && echo home-wifi; return 0; }}
systemctl() {{ :; }}
journalctl() {{ :; }}
{_extract_is_network_session()}
{_extract_env_abort_fn()}
{_extract_defaults_and_owner_keys()}
# AFTER the span above, which sources the real lib/state.sh (litclock-dev#840)
# and would otherwise replace this stub with the real flocked writer.
{writer}
{_ERREXIT_PROBE}
{_extract_steps_2_through_gate()}
echo REACHED_GATE_DONE
"""
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert not (result.returncode == 99 and _ERREXIT_PROBE_MARKER in result.stderr), result.stderr
    return result, {
        "env_sh": install / "env.sh",
        "hotspot_password": state / "hotspot-password",
        "home_wifi": profiles / "home-wifi.nmconnection",
        "pi_hist": pi_hist,
        "prompts": prompts,
        "nm_rec": nm_rec,
        "setup_complete": config / ".setup-complete",
        "handoff_complete": config / ".handoff-complete",
    }


class TestStepTwoAbortsBeforeTheWifiPrompt:
    @pytest.mark.parametrize("write_rc", [75, 1], ids=["flock-timeout", "failed-write"])
    def test_a_failed_wipe_stops_before_anything_else_is_touched(self, tmp_path, write_rc):
        r, card = _run_steps_2_through_gate(tmp_path, write_rc=write_rc)
        assert r.returncode == 1, f"expected the Step 2 abort, got {r.returncode}\n{r.stdout}{r.stderr}"
        assert "Do NOT clone this card" in r.stdout, "the abort must carry the same banner as the gate"
        assert "REACHED_GATE_DONE" not in r.stdout
        # BEFORE the WiFi prompt: the prompt was never asked...
        assert not card["prompts"].exists(), f"the WiFi prompt ran after a failed wipe: {card['prompts'].read_text()}"
        # ...so no NM profile was deleted, on the card or in the daemon...
        assert card["home_wifi"].exists(), "the saved WiFi profile was deleted after a failed wipe"
        assert not card["nm_rec"].exists(), f"nmcli was called after a failed wipe: {card['nm_rec'].read_text()}"
        # ...the setup-WiFi key is still there (the litclock-dev#839 bench observation
        # was "password... done" printed BEFORE the env.sh failure)...
        assert card["hotspot_password"].exists(), "Step 8 ran after a failed wipe"
        # ...and env.sh is exactly as it was, owner data included, which is why
        # the operator is told not to clone.
        # BYTE-FOR-BYTE (litclock-dev#855 review E1). A substring check on the
        # API key passes a partial rewrite that dropped WEATHER_LOCATION_NAME —
        # and "env.sh is untouched" is exactly what the banner claims.
        assert card["env_sh"].read_text() == _ENV_SH_BEFORE, "env.sh was modified by the aborted run"
        assert card["pi_hist"].is_file(), "Step 6 ran after a failed wipe"
        # litclock-dev#855 review item A — the abort must leave a BOOTABLE
        # device. litclock-firstboot.service is disabled on a provisioned clock
        # and is only re-enabled at Step 4, while litclock.service and
        # litclock-control.service are ConditionPathExists-gated on these two
        # markers: removing them before an abortable step meant an aborted run
        # left a device that booted into neither setup nor the clock — the
        # litclock-dev#659 looks-bricked state, reachable from a flock timeout.
        assert card["setup_complete"].exists() and card["handoff_complete"].exists(), (
            "the Step 2 abort removed the setup-state markers, so this device boots into nothing"
        )
        assert "Nothing on this card has been changed" in r.stdout, (
            "the banner must tell the operator the card is untouched and still boots normally"
        )
        assert "boots into its normal clock" in r.stdout
        if write_rc == 75:
            assert "locked by another writer" in r.stdout
        else:
            assert f"rc={write_rc}" in r.stdout

    def test_a_card_with_no_env_sh_reaches_the_gate_with_none(self, tmp_path):
        """litclock-dev#855 review E2 — the half of test_an_absent_env_sh_passes
        _the_gate that was argued in a docstring while the gate ran in
        isolation. Run Steps 2 through the gate on a card with NO env.sh: Step
        2's `-f` guard must not create one, nothing in Steps 2b-8 may create or
        remove one, and the gate must still pass it."""
        r, card = _run_steps_2_through_gate(tmp_path, write_rc=0, with_env_sh=False)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "REACHED_GATE_DONE" in r.stdout
        assert not card["env_sh"].exists(), "something between Step 2 and the gate created an env.sh"
        assert "Do NOT clone this card" not in r.stdout

    def test_a_successful_wipe_runs_through_to_the_gate(self, tmp_path):
        """Positive control for the harness: with the write succeeding, every
        step it claims to guard actually runs in it, so the negative assertions
        above are about ORDER, not about a span that never reaches Step 3."""
        r, card = _run_steps_2_through_gate(tmp_path, write_rc=0)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "REACHED_GATE_DONE" in r.stdout
        assert "Do NOT clone this card" not in r.stdout
        assert card["prompts"].exists() and "Clear saved WiFi networks?" in card["prompts"].read_text()
        # ...and the markers ARE removed once the wipe succeeded, so the move in
        # item A delayed the removal rather than dropping it.
        assert not card["setup_complete"].exists() and not card["handoff_complete"].exists(), (
            "Step 2b did not remove the setup-state markers on a successful run"
        )
        nm = card["nm_rec"].read_text()
        assert "nmcli connection delete home-wifi" in nm, nm
        assert "nmcli connection delete litclock-hotspot" in nm, nm
        assert not card["home_wifi"].exists()
        assert not card["hotspot_password"].exists()
        env = card["env_sh"].read_text()
        assert "deadbeefcafe" not in env and "export WEATHER_LOCATION_MODE=auto" in env
        assert card["pi_hist"].is_dir(), "Step 6 did not lock the history"


# ── litclock-dev#834: the bash history lock ──────────────────────────────────
#
# `history -c` reaches only the script's own shell; the interactive shell the
# operator ran it from writes its history back on exit, during the power-off.
# Bench, 2026-09-13: "Clearing bash history... done" at 11:32:05, and the file
# was back at 11:32:13 with the operator's last command in it. The lock is an
# empty DIRECTORY at each path, and the test that matters is the last one: a
# real interactive bash, holding history, exiting against the locked path.

_HIST_START = 'echo -n "Clearing bash history... "'
_HIST_END = "unset _HIST_DIRTY _HIST_UNLOCKED _h"


def _extract_history_step() -> str:
    body = PREPARE_SH.read_text()
    assert body.count(_HIST_START) == 1, "history step anchor is no longer unique"
    start = body.index(_HIST_START)
    # Through the `done` that CLOSES the step, not just the unset: the green
    # line is the thing the failure cases must not reach.
    end = body.index('echo -e "${GREEN}done${NC}"', body.index(_HIST_END, start))
    end += len('echo -e "${GREEN}done${NC}"')
    span = body[start:end]
    assert "mkdir" in span and "history -c" in span, "span lost the lock or the clear"
    assert span.count("_abort_do_not_clone") == 2, "span lost one of the two aborts"
    return span


def _run_history_step(tmp_path, pi_path: Path, root_path: Path):
    script = f"""{_script_shell_environment()}
{_YELLOW_DECL}
_PI_BASH_HISTORY={shlex.quote(str(pi_path))}
_ROOT_BASH_HISTORY={shlex.quote(str(root_path))}
{_extract_env_abort_fn()}
{_ERREXIT_PROBE}
{_extract_history_step()}
echo REACHED-NEXT-STEP
"""
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert not (r.returncode == 99 and _ERREXIT_PROBE_MARKER in r.stderr), r.stderr
    return r


def _interactive_shell_exits_with_history(histfile: Path) -> None:
    """A real `bash -i` that records a line, forces a `history -w` and then
    exits — the exit-time save is the bench mechanism, the explicit -w is the
    rename() path an empty mode-0 file does NOT stop."""
    subprocess.run(
        ["bash", "--norc", "-i"],
        input="echo nmcli-password-line\nhistory -w\nexit\n",
        env={**os.environ, "HISTFILE": str(histfile), "HISTSIZE": "500", "HISTFILESIZE": "500"},
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestBashHistoryLock:
    @staticmethod
    def _paths(tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        pi, root = home / "pi.bash_history", home / "root.bash_history"
        pi.write_text("nmcli dev wifi connect x password y\n")
        root.write_text("sudo -i\n")
        return home, pi, root

    def test_both_histories_become_empty_directories(self, tmp_path):
        home, pi, root = self._paths(tmp_path)
        r = _run_history_step(tmp_path, pi, root)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "done" in r.stdout and "FAILED" not in r.stdout
        assert "REACHED-NEXT-STEP" in r.stdout
        for p in (pi, root):
            assert p.is_dir() and not p.is_symlink(), f"{p} is not the lock directory"
            assert not any(p.iterdir()), f"{p} is not empty"
        assert sorted(q.name for q in home.iterdir()) == ["pi.bash_history", "root.bash_history"], (
            "the step left something else in the home directory"
        )

    def test_a_second_run_over_the_lock_is_a_noop(self, tmp_path):
        """A re-run after an earlier abort meets the directory, not a file."""
        _, pi, root = self._paths(tmp_path)
        assert _run_history_step(tmp_path, pi, root).returncode == 0
        r = _run_history_step(tmp_path, pi, root)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "FAILED" not in r.stdout
        assert pi.is_dir() and root.is_dir()

    def test_the_harness_shell_really_writes_history(self, tmp_path):
        """Control for the test below: WITHOUT the lock the same shell writes
        the line back, so a passing lock test is not a shell that never saved."""
        hist = tmp_path / ".bash_history"
        _interactive_shell_exits_with_history(hist)
        assert hist.is_file(), "the harness shell did not save history at all; the lock test would be vacuous"
        assert "nmcli-password-line" in hist.read_text()

    def test_the_lock_survives_an_interactive_shell_exiting(self, tmp_path):
        """The bench mechanism, reproduced: lock, then let a real interactive
        shell holding a password-bearing line exit against the locked path."""
        home, pi, root = self._paths(tmp_path)
        assert _run_history_step(tmp_path, pi, root).returncode == 0
        _interactive_shell_exits_with_history(pi)
        assert pi.is_dir(), "the departing shell replaced the lock with a history file"
        assert not any(pi.iterdir())
        assert sorted(q.name for q in home.iterdir()) == ["pi.bash_history", "root.bash_history"], (
            "the departing shell left its history somewhere next to the lock"
        )
        for q in home.rglob("*"):
            if q.is_file():
                assert "nmcli-password-line" not in q.read_text(), f"the line was written back to {q}"

    def test_a_pre_existing_non_empty_lock_directory_aborts(self, tmp_path):
        """litclock-dev#855 review item B, the shape that printed GREEN. An
        aborted earlier run leaves the lock directory; anything dropped inside
        it defeats `rmdir`, defeats `rm -f`, defeats `mkdir` — and a bare
        `[[ -d ]]` then reads "locked". first-boot's rmdir leaves the contents
        too, so they reach every clone."""
        home, pi, root = self._paths(tmp_path)
        pi.unlink()
        pi.mkdir()
        (pi / "history.bak").write_text("nmcli dev wifi connect x password y\n")
        r = _run_history_step(tmp_path, pi, root)
        assert r.returncode == 1, f"a non-empty lock directory was accepted\n{r.stdout}"
        assert "FAILED" in r.stdout and "Do NOT clone this card" in r.stdout
        assert "Could not clear the shell history" in r.stdout and str(pi) in r.stdout
        assert "setup-WiFi key NOT yet removed" in r.stdout, "the abort must say what state the card is in"
        assert "REACHED-NEXT-STEP" not in r.stdout
        assert (pi / "history.bak").exists(), "the contents are still there — which is why this is fatal"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions; the failure cannot be staged")
    def test_a_history_file_that_cannot_be_removed_aborts(self, tmp_path):
        """The chattr +i / dying-card shape, staged with a read-only parent.
        The credentials are still on disk, so a cloning master must stop."""
        home, pi, root = self._paths(tmp_path)
        home.chmod(0o555)
        try:
            r = _run_history_step(tmp_path, pi, root)
        finally:
            home.chmod(0o755)
        assert r.returncode == 1, f"an unremovable history file was accepted\n{r.stdout}"
        assert "Could not clear the shell history" in r.stdout
        assert str(pi) in r.stdout and str(root) in r.stdout
        assert "REACHED-NEXT-STEP" not in r.stdout
        assert pi.is_file() and "password" in pi.read_text(), "the history really did survive"

    def test_a_path_that_is_cleared_but_cannot_be_locked_aborts(self, tmp_path):
        """The other half of B: nothing survives at the path, but the lock
        cannot be applied either (here: the parent directory does not exist, so
        `rm -f` succeeds vacuously and `mkdir` fails). The history is gone, yet
        the shell running this script will still write its own back on exit —
        litclock-dev#834 itself — so green would be a lie."""
        _, _, root = self._paths(tmp_path)
        pi = tmp_path / "no-such-dir" / ".bash_history"
        r = _run_history_step(tmp_path, pi, root)
        assert r.returncode == 1, f"an unlockable path was accepted\n{r.stdout}"
        assert "Could not lock" in r.stdout and str(pi) in r.stdout
        assert "litclock-dev#834" in r.stdout, "the abort must point at the issue it enforces"
        assert "REACHED-NEXT-STEP" not in r.stdout
        assert not pi.exists()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions; the failure cannot be staged")
    def test_a_symlinked_history_that_cannot_be_removed_aborts(self, tmp_path):
        """A symlink to a directory is the one shape where `-d` and `-L` are
        both true. With the parent read-only it cannot be unlinked either, so
        whatever it points at rides the card."""
        home, pi, root = self._paths(tmp_path)
        pi.unlink()
        target = tmp_path / "elsewhere"
        target.mkdir()
        (target / "history").write_text("nmcli dev wifi connect x password y\n")
        pi.symlink_to(target)
        home.chmod(0o555)
        try:
            r = _run_history_step(tmp_path, pi, root)
        finally:
            home.chmod(0o755)
        assert r.returncode == 1, f"a symlinked history survived and was accepted\n{r.stdout}"
        assert "Could not clear the shell history" in r.stdout and str(pi) in r.stdout
        assert "REACHED-NEXT-STEP" not in r.stdout
        assert pi.is_symlink() and (target / "history").exists()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions; the failure cannot be staged")
    def test_a_dangling_symlink_that_cannot_be_removed_aborts(self, tmp_path):
        """The `-L` arm's own case (litclock-dev#855 review E3). `-e` FOLLOWS
        symlinks and is false for a dangling one, so without `|| -L` an entry
        that survived at the history path reads as "removed" — and the next
        shell to exit writes its history straight through it."""
        home, pi, root = self._paths(tmp_path)
        pi.unlink()
        pi.symlink_to(tmp_path / "gone")
        home.chmod(0o555)
        try:
            r = _run_history_step(tmp_path, pi, root)
        finally:
            home.chmod(0o755)
        assert r.returncode == 1, f"a dangling symlink at the history path was accepted\n{r.stdout}"
        assert "Could not clear the shell history" in r.stdout and str(pi) in r.stdout
        assert "REACHED-NEXT-STEP" not in r.stdout
        assert pi.is_symlink(), "the symlink really did survive"

    def test_the_operator_rule_is_in_the_header_and_the_doc(self):
        """The cheap half of litclock-dev#834: run it as the only open session."""
        raw = PREPARE_SH.read_text().split("\nset -e\n", 1)[0]
        # Unwrapped: the header is comment-wrapped at ~76 columns, so a phrase
        # test on the raw text fails on where the wrap happens to fall.
        header = " ".join(ln.lstrip("#").strip() for ln in raw.splitlines())
        assert "ONLY open session" in header and "history -c" in header, "the header no longer states the rule"
        # litclock-dev#855 review item D: the rule cannot cover the shell the
        # operator is typing in, and the header must not imply that it does.
        assert "cannot be one of them" in header, "the header no longer says the initiating shell is not covered"
        doc_text = (REPO_ROOT / "docs" / "sd-card-cloning.md").read_text().lower()
        assert "the shell you run it from" in doc_text, "the doc no longer says the initiating shell is not covered"
        doc = (REPO_ROOT / "docs" / "sd-card-cloning.md").read_text()
        assert "only open session" in doc.lower() and "log out" in doc.lower(), (
            "the cloning doc no longer states the rule"
        )
        assert "history" in doc.lower()

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

    def test_the_memo_is_removed(self, prepare_sh_content):
        executed = "\n".join(
            ln for ln in prepare_sh_content.splitlines() if not ln.lstrip().startswith("#")
        )
        assert 'rm -f "$STATE_DIR/runtime-render-validation.json"' in executed, (
            "the runtime-render validation memo survives clone preparation; the next owner (or "
            "every clone) inherits a failure verdict about someone else's device"
        )

    def test_it_sits_with_the_other_state_removals(self, prepare_sh_content):
        """Next to the reset-failed marker, which is the same class of
        per-device bookkeeping and the same best-effort treatment — so the two
        cannot drift apart into different failure policies."""
        executed = "\n".join(
            ln for ln in prepare_sh_content.splitlines() if not ln.lstrip().startswith("#")
        )
        a = executed.index('rm -f "$STATE_DIR/reset-failed"')
        b = executed.index('rm -f "$STATE_DIR/runtime-render-validation.json"')
        assert abs(a - b) < 200, executed[min(a, b): max(a, b) + 80]
