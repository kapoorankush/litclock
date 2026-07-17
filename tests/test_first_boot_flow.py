"""Tests for first-boot flow validation (#111).

Validates the complete first-boot sequence for image-based deployment:
- WiFi provisioning with zero saved networks
- Boot ordering (splash → firstboot → timer)
- SSL cert generation on a fresh image
- NTP sync after WiFi is provisioned
- Timezone setting
"""

import os
import re
import subprocess
import tempfile

import pytest

import setup_server

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
FIRST_BOOT_SH = os.path.join(REPO_ROOT, "scripts", "first-boot.sh")
BOOT_SPLASH_SH = os.path.join(REPO_ROOT, "scripts", "boot-splash.sh")


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
        assert "Unplug power" in content

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


# ── SSL cert generation (#111) ───────────────────────────────────────


class TestSSLCertGeneration:
    """Verify SSL certificate generation for the setup server."""

    def test_generates_cert_when_missing(self, tmp_path):
        """On a fresh image, no certs exist. generate_self_signed_cert
        should create them."""
        cert_dir = str(tmp_path / "certs")
        os.makedirs(cert_dir)

        cert_file, key_file = setup_server.generate_self_signed_cert(cert_dir)

        if cert_file is None:
            pytest.skip("openssl not available in test environment")

        assert os.path.isfile(cert_file)
        assert os.path.isfile(key_file)
        assert cert_file.endswith("cert.pem")
        assert key_file.endswith("key.pem")

    def test_reuses_existing_cert(self, tmp_path):
        """If certs already exist, should return them without regenerating."""
        cert_dir = str(tmp_path / "certs")
        os.makedirs(cert_dir)

        cert_path = os.path.join(cert_dir, "cert.pem")
        key_path = os.path.join(cert_dir, "key.pem")
        with open(cert_path, "w") as f:
            f.write("existing-cert")
        with open(key_path, "w") as f:
            f.write("existing-key")

        cert_file, key_file = setup_server.generate_self_signed_cert(cert_dir)
        assert cert_file == cert_path
        assert key_file == key_path
        # Content should be unchanged (not regenerated)
        with open(cert_path) as f:
            assert f.read() == "existing-cert"

    def test_returns_none_when_openssl_missing(self, mocker):
        """If openssl is not installed, should gracefully return None.

        Mocks `https_cert.subprocess.run` — the actual subprocess call lives
        there since M1's extraction; setup_server.generate_self_signed_cert
        is a thin delegate. Pre-#414 the test happened to work by mocking
        `setup_server.subprocess.run` because setup_server had an
        `import subprocess` for set_system_timezone, but the patched name
        was never actually invoked. After #414 moved set_system_timezone +
        its subprocess dep to geocoding, setup_server has no subprocess
        attribute at all, so the bug-passing-as-test-pass surfaced."""
        mocker.patch(
            "https_cert.subprocess.run",
            side_effect=FileNotFoundError("openssl not found"),
        )
        with tempfile.TemporaryDirectory() as cert_dir:
            cert_file, key_file = setup_server.generate_self_signed_cert(cert_dir)
        assert cert_file is None
        assert key_file is None

    def test_generated_cert_has_correct_subject(self, tmp_path):
        """The generated cert should have litclock.local as CN."""
        cert_dir = str(tmp_path / "certs")
        os.makedirs(cert_dir)

        cert_file, _ = setup_server.generate_self_signed_cert(cert_dir)
        if cert_file is None:
            pytest.skip("openssl not available")

        result = subprocess.run(
            ["openssl", "x509", "-in", cert_file, "-noout", "-subject"],
            capture_output=True,
            text=True,
        )
        assert "litclock.local" in result.stdout

    def test_generated_cert_has_san(self, tmp_path):
        """The cert should include SANs for litclock.local and localhost."""
        cert_dir = str(tmp_path / "certs")
        os.makedirs(cert_dir)

        cert_file, _ = setup_server.generate_self_signed_cert(cert_dir)
        if cert_file is None:
            pytest.skip("openssl not available")

        result = subprocess.run(
            ["openssl", "x509", "-in", cert_file, "-noout", "-text"],
            capture_output=True,
            text=True,
        )
        assert "litclock.local" in result.stdout
        assert "localhost" in result.stdout


# ── Timezone setting (#111) ──────────────────────────────────────────


class TestTimezoneInFirstBoot:
    """Verify timezone is set during the setup flow."""

    def test_setup_server_sets_timezone(self, mocker):
        """The setup POST handler calls set_system_timezone.

        #414 item #5: set_system_timezone lives in `geocoding` since the
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
        """#274 follow-up: first-boot.sh must use the shared sidecar-flock
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
            "prepare-for-cloning.sh (#274 cross-writer interlock)"
        )

    def test_first_boot_uses_no_block_for_timer(self):
        """Starting litclock.timer from within a service MUST use --no-block
        to avoid systemd deadlock."""
        with open(FIRST_BOOT_SH) as f:
            content = f.read()
        assert "--no-block" in content

    def test_first_boot_starts_litclock_control(self):
        """#245 M5 hardware-QA fix: after firstboot writes .setup-complete back
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
        ExecStartPost on litclock-splash.service (issue #269). Verify the
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
        """Gift-mode marker (#189) must be removed in the first-boot success
        path so subsequent shutdowns paint the normal 'Powered Off' splash.

        #316 /review CRITICAL ordering fix: the rm happens BEFORE
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
            "rm permanently strands the gift welcome marker (#316 /review)"
        )

    def test_first_boot_consumes_welcome_message_before_setup_complete(self):
        """#280: the optional personalized welcome message (set via the PWA
        Prepare-for-Gifting flow) must be cleaned up alongside the
        .welcome-mode marker. #316 /review: same ordering invariant as
        .welcome-mode — rm before mark_setup_complete to defeat the
        power-loss race."""
        with open(FIRST_BOOT_SH) as f:
            content = f.read()
        assert ".welcome-message" in content, "first-boot.sh must clean up the optional .welcome-message file (#280)"
        rm_idx = content.find(".welcome-message")
        # Find the rm command (not a comment).
        rm_idx = content.find("rm -f /etc/litclock/.welcome-mode")
        call_idx = content.find("mark_setup_complete", rm_idx)
        assert call_idx > rm_idx, (
            ".welcome-message rm must precede mark_setup_complete CALL — "
            "same power-loss race as .welcome-mode (#316 /review)"
        )


def test_first_boot_default_env_includes_mode_and_ip_country():
    """#337 A3 + /review testing-gap: first-boot.sh's env.sh template (both
    the state.sh-flock path AND the legacy heredoc fallback) must include
    the new MODE + IP_COUNTRY defaults. Without these, a fresh-flash Pi
    would have no MODE/IP_COUNTRY keys at all — pre-S2 migration semantics
    would kick in (which work, but are an unnecessary code path for new
    installs)."""
    from pathlib import Path

    content = (Path(__file__).parent.parent / "scripts/first-boot.sh").read_text()
    # The keys appear in TWO blocks (flock path + heredoc fallback) — count both.
    assert content.count("export WEATHER_LOCATION_MODE=auto") >= 2, (
        "#337 A3: first-boot.sh must include MODE=auto in BOTH the atomic-write "
        "path AND the heredoc-fallback path (so the keys ship regardless of which "
        "writer fires)."
    )
    assert content.count("export WEATHER_IP_COUNTRY=") >= 2, (
        "#337 A3: first-boot.sh must include WEATHER_IP_COUNTRY= in both writer paths"
    )


# ── #529: power off after the Setup-Incomplete timeout ──────────────


class TestSetupIncompletePoweroff:
    """#529: after the setup-wait times out, the device must paint recovery
    instructions, wait a grace period, and power off — not idle forever in
    a half-provisioned state."""

    @pytest.fixture(scope="class")
    def content(self):
        with open(FIRST_BOOT_SH) as f:
            return f.read()

    def _timeout_block(self, content):
        idx = content.find('display_message "Setup Incomplete"')
        assert idx != -1, "Setup Incomplete branch missing"
        # End at the function's closing brace on its own line — a bare
        # find("}") would truncate at the first ${VAR:-default} expansion.
        return content[idx : content.find("\n}", idx)]

    def test_timeout_path_powers_off(self, content):
        block = self._timeout_block(content)
        # `sudo systemctl poweroff` — the sudo-systemctl form used elsewhere
        # in this script + the scoped 020 sudoers allowlist (/review).
        assert "sudo systemctl poweroff" in block

    def test_no_grace_sleep_between_paint_and_poweroff(self, content):
        """Owner decision on #529: NO delay between painting the recovery
        copy and powering off. The copy invites the user to pull power, so
        every running second after the paint is a window for an unclean
        power cut (SD-corruption class). The 30-minute setup timeout was
        the grace period."""
        block = self._timeout_block(content)
        # Match a sleep COMMAND (line-leading), not the word in comments.
        assert not re.search(r"^\s*sleep\b", block, re.M), "no sleep command allowed in the Setup-Incomplete branch"
        assert "FIRSTBOOT_POWEROFF_GRACE" not in block

    def test_splash_suppressed_so_message_persists(self, content):
        """The bistable e-ink keeps 'Setup Incomplete' visible while off —
        but only if litclock-shutdown.service's ExecStop is told not to
        repaint. The root-only suppress marker must be touched (via sudo)
        BEFORE the poweroff."""
        block = self._timeout_block(content)
        marker_idx = block.find("sudo touch /run/litclock-splash-suppress")
        off_idx = block.find("sudo systemctl poweroff")
        assert marker_idx != -1, "suppress marker touch missing"
        assert marker_idx < off_idx

    def test_no_ssh_copy_on_powered_off_screen(self, content):
        """The device is about to be off — 'SSH in' would be a lie on the
        persisted screen (and gift recipients don't SSH). The recovery copy
        is unplug/replug, which matches what a power-cycle actually does."""
        block = self._timeout_block(content)
        assert "SSH" not in block.split("\n")[0], "Setup Incomplete copy must not mention SSH"
        assert "Unplug" in block or "unplug" in block

    def test_setup_timeout_is_hardcoded_not_env_overridable(self, content):
        """/review: the setup wait is a fixed 1800s. No env-var override in
        shipped code — a stray systemd drop-in setting it to 0 (instant
        poweroff) or a huge value (infinite idle) is a footgun, and the QA
        it was added for is complete."""
        assert 'wait_for_setup "$SERVER_PID" 1800' in content
        assert "FIRSTBOOT_SETUP_TIMEOUT" not in content
