"""Tests for the pi-gen custom stage.

Validates that the stage structure is correct, the package list stays
self-consistent, and build configuration is consistent.
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


# ── Package list is self-describing ──────────────────────────────────


def _strip_comments(body):
    """Drop full-line shell comments so text assertions cannot be satisfied by
    commented-out code. Without this, `# cp "${INSTALL_DIR}/systemd/x.service"`
    would look identical to the real thing to a substring search."""
    return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))


class TestPackageList:
    """pi-gen's 00-packages was previously cross-checked against
    scripts/install.sh, both directions. That guard died with install.sh
    (litclock-dev#547): with one install path there is no second list to drift
    from, so "parity" has nothing to compare against.

    What survives is the invariant that actually mattered — the apt-provisioned
    GPIO/SPI packages must stay in lockstep with requirements-apt.txt, which is
    the single source of truth for names pip must NOT install. That is enforced
    by tests/test_apt_provisioned_drift.py::test_pi_gen_gpio_packages_are_in_requirements_apt,
    which never depended on install.sh.

    Kept here: the list must parse, and must not regain the packages #214
    deliberately removed.
    """

    @staticmethod
    def _parse_pi_gen_packages():
        packages_file = os.path.join(STAGE_DIR, "00-install-deps", "00-packages")
        packages = set()
        with open(packages_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    packages.add(line)
        return packages

    # The list install.sh used to be diffed against, pinned here instead.
    # Review caught that `len(pkgs) > 10` let 14 of the 25 lines be deleted
    # with the whole suite green — including wireless-tools, whose absence
    # silently unships the WiFi power-save mitigation (see below).
    REQUIRED = {
        "git",
        "python3",
        "python3-pip",
        "python3-venv",
        "python3-dev",
        # Pillow build deps — the renderer will not import without them.
        "libopenjp2-7-dev",
        "libjpeg-dev",
        "zlib1g-dev",
        "libfreetype6-dev",
        # CJK fonts: the corpus is EN today, but #19/#532 land per-language
        # corpora and a missing font renders tofu rather than failing loudly.
        "ttf-wqy-zenhei",
        "ttf-wqy-microhei",
        # /usr/sbin/iwconfig, baked into /etc/rc.local by
        # pi-gen/stage3/02-configure-system/00-run.sh to disable brcmfmac
        # power save. rc.local has no `set -e` and ends `exit 0`, so a missing
        # iwconfig fails SILENTLY and the mitigation just stops applying on
        # every flashed device. That mitigation has already been broken once
        # by a different mechanism (the rc.local shebang escape); this pins
        # the package half of it.
        "wireless-tools",
        # jq — M5 status-file helper needs it for atomic JSON writes.
        "jq",
    }

    def test_required_packages_are_present(self):
        """Replaces the old install.sh package-parity check. Parity needed two
        parties; this needs none — it states the requirement directly."""
        pkgs = self._parse_pi_gen_packages()
        missing = self.REQUIRED - pkgs
        assert not missing, f"00-packages is missing required packages: {sorted(missing)}"

    def test_package_list_is_non_empty_and_parses(self):
        pkgs = self._parse_pi_gen_packages()
        assert len(pkgs) > 10, f"00-packages looks truncated: {sorted(pkgs)}"

    def test_rpi_gpio_apt_package_not_reintroduced(self):
        """#214 removed python3-rpi.gpio — the runtime chain
        (display_driver -> waveshare_epd.epd7in5_V2 -> epdconfig.py) binds to
        gpiozero's lgpio pin factory and never imports RPi.GPIO."""
        assert "python3-rpi.gpio" not in self._parse_pi_gen_packages()


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


# ── BCM2835 version is declared ──────────────────────────────────────


class TestBCM2835:
    """Previously cross-checked against scripts/install.sh. With install.sh
    retired (litclock-dev#547) pi-gen is the sole definer, so there is no
    second value to disagree with. What is still worth asserting is that the
    version is declared at all and is a plausible version string — a silently
    empty BCM2835_VERSION would build an image whose e-ink driver never
    links."""

    def test_version_is_declared_and_well_formed(self):
        chroot_sh = os.path.join(STAGE_DIR, "00-install-deps", "01-run.sh")
        with open(chroot_sh) as f:
            chroot_content = f.read()
        match = re.search(r'BCM2835_VERSION="(\S+)"', chroot_content)
        assert match, "BCM2835_VERSION not found in pi-gen chroot script"
        assert re.fullmatch(r"\d+(\.\d+)+", match.group(1)), f"BCM2835_VERSION looks malformed: {match.group(1)!r}"


# ── Systemd units referenced in stage match repo ─────────────────────


class TestSystemdUnitsInStage:
    def test_stage_copies_all_required_units(self):
        """The install-services chroot script must copy every unit the
        clock needs at runtime."""
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


class TestPiGenVenvPosture:
    """The #214/#321/#323 venv invariants, asserted against the IMAGE build.

    These were previously covered for scripts/install.sh (tests/test_install_sh.py,
    deleted with it in litclock-dev#547) and for scripts/update.sh
    (tests/test_update_sh.py). Nothing ever read
    pi-gen/stage3/01-setup-app/00-run.sh — which is where a flashed device
    actually gets its venv.

    Before litclock-dev#547 that was 2 of 3 install paths guarded. Retiring
    install.sh would have left 1 of 2, with the unguarded one being the ONLY
    path that ships. Review flagged it; retiring the mirror is the moment to
    point the assertions at the survivor rather than lose them.
    """

    @staticmethod
    def _setup_app():
        with open(os.path.join(STAGE_DIR, "01-setup-app", "00-run.sh")) as f:
            return f.read()

    def test_venv_uses_system_site_packages(self):
        """#214: the apt-provisioned GPIO/SPI wheels are only visible to the
        venv with --system-site-packages. Without it the driver chain cannot
        import lgpio and the panel never paints."""
        body = self._setup_app()
        assert "python3 -m venv --system-site-packages" in body, (
            "pi-gen must create the venv with --system-site-packages (#214)"
        )

    def test_pip_install_filters_apt_provisioned_names(self):
        """#214: requirements-apt.txt is the single source of truth for names
        pip must NOT install. The filter builds EXCLUDE_RE from that file, so
        a hand-edited list here would silently drift."""
        # Comments stripped first: mutation showed `"requirements-apt.txt" in
        # body` was satisfied by the explanatory comment above the code, so the
        # executable line could stop reading the file and this stayed green.
        body = _strip_comments(self._setup_app())
        assert re.search(r"EXCLUDE_RE=.*requirements-apt\.txt", body), (
            "pi-gen must build EXCLUDE_RE from requirements-apt.txt itself (#214) — "
            "a hand-maintained list here is exactly the drift that file exists to prevent"
        )
        assert re.search(r"grep -vE .*EXCLUDE_RE.* requirements\.txt", body), (
            "pi-gen must filter requirements.txt through EXCLUDE_RE before pip install"
        )

    def test_pip_install_is_not_eager(self):
        """#322: eager upgrade-strategy silently bumps transitives fleet-wide;
        the smoke test never imports Flask, so a break would ship."""
        body = self._setup_app()
        assert "--upgrade-strategy eager" not in body, (
            "pi-gen must NOT use eager upgrade-strategy (#322) — transitive breaks ship unnoticed"
        )


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
