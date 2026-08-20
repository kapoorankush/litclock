"""Tests for the pi-gen custom stage.

Validates that the stage structure is correct, packages are well-formed,
and build configuration is consistent.
"""

import os
import re
import shlex
import stat
import subprocess

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


class TestPackageList:
    """pi-gen's 00-packages was previously cross-checked against
    scripts/install.sh, both directions. That guard died with install.sh
    (litclock-dev#547): with one install path there is no second list to drift from,
    so "parity" has nothing to compare against.

    What survives is the invariant that actually mattered — the apt-provisioned
    GPIO/SPI packages must stay in lockstep with requirements-apt.txt, which is
    the single source of truth for names pip must NOT install. That is enforced
    by tests/test_apt_provisioned_drift.py::test_pi_gen_gpio_packages_are_in_requirements_apt,
    which never depended on install.sh.

    Kept here: the list must parse, and must not regain the packages litclock-dev#214
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
        # CJK fonts: the corpus is EN today, but #19/litclock-dev#532 land per-language
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
        # litclock-dev#605 item 18 (Codex, #45 port review): the old
        # install.sh parity diff covered the packages below too; the pinned
        # floor replacing it did not, so deleting any of them stayed green.
        # (item 18 also proposed qrencode, but its stated why — "renders the
        # setup/handoff QRs" — is false: every QR is drawn by the pip
        # `qrcode` library, and nothing invokes the qrencode binary. It was
        # REMOVED from 00-packages + docs/building-image.md as the litclock-dev#605
        # remainder; test_qrencode_is_not_installed below pins its absence.)
        # Secondary Pillow build deps. The primary four above are what the
        # import needs today; these are what pip compiles AGAINST when a
        # Pillow bump rebuilds the wheel on-device (update.sh venv path) —
        # absent, the rebuild silently drops the corresponding features.
        "liblcms2-dev",
        "libwebp-dev",
        "libharfbuzz-dev",
        "libfribidi-dev",
        "libxcb1-dev",
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

    def test_qrencode_is_not_installed(self):
        """litclock-dev#605: qrencode was removed — every QR is drawn by the pip
        `qrcode` library and nothing invokes the qrencode binary. Pin its
        absence so a future edit doesn't re-add an unused apt package."""
        assert "qrencode" not in self._parse_pi_gen_packages()

    def test_rpi_gpio_apt_package_not_reintroduced(self):
        """litclock-dev#214 removed python3-rpi.gpio — the runtime chain
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
    retired (litclock-dev#547) pi-gen is the sole definer, so there is no second value
    to disagree with. What is still worth asserting is that the version is
    declared at all and is a plausible version string — a silently empty
    BCM2835_VERSION would build an image whose e-ink driver never links."""

    def test_version_is_declared_and_well_formed(self):
        chroot_sh = os.path.join(STAGE_DIR, "00-install-deps", "01-run.sh")
        with open(chroot_sh) as f:
            chroot_content = f.read()
        match = re.search(r'BCM2835_VERSION="(\S+)"', chroot_content)
        assert match, "BCM2835_VERSION not found in pi-gen chroot script"
        assert re.fullmatch(r"\d+(\.\d+)+", match.group(1)), f"BCM2835_VERSION looks malformed: {match.group(1)!r}"


# ── Systemd units referenced in stage match repo ─────────────────────


SYSTEMD_DIR = os.path.join(REPO_ROOT, "systemd")
INSTALL_SERVICES_SH = os.path.join(STAGE_DIR, "03-install-services", "00-run.sh")
SMOKE_TEST_SH = os.path.join(STAGE_DIR, "05-smoke-test", "00-run.sh")


UNIT_SUFFIXES = (".service", ".timer")

# systemd/ subdirectories that the stage scripts know how to install. A new one
# would be invisible to every glob in 03 and 05 at once — see
# test_systemd_subdirectories_are_all_installed below.
TMPFILES_DIR = os.path.join(SYSTEMD_DIR, "tmpfiles.d")
KNOWN_SYSTEMD_SUBDIRS = {"tmpfiles.d"}


# systemd strstrip()s every line, so `  [Install]` and `  WantedBy=x` are legal.
# Column-0 anchors are blind to that, which made an indented [Install] invisible
# to BOTH the shell check and this file — litclock-dev#547's failure mode arriving through
# whitespace instead of a name list, with no second opinion.
_INSTALL_HEADER_RE = re.compile(r"^[ \t]*\[Install\]", re.MULTILINE)
# The full documented [Install] directive set. Not a curated subset: an
# allow-list of directive NAMES is the same enumeration bug as a list of unit
# names, one level down. UpheldBy= is valid since systemd 249.
INSTALL_DIRECTIVES = ("Alias", "WantedBy", "RequiredBy", "UpheldBy", "Also", "DefaultInstance")
_INSTALL_DIRECTIVE_RE = re.compile(rf"^[ \t]*(?:{'|'.join(INSTALL_DIRECTIVES)})[ \t]*=[ \t]*\S", re.MULTILINE)


def _shell_floor(path, var="MIN_EXPECTED_UNITS"):
    """Read a MIN_EXPECTED_* floor out of a stage script.

    Asserts EXACTLY ONE assignment. `re.search` returns the first match, but a
    plain shell variable is governed by the LAST assignment — so a second one
    added later would drive the running guard while this kept reporting the old
    value, green. Same defeat that
    test_resolved_shas_are_assigned_exactly_once closes for the workflow.
    """
    body = open(path).read()
    matches = re.findall(rf"^{re.escape(var)}=(\d+)$", body, re.MULTILINE)
    assert matches, f"{var} not found in {path}"
    assert len(matches) == 1, (
        f"{var} is assigned {len(matches)} times in {path}: {matches}. "
        f"The last assignment wins at runtime; this reader would report the first."
    )
    return int(matches[0])


def _strip_comments(body):
    """Drop full-line shell comments so text assertions cannot be satisfied by
    commented-out code. Without this, `# cp "${INSTALL_DIR}/systemd/x.service"`
    would look identical to the real thing to a substring search."""
    return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))


def _repo_units():
    """Every first-level unit file in systemd/, derived from disk.

    Derived on purpose. Three hand-maintained lists (this file, the
    03-install-services cp block, and the 05-smoke-test required_files array)
    all drifted together and let litclock-reresolve-location.service reach no
    flashed image at all (litclock-dev#547). A literal list here would be the fourth.

    The floor is load-bearing, not decorative. Every guard below derives its
    expected set from this function, so if the derivation ever collapses to []
    (wrong path, renamed directory, changed suffix) those guards would assert
    over an empty collection and pass GREEN with zero real assertions. That is
    the same vacuous-pass failure the shell-side MIN_EXPECTED_UNITS exists to
    prevent, and it was verified reachable: emptying systemd/ left 8 of the 10
    systemd tests passing before this assert was added.
    """
    units = sorted(f for f in os.listdir(SYSTEMD_DIR) if f.endswith(UNIT_SUFFIXES))
    floor = _shell_floor(INSTALL_SERVICES_SH)
    assert len(units) >= floor, (
        f"systemd/ derivation collapsed to {len(units)} units (floor {floor}). "
        f"Every derived guard in this file would otherwise pass vacuously."
    )
    return units


def _repo_tmpfiles():
    """Every systemd-tmpfiles drop-in in systemd/tmpfiles.d/, derived from disk.

    Derived for the same reason _repo_units() is, and with the same vacuous-pass
    guard: if the derivation collapses to [] the checks below would assert over
    an empty collection and pass green.
    """
    confs = sorted(f for f in os.listdir(TMPFILES_DIR) if f.endswith(".conf"))
    floor = _shell_floor(INSTALL_SERVICES_SH, "MIN_EXPECTED_TMPFILES")
    assert len(confs) >= floor, f"systemd/tmpfiles.d/ derivation collapsed to {len(confs)} drop-ins (floor {floor})."
    return confs


def _units_with_install_section():
    out = []
    for name in _repo_units():
        with open(os.path.join(SYSTEMD_DIR, name)) as f:
            if _INSTALL_HEADER_RE.search(f.read()):
                out.append(name)
    return out


def _normalise_unit_body(text):
    """Normalise a unit file the way systemd's parser sees it.

    Comments are stripped BEFORE continuations are joined, and the order is the
    whole point: systemd does not continue a comment line. Joining first made a
    unit whose comment above [Install] ends in a backslash look like
    `# comment [Install]` — so this file rejected a unit real systemd enables,
    while 05-smoke-test silently SKIPPED the same unit. Two normalisers, two
    different wrong answers, neither matching systemd.

    Kept as a module-level function so it can be tested against those bodies
    directly rather than only through the repo's own (currently comment-free)
    units.
    """
    text = text.replace("\r\n", "\n")
    # Comments first, then join, then drop any dangling backslash left when the
    # line a continuation pointed at was itself a comment. 05-smoke-test does the
    # same three passes; they must agree with each other and with systemd.
    text = re.sub(r"(?m)^[ \t]*[#;].*$", "", text)
    text = re.sub(r"\\\n[ \t]*", " ", text)
    return re.sub(r"(?m)[ \t]*\\[ \t]*$", "", text)


class TestInstallSectionValidity:
    """An [Install] section must actually install something.

    This lived briefly in 05-smoke-test as a hard failure and was moved here.
    It is a property of a file in the repo, so a 0.3-second test can prove it;
    in the smoke test a false positive blocks every release ~38 minutes into a
    40-minute build, and the hard-fail version had four ways to reject a legal
    unit (UpheldBy=, DefaultInstance=, indented directives, and
    `WantedBy=a \\` continuations). Enablement still belongs in the smoke test —
    only a built image has the .wants symlinks.
    """

    def test_every_install_section_carries_a_real_directive(self):
        """The litclock-dev#547 case this is really for: typo `WnatedBy=` on
        litclock-reresolve-location.service and the section installs nothing."""
        bad = []
        for name in _units_with_install_section():
            with open(os.path.join(SYSTEMD_DIR, name)) as f:
                body = _normalise_unit_body(f.read())
            install_block = body[_INSTALL_HEADER_RE.search(body).start() :]
            # Stop at the next section header, if any.
            nxt = re.search(r"^[ \t]*\[(?!Install\])", install_block[1:], re.MULTILINE)
            if nxt:
                install_block = install_block[: nxt.start() + 1]
            if not _INSTALL_DIRECTIVE_RE.search(install_block):
                bad.append(name)
        assert not bad, (
            f"units carry an [Install] section with none of {INSTALL_DIRECTIVES}: {bad}. "
            f"That section installs nothing — fix the directive or drop the section."
        )

    def test_normaliser_does_not_continue_a_comment_line(self):
        """systemd does not treat a trailing backslash on a COMMENT as a
        continuation. Verified against a real `systemctl --root=` tree: such a
        unit is enabled normally. If the join runs first, [Install] disappears
        into the comment."""
        body = "[Unit]\nDescription=x\n# trailing comment continuation \\\n[Install]\nWantedBy=multi-user.target\n"
        norm = _normalise_unit_body(body)
        assert _INSTALL_HEADER_RE.search(norm), f"[Install] was swallowed by a comment continuation: {norm!r}"
        assert _INSTALL_DIRECTIVE_RE.search(norm), "WantedBy= lost"

    def test_normaliser_does_not_let_a_comment_eat_the_next_directive(self):
        """Same bug one level in: `# note \\` INSIDE [Install] swallowed the
        WantedBy= line below it."""
        body = "[Unit]\nDescription=x\n[Install]\n# note \\\nWantedBy=multi-user.target\n"
        norm = _normalise_unit_body(body)
        assert _INSTALL_DIRECTIVE_RE.search(norm), f"WantedBy= eaten by the comment: {norm!r}"

    def test_a_comment_after_a_continuation_does_not_become_a_target(self):
        """`WantedBy=a.target \\` followed by a COMMENT. Real systemd enables into
        a.target only. One pass left `a.target # comment`; stripping comments
        first still left a dangling `\\` that iterated as a bogus target."""
        body = "[Install]\nWantedBy=multi-user.target \\\n# trailing comment\n"
        norm = _normalise_unit_body(body)
        targets = re.search(r"^WantedBy[ \t]*=[ \t]*(.*)$", norm, re.MULTILINE).group(1).split()
        assert targets == ["multi-user.target"], f"parsed targets {targets}"

    def test_a_trailing_backslash_at_eof_is_not_a_target(self):
        """A unit whose last line ends in a backslash with no newline after it.
        The join pass needs a following newline to match, so without the final
        cleanup the bare `\\` survives and iterates as a bogus target — the
        shell side hits the same shape via sed's `N` at EOF."""
        body = "[Install]\nWantedBy=multi-user.target \\"
        norm = _normalise_unit_body(body)
        targets = re.search(r"^WantedBy[ \t]*=[ \t]*(.*)$", norm, re.MULTILINE).group(1).split()
        assert targets == ["multi-user.target"], f"parsed targets {targets}"

    def test_normaliser_still_joins_real_continuations(self):
        """The comment fix must not break the case the join exists for."""
        body = "[Install]\nWantedBy=multi-user.target \\\n         graphical.target\n"
        # Whitespace-tolerant: the join can leave a double space (the space
        # before the backslash plus the joined one). Every consumer splits on
        # whitespace, so the property is "both targets present and separated",
        # not "exactly one space".
        assert re.search(r"multi-user\.target\s+graphical\.target", _normalise_unit_body(body))

    def test_directive_list_covers_what_the_smoke_test_verifies(self):
        """The smoke test verifies WantedBy= and RequiredBy= by symlink. Both
        must be in the list this file checks, or a unit could pass here on a
        directive the image gate then ignores."""
        for d in ("WantedBy", "RequiredBy"):
            assert d in INSTALL_DIRECTIVES

    def test_smoke_test_also_hard_fails_on_a_directiveless_install(self):
        """Checked in BOTH places on purpose.

        This check was briefly moved out of 05 into pytest alone — until it
        turned out build-image.yml has no pytest step and no `needs:`, so
        "moved to pytest" meant "removed from the release path". It is back in
        05, narrowed, and pytest now also gates the build via a workflow step.
        A gate that lives in exactly one place is how it went missing.

        Narrow enough to be safe now: the four false-positive causes that made
        the original version reject legal units are all fixed — the full
        directive set, whitespace stripping, continuation joining, and comment
        deletion.
        """
        body = _strip_comments(open(SMOKE_TEST_SH).read())
        assert "no recognised install directive" in body, (
            "05-smoke-test no longer hard-fails on an [Install] section with no directive"
        )
        # The shell allow-list must be the same full set this file uses, or the
        # two disagree and a legal unit fails a 40-minute build.
        for d in INSTALL_DIRECTIVES:
            assert d in body, f"05-smoke-test's directive allow-list is missing {d}"

    def test_smoke_test_normalises_leading_whitespace_and_continuations(self):
        """systemd strstrip()s lines and honours `\\` continuations. Column-0
        anchors skipped an indented [Install] entirely (litclock-dev#547's shape via
        whitespace) and turned a continuation into a literal `\\` target."""
        body = _strip_comments(open(SMOKE_TEST_SH).read())
        assert "s/^[[:space:]]*//" in body, "05 must strip leading whitespace before matching [Install]"
        assert "/\\\\$/N" in body, "05 must join line continuations before splitting targets"


def _exclusion_list(path, var_name):
    """Parse a bash array of bare unit names, e.g. BUILD_ENABLE_EXCLUSIONS=(...)."""
    with open(path) as f:
        body = f.read()
    m = re.search(rf"^{var_name}=\((.*?)^\)", body, re.MULTILINE | re.DOTALL)
    assert m, f"{var_name} array not found in {path}"
    return sorted(line.strip() for line in m.group(1).splitlines() if line.strip() and not line.strip().startswith("#"))


class TestSystemdUnitsInStage:
    def test_stage_globs_units_instead_of_enumerating(self):
        """The cp list must stay derived. Re-introducing per-unit cp lines
        reopens the litclock-dev#547 / v0.211.0 bug class.

        Comments are stripped first: a commented-out glob must not satisfy the
        positive assertion, and a commented-out cp must not trip the negative.
        """
        body = _strip_comments(open(INSTALL_SERVICES_SH).read())
        # Whitespace/brace tolerant — a cosmetic reformat should not fail the
        # suite when behaviour is identical.
        assert re.search(
            r"units=\(\s*\"?\$\{?INSTALL_DIR\}?\"?/systemd/\*\.service\s+\"?\$\{?INSTALL_DIR\}?\"?/systemd/\*\.timer\s*\)",
            body,
        ), "03-install-services must glob systemd/ rather than naming units"
        # Deliberately broad — any per-unit copy spelling counts, quoted or not,
        # braced or not, indented or sudo-prefixed.
        hardcoded = re.findall(
            r"^\s*(?:sudo\s+)?(?:cp|install)\b[^\n]*?/systemd/[\w.\-]+\.(?:service|timer)",
            body,
            re.MULTILINE,
        )
        assert not hardcoded, f"per-unit cp/install lines reintroduced: {hardcoded}"

    def test_systemd_dir_holds_only_known_unit_types(self):
        """The three globs all assume .service/.timer. A .path, .socket or
        .target unit would be invisible to the copy loop, the enable guard, and
        the smoke test simultaneously — litclock-dev#547's shape, relocated from a name
        list to an extension list. Force an explicit decision instead."""
        unknown = [
            f
            for f in os.listdir(SYSTEMD_DIR)
            if os.path.isfile(os.path.join(SYSTEMD_DIR, f)) and not f.endswith(UNIT_SUFFIXES)
        ]
        assert not unknown, (
            f"unit types outside {UNIT_SUFFIXES} found in systemd/: {unknown}. "
            f"Widen the globs in 03-install-services, 05-smoke-test and UNIT_SUFFIXES together, "
            f"or these units will silently never reach an image."
        )

    def test_systemd_subdirectories_are_all_installed(self):
        """The meta-guard behind the one above.

        Every glob in 03 and 05 reads the FIRST level of systemd/ only, so a
        subdirectory is invisible to all of them simultaneously. That is not
        hypothetical: tmpfiles.d/ sat there for months installed by a single
        hardcoded `cp`, checked by nothing, while the PR that exists to close
        litclock-dev#547 added guards all around it. A new subdirectory must force an
        explicit decision — install path, derived smoke check, tests — rather
        than inheriting that silence.
        """
        subdirs = {f for f in os.listdir(SYSTEMD_DIR) if os.path.isdir(os.path.join(SYSTEMD_DIR, f))}
        unknown = subdirs - KNOWN_SYSTEMD_SUBDIRS
        assert not unknown, (
            f"unhandled subdirectories in systemd/: {sorted(unknown)}. The unit globs read the "
            f"first level only, so nothing in here reaches an image unless 03-install-services "
            f"copies it, 05-smoke-test verifies it, and KNOWN_SYSTEMD_SUBDIRS names it."
        )

    def test_repo_names_cannot_shadow_distro_files(self):
        """The enumerated lists made a name collision impossible; a glob does not.

        /etc/tmpfiles.d shadows /usr/lib/tmpfiles.d BY BASENAME, and
        /etc/systemd/system shadows the vendor unit directory the same way. So a
        repo file named tmp.conf, var.conf or home.conf would silently override a
        distro rule that owns /tmp, /var/log or /home — and these globs now copy
        it to every fielded Pi over OTA. Suffix checks cannot see this; only a
        name check can.
        """
        allowed = ("litclock", "wifi-watchdog")
        offenders = [name for name in _repo_units() + _repo_tmpfiles() if not name.startswith(allowed)]
        assert not offenders, (
            f"files in systemd/ whose names are not clearly ours: {offenders}. "
            f"/etc/tmpfiles.d and /etc/systemd/system shadow the distro's copies by basename, "
            f"so a generic name silently overrides a vendor rule on every device."
        )

    def test_tmpfiles_dir_holds_only_conf_files(self):
        """systemd-tmpfiles only reads *.conf from /etc/tmpfiles.d/, and both
        stage scripts glob *.conf. A file with any other suffix would be copied
        by nothing and read by nothing."""
        unknown = [
            f
            for f in os.listdir(TMPFILES_DIR)
            if os.path.isfile(os.path.join(TMPFILES_DIR, f)) and not f.endswith(".conf")
        ]
        assert not unknown, (
            f"non-.conf files in systemd/tmpfiles.d/: {unknown}. systemd-tmpfiles ignores "
            f"anything but *.conf, and so do the globs in 03-install-services and 05-smoke-test."
        )

    def test_stage_globs_tmpfiles_instead_of_naming_the_file(self):
        """litclock-dev#547's shape, one directory down. `cp .../tmpfiles.d/litclock.conf`
        is exactly the hand-maintained spelling that drifted three times for
        units, and it is worse here: a missing drop-in produces no error at all,
        just a clock with no /run/litclock and permanently stale status."""
        body = _strip_comments(open(INSTALL_SERVICES_SH).read())
        assert re.search(
            r"tmpfiles_confs=\(\s*\"?\$\{?INSTALL_DIR\}?\"?/systemd/tmpfiles\.d/\*\.conf\s*\)",
            body,
        ), "03-install-services must glob systemd/tmpfiles.d/ rather than naming the drop-in"
        hardcoded = re.findall(
            r"^\s*(?:sudo\s+)?(?:cp|install)\b[^\n]*?/systemd/tmpfiles\.d/[\w.\-]+\.conf",
            body,
            re.MULTILINE,
        )
        assert not hardcoded, f"per-file cp/install lines reintroduced: {hardcoded}"

    def test_smoke_test_derives_tmpfiles_from_source_tree(self):
        """The drop-in must be verified in the repo->image direction like the
        units, AND asserted by absolute path as an independent backstop. The
        derived check catches drift; the absolute path catches a corrupted
        source tree that both derived checks would agree with."""
        body = _strip_comments(open(SMOKE_TEST_SH).read())
        assert 'src_tmpfiles=( "$SRC_TMPFILES_DIR"/*.conf )' in body, (
            "smoke test must derive its tmpfiles set from the source tree"
        )
        # The derivation itself sits outside the sentinels (the harness below
        # injects its own value), so assert it here or it is covered nowhere.
        assert 'SRC_TMPFILES_DIR="${SRC_SYSTEMD_DIR}/tmpfiles.d"' in body, (
            "SRC_TMPFILES_DIR must be derived from SRC_SYSTEMD_DIR, not named independently"
        )
        assert "/etc/tmpfiles.d/litclock.conf" in body, (
            "the tmpfiles drop-in lost its absolute-path backstop in required_files"
        )

    def test_unit_syntax_check_verifies_the_staged_file_not_a_unit_name(self):
        """`systemd-analyze verify litclock.service` resolves the name through
        the unit load path, so it verifies whatever the CHROOT already has
        installed — which, for a stale unit from a reused CLEAN=0 work dir, is
        not the file this build staged. `-- "$src"` pins it to the path. The
        `--` also keeps a unit whose name begins with a dash from being read as
        an option. Reverting to the bare-name form leaves the build green while
        syntax-checking the wrong file, so it is asserted here — the loop
        itself needs a working systemd to run, which the harness has not got.
        """
        body = _strip_comments(open(SMOKE_TEST_SH).read())
        assert 'systemd-analyze verify -- "$src"' in body, (
            "the unit syntax check must verify the staged PATH, not a bare unit name"
        )
        # Command position only — an `echo "systemd-analyze verify exited ..."`
        # diagnostic is not an invocation.
        bare = [
            ln.strip()
            for ln in body.splitlines()
            if re.search(r"(?:^|[|(`]|\$\()\s*systemd-analyze verify (?!-- )", ln)
        ]
        assert not bare, f"bare-name systemd-analyze verify reintroduced: {bare}"

    def test_stage_scripts_have_no_env_overridable_destination_seams(self):
        """Regression guard for the seam removed in 2f2d14be.

        on_chroot uses capsh, which does not scrub the environment. A
        `${ETC_SYSTEMD_DIR:-/etc/systemd/system}` seam lets a stray exported
        variable redirect where a ROOT-run build script installs AND where it
        verifies the result — point it at the source tree and every `cmp -s`
        self-compares, so all three checks pass green on an uninspected image.
        The tests inject their own assignments ahead of the sentinel blocks, so
        no seam is needed for testability.
        """
        for path in (INSTALL_SERVICES_SH, SMOKE_TEST_SH):
            body = _strip_comments(open(path).read())
            seams = re.findall(r"\$\{(?:SRC|ETC)_(?:SYSTEMD|TMPFILES)_DIR:[-=?+]", body)
            assert not seams, (
                f"{os.path.basename(os.path.dirname(path))} reintroduced an env-overridable "
                f"destination seam: {seams}. Assign unconditionally."
            )

    def test_destination_paths_are_pinned_to_their_real_values(self):
        """Forbidding the `:-` seam is not enough — it never pinned the VALUE.

        These assignments sit outside every sentinel and the harness injects its
        own paths, so their real values were exercised by nothing. A single typo
        (`ETC_TMPFILES_DIR=/etc/tmpfile.d`) survived the whole suite green, and
        because the copy loop runs `install -d` it CREATES the wrong directory —
        so stage 03 exits 0 printing "OK: copied 1 tmpfiles.d drop-in(s)" on an
        image whose tmpfiles rules are never read.
        """
        expected = {
            INSTALL_SERVICES_SH: {
                "ETC_SYSTEMD_DIR": "/etc/systemd/system",
                "ETC_TMPFILES_DIR": "/etc/tmpfiles.d",
            },
            SMOKE_TEST_SH: {
                "SRC_SYSTEMD_DIR": "/home/pi/litclock/systemd",
                "ETC_SYSTEMD_DIR": "/etc/systemd/system",
                "ETC_TMPFILES_DIR": "/etc/tmpfiles.d",
            },
        }
        for path, wanted in expected.items():
            body = open(path).read()
            where = os.path.basename(os.path.dirname(path))
            for var, value in wanted.items():
                found = re.findall(rf"^{var}=(\S+)$", body, re.MULTILINE)
                assert found == [value], f"{where}: {var} is {found}, expected exactly ['{value}']"

    def test_every_installer_copies_what_it_enables(self):
        """The invariant, applied to BOTH installers rather than pi-gen only.

        This PR globbed pi-gen and update.sh and added 30 tests enforcing the
        rule for pi-gen — while scripts/install.sh sat two directories away
        running `systemctl enable litclock-reresolve-location.service` for a unit
        its enumerated cp list never copied, under `set -e`, so the enable failed
        and aborted the whole DIY install. Enforcing a rule on one of three
        implementations is how it came back.
        """
        installers = {
            "pi-gen/stage3/03-install-services": INSTALL_SERVICES_SH,
            "scripts/update.sh": os.path.join(REPO_ROOT, "scripts", "update.sh"),
        }
        # Assert the floor rather than silently skipping: review demonstrated
        # that renaming BOTH paths away left this test green with zero
        # assertions executed. Same anti-vacuity pattern as _repo_units().
        assert len(installers) == 2, f"expected 2 installers, got {sorted(installers)}"
        for label, path in installers.items():
            assert os.path.isfile(path), f"{label} is missing — this test would otherwise pass vacuously"
            body = _strip_comments(open(path).read())
            enabled = set(re.findall(r"systemctl enable ([\w.\-]+)", body))

            # Which suffixes does this installer's glob actually cover? The
            # earlier version asked a yes/no question — `re.search(r"/systemd/
            # \*\.service")` — and `continue`d on a hit, which every installer
            # scored, so the assertion below was dead code on all three paths.
            # Verified: dropping the `*.timer` term from install.sh's glob left
            # the whole suite green while install.sh enabled six timers it never
            # copied, under `set -e`. A test written to close that exact bug,
            # blind to it. Collect the covered suffixes instead of a boolean.
            globbed = set(re.findall(r"/systemd/\*\.(\w+)", body))
            copied = set(re.findall(r"/systemd/([\w.\-]+\.(?:service|timer))", body))

            missing = sorted(u for u in enabled if u not in copied and u.rsplit(".", 1)[-1] not in globbed)
            assert not missing, (
                f"{label} enables units it neither copies explicitly nor covers with a glob: "
                f"{missing} (globbed suffixes: {sorted(globbed) or 'none'}). "
                f"Under `set -e` the enable aborts the whole install (the #14 bug); "
                f"without it the unit ships disabled forever (litclock-dev#547)."
            )

    def test_tmpfiles_floors_agree_and_are_real_floors(self):
        stage_floor = _shell_floor(INSTALL_SERVICES_SH, "MIN_EXPECTED_TMPFILES")
        smoke_floor = _shell_floor(SMOKE_TEST_SH, "MIN_EXPECTED_TMPFILES")
        assert stage_floor == smoke_floor, (
            f"tmpfiles floors drifted: 03-install-services={stage_floor}, 05-smoke-test={smoke_floor}"
        )
        # There is only one drop-in today, so unlike the unit floor this one is
        # allowed to EQUAL the real count. It must still be >= 1, or the glob
        # matching nothing — the case it exists for — would pass.
        assert stage_floor >= 1, "a floor of 0 cannot catch a glob that matched nothing"
        assert stage_floor <= len(_repo_tmpfiles()), (
            f"floor {stage_floor} exceeds the real drop-in count {len(_repo_tmpfiles())}"
        )

    def test_floors_agree_and_sit_below_the_real_count(self):
        """Both stage scripts carry MIN_EXPECTED_UNITS. They must match, and the
        floor must actually be a floor — a floor at or above the real count
        would fail every build."""
        stage_floor = _shell_floor(INSTALL_SERVICES_SH)
        smoke_floor = _shell_floor(SMOKE_TEST_SH)
        assert stage_floor == smoke_floor, (
            f"floors drifted: 03-install-services={stage_floor}, 05-smoke-test={smoke_floor}"
        )
        assert stage_floor < len(_repo_units()), (
            f"floor {stage_floor} is not below the real unit count {len(_repo_units())}"
        )

    def test_every_install_unit_is_enabled_or_explicitly_excluded(self):
        """A unit shipping an [Install] section must either be enabled at build
        time or named in BUILD_ENABLE_EXCLUSIONS. This is the guard that would
        have caught litclock-reresolve-location.service (litclock-dev#547): it has
        [Install] WantedBy=multi-user.target and was in neither place."""
        body = _strip_comments(open(INSTALL_SERVICES_SH).read())
        enabled = set(re.findall(r"^systemctl enable ([\w.\-]+)", body, re.MULTILINE))
        excluded = set(_exclusion_list(INSTALL_SERVICES_SH, "BUILD_ENABLE_EXCLUSIONS"))

        missing = [u for u in _units_with_install_section() if u not in enabled and u not in excluded]
        assert not missing, (
            f"units carry [Install] but are neither enabled at build time nor listed in "
            f"BUILD_ENABLE_EXCLUSIONS: {missing}. Either enable them, or add them to the "
            f"exclusion list with a comment explaining why they must not be enabled."
        )

    def test_reresolve_location_is_enabled(self):
        """litclock-dev#547 regression. Copy alone does not fix it: update.sh's
        `was_pre_existing` check treats an already-present unit file as
        pre-existing and never auto-enables it,
        so an image that copies without enabling strands the unit disabled
        forever on every Pi flashed from it."""
        body = _strip_comments(open(INSTALL_SERVICES_SH).read())
        assert "systemctl enable litclock-reresolve-location.service" in body

    def test_enabled_units_all_exist_in_repo(self):
        """Inverse direction: nothing is enabled that we do not ship."""
        body = _strip_comments(open(INSTALL_SERVICES_SH).read())
        for unit in re.findall(r"^systemctl enable ([\w.\-]+)", body, re.MULTILINE):
            assert unit in _repo_units(), f"stage enables {unit} but it is not in systemd/"

    def test_exclusion_lists_agree_across_stage_and_smoke_test(self):
        """03 and 05 each carry an exclusion list. They must stay identical or
        the smoke test would silently stop verifying a real exclusion."""
        assert _exclusion_list(INSTALL_SERVICES_SH, "BUILD_ENABLE_EXCLUSIONS") == _exclusion_list(
            SMOKE_TEST_SH, "SMOKE_ENABLE_EXCLUSIONS"
        )

    def test_exclusions_name_real_install_units(self):
        """Checking the two lists against each other proves only that they
        agree, not that they mean anything. A typo (litclock.timers) or a name
        left behind after a unit is deleted would agree with itself forever
        while protecting nothing, and the shell-side sanity loop cannot catch
        it either — it probes for a path that will never exist, so it always
        passes."""
        excluded = set(_exclusion_list(INSTALL_SERVICES_SH, "BUILD_ENABLE_EXCLUSIONS"))
        install_units = set(_units_with_install_section())
        bogus = excluded - install_units
        assert not bogus, (
            f"BUILD_ENABLE_EXCLUSIONS names units that do not exist or carry no [Install] "
            f"section: {sorted(bogus)}. A stale exclusion silently protects nothing."
        )

    def test_smoke_test_derives_units_from_source_tree(self):
        """05-smoke-test previously named 7 of 20 units by hand, so it passed
        green on the litclock-dev#547 bug it existed to catch."""
        body = _strip_comments(open(SMOKE_TEST_SH).read())
        assert 'src_units=( "$SRC_SYSTEMD_DIR"/*.service' in body, (
            "smoke test must derive its unit set from the source tree"
        )
        # Three absolute paths are deliberate: they are an INDEPENDENT backstop
        # that does not read the source tree, so source corruption fails them
        # even when the derived check agrees with a wrong tree. Anything beyond
        # these three is drift.
        intentional = {
            "/etc/systemd/system/litclock.service",
            "/etc/systemd/system/litclock.timer",
            "/etc/systemd/system/wifi-watchdog.service",
        }
        found_paths = {p.strip() for p in re.findall(r"^\s+(/etc/systemd/system/[\w.\-]+)$", body, re.MULTILINE)}
        assert intentional <= found_paths, (
            f"the independent load-bearing backstop was removed: {sorted(intentional - found_paths)}. "
            f"Without it, a corrupted source tree passes both gates (they read the same tree)."
        )
        stale = found_paths - intentional
        assert not stale, f"hardcoded unit paths beyond the intentional backstop: {sorted(stale)}"


# ── The copy guard must FAIL, not merely print ───────────────────────


def _extract_block(path, name):
    """Pull a sentinel-bracketed block out of a stage script, so these tests
    exercise the shipped assertions rather than a reimplementation of them.
    If someone edits the block, these tests run the edited version.

    Anchored on explicit sentinels, not on a human-readable log line. With a
    log-line anchor the covered region could silently SHRINK (a guard added
    after the echo would go unexercised with no test failure), and a cosmetic
    reword of the echo would hard-fail the suite for a non-behavioural edit.
    """
    body = open(path).read()
    m = re.search(rf"^# BEGIN {name}\b.*?$(.*?)^# END {name}\b", body, re.MULTILINE | re.DOTALL)
    assert m, f"{name} sentinels not found in {os.path.basename(os.path.dirname(path))}/00-run.sh"
    return m.group(1)


def _extract_copy_guard():
    guard = _extract_block(INSTALL_SERVICES_SH, "copy-guard")
    assert "MIN_EXPECTED_UNITS" in guard, "copy-guard region no longer contains the floor check"
    assert 'copied" -ne "$found' in guard, "copy-guard region no longer contains the equality check"
    return guard


def _extract_tmpfiles_guard():
    guard = _extract_block(INSTALL_SERVICES_SH, "tmpfiles-guard")
    assert "MIN_EXPECTED_TMPFILES" in guard, "tmpfiles-guard region no longer contains the floor check"
    assert 'tmpfiles_copied" -ne "$tmpfiles_found' in guard, (
        "tmpfiles-guard region no longer contains the equality check"
    )
    return guard


def _run_tmpfiles_guard(found, copied):
    script = f"set -e\ntmpfiles_found={found}\ntmpfiles_copied={copied}\n{_extract_tmpfiles_guard()}\n"
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=15)


def _run_copy_guard(found, copied):
    script = f"set -e\nfound={found}\ncopied={copied}\n{_extract_copy_guard()}\n"
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=15)


class TestCopyGuardFailsLoudly:
    """A gate that prints a mismatch and exits 0 is not a gate.

    Each case feeds the SHIPPED guard a tampered input and asserts a non-zero
    exit. Without these, the guard could be silently broken by a refactor and
    every build would keep passing — which is exactly how the hardcoded lists
    this PR replaces managed to drift unnoticed for three releases.
    """

    def test_healthy_case_passes(self):
        real = len(_repo_units())
        assert _run_copy_guard(real, real).returncode == 0

    def test_zero_units_fails(self):
        """The catastrophic case: glob matched nothing, image would be inert."""
        result = _run_copy_guard(0, 0)
        assert result.returncode != 0
        assert "floor" in result.stderr.lower()

    def test_partial_source_tree_fails(self):
        """Half-failed 01-setup-app: found == copied, but both are small.
        The equality check alone cannot catch this; the floor is what does."""
        assert _run_copy_guard(3, 3).returncode != 0

    def test_copy_count_mismatch_fails(self):
        assert _run_copy_guard(20, 19).returncode != 0


class TestTmpfilesGuardFailsLoudly:
    """Same treatment for the tmpfiles copy. The floor matters MORE here than
    for units: a missing unit eventually surfaces as a service that will not
    start, while a missing /run/litclock surfaces as nothing whatsoever."""

    def test_healthy_case_passes(self):
        real = len(_repo_tmpfiles())
        assert _run_tmpfiles_guard(real, real).returncode == 0

    def test_zero_dropins_fails(self):
        """The glob matched nothing — `cp` would have failed under errexit,
        a glob exits 0 and ships an image with no /run/litclock."""
        result = _run_tmpfiles_guard(0, 0)
        assert result.returncode != 0
        assert "floor" in result.stderr.lower()

    def test_copy_count_mismatch_fails(self):
        assert _run_tmpfiles_guard(2, 1).returncode != 0


# ── The 03 copy + exclusion LOOPS, not just their counters ───────────
#
# The guards above only ever saw integers. Everything that decides what
# actually lands in the image — the glob, the symlink rejection, the
# `install -m 0644` mode, the every-*.wants exclusion probe — sat outside any
# sentinel and was verified by nothing. Four of the five behavioural changes in
# this PR survived outright deletion with the whole suite green.


_INSTALL_STUB = r"""#!/bin/bash
# Records argv, then delegates to the real install with -o/-g stripped: the
# harness does not run as root, so ownership is asserted from the recorded
# arguments while mode and content are asserted from the real files.
printf '%s\n' "$*" >> "$INSTALL_LOG"
args=(); skip=0
for a in "$@"; do
    if [ "$skip" = 1 ]; then skip=0; continue; fi
    case "$a" in
        -o|-g) skip=1 ;;
        *) args+=( "$a" ) ;;
    esac
done
exec /usr/bin/install "${args[@]}"
"""


def _run_stage_block(name, tmp_path, assigns, preamble=""):
    """Run a sentinel block from 03-install-services under bash.

    Returns (CompletedProcess, recorded `install` argv lines).
    """
    bindir = tmp_path / "stubbin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "install"
    stub.write_text(_INSTALL_STUB)
    stub.chmod(0o755)
    log = tmp_path / "install.log"

    script = (
        "set -e\n"
        + "".join(f"{k}={shlex.quote(str(v))}\n" for k, v in assigns.items())
        + f"{preamble}\n"
        + f"{_extract_block(INSTALL_SERVICES_SH, name)}\n"
    )
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "INSTALL_LOG": str(log)}
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=15, env=env)
    return r, (log.read_text().splitlines() if log.exists() else [])


def _fake_install_dir(tmp_path, units=None, confs=None):
    """A miniature INSTALL_DIR plus empty destination trees."""
    install_dir = tmp_path / "litclock"
    (install_dir / "systemd" / "tmpfiles.d").mkdir(parents=True, exist_ok=True)
    for name, body in (units or {}).items():
        (install_dir / "systemd" / name).write_text(body)
    for name, body in (confs or {}).items():
        (install_dir / "systemd" / "tmpfiles.d" / name).write_text(body)
    etc = tmp_path / "etc-systemd"
    etc_tmpfiles = tmp_path / "etc-tmpfiles"
    etc.mkdir(exist_ok=True)
    etc_tmpfiles.mkdir(exist_ok=True)
    return install_dir, etc, etc_tmpfiles


class TestUnitCopyLoop:
    """The loop that decides what reaches /etc/systemd/system/."""

    def _assigns(self, install_dir, etc, etc_tmpfiles):
        return {"INSTALL_DIR": install_dir, "ETC_SYSTEMD_DIR": etc, "ETC_TMPFILES_DIR": etc_tmpfiles}

    def test_globs_every_unit_and_counts_them(self, tmp_path):
        """A glob cannot forget a new unit — that is the whole point of the
        change, and nothing was checking that the glob ran at all."""
        install_dir, etc, etc_t = _fake_install_dir(
            tmp_path, units={"a.service": PLAIN, "b.timer": PLAIN, "c.service": PLAIN}
        )
        r, _ = _run_stage_block("unit-copy-loop", tmp_path, self._assigns(install_dir, etc, etc_t))
        assert r.returncode == 0, r.stderr
        assert sorted(p.name for p in etc.iterdir()) == ["a.service", "b.timer", "c.service"]

    def test_non_unit_suffixes_are_not_copied(self, tmp_path):
        """The glob is *.service/*.timer. A README or a .conf in systemd/ must
        not be shovelled into /etc/systemd/system/."""
        install_dir, etc, etc_t = _fake_install_dir(
            tmp_path, units={"a.service": PLAIN, "README.md": "x\n", "notes.conf": "y\n"}
        )
        r, _ = _run_stage_block("unit-copy-loop", tmp_path, self._assigns(install_dir, etc, etc_t))
        assert r.returncode == 0, r.stderr
        assert [p.name for p in etc.iterdir()] == ["a.service"]

    def test_executable_source_lands_non_executable(self, tmp_path):
        """`install -m 0644` rather than `cp`. cp applies the SOURCE mode, so a
        unit committed with the exec bit set would land executable in
        /etc/systemd/system/. Reverting to cp reds this test.

        Run over a MULTI-unit fixture: with one unit, a per-file guarantee and a
        once-anywhere guarantee are indistinguishable.
        """
        install_dir, etc, etc_t = _fake_install_dir(
            tmp_path, units={"a.service": PLAIN, "b.timer": PLAIN, "c.service": PLAIN}
        )
        (install_dir / "systemd" / "a.service").chmod(0o755)
        (install_dir / "systemd" / "c.service").chmod(0o600)
        r, calls = _run_stage_block("unit-copy-loop", tmp_path, self._assigns(install_dir, etc, etc_t))
        assert r.returncode == 0, r.stderr
        for name in ("a.service", "b.timer", "c.service"):
            mode = stat.S_IMODE((etc / name).stat().st_mode)
            assert mode == 0o644, f"{name} landed with mode {oct(mode)}"
        # Ownership cannot be asserted as an effect without root, so assert the
        # intent from the recorded argv. `all`, not `any`: the tmpfiles loop
        # already logs two calls, so a future `install -d -o root -g root` would
        # satisfy an `any` after the file copy lost its ownership flags.
        copy_calls = [c for c in calls if not c.startswith("-d")]
        assert len(copy_calls) == 3, f"expected one install call per unit, got {copy_calls}"
        assert all("-m 0644 -o root -g root" in c for c in copy_calls), (
            f"not every unit was installed 0644 root:root — {copy_calls}"
        )

    def test_symlinked_unit_is_rejected(self, tmp_path):
        """01-setup-app stages with `cp -a`, which preserves symlinks. A
        symlinked unit would have its target's content silently copied out."""
        install_dir, etc, etc_t = _fake_install_dir(tmp_path, units={"real.service": PLAIN})
        (install_dir / "systemd" / "link.timer").symlink_to(install_dir / "systemd" / "real.service")
        r, _ = _run_stage_block("unit-copy-loop", tmp_path, self._assigns(install_dir, etc, etc_t))
        assert r.returncode != 0
        assert "not a regular file" in r.stderr

    def test_directory_named_like_a_unit_is_rejected(self, tmp_path):
        install_dir, etc, etc_t = _fake_install_dir(tmp_path, units={"a.service": PLAIN})
        (install_dir / "systemd" / "weird.service").mkdir()
        r, _ = _run_stage_block("unit-copy-loop", tmp_path, self._assigns(install_dir, etc, etc_t))
        assert r.returncode != 0
        assert "not a regular file" in r.stderr

    def test_empty_source_tree_exits_zero_with_found_zero(self, tmp_path):
        """The loop itself cannot fail here — an unmatched glob is not an
        error. That is precisely why the copy-guard exists downstream, and this
        test pins the seam between the two so neither is assumed to cover it."""
        install_dir, etc, etc_t = _fake_install_dir(tmp_path)
        r, _ = _run_stage_block(
            "unit-copy-loop", tmp_path, self._assigns(install_dir, etc, etc_t), preamble="trap 'echo found=$found' EXIT"
        )
        assert r.returncode == 0
        assert "found=0" in r.stdout


class TestTmpfilesCopyLoop:
    def _assigns(self, install_dir, etc, etc_tmpfiles):
        return {"INSTALL_DIR": install_dir, "ETC_SYSTEMD_DIR": etc, "ETC_TMPFILES_DIR": etc_tmpfiles}

    def test_globs_every_dropin(self, tmp_path):
        install_dir, etc, etc_t = _fake_install_dir(
            tmp_path, confs={"litclock.conf": TMPFILES_BODY, "extra.conf": TMPFILES_BODY}
        )
        r, _ = _run_stage_block("tmpfiles-copy-loop", tmp_path, self._assigns(install_dir, etc, etc_t))
        assert r.returncode == 0, r.stderr
        assert sorted(p.name for p in etc_t.iterdir()) == ["extra.conf", "litclock.conf"]

    def test_executable_source_lands_non_executable(self, tmp_path):
        install_dir, etc, etc_t = _fake_install_dir(tmp_path, confs={"litclock.conf": TMPFILES_BODY})
        (install_dir / "systemd" / "tmpfiles.d" / "litclock.conf").chmod(0o777)
        r, calls = _run_stage_block("tmpfiles-copy-loop", tmp_path, self._assigns(install_dir, etc, etc_t))
        assert r.returncode == 0, r.stderr
        assert stat.S_IMODE((etc_t / "litclock.conf").stat().st_mode) == 0o644
        copy_calls = [c for c in calls if not c.startswith("-d")]
        assert copy_calls and all("-m 0644 -o root -g root" in c for c in copy_calls), (
            f"drop-in was not installed 0644 root:root — {copy_calls}"
        )

    def test_symlinked_dropin_is_rejected(self, tmp_path):
        install_dir, etc, etc_t = _fake_install_dir(tmp_path, confs={"real.conf": TMPFILES_BODY})
        d = install_dir / "systemd" / "tmpfiles.d"
        (d / "link.conf").symlink_to(d / "real.conf")
        r, _ = _run_stage_block("tmpfiles-copy-loop", tmp_path, self._assigns(install_dir, etc, etc_t))
        assert r.returncode != 0
        assert "not a regular file" in r.stderr

    def test_destination_directory_is_created(self, tmp_path):
        """/etc/tmpfiles.d exists on Debian, but the loop must not depend on
        that — `install -d` is what makes the copy safe on any rootfs."""
        install_dir, etc, etc_t = _fake_install_dir(tmp_path, confs={"litclock.conf": TMPFILES_BODY})
        etc_t.rmdir()
        r, _ = _run_stage_block("tmpfiles-copy-loop", tmp_path, self._assigns(install_dir, etc, etc_t))
        assert r.returncode == 0, r.stderr
        assert (etc_t / "litclock.conf").is_file()
        # Mode matters as much as existence: `install -d -m 0777` survived the
        # whole suite green, which is a world-writable /etc/tmpfiles.d created by
        # a root-run build script on a shipped image.
        mode = stat.S_IMODE(etc_t.stat().st_mode)
        assert mode == 0o755, f"/etc/tmpfiles.d created with mode {oct(mode)}"


class TestEnableExclusionCheck:
    """A guard that cannot fail is the exact failure mode 03 exists to remove.

    Before this class, reverting the every-*.wants probe to the old hardcoded
    multi-user.target pair left the suite fully green — the same defect this PR
    fixes in the 05 counterpart, unfixed in 03 because nothing tested it.
    """

    def _run(self, tmp_path, etc, exclusions):
        arr = " ".join(exclusions)
        return _run_stage_block(
            "enable-exclusion-check",
            tmp_path,
            {"ETC_SYSTEMD_DIR": etc},
            preamble=f"BUILD_ENABLE_EXCLUSIONS=( {arr} )",
        )[0]

    def _enable(self, etc, target, unit):
        d = etc / f"{target}.wants"
        d.mkdir(exist_ok=True)
        (d / unit).write_text("")

    def test_excluded_unit_left_disabled_passes(self, tmp_path):
        _, etc, _ = _fake_install_dir(tmp_path)
        self._enable(etc, "multi-user.target", "something-else.service")
        assert self._run(tmp_path, etc, ["litclock.timer"]).returncode == 0

    def test_excluded_unit_enabled_in_multi_user_fails(self, tmp_path):
        """The obvious case: an `systemctl enable litclock.timer` line added
        above without removing it from the exclusion list."""
        _, etc, _ = _fake_install_dir(tmp_path)
        self._enable(etc, "multi-user.target", "litclock.timer")
        r = self._run(tmp_path, etc, ["litclock.timer"])
        assert r.returncode != 0
        assert "deliberate" in r.stderr.lower() or "BUILD_ENABLE_EXCLUSIONS" in r.stderr

    def test_excluded_unit_enabled_in_an_unexpected_target_fails(self, tmp_path):
        """The reason the probe globs every *.wants. A unit enabled into
        timers.target, sockets.target or a custom target sails past a
        hardcoded multi-user/timers pair while genuinely being enabled — and
        litclock.timer's own [Install] is WantedBy=timers.target, so the
        hardcoded pair was checking the wrong directory for the one unit on
        the list."""
        _, etc, _ = _fake_install_dir(tmp_path)
        self._enable(etc, "sockets.target", "litclock.timer")
        assert self._run(tmp_path, etc, ["litclock.timer"]).returncode != 0

    def test_empty_exclusion_list_passes(self, tmp_path):
        """`set -u` is not in force here, but an empty array must not explode
        or silently short-circuit into a pass that means nothing."""
        _, etc, _ = _fake_install_dir(tmp_path)
        assert self._run(tmp_path, etc, []).returncode == 0


# ── The smoke-test loops must FAIL too ───────────────────────────────
#
# 05 is the last gate before an image is produced, and it runs ONLY inside a
# ~40 minute image build — a logic error there surfaces an hour in, or worse,
# silently stops catching things. Same treatment as the 03 guards: run the
# SHIPPED loop, not a reimplementation.


def _extract_smoke_block(name):
    return _extract_block(SMOKE_TEST_SH, name)


def _run_smoke_block(name, src_dir, etc_dir, preamble=""):
    # Exclusions parsed from the SHIPPED array, not retyped. A hardcoded
    # `( litclock.timer )` here would keep passing after the real list changed,
    # so the harness would be verifying a policy the build no longer has —
    # while _exclusion_list() already exists two hundred lines up.
    exclusions = " ".join(_exclusion_list(SMOKE_TEST_SH, "SMOKE_ENABLE_EXCLUSIONS"))
    script = (
        "set -e\n"
        f'SRC_SYSTEMD_DIR="{src_dir}"\n'
        f'ETC_SYSTEMD_DIR="{etc_dir}"\n'
        f"SMOKE_ENABLE_EXCLUSIONS=( {exclusions} )\n"
        f"{preamble}\n"
        f"{_extract_smoke_block(name)}\n"
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=15)


def _fake_tree(tmp_path, units, installed=None, wants=None, pad=True):
    """Build a miniature source + installed tree. units maps name -> file body.

    Padded to clear MIN_EXPECTED_UNITS by default: unit-install-check declares
    its own floor, so a fixture below it would trip the floor instead of the
    behaviour under test. Pass pad=False to exercise the floor itself.

    The padding is load-bearing for unit-install-check ONLY. enable-symlink-check
    declares no floor and every caller overrides src_units via preamble, so the
    pad units are inert there — do not read this as a guarantee for all callers.
    """
    src = tmp_path / "systemd"
    etc = tmp_path / "etc"
    src.mkdir(exist_ok=True)
    etc.mkdir(exist_ok=True)
    if pad:
        floor = _shell_floor(SMOKE_TEST_SH)
        units = dict(units)
        for i in range(floor):
            units.setdefault(f"pad{i}.service", PLAIN)
    for unit_name, body in units.items():
        (src / unit_name).write_text(body)
    to_install = units if installed is None else [*installed, *[u for u in units if u.startswith("pad")]]
    for unit_name in to_install:
        (etc / unit_name).write_text(units[unit_name])
    for target, names in (wants or {}).items():
        d = etc / f"{target}.wants"
        d.mkdir(exist_ok=True)
        for unit_name in names:
            (d / unit_name).write_text("")
    return str(src), str(etc)


PLAIN = "[Unit]\nDescription=x\n[Service]\nExecStart=/bin/true\n"
INSTALLED_UNIT = PLAIN + "[Install]\nWantedBy=multi-user.target\n"


class TestSmokeTestLoopsFailLoudly:
    """The 05 loops carry the same burden as the 03 guard: they must exit
    non-zero on a broken image, not merely print."""

    def test_all_installed_and_matching_passes(self, tmp_path):
        src, etc = _fake_tree(tmp_path, {"a.service": PLAIN, "b.timer": PLAIN})
        assert _run_smoke_block("unit-install-check", src, etc).returncode == 0

    def test_unit_never_installed_fails(self, tmp_path):
        """The litclock-dev#547 shape: present in systemd/, absent from the image.

        Asserted on the DISTINGUISHING text. Both this check and the `cmp -s`
        immediately after it name the offending unit, so `"b.timer" in stdout`
        passed just as happily when the never-installed branch was deleted and
        cmp caught it instead — which is a different failure with a different
        cause, and the one case where cmp cannot substitute is a missing
        destination file, exactly this one."""
        src, etc = _fake_tree(tmp_path, {"a.service": PLAIN, "b.timer": PLAIN}, installed=["a.service"])
        r = _run_smoke_block("unit-install-check", src, etc)
        assert r.returncode != 0
        assert "b.timer" in r.stdout
        assert "never installed" in r.stdout, f"masked by the cmp -s branch: {r.stdout!r}"

    def test_installed_unit_differing_from_source_fails(self, tmp_path):
        """Stale unit from a reused CLEAN=0 work dir satisfies -e but differs."""
        src, etc = _fake_tree(tmp_path, {"a.service": PLAIN, "b.timer": PLAIN})
        (tmp_path / "etc" / "a.service").write_text(PLAIN + "# drifted\n")
        assert _run_smoke_block("unit-install-check", src, etc).returncode != 0

    def test_empty_source_tree_fails(self, tmp_path):
        src, etc = _fake_tree(tmp_path, {}, pad=False)
        assert _run_smoke_block("unit-install-check", src, etc).returncode != 0

    def test_install_unit_missing_its_symlink_fails(self, tmp_path):
        """The .wants directory EXISTS and holds other units — the only state a
        real image is ever in. multi-user.target.wants/ always exists on a Pi,
        so a fixture with no directory at all let the guard be reduced to a
        container-existence check and stay green while being 100% inert in the
        field."""
        src, etc = _fake_tree(
            tmp_path, {"a.service": INSTALLED_UNIT}, wants={"multi-user.target": ["someone-else.service"]}
        )
        r = _run_smoke_block("enable-symlink-check", src, etc, preamble='src_units=( "$SRC_SYSTEMD_DIR"/a.service )')
        assert r.returncode != 0
        assert "no enable symlink" in r.stdout

    def test_install_unit_missing_its_symlink_and_its_wants_dir_fails(self, tmp_path):
        """The other half: no .wants directory at all. Rarer in the field, but
        it is what a half-run `systemctl enable` leaves behind."""
        src, etc = _fake_tree(tmp_path, {"a.service": INSTALLED_UNIT}, wants={})
        r = _run_smoke_block("enable-symlink-check", src, etc, preamble='src_units=( "$SRC_SYSTEMD_DIR"/a.service )')
        assert r.returncode != 0
        assert "no enable symlink" in r.stdout

    def test_install_unit_with_symlink_passes(self, tmp_path):
        src, etc = _fake_tree(tmp_path, {"a.service": INSTALLED_UNIT}, wants={"multi-user.target": ["a.service"]})
        assert (
            _run_smoke_block(
                "enable-symlink-check", src, etc, preamble='src_units=( "$SRC_SYSTEMD_DIR"/a.service )'
            ).returncode
            == 0
        )

    def test_excluded_unit_that_got_enabled_fails(self, tmp_path):
        """litclock.timer enabled at build time would start the clock before
        provisioning finishes and race splash/firstboot for GPIO."""
        src, etc = _fake_tree(
            tmp_path,
            {"litclock.timer": PLAIN + "[Install]\nWantedBy=timers.target\n"},
            wants={"timers.target": ["litclock.timer"]},
        )
        r = _run_smoke_block(
            "enable-symlink-check", src, etc, preamble='src_units=( "$SRC_SYSTEMD_DIR"/litclock.timer )'
        )
        assert r.returncode != 0
        assert "deliberate build-time exclusion" in r.stdout

    @pytest.mark.parametrize(
        "directive",
        ["Alias=other.service", "Also=other.service", "UpheldBy=other.target", "DefaultInstance=main"],
    )
    def test_unverifiable_directives_report_but_do_not_fail(self, tmp_path, directive):
        """Alias=, Also= and UpheldBy= are legal [Install] directives whose
        enablement the .wants/.requires convention cannot verify. Hard-failing
        on them blocked every build the moment someone shipped such a unit —
        UpheldBy= has been valid since systemd 249 and passes
        `systemd-analyze verify` clean."""
        src, etc = _fake_tree(tmp_path, {"a.service": PLAIN + f"[Install]\n{directive}\n"})
        r = _run_smoke_block("enable-symlink-check", src, etc, preamble='src_units=( "$SRC_SYSTEMD_DIR"/a.service )')
        assert r.returncode == 0, r.stdout
        assert "not verified here" in r.stdout

    def test_requiredby_is_verified_via_the_requires_dir(self, tmp_path):
        """RequiredBy= creates <target>.requires/<unit>, exactly as checkable as
        .wants/. It used to be lumped in with Alias=/Also= as unverifiable,
        which would have let a future RequiredBy= unit ship un-enabled behind a
        NOTE — litclock-dev#547 surviving inside the check built to eliminate it."""
        unit = PLAIN + "[Install]\nRequiredBy=multi-user.target\n"
        src, etc = _fake_tree(tmp_path, {"a.service": unit}, wants={"multi-user.target": ["someone-else.service"]})
        r = _run_smoke_block("enable-symlink-check", src, etc, preamble='src_units=( "$SRC_SYSTEMD_DIR"/a.service )')
        assert r.returncode != 0
        assert ".requires symlink" in r.stdout

        req = tmp_path / "etc" / "multi-user.target.requires"
        req.mkdir()
        (req / "a.service").write_text("")
        ok = _run_smoke_block("enable-symlink-check", src, etc, preamble='src_units=( "$SRC_SYSTEMD_DIR"/a.service )')
        assert ok.returncode == 0, ok.stdout

    def test_install_with_no_directive_at_all_hard_fails(self, tmp_path):
        """The literal litclock-dev#547 typo. `systemd-analyze verify` exits 0 on it
        (verified on systemd 255: `Unknown key name 'WnatedBy' ... ignoring.`,
        rc=0), so the syntax loop cannot catch it either — this is the only
        build-time gate that sees it."""
        src, etc = _fake_tree(tmp_path, {"a.service": PLAIN + "[Install]\nWnatedBy=multi-user.target\n"})
        r = _run_smoke_block("enable-symlink-check", src, etc, preamble='src_units=( "$SRC_SYSTEMD_DIR"/a.service )')
        assert r.returncode != 0, r.stdout
        assert "no recognised install directive" in r.stdout

    def test_backslash_comment_above_install_does_not_hide_the_unit(self, tmp_path):
        """systemd does NOT continue a comment line — a unit whose comment above
        [Install] ends in a backslash is enabled normally (verified against a
        real `systemctl --root=` tree). Joining that continuation turned the
        section header into `# comment [Install]`, so the unit was skipped by
        this entire check with no output: the silent-skip the normalisation was
        added to close, reintroduced by the normalisation."""
        unit = "[Unit]\nDescription=x\n# a trailing comment continuation \\\n[Install]\nWantedBy=multi-user.target\n"
        src, etc = _fake_tree(tmp_path, {"a.service": unit}, wants={"multi-user.target": ["someone-else.service"]})
        r = _run_smoke_block("enable-symlink-check", src, etc, preamble='src_units=( "$SRC_SYSTEMD_DIR"/a.service )')
        assert r.returncode != 0, f"unit was skipped instead of verified: {r.stdout!r}"
        assert "no enable symlink" in r.stdout

    def test_backslash_comment_inside_install_does_not_eat_wantedby(self, tmp_path):
        """Same bug one level in: `# note \\` inside [Install] swallowed the
        following WantedBy= and downgraded a verifiable unit to a NOTE."""
        unit = PLAIN + "[Install]\n# note \\\nWantedBy=multi-user.target\n"
        src, etc = _fake_tree(tmp_path, {"a.service": unit}, wants={"multi-user.target": ["a.service"]})
        r = _run_smoke_block("enable-symlink-check", src, etc, preamble='src_units=( "$SRC_SYSTEMD_DIR"/a.service )')
        assert r.returncode == 0, r.stdout
        assert "not verified here" not in r.stdout, "WantedBy= was swallowed by the comment continuation"

    def test_indented_install_section_is_still_verified(self, tmp_path):
        """systemd strstrip()s every line, so this unit is legal. A column-0
        `grep -q '^\\[Install\\]'` skipped it entirely and the loop verified
        nothing — litclock-dev#547's own failure mode via whitespace."""
        unit = "[Unit]\nDescription=x\n  [Install]\n  WantedBy=multi-user.target\n"
        src, etc = _fake_tree(tmp_path, {"a.service": unit}, wants={"multi-user.target": ["someone-else.service"]})
        r = _run_smoke_block("enable-symlink-check", src, etc, preamble='src_units=( "$SRC_SYSTEMD_DIR"/a.service )')
        assert r.returncode != 0, "indented [Install] was skipped instead of verified"
        assert "no enable symlink" in r.stdout

    def test_line_continuation_targets_are_all_verified(self, tmp_path):
        """`WantedBy=a.target \\` + `b.target` is legal and enables into BOTH.
        Unjoined it produced a literal `\\` target (hard-failing a healthy
        build) and dropped the continued target entirely."""
        unit = PLAIN + "[Install]\nWantedBy=multi-user.target \\\n         extra.target\n"
        src, etc = _fake_tree(
            tmp_path,
            {"a.service": unit},
            wants={"multi-user.target": ["a.service"], "extra.target": ["someone-else.service"]},
        )
        r = _run_smoke_block("enable-symlink-check", src, etc, preamble='src_units=( "$SRC_SYSTEMD_DIR"/a.service )')
        assert r.returncode != 0
        assert "WantedBy=extra.target but no enable symlink" in r.stdout
        assert "\\" not in r.stdout.split("WantedBy=")[1][:20], f"literal backslash parsed as a target: {r.stdout!r}"

    def test_comment_after_a_continuation_does_not_become_a_target(self, tmp_path):
        """The shell normaliser's version of the same case."""
        unit = PLAIN + "[Install]\nWantedBy=multi-user.target \\\n# trailing comment\n"
        src, etc = _fake_tree(tmp_path, {"a.service": unit}, wants={"multi-user.target": ["a.service"]})
        r = _run_smoke_block("enable-symlink-check", src, etc, preamble='src_units=( "$SRC_SYSTEMD_DIR"/a.service )')
        assert r.returncode == 0, f"a legal unit was rejected: {r.stdout!r}"

    def test_crlf_unit_is_parsed(self, tmp_path):
        """`tr -d '\\r'` was in the shipped code and exercised by no fixture."""
        unit = "[Unit]\r\nDescription=x\r\n[Install]\r\nWantedBy=multi-user.target\r\n"
        src, etc = _fake_tree(tmp_path, {"a.service": unit}, wants={"multi-user.target": ["a.service"]})
        r = _run_smoke_block("enable-symlink-check", src, etc, preamble='src_units=( "$SRC_SYSTEMD_DIR"/a.service )')
        assert r.returncode == 0, r.stdout

    def test_wantedby_outside_the_install_section_is_ignored(self, tmp_path):
        """The scoping the shipped comment justifies. Replacing the sed range
        with `cat "$src"` left every other test green, so a decoy WantedBy= in
        [Unit] proves the scope is real."""
        unit = "[Unit]\nWantedBy=decoy.target\n[Service]\nExecStart=/bin/true\n[Install]\nWantedBy=multi-user.target\n"
        src, etc = _fake_tree(tmp_path, {"a.service": unit}, wants={"multi-user.target": ["a.service"]})
        r = _run_smoke_block("enable-symlink-check", src, etc, preamble='src_units=( "$SRC_SYSTEMD_DIR"/a.service )')
        assert r.returncode == 0, f"decoy WantedBy= in [Unit] leaked into the check: {r.stdout!r}"
        assert "decoy.target" not in r.stdout

    def test_excluded_unit_left_disabled_passes(self, tmp_path):
        """The POSITIVE exclusion path. Deleting the `continue` after the
        exclusion probe hard-fails every real build ~40 minutes in, and no test
        covered it — only the strictness was pinned, not the tolerance."""
        unit = PLAIN + "[Install]\nWantedBy=timers.target\n"
        src, etc = _fake_tree(tmp_path, {"litclock.timer": unit}, wants={"timers.target": ["someone-else.service"]})
        r = _run_smoke_block(
            "enable-symlink-check", src, etc, preamble='src_units=( "$SRC_SYSTEMD_DIR"/litclock.timer )'
        )
        assert r.returncode == 0, r.stdout

    def test_unit_without_install_section_is_skipped(self, tmp_path):
        """The other tolerance `continue`. Deleting
        `grep -q '^\\[Install\\]' || continue` hard-fails every build; 8 of our
        units have no [Install] section."""
        src, etc = _fake_tree(tmp_path, {"a.service": PLAIN})
        r = _run_smoke_block("enable-symlink-check", src, etc, preamble='src_units=( "$SRC_SYSTEMD_DIR"/a.service )')
        assert r.returncode == 0, r.stdout

    def test_empty_src_units_fails_instead_of_passing_vacuously(self, tmp_path):
        """src_units is set in a DIFFERENT sentinel block 70 lines up. If it is
        ever empty every loop here is a no-op and the section prints its success
        line having verified nothing. The harness injects its own src_units for
        every other case, so without this the dependency is covered nowhere."""
        src, etc = _fake_tree(tmp_path, {"a.service": INSTALLED_UNIT})
        r = _run_smoke_block("enable-symlink-check", src, etc, preamble="src_units=( )")
        assert r.returncode != 0
        assert "src_units is empty" in r.stdout

    def test_success_line_does_not_claim_units_it_did_not_verify(self, tmp_path):
        """The NOTE was printed and the very next line still said "every
        [Install] unit enabled". A build log that contradicts itself one line
        later is a log nobody reads twice."""
        src, etc = _fake_tree(tmp_path, {"a.service": PLAIN + "[Install]\nAlias=other.service\n"})
        r = _run_smoke_block("enable-symlink-check", src, etc, preamble='src_units=( "$SRC_SYSTEMD_DIR"/a.service )')
        assert r.returncode == 0
        assert "1 unit(s) NOT verified" in r.stdout
        assert "OK: every [Install] unit enabled" not in r.stdout

    def test_success_line_is_unqualified_when_everything_was_verified(self, tmp_path):
        src, etc = _fake_tree(tmp_path, {"a.service": INSTALLED_UNIT}, wants={"multi-user.target": ["a.service"]})
        r = _run_smoke_block("enable-symlink-check", src, etc, preamble='src_units=( "$SRC_SYSTEMD_DIR"/a.service )')
        assert r.returncode == 0
        assert "OK: every [Install] unit enabled" in r.stdout
        assert "NOT verified" not in r.stdout

    def test_multi_target_wantedby_requires_every_target(self, tmp_path):
        """WantedBy=a.target b.target enables into BOTH; checking only the first
        would half-verify it."""
        unit = PLAIN + "[Install]\nWantedBy=multi-user.target extra.target\n"
        # extra.target.wants EXISTS and holds another unit — see the note on
        # test_install_unit_missing_its_symlink_fails.
        src, etc = _fake_tree(
            tmp_path,
            {"a.service": unit},
            wants={"multi-user.target": ["a.service"], "extra.target": ["someone-else.service"]},
        )
        r = _run_smoke_block("enable-symlink-check", src, etc, preamble='src_units=( "$SRC_SYSTEMD_DIR"/a.service )')
        assert r.returncode != 0
        # The discriminating form. Bare `"extra.target" in stdout` also passes if
        # word-splitting regressed and the loop iterated the single token
        # "multi-user.target extra.target" — failing for the wrong reason.
        assert "WantedBy=extra.target but no enable symlink" in r.stdout
        assert "WantedBy=multi-user.target extra.target" not in r.stdout


_ANALYZE_STUB = r"""#!/bin/bash
# Stand-in for systemd-analyze. Emits whatever ANALYZE_OUT holds and exits
# ANALYZE_RC, so the classification logic can be exercised without systemd.
# STDERR, not stdout. systemd-analyze writes ALL diagnostics to stderr and
# nothing to stdout (verified on 255), so a stub printing to stdout exercised a
# stream the real tool never uses — and deleting the shipped `2>&1` left every
# test in TestUnitSyntaxCheck green while the gate became a no-op.
printf '%s\n' "$ANALYZE_OUT" >&2
exit "${ANALYZE_RC:-0}"
"""


def _run_syntax_check(tmp_path, out, rc, units=("a.service",)):
    """Run 05's unit-syntax-check block against a stubbed systemd-analyze."""
    bindir = tmp_path / "analyzebin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "systemd-analyze"
    stub.write_text(_ANALYZE_STUB)
    stub.chmod(0o755)
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    for u in units:
        (src / u).write_text(PLAIN)
    listing = " ".join(f'"{src}/{u}"' for u in units)
    script = "set -e\n" + f"src_units=( {listing} )\n" + _extract_smoke_block("unit-syntax-check") + "\n"
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "ANALYZE_OUT": out,
        "ANALYZE_RC": str(rc),
    }
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=15, env=env)


class TestUnitSyntaxCheck:
    """Widening this loop from 7 hardcoded units to the whole tree made it
    receive the litclock-dev#547 evidence — and it was throwing it away.

    `systemd-analyze verify` reports "Command ... is not executable" for an
    ExecStart= whose target never reached the image. That message matched none of
    fatal_patterns, so the loop printed "(warnings only — not fatal)" and then
    "OK: all unit files pass". Five helpers referenced by units are in no
    required_files entry, so nothing else in the file would have noticed.
    """

    def test_clean_run_passes(self, tmp_path):
        r = _run_syntax_check(tmp_path, "", 0)
        assert r.returncode == 0, r.stdout
        assert "OK: all unit files pass" in r.stdout

    def test_parse_error_is_fatal(self, tmp_path):
        r = _run_syntax_check(tmp_path, "a.service: Failed to parse Unit section", 1)
        assert r.returncode != 0
        assert "has real errors" in r.stdout

    @pytest.mark.parametrize(
        "message",
        [
            "a.service: Command /usr/local/lib/litclock/litclock-set-timezone is not executable: Permission denied",
            "a.service: Command /usr/local/bin/missing.sh: No such file or directory",
        ],
    )
    def test_missing_or_non_executable_command_is_fatal(self, tmp_path, message):
        """The litclock-dev#547 shape: the unit shipped, the thing it runs did not. On the
        device this is status=203/EXEC in a journal nobody reads."""
        r = _run_syntax_check(tmp_path, message, 1)
        assert r.returncode != 0, f"missing ExecStart target was treated as a warning: {r.stdout!r}"
        assert "missing or not executable" in r.stdout

    def test_classifies_on_output_even_when_the_exit_code_is_zero(self, tmp_path):
        """THE bug this loop had. `systemd-analyze verify` exits ZERO for
        config-parse warnings — verified on systemd 255, a unit containing
        `WnatedBy=multi-user.target` (the literal litclock-dev#547 typo) produces
        `Unknown key name 'WnatedBy' in section 'Install', ignoring.` with rc=0.

        The old `if output=$(...); then :; else <classify>; fi` therefore never
        reached the classifier for it: nothing echoed, neither pattern
        consulted, the counter not incremented, and the success line printed.
        The exit code is not a classifier."""
        r = _run_syntax_check(tmp_path, "a.service:7: Unknown key name 'WnatedBy' in section 'Install', ignoring.", 0)
        assert r.returncode != 0, (
            f"rc=0 output was not classified — the gate is unreachable for the litclock-dev#547 typo: {r.stdout!r}"
        )
        assert "has real errors" in r.stdout

    def test_unknown_key_name_is_fatal(self, tmp_path):
        """`Unknown key name` is the wording systemd has used since v246;
        bookworm ships 252. `Unknown lvalue` alone is pre-246 and matches
        nothing on any Pi we ship."""
        # Read the ASSIGNMENT, not the file: "Unknown key name" also appears in
        # the rationale comment above it, so a whole-file substring search passed
        # even with the pattern deleted from fatal_patterns.
        body = open(SMOKE_TEST_SH).read()
        m = re.search(r"^fatal_patterns='([^']*)'", body, re.MULTILINE)
        assert m, "fatal_patterns assignment not found"
        assert "Unknown key name" in m.group(1), "fatal_patterns is missing the message systemd emits since v246"

    def test_a_bare_errno_is_not_treated_as_a_missing_command(self, tmp_path):
        """exec_fatal_patterns is anchored to `Command .* is not executable`, not
        the bare errno. Unanchored, ANY chroot-level failure carrying
        `No such file or directory` (dbus, /proc, machine-id) hard-fails every
        unit 38 minutes into a 40-minute build."""
        r = _run_syntax_check(tmp_path, "Failed to connect to bus: No such file or directory", 0)
        assert r.returncode == 0, f"a bare errno was treated as fatal: {r.stdout!r}"
        assert "missing or not executable" not in r.stdout

    def test_verifier_failing_with_no_output_is_not_a_pass(self, tmp_path):
        """rc!=0 with empty output means the VERIFIER died — under
        qemu-user-static a signal death gives rc=132/139 and prints nothing.
        Discarding rc laundered that into `OK: all unit files pass`."""
        r = _run_syntax_check(tmp_path, "", 1)
        assert r.returncode != 0, f"verifier failure reported as success: {r.stdout!r}"
        assert "OK: all unit files pass" not in r.stdout

    def test_a_warning_with_rc_zero_is_still_counted(self, tmp_path):
        """Non-fatal output on a zero exit must still suppress the unqualified
        success line."""
        r = _run_syntax_check(tmp_path, "a.service: Unit is bound to inactive device", 0)
        assert r.returncode == 0, r.stdout
        assert "non-fatal warnings" in r.stdout
        assert "OK: all unit files pass" not in r.stdout

    def test_genuine_warning_is_counted_not_hidden(self, tmp_path):
        """A non-fatal warning must not be laundered into an unqualified
        "all unit files pass" — the success line has to reflect that a unit
        printed output."""
        r = _run_syntax_check(tmp_path, "a.service: Unit is bound to inactive device", 1)
        assert r.returncode == 0, r.stdout
        assert "non-fatal warnings" in r.stdout
        assert "OK: all unit files pass" not in r.stdout


TMPFILES_BODY = "d /run/litclock 0755 pi pi -\n"


def _fake_tmpfiles_tree(tmp_path, confs, installed=None):
    """Miniature systemd/tmpfiles.d/ + /etc/tmpfiles.d/ pair."""
    src = tmp_path / "tmpfiles.d"
    etc = tmp_path / "etc-tmpfiles.d"
    src.mkdir(exist_ok=True)
    etc.mkdir(exist_ok=True)
    for name, body in confs.items():
        (src / name).write_text(body)
    for name in confs if installed is None else installed:
        (etc / name).write_text(confs[name])
    return str(src), str(etc)


def _run_tmpfiles_install_check(src_dir, etc_dir):
    script = (
        "set -e\n"
        f'SRC_TMPFILES_DIR="{src_dir}"\n'
        f'ETC_TMPFILES_DIR="{etc_dir}"\n'
        f"{_extract_smoke_block('tmpfiles-install-check')}\n"
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=15)


class TestTmpfilesSmokeCheckFailsLoudly:
    """The tmpfiles drop-in reaching the image is the difference between a
    working clock and one that paints quotes while every piece of runtime state
    silently no-ops. This loop is the only thing that catches it, so it has to
    exit non-zero rather than print."""

    def test_installed_and_matching_passes(self, tmp_path):
        src, etc = _fake_tmpfiles_tree(tmp_path, {"litclock.conf": TMPFILES_BODY})
        assert _run_tmpfiles_install_check(src, etc).returncode == 0

    def test_dropin_never_installed_fails(self, tmp_path):
        """litclock-dev#547's shape, in tmpfiles.d/: present in the repo, absent from
        the image, and nothing on the device ever complains."""
        src, etc = _fake_tmpfiles_tree(tmp_path, {"litclock.conf": TMPFILES_BODY}, installed=[])
        r = _run_tmpfiles_install_check(src, etc)
        assert r.returncode != 0
        assert "never installed" in r.stdout

    def test_installed_dropin_differing_from_source_fails(self, tmp_path):
        """Stale drop-in from a reused CLEAN=0 work dir satisfies -e but
        differs — an image shipping ownership rules nobody reviewed."""
        src, etc = _fake_tmpfiles_tree(tmp_path, {"litclock.conf": TMPFILES_BODY})
        (tmp_path / "etc-tmpfiles.d" / "litclock.conf").write_text("d /run/litclock 0755 root root -\n")
        assert _run_tmpfiles_install_check(src, etc).returncode != 0

    def test_empty_source_tree_fails(self, tmp_path):
        src, etc = _fake_tmpfiles_tree(tmp_path, {})
        r = _run_tmpfiles_install_check(src, etc)
        assert r.returncode != 0
        assert "repo staging is broken" in r.stdout

    def test_second_dropin_is_covered_without_editing_the_check(self, tmp_path):
        """The point of deriving rather than naming: adding a drop-in must be
        covered automatically, or this is the hardcoded list again."""
        src, etc = _fake_tmpfiles_tree(
            tmp_path,
            {"litclock.conf": TMPFILES_BODY, "extra.conf": "d /run/other 0755 pi pi -\n"},
            installed=["litclock.conf"],
        )
        r = _run_tmpfiles_install_check(src, etc)
        assert r.returncode != 0
        assert "extra.conf" in r.stdout


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
    it impossible to debug failed first-boots on real hardware (litclock-dev#172).
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


# ── venv posture on the path that actually ships ─────────────────────


class TestPiGenVenvPosture:
    """The litclock-dev#214/litclock-dev#321/litclock-dev#323 venv invariants, asserted against the IMAGE build.

    These were previously covered for scripts/install.sh (tests/test_install_sh.py,
    deleted with it in litclock-dev#547) and for scripts/update.sh (tests/test_update_sh.py).
    Nothing ever read pi-gen/stage3/01-setup-app/00-run.sh — which is where a
    flashed device actually gets its venv.

    Before litclock-dev#547 that was 2 of 3 install paths guarded. Retiring install.sh
    would have left 1 of 2, with the unguarded one being the ONLY path that
    ships. Review flagged it; retiring the mirror is the moment to point the
    assertions at the survivor rather than lose them.
    """

    @staticmethod
    def _setup_app():
        with open(os.path.join(STAGE_DIR, "01-setup-app", "00-run.sh")) as f:
            return f.read()

    def test_venv_uses_system_site_packages(self):
        """litclock-dev#214: the apt-provisioned GPIO/SPI wheels are only visible to the
        venv with --system-site-packages. Without it the driver chain cannot
        import lgpio and the panel never paints."""
        body = self._setup_app()
        assert "python3 -m venv --system-site-packages" in body, (
            "pi-gen must create the venv with --system-site-packages (litclock-dev#214)"
        )

    def test_pip_install_filters_apt_provisioned_names(self):
        """litclock-dev#214: requirements-apt.txt is the single source of truth for names
        pip must NOT install. The filter builds EXCLUDE_RE from that file, so
        a hand-edited list here would silently drift."""
        # Comments stripped first: mutation showed `"requirements-apt.txt" in
        # body` was satisfied by the explanatory comment above the code, so the
        # executable line could stop reading the file and this stayed green.
        body = _strip_comments(self._setup_app())
        assert re.search(r"EXCLUDE_RE=.*requirements-apt\.txt", body), (
            "pi-gen must build EXCLUDE_RE from requirements-apt.txt itself (litclock-dev#214) — "
            "a hand-maintained list here is exactly the drift that file exists to prevent"
        )
        assert re.search(r"grep -vE .*EXCLUDE_RE.* requirements\.txt", body), (
            "pi-gen must filter requirements.txt through EXCLUDE_RE before pip install"
        )

    def test_pip_install_is_not_eager(self):
        """litclock-dev#322: eager upgrade-strategy silently bumps transitives fleet-wide;
        the smoke test never imports Flask, so a break would ship."""
        body = self._setup_app()
        assert "--upgrade-strategy eager" not in body, (
            "pi-gen must NOT use eager upgrade-strategy (litclock-dev#322) — transitive breaks ship unnoticed"
        )

    def test_pip_installs_filtered_requirements_with_upgrade(self):
        """litclock-dev#321 / litclock-dev#605 item 19: the deleted installer test pinned
        `pip install --upgrade -r`; nothing re-pinned it against pi-gen. pip
        without --upgrade may silently SKIP an already-satisfied pin — this
        exact class has shipped before in this repo — and the
        install must target the FILTERED file, not raw requirements.txt,
        or the apt-provisioned GPIO stack gets shadowed by wheels."""
        body = _strip_comments(self._setup_app())
        assert re.search(r"venv/bin/pip install --upgrade -r \S*requirements-pigen", body), (
            "pi-gen must `venv/bin/pip install --upgrade -r` the filtered requirements "
            "file (litclock-dev#321/litclock-dev#605) — venv-pip specifically, a bare `pip` is the documented "
            "wrong-interpreter pitfall class"
        )
