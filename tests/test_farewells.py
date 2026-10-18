"""litclock-dev#532 T7 (eng R5 = B): the farewell lines on the shutdown,
reboot and gift screens are catalog keys, ``shutdown.farewell.<screen>.N``,
and a language supplies any number per screen from 1 up — its curator's own
lines from its own literature. ``.1`` is required; every higher number is
optional; a language may go past English's five. The painter picks one at
random, in the device language, from the same resolution as the rest of the
splash.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
from collections import Counter
from pathlib import Path

import pytest

import catalog_lint
import eink_display as e
import strings_catalog

REPO = Path(__file__).resolve().parent.parent
EN = json.loads((REPO / "languages" / "en" / "strings.json").read_text(encoding="utf-8"))
SCREENS = ("welcome", "reboot", "poweroff")

# The five per screen that lived in scripts/shutdown-splash.sh, with the
# straight quotes made typographic (“”) on 2026-10-16 to match the corpus.
ENGLISH = {
    "welcome": [
        '“It was the best of times…” — Dickens',
        '“Call me Ishmael.” — Melville',
        '“All happy families are alike.” — Tolstoy',
        '“It was a bright cold day in April.” — Orwell',
        '“Many years later, as he faced the firing squad…” — García Márquez',
    ],
    "reboot": [
        '“To sleep, perchance to dream.” — Shakespeare',
        '“I have promises to keep.” — Robert Frost',
        '“Not all those who wander are lost.” — Tolkien',
        '“Be back before the next chapter begins.”',
        '“And miles to go before I sleep.” — Robert Frost',
    ],
    "poweroff": [
        '“The rest is silence.” — Shakespeare',
        '“And so it goes.” — Kurt Vonnegut',
        '“After all, tomorrow is another day.” — Margaret Mitchell',
        '“So we beat on, boats against the current.” — Fitzgerald',
        '“A far, far better rest that I go to.” — Dickens',
    ],
}


@pytest.fixture(autouse=True)
def _english(monkeypatch, tmp_path):
    monkeypatch.setenv("LITCLOCK_ENV_FILE", str(tmp_path / "no-env.sh"))
    monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
    strings_catalog.reset_cache()
    yield
    strings_catalog.reset_cache()


def _without_farewells(bundle):
    return {k: v for k, v in bundle.items() if not catalog_lint.is_farewell_key(k)}


def _language(tmp_path, monkeypatch, per_screen: dict[str, int]):
    """A device in `xx`: English's copy, plus `n` of xx's own lines per screen."""
    bundle = _without_farewells(EN)
    for screen, n in per_screen.items():
        for i in range(1, n + 1):
            bundle[f"shutdown.farewell.{screen}.{i}"] = f"«{screen} {i}»"
    for code, data in (("en", EN), ("xx", bundle)):
        target = tmp_path / "languages" / code / "strings.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    registry = json.loads((REPO / "languages.json").read_text(encoding="utf-8"))
    registry["languages"]["en"]["strings"] = "languages/en/strings.json"
    registry["languages"]["xx"] = dict(registry["languages"]["en"], code="xx", strings="languages/xx/strings.json")
    (tmp_path / "languages.json").write_text(json.dumps(registry), encoding="utf-8")
    monkeypatch.setattr(strings_catalog, "REGISTRY_PATH", tmp_path / "languages.json")
    monkeypatch.setattr(strings_catalog, "_REPO_ROOT", tmp_path)
    monkeypatch.setenv("LITCLOCK_LANGUAGE", "xx")
    strings_catalog.reset_cache()
    return bundle


def _args(screen, part):
    return argparse.Namespace(
        catalog_prefix=f"shutdown.splash.{screen}",
        slot=[],
        title=None,
        message=None,
        submessage=None,
        farewell=(screen, part),
    )


_PART = {"welcome": "submessage", "reboot": "submessage", "poweroff": "message"}


def _painted(screen):
    title, message, submessage = e._resolve_status_parts(_args(screen, _PART[screen]))
    return message if _PART[screen] == "message" else submessage


# ── English: the same five per screen ───────────────────────────────────────


@pytest.mark.parametrize("screen", SCREENS)
def test_english_keeps_its_five_lines_per_screen(screen):
    assert strings_catalog.snapshot().numbered(f"shutdown.farewell.{screen}") == ENGLISH[screen]


@pytest.mark.parametrize("screen", SCREENS)
def test_the_painter_picks_every_line_over_time(screen):
    random.seed(532)
    seen = Counter(_painted(screen) for _ in range(300))
    assert set(seen) == set(ENGLISH[screen])


def test_the_rest_of_the_splash_is_the_catalog_triplet():
    title, message, submessage = e._resolve_status_parts(_args("poweroff", "message"))
    assert (title, submessage) == (
        EN["shutdown.splash.poweroff.title"],
        EN.get("shutdown.splash.poweroff.submessage", ""),
    )
    assert message in ENGLISH["poweroff"]


# ── Languages with their own counts ─────────────────────────────────────────


@pytest.mark.parametrize("n", [1, 3, 7])
def test_a_language_supplies_its_own_number_of_lines(tmp_path, monkeypatch, n):
    _language(tmp_path, monkeypatch, {screen: n for screen in SCREENS})
    snap = strings_catalog.snapshot()
    assert snap.usable and snap.language == "xx", "a 1/3/7-line language must be a usable bundle"
    for screen in SCREENS:
        lines = snap.numbered(f"shutdown.farewell.{screen}")
        assert lines == [f"«{screen} {i}»" for i in range(1, n + 1)]
        random.seed(n)
        assert {_painted(screen) for _ in range(200)} == set(lines)


def test_a_screen_without_its_first_line_makes_the_bundle_unusable(tmp_path, monkeypatch):
    """At least one per screen (R5): `.1` is required, so a bundle with no
    farewell for a screen is incomplete — the whole surface falls back."""
    _language(tmp_path, monkeypatch, {"welcome": 2, "reboot": 2, "poweroff": 0})
    assert not strings_catalog.snapshot().usable


def test_blank_lines_are_skipped_and_numbering_gaps_are_fine(tmp_path, monkeypatch):
    bundle = _language(tmp_path, monkeypatch, {screen: 1 for screen in SCREENS})
    bundle["shutdown.farewell.reboot.2"] = "  "
    bundle["shutdown.farewell.reboot.9"] = "«nine»"
    (tmp_path / "languages" / "xx" / "strings.json").write_text(json.dumps(bundle, ensure_ascii=False), "utf-8")
    strings_catalog.reset_cache()
    assert strings_catalog.snapshot().numbered("shutdown.farewell.reboot") == ["«reboot 1»", "«nine»"]


def test_the_key_class_is_exactly_the_three_screens():
    assert catalog_lint.is_farewell_key("shutdown.farewell.reboot.12")
    assert not catalog_lint.is_optional_key("shutdown.farewell.reboot.1")
    assert catalog_lint.is_optional_key("shutdown.farewell.reboot.2")
    for not_farewell in ("shutdown.farewell.boot.2", "shutdown.farewell.reboot.0", "shutdown.farewell.reboot.x",
                         "shutdown.splash.reboot.title", "shutdown.farewell.reboot.100"):  # fmt: skip
        assert not catalog_lint.is_farewell_key(not_farewell), not_farewell


def test_the_validator_accepts_a_language_with_its_own_count(tmp_path):
    spec = importlib.util.spec_from_file_location("validate_translation", REPO / "scripts" / "validate_translation.py")
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    bundle = _without_farewells(EN)
    for screen in SCREENS:
        bundle[f"shutdown.farewell.{screen}.1"] = f"«{screen} 1»"
        bundle[f"shutdown.farewell.{screen}.6"] = f"«{screen} 6»"
    bundle["shutdown.farewell.poweroff.2"] = ""  # the kit exports every key; blank = not supplied
    path = tmp_path / "es.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    findings = [f for f in validator.validate("es", str(path)) if "farewell" in f]
    assert findings == [], findings
    del bundle["shutdown.farewell.reboot.1"]
    path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    assert any("MISSING KEY  shutdown.farewell.reboot.1" in f for f in validator.validate("es", str(path)))


def test_a_bad_farewell_argument_is_refused():
    with pytest.raises(argparse.ArgumentTypeError):
        e._farewell_arg("boot:message")
    with pytest.raises(argparse.ArgumentTypeError):
        e._farewell_arg("reboot:title")
    assert e._farewell_arg("reboot:submessage") == ("reboot", "submessage")


def test_lines_come_in_number_order_not_file_order(tmp_path, monkeypatch):
    bundle = _language(tmp_path, monkeypatch, {screen: 1 for screen in SCREENS})
    reordered = {k: v for k, v in bundle.items() if not k.startswith("shutdown.farewell.reboot.")}
    for n in (10, 3, 1, 2):
        reordered[f"shutdown.farewell.reboot.{n}"] = f"«{n}»"
    (tmp_path / "languages" / "xx" / "strings.json").write_text(json.dumps(reordered, ensure_ascii=False), "utf-8")
    strings_catalog.reset_cache()
    assert strings_catalog.snapshot().numbered("shutdown.farewell.reboot") == ["«1»", "«2»", "«3»", "«10»"]


def test_a_wrapped_status_line_is_centred_line_by_line(monkeypatch):
    """The fit wraps an over-long single line (a long farewell) onto two
    lines; they centre on the panel like the line they replace. The gift
    steps, which arrive multi-line, keep their left-aligned list."""
    from PIL import ImageDraw

    calls = []
    real = ImageDraw.ImageDraw.text

    def spy(self, xy, text, *a, **k):
        calls.append((text, k.get("align")))
        return real(self, xy, text, *a, **k)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    long_line = "So we beat on, boats against the current, borne back ceaselessly into the past. — Fitzgerald"
    e.create_status_image("LitClock", long_line, None)
    wrapped = [(t, align) for t, align in calls if "\n" in t]
    assert wrapped and wrapped[0][1] == "center", calls
    calls.clear()
    e.create_status_image("Welcome", "1. One step\n2. Another step", None)
    assert [align for t, align in calls if "\n" in t] == ["left"]


def test_the_ci_gates_agree_with_the_validator_on_farewells(tmp_path):
    """A bundle the validator accepts must pass CI's content lint too: blank
    optional lines are not supplied (not 'invisible content'), and an extra
    line past English's count is linted like any other (litclock-dev#923 review)."""
    spec = importlib.util.spec_from_file_location("validate_translation", REPO / "scripts" / "validate_translation.py")
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    bundle = dict(EN)
    bundle["shutdown.farewell.reboot.2"] = "   "
    bundle["shutdown.farewell.reboot.6"] = "A line with an {unknown} slot"
    root = tmp_path / "repo"
    (root / "languages" / "xx").mkdir(parents=True)
    (root / "languages" / "en").mkdir(parents=True)
    (root / "languages" / "en" / "strings.json").write_text(json.dumps(EN, ensure_ascii=False), "utf-8")
    (root / "languages" / "xx" / "strings.json").write_text(json.dumps(bundle, ensure_ascii=False), "utf-8")
    registry = {
        "languages": {
            "en": {"strings": "languages/en/strings.json"},
            "xx": {"strings": "languages/xx/strings.json"},
        }
    }
    (root / "languages.json").write_text(json.dumps(registry), "utf-8")
    ci = [f for f in catalog_lint.registry_errors(root, source_root=REPO) if f.startswith("xx:")]
    assert ci == ["xx:shutdown.farewell.reboot.6: " + e for e in catalog_lint.value_errors(
        "A line with an {unknown} slot", en_value=None)], ci  # fmt: skip
    path = tmp_path / "xx.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    local = [f for f in validator.validate("xx", str(path)) if "farewell" in f]
    assert local and all("shutdown.farewell.reboot.6" in f for f in local), local
