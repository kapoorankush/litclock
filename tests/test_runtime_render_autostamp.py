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

import ast
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"
UPDATE_UNIT = REPO_ROOT / "systemd" / "litclock-update.service"
# litclock-dev#835: `validate_measurement.py check --stamp` wall-clock on the
# bench Pi Zero 2W, 2026-09-12, fresh venv, idle (x86: ~21s). Re-measure and
# update HERE; the prose copies in update.sh and the unit point at this pin.
MEASURED_VALIDATOR_S = 182
VALIDATOR_MARGIN = 1.4
PIGEN_APP = REPO_ROOT / "pi-gen" / "stage3" / "01-setup-app" / "00-run.sh"
BUILD_IMAGE_YML = REPO_ROOT / ".github" / "workflows" / "build-image.yml"
# litclock-dev#840: the same check inside pi-gen's arm64 chroot under qemu on
# a GitHub runner -- 2026-09-13 master build, job 103756148558, "Validating
# GD-exact measurement" 17:36:20 to PASSED 17:39:19 (176s reported by the
# check itself). One sample; re-measure from a build log and update HERE.
MEASURED_CHROOT_VALIDATOR_S = 179
VALIDATOR = REPO_ROOT / "tools" / "validate_measurement.py"
DUMP = REPO_ROOT / "tools" / "gd-expected-measurements.json.gz"

# Anchored on an INTERPRETER PREFIX so only the EXECUTED call matches. Without
# it, `.search()` returned the first of three prose mentions in update.sh — a
# comment at :813 and two log strings in the revoke block — and every position
# assertion below compared against the wrong offset. Found by running the
# tests; the same trap litclock-dev#782 catalogued and this session has hit twice.
STAMP_CALL = re.compile(r'(?:"\$PYTHON"|\./venv/bin/python3)\s+tools/validate_measurement\.py\s+check\s+--stamp')

# The self-test arm's two bounds, NAMED because more than one thing computes
# against them (litclock-dev#883): `_run`'s default and
# `test_the_boundary_is_exact`'s arithmetic. They were bare literals in both,
# linked only by a comment — so the boundary test could silently stop tracking
# the default it claims to follow. Only the RESERVE mirrors `scripts/update.sh`;
# the 2s timeout is a harness choice that keeps the rc-124 test fast, and
# production ships 60s (update.sh:1724).
SELFTEST_TIMEOUT_DEFAULT_S = 2
SELFTEST_BUDGET_RESERVE_S = 120


def render_lead_s():
    """The render lead, the sanity bound an honestly recorded `duration_s` is
    checked against (NOT a Stage B gate: Stage B reads `result` and `sha`), read from the
    source of truth so a change to the lead moves the test with it (litclock-dev#883).

    Read by AST, not by regex, and called from the test rather than at import.
    A regex over the assignment was the first version and it is wrong three
    ways the review found by trying them: `RENDER_LEAD_DEFAULT_S: float = 4.0`
    and `= (4.0)` both report the constant "gone", `= 40e-1` reads FORTY, and
    an import-time assert on any of those stops the whole FILE collecting —
    including the pi-gen tests, which have nothing to do with this. `ast`
    handles the annotation and the parens, `literal_eval` handles the exponent,
    and a computed value raises here instead of being silently misread.
    `src/literary_clock.py` is still not IMPORTED: these are bash-harness tests
    and pulling PIL in for one float would be the heaviest import in the module.
    """
    tree = ast.parse((REPO_ROOT / "src" / "literary_clock.py").read_text())
    for node in tree.body:
        targets = [node.target] if isinstance(node, ast.AnnAssign) else getattr(node, "targets", [])
        for t in targets:
            if isinstance(t, ast.Name) and t.id == "RENDER_LEAD_DEFAULT_S":
                return float(ast.literal_eval(node.value))
    raise AssertionError(
        "RENDER_LEAD_DEFAULT_S is gone from src/literary_clock.py — "
        "litclock-dev#883's gate-boundary test has no boundary to sit above"
    )


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
        out = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(DUMP.relative_to(REPO_ROOT))],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
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
        window = body[max(0, stamp_at - 400) : stamp_at + 200]
        assert 'timeout "$VALIDATOR_TIMEOUT_S" "$PYTHON" tools/validate_measurement.py' in window, (
            "the validation call must be bounded by VALIDATOR_TIMEOUT_S — one value, used by "
            "both the bound and the remaining-budget guard"
        )

        unit = UPDATE_UNIT.read_text(encoding="utf-8")
        um = re.search(r"^TimeoutStartSec=(\d+)\s*$", unit, re.MULTILINE)
        assert um, "litclock-update.service must set an integer TimeoutStartSec"
        unit_s = int(um.group(1))

        executed = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
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
        guard = body[guard_at : body.index("\n", guard_at)]
        assert '"${ROLLBACK_MODE:-0}" -ne 1' in guard, guard

    def test_a_failed_validation_does_not_fail_the_update(self):
        """No marker means the pre-rendered tier, which is the safe side. A wrong
        fallback is impossible; a wrong render is not."""
        body = self._body()
        stamp_at = STAMP_CALL.search(body).start()
        window = body[stamp_at : stamp_at + 1200]
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
        stamps: bool = False,
        memo_exists: bool = False,
    ):
        """elapsed_s=None means "not under systemd" (the helper returns
        non-zero, the guard waives the budget); an int stubs the elapsed
        clock. installed_budget_s writes a fake INSTALLED unit file."""
        fake_py = tmp_path / "python3"
        # `stamps` makes the fake validator do what the real one does on a pass:
        # write the marker (its last argument). Without it rc=0 is a validator
        # that reported success and stamped nothing, which the arm rightly
        # treats as "did not pass".
        stamp_line = '[ "$STAMPS" = 1 ] && : > "${@: -1}"\n'
        fake_py.write_text(f"#!/bin/bash\necho FAKE_VALIDATOR_RAN >&2\n{stamp_line}exit {validator_rc}\n")
        fake_py.chmod(0o755)
        marker = tmp_path / "marker"
        if marker_exists:
            marker.write_text("present")
        memo = tmp_path / "memo.json"
        if memo_exists:
            memo.write_text('{"result": "failed", "rc": 1, "reason": "old", "sha": null, "at_unix": 1}')
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
            'log_warn() { echo "[WARN] $1"; }\n'
            'log_error() { echo "[ERROR] $1"; }\n'
            # Behaving stubs (litclock-dev#847 item 1): the memo tests read the
            # file the arm writes. /dev/null stands in for the markers this
            # harness does not care about, so those two paths stay inert.
            'atomic_remove_file() { [ "$1" = /dev/null ] || rm -f "$1"; }\n'
            'atomic_write_file() { [ "$1" = /dev/null ] || printf "%s" "$2" > "$1"; }\n'
            f"export STAMPS={1 if stamps else 0}\n"
            f"PYTHON={fake_py}\nRUNTIME_MARKER={marker}\nUPDATE_FAILED_FILE=/dev/null\n"
            f"RUNTIME_VALIDATION_MEMO_FILE={memo}\n"
            f"POST_UPDATE_GRACE_FILE=/dev/null\nsmoke_rc=0\n"
            f"ROLLBACK_MODE={1 if rollback_mode else 0}\n"
            # litclock-dev#871 Stage A: the KEEP arm now calls the self-test after
            # the stamp block. Stubbed DISTINGUISHABLY (this harness has no
            # `set -e`, so an unstubbed call would print `command not found` and
            # carry on green); the real function is driven on its own in
            # TestRuntimeRenderSelftestExecutes below.
            "_runtime_render_selftest() { echo STUB_SELFTEST; }\n"
            # ...and Stage B's migration, called straight after it. It was left
            # unstubbed and printed `command not found` on every marker-present
            # run while the tests stayed green (litclock-dev#871 port /review).
            "_runtime_render_migrate() { echo STUB_MIGRATE; }\n"
            f"{self._budget_helpers()}"
            # Stub AFTER the lifted helpers so the stub wins; the guard itself
            # is the real one.
            f"{elapsed_stub}"
            f"{self._keep_arm()}"
            "echo REACHED_END\n"
        )
        return subprocess.run(["bash", "-c", program], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)

    @staticmethod
    def _memo(tmp_path):
        path = tmp_path / "memo.json"
        return json.loads(path.read_text()) if path.exists() else None

    def test_the_migration_runs_after_the_selftest_only_with_a_marker_and_never_in_rollback(self, tmp_path):
        """Executed, not positional: the flip is gated exactly like the self-test."""
        # One directory per run: the marker is a file in tmp_path, so a shared
        # one carries the first run's marker into the "no marker" run.
        dirs = [tmp_path / n for n in ("present", "absent", "rollback")]
        for d in dirs:
            d.mkdir()
        present = self._run(dirs[0], marker_exists=True, validator_rc=0)
        out = present.stdout
        assert "STUB_SELFTEST" in out and "STUB_MIGRATE" in out, out + present.stderr
        assert out.index("STUB_SELFTEST") < out.index("STUB_MIGRATE"), "the flip must follow the verdict"
        absent = self._run(dirs[1], marker_exists=False, validator_rc=1)
        assert "STUB_MIGRATE" not in absent.stdout, "no marker, nothing to migrate to"
        rollback = self._run(dirs[2], marker_exists=True, validator_rc=0, rollback_mode=True)
        assert "STUB_MIGRATE" not in rollback.stdout, "a rollback must never flip the flag"
        for r in (present, absent, rollback):
            assert "command not found" not in r.stderr, f"an unstubbed call in the KEEP arm:\n{r.stderr}"
            assert "REACHED_END" in r.stdout

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

    @pytest.mark.parametrize("slack,runs", [(0, True), (-1, False)], ids=["exactly-fits", "one-second-short"])
    def test_the_budget_boundary_is_exact(self, tmp_path, slack, runs):
        """`elapsed + timeout + reserve > budget` defers. The two cases either
        side of that line are derived from the script's OWN constants, so a
        `>=`, or a constant dropped from the sum, goes red here rather than
        surviving the 400/600 and 400/1800 cases above (v0.227.0 port review,
        testing pass)."""
        helpers = self._budget_helpers()
        timeout_s = int(re.search(r"^VALIDATOR_TIMEOUT_S=(\d+)", helpers, re.M).group(1))
        reserve_s = int(re.search(r"^VALIDATOR_BUDGET_RESERVE_S=(\d+)", helpers, re.M).group(1))
        # litclock-dev#871 (litclock-dev#875 red team): the validator's guard does NOT
        # reserve for the self-test that follows it — on the transition tick
        # that would defer earning the marker to protect an inert probe. The
        # self-test's own guard is driven in TestRuntimeRenderSelftestExecutes.
        budget = 600
        elapsed = budget - timeout_s - reserve_s - slack
        r = self._run(tmp_path, marker_exists=False, validator_rc=0, elapsed_s=elapsed, installed_budget_s=budget)
        assert ("[validate] FAKE_VALIDATOR_RAN" in r.stdout) is runs, f"elapsed={elapsed} budget={budget}\n{r.stdout}"
        assert ("deferring runtime-render validation" in r.stdout) is (not runs)

    def test_a_run_outside_systemd_has_no_budget_to_respect(self, tmp_path):
        """A manual `sudo ./scripts/update.sh` has no TimeoutStartSec; the
        helper returns non-zero and the guard waives the check."""
        r = self._run(tmp_path, marker_exists=False, validator_rc=0, elapsed_s=None, installed_budget_s=600)
        assert "[validate] FAKE_VALIDATOR_RAN" in r.stdout, r.stdout

    def test_an_unknown_budget_defers_rather_than_waives(self, tmp_path):
        """litclock-dev#835 review: every measurement failure used to WAIVE the budget.
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

    def test_a_failing_validation_lets_the_update_continue(self, tmp_path):
        r = self._run(tmp_path, marker_exists=False, validator_rc=1)
        # The block pipes the validator through `sed 's/^/[validate] /'` with
        # 2>&1, so its stderr lands in STDOUT, prefixed. Look there.
        assert "[validate] FAKE_VALIDATOR_RAN" in r.stdout, (
            "the validator must actually be invoked when the marker is absent"
        )
        assert "REACHED_END" in r.stdout, f"a failed validation stopped the updater\n{r.stdout}\n{r.stderr}"
        # The failure arm must be REACHED — the first version of the block tested
        # the pipeline's exit (sed's, always 0) and logged "reported success" for
        # a validator that exited 1. This executed test is what caught it.
        assert "did not pass" in r.stdout and "rc=1" in r.stdout, (
            f"a failing validator must take the failure arm with its real rc\n{r.stdout}"
        )
        assert "not an update failure" in r.stdout
        assert r.returncode == 0

    def test_a_present_marker_skips_the_validation(self, tmp_path):
        r = self._run(tmp_path, marker_exists=True, validator_rc=1)
        assert "FAKE_VALIDATOR_RAN" not in r.stdout, "a present marker must skip the (expensive) check"
        assert "REACHED_END" in r.stdout

    def test_the_arm_survives_set_u(self, tmp_path):
        """The exact way it first broke: the harness in test_update_sh.py runs
        under `set -u`, and an unseeded RUNTIME_MARKER killed bash at the
        guard. Every variable the block reads must be one the script defines."""
        r = self._run(tmp_path, marker_exists=False, validator_rc=0)
        assert "unbound variable" not in r.stderr, r.stderr
        assert "REACHED_END" in r.stdout

    def test_the_failure_arm_removes_the_validators_litter_and_nothing_else(self, tmp_path):
        """litclock-dev#835 port review (Codex): the litter glob was six `?`,
        but Python's mkstemp appends EIGHT characters, so a validator killed
        by `timeout` left its temp file behind on every failure — and the
        six-wide glob DID match `<marker>.backup`, which a sibling file with
        that name would have paid for. This creates the litter with the real
        tempfile call the validator uses, so the glob is pinned to the
        writer's scheme rather than to a count someone read off a docstring."""
        marker = tmp_path / "marker"
        fd, litter = tempfile.mkstemp(dir=str(tmp_path), prefix=marker.name + ".")
        os.close(fd)
        keep = [tmp_path / "marker.backup", tmp_path / "marker.bak", tmp_path / "marker.old.json"]
        for k in keep:
            k.write_text("keep me")
        r = self._run(tmp_path, marker_exists=False, validator_rc=1)
        assert "did not pass" in r.stdout, r.stdout
        assert not Path(litter).exists(), f"mkstemp litter {Path(litter).name} survived the failure arm"
        for k in keep:
            assert k.exists(), f"{k.name} is not validator litter and must not be removed"

    # ── litclock-dev#847 item 1: the negative-result memo ───────────────────

    def test_a_deferral_is_memoed_with_its_reason(self, tmp_path):
        """A device that never has budget never validates; the memo is the only
        durable trace, and it carries the same reason the journal line does."""
        r = self._run(tmp_path, marker_exists=False, validator_rc=0, elapsed_s=400, installed_budget_s=600)
        assert "deferring runtime-render validation" in r.stdout, r.stdout
        memo = self._memo(tmp_path)
        assert memo is not None, f"the deferral was not persisted\n{r.stdout}\n{r.stderr}"
        assert memo["result"] == "deferred"
        assert memo["rc"] is None, "nothing ran, so there is no rc"
        assert "budget" in memo["reason"] and "400s" in memo["reason"], memo
        assert isinstance(memo["at_unix"], int) and memo["at_unix"] > 1_600_000_000, memo
        assert re.fullmatch(r"[0-9a-f]{40}", memo["sha"] or ""), memo

    @pytest.mark.parametrize(
        "kw",
        [
            {"elapsed_s": "unknown", "installed_budget_s": 1800},
            {"elapsed_s": 100, "installed_budget_s": None},
        ],
        ids=["unknown-elapsed", "unknown-budget"],
    )
    def test_every_deferral_branch_writes_the_memo(self, tmp_path, kw):
        r = self._run(tmp_path, marker_exists=False, validator_rc=0, **kw)
        assert "deferring" in r.stdout, r.stdout
        memo = self._memo(tmp_path)
        assert memo is not None and memo["result"] == "deferred", (memo, r.stdout)

    def test_a_failed_validation_is_memoed_with_its_rc(self, tmp_path):
        r = self._run(tmp_path, marker_exists=False, validator_rc=1)
        assert "did not pass" in r.stdout, r.stdout
        memo = self._memo(tmp_path)
        assert memo is not None, f"the failure was not persisted\n{r.stdout}\n{r.stderr}"
        assert memo["result"] == "failed" and memo["rc"] == 1, memo
        assert "exited 1" in memo["reason"], memo

    def test_a_timed_out_validation_is_told_apart_from_a_verdict(self, tmp_path):
        """124 is coreutils timeout's own status. A timeout may pass next week
        on an idle device; a verdict will not, and the operator should be able
        to tell which they are looking at."""
        r = self._run(tmp_path, marker_exists=False, validator_rc=124)
        memo = self._memo(tmp_path)
        assert memo is not None and memo["result"] == "timeout" and memo["rc"] == 124, (memo, r.stdout)
        assert "did not finish" in memo["reason"], memo

    def test_a_pass_clears_an_earlier_memo(self, tmp_path):
        r = self._run(tmp_path, marker_exists=False, validator_rc=0, stamps=True, memo_exists=True)
        assert "validation PASSED" in r.stdout, (r.stdout, r.stderr)
        assert self._memo(tmp_path) is None, "a pass must remove the negative memo"

    def test_rollback_mode_keeps_a_stale_memo_because_nothing_re_asks(self, tmp_path):
        """litclock-dev#875 red team: a rollback tick skips the self-test, so clearing a
        `selftest-failed` memo there would turn 'failed' into 'no record'."""
        r = self._run(tmp_path, marker_exists=True, validator_rc=0, rollback_mode=True, memo_exists=True)
        assert "REACHED_END" in r.stdout
        assert self._memo(tmp_path) is not None, "the memo must survive a rollback tick"

    def test_a_present_marker_clears_a_stale_memo_without_running_anything(self, tmp_path):
        """Hand-stamped since, or stamped by a later tick: the marker wins, and
        a memo left behind would have the PWA report a failure it contradicts."""
        r = self._run(tmp_path, marker_exists=True, validator_rc=1, memo_exists=True)
        assert "FAKE_VALIDATOR_RAN" not in r.stdout
        assert "REACHED_END" in r.stdout
        assert self._memo(tmp_path) is None, "a present marker must supersede the memo"

    def test_rollback_mode_writes_no_memo(self, tmp_path):
        """A rollback neither runs nor defers THIS device's validation — the
        next normal update does — so it must not leave a memo claiming either."""
        self._run(tmp_path, marker_exists=False, validator_rc=0, rollback_mode=True)
        assert self._memo(tmp_path) is None

    def test_the_memo_is_the_writer_helper_not_an_inline_write(self, tmp_path):
        """Structural: every result goes through _runtime_validation_memo_write,
        which is the one place the JSON shape lives (the reader in
        control_server pins that shape from the other side)."""
        arm = "\n".join(ln for ln in self._keep_arm().splitlines() if not ln.lstrip().startswith("#"))
        assert arm.count("_runtime_validation_memo_write ") == 2, arm
        assert 'atomic_remove_file "$RUNTIME_VALIDATION_MEMO_FILE"' in arm
        assert "RUNTIME_VALIDATION_MEMO_FILE" not in arm.replace(
            'atomic_remove_file "$RUNTIME_VALIDATION_MEMO_FILE"', ""
        ), "the arm must not write the memo file directly"

    def test_the_memo_is_left_readable_by_the_control_server(self, tmp_path):
        """litclock-dev#854 review — atomic_write_file stages with mktemp (0600, owned by
        whoever runs the script), so a maintainer's `sudo ./scripts/update.sh`
        left a 0600 root:root memo the pi-user control_server could not open
        and /api/status silently reported null. The writer now chmods 0644
        (best-effort chown too, unobservable here). Driven through the REAL
        writer with a 0600-creating stub so the fix is what is measured."""
        memo = tmp_path / "memo.json"
        body = UPDATE_SH.read_text()
        start = body.index("# --- litclock-dev#835 budget helpers BEGIN ---")
        end = body.index("# --- litclock-dev#835 budget helpers END ---", start)
        program = (
            "set -u\n"
            'log_info() { :; }\nlog_warn() { echo "[WARN] $1"; }\n'
            # mktemp's own mode, which is the shape the real atomic_write_file
            # leaves behind: if the chmod is dropped, this is what survives.
            'atomic_write_file() { printf "%s" "$2" > "$1"; chmod 0600 "$1"; }\n'
            f"RUNTIME_VALIDATION_MEMO_FILE={memo}\n"
            + body[start:end]
            + '_runtime_validation_memo_write failed 1 "exited 1"\n'
        )
        r = subprocess.run(["bash", "-c", program], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        assert memo.exists(), (r.stdout, r.stderr)
        mode = memo.stat().st_mode & 0o777
        assert mode == 0o644, (
            f"the memo is mode {mode:o}; a root-run update would leave it unreadable to the "
            "pi-user control_server and the API would silently report null"
        )


class TestBudgetHelpersExecute:
    """litclock-dev#835 review: the guard's two feeders —
    `_update_elapsed_seconds` and `_update_budget_seconds` — were stubbed in
    every executed test, so a one-token slip in either (the µs divisor, the
    property name) would defer the runtime tier fleet-wide with only a
    journal line to show. Seven such mutants survived. These run the REAL
    helpers against a fake `systemctl` on PATH."""

    def _run(self, tmp_path, *, invocation_id, owner, start_us, budget_us, systemctl_rc=0, busctl_rc=0):
        """A fake systemctl and a fake busctl that answer ONLY the exact
        queries the helpers are supposed to make (the review found: a
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
            dict(invocation_id="abc", owner="abc", start_us=0, budget_us="t 1800000000"),  # zero stamp
            dict(invocation_id="abc", owner="abc", start_us="", budget_us="t 1800000000"),  # empty
            dict(invocation_id="abc", owner="abc", start_us="abc", budget_us="t 1800000000"),  # garbage
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


class TestPiGenStampsInTheChroot:
    def test_it_runs_after_pip_in_the_chroot(self):
        """The wheel under test must be the device's, not the build host's."""
        body = PIGEN_APP.read_text()
        pip_at = body.index("./venv/bin/pip install --upgrade -r /tmp/requirements-pigen.txt")
        stamp_at = STAMP_CALL.search(body).start()
        assert pip_at < stamp_at, "the stamp must run after the chroot's pip install"

    def test_it_uses_the_venv_interpreter(self):
        """`python3` alone would be the chroot's system interpreter, whose
        freetype-py is not the one the clock runs."""
        body = PIGEN_APP.read_text()
        line = next(ln for ln in body.splitlines() if STAMP_CALL.search(ln))
        assert "./venv/bin/python3" in line, f"must use the venv interpreter, got: {line.strip()}"

    def test_it_precedes_the_ownership_fix(self):
        """The marker is written as root; `chown -R pi:pi` must come after or the
        pi user owns a directory containing a root-owned marker."""
        body = PIGEN_APP.read_text()
        stamp_at = STAMP_CALL.search(body).start()
        chown_at = body.index("chown -R pi:pi /home/pi/litclock")
        assert stamp_at < chown_at, "the stamp must precede the chown that fixes its ownership"

    def test_the_stamp_is_bounded_against_the_measurement_and_the_job(self):
        """litclock-dev#840. Unbounded, a validator that hangs under qemu is
        killed by the job's `timeout-minutes`, which fails the image build
        instead of degrading to "no marker". The bound must cover the measured
        chroot run with real margin -- qemu on a shared runner is the least
        stable clock here -- and still be a small fraction of the job, so a
        hung check costs the build minutes rather than the build."""
        body = PIGEN_APP.read_text()
        vm = re.search(r"^PIGEN_VALIDATOR_TIMEOUT_S=(\d+)\s*$", body, re.MULTILINE)
        assert vm, "00-run.sh must define PIGEN_VALIDATOR_TIMEOUT_S as a plain integer"
        bound_s = int(vm.group(1))
        gm = re.search(r"^PIGEN_VALIDATOR_KILL_GRACE_S=(\d+)\s*$", body, re.MULTILINE)
        assert gm, "00-run.sh must define PIGEN_VALIDATOR_KILL_GRACE_S as a plain integer"
        grace_s = int(gm.group(1))
        assert grace_s > 0, (
            "a zero kill grace DISABLES the kill: `timeout -k 0` waits for the child forever and "
            "still reports 124 (measured), which is the unbounded behaviour this bound exists to "
            "remove, spelled differently"
        )
        line = next(ln for ln in body.splitlines() if STAMP_CALL.search(ln))
        assert 'timeout -k "$PIGEN_VALIDATOR_KILL_GRACE_S" "$PIGEN_VALIDATOR_TIMEOUT_S" ./venv/bin/python3' in line, (
            "the chroot validation call must be bounded by PIGEN_VALIDATOR_TIMEOUT_S WITH a -k kill "
            f"grace — timeout sends one SIGTERM and otherwise waits forever; got: {line.strip()}"
        )

        job_min = int(yaml.safe_load(BUILD_IMAGE_YML.read_text())["jobs"]["build"]["timeout-minutes"])

        assert bound_s >= 2 * MEASURED_CHROOT_VALIDATOR_S, (
            f"bound {bound_s}s must cover the measured {MEASURED_CHROOT_VALIDATOR_S}s chroot run "
            "at least twice over — qemu on a shared runner varies"
        )
        assert bound_s <= job_min * 60 / 6, (
            f"bound {bound_s}s is more than a sixth of the {job_min}-minute job; a hung "
            "validator would eat the build budget it exists to protect"
        )
        assert grace_s <= bound_s / 10, (
            f"a {grace_s}s kill grace against a {bound_s}s bound is not a grace — the validator "
            "has no cleanup worth waiting for"
        )


class TestPiGenStampBlockExecutes:
    """The chroot block itself, RUN under `set -e` with a fake venv interpreter
    (litclock-dev#840). A source check cannot see the exit status the else arm
    reads, nor whether an annotation actually reaches stdout -- and the
    PIPESTATUS trap (`if timeout … | sed …; then` tests sed) is invisible to
    a grep, which is how update.sh shipped it once.
    """

    @staticmethod
    def _block(timeout_s=None):
        body = PIGEN_APP.read_text()
        start = re.search(r"^PIGEN_VALIDATOR_TIMEOUT_S=\d+$", body, re.MULTILINE).start()
        end = body.index("# Clean pip cache", start)
        block = body[start:end]
        if timeout_s is not None:
            block, n = re.subn(
                r"^PIGEN_VALIDATOR_TIMEOUT_S=\d+$", f"PIGEN_VALIDATOR_TIMEOUT_S={timeout_s}", block, flags=re.MULTILINE
            )
            assert n == 1, "PIGEN_VALIDATOR_TIMEOUT_S must be one plain assignment the test can override"
            # The grace is SCALED, not replaced: `timeout -k 0` disables the
            # kill entirely (measured — it then waits for the child forever and
            # still reports 124), so overriding the grace to a positive value
            # would hide exactly the mutant these tests exist to catch. min()
            # keeps a zero zero and makes a real 30s grace test-sized.
            grace_re = r"^PIGEN_VALIDATOR_KILL_GRACE_S=(\d+)$"
            grace = min(int(re.search(grace_re, block, re.MULTILINE).group(1)), timeout_s)
            block, n = re.subn(grace_re, f"PIGEN_VALIDATOR_KILL_GRACE_S={grace}", block, flags=re.MULTILINE)
            assert n == 1, "PIGEN_VALIDATOR_KILL_GRACE_S must be one plain assignment the test can scale"
        return block

    # Exactly GitHub's grammar: `::warning title=<title>::<message>`. A title
    # with a comma is cut at the comma (properties are comma-separated), and a
    # leading space or an empty message drops the annotation silently.
    ANNOTATION = re.compile(r"::warning title=[^,:\r\n]+::\S.*")

    def _run(self, tmp_path, *, rc=0, sleep_s=0, timeout_s=None, ignore_term=False, stamp_late=False):
        (tmp_path / "venv" / "bin").mkdir(parents=True)
        (tmp_path / "tools").mkdir()
        (tmp_path / "tools" / "validate_measurement.py").write_text("")
        fake = tmp_path / "venv" / "bin" / "python3"
        fake.write_text(
            "#!/bin/bash\n"
            + ("trap '' TERM\n" if ignore_term else "")
            # A validator that outlives the TERM and then finishes stamps
            # AFTER the bound has expired -- the marker must not survive.
            + ("trap 'touch .runtime-render-validated; exit 0' TERM\n" if stamp_late else "")
            + f"sleep {sleep_s}\n"
            + f"[ {rc} -eq 0 ] && touch .runtime-render-validated\n"
            + f"exit {rc}\n"
        )
        fake.chmod(0o755)
        # Litter a killed validator leaves, made by the real call that makes it
        # (Python's mkstemp: prefix plus eight characters), plus a decoy that
        # shares the prefix but not the shape.
        fd, self.litter = tempfile.mkstemp(dir=tmp_path, prefix=".runtime-render-validated.")
        os.close(fd)
        (tmp_path / ".runtime-render-validated.backup").write_text("decoy")
        try:
            proc = subprocess.run(
                ["bash", "-e", "-c", self._block(timeout_s)],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except subprocess.TimeoutExpired:
            pytest.fail("the block did not return within 20s: the bound is not enforced against this interpreter")
        return proc

    def _annotations(self, proc):
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("::")]
        for ln in lines:
            assert self.ANNOTATION.fullmatch(ln), f"not a well-formed GitHub annotation: {ln!r}"
        return lines

    def test_a_passing_check_stamps_and_annotates_nothing(self, tmp_path):
        proc = self._run(tmp_path, rc=0)
        assert proc.returncode == 0, proc.stderr
        assert "validation PASSED" in proc.stdout, proc.stdout
        assert self._annotations(proc) == [], "a green build must not carry a warning annotation"
        assert (tmp_path / ".runtime-render-validated").exists()

    def test_a_failing_check_annotates_with_its_exit_status_and_does_not_fail_the_build(self, tmp_path):
        proc = self._run(tmp_path, rc=3)
        assert proc.returncode == 0, f"a failed validation must not fail the image build\n{proc.stderr}"
        warnings = self._annotations(proc)
        assert len(warnings) == 1, proc.stdout
        assert "exited 3" in warnings[0], warnings[0]
        assert "WITHOUT the runtime-render marker" in warnings[0]
        assert "FAILED in the build chroot (rc=3)" in proc.stdout, proc.stdout
        assert "usable" in proc.stdout, "the log must say the image still works on the pre-rendered tier"
        assert "TIMED OUT" not in proc.stdout
        assert not (tmp_path / ".runtime-render-validated").exists()
        assert not Path(self.litter).exists(), "mkstemp litter must be removed"
        assert (tmp_path / ".runtime-render-validated.backup").exists(), "the glob must not reach a .backup sibling"

    def test_a_hung_check_is_killed_annotated_and_does_not_fail_the_build(self, tmp_path):
        proc = self._run(tmp_path, rc=0, sleep_s=20, timeout_s=1)
        assert proc.returncode == 0, f"a timed-out validation must not fail the image build\n{proc.stderr}"
        warnings = self._annotations(proc)
        assert len(warnings) == 1, proc.stdout
        assert "timed out" in warnings[0] and "exceeded 1s" in warnings[0], warnings[0]
        assert "(rc=124)" in warnings[0], f"a child that dies to the TERM is coreutils' 124: {warnings[0]}"
        assert "TIMED OUT after 1s" in proc.stdout, proc.stdout
        assert "validation PASSED" not in proc.stdout, "the else arm must see timeout's 124, not the fake's 0"
        assert not (tmp_path / ".runtime-render-validated").exists()

    def test_a_check_that_ignores_sigterm_is_still_killed_within_the_grace(self, tmp_path):
        """PR litclock-dev#852 review (Codex P1, Claude F1). coreutils `timeout` sends ONE
        SIGTERM and then waits for the child forever; a validator wedged under
        qemu-user is exactly the child that does not act on it. Without `-k`
        this block ran to the job's 180-minute kill.

        And the escalation does NOT come back as 124: coreutils reports 128+9 =
        137 when it has to SIGKILL. Testing 124 alone sent the wedge case --
        the one the grace exists for -- down the FAIL arm, labelled "exited
        137" and skipping the marker removal. Writing this test is what found
        that; the source read fine.
        """
        started = time.monotonic()
        proc = self._run(tmp_path, rc=0, sleep_s=60, timeout_s=1, ignore_term=True)
        elapsed = time.monotonic() - started
        assert proc.returncode == 0, proc.stderr
        assert elapsed < 10, f"the timeout arm took {elapsed:.1f}s to reach; bound 1s + grace 1s was not enforced"
        warnings = self._annotations(proc)
        assert len(warnings) == 1, proc.stdout
        assert "timed out" in warnings[0], (
            f"a SIGKILLed child comes back as 137, not 124, and must still take the TIMEOUT arm: {warnings[0]}"
        )
        assert "(rc=137)" in warnings[0], warnings[0]
        assert "TIMED OUT" in proc.stdout, proc.stdout
        assert not (tmp_path / ".runtime-render-validated").exists()

    def test_a_marker_stamped_after_the_bound_expired_is_removed(self, tmp_path):
        """PR litclock-dev#852 review (Claude F2). A validator that survives the TERM long
        enough to finish stamps a marker while the annotation says the image
        ships without one. A proof from a run that overran its bound is not
        trusted: the 124 arm removes it."""
        proc = self._run(tmp_path, rc=0, sleep_s=20, timeout_s=1, stamp_late=True)
        assert proc.returncode == 0, proc.stderr
        warnings = self._annotations(proc)
        assert len(warnings) == 1 and "timed out" in warnings[0], proc.stdout
        assert not (tmp_path / ".runtime-render-validated").exists(), (
            "a marker written after the bound expired shipped in the image while the annotation said there was none"
        )


class TestTheCheckItselfBehaves:
    """Executed, against the real validator and the real dump."""

    def test_check_passes_and_stamps_here(self, tmp_path):
        # The validator imports freetype-py, which lives in requirements.txt
        # (it ships to the device), not requirements-dev.txt. CI installs
        # both; the documented dev setup installs only -dev, and this test
        # was red there for that reason alone (litclock-dev#840). Skip with
        # the diagnosis rather than fail on a box that cannot run the check.
        pytest.importorskip(
            "freetype",
            reason="freetype-py is not installed: `pip install --user --break-system-packages freetype-py` "
            "to run the real validator here (litclock-dev#840)",
        )
        marker = tmp_path / ".runtime-render-validated"
        r = subprocess.run(
            [sys.executable, str(VALIDATOR), "check", "--stamp", "--marker", str(marker)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert r.returncode == 0, f"validation failed on the dev box\n{r.stdout}\n{r.stderr}"
        assert marker.is_file(), "PASS must write the marker at the requested path"

    def test_stamp_refuses_a_dump_that_is_not_the_committed_one(self, tmp_path):
        """The guard update.sh and pi-gen rely on without knowing it.

        `--stamp` against a `--dump` that differs from the committed
        tools/gd-expected-measurements.json.gz exits 2 and touches NOTHING —
        not the marker, not the dump. An earlier version of this test expected
        a bogus dump to REMOVE a stale marker; it does not, and that is
        correct: acting on an unverified dump in either direction would let a
        tampered or half-written file decide whether the runtime tier is on.
        """
        marker = tmp_path / ".runtime-render-validated"
        marker.write_text("pre-existing")
        bogus = tmp_path / "bogus.json.gz"
        bogus.write_bytes(b"not a gzip")
        r = subprocess.run(
            [sys.executable, str(VALIDATOR), "check", "--stamp", "--dump", str(bogus), "--marker", str(marker)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert r.returncode == 2, f"expected the refusal exit (2), got {r.returncode}\n{r.stderr}"
        assert "refusing --stamp" in (r.stdout + r.stderr)
        assert marker.read_text() == "pre-existing", "a refused stamp must leave the marker untouched"


class TestTheSelftestIsCalledFromTheKeepArm:
    """litclock-dev#871 Stage A — WHEN the KEEP arm asks the self-test, pinned
    by the stub in the harness above: with a marker (surviving or just
    stamped), never without one, never in rollback mode."""

    _h = TestUpdateShStampBlockExecutes

    def test_it_runs_after_a_fresh_stamp(self, tmp_path):
        r = self._h()._run(tmp_path, marker_exists=False, validator_rc=0, stamps=True)
        assert "validation PASSED" in r.stdout
        assert "STUB_SELFTEST" in r.stdout, r.stdout
        assert r.stdout.index("validation PASSED") < r.stdout.index("STUB_SELFTEST"), "stamp first, then ask"

    def test_it_runs_with_a_surviving_marker(self, tmp_path):
        r = self._h()._run(tmp_path, marker_exists=True, validator_rc=1)
        assert "FAKE_VALIDATOR_RAN" not in r.stderr, "a present marker skips the validator"
        assert "STUB_SELFTEST" in r.stdout, r.stdout

    def test_it_is_skipped_when_the_validation_did_not_stamp(self, tmp_path):
        r = self._h()._run(tmp_path, marker_exists=False, validator_rc=1)
        assert "did not pass" in r.stdout
        assert "STUB_SELFTEST" not in r.stdout, "no marker, no self-test: the painter would decline before trying"

    def test_it_is_skipped_in_rollback_mode(self, tmp_path):
        r = self._h()._run(tmp_path, marker_exists=True, validator_rc=0, rollback_mode=True)
        assert "STUB_SELFTEST" not in r.stdout, "a rollback run exists to get the clock painting, not to learn"

    def test_the_call_sits_inside_the_keep_arm_after_the_validation_block(self):
        body = UPDATE_SH.read_text()
        keep = body.index('if [[ "$smoke_rc" -eq 0 ]]; then\n    log_info "Smoke test passed"')
        else_at = body.index('else\n    log_error "Smoke test failed', keep)
        arm = body[keep:else_at]
        executed = [ln for ln in arm.splitlines() if not ln.lstrip().startswith("#")]
        calls = [ln for ln in executed if ln.strip() == "_runtime_render_selftest"]
        assert len(calls) == 1, f"exactly one self-test call inside the KEEP arm; found {calls}"
        assert arm.index("unset _validate_rc") < arm.index("\n        _runtime_render_selftest\n"), (
            "the self-test must follow the validation block (a fresh stamp is what makes it worth asking)"
        )
        executed_all = [ln for ln in body.splitlines() if not ln.lstrip().startswith("#")]
        whole = [ln for ln in executed_all if ln.strip() == "_runtime_render_selftest"]
        assert len(whole) == 1, "the self-test must not be called from anywhere else"


class TestRuntimeRenderSelftestExecutes:
    """The self-test function itself, RUN with a fake painter (litclock-dev#871
    Stage A). What it must do: force the renderer on, source env.sh for the
    device's language, pass --require-runtime-render, point the frame
    directory somewhere disposable, and turn the painter's exit code into the
    memo — 0 clears nothing, 3 is 'fell back', 124 is a timeout, anything
    else is 'died'. And never fail the update."""

    def _selftest_fn(self):
        body = UPDATE_SH.read_text()
        assert body.count("_runtime_render_selftest() {") == 1
        start = body.index("_runtime_render_selftest() {")
        end = body.index("\n}\n", start) + len("\n}\n")
        fn = body[start:end]
        for required, why in (
            ("env LITCLOCK_RUNTIME_RENDER=true", "the renderer forced on, via env(1) so a readonly cannot defeat it"),
            ("--require-runtime-render", "the flag that makes a fallback a failure"),
            ("LITCLOCK_RUNTIME_RENDER_DIR", "the throwaway frame directory"),
            ("WEATHER_ENABLED=false \\", "weather forced off — a capability probe stays off the network"),
            ('source "${INSTALL_DIR:-}/env.sh"', "env.sh sourced for the device's language"),
            ("selftest-deferred", "the deferral token, distinct from the validator's"),
            ('_validation_fits_remaining_budget "$SELFTEST_TIMEOUT_S"', "the shared budget guard, own bound"),
            ('rc="${PIPESTATUS[0]}"', "the painter's status, not sed's"),
            ('[[ -n "$dir" && -d "$dir" ]] && rm -rf -- "$dir"', "cleanup of the fresh directory only"),
            ("_runtime_selftest_record_write", "the durable pass record"),
            ('atomic_remove_file "$RUNTIME_SELFTEST_RECORD_FILE"', "a fail retiring an earlier pass"),
            ("selftest-failed", "the memo token"),
            ("return 0", "never failing the update"),
        ):
            assert required in fn, f"_runtime_render_selftest lost {why} ({required!r})"
        assert "exit " not in "\n".join(ln for ln in fn.splitlines() if not ln.lstrip().startswith("#")), (
            "the self-test must never exit the updater"
        )
        return fn

    def _run(
        self,
        tmp_path,
        *,
        painter_rc: int,
        elapsed_s=None,
        installed_budget_s=None,
        language="xx",
        sleep=0,
        selftest_timeout_s=SELFTEST_TIMEOUT_DEFAULT_S,
        mktemp_fails=False,
        pass_record_exists=False,
        preseed_result=None,
        env_extra="",
    ):
        log = tmp_path / "painter.log"
        fake_py = tmp_path / "python3"
        # The fake painter records what it was told, whether the scratch
        # directory EXISTS (not merely that the variable is set — litclock-dev#875 testing
        # specialist), and leaves a file in it so the cleanup has to remove a
        # non-empty directory.
        fake_py.write_text(
            "#!/bin/bash\n"
            f'printf "argv=%s\\n" "$*" >> {log}\n'
            f'printf "render=%s lang=%s weather=%s dir=%s\\n" "${{LITCLOCK_RUNTIME_RENDER:-unset}}" '
            f'"${{LITCLOCK_LANGUAGE:-unset}}" "${{WEATHER_ENABLED:-unset}}" '
            f'"${{LITCLOCK_RUNTIME_RENDER_DIR:-unset}}" >> {log}\n'
            f'if [ -d "${{LITCLOCK_RUNTIME_RENDER_DIR:-}}" ]; then echo exists=yes >> {log}; '
            f': > "$LITCLOCK_RUNTIME_RENDER_DIR/current-quote.png"; else echo exists=no >> {log}; fi\n'
            # `|| exit 97`: the duration tests are the first to depend on the
            # painter actually SLEEPING, and a swallowed failure here reports as
            # a defect in the shipped writer. 97 is outside every arm the
            # self-test distinguishes (0/3/124), so it reads as the harness.
            f"sleep {sleep} || exit 97\n"
            "echo painter-says-hello\n"
            f"exit {painter_rc}\n"
        )
        fake_py.chmod(0o755)
        install = tmp_path / "install"
        install.mkdir(exist_ok=True)
        (install / "env.sh").write_text(
            f"export LITCLOCK_LANGUAGE={language}\nexport LITCLOCK_RUNTIME_RENDER=false\nexport WEATHER_ENABLED=true\n"
            + env_extra
        )
        memo = tmp_path / "memo.json"
        # A test may call this twice in one tmp_path: start each run clean, or
        # the second run's assertions read the first run's log and memo.
        log.unlink(missing_ok=True)
        memo.unlink(missing_ok=True)
        h = TestUpdateShStampBlockExecutes()
        if elapsed_s is None:
            stub = "_update_elapsed_seconds() { return 2; }\n"
        elif elapsed_s == "unknown":
            stub = "_update_elapsed_seconds() { return 1; }\n"
        else:
            stub = f"_update_elapsed_seconds() {{ echo {int(elapsed_s)}; }}\n"
        if installed_budget_s is None:
            stub += "_update_budget_seconds() { return 1; }\n"
        else:
            stub += f"_update_budget_seconds() {{ echo {int(installed_budget_s)}; }}\n"
        program = (
            "set -u\n"
            'log_info() { echo "[INFO] $1"; }\n'
            'log_warn() { echo "[WARN] $1"; }\n'
            'log_error() { echo "[ERROR] $1"; }\n'
            'atomic_remove_file() { [ "$1" = /dev/null ] || rm -f "$1"; }\n'
            'atomic_write_file() { [ "$1" = /dev/null ] || printf "%s" "$2" > "$1"; }\n'
            f"PYTHON={fake_py}\nINSTALL_DIR={install}\nRUNTIME_VALIDATION_MEMO_FILE={memo}\n"
            f"RUNTIME_SELFTEST_RECORD_FILE={tmp_path / 'selftest.json'}\n"
            f"{h._budget_helpers()}"
            # AFTER the lifted helpers, which carry the script's own constants:
            # the 2s default keeps the timeout case fast, and the boundary
            # tests compute against it. The duration tests raise it, because
            # they need a painter that runs LONGER than the default bound.
            f"SELFTEST_TIMEOUT_S={selftest_timeout_s}\nVALIDATOR_BUDGET_RESERVE_S={SELFTEST_BUDGET_RESERVE_S}\n"
            f"{stub}"
            + ("mktemp() { return 1; }\n" if mktemp_fails else "")
            + (f"RUNTIME_SELFTEST_RESULT={preseed_result}\n" if preseed_result else "")
            + f"{self._record_fn()}"
            + f"{self._selftest_fn()}"
            "_runtime_render_selftest\n"
            # RESULT is what Stage B reads (`!= pass` returns silently). Printed
            # on the same line so `rc=$?` is still the function's status.
            'echo "REACHED_END rc=$? RESULT=${RUNTIME_SELFTEST_RESULT:-<unset>}"\n'
        )
        record = tmp_path / "selftest.json"
        record.unlink(missing_ok=True)
        if pass_record_exists:
            record.write_text('{"result": "passed", "duration_s": 1.0, "sha": null, "at_unix": 1}')
        r = subprocess.run(["bash", "-c", program], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        painter = log.read_text() if log.exists() else ""
        return r, painter, (json.loads(memo.read_text()) if memo.exists() else None)

    def _record_fn(self):
        body = UPDATE_SH.read_text()
        assert body.count("_runtime_selftest_record_write() {") == 1
        start = body.index("_runtime_selftest_record_write() {")
        end = body.index("\n}\n", start) + len("\n}\n")
        fn = body[start:end]
        assert 'result: "passed"' in fn and "duration_s" in fn
        return fn

    @staticmethod
    def _record(tmp_path):
        path = tmp_path / "selftest.json"
        return json.loads(path.read_text()) if path.exists() else None

    @pytest.mark.parametrize(
        "kwargs, want",
        [
            (dict(painter_rc=0), "pass"),
            (dict(painter_rc=3), "fail"),
            (dict(painter_rc=124), "fail"),
            # The early returns, with a stale `pass` pre-seeded: the reset at the
            # top is what stops Stage B acting on a verdict nobody reached.
            (dict(painter_rc=0, mktemp_fails=True, preseed_result="pass"), "deferred"),
            (dict(painter_rc=0, elapsed_s=5000, installed_budget_s=600, preseed_result="pass"), "deferred"),
        ],
    )
    def test_the_verdict_stage_b_reads_matches_the_outcome(self, tmp_path, kwargs, want):
        """The handoff between the two stages: Stage B acts only on
        RUNTIME_SELFTEST_RESULT == pass, and every Stage B test injects that
        value by hand, so nothing pinned the producer (port /review)."""
        r, _painter, _memo = self._run(tmp_path, **kwargs)
        # Whole token, end of line: a substring match let `passed` satisfy
        # `pass`, which is exactly the rename this test exists to catch.
        assert f"RESULT={want}\n" in r.stdout, r.stdout + r.stderr

    def test_a_readonly_env_sh_cannot_defeat_the_selftests_overrides(self, tmp_path):
        """Same hole as the smoke gate's (port /review round 2): with `export`, a
        readonly in env.sh put the probe on the network and its frame in the
        live directory."""
        r, painter, _memo = self._run(
            tmp_path,
            painter_rc=0,
            env_extra=(
                "readonly WEATHER_ENABLED LITCLOCK_RUNTIME_RENDER\n"
                "export LITCLOCK_RUNTIME_RENDER_DIR=/run/litclock\nreadonly LITCLOCK_RUNTIME_RENDER_DIR\n"
            ),
        )
        assert "render=true" in painter and "weather=false" in painter, painter + r.stderr
        assert "dir=/run/litclock" not in painter and "exists=yes" in painter, painter

    def test_a_pass_forces_the_renderer_on_with_the_devices_language_and_writes_no_memo(self, tmp_path):
        r, painter, memo = self._run(tmp_path, painter_rc=0)
        assert "REACHED_END rc=0" in r.stdout, r.stdout + r.stderr
        assert "self-test PASSED" in r.stdout
        assert "[selftest] painter-says-hello" in r.stdout, "the painter's output must reach the journal, prefixed"
        assert "argv=src/literary_clock.py --dry-run --require-runtime-render" in painter, painter
        assert "render=true" in painter, "the renderer must be FORCED on, whatever env.sh says"
        assert "lang=xx" in painter, "env.sh must be sourced so the device's language is the one rendered"
        assert "weather=false" in painter, "weather must be forced OFF, whatever env.sh says"
        assert "exists=yes" in painter, "the frame directory must EXIST while the painter runs"
        assert "self-test PASSED in " in r.stdout, "a pass must be logged with its duration"
        assert memo is None
        rec = self._record(tmp_path)
        assert rec is not None and rec["result"] == "passed", "a pass must leave a DURABLE record for Stage B"
        # SHAPE only: any constant in [0, 5) satisfies this, `0.0` included,
        # which is what litclock-dev#883 was filed about. The VALUE is asserted
        # by TestSelfTestDurationReachesTheRecord below.
        assert isinstance(rec["duration_s"], (int, float)) and 0 <= rec["duration_s"] < 5, rec
        assert rec["sha"] and rec["at_unix"] > 0

    def test_a_fail_retires_an_earlier_pass_record(self, tmp_path):
        _, _, memo = self._run(tmp_path, painter_rc=3, pass_record_exists=True)
        assert memo is not None and memo["result"] == "selftest-failed"
        assert self._record(tmp_path) is None, "Stage B must never flip on a stale pass"

    def test_a_deferral_leaves_an_earlier_pass_record_alone(self, tmp_path):
        _, painter, memo = self._run(tmp_path, painter_rc=0, mktemp_fails=True, pass_record_exists=True)
        assert painter == "" and memo is not None and memo["result"] == "selftest-deferred"
        assert self._record(tmp_path) is not None, "nothing was asked, nothing learned; the record stands"

    def test_a_fallback_is_memoed_as_selftest_failed_with_rc_3(self, tmp_path):
        r, _, memo = self._run(tmp_path, painter_rc=3)
        assert "REACHED_END rc=0" in r.stdout, "a failed self-test is not an update failure"
        assert "fell back to pre-rendered images" in r.stdout
        assert memo is not None and memo["result"] == "selftest-failed" and memo["rc"] == 3, memo
        assert "fell back" in memo["reason"]

    def test_a_dead_painter_is_memoed_with_its_rc(self, tmp_path):
        r, _, memo = self._run(tmp_path, painter_rc=1)
        assert "REACHED_END rc=0" in r.stdout
        assert memo is not None and memo["result"] == "selftest-failed" and memo["rc"] == 1, memo
        assert "exited 1" in memo["reason"]

    def test_a_timeout_is_told_apart(self, tmp_path):
        r, _, memo = self._run(tmp_path, painter_rc=0, sleep=5)
        assert "REACHED_END rc=0" in r.stdout
        assert "did not finish" in r.stdout
        assert memo is not None and memo["result"] == "selftest-failed" and memo["rc"] == 124, memo

    def test_a_disposable_frame_directory_is_removed_afterwards(self, tmp_path):
        _, painter, _ = self._run(tmp_path, painter_rc=0)
        d = next(ln.split("dir=", 1)[1].strip() for ln in painter.splitlines() if "dir=" in ln)
        assert d and d != "unset" and d != "/run/litclock", d
        assert "exists=yes" in painter, "the painter left a file in it, so the cleanup removed a NON-empty directory"
        assert not Path(d).exists(), d

    def test_no_scratch_directory_means_no_run(self, tmp_path):
        """litclock-dev#875 review (Codex, security, adversarial): with mktemp failed the
        painter's default frame directory IS the panel's (/run/litclock), so
        running anyway would overwrite the live frame. Defer instead."""
        r, painter, memo = self._run(tmp_path, painter_rc=0, mktemp_fails=True)
        assert "REACHED_END rc=0" in r.stdout
        assert painter == "", "the painter must not run without a scratch directory"
        assert memo is not None and memo["result"] == "selftest-deferred" and "scratch" in memo["reason"], memo

    def test_an_unreadable_budget_defers_even_when_elapsed_is_known(self, tmp_path):
        """litclock-dev#875 testing specialist: this arm was untested, and a mutant that read
        an unreadable budget as UNLIMITED — the inversion the litclock-dev#835 helpers
        forbid — stayed green."""
        r, painter, memo = self._run(tmp_path, painter_rc=0, elapsed_s=100, installed_budget_s=None)
        assert "REACHED_END rc=0" in r.stdout
        assert painter == "", "an unreadable budget is 'cannot afford it', never 'no budget'"
        assert memo is not None and memo["result"] == "selftest-deferred" and "TimeoutStartSec" in memo["reason"], memo

    def test_no_budget_left_defers_with_a_memo_and_does_not_run_the_painter(self, tmp_path):
        r, painter, memo = self._run(tmp_path, painter_rc=0, elapsed_s=1700, installed_budget_s=1800)
        assert "REACHED_END rc=0" in r.stdout
        assert "deferring the runtime-render self-test" in r.stdout
        assert painter == "", "the painter must not run when the budget cannot hold it"
        assert memo is not None and memo["result"] == "selftest-deferred", memo

    def test_enough_budget_runs_it(self, tmp_path):
        _, painter, memo = self._run(tmp_path, painter_rc=0, elapsed_s=1600, installed_budget_s=1800)
        assert "argv=" in painter and memo is None

    def test_the_boundary_is_exact(self, tmp_path):
        # elapsed + timeout + reserve > budget defers; == does not. DERIVED from
        # the two constants rather than the hand-computed 1678/1679 this used to
        # carry (litclock-dev#883): the bound became a defaulted parameter, and a
        # literal here would track it only by comment.
        budget = 1800
        fits = budget - SELFTEST_BUDGET_RESERVE_S - SELFTEST_TIMEOUT_DEFAULT_S
        _, painter, _ = self._run(tmp_path, painter_rc=0, elapsed_s=fits, installed_budget_s=budget)
        assert "argv=" in painter, f"{fits}+{SELFTEST_TIMEOUT_DEFAULT_S}+{SELFTEST_BUDGET_RESERVE_S} == {budget} fits"
        _, painter, _ = self._run(tmp_path, painter_rc=0, elapsed_s=fits + 1, installed_budget_s=budget)
        assert painter == "", f"{fits + 1}+{SELFTEST_TIMEOUT_DEFAULT_S}+{SELFTEST_BUDGET_RESERVE_S} > {budget} defers"

    def test_an_unknown_budget_defers_an_unlimited_one_runs(self, tmp_path):
        _, painter, memo = self._run(tmp_path, painter_rc=0, elapsed_s="unknown")
        assert painter == "" and memo is not None and memo["result"] == "selftest-deferred"
        _, painter, memo = self._run(tmp_path, painter_rc=0, elapsed_s=5000, installed_budget_s=0)
        assert "argv=" in painter and memo is None

    def test_outside_systemd_there_is_no_budget_to_respect(self, tmp_path):
        _, painter, memo = self._run(tmp_path, painter_rc=0, elapsed_s=None)
        assert "argv=" in painter and memo is None


class TestSelfTestDurationReachesTheRecord:
    """litclock-dev#883 — `duration_s` in the REAL record, the field litclock-dev#871
    Stage B is designed to gate on ("does this device render inside the render lead").

    `TestRuntimeRenderSelftestExecutes` is the only place the real
    `_runtime_selftest_record_write` executes, and every duration assertion it
    made was `0 <= duration_s < 5` — a band any constant in it satisfies, `0.0`
    included. `TestSelfTestDurationSurvivesToTheRecord` in test_update_sh.py
    covers the layer above and cannot see this one: it STUBS the writer, so it is
    scoped to what `_runtime_render_selftest` HANDS the writer. The boundary
    between the two classes is the writer itself, which is why these live here.
    Measured: `local duration="0.0"` in the writer is red here and GREEN there.

    Three shapes, because the first draft of this class was one band plus one
    movement control and four independent review passes each defeated it.
    MEASURED against an eleven-mutation battery on a quiet box (x = caught):

        mutation                          accuracy  control  lead
        writer: hardcoded 0.0                 x        x       x
        writer: constant 1.5                  -        x       x
        writer: epoch                         x        -       x
        writer: floor                         x        -       x
        writer: +0.9                          x        -       x
        writer: *0.7 / *1.5                   x        -       x
        writer: *0.98                         x        -       x
        writer: clamp at 3                    -        -       x
        selftest: dropped decisecond          x        -       x
        selftest: $SECONDS revert             x        -       x

    Read that honestly, in three parts.

    The lead test catches all eleven and the other two are dominated on THIS
    battery. They stay for their diagnosis, not their coverage: "below the
    sleep", "not tracking the clock" and "Stage B would pass a device that
    cannot make it" send the next reader to three different places. The control
    also holds the one RELATIONAL property — compared between two records rather
    than against a computed bound — so it survives any future loosening of the
    bounds below.

    The battery is a measurement, not a guarantee. The accuracy window is
    `[sleep, wall]`, so its WIDTH is however much the box stalled: ~0.05s idle,
    and a second wide under a second of stall, where a coarse mutation such as
    `floor` fits inside it and passes (measured, by injecting the delay). The
    degradation is one-way — load widens the window, so it costs catching power
    and never produces a false red — which is the right direction for CI, and
    the reason to run the battery on a quiet box when it matters.

    The one genuinely unqualified claim is the LOWER bound: a reading below the
    sleep cannot be the elapsed time. Its only escape is a backward
    CLOCK_REALTIME step inside the painter's window, which reds it. The upper
    bound is now measured on the same clock as the value (see `_timed_run`), so
    a step moves both together and cannot break the nesting.

    Total cost 8.8s of sleeping in a suite that runs ~330s.

    **The accuracy bound is the painter's own sleep and the subprocess's measured
    wall clock, not a fixed tolerance.** The first draft used `abs(d - 1.5) <= 1.0`
    and four imprecision mutations stayed green under it, the sharpest being
    `t0=$SECONDS` / `dur_ms=$(( (SECONDS-t0)*1000 ))` — a literal revert of the
    decision recorded four lines above the code it mutates ("whole-second
    $SECONDS is ±1s on it", the litclock-dev#875 red team), because a ±1.0s tolerance is
    exactly what that revert costs. `floor`, `+0.9` and a dropped decisecond
    survived it too. A tighter fixed tolerance would trade that for flake, so the
    bound is not fixed: the painter really did sleep `S`, so an honest reading is
    never below `S`; and the whole subprocess is strictly longer than the painter
    inside it, so an honest reading is never above the wall clock. Neither side
    is a number anybody had to guess at, and the failure direction under load is
    one-way — see the third part of the battery note below for what that does
    and does not buy.

    What these do NOT cover, stated so it is not re-derived: a mutation that
    keeps the value honest at every sleep used here. The locale layer
    (`TestSelfTestDurationIsLocaleProof`, test_update_sh.py) covers the separator
    class, which no amount of sleeping reaches.
    """

    # Must exceed every sleep below: the painter is wrapped in `timeout
    # "$SELFTEST_TIMEOUT_S"`, and exceeding it takes the rc-124 arm, which
    # deletes the record and surfaces as "no record" rather than "bound too low".
    TIMEOUT_S = 30
    # Every sleep sits on a decisecond BOUNDARY, which is what lets the lower
    # bound be the sleep exactly rather than the sleep minus a quantum
    # (litclock-dev#883, second review round). `duration` is formatted `S.d` by
    # truncation, and truncating an elapsed time of at least 1.5s cannot yield
    # 1.4s — so subtracting a quantum "for safety" admits values the shipped
    # formatter cannot honestly produce. It cost a real mutation: `d * 0.98`
    # passed all three tests, recording a 4.05s paint as 3.92s — inside the lead.
    SLEEP_S = 1.5  # deliberately mid-second: whole-second arithmetic
    #                            lands 0.5s away whichever way it rounds
    CONTROL_SHORT_S = 0.3
    CONTROL_LONG_S = 2.5
    MIN_MOVE_S = 1.5  # true movement 2.2s; ~0.7s of slack
    SLOW_MARGIN_S = 0.5  # the slow painter runs this far ABOVE the lead

    H = TestRuntimeRenderSelftestExecutes

    def _timed_run(self, tmp_path, **kw):
        """Run the real self-test and time the whole subprocess.

        The elapsed wall clock is an UPPER bound on what the painter inside it
        can honestly have taken — it also carries bash startup, the lifted
        helpers and the writer's own `jq`/`git`.
        """
        # time.time(), NOT time.monotonic(): the value under test comes from
        # EPOCHREALTIME, which is CLOCK_REALTIME. Timing the outside on the
        # monotonic clock compares two clocks that step differently, and an NTP
        # step or a suspend then breaks the nesting this bound rests on (review
        # replayed both directions). Reading the SAME clock on both sides means
        # a step moves the inner and outer measurement together.
        t0 = time.time()
        r, _painter, memo = self.H()._run(tmp_path, selftest_timeout_s=self.TIMEOUT_S, **kw)
        wall = time.time() - t0
        assert "self-test PASSED" in r.stdout, (
            f"the run did not take the PASS arm, so there is no duration to judge — "
            f"rc 97 here means the HARNESS's `sleep` failed, not the writer:\n{r.stdout}\n{r.stderr}"
        )
        assert memo is None, f"a pass writes no memo, got {memo}"
        rec = self.H._record(tmp_path)
        assert rec is not None, "the pass arm must have written the record"
        return rec, wall

    def _assert_honest(self, rec, slept, wall):
        d = rec["duration_s"]
        assert d >= slept, (
            f"the record says {d}s for a painter that slept {slept}s — a reading BELOW the "
            f"sleep cannot be the elapsed time, whatever the machine was doing"
        )
        assert d <= wall, (
            f"the record says {d}s but the whole subprocess took {wall:.2f}s — the painter "
            f"is strictly inside that, so the value is not the elapsed time"
        )

    def test_the_recorded_duration_is_the_real_elapsed_time(self, tmp_path):
        """A painter that sleeps a known time must put THAT time in the record."""
        rec, wall = self._timed_run(tmp_path, painter_rc=0, sleep=self.SLEEP_S)
        self._assert_honest(rec, self.SLEEP_S, wall)

    def test_a_slower_painter_records_a_longer_duration(self, tmp_path):
        """The movement control.

        A constant INSIDE the accuracy bounds passes them exactly as the real
        clock does; only a second measurement at a different paint time
        separates the two. The reverse also holds, which is the part worth
        recording: an epoch-valued duration MOVES between the two runs and
        passes this test, while failing accuracy — measured, and the inverse of
        what this docstring claimed in its first draft. So accuracy and movement
        each catch something the other does not, even though the lead test above
        happens to catch both.
        """
        short, _ = self._timed_run(tmp_path, painter_rc=0, sleep=self.CONTROL_SHORT_S)
        long, _ = self._timed_run(tmp_path, painter_rc=0, sleep=self.CONTROL_LONG_S)
        moved = long["duration_s"] - short["duration_s"]
        asked = self.CONTROL_LONG_S - self.CONTROL_SHORT_S
        assert moved >= self.MIN_MOVE_S, (
            f"a painter asked to run {asked:.1f}s longer moved the recorded duration by only "
            f"{moved}s ({short['duration_s']} -> {long['duration_s']}) — duration_s is not "
            f"tracking the clock (or this box stalled the short run by ~{asked - self.MIN_MOVE_S:.1f}s)"
        )

    def test_a_paint_slower_than_the_lead_is_recorded_as_slower(self, tmp_path):
        """The gate boundary — the shape the other two cannot see.

        Anyone who ever asks "did this device render inside the lead" needs the
        value honest AT that boundary, not merely somewhere near 1.5s. Stage B
        as built does not ask it (it gates on `result` and `sha`), which is why
        this is a property of the record, not a Stage B threshold.
        Measured: a writer that CLAMPS the duration (`min(d, 3)`) passes both
        tests above and would report every slow device as comfortably inside the
        lead — the exact "falsely SMALL value sailing through a threshold"
        hazard `update.sh`'s own litclock-dev#879 comment names. (A scale-down
        like `d * 0.7` is caught by accuracy as well; only the clamp reaches
        here alone.)

        The lead assertion runs BEFORE the accuracy bounds on purpose. It is the
        weaker of the two — the lower bound already implies it — so behind them
        it could never fail, and the clamp would report as "below the sleep"
        rather than as the Stage B consequence, which is the diagnosis worth
        reading.
        """
        lead = render_lead_s()
        slept = lead + self.SLOW_MARGIN_S
        rec, wall = self._timed_run(tmp_path, painter_rc=0, sleep=slept)
        assert rec["duration_s"] >= lead, (
            f"a paint that took {slept}s was recorded as {rec['duration_s']}s, inside "
            f"the {lead}s lead — the record would call a slow device fast"
        )
        self._assert_honest(rec, slept, wall)


class TestStageBMigrationExecutes:
    """litclock-dev#871 Stage B — the FLIP, run with the real functions.

    The one part of this feature that changes a device's behaviour, so every
    guard arm is EXECUTED here and each asserts what env.sh looks like
    AFTERWARDS. A guard that is only grepped for is a guard nobody has seen
    refuse anything (litclock-dev#782's whole finding), and this one decides
    whether a fielded clock stops using its pre-rendered images.
    """

    SAMPLE_ENV = (
        "export WEATHER_ENABLED=true\n"
        "export LITCLOCK_LANGUAGE=en\n"
        "export LITCLOCK_RUNTIME_RENDER=false\n"
        "# export LOG_LEVEL=WARNING\n"
    )

    @staticmethod
    def _pass_record(tmp_path):
        """A durable sha-matched pass record, which Stage B gates on."""
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30
        ).stdout.strip()
        path = tmp_path / "selftest.json"
        path.write_text(json.dumps({"result": "passed", "duration_s": 1.2, "sha": head, "at_unix": 1}))
        return path

    @staticmethod
    def _fn(name):
        body = UPDATE_SH.read_text()
        assert body.count(f"{name}() {{") == 1, name
        start = body.index(f"{name}() {{")
        return body[start : body.index("\n}\n", start) + 3]

    def _run(self, tmp_path, *, selftest="pass", env_body=None, images=True, env_file=True, record="match"):
        install = tmp_path / "install"
        install.mkdir(exist_ok=True)
        if env_file:
            (install / "env.sh").write_text(self.SAMPLE_ENV if env_body is None else env_body)
        if images:
            meta = install / "images" / "metadata"
            meta.mkdir(parents=True, exist_ok=True)
            (meta / "quote_0000_0_credits.png").write_bytes(b"x")
        memo = tmp_path / "memo.json"
        memo.unlink(missing_ok=True)
        # The durable sha-matched pass record Stage B gates on alongside the
        # in-run verdict (litclock-dev#871; the Stage A comment above
        # RUNTIME_SELFTEST_RECORD_FILE promises exactly this).
        rec = tmp_path / "selftest.json"
        rec.unlink(missing_ok=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30
        ).stdout.strip()
        if record == "match":
            rec.write_text(json.dumps({"result": "passed", "duration_s": 1.2, "sha": head, "at_unix": 1}))
        elif record == "stale":
            rec.write_text(json.dumps({"result": "passed", "duration_s": 1.2, "sha": "0" * 40, "at_unix": 1}))
        elif record == "failed":
            rec.write_text(json.dumps({"result": "selftest-failed", "sha": head, "at_unix": 1}))
        elif record == "corrupt":
            # A VALID object followed by garbage. jq prints the matching value
            # and THEN exits non-zero, so a `|| echo ""` fallback never runs and
            # the substitution keeps the value — measured: the old form captured
            # "passed" from exactly this file and the gate accepted it.
            rec.write_text(json.dumps({"result": "passed", "sha": head, "at_unix": 1}) + "\ngarbage{\n")
        # record == "absent": leave it unwritten
        program = (
            "set -u\n"
            'log_info() { echo "[INFO] $1"; }\n'
            'log_warn() { echo "[WARN] $1"; }\n'
            'log_error() { echo "[ERROR] $1"; }\n'
            'atomic_write_file() { printf "%s" "$2" > "$1"; }\n'
            'atomic_remove_file() { rm -f "$1"; }\n'
            # The REAL lock helper and the REAL env.sh finalizer, lifted from
            # state.sh — the flip's atomicity and its owner/mode preservation
            # are the point, so a stub here would test a different program.
            f'source "{REPO_ROOT}/scripts/lib/state.sh"\n'
            f"INSTALL_DIR={install}\nRUNTIME_VALIDATION_MEMO_FILE={memo}\n"
            f"RUNTIME_SELFTEST_RECORD_FILE={rec}\n"
            f"RUNTIME_SELFTEST_RESULT={selftest}\n"
            f"{self._fn('_atomic_write_env_sh_finalize_from_body')}"
            f"{self._fn('_runtime_render_migrate_locked')}"
            f"{self._fn('_runtime_validation_memo_write')}"
            f"{self._fn('_runtime_render_migrate')}"
            "_runtime_render_migrate\n"
            'echo "REACHED_END rc=$?"\n'
        )
        r = subprocess.run(["bash", "-c", program], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        after = (install / "env.sh").read_text() if (install / "env.sh").exists() else None
        return r, after, (json.loads(memo.read_text()) if memo.exists() else None)

    # ── the one arm that migrates ────────────────────────────────────────

    def test_a_passing_selftest_with_images_flips_the_flag(self, tmp_path):
        r, after, memo = self._run(tmp_path)
        assert "REACHED_END rc=0" in r.stdout, r.stdout + r.stderr
        assert "export LITCLOCK_RUNTIME_RENDER=true" in after
        assert "=false" not in after
        assert "MIGRATED to runtime text rendering" in r.stdout
        assert memo is None, "a successful migration is not a negative result"

    def test_it_changes_nothing_else_in_env_sh(self, tmp_path):
        """The flip rewrites the whole file, so 'it only touched that line' is a
        property worth asserting rather than assuming."""
        _, after, _ = self._run(tmp_path)
        assert after == self.SAMPLE_ENV.replace("RUNTIME_RENDER=false", "RUNTIME_RENDER=true")

    def test_it_is_idempotent(self, tmp_path):
        """Self-limiting by design: after the flip the pattern no longer matches,
        which is what lets Stage B ship with no one-shot marker."""
        self._run(tmp_path)
        r, after, memo = self._run(
            tmp_path, env_body=self.SAMPLE_ENV.replace("RUNTIME_RENDER=false", "RUNTIME_RENDER=true")
        )
        assert "MIGRATED" not in r.stdout
        assert "export LITCLOCK_RUNTIME_RENDER=true" in after
        assert memo is None, "an already-migrated device is the goal state, not a negative result"

    # ── every arm that must NOT migrate ──────────────────────────────────

    @pytest.mark.parametrize("selftest", ["fail", "deferred", ""])
    def test_a_selftest_that_did_not_pass_leaves_env_sh_alone(self, tmp_path, selftest):
        """The marker is not enough. It says freetype reproduces the measurement
        dump; only the self-test says this device painted a quote from text."""
        r, after, memo = self._run(tmp_path, selftest=selftest)
        assert after == self.SAMPLE_ENV, "env.sh must be byte-identical"
        assert "MIGRATED" not in r.stdout
        assert memo is None, "the self-test already wrote its own memo; do not contradict it"

    @pytest.mark.parametrize(
        "record,why,reason_fragment",
        [
            # The REASON, not just the token: an absent record and a mismatched
            # one both refuse, so asserting only "migration-skipped" leaves the
            # two arms indistinguishable and the absent-record branch could be
            # deleted with the tests still green (mutation-checked).
            ("absent", "the record could not be written at all", "pass record is missing"),
            ("stale", "the record is from a different release", "does not match this release"),
            ("failed", "the record says the self-test did not pass", "does not match this release"),
            ("corrupt", "the record is a valid object followed by garbage", "not readable JSON"),
        ],
    )
    def test_the_durable_record_must_match_this_release(self, tmp_path, record, why, reason_fragment):
        """Stage A's comment above RUNTIME_SELFTEST_RECORD_FILE says Stage B
        gates on that file, sha-matched. It now does, and not redundantly with
        the in-run verdict: if the record could not be written — no jq, an
        unwritable state dir — then Stage A's own durability claim is false on
        this device, and a later reader would find a migrated clock with no
        evidence for why. Refusing keeps the device and its record consistent,
        and it retries next week.
        """
        r, after, memo = self._run(tmp_path, record=record)
        assert after == self.SAMPLE_ENV, f"env.sh must be byte-identical when {why}"
        assert "MIGRATED" not in r.stdout
        assert memo is not None and memo["result"] == "migration-skipped", r.stdout
        assert reason_fragment in memo["reason"], (
            f"the memo must say WHICH way the record failed, got: {memo['reason']!r}"
        )

    def test_no_images_leaves_env_sh_alone_and_memos_why(self, tmp_path):
        """Migrating a device with no PNG tier would remove the only rung below
        the text renderer — the one change that could turn a render fault into a
        blank panel."""
        r, after, memo = self._run(tmp_path, images=False)
        assert after == self.SAMPLE_ENV, "env.sh must be byte-identical"
        assert "MIGRATED" not in r.stdout
        assert memo is not None and memo["result"] == "migration-skipped"
        assert "fallback" in memo["reason"]

    def test_an_empty_images_metadata_counts_as_absent(self, tmp_path):
        """`images/metadata/` present but empty is what a half-finished cleanup
        leaves, and the painter's fallback globs that directory."""
        install = tmp_path / "install"
        (install / "images" / "metadata").mkdir(parents=True, exist_ok=True)
        r, after, memo = self._run(tmp_path, images=False)
        assert after == self.SAMPLE_ENV
        assert memo is not None and memo["result"] == "migration-skipped"

    def test_a_commented_line_beside_the_active_one_still_migrates(self, tmp_path):
        """The realistic env.sh: `env.sh.sample` documents the flag in comments
        above the live assignment, and update.sh Phase 3 merges sample lines in.
        The count guard must see ONE active assignment there, not two — a
        loosened match refuses to migrate any device with the documentation
        still attached, which is all of them.
        """
        body = (
            "# export LITCLOCK_RUNTIME_RENDER=false  <- what this used to default to\n"
            "export LITCLOCK_RUNTIME_RENDER=false\n"
        )
        r, after, memo = self._run(tmp_path, env_body=body)
        assert "MIGRATED to runtime text rendering" in r.stdout, r.stdout
        assert after == body.replace("\nexport LITCLOCK_RUNTIME_RENDER=false", "\nexport LITCLOCK_RUNTIME_RENDER=true")
        assert after.startswith("# export LITCLOCK_RUNTIME_RENDER=false"), "the comment is untouched"

    def test_a_commented_assignment_is_not_rewritten(self, tmp_path):
        """`env.sh.sample` ships commented examples and this runs over a real
        device's env.sh; rewriting a comment would edit documentation."""
        body = "# export LITCLOCK_RUNTIME_RENDER=false\nexport WEATHER_ENABLED=true\n"
        r, after, memo = self._run(tmp_path, env_body=body)
        assert after == body, "a commented line is prose"
        assert "MIGRATED" not in r.stdout
        assert memo is None, "nothing to migrate is not a negative result"

    def test_two_active_assignments_are_refused(self, tmp_path):
        """A device whose env.sh carries two is not one to guess about.

        Also pins the outer function's GENERIC failure branch — the `else` arm
        of `_runtime_render_migrate`, reached whenever the locked helper returns
        anything but 0 or 75. Asserting only "env.sh unchanged" would not: every
        other guard leaves it unchanged too, so that assertion passes when a
        DIFFERENT guard refuses, and deleting the branch outright left the whole
        suite green (/review 2026-09-23). The memo is the only operator-visible
        evidence this path produces, so the memo is what the test asserts.
        """
        body = "export LITCLOCK_RUNTIME_RENDER=false\nexport LITCLOCK_RUNTIME_RENDER=false\n"
        r, after, memo = self._run(tmp_path, env_body=body)
        assert after == body, "env.sh must be byte-identical"
        assert "found 2" in r.stdout
        assert memo is not None, "the generic failure branch must write a memo"
        assert memo["result"] == "migration-skipped", memo
        assert "rewrite did not complete" in memo["reason"], memo
        assert "the env.sh rewrite failed" in r.stdout, r.stdout

    def test_a_lock_timeout_is_memoed_as_such_and_not_as_a_rewrite_failure(self, tmp_path):
        """rc=75 from `with_env_lock` is its own arm, and says something different.

        A real `flock` is held on the sidecar for the whole wait, which is what
        the PWA's config.py writing env.sh at the same moment looks like. The
        distinction matters to whoever reads the memo: "another writer had it"
        is a retry-next-week condition, while "the rewrite did not complete"
        points at the rewrite itself. Deleting both arms left 307 tests green
        (/review 2026-09-23), so this asserts the rc and the reason, not merely
        that env.sh survived.
        """
        install = tmp_path / "install"
        install.mkdir(exist_ok=True)
        (install / "env.sh").write_text(self.SAMPLE_ENV)
        meta = install / "images" / "metadata"
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "q.png").write_bytes(b"x")
        memo = tmp_path / "memo.json"
        lockfile = install / "env.sh.lock"
        held = tmp_path / "held"

        if subprocess.run(["bash", "-c", "command -v flock"], capture_output=True).returncode != 0:
            pytest.skip("flock(1) unavailable — with_env_lock takes its no-lock fallback")

        # Hold the sidecar, and signal only once it is actually held: starting
        # the holder is not the same as it owning the lock, and racing that
        # would make this test pass for the wrong reason.
        holder = subprocess.Popen(
            ["bash", "-c", f'exec 200>"{lockfile}"; flock 200; : > "{held}"; sleep 30'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.time() + 10
            while not held.exists() and time.time() < deadline:
                time.sleep(0.05)
            assert held.exists(), "the lock holder never acquired the sidecar"
            program = (
                "set -u\n"
                'log_info() { echo "[INFO] $1"; }\n'
                'log_warn() { echo "[WARN] $1"; }\n'
                'atomic_write_file() { printf "%s" "$2" > "$1"; }\n'
                'atomic_remove_file() { rm -f "$1"; }\n'
                f'source "{REPO_ROOT}/scripts/lib/state.sh"\n'
                f"INSTALL_DIR={install}\nRUNTIME_VALIDATION_MEMO_FILE={memo}\n"
                f"RUNTIME_SELFTEST_RECORD_FILE={self._pass_record(tmp_path)}\n"
                "RUNTIME_SELFTEST_RESULT=pass\nLITCLOCK_ENV_LOCK_WAIT=1\n"
                f"{self._fn('_atomic_write_env_sh_finalize_from_body')}"
                f"{self._fn('_runtime_render_migrate_locked')}"
                f"{self._fn('_runtime_validation_memo_write')}"
                f"{self._fn('_runtime_render_migrate')}"
                "_runtime_render_migrate\n"
                'echo "REACHED_END rc=$?"\n'
            )
            r = subprocess.run(["bash", "-c", program], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        finally:
            holder.kill()
            holder.wait(timeout=10)

        assert "REACHED_END rc=0" in r.stdout, r.stdout + r.stderr
        assert (install / "env.sh").read_text() == self.SAMPLE_ENV, "env.sh must be byte-identical"
        assert "MIGRATED" not in r.stdout, r.stdout
        loaded = json.loads(memo.read_text())
        assert loaded["result"] == "migration-skipped", loaded
        assert str(loaded["rc"]) == "75", loaded
        assert "locked by another writer" in loaded["reason"], loaded
        # The two arms must stay distinguishable — this is the whole point.
        assert "rewrite did not complete" not in loaded["reason"], loaded

    def test_the_under_lock_recheck_catches_a_writer_that_raced_the_outer_guard(self, tmp_path):
        """`_runtime_render_migrate_locked` re-reads and re-counts, and that is
        not redundant with the outer guard in `_runtime_render_migrate`.

        The outer grep runs BEFORE `with_env_lock`. Another writer — the PWA's
        `config.py`, a `reset-setup.sh` run — can change env.sh in that window,
        which is the whole reason the lock exists. The re-check is what stops
        the migration acting on what the file said a moment ago.

        Unreachable with a static file, so the race is staged: `with_env_lock`
        is replaced by a stub that rewrites env.sh into a bare-only form and
        THEN calls the target, which is what losing that race looks like.
        """
        install = tmp_path / "install"
        install.mkdir(exist_ok=True)
        (install / "env.sh").write_text(self.SAMPLE_ENV)
        meta = install / "images" / "metadata"
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "q.png").write_bytes(b"x")
        memo = tmp_path / "memo.json"
        raced = "LITCLOCK_RUNTIME_RENDER=false\n"  # bare: never reaches the painter
        program = (
            "set -u\n"
            'log_info() { echo "[INFO] $1"; }\n'
            'log_warn() { echo "[WARN] $1"; }\n'
            'atomic_write_file() { printf "%s" "$2" > "$1"; }\n'
            'atomic_remove_file() { rm -f "$1"; }\n'
            f'source "{REPO_ROOT}/scripts/lib/state.sh"\n'
            # the racing writer, standing in for the lock helper
            f'with_env_lock() {{ printf %s {shlex.quote(raced)} > "{install}/env.sh"; "$@"; }}\n'
            f"INSTALL_DIR={install}\nRUNTIME_VALIDATION_MEMO_FILE={memo}\n"
            f"RUNTIME_SELFTEST_RECORD_FILE={self._pass_record(tmp_path)}\n"
            "RUNTIME_SELFTEST_RESULT=pass\n"
            f"{self._fn('_atomic_write_env_sh_finalize_from_body')}"
            f"{self._fn('_runtime_render_migrate_locked')}"
            f"{self._fn('_runtime_validation_memo_write')}"
            f"{self._fn('_runtime_render_migrate')}"
            "_runtime_render_migrate\n"
        )
        r = subprocess.run(["bash", "-c", program], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        after = (install / "env.sh").read_text()
        assert after == raced, f"the raced file must be left as the other writer wrote it:\n{after}"
        assert "MIGRATED" not in r.stdout, r.stdout
        assert "0 exported =false" in r.stdout, r.stdout

    def test_a_missing_env_sh_is_a_silent_no_op(self, tmp_path):
        r, after, memo = self._run(tmp_path, env_file=False)
        assert after is None and "MIGRATED" not in r.stdout
        assert memo is None

    def test_an_already_true_device_writes_no_memo(self, tmp_path):
        body = "export LITCLOCK_RUNTIME_RENDER=true\n"
        r, after, memo = self._run(tmp_path, env_body=body)
        assert after == body and memo is None

    # ── the call site ────────────────────────────────────────────────────

    def test_the_flip_is_called_after_the_selftest_inside_the_keep_arm(self):
        """Position is the safety property, as it is for the validator above: a
        smoke failure git-resets the tree, so a flip outside the KEEP arm could
        migrate a device onto code that was just reverted."""
        body = UPDATE_SH.read_text()
        keep = body.index('if [[ "$smoke_rc" -eq 0 ]]; then\n    log_info "Smoke test passed"')
        else_at = body.index('else\n    log_error "Smoke test failed', keep)
        selftest_call = body.index("        _runtime_render_selftest\n", keep)
        migrate_call = body.index("        _runtime_render_migrate\n", keep)
        assert keep < selftest_call < migrate_call < else_at
        assert "ROLLBACK_MODE" in body[keep:selftest_call].rsplit("if [[", 1)[-1]

    # ── the review's findings, each with an executed test ────────────────

    def test_another_assignment_with_a_different_value_is_refused(self, tmp_path):
        """`=false` followed by `=0` used to migrate "successfully" while the
        effective value stayed OFF — the last assignment wins when the file is
        sourced, so the flip was cosmetic and the memo was cleared as if it had
        worked. The count now looks at every ACTIVE assignment, not just the
        `=false` ones."""
        body = "export LITCLOCK_RUNTIME_RENDER=false\nexport LITCLOCK_RUNTIME_RENDER=0\n"
        r, after, _ = self._run(tmp_path, env_body=body)
        assert after == body, "env.sh must be byte-identical"
        assert "MIGRATED" not in r.stdout
        assert "found 2 active" in r.stdout, r.stdout

    def test_a_true_assignment_beside_the_false_one_is_refused(self, tmp_path):
        body = "export LITCLOCK_RUNTIME_RENDER=false\nexport LITCLOCK_RUNTIME_RENDER=true\n"
        r, after, _ = self._run(tmp_path, env_body=body)
        assert after == body
        assert "MIGRATED" not in r.stdout

    def test_a_failed_transformation_is_not_committed(self, tmp_path):
        """A `sed` killed mid-stream emits a PARTIAL body. It differs from the
        original, so the "did it change anything" check accepted it and the
        finalizer renamed a truncated env.sh into place, reported as a
        successful migration. An atomic rename protects against a torn write,
        not against committing a complete-looking truncated body.

        `sed` is shadowed by a function that prints one line and exits 137, the
        shape a SIGKILL leaves.
        """
        install = tmp_path / "install"
        install.mkdir(exist_ok=True)
        (install / "env.sh").write_text(self.SAMPLE_ENV)
        meta = install / "images" / "metadata"
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "q.png").write_bytes(b"x")
        memo = tmp_path / "memo.json"
        program = (
            "set -u\n"
            'log_info() { echo "[INFO] $1"; }\n'
            'log_warn() { echo "[WARN] $1"; }\n'
            'atomic_write_file() { printf "%s" "$2" > "$1"; }\n'
            'atomic_remove_file() { rm -f "$1"; }\n'
            f'source "{REPO_ROOT}/scripts/lib/state.sh"\n'
            f"INSTALL_DIR={install}\nRUNTIME_VALIDATION_MEMO_FILE={memo}\n"
            f"RUNTIME_SELFTEST_RECORD_FILE={self._pass_record(tmp_path)}\n"
            "RUNTIME_SELFTEST_RESULT=pass\n"
            # the mid-stream kill
            'sed() { echo "export WEATHER_ENABLED=true"; return 137; }\n'
            f"{self._fn('_atomic_write_env_sh_finalize_from_body')}"
            f"{self._fn('_runtime_render_migrate_locked')}"
            f"{self._fn('_runtime_validation_memo_write')}"
            f"{self._fn('_runtime_render_migrate')}"
            "_runtime_render_migrate\n"
            'echo "REACHED_END rc=$?"\n'
        )
        r = subprocess.run(["bash", "-c", program], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        after = (install / "env.sh").read_text()
        assert after == self.SAMPLE_ENV, f"a partial body was committed:\n{after}"
        assert "MIGRATED" not in r.stdout
        assert "sed exited 137" in r.stdout, r.stdout

    def test_a_transformation_that_loses_lines_is_not_committed(self, tmp_path):
        """The line-count belt, for a `sed` that truncates and still exits 0."""
        install = tmp_path / "install"
        install.mkdir(exist_ok=True)
        (install / "env.sh").write_text(self.SAMPLE_ENV)
        meta = install / "images" / "metadata"
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "q.png").write_bytes(b"x")
        program = (
            "set -u\n"
            'log_info() { echo "[INFO] $1"; }\n'
            'log_warn() { echo "[WARN] $1"; }\n'
            'atomic_write_file() { printf "%s" "$2" > "$1"; }\n'
            'atomic_remove_file() { rm -f "$1"; }\n'
            f'source "{REPO_ROOT}/scripts/lib/state.sh"\n'
            f"INSTALL_DIR={install}\nRUNTIME_VALIDATION_MEMO_FILE={tmp_path / 'memo.json'}\n"
            f"RUNTIME_SELFTEST_RECORD_FILE={self._pass_record(tmp_path)}\n"
            "RUNTIME_SELFTEST_RESULT=pass\n"
            'sed() { echo "export LITCLOCK_RUNTIME_RENDER=true"; return 0; }\n'
            f"{self._fn('_atomic_write_env_sh_finalize_from_body')}"
            f"{self._fn('_runtime_render_migrate_locked')}"
            f"{self._fn('_runtime_validation_memo_write')}"
            f"{self._fn('_runtime_render_migrate')}"
            "_runtime_render_migrate\n"
        )
        r = subprocess.run(["bash", "-c", program], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        assert (install / "env.sh").read_text() == self.SAMPLE_ENV
        assert "changed the line count" in r.stdout, r.stdout

    def test_the_sample_stays_false_because_phase_3_backfills_it(self):
        """THE finding that held Stage B back a release.

        `env.sh.sample` is not the fresh-flash default — it is what Phase 3
        copies VERBATIM onto any existing device missing the key. A `true` here
        switches on an old clock with none of the five guards run, before the
        smoke gate, with no revert path. litclock-dev#783 records devices born
        missing up to ten of these knobs.
        """
        sample = (REPO_ROOT / "env.sh.sample").read_text()
        assert re.search(r"^export LITCLOCK_RUNTIME_RENDER=false$", sample, re.M), (
            "the sample is the BACKFILL source for existing devices and must stay false"
        )

    def test_the_seeder_defaults_to_false_and_only_a_fresh_flash_asks_for_true(self, tmp_path):
        """`env_sh_defaults()` is NOT a fresh-flash-only seeder, which is what
        the first fix for the above assumed.

        `reset-setup.sh` and `prepare-for-cloning.sh` both call it on an
        EXISTING device, and neither removes `.runtime-render-validated` — they
        clear the self-test record, not the marker. So a `true` default handed a
        reset clock runtime render with none of Stage B's guards run, including
        a clock whose self-test had FAILED, which is exactly the device the
        guards exist to hold back (review, round 4).

        Executed against the real helper rather than grepped, because the value
        is a parameter now and a source-text check would pin the wrong thing.
        """
        state_sh = REPO_ROOT / "scripts" / "lib" / "state.sh"

        def seed(*args):
            call = "env_sh_defaults " + " ".join(f'"{a}"' for a in args) if args else "env_sh_defaults"
            r = subprocess.run(
                ["bash", "-c", f'. "{state_sh}"\n{call}'],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert r.returncode == 0, r.stderr
            return r.stdout

        assert "export LITCLOCK_RUNTIME_RENDER=false" in seed(), "the bare default must be conservative"
        assert "export LITCLOCK_RUNTIME_RENDER=false" in seed("en"), "a language-only call is a reset caller"
        assert "export LITCLOCK_RUNTIME_RENDER=true" in seed("", "true"), "a fresh flash asks for text"
        # A caller that passes nonsense gets the safe value, not the nonsense.
        assert "export LITCLOCK_RUNTIME_RENDER=false" in seed("", "yes")

    def test_the_reset_and_cloning_callers_take_the_conservative_default(self):
        """Neither may start asking for `true`: both run on an existing device
        whose self-test may have failed, and update.sh migrates it properly on
        the next tick if it can render."""
        for name in ("reset-setup.sh", "prepare-for-cloning.sh"):
            body = (REPO_ROOT / "scripts" / name).read_text()
            for call in re.findall(r"env_sh_defaults[^\n)]*", body):
                assert "true" not in call, f"{name} must not seed runtime render on an existing device: {call!r}"

    def test_first_boot_asks_for_true_in_both_of_its_seeders(self):
        """first-boot has two: the helper call and the state.sh-missing heredoc.
        litclock-dev#783 found the second one missed by a guard that only saw the first."""
        body = (REPO_ROOT / "scripts" / "first-boot.sh").read_text()
        assert re.search(r'env_sh_defaults\s+""\s+true', body), "the helper call must ask for text render"
        assert re.search(r"^export LITCLOCK_RUNTIME_RENDER=true$", body, re.M), (
            "the fallback heredoc seeds a fresh device too"
        )

    def test_a_bare_assignment_beside_the_export_is_refused(self, tmp_path):
        """`export ...=false` plus a later bare `...=0` used to migrate
        "successfully" while the effective value stayed off — re-assigning does
        not clear the export attribute, so the bare line wins. A bare assignment
        is not an invented shape: the PWA's own writer treats `export` as
        optional (`_KV_PATTERN`, src/config.py)."""
        body = "export LITCLOCK_RUNTIME_RENDER=false\nLITCLOCK_RUNTIME_RENDER=0\n"
        r, after, _ = self._run(tmp_path, env_body=body)
        assert after == body, "env.sh must be byte-identical"
        assert "found 2 active" in r.stdout, r.stdout

    def test_a_lone_bare_assignment_is_refused_because_it_never_reached_the_painter(self, tmp_path):
        """Migrating a bare assignment would be a no-op reported as success.

        `runtheclock.sh` does `source ./env.sh` with NO `set -a`, so a bare
        assignment never enters the painter's environment — measured: a child
        process sees the variable UNSET. A device with a lone bare `=false` was
        therefore not honouring the flag either way, and rewriting it to a bare
        `=true` changes the file and nothing else while clearing the memo and
        logging a migration. Refused with a reason instead.

        Bare assignments are still COUNTED, because a bare one beside an
        exported one does change the exported value — that case is covered by
        `test_a_bare_assignment_beside_the_export_is_refused`.
        """
        body = "LITCLOCK_RUNTIME_RENDER=false\nexport WEATHER_ENABLED=true\n"
        r, after, memo = self._run(tmp_path, env_body=body)
        assert "MIGRATED" not in r.stdout, r.stdout
        assert after == body, "env.sh must be byte-identical"
        # SILENT, and that is what pins the OUTER guard rather than the inner
        # count: a device with no exported assignment was never eligible, so it
        # is not a "could have migrated and did not" case. Loosening the outer
        # guard to accept bare forms pushes this into the locked helper, which
        # refuses with a warning and a memo — same file, different diagnosis.
        assert memo is None, f"a device that was never eligible must not be memoed: {memo}"
        assert "Stage B:" not in r.stdout, r.stdout

    def test_a_bare_assignment_really_does_not_reach_a_child(self, tmp_path):
        """The measurement the refusal above rests on, executed rather than
        asserted in prose — and the control beside it, since `export` must."""
        env = tmp_path / "env.sh"
        results = {}
        for label, line in (("bare", "FOO_X=1\n"), ("export", "export FOO_X=1\n")):
            env.write_text(line)
            r = subprocess.run(
                ["bash", "-c", f". \"{env}\"; python3 -c \"import os;print(os.environ.get('FOO_X','<unset>'))\""],
                capture_output=True,
                text=True,
                timeout=30,
            )
            results[label] = r.stdout.strip()
        assert results["bare"] == "<unset>", results
        assert results["export"] == "1", results

    def test_phase_3_sees_an_indented_existing_assignment(self):
        """A tab-indented assignment went undetected, so Phase 3 appended the
        sample's line beside it and the sample's value silently became the
        effective one. Not harmless before Stage B either: the sample's example
        WEATHER_LATITUDE/LONGITUDE already differed from what a device is seeded
        with. Stage B is what surfaced it, not the first divergence."""
        body = UPDATE_SH.read_text()
        assert 'grep -q "^[[:space:]#]*export[[:space:]]\\+${varname}="' in body, (
            "Phase 3's existing-key detection must allow leading whitespace"
        )

    def test_the_finalizer_sets_mode_before_ownership(self):
        """Ownership first installs an unreadable env.sh: the mktemp file is
        pi-owned 0600, `sudo chown root:root` succeeds, the unprivileged `chmod
        644` then fails on a file pi no longer owns, and the rename replaces a
        world-readable config with a root-owned 0600 one. Both failures are
        swallowed, so the caller reports success."""
        state = (REPO_ROOT / "scripts" / "lib" / "state.sh").read_text()
        fn = state[state.index("_atomic_write_env_sh_finalize() {") :]
        fn = fn[: fn.index("\n}\n")]
        assert fn.index('chmod "$mode"') < fn.index('chown "$owner"'), (
            "mode must be set while pi still owns the staging file"
        )


class TestMemoTokensRoundTripToThePWA:
    """The shell writes the memo; `routes/status.py` decides whether to show it.

    Those two vocabularies live in different languages and nothing connected
    them, so Stage B shipped `migration-skipped` that `RUNTIME_VALIDATION_RESULTS`
    did not accept: every refusal was parsed, rejected and surfaced as `null` —
    byte-identical to what a healthy device sends, on the one feature whose
    whole safety argument is that a refusal would be visible (/review
    2026-09-23, adversarial). Grepping either side alone cannot catch that; the
    test has to compare them.
    """

    # Every position a memo token can enter from. Two of these are forwarding
    # wrappers (`_defer_runtime_validation`, `_validation_fits_remaining_budget`)
    # that take the token as $2 and hand it to the writer as `"$token"`, so
    # scanning only the writer's call sites would miss `selftest-deferred`
    # entirely and make this test weaker than it looks.
    TOKEN_SINKS = (
        r"_runtime_validation_memo_write\s+(\S+)",
        r"_defer_runtime_validation\s+(?:\"[^\"]*\"|\S+)\s+(\S+)",
        r"_validation_fits_remaining_budget\s+(?:\"[^\"]*\"|\S+)\s+(\S+)",
    )
    # The wrappers' own `${2:-<default>}` fallbacks are live tokens too: a caller
    # that omits $2 writes the default.
    DEFAULT_TOKEN_RE = r"local\s+(?:\w+=\S+\s+)?token=\"\$\{2:-([a-z-]+)\}\""

    @classmethod
    def _tokens_written_by_the_shell(cls):
        """Every literal memo token reachable in update.sh.

        A forwarded `"$token"` is expected at the two wrapper sites and is
        resolved through their callers and defaults instead. Any OTHER
        non-literal is refused rather than skipped quietly, because a token the
        extraction cannot see is a token this test silently stops guarding.
        """
        body = UPDATE_SH.read_text()
        tokens = set()
        for pattern in cls.TOKEN_SINKS:
            for raw in re.findall(pattern, body):
                if raw in ('"$token"', "$token"):
                    continue  # forwarded; resolved via callers + defaults below
                assert not raw.startswith(("$", '"', "'")), (
                    f"non-literal memo token {raw!r} — the round-trip check cannot see it; "
                    "keep the token a bare literal at the call site, or teach TOKEN_SINKS "
                    "how to resolve the new wrapper"
                )
                tokens.add(raw)
        defaults = re.findall(cls.DEFAULT_TOKEN_RE, body)
        assert defaults, "no `${2:-<token>}` wrapper default found — did update.sh refactor?"
        tokens.update(defaults)
        return tokens

    def test_every_token_the_shell_writes_is_renderable_by_the_pwa(self):
        from control_server.routes.status import RUNTIME_VALIDATION_RESULTS

        written = self._tokens_written_by_the_shell()
        assert written, "found no memo-write call sites — did update.sh refactor?"
        missing = sorted(written - set(RUNTIME_VALIDATION_RESULTS))
        assert not missing, (
            f"update.sh writes {missing} but routes/status.py's "
            f"RUNTIME_VALIDATION_RESULTS does not accept them, so those memos reach "
            f"the PWA as null — indistinguishable from a healthy device. "
            f"Add them to RUNTIME_VALIDATION_RESULTS."
        )

    def test_the_stage_b_token_specifically_round_trips(self):
        """Named on its own so a regression points at the right release."""
        from control_server.routes.status import RUNTIME_VALIDATION_RESULTS

        assert "migration-skipped" in self._tokens_written_by_the_shell()
        assert "migration-skipped" in RUNTIME_VALIDATION_RESULTS

    def test_a_refusal_memo_actually_renders(self, tmp_path):
        """End to end through the real reader, not just the allowlist.

        Membership is necessary and not sufficient: the reader also gates on
        `at_unix`, so a token in the set whose memo the reader still drops would
        pass the check above and fail the owner.
        """
        from control_server.routes.status import _resolve_runtime_validation_memo

        memo = tmp_path / "runtime-render-validation.json"
        memo.write_text(
            json.dumps(
                {
                    "result": "migration-skipped",
                    "rc": None,
                    "reason": "the PNG fallback tier (images/metadata) is absent",
                    "sha": "0" * 40,
                    "at_unix": 1790000000,
                }
            )
        )
        resolved = _resolve_runtime_validation_memo(memo_file=memo)
        assert resolved is not None, "a Stage B refusal must reach /api/status, not vanish"
        assert resolved.get("result") == "migration-skipped", resolved


class TestTheSmokeGateRendersTheTierTheDeviceIsOn:
    """The smoke gate is the only thing that can revert a release.

    Before litclock-dev#871 Stage B it ran the painter with no `env.sh`, so it
    always exercised the PNG tier. That was harmless while the whole fleet was
    on PNGs and became a hole the moment Stage B migrated them: a regression in
    the text renderer would render a PNG here, pass, and never be reverted —
    and `src/literary_clock.py` is not one of the six proof inputs, so the
    marker-revoke block does not cover it either (/review 2026-09-23).
    """

    SMOKE_START = '        [[ -f "$INSTALL_DIR/env.sh" ]] && { source "$INSTALL_DIR/env.sh" 2>/dev/null || true; }'
    SMOKE_END = "    ) | sed 's/^/[smoke] /'"

    def test_the_gate_is_the_one_that_reverts(self):
        """Anchor: the block we lift is the one whose status becomes smoke_rc."""
        body = UPDATE_SH.read_text()
        end = body.index(self.SMOKE_END)
        after = body[end : end + 200]
        assert 'smoke_rc="${PIPESTATUS[0]}"' in after, (
            "the sourced block must be the one feeding smoke_rc, or this test guards a different gate"
        )

    @classmethod
    def _smoke_sequence(cls):
        """The scratch-dir setup, the subshell, smoke_rc, and the cleanup.

        From the `mktemp` line through the `rm` line, so the isolation added in
        the port /review (weather off, a throwaway frame directory) is executed
        as written, not re-typed here.
        """
        body = UPDATE_SH.read_text()
        start = body.index('    smoke_render_dir="$(mktemp -d 2>/dev/null)"')
        rm = body.index('rm -rf -- "$smoke_render_dir"', start)
        end = body.index("\n", rm)
        seq = body[start:end]
        assert cls.SMOKE_START in seq and 'smoke_rc="${PIPESTATUS[0]}"' in seq, seq
        return seq

    def _run_block(self, tmp_path, env_body, mktemp_fails=False, painter_rc=0):
        install = tmp_path / "install"
        install.mkdir(exist_ok=True)
        (install / "env.sh").write_text(env_body)
        # A stub painter that reports what it was actually handed.
        stub = tmp_path / "python3"
        stub.write_text(
            "#!/bin/bash\n"
            "echo \"SAW=${LITCLOCK_RUNTIME_RENDER:-<unset>}\"\n"
            "echo \"WEATHER=${WEATHER_ENABLED:-<unset>}\"\n"
            "echo \"DIR=${LITCLOCK_RUNTIME_RENDER_DIR:-<unset>}\"\n"
            "[[ -n \"${LITCLOCK_RUNTIME_RENDER_DIR:-}\" && -d \"$LITCLOCK_RUNTIME_RENDER_DIR\" ]]"
            " && echo DIR_EXISTS_DURING\n"
            f"exit {painter_rc}\n"
        )
        stub.chmod(0o755)
        program = (
            f"INSTALL_DIR={install}\nPYTHON={stub}\n"
            'log_warn() { echo "[WARN] $1"; }\n'
            + ("mktemp() { return 1; }\n" if mktemp_fails else "")
            # Prove the subshell isolation at the same time.
            + "LITCLOCK_RUNTIME_RENDER=outer\n"
            f"{self._smoke_sequence()}\n"
            'echo "rc=$smoke_rc"\n'
            'echo "PARENT_AFTER=$LITCLOCK_RUNTIME_RENDER"\n'
            'echo "SCRATCH=${smoke_render_dir:-<none>}"\n'
            '[[ -n "${smoke_render_dir:-}" && -e "$smoke_render_dir" ]] && echo SCRATCH_LEFT_BEHIND\n'
        )
        # A scrubbed environment: inherited from the runner, WEATHER_ENABLED or
        # LITCLOCK_RUNTIME_RENDER_DIR would satisfy (or break) the assertions
        # below for reasons that have nothing to do with the gate (port /review).
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("LITCLOCK_", "WEATHER_")) and k != "ROLLBACK_MODE"
        }
        return subprocess.run(
            ["bash", "-c", program], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, env=env
        )

    def test_a_migrated_device_smoke_tests_the_text_tier(self, tmp_path):
        r = self._run_block(tmp_path, "export LITCLOCK_RUNTIME_RENDER=true\n")
        assert "[smoke] SAW=true" in r.stdout, r.stdout + r.stderr
        assert "rc=0" in r.stdout, r.stdout

    def test_an_unmigrated_device_still_smoke_tests_the_png_tier(self, tmp_path):
        r = self._run_block(tmp_path, "export LITCLOCK_RUNTIME_RENDER=false\n")
        assert "[smoke] SAW=false" in r.stdout, r.stdout + r.stderr

    def test_env_sh_does_not_leak_into_the_updater(self, tmp_path):
        """A subshell, not a bare source: update.sh must not inherit env.sh."""
        r = self._run_block(tmp_path, "export LITCLOCK_RUNTIME_RENDER=true\n")
        assert "PARENT_AFTER=outer" in r.stdout, (
            "env.sh leaked into the updater's own shell:\n" + r.stdout
        )

    def test_a_broken_env_sh_does_not_fail_the_gate(self, tmp_path):
        """The asymmetry that matters: a false RED reverts a healthy release.

        Since litclock-dev#865 the smoke-revert path blocks the release it fled,
        so a gate that a corrupt env.sh could fail would hold every release off
        every such device until the next one ships, and fail that one too.
        """
        r = self._run_block(tmp_path, "export LITCLOCK_RUNTIME_RENDER=true\nthis is ( not valid shell\n")
        assert "rc=0" in r.stdout, (
            "a syntactically broken env.sh must not make the smoke gate revert the release:\n"
            + r.stdout + r.stderr
        )


    @pytest.mark.parametrize("painter_rc", [0, 3, 124])
    def test_the_painters_verdict_reaches_smoke_rc_and_the_scratch_dir_goes(self, tmp_path, painter_rc):
        """A failing painter must reach smoke_rc THROUGH the new lines around the
        pipeline — a mutant that overwrote PIPESTATUS survived every test while
        the stub always exited 0 — and the scratch directory must go either way."""
        r = self._run_block(tmp_path, "export LITCLOCK_RUNTIME_RENDER=true\n", painter_rc=painter_rc)
        assert f"rc={painter_rc}\n" in r.stdout, r.stdout + r.stderr
        assert "SCRATCH_LEFT_BEHIND" not in r.stdout, r.stdout

    def test_weather_is_off_even_when_env_sh_never_mentions_it(self, tmp_path):
        """The case that needs the override itself: nothing in env.sh to undo."""
        r = self._run_block(tmp_path, "export LITCLOCK_RUNTIME_RENDER=true\n")
        assert "[smoke] WEATHER=false" in r.stdout, r.stdout + r.stderr

    def test_a_readonly_env_sh_cannot_defeat_the_overrides(self, tmp_path):
        """env.sh is sourced first, so a `readonly` there made `export` fail
        and the painter got the owner's values (reproduced in review)."""
        r = self._run_block(
            tmp_path,
            "export LITCLOCK_RUNTIME_RENDER=true\n"
            "export WEATHER_ENABLED=true\nreadonly WEATHER_ENABLED\n"
            "export LITCLOCK_RUNTIME_RENDER_DIR=/run/litclock\nreadonly LITCLOCK_RUNTIME_RENDER_DIR\n",
        )
        assert "[smoke] WEATHER=false" in r.stdout, r.stdout + r.stderr
        assert "[smoke] DIR=/run/litclock" not in r.stdout, r.stdout
        assert "[smoke] DIR_EXISTS_DURING" in r.stdout, r.stdout
        assert "rc=0" in r.stdout, r.stdout

    def test_weather_is_forced_off_whatever_env_sh_says(self, tmp_path):
        """litclock-dev#871 port /review: sourcing env.sh gave the gate a
        location, so it started fetching live weather, and a slow provider could
        push the dry-run past its 60s bound and revert a healthy release."""
        r = self._run_block(
            tmp_path, "export LITCLOCK_RUNTIME_RENDER=true\nexport WEATHER_ENABLED=true\n"
        )
        assert "[smoke] WEATHER=false" in r.stdout, r.stdout + r.stderr
        assert "rc=0" in r.stdout, r.stdout

    def test_the_frame_goes_to_a_throwaway_directory_that_is_removed(self, tmp_path):
        """On the text tier the dry-run writes current-quote.png, the live frame
        the support bundle reports. The gate's frame must not land there."""
        r = self._run_block(tmp_path, "export LITCLOCK_RUNTIME_RENDER=true\n")
        dirs = [ln.split("DIR=", 1)[1] for ln in r.stdout.splitlines() if "[smoke] DIR=" in ln]
        assert dirs and dirs[0] not in ("<unset>", "/run/litclock"), r.stdout + r.stderr
        assert "[smoke] DIR_EXISTS_DURING" in r.stdout, "the scratch directory must exist while the painter runs"
        assert f"SCRATCH={dirs[0]}" in r.stdout, r.stdout
        assert "SCRATCH_LEFT_BEHIND" not in r.stdout, "the scratch directory must be removed after the gate"

    def test_no_scratch_directory_still_runs_the_gate(self, tmp_path):
        """The gate is the only revert path, so it must not be skipped for want
        of a scratch directory: it runs into the live path and says so."""
        r = self._run_block(tmp_path, "export LITCLOCK_RUNTIME_RENDER=true\n", mktemp_fails=True)
        assert "[smoke] SAW=true" in r.stdout, r.stdout + r.stderr
        assert "[smoke] DIR=<unset>" in r.stdout, r.stdout
        assert "[smoke] WEATHER=false" in r.stdout, r.stdout
        assert "could not create a scratch directory" in r.stdout, r.stdout
        assert "rc=0" in r.stdout, r.stdout


class TestTheRewriteHasNoUncheckedProducer:
    """`$?` on a pipeline is its LAST element, so only the consumer was checked.

    The fix for a killed `sed` (take `$?`, not `PIPESTATUS[1]`) left the
    `printf` feeding it unguarded. A producer dying after a partial final line
    is invisible three times over: sed exits 0 on the short input, sed supplies
    the missing newline so the line-count belt matches, and the body differs
    from the original so the "changed nothing" check passes. Reproduced
    2026-09-23 committing a truncated `OPENWEATHERMAP_APIKEY`.
    """

    @staticmethod
    def _rewrite_line():
        for line in UPDATE_SH.read_text().splitlines():
            s = line.strip()
            if s.startswith("new=$(") and "LITCLOCK_RUNTIME_RENDER=false" in s:
                return s
        raise AssertionError("the env.sh rewrite line is gone — did update.sh refactor?")

    def test_the_transformation_is_not_fed_by_a_pipeline(self):
        line = self._rewrite_line()
        assert "|" not in line, (
            "the rewrite is fed by a pipeline again; $? cannot see the producer, so a "
            "producer killed mid-stream commits a truncated env.sh and reports success:\n"
            f"  {line}"
        )
        assert "<<<" in line, f"expected a herestring feed, got:\n  {line}"

    def test_a_killed_producer_is_what_this_prevents(self):
        """The failure the shape change removes, demonstrated on the OLD shape.

        Kept executable so the reason survives: without it, a future reader
        sees only a style preference for `<<<` and may pipe it back.
        """
        old_shape = r"""
body=$'export A=1\nexport LITCLOCK_RUNTIME_RENDER=false\nexport SECRET=original-secret'
flip='s/^([[:space:]]*export[[:space:]]+)LITCLOCK_RUNTIME_RENDER=false[[:space:]]*$/\1LITCLOCK_RUNTIME_RENDER=true/'
new=$( { printf '%s' $'export A=1\nexport LITCLOCK_RUNTIME_RENDER=false\nexport SECRET=original-'; kill -9 $BASHPID; } \
  | sed -E "$flip")
rc=$?
b=$(printf '%s\n' "$body" | wc -l); a=$(printf '%s\n' "$new" | wc -l)
echo "rc=$rc lines=$b/$a changed=$([[ "$new" == "$body" ]] && echo no || echo yes)"
printf '%s\n' "$new" | tail -1
"""
        r = subprocess.run(["bash", "-c", old_shape], capture_output=True, text=True, timeout=60)
        assert "rc=0" in r.stdout, r.stdout + r.stderr
        assert "lines=3/3" in r.stdout, f"the line-count belt caught it after all:\n{r.stdout}"
        assert "changed=yes" in r.stdout, r.stdout
        assert "SECRET=original-\n" in r.stdout, (
            f"expected the truncated secret the old shape committed:\n{r.stdout}"
        )

    def test_the_herestring_feeds_sed_the_same_bytes_printf_did(self):
        """`<<<` must append exactly the newline `printf '%s\\n'` did.

        `before_lines` is still computed with `printf '%s\\n' "$body"`, so a
        feed that differed by a trailing newline would make the belt compare
        mismatched shapes and refuse every migration.
        """
        for body in ("a\nb\nc", "a\nb\nc\n", "single", ""):
            prog = (
                f"body={shlex.quote(body)}\n"
                'p=$(printf "%s\\n" "$body" | cat | od -c | md5sum)\n'
                'h=$(cat <<<"$body" | od -c | md5sum)\n'
                '[[ "$p" == "$h" ]] && echo SAME || echo DIFF\n'
                'printf "%s\\n" "$body" | od -c\n'
                'cat <<<"$body" | od -c\n'
            )
            r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=30)
            assert "SAME" in r.stdout, f"feed differs for {body!r}:\n{r.stdout}"


class TestAbandonedEnvStagingFilesAreNotShipped:
    """`mktemp "${dest}.XXXXXX"` beside env.sh is a full unredacted copy.

    Every in-script failure arm removes it. A SIGKILL or power loss inside the
    printf -> chmod -> chown -> mv window does not, and `with_env_lock` runs the
    writer in a subshell where bash has reset the caller's traps to default, so
    the signal handler does not cover it either. It is untracked (so
    `git reset --hard` never removes it), was unignored (so it could be
    committed), and neither the clone-prep credential gate nor the gift wipe
    looked at it — so the previous owner's API key and home coordinates shipped
    on every clone (/review 2026-09-23, adversarial).
    """

    def test_the_staging_shape_is_gitignored(self):
        r = subprocess.run(
            ["git", "check-ignore", "--no-index", "env.sh.A1b2C3"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0, "env.sh.<mktemp suffix> must be gitignored; it holds the owner's credentials"

    def test_the_tracked_sample_is_not_swept_up_by_that_pattern(self):
        """`sample` is also six characters.

        Harmless while env.sh.sample stays tracked, because ignore rules do not
        apply to tracked files — and a silent trap the moment it is removed and
        re-added, which is exactly the kind of latency this repo keeps finding.
        """
        r = subprocess.run(
            ["git", "check-ignore", "--no-index", "-v", "env.sh.sample"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
        assert "!env.sh.sample" in r.stdout, (
            "env.sh.sample must be explicitly re-included, not left to tracked-status luck:\n" + r.stdout
        )

    def test_clone_prep_refuses_rather_than_certifying_the_card(self):
        body = (REPO_ROOT / "scripts" / "prepare-for-cloning.sh").read_text()
        assert "env.sh.??????" in body, "the clone-prep credential gate does not look for staging files"
        idx = body.index("env.sh.??????")
        after = body[idx : idx + 1200]
        assert "_abort_env_credentials" in after, (
            "clone prep must ABORT on a staging file, not delete it quietly: its job is to "
            "certify the card, and a staging file means a writer died mid-write"
        )
        assert "env.sh.sample" in after, "the six-character tracked sample must be excluded by name"

    def test_gift_mode_sweeps_them_before_writing_defaults(self):
        body = (REPO_ROOT / "scripts" / "reset-setup.sh").read_text()
        assert "env.sh.??????" in body, "the gift wipe does not sweep staging files"
        idx = body.index("env.sh.??????")
        wipe = body.index('atomic_write_env_sh "$INSTALL_DIR/env.sh" "$DEFAULTS"')
        assert idx < wipe, "the sweep must run BEFORE the wipe, or the writer can stage a fresh one after it"

    def test_the_gate_actually_fires_on_a_planted_staging_file(self, tmp_path):
        """Executed, because a grep for the glob proves only that it was typed."""
        install = tmp_path / "litclock"
        install.mkdir()
        (install / "env.sh").write_text("export WEATHER_ENABLED=true\n")
        (install / "env.sh.A1b2C3").write_text("export OPENWEATHERMAP_APIKEY=leaked-key\n")
        (install / "env.sh.sample").write_text("export WEATHER_ENABLED=true\n")
        prog = f'''
INSTALL_DIR={install}
shopt -s nullglob
_ENV_STAGING=("$INSTALL_DIR"/env.sh.??????)
shopt -u nullglob
_ENV_STAGING_REAL=()
for _s in "${{_ENV_STAGING[@]}}"; do
    [[ "$(basename "$_s")" == "env.sh.sample" ]] && continue
    _ENV_STAGING_REAL+=("$_s")
done
echo "COUNT=${{#_ENV_STAGING_REAL[@]}}"
printf 'FOUND=%s\\n' "${{_ENV_STAGING_REAL[@]}}"
'''
        r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=30)
        assert "COUNT=1" in r.stdout, f"expected exactly the planted staging file:\n{r.stdout}"
        assert "env.sh.A1b2C3" in r.stdout, r.stdout
        assert "env.sh.sample" not in r.stdout, f"the tracked sample must not be flagged:\n{r.stdout}"
