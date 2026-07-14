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
    """#337 /review testing-gap: install.sh must COPY and enable the oneshot.
    The original assertion only checked the unit NAME appeared somewhere —
    the enable line alone satisfied it, so the missing cp shipped: under
    set -e, `systemctl enable` of a never-copied unit aborted every DIY
    install. Assert the copy explicitly (name-presence is not installation)."""
    src = INSTALL_SH.read_text()
    assert 'cp "$INSTALL_DIR/systemd/litclock-reresolve-location.service"' in src, (
        "install.sh must COPY litclock-reresolve-location.service before enabling it — "
        "enable-without-copy aborts the whole DIY install under set -e"
    )
    assert "systemctl enable litclock-reresolve-location.service" in src, (
        "#337 /review: install.sh must enable the new oneshot. "
        "Without this, the unit exists but never fires on fresh-flash boots."
    )


def test_installs_prepare_for_gift_service():
    """#317: the control app's Prepare-for-Gifting starts this unit on demand.
    No [Install] section, so copy only — never enabled."""
    src = INSTALL_SH.read_text()
    assert 'cp "$INSTALL_DIR/systemd/litclock-prepare-for-gift.service"' in src, (
        "install.sh must copy litclock-prepare-for-gift.service or the PWA's "
        "Prepare-for-Gifting action fails on DIY installs"
    )


def test_every_repo_unit_is_referenced_by_install_sh():
    """Drift guard for the whole enabled-but-never-copied class: every unit
    shipped in systemd/ must at least be REFERENCED by install.sh (copied
    directly, or handled in a conditional block like wifi-watchdog). A new
    unit added to systemd/ without install.sh wiring fails here instead of
    aborting a stranger's DIY install at runtime."""
    src = INSTALL_SH.read_text()
    systemd_dir = INSTALL_SH.parent.parent / "systemd"
    units = sorted(p.name for p in systemd_dir.iterdir() if p.is_file() and p.suffix in (".service", ".timer"))
    assert units, "systemd/ unit inventory came back empty — test wiring broke"
    missing = [u for u in units if f"systemd/{u}" not in src]
    assert not missing, (
        f"units in systemd/ never referenced by install.sh: {missing} — "
        "DIY installs won't get them (and enabling one aborts under set -e)"
    )
