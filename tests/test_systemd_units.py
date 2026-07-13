"""Tests for systemd unit file correctness.

Validates boot ordering, dependency chains, and critical properties
of all LitClock systemd units. Covers the validation checklist from
issue #111 (first-boot flow for image-based deployment).
"""

import configparser
import os

import pytest

SYSTEMD_DIR = os.path.join(os.path.dirname(__file__), "..", "systemd")


def parse_unit(filename):
    """Parse a systemd unit file into a configparser object."""
    path = os.path.join(SYSTEMD_DIR, filename)
    parser = configparser.ConfigParser(interpolation=None)
    # systemd allows duplicate keys (e.g., After=), but configparser doesn't.
    # For our tests, the last value wins — which matches how we write the files.
    parser.read(path)
    return parser


# Codex /review pointed out: a digits-only parse interprets `1min` as `1`,
# so a regression to `TimeoutStopSec=1min` would silently pass a `<= 30`
# guard even though `1min` = 60s exceeds the shutdown-splash budget. Parse
# the full systemd duration syntax: `<num><unit>` chunks where unit ∈
# {us, ms, s, m/min, h, d, w} (case-insensitive), bare numbers = seconds.
# See systemd.time(7).
_SYSTEMD_DURATION_UNITS = {
    "us": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
    "sec": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "m": 60.0,
    "min": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "d": 86400.0,
    "day": 86400.0,
    "days": 86400.0,
    "w": 604800.0,
    "week": 604800.0,
    "weeks": 604800.0,
}


def _parse_systemd_duration_to_seconds(value):
    """Convert a systemd duration string to seconds. Returns None on
    unparseable / empty input. Bare numbers default to seconds (systemd's
    default unit for *TimeoutStopSec* / *TimeoutStartSec*)."""
    import re as _re

    if not value:
        return None
    s = value.strip().lower()
    if not s:
        return None
    # Bare number → seconds (systemd convention for *TimeoutStopSec*).
    if _re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    # Otherwise chunks of `<num><unit>`, possibly whitespace-separated.
    total = 0.0
    for match in _re.finditer(r"(\d+(?:\.\d+)?)\s*([a-z]+)", s):
        num_str, unit = match.group(1), match.group(2)
        if unit not in _SYSTEMD_DURATION_UNITS:
            return None
        total += float(num_str) * _SYSTEMD_DURATION_UNITS[unit]
    return total if total > 0 else None


# ── Boot ordering (#111, updated for #128) ──────────────────────────


class TestBootOrdering:
    """Verify boot ordering: all services wait for sysinit.target (hardware
    ready), but splash/firstboot/timer are decoupled from each other so a
    display hang doesn't block the entire boot chain."""

    def test_splash_runs_after_sysinit(self):
        unit = parse_unit("litclock-splash.service")
        after = unit.get("Unit", "After", fallback="")
        assert "sysinit.target" in after

    def test_splash_has_timeout(self):
        """Splash must have a startup timeout so a display hang doesn't
        block systemd forever."""
        unit = parse_unit("litclock-splash.service")
        timeout = unit.get("Service", "TimeoutStartSec", fallback="")
        assert timeout, "TimeoutStartSec must be set"

    def test_splash_has_no_network_dependency(self):
        """Splash must not depend on network — it runs before WiFi is set up."""
        unit = parse_unit("litclock-splash.service")
        after = unit.get("Unit", "After", fallback="")
        wants = unit.get("Unit", "Wants", fallback="")
        requires = unit.get("Unit", "Requires", fallback="")
        for field in [after, wants, requires]:
            assert "network-online.target" not in field
            assert "network.target" not in field

    def test_firstboot_runs_after_sysinit(self):
        unit = parse_unit("litclock-firstboot.service")
        after = unit.get("Unit", "After", fallback="")
        assert "sysinit.target" in after

    def test_firstboot_runs_after_splash(self):
        """Firstboot must wait for splash to finish to avoid GPIO busy errors
        — both services access the e-ink display via SPI/GPIO."""
        unit = parse_unit("litclock-firstboot.service")
        after = unit.get("Unit", "After", fallback="")
        assert "litclock-splash.service" in after

    def test_firstboot_has_no_network_online_dependency(self):
        """Critical fix from #111 / PR #117: firstboot must NOT depend on
        network-online.target because WiFi isn't configured on a fresh image."""
        unit = parse_unit("litclock-firstboot.service")
        after = unit.get("Unit", "After", fallback="")
        wants = unit.get("Unit", "Wants", fallback="")
        requires = unit.get("Unit", "Requires", fallback="")
        for field in [after, wants, requires]:
            assert "network-online.target" not in field

    def test_timer_runs_after_sysinit(self):
        unit = parse_unit("litclock.timer")
        after = unit.get("Unit", "After", fallback="")
        assert "sysinit.target" in after

    def test_timer_decoupled_from_splash(self):
        """Timer must NOT depend on splash — a display hang must not
        prevent clock updates."""
        unit = parse_unit("litclock.timer")
        after = unit.get("Unit", "After", fallback="")
        assert "litclock-splash.service" not in after

    def test_timer_fires_every_minute(self):
        unit = parse_unit("litclock.timer")
        assert unit.get("Timer", "OnCalendar") == "*-*-* *:*:00"

    def test_timer_accuracy_is_one_second(self):
        unit = parse_unit("litclock.timer")
        assert unit.get("Timer", "AccuracySec") == "1s"


# ── Service properties ───────────────────────────────────────────────


class TestServiceProperties:
    def test_firstboot_is_oneshot(self):
        unit = parse_unit("litclock-firstboot.service")
        assert unit.get("Service", "Type") == "oneshot"

    def test_firstboot_runs_as_pi(self):
        """Firstboot must run as the pi user, not root — the clock and venv
        are owned by pi, and files created during setup must be writable."""
        unit = parse_unit("litclock-firstboot.service")
        assert unit.get("Service", "User") == "pi"

    def test_firstboot_remain_after_exit(self):
        """RemainAfterExit=yes keeps the unit 'active' so the timer's
        After= dependency is properly satisfied."""
        unit = parse_unit("litclock-firstboot.service")
        assert unit.get("Service", "RemainAfterExit") == "yes"

    def test_splash_is_oneshot(self):
        unit = parse_unit("litclock-splash.service")
        assert unit.get("Service", "Type") == "oneshot"

    def test_clock_service_is_oneshot(self):
        unit = parse_unit("litclock.service")
        assert unit.get("Service", "Type") == "oneshot"

    def test_shutdown_has_conflicts_shutdown_target(self):
        """Without Conflicts=shutdown.target, systemd won't gracefully stop
        the service during shutdown — it just kills it, bypassing ExecStop."""
        unit = parse_unit("litclock-shutdown.service")
        conflicts = unit.get("Unit", "Conflicts", fallback="")
        conflicts_tokens = set(conflicts.split())
        assert "shutdown.target" in conflicts_tokens

    def test_shutdown_conflicts_covers_all_poweroff_paths(self):
        """Issue #186: `shutdown -h now` isolates to halt.target, which doesn't
        reliably pull in shutdown.target. Without halt.target + poweroff.target
        in Conflicts=, ExecStop doesn't fire on `shutdown -h now` even though
        it fires on `poweroff`. Must cover all four targets."""
        unit = parse_unit("litclock-shutdown.service")
        conflicts = unit.get("Unit", "Conflicts", fallback="")
        conflicts_tokens = set(conflicts.split())
        for target in ("shutdown.target", "reboot.target", "halt.target", "poweroff.target"):
            assert target in conflicts_tokens, (
                f"{target} missing from Conflicts= — ExecStop may not fire when systemd isolates to it"
            )

    def test_shutdown_orders_against_all_shutdown_paths(self):
        """Issue #186 / pre-#268 invariant: Before= must order our
        ExecStop against every shutdown path (reboot / poweroff / halt /
        shutdown) so the paint completes before each path's terminal
        target. A prior PR briefly replaced these four entries with
        Before=final.target as an attempted #268 fix, but codex review
        flagged that Before=final.target only constrains stop-finish
        (not stop-start), so it doesn't actually defer the paint. Keep
        the four-target form for correctness; #268 stays open with a
        proper late-hook design TBD."""
        unit = parse_unit("litclock-shutdown.service")
        before = unit.get("Unit", "Before", fallback="")
        before_tokens = set(before.split())
        for target in ("shutdown.target", "reboot.target", "halt.target", "poweroff.target"):
            assert target in before_tokens, (
                f"{target} missing from Before= — ExecStop ordering breaks on this shutdown path"
            )

    def test_shutdown_has_no_default_dependencies(self):
        unit = parse_unit("litclock-shutdown.service")
        assert unit.get("Unit", "DefaultDependencies") == "no"

    def test_shutdown_does_NOT_conflict_with_litclock_service(self):
        """Issue #271 codex-review regression guard.

        An earlier draft of the #271 fix added Conflicts=litclock.service
        to force a stop of any mid-render service before ExecStop. That
        was WRONG: Conflicts= is bidirectional, and this unit is kept
        active by RemainAfterExit=yes — so every timer-fired start of
        litclock.service would conflict-stop this unit and fire
        shutdown-splash.sh during normal operation (painting "Powered
        Off" on the e-ink every single minute).

        The ordering directive (test_shutdown_before_litclock_service)
        is sufficient because litclock.service has its own default
        Conflicts=shutdown.target — both units are being stopped in the
        same shutdown transaction; we just need the order constraint.

        Do NOT reintroduce Conflicts=litclock.service.

        Tokenization note: a substring match on the raw Conflicts= value
        would false-fail under stylistic edits like
        ``Conflicts=shutdown.target ; ... mentions litclock.service ...``
        (configparser preserves inline ``;`` trailers) and could also miss
        a multi-line continuation that splits ``Conflicts=`` across lines.
        Tokenizing on whitespace and asserting set membership is the
        precise contract we want.
        """
        unit = parse_unit("litclock-shutdown.service")
        conflicts = unit.get("Unit", "Conflicts", fallback="")
        conflicts_tokens = set(conflicts.split())
        assert "litclock.service" not in conflicts_tokens, (
            "Conflicts=litclock.service is bidirectional + RemainAfterExit=yes — "
            "would fire ExecStop on every per-minute clock render. Use Before= only."
        )

    def test_shutdown_before_litclock_service(self):
        """Issue #271: Before=litclock.service is the ordering anchor that
        closes the SPI/GPIO race. In shutdown direction, Before= reverses
        to mean 'our stop runs after theirs' — so ExecStop only fires
        once the mid-render clock.service has fully stopped and released
        the SPI/GPIO handle. litclock.service's own default
        Conflicts=shutdown.target ensures it IS being stopped in the
        same shutdown transaction, so this Before= alone is sufficient.
        Without it, systemd is free to stop both units concurrently and
        the race the issue describes returns."""
        unit = parse_unit("litclock-shutdown.service")
        before = unit.get("Unit", "Before", fallback="")
        before_tokens = set(before.split())
        assert "litclock.service" in before_tokens, (
            "Before=litclock.service required for #271 — orders our ExecStop after mid-render stop"
        )

    def test_shutdown_does_NOT_conflict_with_litclock_timer(self):
        """Codex /review caught: bidirectional Conflicts=litclock.timer
        breaks normal operation. Both this unit (RemainAfterExit=yes) and
        litclock.timer stay active throughout normal runtime — at boot,
        whichever starts first conflict-stops the other, so either the
        shutdown splash paints during boot or the timer never fires.

        The narrow ~1s timer-queued-job race that motivated this addition
        is a P3 follow-up; the bidirectional-Conflicts fix is worse than
        the bug. Do NOT reintroduce.
        """
        unit = parse_unit("litclock-shutdown.service")
        conflicts = unit.get("Unit", "Conflicts", fallback="")
        conflicts_tokens = set(conflicts.split())
        assert "litclock.timer" not in conflicts_tokens, (
            "Conflicts=litclock.timer is bidirectional and breaks boot/runtime — "
            "the units coexist normally. Use Before= alone or a different mechanism."
        )

    def test_litclock_service_has_bounded_stop_timeout(self):
        """Issue #271 follow-up — wedged-render timeout guard.

        litclock.service is Type=oneshot and inherits
        DefaultTimeoutStopSec (90s on Bookworm systemd 252). If a render
        is wedged on the SPI bus during reboot (GPIO contention from a
        previous unclean exit), 90s would outlast
        litclock-shutdown.service's own 30s TimeoutStopSec — meaning the
        splash ExecStop is SIGKILLed before it can paint "Powered Off".

        Cap TimeoutStopSec at <= 30s so a wedged render is force-killed
        within the shutdown-splash budget. Healthy renders are ~9s, so
        a small cap is fine.
        """
        unit = parse_unit("litclock.service")
        timeout = unit.get("Service", "TimeoutStopSec", fallback="")
        seconds = _parse_systemd_duration_to_seconds(timeout)
        assert seconds is not None, (
            f"TimeoutStopSec must be set on litclock.service and parseable as a systemd duration; got {timeout!r}"
        )
        assert 1 <= seconds <= 30, (
            f"TimeoutStopSec={timeout!r} (= {seconds}s) must be bounded below "
            "shutdown-splash's 30s so a wedged render gets SIGKILL'd inside the budget"
        )


# ── Splash ExecStartPost trigger (#269) ──────────────────────────────


class TestSplashExecStartPost:
    """Issue #269 — the on-boot clock render moved from a direct
    runtheclock.sh call inside boot-splash.sh into ExecStartPost on
    litclock-splash.service. systemd's job queue then serializes the trigger
    against timer-fired litclock.service runs (no GPIO contention), and the
    current quote renders within ~10s of splash start instead of waiting up
    to ~60s for the next OnCalendar tick."""

    @pytest.fixture(scope="class")
    def post_line(self):
        path = os.path.join(SYSTEMD_DIR, "litclock-splash.service")
        with open(path) as f:
            for ln in f:
                if ln.startswith("ExecStartPost="):
                    return ln.rstrip("\n")
        return None

    def test_exec_start_post_set(self, post_line):
        assert post_line is not None, "litclock-splash.service must have ExecStartPost"

    def test_runs_as_root(self, post_line):
        """`+` prefix runs the post-hook as root despite User=pi —
        required for `systemctl start` without sudo."""
        assert post_line.startswith("ExecStartPost=+"), (
            "ExecStartPost must use `+` prefix to run as root despite User=pi"
        )

    def test_uses_no_block(self, post_line):
        """Without --no-block, calling systemctl from inside a running
        systemd job deadlocks (job queued behind self)."""
        assert "--no-block" in post_line

    def test_targets_litclock_service_not_runtheclock(self, post_line):
        """Must trigger the timer-managed unit, not the underlying script —
        otherwise systemd can't serialize against timer fires (GPIO race)."""
        assert "litclock.service" in post_line
        assert "runtheclock" not in post_line

    def test_guards_on_setup_complete(self, post_line):
        """Guard preserves first-boot path: when .setup-complete is absent,
        the post-hook is a no-op and litclock-firstboot.service handles
        provisioning unimpeded."""
        assert ".setup-complete" in post_line

    def test_swallows_failure_with_or_true(self, post_line):
        """Without `|| true`, the post-hook returns non-zero when
        .setup-complete is absent (every fresh image) and marks the splash
        unit as failed — breaking the first-boot path the guard was added
        to protect."""
        assert "|| true" in post_line, (
            "ExecStartPost must end with `|| true` so a missing .setup-complete "
            "flag does not fail litclock-splash.service"
        )

    def test_exec_start_still_runs_boot_splash(self):
        """The `+` prefix on ExecStartPost is only meaningful while the
        unit's primary ExecStart runs as User=pi via boot-splash.sh.
        Pin ExecStart so a refactor can't silently drop the splash render."""
        unit = parse_unit("litclock-splash.service")
        assert unit.get("Service", "ExecStart") == "/home/pi/litclock/scripts/boot-splash.sh"

    def test_splash_still_runs_as_pi(self):
        """The `+` prefix is only meaningful when User=pi. If User= changes
        to root, test_runs_as_root passes but the invariant it protects
        (unit-level least privilege) is silently lost."""
        unit = parse_unit("litclock-splash.service")
        assert unit.get("Service", "User") == "pi"


# ── Install targets ──────────────────────────────────────────────────


class TestInstallTargets:
    @pytest.mark.parametrize(
        "unit_file,expected_target",
        [
            ("litclock-splash.service", "multi-user.target"),
            ("litclock-firstboot.service", "multi-user.target"),
            ("litclock-shutdown.service", "multi-user.target"),
            ("litclock.timer", "timers.target"),
            ("wifi-watchdog.timer", "timers.target"),
        ],
    )
    def test_install_wanted_by(self, unit_file, expected_target):
        unit = parse_unit(unit_file)
        wanted_by = unit.get("Install", "WantedBy", fallback="")
        assert expected_target in wanted_by


# ── All units reference valid paths ─────────────────────────────────


class TestExecPaths:
    """Verify that ExecStart scripts exist in the repo."""

    REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")

    @pytest.mark.parametrize(
        "unit_file",
        [
            "litclock-splash.service",
            "litclock-firstboot.service",
            "litclock.service",
        ],
    )
    def test_exec_start_script_exists(self, unit_file):
        unit = parse_unit(unit_file)
        exec_start = unit.get("Service", "ExecStart", fallback="")
        # ExecStart paths are absolute (/home/pi/litclock/...), map to repo
        script = exec_start.replace("/home/pi/litclock/", "")
        script_path = os.path.join(self.REPO_ROOT, script)
        assert os.path.exists(script_path), f"{script} not found in repo"


# ── #241 LKG writer regression rules ─────────────────────────────────


class TestLkgWriterUnitShape:
    """Issue #241 — the previous unit shape (Requisite= + WantedBy= +
    Type=simple) caused the writer to fail-start at boot. Pin the new
    design so a refactor can't silently bring back any of those bugs."""

    def test_litclock_lkg_service_no_requisite(self):
        unit = parse_unit("litclock-lkg.service")
        requisite = unit.get("Unit", "Requisite", fallback="")
        assert requisite == "", (
            "Requisite=litclock.service was the cause of the writer's parallel-start failure. Do not reintroduce."
        )

    def test_litclock_lkg_service_has_no_install_section(self):
        """The new service is driven by litclock-lkg.timer; it has no
        [Install] section so `systemctl enable litclock-lkg.service` would
        be an error. The timer is the user-facing enable surface."""
        unit = parse_unit("litclock-lkg.service")
        assert not unit.has_section("Install"), (
            "litclock-lkg.service must have NO [Install] — the timer is the enable surface (#241)"
        )

    def test_litclock_lkg_service_is_oneshot(self):
        """Polling design: the script is a fast oneshot driven by the timer.
        Type=simple was wrong because there's no long-running process."""
        unit = parse_unit("litclock-lkg.service")
        assert unit.get("Service", "Type") == "oneshot"

    def test_litclock_lkg_timer_exists_and_drives_service(self):
        unit = parse_unit("litclock-lkg.timer")
        assert unit.get("Timer", "Unit") == "litclock-lkg.service"

    def test_litclock_lkg_timer_cadence(self):
        """OnBootSec=10min gives the heartbeat a few render cycles to
        materialize before the first poll on a fresh boot.
        OnActiveSec=10min covers the mid-uptime install/upgrade case —
        without it, a freshly-enabled timer would sit dormant until the
        next reboot because OnBootSec is already in the past.
        OnUnitActiveSec=5min is the polling cadence after the first fire."""
        unit = parse_unit("litclock-lkg.timer")
        assert unit.get("Timer", "OnBootSec") == "10min"
        assert unit.get("Timer", "OnActiveSec") == "10min", (
            "OnActiveSec=10min is required so timers enabled mid-uptime fire — "
            "OnBootSec alone leaves the LKG writer dormant until reboot (caught in /review)"
        )
        assert unit.get("Timer", "OnUnitActiveSec") == "5min"

    def test_litclock_lkg_timer_wanted_by_timers_target(self):
        unit = parse_unit("litclock-lkg.timer")
        wanted = unit.get("Install", "WantedBy", fallback="")
        assert "timers.target" in wanted

    def test_tmpfiles_d_creates_run_litclock(self):
        """The heartbeat lives at /run/litclock/heartbeat, so the directory
        must be created on every boot via tmpfiles.d (tmpfs is reset)."""
        path = os.path.join(SYSTEMD_DIR, "tmpfiles.d", "litclock.conf")
        assert os.path.exists(path), "systemd/tmpfiles.d/litclock.conf must exist"
        body = open(path).read()
        # Find the actual directive line (skip comments / blanks).
        directive = next(
            (line for line in body.splitlines() if line.strip() and not line.strip().startswith("#")),
            "",
        )
        assert directive.startswith("d "), f"first directive must be `d /run/litclock ...`; got {directive!r}"
        assert "/run/litclock" in directive
        assert " pi pi " in directive, "ownership must be pi:pi so the clock can write the heartbeat"


# ── #245 M1 Control PWA — pin the unit shape ────────────────────────


class TestControlServiceUnitShape:
    """Anti-regression on the litclock-control.service shape locked by
    PLAN-LitClock-Control-PWA.md A2:

    - Type=simple, always-on (a server, not a oneshot).
    - User=pi (the rest of the project runs as pi; matching avoids env-var
      drift and keeps file ownership consistent on env.sh writes).
    - After=litclock-firstboot.service (setup_server owns the device during
      provisioning until .setup-complete is written). Post-#343 the two are on
      DIFFERENT ports (control_server 80, setup_server 8443), so this is a
      phase/state ordering, not a bind-clash avoidance.
    - ConditionPathExists=/etc/litclock/.setup-complete (the only signal
      that firstboot is truly done; without it the control surface could come
      up mid-provisioning, before setup + IP-geo have run).
    - Restart=on-failure (single-tenant device should self-heal on Python
      crash; manual intervention is an exception, not the rule).
    """

    def test_unit_file_exists(self):
        path = os.path.join(SYSTEMD_DIR, "litclock-control.service")
        assert os.path.exists(path), "systemd/litclock-control.service must exist"

    def test_type_is_simple(self):
        unit = parse_unit("litclock-control.service")
        assert unit.get("Service", "Type") == "simple"

    def test_runs_as_pi(self):
        unit = parse_unit("litclock-control.service")
        assert unit.get("Service", "User") == "pi"

    def test_starts_after_firstboot(self):
        unit = parse_unit("litclock-control.service")
        after = unit.get("Unit", "After", fallback="")
        assert "litclock-firstboot.service" in after, (
            "control_server must order After= firstboot — setup owns the device during provisioning (PLAN A2)"
        )

    def test_threads_env_var_set_to_8(self):
        """#416 / eng-review E1=B bumped waitress threads from default 4 to 8.

        The cap of 6 SSE connections (OV-2=A) plus the existing /api/* poll
        cadence means the prior 4-thread budget could starve under household
        load (4 family members opening the live drawer). 8 leaves 2 free
        threads for normal requests in the worst case. A future merge that
        reverted this would silently reintroduce the starvation; this test
        is the canary.
        """
        unit = parse_unit("litclock-control.service")
        env = unit.get("Service", "Environment", fallback="")
        assert "LITCLOCK_CONTROL_THREADS=8" in env, (
            "plan-eng-review E1=B bumped waitress threads to 8 so SSE + /api/* "
            "polls coexist; reverting reintroduces thread starvation under "
            "household load (6 SSE cap + concurrent polls)."
        )
        # All three values share one Environment= directive — configparser
        # rejects duplicate keys, so a second Environment= line breaks the
        # unit-shape tests AND systemd parses the second line as overriding
        # the first. Lock the single-line shape too.
        assert "LITCLOCK_DIR=" in env
        assert "LITCLOCK_ENV_FILE=" in env

    def test_gates_on_setup_complete(self):
        unit = parse_unit("litclock-control.service")
        condition = unit.get("Unit", "ConditionPathExists", fallback="")
        assert condition == "/etc/litclock/.setup-complete", (
            "ConditionPathExists is the only thing preventing port-collision "
            "with setup_server during provisioning — must point at .setup-complete (PLAN A2)"
        )

    def test_restarts_on_failure(self):
        unit = parse_unit("litclock-control.service")
        assert unit.get("Service", "Restart") == "on-failure"

    def test_invokes_app_module_via_venv(self):
        unit = parse_unit("litclock-control.service")
        exec_start = unit.get("Service", "ExecStart", fallback="")
        # Must use the venv interpreter so transitive deps (Flask, waitress,
        # jinja2) resolve. Bare `python3` would hit system Python and miss them.
        assert "/home/pi/litclock/venv/bin/python3" in exec_start
        assert "src/control_server/app.py" in exec_start

    def test_install_section_targets_multi_user(self):
        unit = parse_unit("litclock-control.service")
        wanted = unit.get("Install", "WantedBy", fallback="")
        assert "multi-user.target" in wanted

    def test_hardening_directives_present(self):
        """litclock-control.service runs with NO sandboxing directives
        because every one of them implicitly enables NoNewPrivileges
        when the service runs as a non-root User= (per `man
        systemd.exec`). NNP blocks sudo (setuid root), which breaks
        M4's destructive system actions. We verified this on Pi
        hardware three times before landing on this configuration.

        Long-term plan: migrate to polkit + D-Bus
        (org.freedesktop.systemd1.Manager.Reboot/PowerOff) to drop the
        sudo dependency, then sandboxing can come back.

        Defense-in-depth note: 010_pi-nopasswd already grants pi
        NOPASSWD:ALL system-wide, so service-level sandboxing was
        protecting very little anyway.
        """
        unit = parse_unit("litclock-control.service")
        assert unit.get("Service", "NoNewPrivileges") == "no", "NoNewPrivileges must be EXPLICITLY 'no'."
        forbidden_under_user_pi = (
            "PrivateTmp",
            "ProtectSystem",
            "ProtectHome",
            "ReadWritePaths",
            "RestrictAddressFamilies",
            "LockPersonality",
            "MemoryDenyWriteExecute",
            "SystemCallFilter",
            "PrivateUsers",
            "RestrictNamespaces",
            "ProtectKernelTunables",
            "ProtectKernelModules",
            "ProtectKernelLogs",
            "ProtectClock",
            "RestrictSUIDSGID",
            "RestrictRealtime",
        )
        for directive in forbidden_under_user_pi:
            assert unit.get("Service", directive, fallback=None) is None, (
                f"{directive} must NOT be set on litclock-control.service "
                f"— it implicitly enables NoNewPrivileges under User=pi, "
                f"which breaks sudo elevation. See unit comment for the "
                f"long-term polkit+D-Bus plan."
            )

    def test_timeout_stop_capped(self):
        """SIGTERM closes the listening socket and the accept-loop exits.
        TimeoutStopSec caps how long systemd waits for graceful stop before
        SIGKILL — without it, `systemctl restart` from update.sh Phase 7
        could hang for the default 90s."""
        unit = parse_unit("litclock-control.service")
        timeout = unit.get("Service", "TimeoutStopSec", fallback="")
        # Either a number (seconds) or "10s" / "30s" form. Accept anything
        # that parses as a small int.
        digits = "".join(c for c in timeout if c.isdigit())
        assert digits, f"TimeoutStopSec must be set; got {timeout!r}"
        assert 1 <= int(digits) <= 60, "graceful-stop window should be tight on a single-tenant device"


class TestHandoffUnits:
    """EPIC #383 PR2 (#388) — handoff gate on litclock.service + the
    last-resort fallback completer units."""

    def _raw(self, filename):
        with open(os.path.join(SYSTEMD_DIR, filename)) as f:
            return f.read()

    def test_litclock_service_gated_on_handoff_complete(self):
        """The clock must hold quotes until the post-WiFi handoff completes
        (control_server writes /etc/litclock/.handoff-complete). update.sh's
        Option-A migration touches the marker for already-provisioned Pis so
        quotes never stop on upgrade."""
        unit = parse_unit("litclock.service")
        assert unit.get("Unit", "ConditionPathExists", fallback="") == "/etc/litclock/.handoff-complete"

    def test_fallback_units_exist(self):
        for name in ("litclock-handoff-fallback.service", "litclock-handoff-fallback.timer"):
            assert os.path.exists(os.path.join(SYSTEMD_DIR, name)), f"{name} missing"

    def test_fallback_service_gated_to_handoff_window(self):
        """Only fires when setup finished AND handoff hasn't (both conditions),
        so it's a no-op once the handoff completes."""
        raw = self._raw("litclock-handoff-fallback.service")
        assert "ConditionPathExists=/etc/litclock/.setup-complete" in raw
        assert "ConditionPathExists=!/etc/litclock/.handoff-complete" in raw
        assert "/home/pi/litclock/scripts/litclock-handoff-fallback.sh" in raw

    def test_fallback_timer_installs_and_fires_after_boot(self):
        timer = parse_unit("litclock-handoff-fallback.timer")
        assert timer.get("Timer", "Unit", fallback="") == "litclock-handoff-fallback.service"
        # Both boot-relative and active-relative triggers (mid-uptime enable
        # path via update.sh, same as litclock-lkg.timer).
        assert timer.get("Timer", "OnBootSec", fallback="")
        assert timer.get("Timer", "OnActiveSec", fallback="")
        assert timer.get("Install", "WantedBy", fallback="") == "timers.target"


class TestReresolveLocationUnit:
    """#337 A2/A8: the new on-boot reresolve oneshot. Pins the static shape
    properties — handoff-complete gate (A2), best-effort ordering (NO
    Before=litclock.service per A8), correct ExecStart path (fixed by
    /review — `python -m location_resolver` would ModuleNotFoundError on the
    Pi because src/ isn't on sys.path for `-m`), bounded timeout, runs as pi.

    If any of these regress, the oneshot either fails to start (silent
    silent-no-op of the whole feature) or breaks boot ordering."""

    UNIT = "litclock-reresolve-location.service"

    def _raw(self):
        with open(os.path.join(SYSTEMD_DIR, self.UNIT)) as f:
            return f.read()

    def test_unit_file_exists(self):
        assert os.path.exists(os.path.join(SYSTEMD_DIR, self.UNIT)), f"{self.UNIT} missing"

    def test_is_oneshot(self):
        unit = parse_unit(self.UNIT)
        assert unit.get("Service", "Type", fallback="") == "oneshot"

    def test_runs_as_pi(self):
        unit = parse_unit(self.UNIT)
        assert unit.get("Service", "User", fallback="") == "pi"

    def test_handoff_complete_condition(self):
        """A2: prevents the oneshot from racing first-boot's own IP-geo on a
        fresh-flash boot where setup_server is still resolving."""
        unit = parse_unit(self.UNIT)
        assert unit.get("Unit", "ConditionPathExists", fallback="") == "/etc/litclock/.handoff-complete", (
            "#337 A2 regression: ConditionPathExists must gate on /etc/litclock/.handoff-complete"
        )

    def test_after_network_online_ordering(self):
        """The oneshot must run after the network is up — otherwise IP-geo
        always fails."""
        raw = self._raw()
        assert "NetworkManager-wait-online" in raw or "network-online.target" in raw

    def test_does_not_block_first_quote_tick(self):
        """#337 A8 (locked, deliberately rejected during /review): NO
        `Before=litclock.service`. Best-effort boot. Blocking the first
        quote on a 1-33s IP-geo would be a real boot-delay regression.

        Strip `#` comments before checking — the unit file legitimately
        mentions the directive in prose explaining WHY it's absent."""
        raw = self._raw()
        non_comment_lines = [ln for ln in raw.splitlines() if not ln.lstrip().startswith("#")]
        body = "\n".join(non_comment_lines)
        assert "Before=litclock.service" not in body, (
            "#337 A8: must NOT block first quote tick on the IP-geo. "
            "Best-effort by design (first tick may show stale data, second is fresh)."
        )

    def test_execstart_uses_script_path_not_dash_m(self):
        """#337 /review (Codex maintainability + my-own-trace): `python -m
        location_resolver` searches cwd + venv site-packages, NOT src/. On
        the Pi this would ModuleNotFoundError at first boot. The script-path
        form makes Python add the script's dir to sys.path, matching every
        other Python ExecStart in this project."""
        unit = parse_unit(self.UNIT)
        exec_start = unit.get("Service", "ExecStart", fallback="")
        assert "python -m location_resolver" not in exec_start, (
            "#337 /review P1 regression: -m form would ModuleNotFoundError on Pi. "
            "Use the script path: /home/pi/litclock/src/location_resolver.py"
        )
        assert exec_start.endswith("src/location_resolver.py"), (
            f"ExecStart should run the script directly; got: {exec_start!r}"
        )

    def test_env_file_variable_set(self):
        """LITCLOCK_ENV_FILE tells location_resolver.main() which file to
        read/write. Without it, the oneshot would fall through to
        setup_server.ENV_FILE which is None outside first-boot."""
        raw = self._raw()
        assert "LITCLOCK_ENV_FILE=" in raw

    def test_bounded_timeout(self):
        """TimeoutStartSec must be set so a wedged IP-geo doesn't tie up
        systemd indefinitely."""
        unit = parse_unit(self.UNIT)
        timeout = unit.get("Service", "TimeoutStartSec", fallback="")
        digits = "".join(ch for ch in timeout if ch.isdigit())
        assert digits, f"TimeoutStartSec must be set; got {timeout!r}"
        assert 1 <= int(digits) <= 120, "boot resolver budget should be tight (under 2 min)"

    def test_wanted_by_multi_user_target(self):
        unit = parse_unit(self.UNIT)
        assert unit.get("Install", "WantedBy", fallback="") == "multi-user.target"
