"""Tests for scripts/install.sh — structural invariants only.

install.sh is the DIY-install path. It must stay in sync with
pi-gen/stage3/01-setup-app/00-run.sh so that a user installing via
install.sh on their own image ends up with the same venv posture
as the pi-gen-built image.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


@pytest.fixture(scope="module")
def install_sh_content():
    return INSTALL_SH.read_text()


class TestInstallScriptStructure:
    def test_venv_creation_uses_system_site_packages(self, install_sh_content):
        """#214 regression: venv must inherit system site-packages so
        apt-provisioned GPIO libs are reachable without pip-recompiling
        them. Mirrors pi-gen/stage3/01-setup-app/00-run.sh:28.
        """
        venv_calls = re.findall(r"python3 -m venv [^\n|]+", install_sh_content)
        assert venv_calls, "install.sh should create a venv"
        for call in venv_calls:
            assert "--system-site-packages" in call, (
                f"venv creation missing --system-site-packages — a DIY install "
                f"would produce an isolated venv that can't see apt GPIO libs "
                f"and pip would try to compile them from sdist (#214). "
                f"Offending line: {call.strip()}"
            )

    def test_pip_install_filters_apt_provisioned(self, install_sh_content):
        """#214 regression: pip install must filter via requirements-apt.txt."""
        assert "requirements-apt.txt" in install_sh_content, (
            "install.sh must read apt-provisioned names from requirements-apt.txt (#214)"
        )
        assert "grep -vE" in install_sh_content, "install.sh must filter requirements.txt with a grep -vE regex (#214)"
        assert 'install --upgrade -r "$REQUIREMENTS_FILTERED"' in install_sh_content, (
            "pip install must target the filtered requirements, not the raw file (#214)"
        )

    def test_pip_install_uses_upgrade(self, install_sh_content):
        """#321: parity with update.sh — when a user re-runs install.sh after
        pulling code that bumps a pinned dependency, pip without --upgrade
        silently keeps the old version. Pin the flag so a future refactor
        can't silently drop it.

        We intentionally do NOT pin `--upgrade-strategy eager` — see the
        matching test in test_update_sh.py for the Flask-transitive
        rationale (adversarial review).
        """
        import re

        req_install = re.search(
            r"pip\s+install\s+([^\n]*?)-r\s+\"\$REQUIREMENTS_FILTERED\"",
            install_sh_content,
        )
        assert req_install is not None, "could not locate the requirements-file pip install line"
        flags = req_install.group(1)
        assert "--upgrade" in flags, "requirements-file pip install must use --upgrade (#321 parity with update.sh)"
        assert "--upgrade-strategy eager" not in flags, (
            "requirements-file pip install must NOT use --upgrade-strategy eager — see "
            "test_update_sh.py::test_pip_install_uses_upgrade for the Flask-transitive rationale"
        )


def test_installs_and_enables_handoff_fallback_units():
    """EPIC #383 PR2 (#388): same missing-cp-breaks-the-feature trap as
    wifi-reset (#327). The fallback timer must be copied AND enabled; the
    service is timer-driven (no [Install]) so only the timer is enabled."""
    src = INSTALL_SH.read_text()
    assert "litclock-handoff-fallback.service" in src
    assert "litclock-handoff-fallback.timer" in src
    assert "systemctl enable litclock-handoff-fallback.timer" in src


def test_installs_and_enables_reresolve_location_service():
    """#337 /review testing-gap: install.sh must enable the new oneshot.
    Without this enable, the on-boot reresolve service exists on disk but
    never fires — and the entire #337 'Pi moved, location auto-updates'
    promise silently breaks for fresh-flash installs. A future install.sh
    refactor that drops the enable line is a silent feature loss; pin it."""
    src = INSTALL_SH.read_text()
    assert "litclock-reresolve-location.service" in src, (
        "#337: install.sh must reference litclock-reresolve-location.service"
    )
    assert "systemctl enable litclock-reresolve-location.service" in src, (
        "#337 /review: install.sh must enable the new oneshot. "
        "Without this, the unit exists but never fires on fresh-flash boots."
    )
