"""The runtime-render validation marker is stamped automatically (litclock-dev#531).

`literary_clock._runtime_render_enabled()` requires BOTH the
`LITCLOCK_RUNTIME_RENDER` flag AND the `.runtime-render-validated` marker. Until
this landed, **nothing wrote that marker** — not pi-gen, not `update.sh`, and
there is no `install.sh`. So on every fielded device the flag was inert and the
runtime renderer was unreachable: shipped, hardware-A/B-passed, soaked for five
days, and dead. `update.sh`'s litclock-dev#604 block only REVOKES the marker, which
without a re-stamp is a one-way ratchet down to the pre-rendered tier forever.

These tests pin the two writers and, more importantly, WHERE they sit — the
position is what makes them safe.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"
UPDATE_UNIT = REPO_ROOT / "systemd" / "litclock-update.service"
# litclock-dev#835: `validate_measurement.py check --stamp` wall-clock on the
# bench Pi Zero 2W, 2026-09-12, fresh venv, idle (x86: ~21s). Re-measure and
# update HERE; the prose copies in update.sh and the unit point at this pin.
MEASURED_VALIDATOR_S = 182
VALIDATOR_MARGIN = 1.4
PIGEN_APP = REPO_ROOT / "pi-gen" / "stage3" / "01-setup-app" / "00-run.sh"
VALIDATOR = REPO_ROOT / "tools" / "validate_measurement.py"
DUMP = REPO_ROOT / "tools" / "gd-expected-measurements.json.gz"

# Anchored on an INTERPRETER PREFIX so only the EXECUTED call matches. Without
# it, `.search()` returned the first of three prose mentions in update.sh — a
# comment at :813 and two log strings in the revoke block — and every position
# assertion below compared against the wrong offset. Found by running the
# tests; the same trap litclock-dev#782 catalogued and this session has hit twice.
STAMP_CALL = re.compile(r'(?:"\$PYTHON"|\./venv/bin/python3)\s+tools/validate_measurement\.py\s+check\s+--stamp')


class TestSomethingActuallyWritesTheMarker:
    """The regression itself: a tree where nothing stamps."""

    def test_at_least_one_install_path_stamps(self):
        writers = [p.name for p in (UPDATE_SH, PIGEN_APP) if STAMP_CALL.search(p.read_text())]
        assert writers, (
            "NOTHING writes .runtime-render-validated. literary_clock requires it before it "
            "will honour LITCLOCK_RUNTIME_RENDER, so the runtime renderer is unreachable on "
            "every device — the exact state litclock-dev#531 sat in for weeks."
        )

    @pytest.mark.parametrize("script", [UPDATE_SH, PIGEN_APP], ids=["update.sh", "pi-gen"])
    def test_both_install_paths_stamp(self, script):
        assert STAMP_CALL.search(script.read_text()), (
            f"{script.name} does not stamp. A device that only ever flashes (never updates) "
            "or only ever updates (never reflashes) would never reach the runtime tier."
        )

    def test_the_validator_and_its_dump_ship(self):
        """Both are needed ON the device — the check runs there, not on a build host."""
        assert VALIDATOR.is_file(), "the validator must ship; the check runs on the device"
        assert DUMP.is_file(), "the expected-measurement dump must ship alongside it"
        out = subprocess.run(["git", "ls-files", "--error-unmatch", str(DUMP.relative_to(REPO_ROOT))],
                             cwd=REPO_ROOT, capture_output=True, text=True)
        assert out.returncode == 0, "the dump must be tracked, or a fresh clone cannot validate"


class TestUpdateShPlacesItSafely:
    """Position is the whole safety argument in update.sh."""

    def _body(self):
        return UPDATE_SH.read_text()

    def test_the_stamp_is_inside_the_smoke_KEEP_arm(self):
        """A smoke failure git-resets the tree. A marker written outside the KEEP
        arm would certify code that was just reverted."""
        body = self._body()
        keep_at = body.index('if [[ "$smoke_rc" -eq 0 ]]; then\n    log_info "Smoke test passed"')
        revert_at = body.index('log_error "Smoke test failed', keep_at)
        stamp_at = STAMP_CALL.search(body).start()
        assert keep_at < stamp_at < revert_at, (
            "the stamp must sit between the KEEP arm's start and the revert arm, or a reverted "
            "update can leave a marker certifying code that is no longer checked out"
        )

    def test_the_stamp_runs_after_the_pip_rebuild(self):
        """The check certifies THIS freetype-py wheel. Stamping before Phase 4
        would certify the wheel the update is about to replace."""
        body = self._body()
        pip_at = body.index('if [[ "$NEED_PIP" == "true" ]]; then')
        stamp_at = STAMP_CALL.search(body).start()
        assert pip_at < stamp_at, "the stamp must run after Phase 4's pip rebuild"

    def test_the_stamp_runs_after_the_revoke_block(self):
        """Otherwise the litclock-dev#604 revoke would delete the marker just written."""
        body = self._body()
        revoke_at = body.index("RUNTIME_MARKER=$(")
        stamp_at = STAMP_CALL.search(body).start()
        assert revoke_at < stamp_at, "the revoke block must run BEFORE the re-stamp, not after"

    def test_it_only_stamps_when_the_marker_is_absent(self):
        """A surviving marker already passed, and the revoke block is what decides
        that. Re-running ~21s on x86 (minutes on a Pi Zero) every weekly tick
        would be pure cost."""
        body = self._body()
        stamp_at = STAMP_CALL.search(body).start()
        guard = body.rindex('if [[ ! -f "$RUNTIME_MARKER"', 0, stamp_at)
        assert stamp_at - guard < 1200, "the stamp must be guarded by an absent-marker check"

    def test_it_is_bounded_and_the_bound_fits_the_unit(self):
        """litclock-dev#835. The first version of this test asserted only that a
        `timeout ` token existed near the call. It did: `timeout 600` — inside
        `litclock-update.service`, whose TimeoutStartSec was ALSO 600, with
        litclock.timer stopped since Phase 1. A bound that cannot fire before
        systemd's SIGTERM is decoration, and three independent review passes
        on the v0.227.0 port converged on it.

        So this pins the COMPOSITION: the validator's bound covers the
        measurement with margin but is at most twice it (the review's second
        mutant — 600 back inside an 1800 unit — sat exactly on a `<= unit/3`
        boundary and passed), it is strictly under a third of the unit, and
        every inner `timeout N` the script executes, the validator included,
        sums to under half the unit so the unbounded work (pip, git, image
        sync, unit installs) has the other half. What this cannot see is the
        runtime — the remaining-budget guard, executed below, is the half
        that checks the clock rather than the text.
        """
        body = self._body()
        vm = re.search(r"^VALIDATOR_TIMEOUT_S=(\d+)\s*$", body, re.MULTILINE)
        assert vm, "update.sh must define VALIDATOR_TIMEOUT_S as a plain integer"
        validator_s = int(vm.group(1))
        stamp_at = STAMP_CALL.search(body).start()
        window = body[max(0, stamp_at - 400):stamp_at + 200]
        assert 'timeout "$VALIDATOR_TIMEOUT_S" "$PYTHON" tools/validate_measurement.py' in window, (
            "the validation call must be bounded by VALIDATOR_TIMEOUT_S — one value, used by "
            "both the bound and the remaining-budget guard"
        )

        unit = UPDATE_UNIT.read_text(encoding="utf-8")
        um = re.search(r"^TimeoutStartSec=(\d+)\s*$", unit, re.MULTILINE)
        assert um, "litclock-update.service must set an integer TimeoutStartSec"
        unit_s = int(um.group(1))

        executed = "\n".join(
            line for line in body.splitlines() if not line.lstrip().startswith("#")
        )
        inner = [int(n) for n in re.findall(r"\btimeout (\d+)\b", executed)] + [validator_s]
        assert MEASURED_VALIDATOR_S * VALIDATOR_MARGIN <= validator_s <= 2 * MEASURED_VALIDATOR_S, (
            f"validator bound {validator_s}s must cover the measured {MEASURED_VALIDATOR_S}s "
            f"with margin and stay under twice it"
        )
        assert validator_s < unit_s / 3, (
            f"validator bound {validator_s}s must be strictly under a third of the unit's {unit_s}s"
        )
        assert sum(inner) < unit_s / 2, (
            f"the script's inner bounds sum to {sum(inner)}s against a {unit_s}s unit; "
            "the unbounded work (pip, git, image sync, unit installs) needs the other half"
        )

    def test_it_is_skipped_in_rollback_mode(self):
        """litclock-dev#835. A bootcheck rollback exists to get the LKG clock
        painting again; the revoke block has usually just removed the marker,
        and re-earning it there costs minutes with the timer stopped."""
        body = self._body()
        stamp_at = STAMP_CALL.search(body).start()
        guard_at = body.rindex('if [[ ! -f "$RUNTIME_MARKER"', 0, stamp_at)
        guard = body[guard_at:body.index("\n", guard_at)]
        assert '"${ROLLBACK_MODE:-0}" -ne 1' in guard, guard

    def test_a_failed_validation_does_not_fail_the_update(self):
        """No marker means the pre-rendered tier, which is the safe side. A wrong
        fallback is impossible; a wrong render is not."""
        body = self._body()
        stamp_at = STAMP_CALL.search(body).start()
        window = body[stamp_at:stamp_at + 1200]
        assert "not an update failure" in window, (
            "the failure arm must say, on the code, that it does not fail the update"
        )
        assert "exit 1" not in window, "a failed validation must not exit the updater"


class TestUpdateShStampBlockExecutes:
    """The block itself, RUN — not just located. The placement tests above are
    source-order checks; this drives the lifted KEEP arm under the script's own
    `set -u` and proves the two things a source check cannot: a failing
    validation does not stop the updater, and a present marker skips the work.
    """

    def _keep_arm(self):
        body = UPDATE_SH.read_text()
        start = body.index('if [[ "$smoke_rc" -eq 0 ]]; then\n    log_info "Smoke test passed"')
        end = body.index('else\n    log_error "Smoke test failed', start)
        # Close the `if` we opened: the lifted text is the KEEP arm only.
        return body[start:end] + "fi\n"

    def _budget_helpers(self):
        """The litclock-dev#835 helpers the KEEP arm calls, lifted verbatim so the
        harness runs the guard the script runs — not a re-implementation."""
        body = UPDATE_SH.read_text()
        start = body.index("# --- litclock-dev#835 budget helpers BEGIN ---")
        end = body.index("# --- litclock-dev#835 budget helpers END ---", start)
        return body[start:end]

    def _run(
        self,
        tmp_path,
        *,
        marker_exists: bool,
        validator_rc: int,
        rollback_mode: bool = False,
        elapsed_s=None,
        installed_budget_s=None,
    ):
        """elapsed_s=None means "not under systemd" (the helper returns
        non-zero, the guard waives the budget); an int stubs the elapsed
        clock. installed_budget_s writes a fake INSTALLED unit file."""
        fake_py = tmp_path / "python3"
        fake_py.write_text(f"#!/bin/bash\necho FAKE_VALIDATOR_RAN >&2\nexit {validator_rc}\n")
        fake_py.chmod(0o755)
        marker = tmp_path / "marker"
        if marker_exists:
            marker.write_text("present")
        # elapsed_s: None = not under systemd (rc 2, waived); "unknown" = the
        # helper could not tell (rc 1, must defer); int = seconds.
        # installed_budget_s: None = the manager could not be read (must
        # defer); 0 = no limit; int = seconds.
        if elapsed_s is None:
            elapsed_stub = "_update_elapsed_seconds() { return 2; }\n"
        elif elapsed_s == "unknown":
            elapsed_stub = "_update_elapsed_seconds() { return 1; }\n"
        else:
            elapsed_stub = f"_update_elapsed_seconds() {{ echo {int(elapsed_s)}; }}\n"
        if installed_budget_s is None:
            elapsed_stub += "_update_budget_seconds() { return 1; }\n"
        else:
            elapsed_stub += f"_update_budget_seconds() {{ echo {int(installed_budget_s)}; }}\n"
        program = (
            "set -u\n"
            'log_info() { echo "[INFO] $1"; }\n'
            'log_error() { echo "[ERROR] $1"; }\n'
            'atomic_remove_file() { :; }\n'
            'atomic_write_file() { :; }\n'
            f"PYTHON={fake_py}\nRUNTIME_MARKER={marker}\nUPDATE_FAILED_FILE=/dev/null\n"
            f"POST_UPDATE_GRACE_FILE=/dev/null\nsmoke_rc=0\n"
            f"ROLLBACK_MODE={1 if rollback_mode else 0}\n"
            f"{self._budget_helpers()}"
            # Stub AFTER the lifted helpers so the stub wins; the guard itself
            # is the real one.
            f"{elapsed_stub}"
            f"{self._keep_arm()}"
            'echo REACHED_END\n'
        )
        return subprocess.run(["bash", "-c", program], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)

    def test_rollback_mode_skips_the_validation(self, tmp_path):
        """litclock-dev#835, executed: the source pin on the guard line is one
        substring; this proves the arm is actually not entered."""
        r = self._run(tmp_path, marker_exists=False, validator_rc=0, rollback_mode=True)
        assert "[validate] FAKE_VALIDATOR_RAN" not in r.stdout, r.stdout
        assert "REACHED_END" in r.stdout

    def test_the_validation_is_deferred_when_the_installed_budget_cannot_hold_it(self, tmp_path):
        """litclock-dev#835 (Codex): the first tick carrying a bigger
        TimeoutStartSec still runs under the OLD installed unit, because Phase
        5 copies the unit file after Phase 4.5. 400s gone of a 600s budget
        cannot hold a 300s validator plus margin — defer, log, continue."""
        r = self._run(tmp_path, marker_exists=False, validator_rc=0, elapsed_s=400, installed_budget_s=600)
        assert "[validate] FAKE_VALIDATOR_RAN" not in r.stdout, r.stdout
        assert "deferring runtime-render validation" in r.stdout, r.stdout
        assert "REACHED_END" in r.stdout

    def test_the_validation_runs_when_the_installed_budget_holds_it(self, tmp_path):
        r = self._run(tmp_path, marker_exists=False, validator_rc=0, elapsed_s=400, installed_budget_s=1800)
        assert "[validate] FAKE_VALIDATOR_RAN" in r.stdout, r.stdout
        assert "deferring" not in r.stdout

    def test_a_run_outside_systemd_has_no_budget_to_respect(self, tmp_path):
        """A manual `sudo ./scripts/update.sh` has no TimeoutStartSec; the
        helper returns non-zero and the guard waives the check."""
        r = self._run(tmp_path, marker_exists=False, validator_rc=0, elapsed_s=None, installed_budget_s=600)
        assert "[validate] FAKE_VALIDATOR_RAN" in r.stdout, r.stdout

    def test_an_unknown_budget_defers_rather_than_waives(self, tmp_path):
        """Round-2 review: every measurement failure used to WAIVE the budget.
        Unknown must read as "cannot afford it" — only a confirmed manual run
        (no INVOCATION_ID) has no budget to respect."""
        r = self._run(tmp_path, marker_exists=False, validator_rc=0, elapsed_s="unknown", installed_budget_s=1800)
        assert "cannot determine how much of this run's systemd budget is left" in r.stdout, r.stdout
        assert "[validate] FAKE_VALIDATOR_RAN" not in r.stdout
        r = self._run(tmp_path, marker_exists=False, validator_rc=0, elapsed_s=100, installed_budget_s=None)
        assert "cannot read the unit's effective TimeoutStartSec" in r.stdout, r.stdout
        assert "[validate] FAKE_VALIDATOR_RAN" not in r.stdout

    def test_an_unlimited_budget_runs_the_validation(self, tmp_path):
        r = self._run(tmp_path, marker_exists=False, validator_rc=0, elapsed_s=5000, installed_budget_s=0)
        assert "[validate] FAKE_VALIDATOR_RAN" in r.stdout, r.stdout



class TestBudgetHelpersExecute:
    """litclock-dev#835 round-2 review (Claude): the guard's two feeders —
    `_update_elapsed_seconds` and `_update_budget_seconds` — were stubbed in
    every executed test, so a one-token slip in either (the µs divisor, the
    property name) would defer the runtime tier fleet-wide with only a
    journal line to show. Seven such mutants survived. These run the REAL
    helpers against a fake `systemctl` on PATH."""

    def _run(self, tmp_path, *, invocation_id, owner, start_us, budget_us, systemctl_rc=0, busctl_rc=0):
        """A fake systemctl and a fake busctl that answer ONLY the exact
        queries the helpers are supposed to make (round-3 review: a
        substring-matching fake let a wrong unit or property pass)."""
        bindir = tmp_path / "bin"
        bindir.mkdir()
        fake = bindir / "systemctl"
        fake.write_text(
            "#!/bin/bash\n"
            f"[ {systemctl_rc} -ne 0 ] && exit {systemctl_rc}\n"
            'case "$*" in\n'
            f'  "show litclock-update.service -p InvocationID --value") echo "{owner}" ;;\n'
            f'  "show litclock-update.service -p ExecMainStartTimestampMonotonic --value") echo "{start_us}" ;;\n'
            "  *) exit 1 ;;\n"
            "esac\n"
        )
        fake.chmod(0o755)
        busctl = bindir / "busctl"
        busctl.write_text(
            "#!/bin/bash\n"
            f"[ {busctl_rc} -ne 0 ] && exit {busctl_rc}\n"
            'case "$*" in\n'
            '  "get-property org.freedesktop.systemd1 /org/freedesktop/systemd1/unit/litclock_2dupdate_2eservice '
            f'org.freedesktop.systemd1.Service TimeoutStartUSec") echo "{budget_us}" ;;\n'
            "  *) exit 1 ;;\n"
            "esac\n"
        )
        busctl.chmod(0o755)
        body = UPDATE_SH.read_text()
        start = body.index("# --- litclock-dev#835 budget helpers BEGIN ---")
        end = body.index("# --- litclock-dev#835 budget helpers END ---", start)
        program = (
            "set -u\n"
            f"export PATH={fake.parent}:$PATH\n"
            + (f"export INVOCATION_ID={invocation_id}\n" if invocation_id is not None else "unset INVOCATION_ID\n")
            + 'log_info() { echo "[INFO] $1"; }\n'
            + body[start:end]
            + 'e=$(_update_elapsed_seconds); echo "elapsed_rc=$? elapsed=$e"\n'
            + 'b=$(_update_budget_seconds); echo "budget_rc=$? budget=$b"\n'
        )
        r = subprocess.run(["bash", "-c", program], capture_output=True, text=True, timeout=30)
        return dict(kv.split("=", 1) for line in r.stdout.splitlines() for kv in line.split() if "=" in kv)

    def _uptime_s(self):
        return int(float(Path("/proc/uptime").read_text().split()[0]))

    def test_the_real_helpers_measure_this_units_own_run(self, tmp_path):
        start_us = (self._uptime_s() - 100) * 1_000_000
        out = self._run(tmp_path, invocation_id="abc", owner="abc", start_us=start_us, budget_us="t 1800000000")
        assert out["elapsed_rc"] == "0" and 99 <= int(out["elapsed"]) <= 103, out
        assert out["budget_rc"] == "0" and out["budget"] == "1800", out

    def test_a_manual_run_is_waived_not_unknown(self, tmp_path):
        out = self._run(tmp_path, invocation_id=None, owner="abc", start_us=1, budget_us="t 1800000000")
        assert out["elapsed_rc"] == "2", out

    @pytest.mark.parametrize(
        "kw",
        [
            dict(invocation_id="abc", owner="other", start_us=1_000_000, budget_us="t 1800000000"),  # not ours
            dict(invocation_id="abc", owner="abc", start_us=0, budget_us="t 1800000000"),            # zero stamp
            dict(invocation_id="abc", owner="abc", start_us="", budget_us="t 1800000000"),           # empty
            dict(invocation_id="abc", owner="abc", start_us="abc", budget_us="t 1800000000"),        # garbage
            dict(invocation_id="abc", owner="abc", start_us=1_000_000, budget_us="t 1800000000", systemctl_rc=1),
        ],
    )
    def test_anything_the_helpers_cannot_vouch_for_is_unknown(self, tmp_path, kw):
        out = self._run(tmp_path, **kw)
        assert out["elapsed_rc"] == "1", (kw, out)

    @pytest.mark.parametrize(
        "raw,want",
        [
            ("t 600000000", "600"),  # the bench's freshly flashed unit, 2026-09-13
            ("t 1800000000", "1800"),
            ("t 18446744073709551615", "0"),  # infinity, as busctl prints it
            ("t 9223372036854775808", "0"),  # 2^63: 19 digits, above bash's signed range — digit count, not arithmetic
            ("t 9999999999999999999", "0"),
            ("t 1000000000000000000", "0"),  # the intentional cutoff: 19 digits (>= 10^18 µs, ~31,700 yr) = unlimited
            ("t 999999999999999999", "999999999999"),  # 18 digits stays finite
            ("t 500000", "0"),  # a finite sub-second budget rounds to 0 — see the guard's comment
        ],
    )
    def test_the_budget_is_the_raw_dbus_property(self, tmp_path, raw, want):
        out = self._run(tmp_path, invocation_id="abc", owner="abc", start_us=1_000_000, budget_us=raw)
        assert out["budget_rc"] == "0" and out["budget"] == want, out

    @pytest.mark.parametrize(
        "kw", [dict(budget_us="10min"), dict(budget_us="t"), dict(budget_us="t 1800000000", busctl_rc=1)]
    )
    def test_an_unreadable_budget_is_unknown(self, tmp_path, kw):
        out = self._run(tmp_path, invocation_id="abc", owner="abc", start_us=1_000_000, **kw)
        assert out["budget_rc"] == "1", out

