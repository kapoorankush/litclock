"""litclock-dev#532 Stage 3 — the language registry, the catalog loader's
never-raise contract, and the CI completeness gate.

The gate's job (per the approved design + the 2026-08-12 scope audit):
- `languages.json` is the single source of truth; its shape is validated.
- The English catalog is the CANONICAL key set; every other language is
  diffed against it (as dicts, no thresholds — the parity-file lesson).
- Coverage is computed from the REAL corpus, SFW-only (audit item 8: the
  default device filters NSFW rows, so a language must clear the floor on
  the rows a default clock actually shows).
- Plural keys come in complete category sets per the registry's
  plural_forms — a missing `.other` would serve a raw key at runtime.
- aria.* is a reserved key CLASS (audit item 12): when aria keys exist
  they participate in the same canonical diff; the class is pinned here
  so its first use can't be silently namespace-squatted.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import strings_catalog  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_catalog(monkeypatch, tmp_path):
    monkeypatch.delenv("LITCLOCK_LANGUAGE", raising=False)
    # Isolate from the dev box's ambient env.sh channels: point the file
    # channel at a nonexistent path so only what a test sets applies.
    monkeypatch.setenv("LITCLOCK_ENV_FILE", str(tmp_path / "no-env.sh"))
    monkeypatch.delenv("LITCLOCK_DIR", raising=False)
    strings_catalog.reset_cache()
    yield
    strings_catalog.reset_cache()


def _registry() -> dict:
    return json.loads((REPO_ROOT / "languages.json").read_text(encoding="utf-8"))


def _languages() -> dict:
    return _registry()["languages"]


def _catalog_keys(entry: dict) -> set[str]:
    data = json.loads((REPO_ROOT / entry["strings"]).read_text(encoding="utf-8"))
    return {k for k in data if not k.startswith("_")}


class TestRegistryShape:
    REQUIRED = ("code", "native_name", "status", "curator", "corpus", "strings", "nsfw_tagged", "plural_forms")

    def test_registry_parses_and_has_english_active(self):
        reg = _registry()
        assert reg["fleet_default"] == "en"
        en = reg["languages"]["en"]
        assert en["status"] == "active"

    def test_every_entry_carries_required_fields(self):
        for code, entry in _languages().items():
            for field in self.REQUIRED:
                assert field in entry, f"{code} missing registry field {field!r}"
            assert entry["code"] == code
            assert entry["status"] in ("active", "incubating"), (
                f"{code}: status {entry['status']!r} — 'beta' and friends are "
                "deliberately not a thing (incomplete languages are unselectable)"
            )

    def test_strings_paths_resolve_and_parse(self):
        for code, entry in _languages().items():
            path = REPO_ROOT / entry["strings"]
            assert path.is_file(), f"{code}: strings file missing at {entry['strings']}"
            json.loads(path.read_text(encoding="utf-8"))


class TestCanonicalKeySet:
    def test_every_language_matches_the_english_keys_exactly(self):
        en_keys = _catalog_keys(_languages()["en"])
        assert en_keys, "the English catalog is empty — nothing is canonical"
        for code, entry in _languages().items():
            if code == "en":
                continue
            keys = _catalog_keys(entry)
            missing = en_keys - keys
            extra = keys - en_keys
            assert not missing and not extra, (
                f"{code} diverges from the canonical English key set — "
                f"missing={sorted(missing)[:5]} extra={sorted(extra)[:5]}"
            )

    def test_plural_categories_are_complete_per_language(self):
        for code, entry in _languages().items():
            forms = entry["plural_forms"]
            keys = _catalog_keys(entry)
            bases = {k.rsplit(".", 1)[0] for k in keys if k.rsplit(".", 1)[-1] in forms}
            for base in bases:
                for form in forms:
                    assert f"{base}.{form}" in keys, (
                        f"{code}: plural base {base!r} missing category {form!r} — "
                        "runtime would serve a raw key"
                    )

    def test_values_are_nonempty_strings_with_no_positional_slots(self):
        # Named slots only: a translator can reorder {n}; nobody can reorder {}.
        for code, entry in _languages().items():
            data = json.loads((REPO_ROOT / entry["strings"]).read_text(encoding="utf-8"))
            for key, value in data.items():
                if key.startswith("_"):
                    continue
                assert isinstance(value, str) and value.strip(), f"{code}:{key} empty"
                assert "{}" not in value, f"{code}:{key} uses a positional slot"
                import re as _re  # noqa: PLC0415

                assert not _re.search(r"\{\d+\}", value), f"{code}:{key} uses a numbered slot"

    def test_aria_key_class_is_reserved(self):
        # Audit item 12: aria.* is the screen-reader key class. Pin the
        # namespace so the first aria key lands inside the canonical diff
        # (it automatically does — this documents the reservation and fails
        # if someone squats the prefix with a non-string).
        for code, entry in _languages().items():
            data = json.loads((REPO_ROOT / entry["strings"]).read_text(encoding="utf-8"))
            for key, value in data.items():
                if key.startswith("aria."):
                    assert isinstance(value, str), f"{code}:{key} aria key must be a string"


class TestCoverageGate:
    def test_active_languages_clear_the_sfw_coverage_floor(self):
        # PRODUCTION semantics, not a mirror (/review litclock-dev#738 F5: the first
        # draft's yes/true/1 predicate and row-shape threshold diverged
        # from quote_corpus in three ways) — the gate builds the same index
        # the clock reads, so it measures the corpus the clock shows.
        import quote_corpus  # noqa: PLC0415

        for code, entry in _languages().items():
            if entry["status"] != "active":
                continue
            floor = entry.get("min_coverage_pct", 80)
            corpus = REPO_ROOT / entry["corpus"]["path"]
            assert corpus.is_file(), f"{code}: corpus missing at {entry['corpus']['path']}"
            quote_corpus.reset_cache()
            index = quote_corpus._index(corpus)
            sfw_minutes = {
                hhmm
                for hhmm, rows_in_bucket in index.items()
                if any(not r["is_nsfw"] for r in rows_in_bucket)
            }
            rows = sum(len(v) for v in index.values())
            coverage = len(sfw_minutes) / 1440 * 100
            assert coverage >= floor, (
                f"{code}: SFW coverage {coverage:.1f}% below the {floor}% activation floor "
                "(audit item 8 — NSFW rows must not count toward activation)"
            )
            assert rows == entry["corpus"]["rows"], (
                f"{code}: registry says {entry['corpus']['rows']} corpus rows, production "
                f"ingests {rows} — the registry is the single source of truth; keep it true"
            )
            recorded = entry["corpus"].get("sfw_coverage_pct")
            assert recorded is not None and abs(coverage - recorded) < 0.1, (
                f"{code}: registry records sfw_coverage_pct={recorded}, measured {coverage:.2f} — "
                "decorative numbers rot; keep the recorded value true"
            )


class TestLoaderContract:
    def test_known_key_resolves(self):
        assert strings_catalog.get("status.relative.just_now") == "just now"

    def test_plural_selects_whole_sentences(self):
        assert strings_catalog.plural("status.relative.minutes", 1) == "1 minute ago"
        assert strings_catalog.plural("status.relative.minutes", 5) == "5 minutes ago"

    def test_missing_key_serves_the_key_never_raises(self):
        assert strings_catalog.get("no.such.key") == "no.such.key"

    def test_unknown_language_degrades_to_english(self, monkeypatch):
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "zz")
        strings_catalog.reset_cache()
        assert strings_catalog.plural("status.relative.hours", 2) == "2 hours ago"

    def test_corrupt_catalog_never_raises(self, tmp_path, monkeypatch):
        # Point the loader at a broken tree: registry unreadable → empty →
        # every lookup degrades to the key. The screen stays up (audit item 7).
        monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", tmp_path / "nope.json")
        strings_catalog.reset_cache()
        assert strings_catalog.get("status.relative.just_now") == "status.relative.just_now"

    def test_slot_fill_is_inert_to_braces_in_values(self):
        # The dumb-replace contract: a slot value containing braces must not
        # recurse or raise (the reason format() is not used).
        out = strings_catalog.get("status.relative.minutes.other", n="{n}{weird}")
        assert out == "{n}{weird} minutes ago"

    def test_stray_braces_in_template_never_raise(self):
        # A translated template with a literal unmatched brace must pass
        # through untouched rather than raising (str.format would).
        assert strings_catalog._fill("100% {n} done {", {"n": 3}) == "100% 3 done {"


class TestShellSurface:
    """The bash string-lookup path (audit's missed FIFTH hosting context):
    scripts paint panel text via eink_display.py subprocess calls, so
    `catalog-get` is how shell reaches the catalog without parsing JSON."""

    def _run(self, *argv, env=None):
        import os
        import subprocess

        merged = dict(os.environ)
        merged.pop("LANGUAGE", None)
        if env:
            merged.update(env)
        return subprocess.run(
            [sys.executable, str(_SRC / "eink_display.py"), "catalog-get", *argv],
            capture_output=True,
            text=True,
            timeout=30,
            env=merged,
        )

    def test_resolves_key_with_slot(self):
        result = self._run("status.relative.minutes.other", "--slot", "n=7")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "7 minutes ago"

    def test_missing_key_prints_key_and_exits_zero(self):
        # Shell callers must never die on a catalog gap — the key itself is
        # the visible-but-safe degradation.
        result = self._run("no.such.key")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "no.such.key"


class TestEnvFileChannel:
    """/review litclock-dev#738 F3: litclock-control.service never sources env.sh — the
    loader must read the FILE, and re-read it on mtime change so a PWA
    language save reaches a running server without a restart."""

    def _env_file(self, tmp_path, monkeypatch, content: str):
        env = tmp_path / "env.sh"
        env.write_text(content)
        monkeypatch.setenv("LITCLOCK_ENV_FILE", str(env))
        strings_catalog.reset_cache()
        return env

    def test_language_read_from_env_file(self, tmp_path, monkeypatch):
        self._env_file(tmp_path, monkeypatch, "export LITCLOCK_LANGUAGE=en\n")
        assert strings_catalog.active_language() == "en"

    def test_process_env_overrides_file(self, tmp_path, monkeypatch):
        self._env_file(tmp_path, monkeypatch, "export LITCLOCK_LANGUAGE=zz\n")
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
        assert strings_catalog.active_language() == "en"

    def test_mtime_change_is_picked_up_without_reset(self, tmp_path, monkeypatch):
        import os as _os

        env = self._env_file(tmp_path, monkeypatch, "export LITCLOCK_LANGUAGE=zz\n")
        assert strings_catalog.active_language() == "en"  # zz inactive → en
        env.write_text("export LITCLOCK_LANGUAGE=en\n")
        _os.utime(env, (env.stat().st_atime, env.stat().st_mtime + 10))
        # No reset_cache(): the mtime check alone must pick it up.
        assert strings_catalog.active_language() == "en"

    def test_posix_language_variable_is_ignored(self, monkeypatch):
        # The glibc locale-priority variable an SSH session forwards must
        # not reach language selection at all (/review litclock-dev#738 F2).
        monkeypatch.setenv("LANGUAGE", "de_DE:de")
        strings_catalog.reset_cache()
        assert strings_catalog.active_language() == "en"


class TestDegradationHygiene:
    def test_warning_fires_once_per_missing_key(self, caplog):
        import logging as logging_mod

        with caplog.at_level(logging_mod.WARNING, logger="strings_catalog"):
            strings_catalog.get("gone.key")
            strings_catalog.get("gone.key")
            strings_catalog.get("gone.key")
        hits = [r for r in caplog.records if "gone.key" in str(r.args)]
        assert len(hits) == 1, (
            f"missing-key warning must fire once per key, not per lookup: {len(hits)} "
            "(/review litclock-dev#738 — status polls every 30s forever)"
        )

    def test_transient_load_failure_heals_after_ttl(self, tmp_path, monkeypatch):
        # /review litclock-dev#738 F4: a corrupt read during the OTA window must not
        # wedge the process on raw keys for its lifetime.
        reg = tmp_path / "languages.json"
        strings_dir = tmp_path / "languages" / "xx"
        strings_dir.mkdir(parents=True)
        strings_file = strings_dir / "strings.json"
        strings_file.write_text("{ corrupt")
        reg.write_text(json.dumps({
            "languages": {
                "xx": {"code": "xx", "status": "active", "strings": "languages/xx/strings.json"},
                "en": {"code": "en", "status": "active", "strings": "languages/xx/strings.json"},
            }
        }))
        monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", reg)
        monkeypatch.setattr(strings_catalog, "_REPO_ROOT", tmp_path)
        monkeypatch.setattr(strings_catalog, "NEGATIVE_CACHE_TTL_S", 0.0)
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "xx")
        strings_catalog.reset_cache()
        assert strings_catalog.get("k") == "k"  # corrupt → degrade
        strings_file.write_text(json.dumps({"k": "healed"}))
        assert strings_catalog.get("k") == "healed", (
            "a healed catalog must be picked up after the negative-cache TTL"
        )

    def test_incubating_language_is_unselectable(self, tmp_path, monkeypatch):
        # Mutant-killable status gate (/review litclock-dev#738 F6): a registry entry
        # that EXISTS with a real catalog but status=incubating must refuse.
        reg = tmp_path / "languages.json"
        d = tmp_path / "languages" / "xx"
        d.mkdir(parents=True)
        (d / "strings.json").write_text(json.dumps({"k": "xx-value"}))
        reg.write_text(json.dumps({
            "languages": {
                "xx": {"code": "xx", "status": "incubating", "strings": "languages/xx/strings.json"},
            }
        }))
        monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", reg)
        monkeypatch.setattr(strings_catalog, "_REPO_ROOT", tmp_path)
        monkeypatch.setenv("LITCLOCK_LANGUAGE", "xx")
        strings_catalog.reset_cache()
        assert strings_catalog.active_language() == "en", (
            "incubating languages are unselectable by contract — an incomplete "
            "bundle must never be served"
        )


class TestOtaSmokeCoversTheCatalog:
    def test_update_sh_smoke_asserts_a_resolved_string(self):
        # /review litclock-dev#738 (codex): a half-applied OTA missing languages/ passed
        # smoke and shipped raw keys. The smoke must ask the catalog for a
        # known key and require a NON-degraded answer.
        body = (REPO_ROOT / "scripts" / "update.sh").read_text()
        code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
        assert "catalog-get status.relative.just_now" in code, (
            "update.sh smoke lost the catalog probe (litclock-dev#532 /review litclock-dev#738)"
        )
        assert '= "just now"' in code, (
            "the smoke must compare against the RESOLVED value — exit codes can't "
            "discriminate (catalog-get always exits 0)"
        )
        # And the comparison must guard the SAME probe: both live between the
        # dry-run smoke and the smoke_rc dispatch (ordering, comments stripped).
        probe_idx = code.index("catalog-get status.relative.just_now")
        cmp_idx = code.index('= "just now"')
        assert 0 < cmp_idx - probe_idx < 400, "probe and comparison drifted apart"


def _multilang_registry(tmp_path, monkeypatch, extra_status="active"):
    reg = tmp_path / "languages.json"
    reg.write_text(json.dumps({
        "fleet_default": "en",
        "languages": {
            "en": {"code": "en", "native_name": "English", "status": "active",
                   "strings": "languages/en/strings.json"},
            "de": {"code": "de", "native_name": "Deutsch", "status": extra_status,
                   "strings": "languages/de/strings.json"},
        },
    }))
    monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", reg)
    strings_catalog.reset_cache()


class TestNegotiate:
    """litclock-dev#532 pickers: Accept-Language → best ACTIVE language."""

    def test_q_ordering_picks_the_active_match(self, tmp_path, monkeypatch):
        _multilang_registry(tmp_path, monkeypatch)
        assert strings_catalog.negotiate("de-DE,de;q=0.9,en;q=0.5") == "de"
        assert strings_catalog.negotiate("en;q=0.9,de;q=0.5") == "en"

    def test_primary_subtag_matching(self, tmp_path, monkeypatch):
        _multilang_registry(tmp_path, monkeypatch)
        assert strings_catalog.negotiate("de-AT") == "de"

    def test_inactive_language_never_negotiates(self, tmp_path, monkeypatch):
        _multilang_registry(tmp_path, monkeypatch, extra_status="incubating")
        assert strings_catalog.negotiate("de-DE,de;q=0.9") == "en"

    def test_unknown_empty_and_wildcard_default_to_english(self):
        assert strings_catalog.negotiate("zz-ZZ;q=1.0") == "en"
        assert strings_catalog.negotiate("") == "en"
        assert strings_catalog.negotiate(None) == "en"
        assert strings_catalog.negotiate("*") == "en"

    def test_malformed_q_is_treated_as_full_weight(self, tmp_path, monkeypatch):
        # Pinned (/review litclock-dev#742): an unparseable q falls back to the implicit
        # 1.0 — generous beats dropping the user's first-listed language.
        # Both unparseable shapes behave identically (follow-up F7).
        _multilang_registry(tmp_path, monkeypatch)
        assert strings_catalog.negotiate("de;q=banana,en") == "de"
        assert strings_catalog.negotiate("de;q=1.2.3,en") == "de"

    def test_q_zero_means_not_acceptable(self, tmp_path, monkeypatch):
        # RFC 9110: q=0 excludes the language (follow-up F7).
        _multilang_registry(tmp_path, monkeypatch)
        assert strings_catalog.negotiate("de;q=0,en;q=0.5") == "en"
        assert strings_catalog.negotiate("de;q=0") == "en"

    def test_active_codes_always_contains_english(self, tmp_path, monkeypatch):
        reg = tmp_path / "languages.json"
        reg.write_text(json.dumps({"languages": {}}))
        monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", reg)
        strings_catalog.reset_cache()
        assert "en" in strings_catalog.active_codes()


class TestLanguageValidator:
    def test_active_code_accepted_inactive_rejected(self, tmp_path, monkeypatch):
        _multilang_registry(tmp_path, monkeypatch, extra_status="incubating")
        import config  # noqa: PLC0415

        ok, _ = config.validate_setting("LITCLOCK_LANGUAGE", "en")
        assert ok
        ok, msg = config.validate_setting("LITCLOCK_LANGUAGE", "de")
        assert not ok and "active" in msg
        ok, _ = config.validate_setting("LITCLOCK_LANGUAGE", "")
        assert not ok


class TestGetTriplet:
    """litclock-dev#532 bulk extraction: the bash splash surfaces resolve
    (title, message, submessage) triplets in one process."""

    def test_resolves_all_three_parts(self):
        t, m, sub = strings_catalog.get_triplet("firstboot.splash.setup_failed")
        assert t == "Setup Failed"
        assert m == "Unplug power for 10 seconds"
        assert sub == "Then plug back in"

    def test_absent_part_key_means_intentionally_blank(self):
        """The canonical gate forbids empty VALUES, so blank parts are
        expressed by key absence — resolves to '', never the key."""
        t, m, sub = strings_catalog.get_triplet("firstboot.splash.setup_complete")
        assert t == "Setup Complete!"
        assert m == "Starting your clock..."
        assert sub == ""

    def test_slots_fill_in_every_part(self):
        t, m, sub = strings_catalog.get_triplet(
            "firstboot.splash.wifi_retry", attempt=2, max=5
        )
        assert m == "Retrying... (2/5)"
        assert t == "Setup WiFi Failed"

    def test_en_load_failure_serves_keys_not_a_blank_splash(self, monkeypatch, tmp_path):
        """Absence proves 'intentionally blank' only when en LOADED. A
        corrupt bundle must degrade to visible keys, not an empty panel."""
        monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", tmp_path / "nope.json")
        strings_catalog.reset_cache()
        t, m, sub = strings_catalog.get_triplet("firstboot.splash.preparing")
        assert t == "firstboot.splash.preparing.title"
        assert sub == "firstboot.splash.preparing.submessage"


class TestBashTripletParity:
    """The extraction must not change a single painted byte: every catalog
    value equals the literal it replaced in scripts/first-boot.sh and
    scripts/boot-splash.sh (pre-litclock-dev#532-bulk-extraction copy)."""

    FORMER_LITERALS = {
        "boot.splash.starting": ("LitClock", "Starting...", None),
        "firstboot.splash.preparing": ("Setup", "LitClock", "Preparing setup..."),
        "firstboot.splash.wifi_connected": ("WiFi Connected", "Network: {ssid}", None),
        "firstboot.splash.ntp_sync": ("Syncing Time", "Setting clock via NTP...", None),
        "firstboot.splash.detecting_location": (
            "Setting Up",
            "Detecting your location...",
            "No action needed",
        ),
        "firstboot.splash.wifi_retry": (
            "Setup WiFi Failed",
            "Retrying... ({attempt}/{max})",
            "Please wait",
        ),
        "firstboot.splash.setup_failed": (
            "Setup Failed",
            "Unplug power for 10 seconds",
            "Then plug back in",
        ),
        "firstboot.splash.setup_complete": ("Setup Complete!", "Starting your clock...", None),
        "firstboot.splash.setup_incomplete": (
            "Setup Incomplete",
            "Restart to try again",
            "Unplug, then plug back in",
        ),
        "firstboot.splash.recovering": (
            "Setup Problem",
            "Trying to recover...",
            "If this persists: unplug, then plug back in",
        ),
        # slice 2 — the shutdown/doom splashes. None = the part is a
        # runtime literal (curated quote / custom gift title) or absent.
        "shutdown.splash.welcome": (
            "Welcome to LitClock",
            "1. Plug in power\n2. Connect to LitClock-Setup WiFi when prompted\n"
            "3. Be patient — first boot takes a moment :)",
            None,
        ),
        "shutdown.splash.reboot": ("LitClock", "Restarting...", None),
        "shutdown.splash.poweroff": ("Powered Off", None, "LitClock"),
        "bootcheck.splash.gave_up": (
            "Recovery failed",
            "The clock couldn't repair itself after an update.",
            "Please re-flash the SD card — see the LitClock docs.",
        ),
        "reset.splash.failed": (
            "Reset did not finish",
            "This clock may still hold its previous owner's settings.",
            "Do NOT pass it on. Power it off and on, then try Factory reset again.",
        ),
    }

    def test_catalog_values_match_the_former_literals(self):
        catalog = _catalog_json()
        for prefix, (title, message, submessage) in self.FORMER_LITERALS.items():
            assert catalog.get(prefix + ".title") == title, prefix
            if message is None:
                assert prefix + ".message" not in catalog, (
                    f"{prefix}: runtime-literal part must be ABSENT from the catalog"
                )
            else:
                assert catalog.get(prefix + ".message") == message, prefix
            if submessage is None:
                assert prefix + ".submessage" not in catalog, (
                    f"{prefix}: blank part must be ABSENT (the gate forbids empty values)"
                )
            else:
                assert catalog.get(prefix + ".submessage") == submessage, prefix

    def test_every_painted_prefix_is_in_this_table(self):
        """A new display_message site must land here too — the table IS the
        byte-parity guarantee's coverage."""
        import re as _re

        for script in (
            "first-boot.sh",
            "boot-splash.sh",
            "shutdown-splash.sh",
            "litclock-bootcheck-giveup-splash.sh",
            "litclock-reset-failed-splash.sh",
        ):
            body = (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
            code = "\n".join(
                ln for ln in body.splitlines() if not ln.lstrip().startswith("#")
            )
            for m in _re.finditer(
                # Prefixes always contain dots — this keeps prose out of the
                # capture — and the lookahead refuses a trailing dot, so a
                # call site like "recovering." FAILS the sweep instead of
                # matching its clean prefix while runtime serves dotted keys
                # (testing /review: the sweep must observe the exact token).
                # Third alternation: shutdown-splash passes its prefix via
                # a PREFIX="..." variable — capture the assignment too.
                r"(?:display_message(?:_strict)?\s+|--catalog-prefix\s+|PREFIX=\")"
                r"([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)(?![.\w])",
                code,
            ):
                assert m.group(1) in self.FORMER_LITERALS, (
                    f"{script} paints {m.group(1)} — add it to the parity table"
                )
            if script == "shutdown-splash.sh":
                # Anti-vacuity: this file's prefixes ride the PREFIX= form —
                # the sweep must actually see all three arms.
                found = _re.findall(r"PREFIX=\"([a-z0-9_.]+)\"", code)
                assert sorted(found) == [
                    "shutdown.splash.poweroff",
                    "shutdown.splash.reboot",
                    "shutdown.splash.welcome",
                ], found
            # Companion sweep: EVERY display_message first argument must be a
            # dotted prefix (or a $variable) — a dotless typo matches no
            # regex above and would otherwise evade the table entirely.
            for m in _re.finditer(r"display_message(?:_strict)?\s+(\S+)", code):
                # Shell statement punctuation is not part of the token
                # (`if display_message_strict x; then`); a trailing DOT
                # stays — that's the malformed shape this sweep exists for.
                arg = m.group(1).rstrip(";")
                if arg.startswith("$") or arg.startswith('"$'):
                    continue
                assert _re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+", arg), (
                    f"{script}: display_message argument {arg!r} is not a well-formed catalog prefix"
                )


def _catalog_json() -> dict:
    import json

    return json.loads((REPO_ROOT / "languages" / "en" / "strings.json").read_text(encoding="utf-8"))


class TestGetTripletCallerMistakes:
    def test_trailing_dot_prefix_serves_keys_not_blank(self):
        t, m, sub = strings_catalog.get_triplet("firstboot.splash.recovering.")
        assert t == "firstboot.splash.recovering..title"  # visible, diagnosable

    def test_dotless_prefix_serves_keys_not_blank(self):
        t, _m, _s = strings_catalog.get_triplet("firstboot_splash_recovering")
        assert t == "firstboot_splash_recovering.title"

    def test_slot_named_prefix_or_key_never_raises(self):
        """Positional-only params: a --slot literally named 'prefix'/'key'
        must fill (or no-op), never TypeError through the never-raise
        contract."""
        t, _m, _s = strings_catalog.get_triplet("firstboot.splash.preparing", prefix="x", key="y")
        assert t == "Setup"
        assert strings_catalog.get("status.relative.just_now", key="z") == "just now"


class TestGetTripletUnknownPrefix:
    def test_unknown_prefix_serves_keys_not_a_blank_frame(self, tmp_path, monkeypatch):
        """Codex slice-1 /review: a LOADED en catalog missing an entire
        prefix is a typo/partial OTA, not a design choice — all three
        parts must degrade to visible keys, never ('', '', '') (a blank
        strict recovery splash would latch as success)."""
        import json as _json

        es_free = tmp_path / "strings.json"
        es_free.write_text(_json.dumps({"some.other.key": "x"}), encoding="utf-8")
        reg = tmp_path / "languages.json"
        reg.write_text(
            _json.dumps(
                {
                    "languages": {
                        "en": {"code": "en", "status": "active", "strings": str(es_free)}
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", reg)
        strings_catalog.reset_cache()
        t, m, sub = strings_catalog.get_triplet("firstboot.splash.setup_incomplete")
        assert t == "firstboot.splash.setup_incomplete.title"
        assert m == "firstboot.splash.setup_incomplete.message"
        assert sub == "firstboot.splash.setup_incomplete.submessage"


class TestDoomPathImportDegradation:
    def test_missing_strings_catalog_module_paints_key_names(self, monkeypatch):
        """The bootcheck/reset-failed splashes share a venv that may be
        half-deployed: a missing strings_catalog MODULE must degrade to
        painting the visible key names, never kill the one paint that
        outlives a frozen PWA (litclock-dev#532 slice 2)."""
        import argparse
        import sys as _sys

        _eink_dir = str(REPO_ROOT / "src")
        if _eink_dir not in _sys.path:
            _sys.path.insert(0, _eink_dir)
        import eink_display

        # sys.modules[name] = None makes `import name` raise ImportError.
        monkeypatch.setitem(_sys.modules, "strings_catalog", None)
        args = argparse.Namespace(
            catalog_prefix="reset.splash.failed",
            title=None,
            message=None,
            submessage=None,
            slot=[],
        )
        t, m, sub = eink_display._resolve_status_parts(args)
        assert t == "reset.splash.failed.title"
        assert m == "reset.splash.failed.message"
        assert sub == "reset.splash.failed.submessage"

    def test_stale_module_without_get_triplet_paints_key_names(self, monkeypatch):
        """A half-deployed venv can carry a strings_catalog that IMPORTS
        but is stale (no get_triplet) — AttributeError must degrade to key
        names identically, not kill the doom-path paint."""
        import argparse
        import sys as _sys
        import types

        _eink_dir = str(REPO_ROOT / "src")
        if _eink_dir not in _sys.path:
            _sys.path.insert(0, _eink_dir)
        import eink_display

        stale = types.ModuleType("strings_catalog")  # no attributes at all
        monkeypatch.setitem(_sys.modules, "strings_catalog", stale)
        args = argparse.Namespace(
            catalog_prefix="bootcheck.splash.gave_up",
            title=None,
            message=None,
            submessage=None,
            slot=[],
        )
        t, m, sub = eink_display._resolve_status_parts(args)
        assert t == "bootcheck.splash.gave_up.title"
        assert sub == "bootcheck.splash.gave_up.submessage"


# ── litclock-dev#532 Stage-4 content gates ─────────────────────────────────
#
# The Stage-3 gates prove key-set parity; these prove VALUE conformance, so
# the first translated bundle (es) is linted the moment it lands in the
# registry — not on activation day. Recorded needs on litclock-dev#532: placeholder-set
# parity, rich-token balance, and the content lint for the setup-page
# brace/quote family (the litclock-dev#756 review's future-translation traps: the
# mechanism is structurally safe, but a translation carrying stray braces,
# typo'd slots, raw markup, or tokens a resolver won't convert still renders
# literal garbage to the user).
#
# Every check runs over EVERY registry language including en — en's own
# conformance is what keeps the gates honest between now and the es bundle.
#
# Accepted false positive (this gate's /review, on record): the global
# '<'/'>' ban rejects legitimate plain-text uses like "Settings > WiFi".
# That is deliberate — catalog values are plain text across surfaces whose
# escape posture differs (Jinja autoescape, _page_text, json.dumps'd inline
# JS, RAW splices in the CNA bridge), and the gate cannot know per-key
# routing. A blocked translation is rephrased; a missed '</script>' breaks
# the captive portal. Strict wins.

# litclock-dev#532: the Stage-4 content-lint RULES live in the shippable
# src/catalog_lint.py so the CI gate (here), the standalone validator, and
# the translator-kit generator share ONE implementation. This test file
# keeps the anti-vacuity pins (source scan is live, vocab matches the
# production resolvers, wiring is exercised end-to-end) and the red-case
# proofs — the module is the code under test, these are the tests.
import catalog_lint  # noqa: E402

_RICH_TOKEN_NAMES = catalog_lint.RICH_TOKEN_NAMES
_RICH_VOCAB_FULL = catalog_lint.RICH_VOCAB_FULL
_RICH_VOCAB_SETUP = catalog_lint.RICH_VOCAB_SETUP
_TOKEN_RE = catalog_lint.TOKEN_RE
_slot_counts = catalog_lint.slot_counts
_plural_sibling_slots = catalog_lint.plural_sibling_slots


def _rich_capable_keys():
    return catalog_lint.rich_capable_keys(REPO_ROOT)


def _stage4_value_errors(value, **kw):
    return catalog_lint.value_errors(value, **kw)


def _all_stage4_errors(root: Path = REPO_ROOT) -> list[str]:
    # Bundle from `root`; rich-token vocabulary always from the real source
    # tree (the tmp-registry end-to-end test lints a synthetic es bundle
    # against the production resolvers).
    return catalog_lint.registry_errors(root, source_root=REPO_ROOT)


class TestStage4ContentGates:
    def test_every_catalog_value_passes_the_content_lint(self):
        findings = _all_stage4_errors()
        assert not findings, "Stage-4 content lint:\n  " + "\n  ".join(findings[:20])

    def test_rich_scan_is_live_and_covers_every_token_carrying_key(self):
        """Anti-vacuity for the source scan: if the scan regex rots, every
        en key that CARRIES a token would trip the vocabulary check above —
        and this pin fails first with a direct message."""
        capable = _rich_capable_keys()
        assert capable, "rich-capable source scan found nothing — the scan regex is dead"
        assert any(v == _RICH_VOCAB_SETUP for v in capable.values()), "setup_server._rich scan found nothing"
        assert any(v == _RICH_VOCAB_FULL for v in capable.values()), "template t_rich scan found nothing"
        en = _catalog_json()
        carrying = {k for k, v in en.items() if not k.startswith("_") and isinstance(v, str) and _TOKEN_RE.search(v)}
        assert carrying, "no en value carries a rich token — the balance gate has no live subject"
        orphans = carrying - set(capable)
        assert not orphans, f"en values carry rich tokens in keys no resolver routes: {sorted(orphans)}"
        missing_keys = set(capable) - {k for k in en if not k.startswith("_")}
        assert not missing_keys, f"rich call sites reference keys absent from the en catalog: {sorted(missing_keys)}"

    def test_vocab_matches_production_resolvers(self):
        """The vocab tuples are pinned against the PRODUCTION resolver
        sources, so a resolver-side vocab change — especially a SHRINK,
        which every other rule passes silently — goes red here."""
        cs_src = (REPO_ROOT / "src" / "control_server" / "__init__.py").read_text(encoding="utf-8")
        block = re.search(r"_RICH_TOKENS\s*=\s*\((.*?)\n\s*\)", cs_src, re.S)
        assert block, "control_server _RICH_TOKENS table not found — resolver moved; re-pin this test"
        full = set(re.findall(r'\(\s*"\{([a-z]+)\}"\s*,', block.group(1)))
        assert full == set(_RICH_VOCAB_FULL), f"t_rich converts {sorted(full)}, gate allows {sorted(_RICH_VOCAB_FULL)}"
        import inspect  # noqa: PLC0415

        import setup_server  # noqa: PLC0415

        setup = set(re.findall(r'\(\s*"\{([a-z]+)\}"\s*,', inspect.getsource(setup_server._rich)))
        assert setup == set(_RICH_VOCAB_SETUP), (
            f"setup_server._rich converts {sorted(setup)}, gate allows {sorted(_RICH_VOCAB_SETUP)}"
        )

    def test_slot_extraction_is_live(self):
        """Anti-vacuity for the slot extractor against the REAL catalog: a
        known slot-carrying key must yield its slot, or every parity
        assertion above is vacuously green."""
        en = _catalog_json()
        assert _slot_counts(en["setup.banner.error_lead"]) == {"error": 1}
        assert _slot_counts(en["system.sheet.factory_reset.body"]) == {"network": 2}
        inventory = {s for k, v in en.items() if not k.startswith("_") for s in _slot_counts(v)}
        assert len(inventory) >= 10, f"en slot inventory implausibly small: {sorted(inventory)}"

    def test_plural_category_suffix_is_reserved(self):
        """A non-plural key ending in .one/.other would silently gain the
        {n} allowance (and Stage 3 would demand its full category set), so
        the suffix is a reserved namespace: genuinely-plural bases extend
        this list; anything else renames."""
        en = _catalog_json()
        forms = set(_languages()["en"]["plural_forms"])
        bases = {k.rsplit(".", 1)[0] for k in en if not k.startswith("_") and k.rsplit(".", 1)[-1] in forms}
        assert bases == {
            "status.relative.minutes",
            "status.relative.hours",
            "status.relative.days",
        }, f"plural-suffixed bases changed: {sorted(bases)} — extend if genuinely plural, else rename the key"

    def test_registry_field_shapes(self):
        """Stage-3 checks field PRESENCE; a defective es registry entry is
        as dangerous as a defective strings.json (adversarial F7:
        plural_forms as a STRING iterates as characters)."""
        for code, entry in _languages().items():
            forms = entry["plural_forms"]
            assert isinstance(forms, list) and forms and all(isinstance(f, str) for f in forms), (
                f"{code}: plural_forms must be a non-empty list of strings, got {forms!r}"
            )
            floor = entry.get("min_coverage_pct", 80)
            assert isinstance(floor, (int, float)) and not isinstance(floor, bool) and 0 <= floor <= 100, (
                f"{code}: min_coverage_pct must be a number in [0, 100], got {floor!r}"
            )


class TestStage4EndToEnd:
    """The wiring red-case (this gate's /review, testing specialist): with
    only en in the real registry, the parity/vocab/relaxation arms of
    _all_stage4_errors structurally cannot fire — so a synthetic registry
    with a defective es bundle drives every arm through the REAL gate
    path: en_value plumbing, per-key vocab plumbing, relax plumbing."""

    def _write_root(self, tmp_path, es_overrides: dict) -> Path:
        en = {
            "setup.banner.error_lead": "Couldn't join. {error}",
            # A REAL setup_server._rich-routed key so the source-scanned
            # vocab (setup: {b} only) applies through the real plumbing.
            "setup.banner.connecting": "Connecting to {b}{network}{/b}…",
            "shell.drawer.follow_pill": "Follow ({n})",
            "status.relative.minutes.one": "{n} minute ago",
            "status.relative.minutes.other": "{n} minutes ago",
        }
        es = dict(en)
        es.update(es_overrides)
        (tmp_path / "languages").mkdir()
        for code, data in (("en", en), ("es", es)):
            d = tmp_path / "languages" / code
            d.mkdir()
            (d / "strings.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        (tmp_path / "languages.json").write_text(
            json.dumps(
                {
                    "languages": {
                        code: {
                            "code": code,
                            "status": "active",
                            "strings": f"languages/{code}/strings.json",
                            "plural_forms": ["one", "other"],
                        }
                        for code in ("en", "es")
                    }
                }
            ),
            encoding="utf-8",
        )
        return tmp_path

    def test_defective_es_bundle_is_caught_through_the_real_gate(self, tmp_path):
        root = self._write_root(
            tmp_path,
            {
                "setup.banner.error_lead": "No se pudo conectar.",  # drops {error}
                "setup.banner.connecting": "Conectando a {em}{network}{/em}…",  # {em} beyond setup vocab
                "shell.drawer.follow_pill": "Seguir {n} de {n}",  # duplicate — split-rendered key truncates
                "status.relative.minutes.other": "hace minutos",  # drops {n} where n is unbounded
            },
        )
        findings = _all_stage4_errors(root=root)
        assert any(f.startswith("es:setup.banner.error_lead:") and "drops slot" in f for f in findings), findings
        assert any(
            f.startswith("es:setup.banner.connecting:") and "outside this key's resolver vocabulary" in f
            for f in findings
        ), findings
        assert any(
            f.startswith("es:shell.drawer.follow_pill:") and "exceed the English occurrence count" in f
            for f in findings
        ), findings
        assert any(f.startswith("es:status.relative.minutes.other:") and "drops slot" in f for f in findings), findings
        assert not [f for f in findings if f.startswith("en:")], f"clean en flagged: {findings}"

    def test_conforming_es_bundle_passes_clean(self, tmp_path):
        root = self._write_root(
            tmp_path,
            {
                "setup.banner.error_lead": "No se pudo conectar. {error}",
                "setup.banner.connecting": "Conectando a {b}{network}{/b}…",
                "status.relative.minutes.one": "hace un minuto",  # .one may spell the number
                "status.relative.minutes.other": "hace {n} minutos",
            },
        )
        assert _all_stage4_errors(root=root) == []


class TestStage4GatesFireOnDefects:
    """Red-case proofs: each defect class the gate exists for must produce a
    finding from the SAME function the real gate runs. Delete a rule from
    _stage4_value_errors and its proof here goes red."""

    def _errs(self, value, *, en_value="Ready {name}.", vocab=(), **kw):
        return _stage4_value_errors(value, en_value=en_value, vocab=vocab, **kw)

    def test_typoed_slot_is_caught(self):
        errs = self._errs("Listo {nmae}.")
        assert any("exceed the English occurrence count" in e for e in errs)
        assert any("drops slot" in e for e in errs)

    def test_duplicate_slot_is_caught(self):
        # Four templates split-render a slot en uses once; a duplicate
        # silently truncates the tail after the second occurrence.
        errs = self._errs("Antes {name} medio {name} después.")
        assert any("exceed the English occurrence count" in e for e in errs)

    def test_stray_brace_is_caught(self):
        errs = self._errs("Listo {name}. {")
        assert any("stray brace" in e for e in errs)

    def test_dropped_slot_is_caught(self):
        assert any("drops slot" in e for e in self._errs("Listo."))

    def test_unbalanced_token_is_caught(self):
        errs = self._errs("{b}Listo {name}.", vocab=_RICH_VOCAB_FULL)
        assert any("unclosed" in e for e in errs)

    def test_crossed_nesting_is_caught(self):
        errs = self._errs("{b}Listo {em}ya{/b} {name}{/em}.", vocab=_RICH_VOCAB_FULL)
        assert any("closes nothing or crosses" in e for e in errs)

    def test_token_outside_vocabulary_is_caught(self):
        # {em} in a setup_server._rich key would render literally — its
        # resolver converts only {b}.
        errs = self._errs("{em}Listo{/em} {name}.", vocab=_RICH_VOCAB_SETUP)
        assert any("outside this key's resolver vocabulary" in e for e in errs)
        assert any("stray brace" in e for e in errs)

    def test_token_in_plain_key_is_caught(self):
        assert any("vocabulary" in e for e in self._errs("{b}Listo{/b} {name}."))

    def test_raw_markup_is_caught(self):
        errs = self._errs("Listo <strong>{name}</strong>.")
        assert any("markup" in e for e in errs)

    def test_script_close_in_js_literal_key_is_caught(self):
        # json.dumps escapes quotes but not '/', so '</script>' inside an
        # inline-JS literal terminates the script element — the markup rule
        # is what stands between a translation and that.
        assert any("markup" in e for e in self._errs("Listo </script>."))

    def test_html_entity_is_caught(self):
        assert any("HTML entity" in e for e in self._errs("Listo &lt;ya&gt; {name}."))
        assert any("HTML entity" in e for e in self._errs("Listo &#123;b&#125; {name}."))

    def test_confusable_brace_is_caught(self):
        # Fullwidth ｛b｝ matches no resolver and no ASCII-brace rule —
        # visually indistinguishable from a real token in most fonts.
        assert any("brace-confusable" in e for e in self._errs("｛b｝Listo｛/b｝ {name}."))

    def test_bidi_override_is_caught(self):
        assert any("bidi control" in e for e in self._errs("Contrase‮ña {name}."))

    def test_control_character_is_caught(self):
        assert any("control character" in e for e in self._errs("Listo\r {name}."))

    def test_newline_is_allowed(self):
        # shutdown.splash.welcome.message legitimately carries \n.
        assert self._errs("Listo,\n{name}.") == []

    def test_invisible_only_value_is_caught(self):
        # ZWSP survives value.strip() — a blank splash title through both
        # Stage-3 and a naive Stage-4.
        assert any("no visible content" in e for e in self._errs("​", en_value="Ready."))
        assert any("no visible content" in e for e in self._errs("⠀", en_value="Ready."))

    def test_plural_relaxation_is_per_category(self):
        en_one = "{n} minute ago"
        sibling = {"n"}
        # .one may spell the number ("hace un minuto") — {n} relaxed.
        assert (
            _stage4_value_errors(
                "hace un minuto", en_value=en_one, extra_allowed_slots=sibling, relax_slots={"n"}
            )
            == []
        )
        # A named non-{n} slot stays REQUIRED even under relaxation.
        errs = _stage4_value_errors(
            "quedan {n}", en_value="{n} of {total} left", extra_allowed_slots=sibling, relax_slots={"n"}
        )
        assert any("drops slot" in e and "total" in e for e in errs)
        # .other gets NO relaxation (unbounded n cannot be spelled) — the
        # end-to-end test drives this through the real wiring too.
        errs = _stage4_value_errors("hace minutos", en_value="{n} minutes ago", extra_allowed_slots=sibling)
        assert any("drops slot" in e for e in errs)

    def test_conforming_translation_passes_clean(self):
        assert self._errs("Listo, {name} — ¡ya está!") == []
        assert self._errs("{b}Listo{/b}, {name}.", vocab=_RICH_VOCAB_SETUP) == []
