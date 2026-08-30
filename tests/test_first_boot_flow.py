"""Tests for first-boot flow validation (litclock-dev#111).

Validates the complete first-boot sequence for image-based deployment:
- WiFi provisioning with zero saved networks
- Boot ordering (splash → firstboot → timer)
- NTP sync after WiFi is provisioned
- Timezone setting
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

import setup_server

# litclock-dev#670: the shipped password length is a constant now, so a fixture
# that hardcodes 8 stops testing "the shipped path" the moment it changes.
# Sliced from the legible alphabet, deterministic, tracks the real length.
import wifi_provision as _wifi_provision  # noqa: E402

SHIPPED_PW = "Ab3xYz9qKmNpQrTu"[: _wifi_provision.HOTSPOT_PASSWORD_LENGTH]

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
FIRST_BOOT_SH = os.path.join(REPO_ROOT, "scripts", "first-boot.sh")
BOOT_SPLASH_SH = os.path.join(REPO_ROOT, "scripts", "boot-splash.sh")


def _en_catalog():
    """The English catalog — first-boot's panel copy lives here since the
    litclock-dev#532 bulk extraction; copy-property tests must assert on
    these VALUES (plus the call-site KEY) or they observe nothing."""
    import json

    with open(os.path.join(REPO_ROOT, "languages", "en", "strings.json")) as f:
        return json.load(f)


# ── WiFi provisioning with zero saved networks ──────────────────────


class TestWiFiProvisioning:
    """Verify first-boot.sh handles missing WiFi correctly."""

    @staticmethod
    def _read_first_boot():
        with open(FIRST_BOOT_SH) as f:
            return f.read()

    def test_checks_wifi_before_hotspot(self):
        """first-boot.sh must check WiFi status before creating hotspot."""
        content = self._read_first_boot()
        wifi_check = content.find("is_wifi_connected")
        hotspot_call = content.find("create_hotspot")
        assert wifi_check != -1, "is_wifi_connected not found in first-boot.sh"
        assert hotspot_call != -1, "create_hotspot not found in first-boot.sh"
        assert wifi_check < hotspot_call, "WiFi check must happen before hotspot creation"

    def test_hotspot_provisioning_retries(self):
        """Hotspot creation should retry several times on failure."""
        content = self._read_first_boot()
        assert "HOTSPOT_MAX_RETRIES" in content
        match = re.search(r"HOTSPOT_MAX_RETRIES=(\d+)", content)
        assert match, "HOTSPOT_MAX_RETRIES not defined"
        retries = int(match.group(1))
        # Pi Zero 2W brcmfmac can get into stuck states on rapid reboot;
        # we need enough attempts to cover driver-reload recovery.
        assert retries >= 5, "Should retry at least five times"

    def test_retry_escalates_recovery(self):
        """Retry loop must escalate recovery (NM restart, driver reload)."""
        content = self._read_first_boot()
        # NM restart between attempts
        assert "systemctl restart NetworkManager" in content
        # brcmfmac reload as last-ditch recovery
        assert "rmmod brcmfmac" in content
        assert "modprobe brcmfmac" in content

    def test_displays_power_cycle_on_hotspot_failure(self):
        """If hotspot creation fails after all retries, tell the user to power-cycle.

        A software reboot does NOT power-cycle the BCM43436 SDIO chip on Pi
        Zero 2W — only pulling power does. Telling the user to "restart" is
        actively wrong guidance.
        """
        content = self._read_first_boot()
        assert "display_message firstboot.splash.setup_failed" in content
        assert _en_catalog()["firstboot.splash.setup_failed.message"] == "Unplug power for 10 seconds"

    def test_uses_nmcli_hotspot(self):
        """WiFi provisioning should use nmcli via wifi_provision.py, not wifi-connect."""
        content = self._read_first_boot()
        assert "create_hotspot" in content
        assert "wifi-connect" not in content

    def test_captive_portal_via_nm_dnsmasq(self):
        """Captive portal DNS is handled by NM's dnsmasq, not a separate server."""
        content = self._read_first_boot()
        # NM starts dnsmasq in shared mode with --conf-dir=dnsmasq-shared.d/
        # which reads the address=/#/ config written by wifi_provision.py.
        # A separate DNS server would conflict with dnsmasq on port 53.
        assert "start_dns_server" not in content
        assert "start_captive_dns" not in content


# ── NTP sync after WiFi ──────────────────────────────────────────────


class TestNTPSync:
    """Verify NTP sync happens after WiFi and before setup server."""

    @staticmethod
    def _read_first_boot():
        with open(FIRST_BOOT_SH) as f:
            return f.read()

    def test_ntp_enabled_after_wifi(self):
        """timedatectl set-ntp must be called after WiFi is established."""
        content = self._read_first_boot()
        wifi_section = content.find("is_wifi_connected")
        ntp_enable = content.find("timedatectl set-ntp true")
        assert ntp_enable != -1, "timedatectl set-ntp true not found"
        assert wifi_section < ntp_enable, "NTP must be enabled after WiFi check"

    def test_ntp_sync_wait_loop(self):
        """Should poll for NTPSynchronized=yes with a bounded loop."""
        content = self._read_first_boot()
        assert "NTPSynchronized=yes" in content

    def test_ntp_before_setup_server(self):
        """NTP sync should happen before the setup server starts in main()."""
        content = self._read_first_boot()
        # Look within main() body to avoid matching function definitions
        main_start = content.find("\nmain()")
        main_body = content[main_start:]
        ntp_section = main_body.find("timedatectl set-ntp")
        server_start = main_body.find("start_setup_server")
        assert ntp_section != -1, "timedatectl set-ntp not found in main()"
        assert server_start != -1, "start_setup_server not found in main()"
        assert ntp_section < server_start, "NTP sync must happen before setup server starts"

    def test_provisioning_passes_hotspot_credentials(self):
        """Setup server launch in provisioning mode must pass --hotspot-ssid and --hotspot-password."""
        content = self._read_first_boot()
        # Check both the initial launch and the restart path
        assert "--hotspot-ssid" in content, "Missing --hotspot-ssid in setup server launch"
        assert "--hotspot-password" in content, "Missing --hotspot-password in setup server launch"
        # Verify both launch and restart paths pass credentials
        assert content.count("--hotspot-ssid") >= 2, (
            "Both start_setup_server_provisioning and wait_for_setup restart must pass --hotspot-ssid"
        )
        assert content.count("--hotspot-password") >= 2, (
            "Both start_setup_server_provisioning and wait_for_setup restart must pass --hotspot-password"
        )


class TestTimezoneInFirstBoot:
    """Verify timezone is set during the setup flow."""

    def test_setup_server_sets_timezone(self, mocker):
        """The setup POST handler calls set_system_timezone.

        litclock-dev#414 item #5: set_system_timezone lives in `geocoding` since the
        location_resolver extraction — setup_server.set_system_timezone is
        a re-export alias, and the subprocess call happens in geocoding."""
        mocker.patch(
            "geocoding.subprocess.run",
            side_effect=[
                # list-timezones
                subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout="America/Chicago\nAmerica/New_York\n",
                ),
                # set-timezone
                subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            ],
        )
        ok, err = setup_server.set_system_timezone("America/Chicago")
        assert ok is True
        assert err is None

    def test_timezone_validation_rejects_bad_input(self, mocker):
        """Timezone must be validated against timedatectl list-timezones."""
        mocker.patch(
            "geocoding.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="America/New_York\nEurope/London\n",
            ),
        )
        ok, err = setup_server.set_system_timezone("../../../etc/passwd")
        assert ok is False


# ── Boot sequence integrity ──────────────────────────────────────────


class TestBootSequenceIntegrity:
    """End-to-end checks on the boot sequence scripts."""

    def test_first_boot_checks_setup_complete_first(self):
        """The very first action in main() should be checking .setup-complete."""
        with open(FIRST_BOOT_SH) as f:
            content = f.read()

        # Find the main() function body
        main_start = content.find("main()")
        assert main_start != -1
        main_body = content[main_start:]

        # check_setup_complete should appear before any WiFi/NTP/server logic
        check = main_body.find("check_setup_complete")
        wifi = main_body.find("is_wifi_connected")
        assert check < wifi, "setup-complete check must be the first action in main()"

    def test_first_boot_creates_default_env(self):
        """If env.sh doesn't exist, first-boot.sh must create it with defaults."""
        with open(FIRST_BOOT_SH) as f:
            content = f.read()

        assert "OPENWEATHERMAP_APIKEY=" in content
        assert "WEATHER_LATITUDE=" in content
        assert "WEATHER_LONGITUDE=" in content
        assert "WEATHER_UNITS=imperial" in content

    def test_first_boot_routes_default_env_through_atomic_writer(self):
        """litclock-dev#274 follow-up: first-boot.sh must use the shared sidecar-flock
        writer (`atomic_write_env_sh` from `scripts/lib/state.sh`) for the
        default-env-creation path, not a bare `cat > "$ENV_FILE"` heredoc.

        Pin two invariants:
          1. `scripts/lib/state.sh` is sourced near the top of the script.
          2. The default-env-creation branch invokes `atomic_write_env_sh`
             with `$ENV_FILE` as the destination.

        Without these, first-boot.sh would be the only env.sh writer not
        respecting the cross-writer interlock with the Python PWA writer
        in `src/config.py`, and a power loss mid-heredoc would leave a
        half-truncated env.sh.
        """
        with open(FIRST_BOOT_SH) as f:
            content = f.read()
        assert ". " in content and "lib/state.sh" in content, (
            "first-boot.sh must source scripts/lib/state.sh so "
            "atomic_write_env_sh is available for the default-env path"
        )
        assert 'atomic_write_env_sh "$ENV_FILE"' in content, (
            "first-boot.sh must invoke atomic_write_env_sh with $ENV_FILE — "
            "ensures the default-env-creation path goes through the same "
            "sidecar-flock writer as update.sh / reset-setup.sh / "
            "prepare-for-cloning.sh (litclock-dev#274 cross-writer interlock)"
        )

    def test_first_boot_uses_no_block_for_timer(self):
        """Starting litclock.timer from within a service MUST use --no-block
        to avoid systemd deadlock."""
        with open(FIRST_BOOT_SH) as f:
            content = f.read()
        assert "--no-block" in content

    def test_first_boot_starts_litclock_control(self):
        """litclock-dev#245 M5 hardware-QA fix: after firstboot writes .setup-complete back
        on a Reset-WiFi recovery, litclock-control.service must be explicitly
        kicked. The unit's ConditionPathExists=/etc/litclock/.setup-complete
        is evaluated at job-start time only — systemd does NOT re-fire a
        unit when its condition becomes true mid-session.
        """
        with open(FIRST_BOOT_SH) as f:
            content = f.read()
        # The actual kick must be the precise --no-block invocation; comment-
        # only mentions don't count. Pin the line shape so a future edit that
        # drops --no-block (and re-introduces the systemd-from-inside-a-service
        # deadlock M3 already fixed once) fails CI.
        assert "systemctl start --no-block litclock-control.service" in content, (
            "first-boot.sh must invoke `systemctl start --no-block "
            "litclock-control.service` so the Reset-WiFi recovery path brings "
            "the PWA server back online; --no-block avoids the documented "
            "systemd deadlock when starting one unit from inside another."
        )

    def test_first_boot_disables_itself_on_success(self):
        """After successful setup, firstboot should disable itself."""
        with open(FIRST_BOOT_SH) as f:
            content = f.read()
        assert "disable_first_boot" in content
        assert "systemctl disable litclock-firstboot" in content

    def test_first_boot_marks_setup_complete(self):
        """After successful setup, the .setup-complete flag must be created."""
        with open(FIRST_BOOT_SH) as f:
            content = f.read()
        assert "mark_setup_complete" in content
        assert ".setup-complete" in content

    def test_setup_server_signals_completion(self):
        """setup_server.py must write the signal file that first-boot.sh waits for."""
        import inspect

        source = inspect.getsource(setup_server.signal_completion)
        assert "SIGNAL_FILE" in source or "signal_file" in source.lower()

    def test_boot_splash_triggers_clock_if_setup_complete(self):
        """The on-boot clock render moved from boot-splash.sh into an
        ExecStartPost on litclock-splash.service (issue litclock-dev#269). Verify the
        unit file still triggers litclock.service when .setup-complete exists,
        and that it uses --no-block (deadlock prevention) and the `+` prefix
        (run as root despite User=pi)."""
        unit_path = os.path.join(REPO_ROOT, "systemd", "litclock-splash.service")
        with open(unit_path) as f:
            unit = f.read()
        assert "ExecStartPost=" in unit, "Splash unit must trigger clock via ExecStartPost"
        post_lines = [ln for ln in unit.splitlines() if ln.startswith("ExecStartPost=")]
        assert post_lines, "ExecStartPost line missing"
        post = post_lines[0]
        assert ".setup-complete" in post, "Must guard on /etc/litclock/.setup-complete"
        assert "litclock.service" in post, "Must trigger litclock.service (not runtheclock.sh)"
        assert "--no-block" in post, "Must use --no-block to avoid systemctl-from-service deadlock"
        assert post.startswith("ExecStartPost=+"), "Must use `+` prefix to run as root"

    def test_first_boot_consumes_gift_mode_marker_before_setup_complete(self):
        """Gift-mode marker (litclock-dev#189) must be removed in the first-boot success
        path so subsequent shutdowns paint the normal 'Powered Off' splash.

        litclock-dev#316 /review CRITICAL ordering fix: the rm happens BEFORE
        mark_setup_complete (was after). The previous order had a window
        where power loss between mark_setup_complete and the rm would
        leave .welcome-mode stranded with .setup-complete already present.
        On next boot, first-boot.sh short-circuits and the marker NEVER
        gets cleared — every subsequent shutdown paints the gift welcome
        forever, no PWA recovery path. New order means worst-case failure
        is 'first-boot re-runs setup on next boot' (acceptable)."""
        with open(FIRST_BOOT_SH) as f:
            content = f.read()
        assert ".welcome-mode" in content, "first-boot.sh must consume the welcome-mode marker"
        # The rm of markers must precede the `mark_setup_complete` CALL in
        # the success branch. `mark_setup_complete` appears earlier as a
        # function definition (~line 278), so find its CALL site after the
        # marker rm command.
        rm_idx = content.find("rm -f /etc/litclock/.welcome-mode")
        assert rm_idx > 0, "marker rm command not found"
        # The call site is the next `mark_setup_complete` occurrence after
        # the rm command (not the function definition above it).
        call_idx = content.find("mark_setup_complete", rm_idx)
        assert call_idx > rm_idx, (
            "marker rm must precede the mark_setup_complete CALL — "
            "otherwise a power-loss race between mark_setup_complete and "
            "rm permanently strands the gift welcome marker (litclock-dev#316 /review)"
        )

    def test_first_boot_consumes_welcome_message_before_setup_complete(self):
        """litclock-dev#280: the optional personalized welcome message (set via the PWA
        Prepare-for-Gifting flow) must be cleaned up alongside the
        .welcome-mode marker. litclock-dev#316 /review: same ordering invariant as
        .welcome-mode — rm before mark_setup_complete to defeat the
        power-loss race."""
        with open(FIRST_BOOT_SH) as f:
            content = f.read()
        assert ".welcome-message" in content, (
            "first-boot.sh must clean up the optional .welcome-message file "
            "(litclock-dev#280)")
        rm_idx = content.find(".welcome-message")
        # Find the rm command (not a comment).
        rm_idx = content.find("rm -f /etc/litclock/.welcome-mode")
        call_idx = content.find("mark_setup_complete", rm_idx)
        assert call_idx > rm_idx, (
            ".welcome-message rm must precede mark_setup_complete CALL — "
            "same power-loss race as .welcome-mode (litclock-dev#316 /review)"
        )


def test_first_boot_default_env_includes_mode_and_ip_country():
    """litclock-dev#337 A3 + /review testing-gap: first-boot.sh's env.sh template (both
    the state.sh-flock path AND the legacy heredoc fallback) must include
    the new MODE + IP_COUNTRY defaults. Without these, a fresh-flash Pi
    would have no MODE/IP_COUNTRY keys at all — pre-S2 migration semantics
    would kick in (which work, but are an unnecessary code path for new
    installs)."""
    from pathlib import Path

    content = (Path(__file__).parent.parent / "scripts/first-boot.sh").read_text()
    # The keys appear in TWO blocks (flock path + heredoc fallback) — count both.
    assert content.count("export WEATHER_LOCATION_MODE=auto") >= 2, (
        "litclock-dev#337 A3: first-boot.sh must include MODE=auto in BOTH the atomic-write "
        "path AND the heredoc-fallback path (so the keys ship regardless of which "
        "writer fires)."
    )
    assert content.count("export WEATHER_IP_COUNTRY=") >= 2, (
        "litclock-dev#337 A3: first-boot.sh must include WEATHER_IP_COUNTRY= in both writer paths"
    )


# ── litclock-dev#529: power off after the Setup-Incomplete timeout ──────────


class TestSetupIncompletePoweroff:
    """litclock-dev#529: after the setup-wait times out, the device must paint
    recovery instructions and power off — not idle forever in a half-provisioned
    state.

    Authored on the public repo (#22) and back-ported here unchanged apart
    from issue-ref qualification (litclock-dev#657). Until this landed, a dev
    image did NOT power off on this path while the shipped public image did, so
    any bench observation of shutdown behaviour was being made against different
    code than ships — the misattribution failure litclock-dev#648 was filed
    about, arriving by a different route."""

    @pytest.fixture(scope="class")
    def content(self):
        with open(FIRST_BOOT_SH) as f:
            return f.read()

    def _timeout_block(self, content):
        idx = content.find("display_message_strict firstboot.splash.setup_incomplete")
        assert idx != -1, "Setup Incomplete branch missing"
        # End at the function's closing brace on its own line — a bare
        # find("}") would truncate at the first ${VAR:-default} expansion.
        # The end index is ASSERTED: a -1 from find would silently make this
        # `content[idx:-1]`, i.e. the whole rest of the file, and every
        # presence-style assertion below would then be satisfied by any
        # occurrence anywhere downstream (/review).
        end = content.find("\n}", idx)
        assert end != -1, "could not find the end of the enclosing function"
        block = content[idx:end]
        assert len(block) < 4000, f"the Setup-Incomplete block grew to {len(block)} chars — check the span"
        return block

    def _commands(self, content):
        """The block with comment-only lines removed.

        Added on the back-port (litclock-dev#657), not present in #22:
        the rationale comments name `sudo systemctl poweroff` verbatim, so a
        mutation probe showed that DELETING the actual command left
        test_timeout_path_powers_off green. An assertion a comment can satisfy
        is not an assertion about behaviour.
        """
        return "\n".join(ln for ln in self._timeout_block(content).splitlines() if not ln.lstrip().startswith("#"))

    def test_timeout_path_powers_off_unconditionally(self, content):
        """`sudo systemctl poweroff` — the sudo-systemctl form used elsewhere in
        this script + the scoped 020 sudoers allowlist (/review).

        The command must stand ALONE on its line. A substring assertion is
        satisfied by `sudo touch … && sudo systemctl poweroff`, by
        `if [[ "${LITCLOCK_NO_POWEROFF:-0}" != "1" ]]; then …`, and by
        `_CMD="sudo systemctl poweroff"` — all three survived it, and the first
        re-creates the litclock-dev#529 bug this arm fixes: a failed marker
        touch would leave the device stranded ON, half-provisioned, holding the
        hotspot. The code comment states that invariant ("the poweroff is
        deliberately not gated on it"); this is what holds it."""
        cmds = self._commands(content)
        assert re.search(r"(?m)^\s*sudo systemctl poweroff( \|\|.*)?$", cmds), (
            "`sudo systemctl poweroff` must be its own statement, not chained, gated or assigned"
        )

    def test_the_marker_touch_is_never_fatal(self, content):
        """A device stranded ON is worse than a repainted splash, so the touch
        must not be able to abort the arm before the poweroff."""
        cmds = self._commands(content)
        assert re.search(r"sudo touch /run/litclock-splash-suppress[^\n]*\|\| true", cmds), (
            "the suppress-marker touch must end in `|| true`"
        )

    def test_no_grace_sleep_between_paint_and_poweroff(self, content):
        """Owner decision on litclock-dev#529: NO delay between painting the
        recovery copy and powering off. The copy invites the user to pull power,
        so every running second after the paint is a window for an unclean power
        cut (SD-corruption class). The 30-minute setup timeout was the grace
        period."""
        # Over the COMMANDS, with no line anchor. The anchor was there to avoid
        # matching the word in a comment, but _commands() already strips those,
        # so it was buying nothing and costing coverage: `[[ -n "$X" ]] &&
        # sleep 30` and `true && sleep 45` both survived it. `read -t` is a
        # sleep by another name (/review).
        cmds = self._commands(content)
        assert not re.search(r"\b(sleep|read\s+[^\n]*-t)\b", cmds), (
            "no delay of any kind allowed between the paint and the poweroff"
        )
        assert "FIRSTBOOT_POWEROFF_GRACE" not in cmds

    def test_splash_suppressed_so_message_persists(self, content):
        """The bistable e-ink keeps 'Setup Incomplete' visible while off — but
        only if litclock-shutdown.service's ExecStop is told not to repaint. The
        root-only suppress marker must be touched (via sudo) BEFORE the
        poweroff."""
        block = self._commands(content)
        marker_idx = block.find("sudo touch /run/litclock-splash-suppress")
        off_idx = block.find("sudo systemctl poweroff")
        assert marker_idx != -1, "suppress marker touch missing"
        # Asserted so this test can only ever fail for an ORDERING fault. With
        # off_idx left unchecked, deleting the poweroff made `marker < -1`
        # false and this test reddened with an ordering message for an entirely
        # different regression (/review).
        assert off_idx != -1, "poweroff missing — see test_timeout_path_powers_off_unconditionally"
        assert marker_idx < off_idx

    def test_no_ssh_copy_on_powered_off_screen(self, content):
        """The device is about to be off — 'SSH in' would be a lie on the
        persisted screen (and gift recipients don't SSH). The recovery copy is
        unplug/replug, which matches what a power-cycle actually does."""
        # The copy is catalog-resolved now: the call site carries the KEY,
        # the words live in the en catalog — assert on the VALUES (all
        # three parts), or a catalog edit could reintroduce SSH unseen.
        assert "display_message_strict firstboot.splash.setup_incomplete" in self._commands(content)
        cat = _en_catalog()
        parts = [
            cat.get("firstboot.splash.setup_incomplete." + p, "") for p in ("title", "message", "submessage")
        ]
        assert any(parts), "setup_incomplete triplet missing from the en catalog"
        copy = " ".join(parts)
        assert "SSH" not in copy, "Setup Incomplete copy must not mention SSH"
        assert "nplug" in copy, "the copy must tell the owner to unplug and plug back in"

    def test_setup_timeout_is_hardcoded_not_env_overridable(self, content):
        """/review: the setup wait is a fixed 1800s. No env-var override in
        shipped code — a stray systemd drop-in setting it to 0 (instant
        poweroff) or a huge value (infinite idle) is a footgun, and the QA it was
        added for is complete."""
        # Assert the PROPERTY, not the spelling. The literal-string form failed
        # a legitimate refactor to a named constant while still missing any env
        # override spelled differently from FIRSTBOOT_SETUP_TIMEOUT (/review).
        call = re.search(r'wait_for_setup "\$SERVER_PID" (\S+)', content)
        assert call, "the setup wait call is missing"
        arg = call.group(1).rstrip(";")
        if arg.startswith('"$') or arg.startswith("$"):
            name = arg.strip('"$\'{}').split(":")[0]
            assign = re.search(rf'(?m)^\s*{re.escape(name)}=(\S+)', content)
            assert assign, f"{name} is used as the timeout but never assigned in this script"
            arg = assign.group(1)
        assert ":-" not in arg and "$" not in arg, (
            f"the setup timeout must not be environment-overridable (got {arg!r})"
        )
        assert arg.strip('"\'') == "1800", f"the setup timeout must be 1800s (got {arg!r})"


class TestIssueBoxAlignment:
    """litclock-dev#589 item 4 + litclock-dev#626: the /etc/issue console box
    border must align with its content lines for EVERY credential pair the
    create_hotspot boundary validation admits, not just the shipped defaults.
    The box is now sized dynamically at runtime, so this test RUNS the real
    update_issue_hotspot under bash (sudo/log/cp stubbed) instead of statically
    re-deriving widths from the script text — a static check re-derived from
    the template is blind to the exact runtime overflow it exists to catch."""

    def _render(self, tmp_path, ssid, password, ip):
        text = open(FIRST_BOOT_SH).read()
        m = re.search(r"^update_issue_hotspot\(\) \{.*?^\}", text, re.S | re.M)
        assert m, "update_issue_hotspot() not found in first-boot.sh"
        # The regex stops at the first column-0 `}` — if the body ever gains
        # one (or the closer is re-indented), the extraction truncates
        # mid-heredoc and bash fails with a misattributed syntax error. The
        # heredoc terminator inside the capture proves we got the whole
        # function (/review).
        assert "ISSUEEOF" in m.group(0), "extraction truncated before the heredoc terminator"
        out = tmp_path / "issue.out"
        harness = (
            "log() { :; }\n"
            "cp() { :; }\n"
            'sudo() { if [[ "$1" == "tee" ]]; then cat > "$OUT"; else :; fi; }\n'
            f"{m.group(0)}\n"
            'update_issue_hotspot "$1" "$2" "$3"\n'
        )
        subprocess.run(
            ["bash", "-c", harness, "issue-box-test", ssid, password, ip],
            check=True,
            env={**os.environ, "OUT": str(out)},
            timeout=10,
        )
        return out.read_text()

    def _assert_box_aligned(self, rendered):
        box_lines = [ln for ln in rendered.splitlines() if ln.strip()[:1] in tuple("╔╠╚║")]
        assert len(box_lines) == 7, f"expected a 7-line box, got {len(box_lines)}: {box_lines}"
        widths = {len(ln) for ln in box_lines}
        assert len(widths) == 1, f"box lines are not equal width: {sorted(widths)} in\n{rendered}"

    def test_default_credentials_box_is_aligned_and_45_wide(self, tmp_path):
        # The shipped path (14-char default SSID, 8-char password, IPv4 URL)
        # must render the historical 45-col interior byte-for-byte.
        rendered = self._render(tmp_path, "LitClock-Setup", SHIPPED_PW, "10.42.0.1")
        self._assert_box_aligned(rendered)
        assert "║  LitClock's WiFi network: LitClock-Setup" in rendered
        border = re.search(r"╔(═+)╗", rendered)
        assert border and len(border.group(1)) == 45

    def test_max_valid_credentials_grow_the_box_without_breaking_it(self, tmp_path):
        # The widest pair validate_hotspot_credentials admits: a 32-char SSID
        # and a 63-char password. The old fixed %-16s/%-15s fields overflowed
        # the right border on exactly these; the box must grow instead —
        # untruncated, because this banner exists so a tester can read the
        # EXACT credentials.
        ssid = "S" * 32
        password = "p" * 63
        rendered = self._render(tmp_path, ssid, password, "10.42.0.1")
        self._assert_box_aligned(rendered)
        assert ssid in rendered
        assert password in rendered

    def test_odd_width_title_centering_stays_aligned(self, tmp_path):
        # A 62-char password makes interior 92 and interior-title 73 (odd), so
        # pad_r = pad_l + 1 — the asymmetric centering branch no other case
        # exercises. A refactor to symmetric padding would misalign the title
        # row only on odd diffs and pass every even-diff test (/review).
        rendered = self._render(tmp_path, "LitClock-Setup", "p" * 62, "10.42.0.1")
        self._assert_box_aligned(rendered)

    def test_first_growth_step_past_the_floor(self, tmp_path):
        # 17-char SSID -> content 44 + 2 = 46, one past the 45 floor: the
        # boundary between "floor holds" and "box grows" (/review).
        rendered = self._render(tmp_path, "S" * 17, SHIPPED_PW, "10.42.0.1")
        self._assert_box_aligned(rendered)
        border = re.search(r"╔(═+)╗", rendered)
        assert border and len(border.group(1)) == 46

    def test_long_ip_grows_the_box_via_the_url_line(self, tmp_path):
        # The old %-30s URL field was the one surface with NO overflow guard
        # (litclock-dev#626 item 3). Growth must also be drivable by the ip
        # argument alone, not just the credential lines.
        rendered = self._render(tmp_path, "LitClock-Setup", SHIPPED_PW, "fe80::1234:5678:9abc:def0")
        self._assert_box_aligned(rendered)
        assert "http://fe80::1234:5678:9abc:def0:8080" in rendered

    def test_backslash_password_is_doubled_for_agetty(self, tmp_path):
        # agetty expands \d, \l, \e... in /etc/issue, so a backslash-bearing
        # credential (printable ASCII — validator admits it) must be written
        # DOUBLED or the console displays a different password than the AP
        # uses, and \e is terminal-escape injection (/review, three passes).
        rendered = self._render(tmp_path, "LitClock-Setup", "pa\\ss\\word1", "10.42.0.1")
        assert "pa\\\\ss\\\\word1" in rendered, "backslashes must be doubled so getty shows the literal"
        self._assert_box_aligned(rendered)


# ───────────────── litclock-dev#647: pre-connected path has no setup page ───


class TestPreConnectedInlineSetup:
    """litclock-dev#647. The pre-connected branch used to serve an input-free
    page behind a self-signed-cert warning and block on a tap; it now resolves
    location inline and completes setup unattended, landing on the handoff
    splash — which carries the liveness signal (PWA QR + Done / 120s fallback)
    the tap used to provide.

    Assertions run against COMMENT-STRIPPED spans: the branch's own rationale
    comment quotes the retired URL, so raw-text greps would be satisfied (or
    violated) by prose.
    """

    SCRIPT = Path(__file__).parent.parent / "scripts" / "first-boot.sh"

    @classmethod
    def _executed(cls, text):
        return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))

    @classmethod
    def _preconnected_branch(cls):
        body = cls.SCRIPT.read_text()
        start = body.index("if is_wifi_connected; then", body.index("# Step 2: Check WiFi"))
        end = body.index("\n    else", start)
        span = body[start:end]
        assert "location_resolver.py" in span, "span is not the pre-connected branch"
        return span

    def test_no_server_no_qr_no_8443_on_the_preconnected_path(self):
        """Scoped to the env.sh-present arm: the missing-env.sh FALLBACK arm
        deliberately starts the legacy server (/review litclock-dev#712 finding 3b) — that
        is its retry mechanism, not a page anyone is sent to (no QR paints)."""
        branch = self._executed(self._preconnected_branch())
        happy_arm = branch[: branch.index("\n        else")]
        for retired in ("start_setup_server", "display_qr", "8443", "SETUP_URL"):
            assert retired not in happy_arm, (
                f"{retired!r} is back on the pre-connected happy path — the whole point of "
                "litclock-dev#647 is that this branch serves no page and waits on no tap"
            )
        # The fallback arm may start the server but must still paint no QR —
        # its page dies immediately on the missing env file; a QR to it would
        # be a dead end.
        fallback_arm = branch[branch.index("\n        else") :]
        for retired in ("display_qr", "8443", "SETUP_URL"):
            assert retired not in fallback_arm

    def test_resolver_runs_inline_with_the_env_file(self):
        branch = self._executed(self._preconnected_branch())
        assert "location_resolver.py" in branch, "the branch no longer resolves location at all"
        assert 'LITCLOCK_ENV_FILE="$ENV_FILE"' in branch, (
            "the resolver reads LITCLOCK_ENV_FILE; without it main() no-ops against a missing file"
        )
        # The MODE=auto gate lives in location_resolver.main() — invoking the
        # module entry (not resolve_location_from_ip directly) is what keeps a
        # PWA-saved Specific location safe across a first-boot re-run (A15).
        assert '"$PYTHON" "$INSTALL_DIR/src/location_resolver.py"' in branch

    def test_inline_completion_short_circuits_the_wait(self):
        body = self.SCRIPT.read_text()
        executed = self._executed(body)
        assert 'SETUP_DONE_INLINE="false"' in executed, "flag no longer initialized before the branch"
        branch = self._executed(self._preconnected_branch())
        assert 'SETUP_DONE_INLINE="true"' in branch, "the pre-connected branch no longer completes inline"
        gate = next(
            ln for ln in executed.splitlines() if "wait_for_setup" in ln and "SERVER_PID" in ln and "if" in ln
        )
        assert '[[ "$SETUP_DONE_INLINE" == "true" ]] ||' in gate, (
            "inline completion must short-circuit wait_for_setup — no server runs on this path, "
            "so waiting on $SERVER_PID would hang or fail"
        )
        # Ordering: init false → set true → gate.
        init_at = executed.index('SETUP_DONE_INLINE="false"')
        set_at = executed.index('SETUP_DONE_INLINE="true"')
        gate_at = executed.index('[[ "$SETUP_DONE_INLINE" == "true" ]] ||')
        assert init_at < set_at < gate_at

    def test_hotspot_branch_still_serves_the_provisioning_page(self):
        """The no-WiFi path is untouched: a recipient with no saved network
        still needs the picker + password form."""
        body = self.SCRIPT.read_text()
        start = body.index("\n    else", body.index("if is_wifi_connected; then", body.index("# Step 2: Check WiFi")))
        end = body.index("# Step 3: Wait for setup completion", start)
        hotspot = self._executed(body[start:end])
        assert "start_setup_server_provisioning" in hotspot
        assert "display_hotspot" in hotspot
        # /review litclock-dev#712: a stray inline-completion in THIS branch would mark
        # setup complete with zero provisioning — brick-class, and the
        # whole-file positional checks would stay green. Negative-assert it.
        assert 'SETUP_DONE_INLINE="true"' not in hotspot, (
            "the hotspot branch must never complete setup inline"
        )
        assert "SERVER_PID=$SETUP_SERVER_PID" in hotspot, (
            "the hotspot arm must hand its server pid to wait_for_setup, or the gate "
            "restart-loops on an empty pid"
        )

    def test_ntp_sync_survives_on_the_preconnected_path(self):
        """The page is gone; the clock-accuracy step it shared the branch with
        must not go with it — a wrong-time clock is the failure the whole
        handoff gate exists to prevent."""
        branch = self._executed(self._preconnected_branch())
        assert "timedatectl set-ntp true" in branch
        assert "NTPSynchronized=yes" in branch
        # And NTP settles BEFORE the resolver writes a timezone.
        assert branch.index("NTPSynchronized=yes") < branch.index("location_resolver.py")

    def test_resolver_failure_cannot_abort_the_branch(self):
        """main() always exits 0, but the invocation must still be guarded —
        under a future `set -e` (or a resolver crash before main's catch, or a
        timeout expiry) an unguarded non-zero exit would kill first-boot
        mid-branch with setup unmarked, which reads as a brick. Comment-
        stripped (/review litclock-dev#712): the raw span's comments could satisfy this."""
        branch = self._executed(self._preconnected_branch())
        line_start = branch.index("location_resolver.py")
        invocation = branch[branch.rindex("\n", 0, line_start) : branch.index('SETUP_DONE_INLINE="true"', line_start)]
        assert "|| log" in invocation, "the resolver invocation has no failure guard"

    def test_resolver_is_time_bounded(self):
        """/review litclock-dev#712 finding 1: the old path had a hard 1800s ceiling with
        an on-panel recovery. An unbounded inline resolver turns a wedged
        timedatectl D-Bus or a byte-dripping ip-api middlebox into a forever
        "Setting Up" splash. The invocation itself must carry the bound."""
        branch = self._executed(self._preconnected_branch())
        line_start = branch.index("location_resolver.py")
        invocation = branch[branch.rindex("\n", 0, line_start) : line_start]
        assert re.search(r"\btimeout\s+\d+\b", invocation), (
            "the resolver invocation has no timeout wrapper — a hang here has no ceiling and "
            "no on-panel recovery (litclock-dev#647 /review)"
        )

    def test_missing_env_file_falls_back_to_the_setup_page_not_inline_completion(self):
        """/review litclock-dev#712 finding 3b: completing inline with env.sh missing makes
        the failure PERMANENT — first-boot disables itself and every later PWA
        env write raises FileNotFoundError. The branch must gate inline
        completion on the env file and fall back to the legacy server (whose
        missing-env exit restart-loops into the 1800s retry ceiling)."""
        branch = self._executed(self._preconnected_branch())
        gate_at = branch.index('if [[ -f "$ENV_FILE" ]]')
        resolver_at = branch.index("location_resolver.py")
        inline_at = branch.index('SETUP_DONE_INLINE="true"')
        assert gate_at < resolver_at and gate_at < inline_at, (
            "inline completion is not gated on env.sh existing"
        )
        fallback = branch[branch.index("else", inline_at) :]
        assert "start_setup_server" in fallback and "SERVER_PID=$SETUP_SERVER_PID" in fallback, (
            "the missing-env.sh arm must fall back to the legacy setup server so the 1800s "
            "ceiling gives a retry on next boot"
        )


class TestRestartLoopCadence:
    """litclock-dev#733 + /review litclock-dev#735: the wait_for_setup restart splash
    painted per iteration (~360 e-ink refreshes on the missing-env doom
    path, ceiling stretched to 1-2h), its copy claimed a page the fallback
    arm no longer has, and the provisioning arm lost the hotspot QR to an
    interstitial forever. These EXECUTE the lifted function with everything
    stubbed and two+ restart iterations forced."""

    def _run(self, tmp_path, provisioning: bool, strict_ok_after: int = 1) -> tuple[str, str]:
        body = (Path(__file__).resolve().parents[1] / "scripts" / "first-boot.sh").read_text()
        anchor = "wait_for_setup() {"
        assert body.count(anchor) == 1
        start = body.index(anchor)
        end = body.index("\n}\n", start) + len("\n}\n")
        span = body[start:end]
        assert "restart_splash_painted" in span, "span lost the paint-once flag"
        assert "display_hotspot" in span, "span lost the QR repaint (/review litclock-dev#735)"
        rec = tmp_path / "rec"
        log = tmp_path / "log"
        script = f"""
REC={rec}
LOGF={log}
STRICT_OK_AFTER={strict_ok_after}
SIGNAL_FILE={tmp_path}/never-created
ENV_FILE=/dev/null
INSTALL_DIR={tmp_path}
PYTHON=/bin/false
HOTSPOT_SSID=TestNet
HOTSPOT_PASSWORD=secret
HOTSPOT_IP=10.42.0.1
PROVISIONING={'true' if provisioning else ''}
log() {{ echo "LOG: $1" >> "$LOGF"; }}
log_error() {{ echo "ERR: $1" >> "$LOGF"; }}
display_message() {{ echo "PLAIN: $1|$2|$3" >> "$REC"; }}
display_message_strict() {{
    echo "PAINT: $1|$2|$3" >> "$REC"
    local n
    n=$(grep -c "^PAINT:" "$REC")
    [ "$n" -ge "$STRICT_OK_AFTER" ]
}}
display_hotspot() {{ echo "QR: $1|$2|$3" >> "$REC"; }}
kill() {{ return 1; }}
sleep() {{ :; }}
{span}
wait_for_setup 99999 21
"""
        subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        paints = rec.read_text() if rec.exists() else ""
        logs = log.read_text() if log.exists() else ""
        return paints, logs

    def test_splash_painted_once_across_many_restarts(self, tmp_path):
        paints, logs = self._run(tmp_path, provisioning=False)
        assert logs.count("restarted (PID") >= 2, (
            "harness must force at least two restart iterations or the paint-once "
            "pin is vacuous (the litclock-dev#662 no-op-harness class)"
        )
        assert paints.count("PAINT:") == 1, f"splash painted per iteration again: {paints!r}"

    def test_failed_paint_is_retried_next_iteration(self, tmp_path):
        # /review litclock-dev#735 F1: a wedged paint must NOT latch the flag — the old
        # per-iteration behavior at least retried. strict fails on call 1,
        # succeeds on call 2 → exactly two attempts, then no more.
        paints, logs = self._run(tmp_path, provisioning=False, strict_ok_after=2)
        assert logs.count("restarted (PID") >= 3, "need 3+ iterations to prove the retry stops"
        assert paints.count("PAINT:") == 2, (
            f"a failed paint must retry once more and a landed paint must stop: {paints!r}"
        )

    def test_fallback_arm_copy_names_no_page(self, tmp_path):
        paints, _ = self._run(tmp_path, provisioning=False)
        # The paint records the catalog KEY now; the copy properties are
        # asserted on the resolved en VALUES (site→key→value chain).
        assert "firstboot.splash.recovering" in paints, f"fallback arm paints the wrong key: {paints!r}"
        cat = _en_catalog()
        copy = " ".join(
            cat.get("firstboot.splash.recovering." + p, "") for p in ("title", "message", "submessage")
        )
        assert "Restarting setup page" not in copy, (
            "the fallback arm has no page (litclock-dev#715) — copy must not claim one"
        )
        assert "unplug" in copy.lower()

    def test_provisioning_arm_repaints_the_hotspot_qr(self, tmp_path):
        # /review litclock-dev#735 (both passes): the QR is what a not-yet-joined user
        # needs; the old interstitial replaced it forever.
        paints, logs = self._run(tmp_path, provisioning=True)
        assert logs.count("restarted (PID") >= 2
        assert paints.count("QR: TestNet|secret|10.42.0.1") == 1, f"QR repaint wrong: {paints!r}"
        assert "PAINT:" not in paints and "PLAIN:" not in paints, (
            "provisioning arm must repaint the QR, not an interstitial"
        )

    def test_restart_sleep_counts_toward_elapsed(self, tmp_path):
        # With sleeps stubbed, iterations are bounded purely by the elapsed
        # arithmetic: timeout=21 with 5+2 per restart iteration → 3
        # iterations (21/7), where the old 5-only accounting allowed 5
        # (21/5). Pin via restart count.
        _, logs = self._run(tmp_path, provisioning=False)
        restarts = logs.count("restarted (PID")
        assert restarts == 3, f"elapsed accounting changed: {restarts} restarts for timeout=21"


class TestGiftLanguageConditionalConsumption:
    """litclock-dev#532 pickers 5b, trap (b): .gift-language is consumed on
    setup success ONLY if its code is ACTIVE (honored); kept otherwise so
    the gifter's intent survives registry regressions / OTA lag."""

    @staticmethod
    def _content():
        with open(FIRST_BOOT_SH) as f:
            return f.read()

    @staticmethod
    def _helper_py(content):
        """Extract the helper's python heredoc for execution."""
        anchor = content.find("gift_language_marker_consumable() {")
        assert anchor != -1, "helper not found"
        py_start = content.index("<<'PY'", anchor) + len("<<'PY'\n")
        py_end = content.index("\nPY\n", py_start)
        return content[py_start:py_end]

    def test_helper_defined_before_success_block_call_site(self):
        """Bash resolves functions at call time, but the lifted-span lesson
        (litclock-dev#719) pins definition-before-call ordering explicitly."""
        content = self._content()
        def_idx = content.find("gift_language_marker_consumable() {")
        call_idx = content.find("if gift_language_marker_consumable; then")
        assert def_idx != -1 and call_idx != -1
        assert def_idx < call_idx

    def test_welcome_markers_still_unconditionally_consumed(self):
        """Only .gift-language went conditional — the welcome markers keep
        the litclock-dev#316 unconditional-consume ordering (a stranded .welcome-mode
        paints the gift welcome on every shutdown forever)."""
        content = self._content()
        assert "sudo rm -f /etc/litclock/.welcome-mode /etc/litclock/.welcome-message" in content
        # And the combined rm must NOT still include .gift-language.
        combined_rm = (
            "sudo rm -f /etc/litclock/.welcome-mode "
            "/etc/litclock/.welcome-message /etc/litclock/.gift-language"
        )
        assert combined_rm not in content

    def test_conditional_consumption_structure(self):
        content = self._content()
        idx = content.find("if gift_language_marker_consumable; then")
        assert idx != -1
        window = content[idx : idx + 400]
        assert 'sudo rm -f /etc/litclock/.gift-language' in window
        assert "keeping .gift-language" in window

    # ── executed behavior against the REAL registry (en active only) ──

    def _run_helper_py(self, tmp_path, marker_content=None, symlink=False):
        import subprocess
        import sys

        py = self._helper_py(self._content())
        marker = tmp_path / "gift-language"
        if symlink:
            target = tmp_path / "target"
            target.write_text("en", encoding="utf-8")
            marker.symlink_to(target)
        elif marker_content is not None:
            marker.write_text(marker_content, encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-", os.path.join(REPO_ROOT, "src"), str(marker)],
            input=py,
            capture_output=True,
            text=True,
        )

    def test_active_code_is_consumable(self, tmp_path):
        proc = self._run_helper_py(tmp_path, marker_content="en")
        assert proc.returncode == 0, proc.stderr

    def test_inactive_code_is_kept(self, tmp_path):
        proc = self._run_helper_py(tmp_path, marker_content="es")
        assert proc.returncode != 0

    def test_garbage_code_is_kept(self, tmp_path):
        proc = self._run_helper_py(tmp_path, marker_content="not a code!!")
        assert proc.returncode != 0

    def test_missing_marker_not_consumable(self, tmp_path):
        proc = self._run_helper_py(tmp_path)
        assert proc.returncode != 0

    def test_symlinked_marker_not_consumable(self, tmp_path):
        """O_NOFOLLOW belt on the marker read — /etc/litclock is root-owned
        so a symlink there already implies root, but uniformity beats
        per-path reasoning (the 5a read-side posture)."""
        proc = self._run_helper_py(tmp_path, symlink=True)
        assert proc.returncode != 0

    # ── executed branch binding (a window grep passes with the branch
    #    bodies swapped — run the real glue with the path rewired) ──

    def test_consume_keep_branch_binding(self, tmp_path):
        """Execute the consume/keep glue with a stubbed consumability
        helper: exit 0 must rm the marker (and not log), exit 1 with the
        marker present must log 'keeping' (and not rm). The absolute
        /etc/litclock path is rewired to tmp — the test asserts exactly
        TWO substitutions so it knows precisely what it changed."""
        import subprocess

        content = self._content()
        idx = content.find("if gift_language_marker_consumable; then")
        assert idx != -1
        end = content.index("fi\n", content.index("keeping .gift-language", idx)) + len("fi\n")
        span = content[idx:end]
        marker = tmp_path / "gift-language"
        rewired = span.replace("/etc/litclock/.gift-language", str(marker))
        assert span.count("/etc/litclock/.gift-language") == 2
        for rc, marker_should_survive, expect_keep_log in ((0, False, False), (1, True, True)):
            marker.write_text("es", encoding="utf-8")
            script = (
                'log() { echo "LOG:$*"; }\n'
                'sudo() { "$@"; }\n'
                f"gift_language_marker_consumable() {{ return {rc}; }}\n"
                f"{rewired}\n"
            )
            proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
            assert proc.returncode == 0, proc.stderr
            assert marker.exists() == marker_should_survive, (rc, proc.stdout)
            assert ("keeping .gift-language" in proc.stdout) == expect_keep_log, (rc, proc.stdout)


class TestCatalogTripletCallSites:
    """litclock-dev#532 bulk extraction: every first-boot splash paints a
    catalog PREFIX through the one-spawn `status --catalog-prefix` path."""

    @staticmethod
    def _content():
        with open(FIRST_BOOT_SH) as f:
            return f.read()

    def test_no_literal_title_paints_remain(self):
        """A display_message call whose first argument is a quoted literal
        is a regression to the pre-extraction form."""
        import re as _re

        content = self._content()
        literal_calls = _re.findall(r'display_message(?:_strict)?\s+"[^"]', content)
        assert not literal_calls, f"literal-title paints crept back in: {literal_calls}"

    def test_all_nine_prefixes_painted(self):
        content = "\n".join(
            ln for ln in self._content().splitlines() if not ln.lstrip().startswith("#")
        )
        for prefix in (
            "firstboot.splash.recovering",
            "firstboot.splash.preparing",
            "firstboot.splash.wifi_connected",
            "firstboot.splash.ntp_sync",
            "firstboot.splash.detecting_location",
            "firstboot.splash.wifi_retry",
            "firstboot.splash.setup_failed",
            "firstboot.splash.setup_complete",
            "firstboot.splash.setup_incomplete",
        ):
            assert prefix in content, f"{prefix} paint site missing"

    def test_dynamic_sites_pass_their_slots(self):
        content = self._content()
        assert 'display_message firstboot.splash.wifi_connected --slot "ssid=$ssid"' in content
        assert (
            'display_message firstboot.splash.wifi_retry --slot "attempt=$attempt" --slot "max=$HOTSPOT_MAX_RETRIES"'
            in content
        )

    def test_helpers_route_through_catalog_prefix(self):
        code = "\n".join(
            ln for ln in self._content().splitlines() if not ln.lstrip().startswith("#")
        )
        assert code.count('--catalog-prefix "$prefix"') == 2  # both helpers, executed lines

    def test_boot_splash_paints_the_catalog_triplet(self):
        with open(BOOT_SPLASH_SH) as f:
            body = f.read()
        # Comments satisfy (and here, falsify) source assertions — keep
        # only executed lines for the negative check.
        code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
        assert "--catalog-prefix boot.splash.starting" in code
        assert '"Starting..."' not in code


class TestStatusCatalogRenderParity:
    """EXECUTED byte parity: `status --catalog-prefix X --save` must render
    the identical PNG as the literal form with the same strings — the
    extraction changes zero painted pixels (guard-observation-window: the
    subject is the RENDER, not the source text)."""

    def test_catalog_and_literal_renders_are_byte_identical(self, tmp_path):
        import subprocess
        import sys

        a = tmp_path / "literal.png"
        b = tmp_path / "catalog.png"
        eink = os.path.join(REPO_ROOT, "src", "eink_display.py")
        r1 = subprocess.run(
            [
                sys.executable, eink, "status", "Setup",
                "--message", "LitClock", "--submessage", "Preparing setup...",
                "--save", str(a),
            ],
            capture_output=True, text=True, timeout=60,
        )
        assert r1.returncode == 0, r1.stderr
        r2 = subprocess.run(
            [
                sys.executable, eink, "status",
                "--catalog-prefix", "firstboot.splash.preparing", "--save", str(b),
            ],
            capture_output=True, text=True, timeout=60,
        )
        assert r2.returncode == 0, r2.stderr
        assert a.read_bytes() == b.read_bytes()

    def test_slot_filled_render_differs_from_template(self, tmp_path):
        """The slot must actually fill on the painted frame (coverage-is-
        not-entropy: assert DISTINCTNESS, not just success)."""
        import subprocess
        import sys

        a = tmp_path / "one.png"
        b = tmp_path / "two.png"
        eink = os.path.join(REPO_ROOT, "src", "eink_display.py")
        for path, ssid in ((a, "HomeNet"), (b, "CafeNet")):
            r = subprocess.run(
                [
                    sys.executable, eink, "status",
                    "--catalog-prefix", "firstboot.splash.wifi_connected",
                    "--slot", f"ssid={ssid}", "--save", str(path),
                ],
                capture_output=True, text=True, timeout=60,
            )
            assert r.returncode == 0, r.stderr
        assert a.read_bytes() != b.read_bytes()

    def test_missing_title_and_prefix_is_a_usage_error(self):
        import subprocess
        import sys

        r = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "src", "eink_display.py"), "status"],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode != 0


class TestDisplayMessageHelpersExecuted:
    """EXECUTED coverage of the real display_message/display_message_strict
    bodies (the litclock-dev#662 no-op-harness class: every other harness
    stubs them). PYTHON points at an argv recorder, so slot forwarding and
    exit-code semantics run for real."""

    @staticmethod
    def _helpers_span():
        with open(FIRST_BOOT_SH) as f:
            content = f.read()
        start = content.index("display_message() {")
        # take everything through the end of display_message_strict
        anchor = content.index("display_message_strict() {")
        end = content.index("\n}", anchor) + len("\n}")
        return content[start:end]

    def _run(self, tmp_path, invocation, recorder_exit=0):
        import subprocess

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "eink_display.py").write_text("", encoding="utf-8")
        rec_file = tmp_path / "argv.rec"
        recorder = tmp_path / "recorder.sh"
        recorder.write_text(
            "#!/bin/bash\n"
            f'for a in "$@"; do printf "%s\\n" "$a"; done > "{rec_file}"\n'
            f"exit {recorder_exit}\n",
            encoding="utf-8",
        )
        recorder.chmod(0o755)
        script = (
            f'INSTALL_DIR="{tmp_path}"\n'
            f'PYTHON="{recorder}"\n'
            f"{self._helpers_span()}\n"
            f"{invocation}\n"
            'echo "RC=$?"\n'
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20)
        argv = rec_file.read_text(encoding="utf-8").splitlines() if rec_file.exists() else []
        return proc, argv

    def test_multiword_slot_arrives_as_one_argv_element(self, tmp_path):
        proc, argv = self._run(
            tmp_path,
            'display_message firstboot.splash.wifi_connected --slot "ssid=My Home WiFi"',
        )
        assert "RC=0" in proc.stdout, proc.stderr
        assert argv == [
            "src/eink_display.py",
            "status",
            "--catalog-prefix",
            "firstboot.splash.wifi_connected",
            "--slot",
            "ssid=My Home WiFi",
        ], argv

    def test_plain_helper_swallows_paint_failure(self, tmp_path):
        proc, _ = self._run(
            tmp_path, "display_message firstboot.splash.preparing", recorder_exit=1
        )
        assert "RC=0" in proc.stdout, "display_message must stay best-effort (|| true)"

    def test_strict_helper_propagates_paint_failure(self, tmp_path):
        proc, _ = self._run(
            tmp_path, "display_message_strict firstboot.splash.recovering", recorder_exit=3
        )
        assert "RC=3" in proc.stdout, proc.stdout

    def test_strict_helper_success(self, tmp_path):
        proc, argv = self._run(
            tmp_path, "display_message_strict firstboot.splash.setup_incomplete", recorder_exit=0
        )
        assert "RC=0" in proc.stdout
        assert argv[2:4] == ["--catalog-prefix", "firstboot.splash.setup_incomplete"]


class TestStatusCatalogRenderParityExtra:
    def test_blank_part_prefix_matches_flag_omitted_literal_render(self, tmp_path):
        """get_triplet returns '' where the old bash form omitted the flag
        entirely — pin that create_status_image treats them identically, or
        a future `is not None` check would shift every blank-part splash."""
        import subprocess
        import sys

        a = tmp_path / "literal.png"
        b = tmp_path / "catalog.png"
        eink = os.path.join(REPO_ROOT, "src", "eink_display.py")
        r1 = subprocess.run(
            [
                sys.executable, eink, "status", "Setup Complete!",
                "--message", "Starting your clock...", "--save", str(a),
            ],
            capture_output=True, text=True, timeout=60,
        )
        assert r1.returncode == 0, r1.stderr
        r2 = subprocess.run(
            [
                sys.executable, eink, "status",
                "--catalog-prefix", "firstboot.splash.setup_complete", "--save", str(b),
            ],
            capture_output=True, text=True, timeout=60,
        )
        assert r2.returncode == 0, r2.stderr
        assert a.read_bytes() == b.read_bytes()

    def test_explicit_literal_part_overrides_its_catalog_part(self, tmp_path):
        """Slice-2 contract: --catalog-prefix fills parts NOT explicitly
        given — shutdown-splash mixes catalog copy with curated quotes and
        the gifter's custom title. A prefix render with a literal title
        override must equal the all-literal render of the same strings."""
        import subprocess
        import sys

        a = tmp_path / "mixed.png"
        b = tmp_path / "pure.png"
        eink = os.path.join(REPO_ROOT, "src", "eink_display.py")
        r1 = subprocess.run(
            [
                sys.executable, eink, "status", "Custom Title",
                "--catalog-prefix", "firstboot.splash.preparing", "--save", str(a),
            ],
            capture_output=True, text=True, timeout=60,
        )
        assert r1.returncode == 0, r1.stderr
        r2 = subprocess.run(
            [
                sys.executable, eink, "status", "Custom Title",
                "--message", "LitClock", "--submessage", "Preparing setup...",
                "--save", str(b),
            ],
            capture_output=True, text=True, timeout=60,
        )
        assert r2.returncode == 0
        assert a.read_bytes() == b.read_bytes()


class TestLeadingDashTitleRender:
    def test_leading_dash_title_after_separator_renders(self, tmp_path):
        """End-to-end: `status --catalog-prefix X -- "-Mom-"` must paint
        (the shutdown welcome path's exact shape)."""
        import subprocess
        import sys

        out = tmp_path / "dash.png"
        r = subprocess.run(
            [
                sys.executable, os.path.join(REPO_ROOT, "src", "eink_display.py"),
                "status", "--catalog-prefix", "shutdown.splash.welcome",
                "--save", str(out), "--", "-Mom-",
            ],
            capture_output=True, text=True, timeout=60,
        )
        assert r.returncode == 0, r.stderr
        assert out.exists()
