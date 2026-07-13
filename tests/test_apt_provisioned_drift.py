"""Drift guard for requirements-apt.txt (#214).

requirements-apt.txt is the single source of truth for Python package
names that come from apt on the Pi and must NOT be pip-installed into
the venv (they'd compile from sdist on a gcc-less image, failing).

Three scripts filter their requirements.txt through this file:
  - pi-gen/stage3/01-setup-app/00-run.sh  (image build)
  - scripts/install.sh                    (DIY install)
  - scripts/update.sh                     (in-place update)

This test catches the drift scenarios we care about:

  1. A new python3-* GPIO package is added to pi-gen and a corresponding
     pip entry is added to requirements.txt, but the operator forgets to
     add the pip name to requirements-apt.txt → venv rebuild on-device
     tries to compile the package as a sdist → fail (#214 repeat).

  2. An apt package is removed from pi-gen but its pip name is left in
     requirements-apt.txt → filter excludes something that should now be
     pip-installed → missing runtime dep.

  3. The regex built from requirements-apt.txt doesn't actually match
     the corresponding requirements.txt lines (e.g. a dot wasn't escaped,
     case mismatch, etc.).

Any future apt-mirrored package added to pi-gen MUST have its (apt, pip)
pair added to APT_TO_PIP here.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = REPO_ROOT / "requirements.txt"
REQUIREMENTS_APT = REPO_ROOT / "requirements-apt.txt"
PI_GEN_PACKAGES = REPO_ROOT / "pi-gen" / "stage3" / "00-install-deps" / "00-packages"

# Known (apt, pip) pairs for Python packages available via both channels
# on Raspberry Pi OS Bookworm. If pi-gen installs the apt package, the
# pip name MUST appear in requirements-apt.txt so the filter catches it.
#
# Add new entries here when introducing a new apt-provisioned GPIO lib.
APT_TO_PIP = {
    "python3-gpiozero": "gpiozero",
    "python3-lgpio": "lgpio",
    "python3-pigpio": "pigpio",
    "python3-spidev": "spidev",
    "python3-colorzero": "colorzero",
    "python3-rpi.gpio": "RPi.GPIO",
}

# pip names in requirements-apt.txt that come from apt TRANSITIVELY — they
# are Depends: of another python3-* package in pi-gen, not installed
# explicitly via 00-packages. Safe to filter from pip because apt still
# provides them via the transitive install.
TRANSITIVELY_PROVIDED = {
    "colorzero",  # python3-gpiozero Depends: on python3-colorzero
}


def _parse_nonblank_lines(path: Path) -> set[str]:
    out = set()
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.add(line)
    return out


def _parse_requirements_names(path: Path) -> set[str]:
    """Return the set of pip package names (pre-`==`) in a requirements file."""
    out = set()
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split("==", 1)[0].strip()
        if name:
            out.add(name)
    return out


def _build_exclude_regex(apt_file: Path) -> re.Pattern:
    """Reproduce the shell regex: dot-escaped names joined by |, anchored
    to start with `==` suffix. Mirrors `grep -vE "^(NAME1|NAME2|...)=="`
    built in the three scripts."""
    names = sorted(_parse_nonblank_lines(apt_file))
    escaped = [re.escape(n) for n in names]
    return re.compile(r"^(" + "|".join(escaped) + r")==")


class TestAptProvisionedDrift:
    def test_requirements_apt_exists(self):
        """The source-of-truth file must exist. If this fails, every
        subsequent drift check is meaningless."""
        assert REQUIREMENTS_APT.is_file(), (
            "requirements-apt.txt must exist at the repo root — it's the "
            "source of truth for apt-provisioned package filtering (#214)"
        )

    def test_every_apt_entry_has_known_mapping(self):
        """Every name in requirements-apt.txt must be in APT_TO_PIP so we
        know which apt package it corresponds to. Unmapped entries are
        impossible to validate against pi-gen."""
        apt_names = _parse_nonblank_lines(REQUIREMENTS_APT)
        known_pip_names = set(APT_TO_PIP.values())
        unknown = apt_names - known_pip_names
        assert not unknown, (
            f"requirements-apt.txt contains pip names with no APT_TO_PIP "
            f"mapping: {unknown}. Add them to APT_TO_PIP in "
            f"tests/test_apt_provisioned_drift.py alongside their "
            f"`python3-*` apt equivalents."
        )

    def test_pi_gen_gpio_packages_are_in_requirements_apt(self):
        """#214 core guard: every python3-* GPIO-family package that
        pi-gen installs MUST have its pip name in requirements-apt.txt.
        Otherwise an on-device venv rebuild tries to pip-compile it as
        sdist and fails on a gcc-less image."""
        pi_gen_pkgs = _parse_nonblank_lines(PI_GEN_PACKAGES)
        apt_names = _parse_nonblank_lines(REQUIREMENTS_APT)
        missing = []
        for apt_pkg, pip_name in APT_TO_PIP.items():
            if apt_pkg in pi_gen_pkgs and pip_name not in apt_names:
                missing.append((apt_pkg, pip_name))
        assert not missing, (
            f"pi-gen installs these apt packages but requirements-apt.txt "
            f"doesn't filter their pip equivalents — on-device venv "
            f"rebuild would try to pip-compile them (#214): {missing}"
        )

    def test_requirements_apt_entries_map_to_installed_apt_packages(self):
        """Inverse drift: if requirements-apt.txt filters a pip name, the
        corresponding apt package MUST actually be installed by pi-gen
        (directly or transitively via TRANSITIVELY_PROVIDED). Otherwise
        the venv has neither the pip nor the apt copy and imports fail
        at runtime."""
        pi_gen_pkgs = _parse_nonblank_lines(PI_GEN_PACKAGES)
        apt_names = _parse_nonblank_lines(REQUIREMENTS_APT)
        stranded = []
        for pip_name in apt_names:
            if pip_name in TRANSITIVELY_PROVIDED:
                continue
            apt_pkg = next((k for k, v in APT_TO_PIP.items() if v == pip_name), None)
            if apt_pkg is None:
                # caught by test_every_apt_entry_has_known_mapping; skip
                continue
            if apt_pkg not in pi_gen_pkgs:
                stranded.append((pip_name, apt_pkg))
        assert not stranded, (
            f"requirements-apt.txt filters these pip names but pi-gen "
            f"doesn't install the corresponding apt packages (and they're "
            f"not in TRANSITIVELY_PROVIDED) — venv would have neither: "
            f"{stranded}"
        )

    def test_rpi_gpio_not_reintroduced(self):
        """#214 guard: RPi.GPIO was removed from requirements.txt because
        the runtime chain (waveshare_epd.epd7in5_V2 → epdconfig.py) binds
        to gpiozero's lgpio pin factory and never imports RPi.GPIO. Having
        RPi.GPIO installed in the venv creates a subtle risk: gpiozero can
        silently fall back to RPi.GPIO as its pin_factory, which uses
        /dev/gpiomem with different reset-pulse timing than lgpio's
        /dev/gpiochip0 — a known correlate with flaky Waveshare 7.5\"V2
        init. Removing it also eliminates the #214 gcc-compile failure.
        Do not reintroduce without re-running the pin-factory probe.
        """
        req_names = _parse_requirements_names(REQUIREMENTS)
        assert "RPi.GPIO" not in req_names, (
            "RPi.GPIO was removed from requirements.txt in #214 because it's "
            "unused at runtime (proven via pin-factory probe on clean Pi) and "
            "its presence risks gpiozero silently picking it over lgpio. Do "
            "not reintroduce without re-verifying the pin_factory binding."
        )
        pi_gen_pkgs = _parse_nonblank_lines(PI_GEN_PACKAGES)
        assert "python3-rpi.gpio" not in pi_gen_pkgs, (
            "python3-rpi.gpio was removed from pi-gen 00-packages in #214 "
            "(see test_rpi_gpio_not_reintroduced docstring for full rationale)."
        )

    def test_regex_filter_actually_excludes_matching_requirements_lines(self):
        """Build the same regex the shell builds, apply it to
        requirements.txt, assert every matching line is in fact an
        apt-provisioned name. Regression guard for dot-escaping,
        case-sensitivity, and version-suffix edge cases."""
        regex = _build_exclude_regex(REQUIREMENTS_APT)
        apt_names = _parse_nonblank_lines(REQUIREMENTS_APT)
        matched = []
        for raw in REQUIREMENTS.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if regex.match(line):
                name = line.split("==", 1)[0]
                matched.append(name)
                assert name in apt_names, (
                    f"regex matched `{line}` but `{name}` is not in requirements-apt.txt — regex is too greedy"
                )
        # Every apt name that's ALSO in requirements.txt should have matched.
        req_names = _parse_requirements_names(REQUIREMENTS)
        for apt_name in apt_names:
            if apt_name in req_names:
                assert apt_name in matched, (
                    f"requirements.txt contains `{apt_name}` but the regex "
                    f"built from requirements-apt.txt did not match it — "
                    f"likely a dot-escape or anchoring bug"
                )
