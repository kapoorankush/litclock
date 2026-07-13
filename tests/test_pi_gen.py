"""Tests for the pi-gen custom stage.

Validates that the stage structure is correct, packages match install.sh,
and build configuration is consistent.
"""

import os
import re
import stat

import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
PI_GEN_DIR = os.path.join(REPO_ROOT, "pi-gen")
STAGE_DIR = os.path.join(PI_GEN_DIR, "stage3")


# ── Stage structure ──────────────────────────────────────────────────


class TestStageStructure:
    def test_stage_directory_exists(self):
        assert os.path.isdir(STAGE_DIR)

    def test_export_image_marker_exists(self):
        assert os.path.exists(os.path.join(STAGE_DIR, "EXPORT_IMAGE"))

    def test_config_file_exists(self):
        assert os.path.isfile(os.path.join(PI_GEN_DIR, "config"))

    def test_build_script_exists_and_executable(self):
        build_sh = os.path.join(PI_GEN_DIR, "build.sh")
        assert os.path.isfile(build_sh)
        assert os.stat(build_sh).st_mode & stat.S_IXUSR

    @pytest.mark.parametrize(
        "substage",
        [
            "00-install-deps",
            "01-setup-app",
            "02-configure-system",
            "03-install-services",
            "04-finalize",
        ],
    )
    def test_substage_directory_exists(self, substage):
        assert os.path.isdir(os.path.join(STAGE_DIR, substage))

    def test_chroot_scripts_are_executable(self):
        for root, _dirs, files in os.walk(STAGE_DIR):
            for f in files:
                if f.endswith("-run-chroot.sh") or f.endswith("-run.sh"):
                    path = os.path.join(root, f)
                    mode = os.stat(path).st_mode
                    assert mode & stat.S_IXUSR, f"{path} is not executable"


# ── Package parity with install.sh ───────────────────────────────────


class TestPackageParity:
    """Ensure pi-gen stage installs the same packages as install.sh."""

    @staticmethod
    def _parse_install_sh_packages():
        """Extract apt packages from install.sh."""
        install_sh = os.path.join(REPO_ROOT, "scripts", "install.sh")
        with open(install_sh) as f:
            content = f.read()

        # Find the apt install block: "sudo apt install -y \" through the
        # last continuation line (indented package name without backslash)
        match = re.search(
            r"sudo apt install -y\s*\\(.*?)(?=\n\s*\n|\n\s*log_info)",
            content,
            re.DOTALL,
        )
        assert match, "Could not find apt install block in install.sh"
        block = match.group(1)
        packages = set()
        for line in block.strip().splitlines():
            pkg = line.strip().rstrip("\\").strip()
            if pkg:
                packages.add(pkg)
        return packages

    @staticmethod
    def _parse_pi_gen_packages():
        """Extract packages from pi-gen 00-packages file."""
        packages_file = os.path.join(STAGE_DIR, "00-install-deps", "00-packages")
        with open(packages_file) as f:
            packages = set()
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    packages.add(line)
        return packages

    def test_pi_gen_has_all_install_sh_packages(self):
        """Every package in install.sh must appear in pi-gen's 00-packages."""
        install_pkgs = self._parse_install_sh_packages()
        pi_gen_pkgs = self._parse_pi_gen_packages()
        missing = install_pkgs - pi_gen_pkgs
        assert not missing, f"Packages in install.sh but missing from pi-gen: {missing}"

    # Hardware GPIO/SPI packages installed via apt in pi-gen to avoid
    # QEMU cross-compilation issues (see #127). install.sh gets these
    # from pip instead, so they are expected to be pi-gen-only.
    #
    # python3-rpi.gpio was removed in #214 — the runtime chain
    # (display_driver → waveshare_epd.epd7in5_V2 → epdconfig.py) binds
    # to gpiozero's lgpio pin factory and never imports RPi.GPIO.
    PI_GEN_ONLY = {
        "python3-gpiozero",
        "python3-lgpio",
        "python3-spidev",
        "python3-pigpio",
    }

    def test_no_extra_packages_in_pi_gen(self):
        """Pi-gen shouldn't install packages that install.sh doesn't
        (except for known pi-gen-only hardware packages)."""
        install_pkgs = self._parse_install_sh_packages()
        pi_gen_pkgs = self._parse_pi_gen_packages()
        extra = pi_gen_pkgs - install_pkgs - self.PI_GEN_ONLY
        assert not extra, f"Packages in pi-gen but not in install.sh: {extra}"


# ── Config file ──────────────────────────────────────────────────────


class TestConfig:
    @staticmethod
    def _read_config():
        config_path = os.path.join(PI_GEN_DIR, "config")
        config = {}
        with open(config_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    config[key.strip()] = value.strip().strip('"')
        return config

    def test_image_name(self):
        config = self._read_config()
        assert config["IMG_NAME"] == "litclock"

    def test_hostname(self):
        config = self._read_config()
        assert config["TARGET_HOSTNAME"] == "litclock"

    def test_first_user(self):
        config = self._read_config()
        assert config["FIRST_USER_NAME"] == "pi"

    def test_ssh_disabled(self):
        config = self._read_config()
        assert config["ENABLE_SSH"] == "0"

    def test_release_is_bookworm(self):
        config = self._read_config()
        assert config["RELEASE"] == "bookworm"

    def test_stage_list_includes_custom_stage(self):
        config = self._read_config()
        assert "stage3" in config["STAGE_LIST"]


# ── BCM2835 version matches install.sh ───────────────────────────────


class TestBCM2835:
    def test_version_matches_install_sh(self):
        """BCM2835 version in pi-gen stage must match install.sh."""
        install_sh = os.path.join(REPO_ROOT, "scripts", "install.sh")
        with open(install_sh) as f:
            install_content = f.read()

        chroot_sh = os.path.join(STAGE_DIR, "00-install-deps", "01-run.sh")
        with open(chroot_sh) as f:
            chroot_content = f.read()

        install_match = re.search(r'BCM2835_VERSION="(\S+)"', install_content)
        chroot_match = re.search(r'BCM2835_VERSION="(\S+)"', chroot_content)

        assert install_match, "BCM2835_VERSION not found in install.sh"
        assert chroot_match, "BCM2835_VERSION not found in chroot script"
        assert install_match.group(1) == chroot_match.group(1), (
            f"BCM2835 version mismatch: install.sh={install_match.group(1)}, pi-gen={chroot_match.group(1)}"
        )


# ── Systemd units referenced in stage match repo ─────────────────────


class TestSystemdUnitsInStage:
    def test_stage_copies_all_required_units(self):
        """The install-services chroot script must copy all units that
        install.sh copies."""
        chroot_sh = os.path.join(STAGE_DIR, "03-install-services", "00-run.sh")
        with open(chroot_sh) as f:
            content = f.read()

        required_units = [
            "litclock-splash.service",
            "litclock-firstboot.service",
            "litclock.service",
            "litclock.timer",
            "litclock-shutdown.service",
            "wifi-watchdog.service",
            "wifi-watchdog.timer",
            # EPIC #383 PR2 (#388) — handoff fallback completer.
            "litclock-handoff-fallback.service",
            "litclock-handoff-fallback.timer",
        ]
        for unit in required_units:
            assert unit in content, f"{unit} not copied in install-services stage"

    def test_stage_enables_required_units(self):
        chroot_sh = os.path.join(STAGE_DIR, "03-install-services", "00-run.sh")
        with open(chroot_sh) as f:
            content = f.read()

        required_enables = [
            "litclock-splash.service",
            "litclock-firstboot.service",
            # litclock.timer is deliberately NOT enabled at build time —
            # first-boot.sh enables it after setup completes (avoids GPIO race)
            "litclock-shutdown.service",
            "wifi-watchdog.timer",
            # EPIC #383 PR2 (#388) — fallback timer (service has no [Install]).
            "litclock-handoff-fallback.timer",
        ]
        for unit in required_enables:
            assert f"systemctl enable {unit}" in content, f"systemctl enable {unit} not found in stage"

    def test_all_copied_units_exist_in_repo(self):
        """Every unit file the stage copies must exist in the systemd/ dir."""
        systemd_dir = os.path.join(REPO_ROOT, "systemd")
        chroot_sh = os.path.join(STAGE_DIR, "03-install-services", "00-run.sh")
        with open(chroot_sh) as f:
            content = f.read()

        # Extract unit filenames from cp commands
        for match in re.finditer(r"cp.*?/systemd/([\w.\-]+)", content):
            unit = match.group(1)
            assert os.path.exists(os.path.join(systemd_dir, unit)), (
                f"Stage copies {unit} but it doesn't exist in systemd/"
            )


# ── Version metadata ─────────────────────────────────────────────────


class TestVersionMetadata:
    def test_finalize_writes_version_file(self):
        chroot_sh = os.path.join(STAGE_DIR, "04-finalize", "00-run.sh")
        with open(chroot_sh) as f:
            content = f.read()

        assert "/etc/litclock-version" in content
        assert "LITCLOCK_VERSION" in content
        assert "LITCLOCK_SHA" in content
        assert "build_date" in content


class TestJournaldConfig:
    """Journald must be persistent so boot-time failures are debuggable.

    Volatile storage (the prior default) wiped logs on every reboot, making
    it impossible to debug failed first-boots on real hardware (#172).
    """

    JOURNALD_CONF = os.path.join(
        STAGE_DIR,
        "02-configure-system",
        "files",
        "litclock-journald.conf",
    )

    def _read(self):
        with open(self.JOURNALD_CONF) as f:
            return f.read()

    def test_journald_conf_exists(self):
        assert os.path.isfile(self.JOURNALD_CONF)

    def test_journald_storage_is_persistent(self):
        content = self._read()
        assert re.search(r"^Storage=persistent", content, re.MULTILINE)
        assert not re.search(r"^Storage=volatile", content, re.MULTILINE)

    def test_journald_has_size_cap(self):
        """Size cap is required to prevent unbounded SD card wear."""
        content = self._read()
        assert re.search(r"^SystemMaxUse=\d+[KMG]?", content, re.MULTILINE)
