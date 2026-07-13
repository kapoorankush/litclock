"""Tests for scripts/prepare-for-cloning.sh (issue #160)."""

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
