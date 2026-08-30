"""Tests for sudoers/020_litclock-control + its install paths.

The Control PWA (litclock-dev#245 M4) needs scoped sudo to run `systemctl reboot`
and `systemctl poweroff`. A malformed sudoers entry locks out `sudo`
system-wide — bricks the appliance worse than any other M4 failure
mode. These tests guard:

  1. The file parses cleanly under `visudo -c -f`.
  2. The exact M4 commands are present (regression guard against
     someone tightening the allowlist below what control_server invokes).
  3. update.sh and pi-gen both install the file via the validate-then-install
     pattern. (install.sh was retired in litclock-dev#547.)
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SUDOERS_FILE = REPO_ROOT / "sudoers" / "020_litclock-control"
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
            # litclock-dev#387: root-owned tz-wrapper for the arbitrary-tz sudo path.
            "/usr/local/lib/litclock/litclock-set-timezone",
            # litclock-dev#387: first-boot NTP enable (latent-010-break without this).
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


class TestFirstBootSetupIncompleteSudo:
    """litclock-dev#657: what the Setup-Incomplete arm needs from sudo.

    Two sudo calls were added there. One is covered by the scoped allowlist and
    one deliberately is not, and the difference is easy to lose — the code
    comment states it, and this is what keeps the comment true."""

    _FIRST_BOOT = REPO_ROOT / "scripts" / "first-boot.sh"

    def test_the_poweroff_is_in_the_scoped_allowlist(self):
        """So it survives a future drop of the broad 010 grant, which is
        exactly what the code comment claims about this line."""
        assert "sudo systemctl poweroff" in self._FIRST_BOOT.read_text()
        # The BARE form, bounded. A plain substring check is satisfied by the
        # `--no-block` grant that follows it in the same line, so deleting the
        # bare one left this green while `sudo systemctl poweroff` — the form
        # first-boot.sh actually runs — was no longer permitted (/review).
        assert re.search(r"/usr/bin/systemctl poweroff\s*(,|$)", SUDOERS_FILE.read_text(), re.M), (
            "first-boot.sh's Setup-Incomplete arm runs bare `sudo systemctl poweroff`; 020 must grant that form, "
            "not only `poweroff --no-block`"
        )

    def test_the_suppress_marker_touch_is_deliberately_NOT_in_the_allowlist(self):
        """The inverse assertion, and it is the load-bearing one.

        Granting pi `touch /run/litclock-splash-suppress` would let a pi-level
        process mute the shutdown splash — including the gift welcome — which
        is the precise thing shutdown-splash.sh's root-owned path exists to
        prevent. So this must stay OUT, and the consequence (the marker stops
        working if 010 is dropped) is recorded rather than silently traded
        away. If someone adds it, this test says why not to.
        """
        assert "/run/litclock-splash-suppress" not in SUDOERS_FILE.read_text(), (
            "granting pi this touch reinstates the gift-welcome suppression the root-owned path "
            "prevents (litclock-dev#657 /review). Closing the 010 gap needs a root-owned wrapper, "
            "like /usr/local/lib/litclock/litclock-set-timezone, not a wider allowlist."
        )


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
        # ORDER, not just presence. litclock-dev#547 review: the deleted install.sh test
        # was the repo's only instance comparing positions, so this one would
        # have stayed green if the loop were reordered to install-then-validate,
        # landing a broken file in /etc/sudoers.d/ before visudo ever ran.
        assert body.index("visudo -c -f") < body.index("install -m 0440"), (
            "update.sh must validate BEFORE installing — otherwise a broken file "
            "lands in /etc/sudoers.d/ before visudo catches it"
        )

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
