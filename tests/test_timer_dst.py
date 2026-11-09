"""The minute timer across a DST fall-back (litclock-dev#980).

A bare `OnCalendar=*-*-* *:*:56` is evaluated in LOCAL time. At the fall-back,
systemd steps from the 01:59:56 run straight to 02:00:56 in the new offset, so
the repeated hour gets no ticks and the panel sits on "1:00" for 61 minutes.
The fix is the `UTC` suffix.

The executed tests ask systemd itself for the schedule, against the spec as it
stands in the unit file. The fall-back tests carry a control: the same spec with
the suffix stripped must reproduce the skip, or the check cannot fail and proves
nothing. The local-second test has no control; it guards the lead (litclock-dev#762)
against the suffix, not the DST skip.

The executed tests skip on a host whose systemd-analyze lacks --base-time, which
would leave only the static pin. Under CI that skip is a failure instead.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TIMER = REPO_ROOT / "systemd" / "litclock.timer"

# Fall-back instants, one hour BEFORE the clocks go back, in UTC.
US_FALL_BACK = "2026-11-01 06:00:00 UTC"  # America/Chicago, 02:00 CDT -> 01:00 CST at 07:00 UTC
AU_FALL_BACK = "2027-04-03 15:00:00 UTC"  # Australia/Sydney, 03:00 AEDT -> 02:00 AEST at 16:00 UTC


def _on_calendar() -> str:
    specs = [
        ln.split("=", 1)[1].strip()
        for ln in TIMER.read_text(encoding="utf-8").splitlines()
        if ln.startswith("OnCalendar=")
    ]
    assert len(specs) == 1, f"expected exactly one OnCalendar= in litclock.timer, found {specs}"
    return specs[0]


def test_the_spec_is_pinned_to_utc():
    spec = _on_calendar()
    assert spec.endswith(" UTC"), (
        f"litclock.timer's OnCalendar is {spec!r}. Without the UTC suffix systemd evaluates "
        "it in local time and skips the repeated hour at every DST fall-back: the panel "
        "freezes on 1:00 for 61 minutes (litclock-dev#980)"
    )


def _systemd_analyze_with_base_time() -> str | None:
    exe = shutil.which("systemd-analyze")
    if not exe:
        return None
    probe = subprocess.run(
        [exe, "calendar", "--base-time=2026-01-01 00:00:00 UTC", "*-*-* *:*:56"],
        capture_output=True,
        text=True,
        env={**os.environ, "TZ": "UTC"},
        timeout=30,
    )
    return exe if probe.returncode == 0 else None


SYSTEMD_ANALYZE = _systemd_analyze_with_base_time()
needs_systemd = pytest.mark.skipif(
    SYSTEMD_ANALYZE is None, reason="needs systemd-analyze with --base-time (systemd >= 254)"
)


def test_ci_runs_the_executed_checks():
    if not os.environ.get("CI"):
        pytest.skip("only enforced under CI")
    assert SYSTEMD_ANALYZE is not None, (
        "CI has no systemd-analyze with --base-time, so the DST fall-back tests below are "
        "skipped and only the static UTC pin guards litclock-dev#980"
    )


_IN_UTC = re.compile(r"\(in UTC\): \w{3} (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC")
_LOCAL = re.compile(r"(?:Next elapse|Iteration #\d+): \w{3} \d{4}-\d{2}-\d{2} \d{2}:\d{2}:(\d{2}) ")


def _fires(spec: str, tz: str, base: str, n: int) -> tuple[list[datetime], list[int]]:
    """The next `n` elapses after `base`, as UTC instants, plus each one's LOCAL second."""
    out = subprocess.run(
        [SYSTEMD_ANALYZE, "calendar", f"--base-time={base}", f"--iterations={n}", spec],
        capture_output=True,
        text=True,
        env={**os.environ, "TZ": tz},
        timeout=30,
        check=True,
    ).stdout
    utc = [datetime.strptime(m, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC) for m in _IN_UTC.findall(out)]
    local_seconds = [int(s) for s in _LOCAL.findall(out)]
    assert len(utc) == n and len(local_seconds) == n, f"could not parse systemd-analyze output:\n{out}"
    return utc, local_seconds


def _largest_gap(fires: list[datetime]) -> timedelta:
    return max(b - a for a, b in zip(fires, fires[1:], strict=False))


@needs_systemd
@pytest.mark.parametrize(
    "tz, base", [("America/Chicago", US_FALL_BACK), ("Australia/Sydney", AU_FALL_BACK)], ids=["us", "au"]
)
def test_every_minute_of_the_repeated_hour_gets_a_tick(tz, base):
    # Three hours of ticks spanning the transition: the hour before, the
    # repeated hour, and the hour after.
    n = 180
    spec = _on_calendar()

    # Control first: the bare spec must reproduce the 2026-11-01 skip, or this
    # test could not see the bug it guards against.
    bare = spec.removesuffix(" UTC")
    assert _largest_gap(_fires(bare, tz, base, n)[0]) == timedelta(minutes=61), (
        "the control no longer reproduces the fall-back skip, so this test cannot fail; "
        "fix the harness before trusting it"
    )

    fires, _ = _fires(spec, tz, base, n)
    assert _largest_gap(fires) == timedelta(minutes=1), (
        f"litclock.timer skips ticks at the {tz} fall-back (litclock-dev#980): largest gap {_largest_gap(fires)}"
    )


@needs_systemd
@pytest.mark.parametrize("tz", ["America/Chicago", "Asia/Kathmandu", "Australia/Lord_Howe", "Asia/Kolkata"])
def test_the_tick_still_lands_on_local_second_56(tz):
    # The lead (litclock-dev#762) is defined in local seconds. A UTC spec keeps
    # it only because every zone's offset is a whole number of minutes; the
    # quarter- and half-hour zones are the ones that would break if that ever
    # stopped holding.
    _, local_seconds = _fires(_on_calendar(), tz, "2026-06-15 12:00:00 UTC", 5)
    assert set(local_seconds) == {56}, f"{tz}: timer fires at local seconds {local_seconds}, not :56"
