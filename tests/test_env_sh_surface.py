"""The env.sh surface has no decorative edges (litclock-dev#791).

Two dead edges were found by the litclock-dev#783 review, and they fail in
opposite directions:

1. ``litclock-reresolve-location.service`` declared
   ``EnvironmentFile=/home/pi/litclock/env.sh``, which loaded **zero**
   variables — systemd's parser reads ``export KEY=VALUE`` as a variable
   literally named ``export KEY`` and drops the line. It read as functional.
2. ``WEATHER_LAST_IP_GEO_AT`` was read in three places and written in none, so
   the diagnostics "Last IP geo" row was permanently null AND the 7-day
   staleness anomaly that gates on it could never fire on any device.

These guards keep both closed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import config  # noqa: E402

SYSTEMD = REPO_ROOT / "systemd"
SAMPLE = REPO_ROOT / "env.sh.sample"
STATE_SH = REPO_ROOT / "scripts" / "lib" / "state.sh"
SEEDERS = (
    STATE_SH,
    REPO_ROOT / "scripts" / "first-boot.sh",
    REPO_ROOT / "scripts" / "reset-setup.sh",
    REPO_ROOT / "scripts" / "prepare-for-cloning.sh",
)
# Since litclock-dev#840 the defaults block has ONE source, `env_sh_defaults()`
# in lib/state.sh, plus first-boot's `lib/state.sh`-missing fallback heredoc —
# the one copy that cannot call the helper. Those two are where a key has to
# appear; the other scripts route through the helper and hold no keys at all.
SEED_SOURCES = (STATE_SH, REPO_ROOT / "scripts" / "first-boot.sh")


class TestNoEnvironmentFileOnEnvSh:
    def test_no_unit_declares_environmentfile_for_env_sh(self):
        offenders = []
        for unit in sorted(SYSTEMD.glob("*.service")) + sorted(SYSTEMD.glob("*.timer")):
            for lineno, line in enumerate(unit.read_text().splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if line.strip().startswith("EnvironmentFile=") and "env.sh" in line:
                    offenders.append(f"{unit.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
        assert not offenders, (
            "systemd cannot parse env.sh — every line is `export KEY=VALUE` and systemd reads the "
            "name as `export KEY`, so the directive loads NOTHING while reading as though it works "
            "(litclock-dev#791). Use Environment=LITCLOCK_ENV_FILE and let the process parse it.\n  "
            + "\n  ".join(offenders)
        )

    def test_env_sh_writers_emit_only_export_lines(self):
        """This is WHY the directive above is inert, pinned as a fact rather
        than as prose. If a writer ever emitted a bare ``KEY=VALUE`` line, the
        directive would start half-working, which is worse than not working —
        so the guard above and this one belong together."""
        assignment = re.compile(r"^(export\s+)?([A-Z][A-Z0-9_]*)=")
        # Scope to the keys env.sh.sample actually documents. The seeder
        # SCRIPTS are full bash programs full of their own locals
        # (INSTALL_DIR=..., PYTHON=...) which are not env.sh content; matching
        # every uppercase assignment would flag those and make the guard
        # useless. The sample is the definition of "an env.sh key".
        sample_text = SAMPLE.read_text()
        env_keys = {m.group(2) for m in (assignment.match(ln.strip()) for ln in sample_text.splitlines()) if m}
        env_keys |= {
            m.group(2)
            for m in (assignment.match(ln.strip().lstrip("# ")) for ln in sample_text.splitlines())
            if m
        }
        assert "WEATHER_UNITS" in env_keys and len(env_keys) > 10, "sample key discovery broke"

        bare = []
        for path in (SAMPLE, *SEEDERS):
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#") or not stripped:
                    continue
                m = assignment.match(stripped)
                if m and not m.group(1) and m.group(2) in env_keys:
                    bare.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {stripped}")
        assert not bare, "env.sh assignments must all be `export`-prefixed:\n  " + "\n  ".join(bare)


class TestLastIpGeoAtHasAWriter:
    KEY = "WEATHER_LAST_IP_GEO_AT"

    def test_key_is_writable_through_atomic_update(self):
        # The allowlist omission is the reason nothing COULD write it:
        # atomic_update rejects keys outside SETTINGS_ALLOWLIST.
        assert self.KEY in config.SETTINGS_ALLOWLIST

    def test_validator_accepts_the_shape_the_resolver_emits(self):
        from datetime import UTC, datetime

        emitted = datetime.now(UTC).isoformat(timespec="seconds")
        ok, err = config.validate_setting(self.KEY, emitted)
        assert ok, f"resolver emits {emitted!r} which the validator rejects: {err}"

    def test_validator_accepts_empty_and_rejects_shell_metacharacters(self):
        assert config.validate_setting(self.KEY, "")[0], "empty is the seeded state"
        for hostile in ("$(id)", "`id`", "2026-09-05T00:00:00; rm -rf /", "2026-09-05T00:00:00\nexport X=1", "nope"):
            ok, _err = config.validate_setting(self.KEY, hostile)
            assert not ok, f"validator accepted {hostile!r} into a bash-sourced file"

    def test_validator_rejects_unicode_digits_and_the_regex_alone_does_too(self):
        """Both guards must stand alone (found by /review).

        Python's `\\d` is Unicode-aware, so the ORIGINAL pattern matched
        Arabic-Indic digits and only `value.isascii()` rejected them. Nothing
        tested that, so dropping `isascii()` as redundant would have reopened it
        silently. A Unicode-digit value is not shell-injectable, but it
        round-trips into env.sh and then fails `datetime.fromisoformat()` inside
        the anomaly detector's bare `except ValueError: pass` — silently
        disabling the staleness anomaly, the exact defect litclock-dev#791 fixed.
        """
        unicode_digits = "\u0662\u0660\u0662\u0666-\u0660\u0669-\u0660\u0665T\u0661\u0664:\u0663\u0661:\u0660\u0660"
        assert not unicode_digits.isascii(), "fixture premise"
        ok, _err = config.validate_setting(self.KEY, unicode_digits)
        assert not ok, "validator must reject Unicode digits"
        assert not config._ISO_TIMESTAMP_RE.fullmatch(unicode_digits), (
            "the regex must reject Unicode digits on its own — use [0-9], not \\d, "
            "so it does not depend on the isascii() guard alone"
        )

    def test_resolver_stamps_it_on_a_successful_ip_geo(self, monkeypatch, tmp_path):
        """Executed end to end through the real writer: a successful IP-geo
        must land a parseable timestamp in env.sh."""
        from datetime import datetime

        import location_resolver

        env = tmp_path / "env.sh"
        env.write_text(
            "export WEATHER_LOCATION_MODE=auto\n"
            "export WEATHER_IP_COUNTRY=\n"
            "export WEATHER_LAST_IP_GEO_AT=\n"
        )

        # ip_geolocate is lazy-imported from geocoding inside the resolver,
        # so the patch has to land on the geocoding module, not this one.
        monkeypatch.setattr(
            "geocoding.ip_geolocate",
            lambda *a, **k: {
                "lat": 37.7749,
                "lon": -122.4194,
                "city": "San Francisco",
                "countryCode": "US",
                "timezone": "America/Los_Angeles",
            },
        )
        monkeypatch.setattr("geocoding.set_system_timezone", lambda tz: (True, None))

        assert location_resolver.resolve_location_from_ip(retries=False, env_file=str(env)) is True

        written = config.load_config(str(env)).get("WEATHER_LAST_IP_GEO_AT")
        assert written, "a successful IP-geo must stamp WEATHER_LAST_IP_GEO_AT"
        # Parseable by the same call the anomaly detector uses.
        assert datetime.fromisoformat(written).tzinfo is not None

    def test_specific_mode_save_does_NOT_stamp_it(self, tmp_path):
        """The stamp means "IP-geo last succeeded". The shared writer also
        serves the PWA's Specific save, which is not an IP-geo event; stamping
        there would make the staleness anomaly lie in the other direction."""
        import location_resolver

        env = tmp_path / "env.sh"
        env.write_text("export WEATHER_LOCATION_MODE=specific\nexport WEATHER_LAST_IP_GEO_AT=\n")
        location_resolver.update_env_location(
            51.5,
            -0.12,
            location_name="London",
            timezone=None,
            mode="specific",
            env_file=str(env),
        )
        assert not config.load_config(str(env)).get("WEATHER_LAST_IP_GEO_AT")

    def test_seeded_everywhere_the_sample_documents_it(self):
        """A key the sample documents must be seeded by every place a device
        can be born from. Since litclock-dev#840 that is the shared helper and
        first-boot's state.sh-missing fallback heredoc — reset-setup.sh and
        prepare-for-cloning.sh route through the helper, so a per-script grep
        there would now assert nothing."""
        assert f"export {self.KEY}=" in SAMPLE.read_text()
        for seeder in SEED_SOURCES:
            assert f"export {self.KEY}=" in seeder.read_text(), f"{seeder.name} must seed {self.KEY}"
