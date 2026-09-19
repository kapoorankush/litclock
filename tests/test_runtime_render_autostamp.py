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

import json
import os
import re
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
            f"{self._budget_helpers()}"
            # Stub AFTER the lifted helpers so the stub wins; the guard itself
            # is the real one.
            f"{elapsed_stub}"
            f"{self._keep_arm()}"
            'echo REACHED_END\n'
        )
        return subprocess.run(["bash", "-c", program], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)

    @staticmethod
    def _memo(tmp_path):
        path = tmp_path / "memo.json"
        return json.loads(path.read_text()) if path.exists() else None

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

    @pytest.mark.parametrize("kw", [
        {"elapsed_s": "unknown", "installed_budget_s": 1800},
        {"elapsed_s": 100, "installed_budget_s": None},
    ], ids=["unknown-elapsed", "unknown-budget"])
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
            block, n = re.subn(
                grace_re, f"PIGEN_VALIDATOR_KILL_GRACE_S={grace}", block, flags=re.MULTILINE
            )
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
                cwd=tmp_path, capture_output=True, text=True, timeout=20,
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
            "a marker written after the bound expired shipped in the image while the annotation "
            "said there was none"
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
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=600,
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
            [sys.executable, str(VALIDATOR), "check", "--stamp",
             "--dump", str(bogus), "--marker", str(marker)],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
        )
        assert r.returncode == 2, f"expected the refusal exit (2), got {r.returncode}\n{r.stderr}"
        assert "refusing --stamp" in (r.stdout + r.stderr)
        assert marker.read_text() == "pre-existing", "a refused stamp must leave the marker untouched"
