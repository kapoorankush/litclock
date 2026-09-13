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
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"
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

    def test_it_is_bounded(self):
        """The check is ~21s on x86 and minutes on a Pi Zero. Unbounded, a wedged
        validation would hold litclock-update.service to its TimeoutStartSec."""
        body = self._body()
        stamp_at = STAMP_CALL.search(body).start()
        window = body[max(0, stamp_at - 400):stamp_at]
        assert "timeout " in window, "the validation call must be bounded by a timeout"

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

    def _run(self, tmp_path, *, marker_exists: bool, validator_rc: int):
        fake_py = tmp_path / "python3"
        fake_py.write_text(f"#!/bin/bash\necho FAKE_VALIDATOR_RAN >&2\nexit {validator_rc}\n")
        fake_py.chmod(0o755)
        marker = tmp_path / "marker"
        if marker_exists:
            marker.write_text("present")
        program = (
            "set -u\n"
            'log_info() { echo "[INFO] $1"; }\n'
            'log_error() { echo "[ERROR] $1"; }\n'
            'atomic_remove_file() { :; }\n'
            f"PYTHON={fake_py}\nRUNTIME_MARKER={marker}\nUPDATE_FAILED_FILE=/dev/null\nsmoke_rc=0\n"
            f"{self._keep_arm()}"
            'echo REACHED_END\n'
        )
        return subprocess.run(["bash", "-c", program], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)

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

    def test_a_failed_validation_does_not_fail_the_image_build(self):
        body = PIGEN_APP.read_text()
        stamp_at = STAMP_CALL.search(body).start()
        window = body[stamp_at:stamp_at + 900]
        assert "WARNING" in window and "usable" in window, (
            "a failed validation must warn and continue — the image still works on the "
            "pre-rendered tier"
        )


class TestTheCheckItselfBehaves:
    """Executed, against the real validator and the real dump."""

    def test_check_passes_and_stamps_here(self, tmp_path):
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
