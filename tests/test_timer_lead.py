"""The timer lead (litclock-dev#762).

The clock fires its render timer ~4s BEFORE the minute boundary and renders the
UPCOMING minute, so the panel's unavoidable blackout falls at the boundary
instead of after it. Firing at `:00` landed the new frame around `:06.6`, which
meant the panel showed the PREVIOUS minute's timestring for ~4s of every
minute — a quote reading 11:59 still on screen at 12:00:03 reads as a broken
clock.

Two halves that are only correct together, which is why they are pinned in one
file: the `.timer` fires early, and the painter offsets its single `now`. The
timer alone renders the previous minute's quote permanently (litclock-dev#762 Trap 1).
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TIMER = REPO_ROOT / "systemd" / "litclock.timer"
PAINTER = REPO_ROOT / "src" / "literary_clock.py"


def _default_lead() -> float:
    """The production constant, imported — not scraped out of the source.

    The first version regexed the literal out of `os.getenv(..., "4")`. That
    broke the moment the parsing gained a guard, which is the tell: a test
    reading source text is coupled to how the value is SPELLED rather than to
    what it IS.
    """
    import literary_clock

    return literary_clock.RENDER_LEAD_DEFAULT_S


def _executed(path: Path) -> str:
    """Full-line comments dropped — this change is heavily commented, and every
    landmark below is also NAMED in the prose beside it."""
    return "\n".join(
        ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#")
    )


class TestTheTwoHalvesAgree:
    """Trap 1: either half alone is a correctness bug, not a partial win."""

    def test_the_timer_fires_before_the_boundary(self):
        body = _executed(TIMER)
        m = re.search(r"OnCalendar=\*-\*-\* \*:\*:(\d{2})", body)
        assert m, f"litclock.timer no longer has a per-minute OnCalendar:\n{body}"
        second = int(m.group(1))
        assert second != 0, (
            "the timer fires AT the boundary again. The panel blackout then falls after "
            ":00 and the clock shows the previous minute's timestring for ~4s of every "
            "minute (litclock-dev#762)"
        )
        assert 45 <= second <= 59, (
            f"OnCalendar fires at :{second:02d}, which is not a lead — it is most of a "
            "minute early. The lead must be a few seconds, not a different minute"
        )

    def test_the_painter_offsets_its_now(self):
        body = _executed(PAINTER)
        assert "RENDER_LEAD_S" in body, (
            "the painter no longer offsets its clock. With the timer at :56 and no offset, "
            "datetime.now() still reports the OLD minute and the clock renders the previous "
            "minute's quote PERMANENTLY (litclock-dev#762 Trap 1)"
        )
        assert re.search(
            r"now = \(datetime\.now\(\)\.astimezone\(\) \+ timedelta\(seconds=RENDER_LEAD_S\)\)"
            r"\.astimezone\(\)\.replace\(tzinfo=None\)",
            body,
        ), (
            "the compose site's `now` is not the offset target, or it stopped being AWARE "
            "arithmetic. Naive `datetime.now() + timedelta` fabricates a minute that does not "
            "exist across a DST spring-forward; reading _time.time() instead escapes every "
            "time-freezing test in this repo, which override datetime.now() (/review)"
        )

    def test_the_lead_covers_the_gap_the_timer_leaves(self):
        """The two constants are one design. A timer at :56 with a 1s lead
        would still render the old minute; a 10s lead at :56 would skip one."""
        second = int(re.search(r"OnCalendar=\*-\*-\* \*:\*:(\d{2})", _executed(TIMER)).group(1))
        lead = _default_lead()
        landing = second + lead
        # The invariant is lead ~= 60 - second, NOT merely "lands in the next
        # minute". /review measured `:45` with a 20s lead surviving a
        # sum-only bound: landing 65 is inside [60,120), but it fires at :45,
        # targets the next minute, and puts that minute's quote on the glass
        # around :55 — about five seconds BEFORE the minute it names begins.
        # Both constants drifting together is the mirror of the bug being fixed.
        # The SUM alone is an identity any co-drifted pair satisfies: /review
        # measured `:45` with a 15s lead landing on exactly 60 and passing,
        # while firing 15s early and putting the next minute's quote on the
        # glass ~10s BEFORE that minute begins. Anchor the timer second to the
        # measured prep+blackout budget too.
        assert 54 <= second <= 58, (
            f"the timer fires at :{second:02d}. Fire-to-legible is ~5.6s (prep ~2.6 + "
            "blackout ~2.4 + busy-tail), so firing earlier than :54 paints the next "
            "minute's quote BEFORE that minute begins — the mirror of the bug being fixed"
        )
        assert 60 <= landing <= 62, (
            f"the timer fires at :{second:02d} and the painter leads by {lead}s, so it renders "
            f"for T+{landing}s. The pair must land ON the boundary (lead ~= 60 - second): "
            f"{landing - 60:+.0f}s off means the blackout falls in the wrong place, and a "
            "negative offset paints a minute before it begins"
        )


class TestEverySiteAgreesOnTheMinute:
    """The invariant: quote, masthead date, fallback draw and status must all
    describe the SAME minute — the upcoming one. Independent `datetime.now()`
    calls would disagree with the threaded target."""

    def test_a_single_threaded_target_not_independent_clock_reads(self):
        body = _executed(PAINTER)
        # Inside main(), after the target is computed, nothing may read the wall
        # clock again — that is how the four sites drift apart.
        anchor = "timedelta(seconds=RENDER_LEAD_S)"
        # Start AFTER the target line itself, or the window re-matches the very
        # call that defines the target.
        start = body.index("\n", body.index(anchor)) + 1
        rest = body[start:]
        end = start + (rest.index("\ndef ") if "\ndef " in rest else len(rest))
        window = body[start:end]
        assert window and anchor not in window, "window slicing is wrong — it still contains the target line"
        assert "datetime.now()" not in window, (
            "a second datetime.now() appears after the offset target is computed. At "
            "23:59:56 that call reports YESTERDAY while the target is tomorrow, so the "
            "masthead date and the quote disagree (litclock-dev#762 Trap 3):\n"
            + "\n".join(ln for ln in window.splitlines() if "datetime.now()" in ln)
        )


class TestTheNightlyClearStillFires:
    """Trap 2, the one that fails SILENTLY.

    The nightly full `epd.Clear()` is gated on minute == 0. Firing at :56 means
    a live `datetime.now().minute` is never 0, so the clear would simply stop
    happening and ghosting would accumulate over weeks with nothing in the logs.
    """

    def test_the_gate_reads_the_offset_target(self):
        body = _executed(PAINTER)
        m = re.search(r"if (\S+)\.minute == 0 and (\S+)\.hour == display_clear_hour:", body)
        assert m, "the nightly clear gate is gone or was reshaped"
        assert m.group(1) == "now" and m.group(2) == "now", (
            f"the clear gate reads {m.group(1)}/{m.group(2)} instead of the offset target. "
            "With the timer at :56 a live clock read is NEVER minute 0, so the hourly "
            "epd.Clear() stops firing and ghosting accumulates silently "
            "(litclock-dev#762 Trap 2)"
        )


class TestTheOffsetSurvivesIntoWhatIsActuallyPublished:
    """The executed half, and the one the issue actually asked for.

    Everything above reads SOURCE. /review measured what that misses: inserting
    `now = now - timedelta(seconds=RENDER_LEAD_S)` immediately after the target
    is computed passes all of it — the anchor still matches the line above the
    rebind, the no-second-clock-read grep sees no new `datetime.now()`, and the
    clear-gate arithmetic recomputes its own target instead of executing
    production code. Ten green tests, offset erased.

    So this runs the real `main()` with a frozen clock and asserts on the value
    that actually reaches the panel and the status file.
    """

    @staticmethod
    def _frozen(monkeypatch, tmp_path, wall):
        """Freeze the clock the painter reads, the way the rest of this repo
        does it — subclass datetime and override now().

        That idiom is load-bearing: an implementation that reads `_time.time()`
        instead escapes it silently, and because the escape yields the REAL
        clock the failure is time-dependent rather than deterministic. That is
        exactly how a green local run turned into a red CI one here.
        """
        import literary_clock

        class FrozenDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return wall

        monkeypatch.setattr(literary_clock, "datetime", FrozenDT)
        monkeypatch.setenv("WEATHER_ENABLED", "false")
        # The attribute, not the env var: STATUS_FILE is resolved at IMPORT
        # time, so setenv after the module is loaded reaches nothing.
        monkeypatch.setattr(literary_clock, "STATUS_FILE", str(tmp_path / "status.json"))
        return literary_clock

    @pytest.mark.parametrize(
        "wall,expected",
        [
            (datetime(2026, 8, 30, 12, 34, 56), "12:35"),   # the ordinary case
            (datetime(2026, 8, 30, 23, 59, 56), "00:00"),   # Trap 3: the date rolls
            (datetime(2026, 8, 30, 1, 59, 56), "02:00"),    # Trap 2: the clear tick
        ],
    )
    def test_main_renders_the_upcoming_minute(self, monkeypatch, tmp_path, wall, expected):
        lc = self._frozen(monkeypatch, tmp_path, wall)

        # RECORD what the pick actually receives, rather than inferring it from
        # the returned meta. The inferring version was guarded on
        # `meta is not None`, and get_current_quote() returns None wherever
        # `images/` is absent — which is EVERY CI job, since images/ is
        # gitignored and only build-image.yml downloads it. So the assertion ran
        # on a dev box with 149MB of images and was skipped in CI, and the
        # un-offset-alias mutant survived the whole suite there. Measured.
        seen: list = []

        # A real PNG: main() opens image_path, and a missing file trips the
        # corrupt-PNG guard, which nulls the meta and skips the assertion.
        from PIL import Image  # noqa: PLC0415

        png = tmp_path / "frame.png"
        Image.new("1", (800, 400), 1).save(png)

        def _recording_pick(now=None, allow_nsfw=False):
            seen.append(now)
            return {
                "time": now.strftime("%H:%M"),
                "quote": "q",
                "author": "a",
                "title": "t",
                "image_path": str(png),
            }

        monkeypatch.setattr(lc, "get_current_quote", _recording_pick)
        monkeypatch.setattr(lc, "get_current_quote_runtime", lambda **kw: None)

        _image, meta, now = lc.main()

        assert seen, "the quote pick was never called — this test proves nothing"
        assert seen[0].strftime("%H:%M") == expected, (
            f"main() targeted {now:%H:%M} but handed the quote pick "
            f"{seen[0]:%H:%M} — an un-offset alias. The panel would paint the previous "
            "minute's quote under a correct-looking target and status file"
        )
        assert meta is not None and meta["time"] == expected
        assert now.strftime("%H:%M") == expected, (
            f"at wall-clock {wall:%H:%M:%S} main() rendered for {now:%H:%M}, not {expected}. "
            "The offset was lost between where it is computed and where it is used — which "
            "no amount of source-text assertion can see"
        )

    def test_the_published_status_carries_the_upcoming_minute(self, monkeypatch, tmp_path):
        """The status file is what /api/status and the PWA hero mirror, so it
        must name the minute that is ON THE PANEL, not the one that has just
        ended."""
        import json

        wall = datetime(2026, 8, 30, 12, 34, 56)
        lc = self._frozen(monkeypatch, tmp_path, wall)
        _image, quote_meta, now = lc.main()
        lc._write_status_file(quote_meta, now)

        published = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
        assert published["time"] == "12:35", (
            f"the status file says {published['time']!r} while the panel shows 12:35 — the "
            "PWA would contradict the device in the user's hand"
        )

    def test_the_date_rolls_with_the_target(self, monkeypatch, tmp_path):
        """Trap 3, executed: at 23:59:56 the render is for tomorrow."""
        wall = datetime(2026, 8, 30, 23, 59, 56)
        lc = self._frozen(monkeypatch, tmp_path, wall)
        _image, _meta, now = lc.main()
        assert (now.year, now.month, now.day) == (2026, 8, 31), (
            f"main() rendered for {now:%Y-%m-%d}; the masthead date must be tomorrow's when "
            "the target crosses midnight"
        )


def test_the_status_write_call_site_passes_the_target():
    """M1 from /review: `_write_status_file(quote_meta, now)` in the __main__
    block is outside `main()`, so no test that calls main() can see it swapped
    for `datetime.now()`. Executing __main__ is not worth it — it talks to the
    panel — so this is a source assertion, on comment-stripped lines, paired
    with the executed test of the function itself above."""
    body = _executed(PAINTER)
    assert "_write_status_file(quote_meta, now)" in body, (
        "the status write no longer passes the threaded target. A live clock read there "
        "publishes the minute that has just ENDED while the panel shows the next one, so "
        "the PWA contradicts the device in the user's hand"
    )
    assert "_write_status_file(quote_meta, datetime.now())" not in body


class TestTheNightlyClearIsDecidedByExecutingTheGate:
    """Trap 2's executed coverage — the half that was missing.

    The clear gate lives in `__main__`, not in `main()`, so no test that calls
    `main()` can see it. /review measured the consequence: rebinding
    `now = datetime.now()` on the line immediately before the gate SURVIVED the
    whole suite. That mutant is the exact failure this PR exists to prevent —
    at :56 a live read is never minute 0, so the nightly `epd.Clear()` stops
    firing forever and ghosting accumulates with nothing in the logs.

    The two source-level tests above could not catch it: the regex still
    matches `now.minute == 0` after a rebind, and the arithmetic test
    re-implements the gate in the test rather than running production code.

    So this LIFTS the real `__main__` block and executes it, the way
    tests/test_exit_without_finalization.py does, with a recording panel.
    """

    class _HardExit(Exception):
        def __init__(self, code):
            self.code = code

    def _run_main_block(self, target: datetime, clear_hour: str = "2") -> list[str]:
        """Execute the shipped __main__ block; return the panel calls it made."""
        import ast
        import logging as _logging
        import os
        import sys
        import types

        calls: list[str] = []
        hard = self._HardExit

        src = Path(REPO_ROOT / "src" / "literary_clock.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        main_if = next(
            n for n in tree.body if isinstance(n, ast.If) and ast.unparse(n.test).startswith("__name__")
        )
        body = "\n".join(ast.unparse(st) for st in main_if.body)

        class _EPD:
            def init(self):
                calls.append("init")

            def getbuffer(self, img):
                return img

            def display(self, buf):
                calls.append("display")

            def sleep(self):
                calls.append("sleep")

            def Clear(self):
                calls.append("Clear")

        class _Args:
            dry_run = False

        class _Parser:
            def __init__(self, *a, **k):
                pass

            def add_argument(self, *a, **k):
                pass

            def parse_args(self, *a, **k):
                return _Args()

        fake_os = types.SimpleNamespace(
            _exit=lambda code: (_ for _ in ()).throw(hard(code)),
            path=os.path,
            environ=os.environ,
            replace=os.replace,
            # The real block reads DISPLAY_CLEAR_HOUR here. Without getenv the
            # AttributeError is swallowed by the block's own `except Exception`
            # and the gate is never reached at all — a harness that "passes"
            # having executed nothing.
            getenv=lambda k, d=None: clear_hour if k == "DISPLAY_CLEAR_HOUR" else os.getenv(k, d),
            abort=lambda: (_ for _ in ()).throw(RuntimeError("os.abort() reached")),
            execv=lambda *a: (_ for _ in ()).throw(RuntimeError("os.execv() reached")),
        )

        driver = types.ModuleType("display_driver")
        driver.epd7in5 = types.SimpleNamespace(EPD=_EPD)

        import literary_clock as _lc

        # Seed from the REAL module globals, then override deliberately. A
        # hand-built namespace makes any name the block references but `ns`
        # lacks raise NameError INTO the block's own `except Exception` swallow
        # — so a mutant referencing `timedelta` or `RENDER_LEAD_S` passed while
        # executing almost nothing. The author patched one instance of this
        # (the getenv note below) rather than the class; this fixes the class.
        ns = dict(vars(_lc))
        ns.update({
            "__name__": "__main__",
            "os": fake_os,
            "sys": sys,
            "logging": _logging,
            "argparse": types.SimpleNamespace(ArgumentParser=_Parser),
            "signal": types.SimpleNamespace(signal=lambda *a: None, SIGTERM=15, SIGINT=2),
            "signal_handler": lambda *a: None,
            # The threaded target is what main() hands the block — the whole
            # point of the change.
            "main": lambda: (types.SimpleNamespace(size=(800, 480)), {"time": target.strftime("%H:%M")}, target),
            # Record the ARGUMENT, not just the call. A stub that discards it
            # lets a rebind between the clear gate and the status write survive:
            # /api/status would publish the minute that has just ENDED while the
            # panel shows the next one. Measured — it passed 4127 tests.
            "_write_status_file": lambda meta, when=None, *a, **k: calls.append(f"status:{when}"),
            "_write_heartbeat": lambda *a, **k: None,
            "datetime": datetime,
            "_epd": None,
        })

        saved = sys.modules.get("display_driver")
        sys.modules["display_driver"] = driver
        try:
            exec(compile(body, "<main-block>", "exec"), ns)
        except hard:
            pass
        finally:
            if saved is None:
                sys.modules.pop("display_driver", None)
            else:
                sys.modules["display_driver"] = saved

        assert "display" in calls, (
            f"the lifted block never reached epd.display — it bailed early and this test "
            f"proves nothing. Calls: {calls}"
        )
        # The status write must publish the TARGET. Recording the argument was
        # not enough on its own — /review measured a rebind between the clear
        # gate and this call surviving the full suite, because nothing asserted
        # on what was recorded. On a device that publishes the minute which has
        # just ENDED while the panel shows the next one.
        published = [c for c in calls if c.startswith("status:")]
        assert published, f"the status file was never written. Calls: {calls}"
        assert published[0] == f"status:{target}", (
            f"the status write published {published[0][7:]!r} but the panel was painted for "
            f"{target} — /api/status and the PWA hero would contradict the device in the "
            "user's hand"
        )
        return calls

    def test_the_clear_fires_on_the_target_hour(self):
        """At wall 01:59:56 the TARGET is 02:00:00, so the clear must fire —
        even though a live clock read would say minute 59."""
        calls = self._run_main_block(datetime(2026, 8, 30, 2, 0, 0))
        assert "Clear" in calls, (
            f"the nightly full clear did not fire for a 02:00 target. At :56 a live "
            f"datetime.now() is never minute 0, so reading the wall clock here makes the "
            f"clear stop happening FOREVER, silently. Calls: {calls}"
        )

    @pytest.mark.parametrize(
        "target",
        [
            datetime(2026, 8, 30, 1, 59, 0),   # target 01:59 — wrong minute
            datetime(2026, 8, 30, 2, 1, 0),    # target 02:01 — already cleared
            datetime(2026, 8, 30, 14, 0, 0),   # target 14:00 — wrong hour
        ],
    )
    def test_the_clear_does_not_fire_otherwise(self, target):
        """Once a day, not on every tick — a full clear is ~3s of extra
        waveform the owner sees as a flash."""
        calls = self._run_main_block(target)
        assert "Clear" not in calls, f"the clear fired for target {target:%H:%M}. Calls: {calls}"

    def test_it_honours_a_configured_clear_hour(self):
        calls = self._run_main_block(datetime(2026, 8, 30, 5, 0, 0), clear_hour="5")
        assert "Clear" in calls, f"DISPLAY_CLEAR_HOUR=5 did not clear at a 05:00 target: {calls}"


class TestTheOperatorKnobCannotBrickTheClock:
    """`RENDER_LEAD_S` is read at IMPORT, above the litclock-dev#531
    `except BaseException` guard (which lives inside `if __name__ ==
    "__main__"`). A bare `float(os.getenv(...))` therefore turns a typo — or an
    empty value — into a painter that dies every minute with the panel frozen
    on the last quote: the exact signature this repo documents as confusable
    with the litclock-dev#531 lgpio wedge — though the JOURNAL does separate them, which
    an earlier version of this comment denied (/review): litclock.service sets
    StandardError=journal, so the traceback names the variable. What is shared is
    the PANEL symptom, not the evidence.

    Empty is the realistic one. `export KEY=` is env.sh.sample's own idiom for
    an unset key, and update.sh merges sample keys into every device's env.sh.
    The OTA smoke gate does not backstop it either: Phase 4.5 runs the dry-run
    without sourcing env.sh, so a bad value passes smoke and Phase 7 starts a
    service that fails forever.
    """

    @pytest.mark.parametrize(
        "raw,why",
        [
            ("", "env.sh.sample's own idiom for an unset key"),
            ("   ", "whitespace-only"),
            ("4s", "a units typo"),
            ("four", "not a number at all"),
            ("-90", "negative — renders 90s into the PAST every minute"),
            ("999", "past a minute — skips one entirely"),
            # The BOUNDARIES themselves. /review widened the range to
            # [-60, 600] and every test still passed, so a lead of -30 or 300
            # would have been honoured. Zero is the sharpest: it is what an
            # operator reaching for "turn this off" types, and with the timer
            # at :56 it renders the previous minute's quote permanently.
            ("0", "zero — the target lands in the minute that is ENDING"),
            ("-0.0", "negative zero"),
            ("2.9", "just under the floor"),
            # /review: these were ACCEPTED until the floor moved 3.0 -> 4.0.
            # With the shipped :56 timer they target :59 of the ENDING minute,
            # which is the same permanent wrong-minute failure "0" is rejected
            # for. ("3", 3.0) was previously asserted HONOURED one test below.
            ("3", "the old floor — targets :59 with the shipped :56 timer"),
            ("3.9", "just under the real floor"),
            ("30.1", "just over the ceiling"),
        ],
    )
    def test_a_bad_value_falls_back_instead_of_raising(self, monkeypatch, raw, why):
        import literary_clock

        monkeypatch.setenv("LITCLOCK_RENDER_LEAD_S", raw)
        assert literary_clock._render_lead_seconds() == literary_clock.RENDER_LEAD_DEFAULT_S, (
            f"LITCLOCK_RENDER_LEAD_S={raw!r} ({why}) did not fall back to the default"
        )

    @pytest.mark.parametrize("raw,expect", [("5.3", 5.3), ("4", 4.0), ("30", 30.0), (" 6 ", 6.0)])
    def test_a_good_value_is_honoured(self, monkeypatch, raw, expect):
        """Including both inclusive boundaries — a bound nobody tests at the
        edge can be widened without going red."""
        import literary_clock

        monkeypatch.setenv("LITCLOCK_RENDER_LEAD_S", raw)
        assert literary_clock._render_lead_seconds() == expect

    def test_an_unset_variable_is_the_default(self, monkeypatch):
        import literary_clock

        monkeypatch.delenv("LITCLOCK_RENDER_LEAD_S", raising=False)
        assert literary_clock._render_lead_seconds() == literary_clock.RENDER_LEAD_DEFAULT_S

    def test_importing_the_painter_survives_a_bad_value(self, monkeypatch):
        """The property that actually matters: IMPORT must not raise. A unit
        test of the parser would pass even if the module-level call were
        reverted to a bare float()."""
        import subprocess
        import sys

        r = subprocess.run(
            [sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); import literary_clock"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "LITCLOCK_RENDER_LEAD_S": ""},
        )
        assert r.returncode == 0, (
            "importing literary_clock with an empty LITCLOCK_RENDER_LEAD_S raised. On a device "
            "that is the painter dying every minute with the panel frozen — indistinguishable "
            f"from the litclock-dev#531 wedge.\n{r.stderr[-1500:]}"
        )


def test_the_target_survives_a_dst_spring_forward(monkeypatch, tmp_path):
    """Executed, not spelled. The source-text guard above pins how the
    expression is WRITTEN, which the same file's own docstring warns against —
    a behaviour-preserving refactor would redden it while a semantic regression
    to naive `datetime.now() + timedelta` would not, if the spelling happened to
    match.

    So: freeze the clock at the instant before a real spring-forward and assert
    the target is the minute that actually follows it. Naive local addition
    yields 02:00 — a minute that does not exist in America/Chicago that day, and
    which would reach the panel, the masthead and /api/status.
    """
    import time as _t

    import literary_clock

    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/Chicago"
    _t.tzset()
    try:
        wall = datetime(2026, 3, 8, 1, 59, 56)

        class FrozenDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return wall

        monkeypatch.setattr(literary_clock, "datetime", FrozenDT)
        monkeypatch.setenv("WEATHER_ENABLED", "false")
        monkeypatch.setattr(literary_clock, "STATUS_FILE", str(tmp_path / "s.json"))
        monkeypatch.setattr(literary_clock, "get_current_quote_runtime", lambda **kw: None)
        monkeypatch.setattr(literary_clock, "get_current_quote", lambda **kw: None)

        _image, _meta, now = literary_clock.main()
        assert now.strftime("%H:%M") == "03:00", (
            f"at 01:59:56 CST the target is {now:%H:%M}; four seconds later the local wall "
            "clock reads 03:00 CDT. 02:00 is a minute that does not exist that day — naive "
            "`datetime.now() + timedelta` produces it, aware arithmetic does not"
        )
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        _t.tzset()
