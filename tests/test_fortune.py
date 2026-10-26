"""litclock-dev#533 — fortune mode: the reader, the renderer, the painter's
fortune tier, the time-led masthead and the PWA Experimental section.

Every database here is SYNTHETIC: fortune text is Debian's to distribute and
the repo carries none (the sourcing decision on litclock-dev#533), so the fixtures are
written by this file in strfile(8) format, offensive rot13 tree included.

The render sweep at the bottom is the CI guard the owner asked for when fortune
mode was planned as a hidden feature (it is visible since 2026-10-24); it stays,
so a regression fails loudly here rather than on a panel. ``TestRealDatabases`` additionally sweeps the
installed Debian databases wherever they exist (the dev box, a Pi), and skips
where they do not (CI does not install them).
"""

from __future__ import annotations

import codecs
import os
import re
import struct
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config  # noqa: E402
import fortune  # noqa: E402
import literary_clock as lc  # noqa: E402
import quote_renderer  # noqa: E402
import strings_catalog  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

# Our own words, in the shapes Debian's databases use.
WISDOM = [
    "Short and sweet.",
    "A watched kettle never boils, but an unwatched one tends to boil over.\n\t\t-- Anonymous Cook",
    "Every clock is right\ntwice a day, and wrong\nthe rest of the time.",
    "The early bird gets the worm,\nbut the second mouse gets the cheese.\n"
    '\t\t-- Old Saying, "Collected\n\t\tSayings of Nobody"',
]
RIDDLES = [
    "Q:\tWhat has hands but cannot clap?\nA:\tA clock.",
    "Knock knock!\n\tWho's there?\nTick.\n\tTick who?\nTick tock, it's time to go.",
]
UNUSABLE = [
    "  /\\_/\\\n ( o.o )\n  > ^ <",  # ASCII art
    "Name    Age    Town\nAnn     34     Leeds",  # aligned columns
    "word " * 200,  # far too long
    "Box \u2500\u2500\u2500 drawing",  # outside the font set
]
OFFENSIVE = ["A rude remark, safely rot13'd on disk."]


def write_db(directory: Path, name: str, entries: list[str], *, rotated: bool = False) -> None:
    """A strfile(8) database: text joined by %-lines plus the big-endian
    .dat index (version 2, numstr, longlen, shortlen, flags, delim)."""
    directory.mkdir(parents=True, exist_ok=True)
    body = b""
    offsets = []
    for entry in entries:
        offsets.append(len(body))
        text = codecs.encode(entry, "rot13") if rotated else entry
        body += text.encode("utf-8") + b"\n%\n"
    offsets.append(len(body))
    (directory / name).write_bytes(body)
    lengths = [len(e.encode()) for e in entries]
    header = struct.pack(">IIIII", 2, len(entries), max(lengths), min(lengths), 0x4 if rotated else 0) + b"%\0\0\0"
    (directory / f"{name}.dat").write_bytes(header + struct.pack(f">{len(offsets)}I", *offsets))
    (directory / f"{name}.u8").symlink_to(name)


@pytest.fixture
def dbroot(tmp_path, monkeypatch):
    root = tmp_path / "fortunes"
    write_db(root, "wisdom", WISDOM)
    write_db(root, "riddles", RIDDLES)
    write_db(root, "junk", UNUSABLE)
    write_db(root / "off", "rude", OFFENSIVE, rotated=True)
    write_db(root / "off", "wisdom", ["An offensive half of a shared name."], rotated=True)
    (root / "orphan").write_text("no index beside me\n%\n")  # no .dat: skipped
    monkeypatch.setattr(fortune, "FORTUNE_DIR", root)
    return root


@pytest.fixture(autouse=True)
def _english(monkeypatch, tmp_path):
    monkeypatch.setenv("LITCLOCK_ENV_FILE", str(tmp_path / "no-env.sh"))
    monkeypatch.setenv("LITCLOCK_LANGUAGE", "en")
    strings_catalog.reset_cache()
    yield
    strings_catalog.reset_cache()


# ─── constants that must not drift ───────────────────────────────────────────


def test_shared_constants_match_their_sources():
    """fortune.py copies two renderer constants rather than importing them at
    module scope, and config.py copies fortune.py's settings shapes rather
    than importing Pillow. Pin every copy to its source."""
    assert fortune.FS_FLOOR == quote_renderer.FS_HARD_FLOOR
    assert fortune.LINE_HEIGHT_RATIO == quote_renderer.LINE_HEIGHT_RATIO
    assert config.CONTENT_MODES == fortune.CONTENT_MODES
    assert config.FORTUNE_DWELL_RANGE == (fortune.DWELL_MIN, fortune.DWELL_MAX)
    assert config._FORTUNE_CATEGORY_RE.pattern == fortune.CATEGORY_RE.pattern
    assert config.FORTUNE_CATEGORIES_MAX == fortune.CATEGORIES_MAX
    assert set(fortune.DWELL_CHOICES) <= set(range(fortune.DWELL_MIN, fortune.DWELL_MAX + 1))
    assert fortune.DWELL_DEFAULT in fortune.DWELL_CHOICES


def test_fortune_is_not_a_proof_input():
    """Editing quote_renderer.py revokes every device's runtime-render marker;
    fortune mode must live outside the digest's inputs."""
    src = (REPO_ROOT / "src" / "quote_renderer.py").read_text()
    assert "fortune" not in src.lower()


# ─── databases ───────────────────────────────────────────────────────────────


class TestDatabases:
    def test_lists_indexed_text_files_only(self, dbroot):
        names = {(db.name, db.offensive) for db in fortune.list_databases()}
        assert names == {("wisdom", False), ("riddles", False), ("junk", False), ("rude", True), ("wisdom", True)}

    def test_offensive_tree_is_opt_in(self, dbroot):
        assert all(not db.offensive for db in fortune.list_databases(include_offensive=False))

    def test_rot13_databases_are_decoded(self, dbroot):
        rude = next(db for db in fortune.list_databases() if db.name == "rude")
        assert rude.rotated
        assert fortune._entry_text(rude, 0) == OFFENSIVE[0]

    def test_entries_round_trip(self, dbroot):
        wisdom = next(db for db in fortune.list_databases() if db.name == "wisdom" and not db.offensive)
        assert [fortune._entry_text(wisdom, i) for i in range(wisdom.count)] == WISDOM

    def test_categories_flag_offensive_only_names(self, dbroot):
        assert fortune.categories() == [
            ("junk", False),
            ("riddles", False),
            ("wisdom", False),
            ("off-rude", True),
            ("off-wisdom", True),
        ]

    def test_missing_directory_is_empty_not_an_error(self, tmp_path):
        assert fortune.list_databases(tmp_path / "absent") == []

    @pytest.mark.parametrize("header", [b"", b"\0" * 23, struct.pack(">IIIII", 9, 1, 1, 1, 0) + b"%\0\0\0"])
    def test_a_bad_index_skips_the_database(self, tmp_path, header):
        write_db(tmp_path, "x", ["fine"])
        (tmp_path / "x.dat").write_bytes(header)
        assert fortune.list_databases(tmp_path) == []


# ─── normalisation ───────────────────────────────────────────────────────────


class TestNormalise:
    def test_attribution_is_split_off(self):
        f = fortune.normalise(WISDOM[1])
        assert f.paragraphs == ("A watched kettle never boils, but an unwatched one tends to boil over.",)
        assert f.attribution == "Anonymous Cook"

    def test_multiline_attribution_is_joined(self):
        assert fortune.normalise(WISDOM[3]).attribution == 'Old Saying, "Collected Sayings of Nobody"'

    def test_verse_keeps_its_lines(self):
        assert fortune.normalise(WISDOM[2]).paragraphs == (
            "Every clock is right",
            "twice a day, and wrong",
            "the rest of the time.",
        )

    def test_hard_wrapped_prose_is_rejoined(self):
        raw = (
            "This sentence was wrapped by a 1980s editor at about seventy columns,\n"
            "so the panel must join it back together again."
        )
        assert len(fortune.normalise(raw).paragraphs) == 1

    def test_question_and_answer_labels_break_lines(self):
        assert fortune.normalise(RIDDLES[0]).paragraphs == ("Q: What has hands but cannot clap?", "A: A clock.")

    def test_indented_replies_are_their_own_lines(self):
        assert len(fortune.normalise(RIDDLES[1]).paragraphs) == 5

    @pytest.mark.parametrize("raw", UNUSABLE + ["", "\n\n", "bad \ufffd byte", "bell\x07"])
    def test_unusable_entries_are_rejected(self, raw):
        assert fortune.normalise(raw) is None

    def test_denylist_key_is_stable(self):
        assert fortune.entry_hash("  x\n") == fortune.entry_hash("x")
        assert re.fullmatch(r"[0-9a-f]{16}", fortune.entry_hash("x"))


def test_shipped_denylist_parses_and_holds_no_fortune_text():
    """Keys only — the denylist must never become a way of committing the
    text the sourcing decision keeps out of the repo."""
    keys = fortune.load_denylist()
    assert all(re.fullmatch(r"[0-9a-f]{16}", k) for k in keys)
    for line in fortune.DENYLIST_PATH.read_text().splitlines():
        assert not line.strip() or line.startswith("#") or re.fullmatch(r"[0-9a-f]{16}(\s+#.*)?", line.strip())


# ─── fit + render ────────────────────────────────────────────────────────────


class TestFit:
    def test_short_fortunes_stop_at_the_poster_cap(self):
        layout = fortune.fit(fortune.normalise("Short and sweet."))
        assert layout.font_size == fortune.FS_MAX

    def test_long_fortunes_shrink_but_never_below_the_floor(self):
        raw = " ".join(["Somewhat long words keep on arriving"] * 9)
        layout = fortune.fit(fortune.normalise(raw))
        assert fortune.FS_FLOOR <= layout.font_size < fortune.FS_MAX

    def test_a_fortune_that_cannot_fit_returns_none(self):
        # One word wider than the canvas at every size: normalises, never fits.
        raw = "Pneumonoultramicroscopicsilicovolcanoconiosis" * 2
        assert fortune.normalise(raw) is not None
        assert fortune.fit(fortune.normalise(raw)) is None

    def test_render_stays_on_the_canvas_and_clear_of_the_qr_notch(self):
        for raw in WISDOM + RIDDLES:
            img = fortune.render(fortune.normalise(raw))
            assert img.size == (fortune.WIDTH, fortune.HEIGHT)
            assert lc._runtime_frame_ok(img) is None, raw
            ink = img.point(lambda v: 255 if v < 128 else 0).getbbox()
            assert ink[0] >= fortune.SIDE_MARGIN - 2 and ink[2] <= fortune.WIDTH - fortune.SIDE_MARGIN + 2

    def test_an_unwieldy_attribution_is_dropped_not_the_fortune(self):
        f = fortune.normalise("Fine body.\n\t\t-- " + "Very Long Name " * 30)
        assert fortune.fit(f).attribution == ()


# ─── selection ───────────────────────────────────────────────────────────────


class TestPick:
    T = datetime(2026, 10, 22, 10, 0)

    def test_same_fortune_for_the_whole_dwell_slot(self, dbroot):
        picks = {fortune.pick(self.T + timedelta(minutes=m), dwell=5)[0].raw for m in range(5)}
        assert len(picks) == 1

    def test_dwell_one_still_deterministic_per_minute(self, dbroot):
        assert fortune.pick(self.T, dwell=1)[0] == fortune.pick(self.T, dwell=1)[0]

    def test_slots_vary_over_a_day(self, dbroot):
        picks = {fortune.pick(self.T + timedelta(minutes=5 * k))[0].raw for k in range(288)}
        assert len(picks) > 3

    def test_unusable_entries_are_never_picked(self, dbroot):
        for k in range(288):
            f, _ = fortune.pick(self.T + timedelta(minutes=5 * k), selected=("junk", "wisdom"))
            assert f.database == "wisdom"

    def test_offensive_only_with_nsfw(self, dbroot):
        picks = [fortune.pick(self.T + timedelta(minutes=5 * k), selected=("off-rude", "wisdom")) for k in range(100)]
        assert not any(f.offensive for f, _ in picks)
        assert fortune.pick(self.T, selected=("off-rude",)) is None
        nsfw = [
            fortune.pick(self.T + timedelta(minutes=5 * k), selected=("off-rude",), allow_nsfw=True) for k in range(5)
        ]
        assert all(f.offensive for f, _ in nsfw)

    def test_denylist_excludes(self, dbroot):
        deny = frozenset(fortune.entry_hash(e) for e in WISDOM[1:])
        f, _ = fortune.pick(self.T, selected=("wisdom",), denylist=deny)
        assert f.raw == WISDOM[0]

    def test_empty_pool_is_none(self, dbroot):
        assert fortune.pick(self.T, selected=("nonexistent",)) is None

    def test_current_fortune_never_raises(self, dbroot, monkeypatch):
        monkeypatch.setattr(fortune, "pick", lambda *a, **k: 1 / 0)
        assert fortune.current_fortune(self.T, {}) is None


class TestSettingsParsing:
    @pytest.mark.parametrize(
        "raw,want",
        [(None, "quotes"), ("", "quotes"), ("fortune", "fortune"), (" Fortune ", "fortune"), ("mix", "quotes")],
    )
    def test_content_mode(self, raw, want):
        assert fortune.content_mode(raw) == want

    # Default 1: a new fortune every minute (owner decision 2026-10-24).
    @pytest.mark.parametrize("raw,want", [(None, 1), ("", 1), ("10", 10), ("0", 1), ("abc", 1), ("61", 1)])
    def test_dwell(self, raw, want):
        assert fortune.dwell_minutes(raw) == want

    def test_categories(self):
        assert fortune.parse_categories(" Wisdom,riddles,,bad name,../x ") == ("riddles", "wisdom")

    @pytest.mark.parametrize(
        "key,value,ok",
        [
            ("LITCLOCK_CONTENT_MODE", "fortune", True),
            ("LITCLOCK_CONTENT_MODE", "", False),
            ("LITCLOCK_CONTENT_MODE", "mix", False),
            ("LITCLOCK_FORTUNE_CATEGORIES", "", True),
            ("LITCLOCK_FORTUNE_CATEGORIES", "wisdom,riddles", True),
            ("LITCLOCK_FORTUNE_CATEGORIES", "wisdom;rm -rf", False),
            ("LITCLOCK_FORTUNE_CATEGORIES", "wisdom,", False),
            ("LITCLOCK_FORTUNE_DWELL_MINUTES", "5", True),
            ("LITCLOCK_FORTUNE_DWELL_MINUTES", "0", False),
            ("LITCLOCK_FORTUNE_DWELL_MINUTES", "٥", False),
        ],
    )
    def test_writer_validation(self, key, value, ok):
        assert config.validate_setting(key, value)[0] is ok


# ─── the painter ─────────────────────────────────────────────────────────────


@pytest.fixture
def painter(dbroot, monkeypatch, tmp_path):
    monkeypatch.setenv("WEATHER_ENABLED", "false")
    monkeypatch.setenv("LITCLOCK_RUNTIME_RENDER_DIR", str(tmp_path))
    monkeypatch.setattr(lc, "RUNTIME_RENDER_DIR", str(tmp_path))
    monkeypatch.setenv("LITCLOCK_CONTENT_MODE", "fortune")
    return monkeypatch


class TestPainter:
    T = datetime(2026, 9, 4, 10, 26)

    def test_fortune_frame(self, painter):
        image, meta, now, reason = lc.main(at=self.T)
        assert meta["render_mode"] == "fortune" and reason is None
        assert meta["quote"] and meta["title"] == ""
        assert image.size == lc.DISPLAY_SIZE

    def test_quotes_mode_is_untouched(self, painter):
        painter.setenv("LITCLOCK_CONTENT_MODE", "quotes")
        _, meta, _, _ = lc.main(at=self.T)
        assert meta is None or meta["render_mode"] != "fortune"

    def test_no_databases_falls_back_to_a_quote_with_the_time_masthead(self, painter, tmp_path):
        painter.setattr(fortune, "FORTUNE_DIR", tmp_path / "nothing-here")
        seen = {}
        real = lc._compose_masthead

        def spy(*a, **k):
            seen.update(k)
            return real(*a, **k)

        painter.setattr(lc, "_compose_masthead", spy)
        _, meta, _, _ = lc.main(at=self.T)
        assert meta is None or meta["render_mode"] != "fortune"
        assert seen["time_text"] == "10:26", "one layout per mode: the time leads even on a quote fallback"

    def test_a_rejected_frame_falls_back(self, painter):
        painter.setattr(lc, "_runtime_frame_ok", lambda frame: "forced")
        assert lc.get_current_fortune(self.T) is None

    def test_a_broken_module_paints_quotes(self, painter):
        painter.setattr(fortune, "content_mode", lambda raw: 1 / 0)
        assert lc._content_mode() == "quotes"

    def test_status_payload_carries_the_fortune(self, painter, tmp_path):
        status = tmp_path / "status.json"
        painter.setattr(lc, "STATUS_FILE", str(status))
        _, meta, now, reason = lc.main(at=self.T)
        lc._write_status_file(meta, now, reason)
        import json

        payload = json.loads(status.read_text())
        assert payload["render_mode"] == "fortune" and payload["quote"] == meta["quote"]


class TestDryRunPin:
    """The weekly self-test certifies the QUOTE renderer; a fortune-mode
    device must still earn it, while the plain smoke gate tests the tier the
    device actually paints."""

    def _run(self, dbroot, tmp_path, *flags):
        env = dict(
            os.environ,
            PYTHONPATH=f"{REPO_ROOT}{os.pathsep}{REPO_ROOT / 'src'}",
            LITCLOCK_UPDATE_FAILED_MARKER="/nonexistent/marker",
            LITCLOCK_CONTENT_MODE="fortune",
            LITCLOCK_FORTUNE_DIR=str(dbroot),
            LITCLOCK_RUNTIME_RENDER_DIR=str(tmp_path),
            WEATHER_ENABLED="false",
            LITCLOCK_ENV_FILE=str(tmp_path / "no-env.sh"),
        )
        return subprocess.run(
            [sys.executable, "-m", "src.literary_clock", "--dry-run", *flags],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_smoke_gate_renders_the_fortune_tier(self, dbroot, tmp_path):
        r = self._run(dbroot, tmp_path)
        assert r.returncode == 0, r.stderr
        assert "render_mode=fortune" in r.stdout

    def test_self_test_pins_quotes(self, dbroot, tmp_path):
        r = self._run(dbroot, tmp_path, "--sample-quote")
        assert "render_mode=fortune" not in r.stdout
        assert "render_mode=" in r.stdout, r.stdout + r.stderr


# ─── masthead lockup ─────────────────────────────────────────────────────────


def _probe():
    return ImageDraw.Draw(Image.new("1", lc.DISPLAY_SIZE, 255))


class TestLockup:
    def test_every_time_has_the_same_slot(self):
        mh = lc._masthead_metrics()
        feats, slot = lc._time_slot_metrics()
        draw = _probe()
        widths = {
            draw.textbbox((0, 0), f"{h:02d}:{m:02d}", font=mh["date_font"], features=feats)[2]
            for h in range(24)
            for m in range(60)
        }
        assert max(widths) <= slot
        if feats:  # raqm present (the Pi and CI): tabular figures, one width
            assert len(widths) == 1

    def test_full_sweep_stays_in_the_band(self):
        """Every English weekday x month x day, at the widest time: the lockup
        ink stays right of the weather rule and short of the QR carve — the
        fortune-mode twin of test_date_clearance_and_bound_full_sweep."""
        snap = strings_catalog.snapshot()
        mh = lc._masthead_metrics()
        draw = _probe()
        worst = 0
        for wk in lc._WEEKDAY_KEYS:
            for mo in lc._MONTH_KEYS:
                for dk in lc._DAY_KEYS:
                    text = snap.get("clock.masthead.date", weekday=snap.get(wk), month=snap.get(mo), day=snap.get(dk))
                    left, right = lc._draw_time_lockup(draw, mh, "00:00", text)
                    assert left >= lc.LOCKUP_BAND[0], text
                    assert right <= lc.MASTHEAD_DATE_RIGHT, text
                    worst = max(worst, right)
        assert worst < 695  # the issue's measured worst case is 668

    def test_lockup_does_not_slide_with_the_time(self):
        mh = lc._masthead_metrics()
        spans = {lc._draw_time_lockup(_probe(), mh, t, "Mon, May 04") for t in ("11:11", "00:00", "20:08")}
        assert len(spans) == 1

    def test_ink_matches_the_reported_span_and_clears_the_rule(self):
        img = Image.new("1", lc.DISPLAY_SIZE, 255)
        draw = ImageDraw.Draw(img)
        lc._compose_masthead(img, draw, "Mon, September 04", None, None, None, time_text="00:00")
        band = img.crop((0, 0, lc.DISPLAY_SIZE[0], lc.RULE_TOP)).point(lambda v: 255 - v)
        box = band.getbbox()
        assert box[0] >= lc.LOCKUP_BAND[0] and box[2] <= lc.MASTHEAD_DATE_RIGHT
        assert box[3] <= lc.RULE_TOP - lc.RULE_CLEARANCE

    def test_a_too_wide_date_shrinks_then_cuts(self):
        mh = lc._masthead_metrics()
        left, right = lc._draw_time_lockup(_probe(), mh, "00:00", "W" * 60)
        assert right <= lc.MASTHEAD_DATE_RIGHT

    def test_quote_mode_masthead_unchanged(self):
        """time_text=None must be byte-identical to the pre-litclock-dev#533 call."""
        a = Image.new("1", lc.DISPLAY_SIZE, 255)
        b = Image.new("1", lc.DISPLAY_SIZE, 255)
        lc._compose_masthead(a, ImageDraw.Draw(a), "Mon, September 04", "71°F", "52°F", None)
        lc._compose_masthead(b, ImageDraw.Draw(b), "Mon, September 04", "71°F", "52°F", None, time_text=None)
        assert a.tobytes() == b.tobytes()


# ─── the hidden PWA section ──────────────────────────────────────────────────


@pytest.fixture
def pwa(tmp_path, dbroot, monkeypatch):
    from control_server import create_app
    from control_server.csrf import CSRF_ACTION

    env = tmp_path / "env.sh"
    env.write_text("export ALLOW_NSFW_QUOTES=false\nexport LITCLOCK_FORTUNE_CATEGORIES=riddles\n")
    monkeypatch.setattr("control_server.routes.settings.subprocess.run", lambda *a, **k: None)
    monkeypatch.setattr("control_server.routes.settings._ad_hoc_tick", lambda: None)
    app = create_app({"ENV_FILE": str(env), "VERSION_OVERRIDE": "v0.test"})
    token, _ = app.extensions["csrf_tokens"].issue(CSRF_ACTION)
    return app.test_client(), token, env


def _post(client, token, query="", **form):
    return client.post(
        f"/settings{query}",
        data={"csrf_token": token, "section": "lab", **form},
        base_url="http://litclock.local",
        headers={"Origin": "http://litclock.local"},
    )


class TestLabSection:
    def test_shown_to_everyone(self, pwa):
        """Owner decision 2026-10-24: no ?lab=1, no cookie."""
        client, _, _ = pwa
        html = client.get("/settings").data.decode()
        assert 'id="settings-lab-h"' in html
        assert 'value="riddles"\n                 checked' in html or re.search(r'value="riddles"\s+checked', html)
        assert "off-rude (mature)" in html
        assert "Minutes between fortunes" in html and "<span>15</span>" in html

    def test_fortune_fields_sit_in_the_fortune_only_block(self, pwa):
        """Dwell and databases show only while Fortunes is checked: they live
        in [data-fortune-only], which settings.css hides on the quotes radio."""
        client, _, _ = pwa
        html = client.get("/settings").data.decode()
        lab = html[html.index('id="settings-lab-h"') :]
        lab = lab[: lab.index("</section>")]
        block = lab[lab.index("data-fortune-only") :]
        mode_pill = lab[: lab.index("data-fortune-only")]
        assert 'name="LITCLOCK_CONTENT_MODE"' in mode_pill and 'name="LITCLOCK_CONTENT_MODE"' not in block
        assert 'name="LITCLOCK_FORTUNE_DWELL_MINUTES"' in block and 'name="fortune_category"' in block

    def test_the_hide_rule_keys_on_the_quotes_radio(self):
        """A HIDE rule on quotes, so a browser without :has() shows the fields
        rather than hiding them for good."""
        css = (REPO_ROOT / "src/control_server/static/css/settings.css").read_text()
        assert re.search(
            r'\.settings-form:has\(input\[name="LITCLOCK_CONTENT_MODE"\]\[value="quotes"\]:checked\)'
            r'\s*\[data-fortune-only\]:not\(:has\(\[role="alert"\]\)\)\s*\{\s*display:\s*none;',
            css,
        )

    def test_the_fortune_block_spaces_its_fields_like_the_form(self):
        """The wrapper is one flex item of .settings-form; without its own gap
        the help line sits flush on the next label (seen in the README shot)."""
        css = (REPO_ROOT / "src/control_server/static/css/settings.css").read_text()
        form_gap = re.search(r"\.settings-form \{[^}]*gap:\s*([^;]+);", css).group(1)
        block = re.search(r"\.settings-fortune-only \{([^}]*)\}", css).group(1)
        assert "display: flex" in block and "flex-direction: column" in block
        assert re.search(r"gap:\s*" + re.escape(form_gap) + ";", block)

    def test_a_field_error_keeps_the_fortune_block_shown(self, pwa):
        """The hide rule spares a block holding an error; that only works if the
        lab's field errors render INSIDE the block with role="alert"."""
        client, _, _ = pwa
        html = client.get("/settings").data.decode()
        tmpl = (REPO_ROOT / "src/control_server/templates/settings.html.j2").read_text()
        block = tmpl[tmpl.index("data-fortune-only>") : tmpl.index("{# Not data-no-js-only: this section")]
        for key in ("LITCLOCK_FORTUNE_DWELL_MINUTES", "LITCLOCK_FORTUNE_CATEGORIES"):
            assert f"field_errors['{key}'] }}}}</p>" in block
            assert block.count('role="alert"') >= 2
        assert "data-fortune-only" in html

    def test_the_default_dwell_is_one_minute(self, pwa):
        client, _, _ = pwa
        html = client.get("/settings").data.decode()
        assert re.search(r'name="LITCLOCK_FORTUNE_DWELL_MINUTES" value="1"\s+checked', html)

    def test_save_joins_the_ticked_boxes(self, pwa):
        client, token, env = pwa
        resp = _post(
            client,
            token,
            LITCLOCK_CONTENT_MODE="fortune",
            LITCLOCK_FORTUNE_DWELL_MINUTES="10",
            fortune_categories_listed="1",
            fortune_category=["wisdom", "riddles"],
        )
        assert resp.status_code == 303
        assert "saved=lab" in resp.headers["Location"]
        saved = config.load_config(str(env))
        assert saved["LITCLOCK_CONTENT_MODE"] == "fortune"
        assert saved["LITCLOCK_FORTUNE_DWELL_MINUTES"] == "10"
        assert saved["LITCLOCK_FORTUNE_CATEGORIES"] == "wisdom,riddles"

    def test_no_ticked_box_means_all(self, pwa):
        client, token, env = pwa
        _post(client, token, LITCLOCK_CONTENT_MODE="fortune", fortune_categories_listed="1")
        assert config.load_config(str(env))["LITCLOCK_FORTUNE_CATEGORIES"] == ""

    def test_an_unrendered_list_leaves_the_pick_alone(self, pwa):
        client, token, env = pwa
        _post(client, token, LITCLOCK_CONTENT_MODE="fortune")
        assert config.load_config(str(env))["LITCLOCK_FORTUNE_CATEGORIES"] == "riddles"

    def test_an_invalid_value_re_renders_the_section(self, pwa):
        client, token, _ = pwa
        resp = _post(client, token, query="", LITCLOCK_CONTENT_MODE="mix")
        assert resp.status_code == 422
        assert b"settings-lab-h" in resp.data

    def test_other_sections_cannot_write_lab_keys(self, pwa):
        client, token, env = pwa
        client.post(
            "/settings",
            data={"csrf_token": token, "section": "advanced", "LITCLOCK_CONTENT_MODE": "fortune"},
            base_url="http://litclock.local",
            headers={"Origin": "http://litclock.local"},
        )
        assert "LITCLOCK_CONTENT_MODE" not in config.load_config(str(env))

    def test_no_databases_shows_the_install_note(self, pwa, monkeypatch, tmp_path):
        client, _, _ = pwa
        monkeypatch.setattr(fortune, "FORTUNE_DIR", tmp_path / "none")
        html = client.get("/settings").data.decode()
        assert "No fortune databases are installed" in html
        assert "fortune_categories_listed" not in html


# ─── install paths ───────────────────────────────────────────────────────────


def test_install_paths_carry_the_databases():
    packages = (REPO_ROOT / "pi-gen/stage3/00-install-deps/00-packages").read_text().split()
    assert "fortunes" in packages
    chroot = (REPO_ROOT / "pi-gen/stage3/00-install-deps/01-run.sh").read_text()
    assert re.search(r"^\s+fortunes$", chroot, re.M)
    assert "fortunes-off" not in chroot.replace("# fortunes-off", ""), "bookworm's archive lacks fortunes-off"


# ─── the CI render sweep ─────────────────────────────────────────────────────


def test_render_sweep_a_whole_day(dbroot, monkeypatch, tmp_path):
    """Every dwell slot of a day composes a full panel frame without error:
    fortune body, time-led masthead, QR. The guard that keeps a permanently
    rarely-used mode from rotting unnoticed (owner decision 2026-08-12)."""
    monkeypatch.setenv("WEATHER_ENABLED", "false")
    monkeypatch.setenv("LITCLOCK_CONTENT_MODE", "fortune")
    monkeypatch.setattr(lc, "RUNTIME_RENDER_DIR", str(tmp_path))
    start = datetime(2026, 12, 31, 0, 0)
    for k in range(288):
        image, meta, _, _ = lc.main(at=start + timedelta(minutes=5 * k))
        assert meta["render_mode"] == "fortune", k
        assert image.size == lc.DISPLAY_SIZE


@pytest.mark.skipif(
    not Path("/usr/share/games/fortunes/fortunes.dat").exists(), reason="Debian fortune databases not installed"
)
class TestRealDatabases:
    """The installed Debian databases, where present. Not a CI gate (CI does
    not install them), but the curation view on a dev box or a Pi."""

    def test_most_entries_are_usable_and_every_usable_one_renders(self, monkeypatch):
        monkeypatch.setattr(fortune, "FORTUNE_DIR", Path("/usr/share/games/fortunes"))
        usable = total = 0
        for db in fortune.list_databases(include_offensive=False):
            for i in range(db.count):
                total += 1
                f = fortune.normalise(fortune._entry_text(db, i), db.name)
                layout = f and fortune.fit(f)
                if layout:
                    usable += 1
                    if usable % 25 == 0:
                        assert lc._runtime_frame_ok(fortune.render(f, layout)) is None, f.raw
        assert usable / total > 0.6, f"only {usable}/{total} usable"


# ─── review follow-ups (gstack /review on the litclock-dev#533 branch) ───────────────────


def test_every_allowed_character_is_in_both_fonts():
    """A range-based allowlist let U+2011, U+201B, U+2023-2025 and friends
    through, which the shipped Literata lacks: tofu on the panel."""
    ttlib = pytest.importorskip("fontTools.ttLib")
    for name in (quote_renderer.FONT_REGULAR, quote_renderer.FONT_CREDITS):
        cmap = ttlib.TTFont(str(quote_renderer.FONTS_DIR / name)).getBestCmap()
        missing = sorted(hex(ord(c)) for c in fortune._ALLOWED if ord(c) not in cmap)
        assert not missing, f"{name} lacks {missing}"


def test_listing_databases_does_not_load_pillow(dbroot):
    """The control server imports fortune only to list databases; Pillow must
    not come along into that long-running process."""
    code = (
        "import sys; sys.path.insert(0, 'src'); import fortune; "
        f"fortune.categories({str(dbroot)!r}); print('PIL' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "False", out.stderr


class TestPickFollowUps:
    T = datetime(2026, 10, 22, 10, 0)

    def test_a_vetoed_frame_draws_another(self, dbroot):
        seen = []

        def accept(f, layout):
            seen.append(f.raw)
            return len(seen) > 1

        f, _ = fortune.pick(self.T, selected=("wisdom", "riddles"), accept=accept)
        assert len(seen) == 2 and f.raw == seen[1]

    def test_painter_retries_a_rejected_frame(self, dbroot):
        calls = []

        def frame_ok(frame):
            calls.append(1)
            return "forced" if len(calls) == 1 else None

        assert fortune.current_fortune(self.T, {}, frame_ok=frame_ok) is not None
        assert len(calls) == 2

    def test_budget_ends_the_pick(self, dbroot, monkeypatch):
        monkeypatch.setattr(fortune, "PICK_BUDGET_S", -1)
        assert fortune.pick(self.T) is None

    def test_empty_pool_is_said(self, dbroot, caplog):
        with caplog.at_level("WARNING"):
            assert fortune.pick(self.T, selected=("off-rude",)) is None
        assert "no databases to draw from" in caplog.text


def test_time_slot_without_raqm(monkeypatch):
    monkeypatch.setattr(lc.features, "check", lambda f: False)
    monkeypatch.setattr(lc, "_masthead_cache", {})
    feats, slot = lc._time_slot_metrics()
    assert feats is None
    mh = lc._masthead_metrics()
    spans = {lc._draw_time_lockup(_probe(), mh, t, "Mon, May 04") for t in ("11:11", "00:00", "20:08")}
    assert len(spans) == 1


class TestLabFollowUps:
    def test_pills_carry_no_server_selected_class(self, pwa):
        client, _, _ = pwa
        html = client.get("/settings").data.decode()
        lab = html[html.index('id="settings-lab-h"') :]
        assert "is-selected" not in lab[: lab.index("</section>")]

    def test_an_off_list_dwell_stays_visible_and_checked(self, pwa):
        client, _, env = pwa
        env.write_text(env.read_text() + "export LITCLOCK_FORTUNE_DWELL_MINUTES=30\n")
        html = client.get("/settings").data.decode()
        assert re.search(r'value="30"\s+checked', html)

    def test_an_uninstalled_pick_survives_a_save(self, pwa):
        client, token, env = pwa
        env.write_text("export LITCLOCK_FORTUNE_CATEGORIES=riddles,gone\n")
        html = client.get("/settings").data.decode()
        assert re.search(r'value="gone"\s+checked>', html) and "gone (not installed)" in html
        _post(
            client,
            token,
            LITCLOCK_CONTENT_MODE="fortune",
            fortune_categories_listed="1",
            fortune_category=["riddles", "gone"],
        )
        assert config.load_config(str(env))["LITCLOCK_FORTUNE_CATEGORIES"] == "riddles,gone"


def test_smoke_gate_also_renders_the_quote_fallback(dbroot, tmp_path):
    r = TestDryRunPin()._run(dbroot, tmp_path)
    assert r.returncode == 0, r.stderr
    assert "fortune-mode fallback tier render_mode=" in r.stdout
    assert r.stdout.rstrip().endswith("render_mode=fortune")


# ─── update.sh backfill, executed (D4 on the litclock-dev#533 review) ───────────────────


def _fortune_block() -> str:
    body = (REPO_ROOT / "scripts" / "update.sh").read_text()
    start = body.index("_fortune_apt_install() {")
    end = body.index("# ─── Phase 1: Stop timer", start)
    return body[start:end]


def _run_backfill(tmp_path, *, installed: bool, install_ok_after_update: bool, install_ok: bool = False):
    """Run the shipped block with sudo/apt-get/dpkg-query stubbed on PATH.
    apt-get records each call; `install` succeeds when install_ok, or once an
    `update` has run when install_ok_after_update (the emptied-lists image)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls"
    stubs = {
        "sudo": 'exec "$@"\n',
        "dpkg-query": f'[ "{int(installed)}" = 1 ] && printf "install ok installed"\nexit 0\n',
        "apt-get": (
            # "install --download-only" is recorded as "download", the real
            # install as "install": the kill timeout bounds only the former.
            f'case " $* " in *" --download-only "*) echo download >> "{calls}";; *) echo "$1" >> "{calls}";; esac\n'
            'case "$1" in\n'
            f'  update) touch "{tmp_path}/updated"; exit 0;;\n'
            f'  install) [ "{int(install_ok)}" = 1 ] && exit 0\n'
            f'           [ "{int(install_ok_after_update)}" = 1 ] && [ -e "{tmp_path}/updated" ] && exit 0\n'
            '           echo "E: Unable to locate package fortunes"; exit 100;;\n'
            "esac\n"
        ),
    }
    for name, text in stubs.items():
        (bindir / name).write_text("#!/bin/bash\n" + text)
        (bindir / name).chmod(0o755)
    script = (
        f"export PATH={bindir}:$PATH\n"
        'log_info() { echo "INFO $*"; }\nlog_warn() { echo "WARN $*"; }\n'
        + _fortune_block()
        + 'echo REACHED_PHASE_1\n'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    made = calls.read_text().split() if calls.exists() else []
    return r, made


class TestUpdateShBackfill:
    def test_installed_package_is_left_alone(self, tmp_path):
        r, calls = _run_backfill(tmp_path, installed=True, install_ok_after_update=True)
        assert calls == [] and "REACHED_PHASE_1" in r.stdout

    def test_missing_package_installs_once(self, tmp_path):
        r, calls = _run_backfill(tmp_path, installed=False, install_ok_after_update=False, install_ok=True)
        assert calls == ["download", "install"]
        assert "WARN" not in r.stdout

    def test_emptied_lists_refresh_and_retry(self, tmp_path):
        r, calls = _run_backfill(tmp_path, installed=False, install_ok_after_update=True)
        assert calls == ["download", "update", "download", "install"]
        assert "WARN" not in r.stdout

    def test_a_failed_install_is_said_and_not_fatal(self, tmp_path):
        r, calls = _run_backfill(tmp_path, installed=False, install_ok_after_update=False)
        assert calls == ["download", "update", "download"]
        assert "WARN fortunes install failed" in r.stdout
        assert "Unable to locate package" in r.stdout, "apt's own reason must reach the journal"
        assert r.returncode == 0 and "REACHED_PHASE_1" in r.stdout


def test_all_never_reaches_the_offensive_tree(dbroot):
    """Owner decision on the litclock-dev#533 review: an empty pick is every ORDINARY
    database; an offensive one needs its own tick AND NSFW on."""
    assert all(not db.offensive for db in fortune.pool((), allow_nsfw=True))
    assert {db.key for db in fortune.pool(("off-rude",), allow_nsfw=True)} == {"off-rude"}
    assert fortune.pool(("off-rude",), allow_nsfw=False) == []


def test_ticking_an_ordinary_name_never_selects_its_offensive_twin(dbroot):
    """Codex review: wisdom and off/wisdom shared one key, so the ordinary box
    pulled in the offensive half with NSFW on."""
    assert {db.key for db in fortune.pool(("wisdom",), allow_nsfw=True)} == {"wisdom"}
    both = fortune.pool(("wisdom", "off-wisdom"), allow_nsfw=True)
    assert {(db.name, db.offensive) for db in both} == {("wisdom", False), ("wisdom", True)}


def test_a_huge_dwell_is_a_field_error_not_a_crash():
    ok, err = config.validate_setting("LITCLOCK_FORTUNE_DWELL_MINUTES", "0" * 5000 + "5")
    assert ok is False and err


def test_an_oversized_index_is_skipped(dbroot, monkeypatch):
    monkeypatch.setattr(fortune, "MAX_DAT_BYTES", 10)
    assert fortune.list_databases() == []


def test_the_budget_covers_the_database_scan(dbroot, monkeypatch):
    real = fortune.list_databases
    clock = [0.0]
    monkeypatch.setattr(fortune.time, "monotonic", lambda: clock[0])

    def slow_scan(*a, **k):
        clock[0] += fortune.PICK_BUDGET_S + 1
        return real(*a, **k)

    monkeypatch.setattr(fortune, "list_databases", slow_scan)
    assert fortune.pick(datetime(2026, 10, 22, 10, 0)) is None


def test_an_out_of_range_stored_dwell_is_not_offered(pwa):
    """A pill for '999' would post it back and fail every later save."""
    client, _, env = pwa
    env.write_text(env.read_text() + "export LITCLOCK_FORTUNE_DWELL_MINUTES=999\n")
    html = client.get("/settings").data.decode()
    assert 'value="999"' not in html


def test_an_uninstalled_pick_can_be_cleared(pwa):
    client, token, env = pwa
    env.write_text("export LITCLOCK_FORTUNE_CATEGORIES=gone\n")
    _post(client, token, LITCLOCK_CONTENT_MODE="fortune", fortune_categories_listed="1")
    assert config.load_config(str(env))["LITCLOCK_FORTUNE_CATEGORIES"] == ""


class TestLabSectionStays:
    """Owner report 2026-10-23: after switching fortune -> quotes the section
    vanished. Since 2026-10-24 it is simply always there."""

    def test_switching_back_to_quotes_keeps_the_section(self, pwa):
        client, token, env = pwa
        _post(client, token, LITCLOCK_CONTENT_MODE="fortune", fortune_categories_listed="1")
        _post(client, token, LITCLOCK_CONTENT_MODE="quotes", fortune_categories_listed="1")
        assert config.load_config(str(env))["LITCLOCK_CONTENT_MODE"] == "quotes"
        assert b"settings-lab-h" in client.get("/settings").data

    def test_a_quotes_save_keeps_the_hidden_fortune_settings(self, pwa):
        """The fortune-only fields are hidden, not removed: they still post."""
        client, token, env = pwa
        _post(
            client,
            token,
            LITCLOCK_CONTENT_MODE="quotes",
            LITCLOCK_FORTUNE_DWELL_MINUTES="10",
            fortune_categories_listed="1",
            fortune_category=["riddles"],
        )
        saved = config.load_config(str(env))
        assert saved["LITCLOCK_FORTUNE_DWELL_MINUTES"] == "10" and saved["LITCLOCK_FORTUNE_CATEGORIES"] == "riddles"
