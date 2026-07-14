"""Tests for sudoers/020_litclock-control + its install paths.

The Control PWA (#245 M4) needs scoped sudo to run `systemctl reboot`
and `systemctl poweroff`. A malformed sudoers entry locks out `sudo`
system-wide — bricks the appliance worse than any other M4 failure
mode. These tests guard:

  1. The file parses cleanly under `visudo -c -f`.
  2. The exact M4 commands are present (regression guard against
     someone tightening the allowlist below what control_server invokes).
  3. install.sh, update.sh, and pi-gen all install the file via the
     validate-then-install pattern.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SUDOERS_FILE = REPO_ROOT / "sudoers" / "020_litclock-control"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"
PI_GEN_CONFIGURE = REPO_ROOT / "pi-gen" / "stage3" / "02-configure-system" / "00-run.sh"


# ─── File contents ──────────────────────────────────────────────────────────


class TestSudoersFile:
    def test_file_exists(self):
        assert SUDOERS_FILE.is_file(), f"missing {SUDOERS_FILE}"

    @pytest.mark.skipif(
        shutil.which("visudo") is None,
        reason="visudo not installed in dev env (sudo package); CI Linux runners have it",
    )
    def test_parses_under_visudo(self):
        result = subprocess.run(
            ["visudo", "-c", "-f", str(SUDOERS_FILE)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"visudo -c -f rejected the file: {result.stdout}{result.stderr}"

    def test_grants_only_pi_user(self):
        body = SUDOERS_FILE.read_text()
        # First non-comment, non-blank line must start with `pi `.
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            assert stripped.startswith("pi "), f"first sudoers rule must apply to user `pi`; saw: {stripped!r}"
            break

    @pytest.mark.parametrize(
        "command",
        [
            "/usr/bin/systemctl reboot",
            "/usr/bin/systemctl reboot --no-block",
            "/usr/bin/systemctl poweroff",
            "/usr/bin/systemctl poweroff --no-block",
            "/usr/bin/systemctl restart litclock.service",
            "/usr/bin/systemctl restart litclock.timer",
            # M3: ad-hoc tick after Settings save (D1).
            "/usr/bin/systemctl start litclock.service",
            "/usr/bin/systemctl start --no-block litclock.service",
            # #387: root-owned tz-wrapper for the arbitrary-tz sudo path.
            "/usr/local/lib/litclock/litclock-set-timezone",
            # #387: first-boot NTP enable (latent-010-break without this).
            "/usr/bin/timedatectl set-ntp true",
        ],
    )
    def test_command_present(self, command):
        """control_server invokes these exact strings (modulo --no-block).
        sudoers matches the binary path + args verbatim, so any deletion
        from this list breaks a control_server route silently.
        """
        body = SUDOERS_FILE.read_text()
        assert command in body, f"missing required command: {command!r}"


# ─── Install paths ──────────────────────────────────────────────────────────


class TestInstallScriptInstallsSudoers:
    """install.sh must install the sudoers drop with validate-then-install."""

    @pytest.fixture(scope="class")
    def script(self):
        return INSTALL_SH.read_text()

    def test_validates_before_install(self, script):
        # The validate (visudo -c -f) must appear before the install/cp,
        # so we never land a broken file in /etc/sudoers.d/.
        validate_pos = script.find("visudo -c -f")
        install_pos = script.find("install -m 0440")
        assert validate_pos != -1, "install.sh must run `visudo -c -f` on the sudoers source"
        assert install_pos != -1, "install.sh must `install -m 0440` the sudoers drop"
        assert validate_pos < install_pos, (
            "install.sh must validate BEFORE installing — otherwise a broken "
            "file lands in /etc/sudoers.d/ before visudo catches it"
        )

    def test_references_sudoers_file_by_name(self, script):
        assert "020_litclock-control" in script


class TestUpdateScriptSyncsSudoers:
    """update.sh must sync sudoers drops on every run, idempotently."""

    @pytest.fixture(scope="class")
    def script(self):
        return UPDATE_SH.read_text()

    def test_validates_before_install(self, script):
        # update.sh syncs all files in sudoers/ via a loop.
        loop_match = re.search(
            r"for sudoers_src in.*sudoers/.*?\n(.*?)done",
            script,
            re.DOTALL,
        )
        assert loop_match, "update.sh must iterate over sudoers/* sources"
        body = loop_match.group(1)
        assert "visudo -c -f" in body, "update.sh sudoers loop must validate via visudo"
        assert "install -m 0440" in body, "update.sh sudoers loop must use `install -m 0440`"

    def test_idempotent_diff_check(self, script):
        # Idempotent: skip re-install when source matches installed copy.
        # Looser regex to tolerate minor whitespace/quoting differences.
        assert re.search(r"cmp\s+-s", script), (
            "update.sh sudoers sync should `cmp -s` against the installed "
            "copy to skip re-install when unchanged (idempotency)"
        )


class TestPiGenInstallsSudoers:
    """pi-gen image build must install the sudoers drop."""

    @pytest.fixture(scope="class")
    def script(self):
        return PI_GEN_CONFIGURE.read_text()

    def test_installs_020_drop(self, script):
        assert "020_litclock-control" in script, (
            "pi-gen/stage3/02-configure-system/00-run.sh must install the "
            "020_litclock-control sudoers drop during image build"
        )

    def test_validates_via_visudo(self, script):
        # In the pi-gen path, set -e at the top of the script means visudo's
        # non-zero exit aborts the build — no need for an explicit guard.
        assert "visudo -c -f" in script
