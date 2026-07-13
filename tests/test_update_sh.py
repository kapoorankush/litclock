"""Tests for scripts/update.sh (issue #160).

Two layers of coverage:

1. Structural tests — grep the script for code patterns that MUST be present.
   These encode every past bug / invariant we care about: the --no-block
   systemd hack, the checksum-based self-reexec guard, GPIO race ordering,
   venv quote-tolerant grep, etc.

2. Execution sandbox tests — run update.sh against a fake install directory
   with PATH stubs for git/sudo/systemctl/python3/pip. Assert the script
   reaches the right phases and makes the expected subprocess calls.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"


@pytest.fixture(scope="module")
def update_sh_content():
    return UPDATE_SH.read_text()


# ── Structural tests ──────────────────────────────────────────────────


class TestUpdateScriptStructure:
    """Grep-based invariants for update.sh."""

    def test_timer_start_uses_no_block(self, update_sh_content):
        """`systemctl start litclock.timer` from within a service deadlocks
        without --no-block. update.sh is often invoked from a systemd unit.

        Actually: update.sh is run interactively by the user, but the pattern
        of starting units from a script context still benefits from non-blocking
        semantics — and we want a guard against regressing to blocking start
        if update.sh ever gets wired into a service.

        Post-#209: the smoke-test failure path (Phase 4.5) also restarts the
        timer before exiting so the clock keeps ticking on the OLD SHA. The
        ordering invariant we care about is the NORMAL path (Phase 7), i.e.
        the LAST timer-start in the script must come after 'Refreshing display'.
        """
        # The normal Phase 7 ordering is what protects against GPIO races.
        refresh_idx = update_sh_content.find("Refreshing display")
        last_timer_start_idx = update_sh_content.rfind("systemctl start litclock.timer")
        assert refresh_idx != -1
        assert last_timer_start_idx != -1
        assert refresh_idx < last_timer_start_idx, (
            "Final (Phase 7) display refresh must run BEFORE final timer restart to avoid GPIO race"
        )

    def test_newly_enabled_timers_started_no_block(self, update_sh_content):
        """#249/#251: enabling a unit registers it with systemd but does not
        activate it in the current session — newly-installed timers idle
        until reboot. update.sh must iterate ENABLED_UNITS, filter to
        *.timer, and `systemctl start --no-block` each one."""
        assert 'for unit in "${ENABLED_UNITS[@]}"' in update_sh_content, (
            "must iterate ENABLED_UNITS (not introduce a parallel array)"
        )
        assert '[[ "$unit" == *.timer ]]' in update_sh_content, "must filter ENABLED_UNITS entries to *.timer suffix"
        assert 'sudo systemctl start --no-block "$unit"' in update_sh_content, (
            "must start newly-enabled timers with --no-block to avoid the "
            "systemctl-from-inside-running-service deadlock"
        )

    def test_timer_start_only_targets_timers_not_services(self, update_sh_content):
        """#251 explicit ask: 'only timers, not services — services should
        remain start-on-trigger via their existing systemd hooks.' The new
        loop's filter must be a *.timer suffix check, not a blanket
        iteration over all enabled units."""
        # Find the new-timer-start loop body and confirm the suffix gate.
        loop_start = update_sh_content.find('for unit in "${ENABLED_UNITS[@]}"')
        assert loop_start != -1, "new-timer-start loop missing"
        loop_body = update_sh_content[loop_start : loop_start + 400]
        assert "*.timer" in loop_body, "loop must filter on *.timer suffix"
        assert "*.service" not in loop_body, (
            "loop must NOT match *.service — services are timer-driven or have their own start hooks"
        )

    def test_timer_start_after_daemon_reload(self, update_sh_content):
        """The new-timer-start loop must run AFTER `systemctl daemon-reload`
        (so systemd has the new unit definitions loaded) and BEFORE Phase 7
        (so the timer is live before any service restart)."""
        daemon_reload_idx = update_sh_content.find("systemctl daemon-reload")
        new_timer_loop_idx = update_sh_content.find('for unit in "${ENABLED_UNITS[@]}"')
        phase_7_idx = update_sh_content.find("Phase 7: Restart services")
        assert daemon_reload_idx != -1
        assert new_timer_loop_idx != -1
        assert phase_7_idx != -1
        assert daemon_reload_idx < new_timer_loop_idx, "new-timer start loop must run AFTER daemon-reload"
        assert new_timer_loop_idx < phase_7_idx, "new-timer start loop must run BEFORE Phase 7 service restarts"

    def test_self_reexec_checksum_guard(self, update_sh_content):
        """PR #94: update.sh re-execs itself if its own bytes changed mid-run,
        otherwise bash reads stale content from the old fd."""
        assert "OLD_SELF_HASH" in update_sh_content
        assert "NEW_SELF_HASH" in update_sh_content
        assert 'exec "$SELF_SCRIPT"' in update_sh_content

    def test_no_set_e(self, update_sh_content):
        """update.sh intentionally omits `set -e` — a single sudo/mkdir
        failure should not abort the whole update mid-phase."""
        lines = update_sh_content.splitlines()
        for line in lines[:20]:  # only check the preamble
            stripped = line.strip()
            assert stripped != "set -e", "update.sh must not use `set -e`"
            assert stripped != "set -eu", "update.sh must not use `set -eu`"
            assert not stripped.startswith("set -e ")

    def test_venv_activate_grep_is_quote_tolerant(self, update_sh_content):
        """Python 3.11 changed activate's VIRTUAL_ENV= quoting (may be bare
        or double-quoted). The grep that validates venv health must match
        both forms."""
        # Look for the tolerant pattern: VIRTUAL_ENV=["']*...["']*
        assert r"VIRTUAL_ENV=[\"" in update_sh_content, (
            "venv activate grep must tolerate both quoted and unquoted VIRTUAL_ENV="
        )

    def test_author_clock_migration_present(self, update_sh_content):
        """The author-clock → litclock rename migration must stay in place
        until we're sure no author-clock installs remain in the wild."""
        assert "author-clock" in update_sh_content
        assert "/etc/authorclock" in update_sh_content
        assert "/etc/litclock" in update_sh_content
        assert "authorclock*.service" in update_sh_content
        assert "authorclock*.timer" in update_sh_content

    def test_firstboot_guard(self, update_sh_content):
        """Must refuse to update while first-boot is running, to avoid
        racing the setup server. Guard reads the marker file directly
        (not `is-active` on the firstboot service, which is Type=oneshot
        and stays active(exited) forever after success)."""
        assert "/etc/litclock/.setup-complete" in update_sh_content
        assert "First-boot setup not yet complete" in update_sh_content
        # Regression: the old `is-active` check on the oneshot service must
        # not creep back in for this guard.
        assert "is-active --quiet litclock-firstboot.service" not in update_sh_content

    def test_pip_hash_file_written_on_success(self, update_sh_content):
        """Hash file should be written only after pip install succeeds — on
        failure we want the next update to retry."""
        import re

        # Find the block around the pip hash write
        hash_write_idx = update_sh_content.find('echo "$PACKAGES_HASH" > "$HASH_FILE"')
        assert hash_write_idx != -1
        # Match the requirements-file pip invocation regardless of intermediate
        # flags (e.g. --upgrade added in #321).
        pip_install_match = None
        for m in re.finditer(r'"\$PIP"\s+install\s+[^\n]*-r\s+"\$REQUIREMENTS_FILTERED"', update_sh_content):
            if m.start() < hash_write_idx:
                pip_install_match = m
        assert pip_install_match is not None
        assert pip_install_match.start() < hash_write_idx

    def test_env_merge_preserves_user_values(self, update_sh_content):
        """Phase 3 must merge new vars from env.sh.sample without overwriting
        existing values in env.sh."""
        # The merge loop reads sample, appends only when varname is NOT
        # already present in env.sh.
        assert "env.sh.sample" in update_sh_content
        assert 'grep -q "^[# ]*export[[:space:]]\\+${varname}=" "$INSTALL_DIR/env.sh"' in update_sh_content

    def test_stale_symlinks_removed(self, update_sh_content):
        """Post-#79 reorg: root-level script symlinks (boot-splash.sh etc.)
        are obsolete and must be cleaned up."""
        for stale in ("boot-splash.sh", "first-boot.sh", "runtheclock.sh", "shutdown-splash.sh"):
            assert f'"$INSTALL_DIR/{stale}"' in update_sh_content, f"{stale} missing from stale-files cleanup list"

    def test_obsolete_systemd_units_removed(self, update_sh_content):
        """Systemd units no longer in the repo should be stopped, disabled,
        and deleted from /etc/systemd/system."""
        assert "/etc/systemd/system/litclock*.service" in update_sh_content
        assert "systemctl disable" in update_sh_content
        assert "Removed obsolete systemd unit" in update_sh_content

    def test_phase7_writes_post_update_grace_marker(self, update_sh_content):
        """#241 — Phase 7 must touch $POST_UPDATE_GRACE_FILE BEFORE any
        service restart, not after. The grace gate must be true throughout
        the restart sequence so a poll firing in the restart window can't
        bypass the soak gate.

        Anti-regression: the original implementation wrote the marker
        AFTER `systemctl start litclock.timer`, leaving a sub-second
        window where a poll could promote a fresh SHA atop a fresh
        heartbeat. Caught in /review pre-merge."""
        import re

        # The grace marker variable must be defined.
        assert "POST_UPDATE_GRACE_FILE" in update_sh_content
        assert "post-update-grace-until" in update_sh_content
        # And written via the atomic helper, BEFORE Phase 7's first systemctl call.
        phase7_idx = update_sh_content.find("Phase 7: Restart services")
        assert phase7_idx != -1
        phase7_block = update_sh_content[phase7_idx:]
        grace_write = re.search(
            r'atomic_write_file\s+"\$POST_UPDATE_GRACE_FILE"',
            phase7_block,
        )
        assert grace_write is not None, "Phase 7 must call atomic_write_file on $POST_UPDATE_GRACE_FILE"
        first_systemctl = re.search(r"sudo systemctl ", phase7_block)
        assert first_systemctl is not None, "Phase 7 should restart at least one systemd unit"
        assert grace_write.start() < first_systemctl.start(), (
            "Grace marker must be written BEFORE any systemctl call in Phase 7 — "
            "otherwise an lkg-record poll firing in the restart window could bypass the soak gate"
        )

    def test_phase7_does_not_restart_litclock_shutdown(self, update_sh_content):
        """#331 — `litclock-shutdown.service` is a stop-hook unit
        (Type=oneshot, ExecStart=/bin/true, ExecStop=shutdown-splash.sh
        which paints "Powered Off" on the e-ink). `systemctl restart`
        = stop+start, so the stop half fires ExecStop mid-update — the
        e-ink alarms the user with "Powered Off" for ~11s while the Pi
        is happily mid-pip-install. There is NO runtime state to
        restart; unit-file changes are already picked up at the Phase 5
        `daemon-reload`. The fix is to never touch this unit from
        update.sh.

        Anti-regression: ANY `systemctl restart|stop` targeting
        litclock-shutdown.service from update.sh must stay out.
        """
        import re

        # Only inspect non-comment lines — the explanatory comment block
        # left in update.sh after the fix legitimately mentions the
        # offending command pattern as a warning to future readers.
        pattern = re.compile(r"systemctl\s+(restart|stop)\s+litclock-shutdown\.service")
        for lineno, line in enumerate(update_sh_content.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            match = pattern.search(line)
            assert match is None, (
                f"update.sh:{lineno} must not stop/restart "
                f"litclock-shutdown.service — its ExecStop paints "
                f"'Powered Off' on the e-ink mid-update (#331). "
                f"Offending line: {line.strip()}"
            )

    def test_sources_lib_state(self, update_sh_content):
        """#241 D3 — atomic helpers were factored to scripts/lib/state.sh.
        update.sh must source it instead of redefining them inline."""
        assert "lib/state.sh" in update_sh_content, "update.sh must source scripts/lib/state.sh"
        # And the helpers must NOT be redefined inside update.sh itself.
        # `atomic_write_file()` definition would look like the function header.
        import re

        local_def = re.search(r"^atomic_write_file\s*\(\s*\)\s*\{", update_sh_content, re.MULTILINE)
        assert local_def is None, "atomic_write_file must NOT be defined inline in update.sh — source from lib"

    def test_phase5_migrates_legacy_lkg_service_install(self, update_sh_content):
        """#241 — pre-#241 Pis have litclock-lkg.service enabled with
        WantedBy=litclock.service, which created a stale .wants/ symlink.
        Phase 5 must disable the OLD unit before cp'ing the new one (which
        has no [Install]) so the symlink is cleaned up and the rewritten
        oneshot doesn't fire on every litclock.service start."""
        # Migration must happen inside Phase 5 (so daemon-reload picks it up).
        phase5_idx = update_sh_content.find("Phase 5: Update systemd units")
        next_phase_idx = update_sh_content.find("Phase 6:", phase5_idx)
        assert phase5_idx != -1 and next_phase_idx != -1
        phase5_block = update_sh_content[phase5_idx:next_phase_idx]
        assert "WantedBy=litclock.service" in phase5_block, (
            "Phase 5 must detect the legacy WantedBy=litclock.service install"
        )
        assert "systemctl disable litclock-lkg.service" in phase5_block, (
            "Phase 5 must disable the legacy litclock-lkg.service to clean its .wants/ symlink"
        )

    def test_phase5_respects_user_disabled_units(self, update_sh_content):
        """Regression for #209 hardware-found defect: Phase 5 must NOT
        re-enable units that the user explicitly disabled via
        `systemctl disable --now litclock-update.timer` (the appliance
        opt-out path documented in the README). The pre-fix logic enabled
        any unit whose `is-enabled` reported `disabled`, but `disabled`
        covers both "never enabled" and "user opted out" — they're
        indistinguishable from systemctl. Phase 5 must use file
        pre-existence as the discriminator instead: only enable units
        that didn't exist in /etc/systemd/system/ before this run."""
        import re

        # Pre-existence check must happen BEFORE the cp.
        assert "was_pre_existing" in update_sh_content, (
            "Phase 5 must track pre-existence to distinguish new installs from user-disabled units"
        )
        # Find the Phase 5 loop body.
        phase5_idx = update_sh_content.find("Phase 5: Update systemd units")
        assert phase5_idx != -1
        phase5_block = update_sh_content[phase5_idx : phase5_idx + 3000]

        # The check pattern: pre-existence test → cp → conditional enable.
        # Verify the pre-existence file test happens.
        assert re.search(r"was_pre_existing\s*=\s*(true|false)", phase5_block), (
            "was_pre_existing must be initialized in the loop"
        )
        assert '[[ -f "/etc/systemd/system/$name" ]]' in phase5_block, (
            "must check unit-file existence in /etc/systemd/system/"
        )
        # The enable must be gated on was_pre_existing == false.
        assert re.search(
            r'was_pre_existing.*==.*"?false"?',
            phase5_block,
        ), "enable must be gated on `was_pre_existing == false`"

        # Anti-regression: the conditional enable (`if systemctl is-enabled
        # ... | grep -q "^disabled$"; then sudo systemctl enable`) must be
        # wrapped INSIDE the `was_pre_existing == false` branch — not at
        # top-level of the loop body. Check ordering: the was_pre_existing
        # variable assignment must appear before the `if systemctl
        # is-enabled` runtime check.
        was_pre_existing_assign = re.search(r"was_pre_existing\s*=", phase5_block)
        if_is_enabled = re.search(r"if\s+systemctl\s+is-enabled", phase5_block)
        assert was_pre_existing_assign is not None
        assert if_is_enabled is not None
        assert was_pre_existing_assign.start() < if_is_enabled.start(), (
            "was_pre_existing assignment must precede the `if systemctl "
            "is-enabled` runtime check so user-disabled units are not "
            "silently re-enabled (#209 D2 fix)"
        )

    def test_venv_creation_uses_system_site_packages(self, update_sh_content):
        """#214 regression: every `python3 -m venv` call MUST include
        --system-site-packages. Without it, a venv rebuild on-device loses
        access to apt-provisioned GPIO libs and pip tries to recompile
        them from sdist (no gcc on the image → fail).

        Must stay in sync with pi-gen/stage3/01-setup-app/00-run.sh:28.
        """
        import re

        venv_calls = re.findall(r"python3 -m venv [^\n|]+", update_sh_content)
        assert venv_calls, "update.sh should have at least one `python3 -m venv` call"
        for call in venv_calls:
            assert "--system-site-packages" in call, (
                f"venv creation missing --system-site-packages — venv rebuild "
                f"would lose apt-provisioned GPIO libs and try to pip-compile "
                f"them (#214). Offending line: {call.strip()}"
            )

    def test_pip_install_filters_apt_provisioned(self, update_sh_content):
        """#214 regression: pip install must filter requirements.txt through
        requirements-apt.txt before installing. Otherwise pip tries to
        install the same packages apt already provides, which on a gcc-less
        image means sdist compilation and failure.

        Must stay in sync with pi-gen/stage3/01-setup-app/00-run.sh:34.
        """
        assert "requirements-apt.txt" in update_sh_content, (
            "update.sh must read apt-provisioned names from requirements-apt.txt (#214)"
        )
        assert "grep -vE" in update_sh_content, "update.sh must filter requirements.txt with a grep -vE regex (#214)"
        # The filtered requirements file, not the raw one, must reach pip.
        assert 'install --upgrade -r "$REQUIREMENTS_FILTERED"' in update_sh_content, (
            "pip install must target the filtered requirements, not the raw file (#214)"
        )

    def test_pip_install_uses_upgrade(self, update_sh_content):
        """#321: without `--upgrade`, pip silently keeps an already-installed
        version of a pinned package even when requirements.txt bumps the pin
        (urllib3==2.6.3 → 2.7.0 was the trigger). Every existing Pi venv
        would skip future security bumps via the weekly auto-update path.
        Pin `--upgrade` so a future refactor can't silently drop it.

        We intentionally do NOT pin `--upgrade-strategy eager`. An earlier
        revision of this PR shipped both flags, but adversarial review
        caught that eager would weekly-upgrade Flask's unpinned transitives
        (Werkzeug, Click, itsdangerous, blinker, MarkupSafe) and Phase 4.5's
        smoke test never imports Flask — a transitive break would ship
        silently and kill the control PWA. Transitive security bumps belong
        at release-cut time via a lockfile (follow-up issue).
        """
        # Locate the requirements-file pip invocation (not the `pip install
        # --upgrade pip` line, which already has --upgrade).
        import re

        req_install = re.search(
            r'"\$PIP"\s+install\s+([^\n]*?)-r\s+"\$REQUIREMENTS_FILTERED"',
            update_sh_content,
        )
        assert req_install is not None, "could not locate the requirements-file pip install line"
        flags = req_install.group(1)
        assert "--upgrade" in flags, (
            "requirements-file pip install must use --upgrade — without it, pinned-version bumps "
            "silently fail to propagate to existing venvs (#321)"
        )
        assert "--upgrade-strategy eager" not in flags, (
            "requirements-file pip install must NOT use --upgrade-strategy eager — it would "
            "weekly-upgrade Flask's unpinned transitives and Phase 4.5 smoke wouldn't catch the "
            "blast (#321 adversarial review). Use a release-cut-time lockfile instead."
        )


# ── Execution sandbox ─────────────────────────────────────────────────


def _setup_fake_install(sandbox, on_master: bool = True, dirty: bool = False):
    """Populate the sandbox with the minimum state update.sh needs to run
    past its pre-flight checks."""
    install = sandbox.root

    # Fake .git so `[[ ! -d .git ]]` passes
    (install / ".git").mkdir()

    # Fake venv with a passable activate + python3
    venv_bin = install / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    activate = venv_bin / "activate"
    activate.write_text(f'VIRTUAL_ENV="{install}/venv"\nexport VIRTUAL_ENV\n')
    python3 = venv_bin / "python3"
    # A python3 stub that succeeds on the PIL/pytz/requests import check
    python3.write_text("#!/bin/bash\nexit 0\n")
    python3.chmod(0o755)
    pip = venv_bin / "pip"
    pip.write_text("#!/bin/bash\nexit 0\n")
    pip.chmod(0o755)

    # Fake requirements.txt + env.sh + env.sh.sample
    (install / "requirements.txt").write_text("pillow==12.2.0\n")
    (install / "env.sh").write_text("export OPENWEATHERMAP_APIKEY=my-key\nexport WEATHER_UNITS=imperial\n")
    (install / "env.sh.sample").write_text(
        "export OPENWEATHERMAP_APIKEY=\nexport WEATHER_UNITS=imperial\nexport NEW_VAR=default\n"
    )

    # Empty systemd dir so the loop exits cleanly
    (install / "systemd").mkdir()

    # Scripts dir with the update.sh copy we're running + chmod target
    scripts = install / "scripts"
    scripts.mkdir()
    (scripts / "placeholder.sh").write_text("#!/bin/bash\n")


class TestUpdateScriptExecution:
    """Run update.sh in a sandbox and verify subprocess orchestration."""

    def _install_stubs(self, sandbox):
        """Install PATH stubs for every external command update.sh invokes."""
        sandbox.stub("sudo")
        sandbox.stub("systemctl")
        # git rev-parse / status / ls-remote / fetch / reset / submodule
        # Need rev-parse to return a sha; use stdout.
        sandbox.stub("git", stdout="abc1234\n")
        # md5sum — return a predictable checksum
        sandbox.stub("md5sum", stdout="d41d8cd98f00b204e9800998ecf8427e  /tmp/x\n")

    def test_aborts_if_firstboot_marker_missing(self, script_sandbox):
        """Pre-flight: update must refuse when /etc/litclock/.setup-complete
        is absent (i.e. first-boot setup hasn't finished yet)."""
        _setup_fake_install(script_sandbox)
        self._install_stubs(script_sandbox)
        # Do NOT create the marker — guard should fire.

        result = script_sandbox.run(UPDATE_SH)

        assert result.returncode == 1
        assert "First-boot setup not yet complete" in result.stdout + result.stderr

    def test_proceeds_when_firstboot_marker_present(self, script_sandbox, tmp_path):
        """Regression for #209 hardware-found defect: after firstboot's
        Type=oneshot service exits successfully it stays in state
        active(exited) forever. The marker file is the correct signal —
        update.sh must proceed past the guard once the marker exists,
        even though `systemctl is-active` would still return true."""
        from pathlib import Path

        _setup_fake_install(script_sandbox)
        self._install_stubs(script_sandbox)

        # Redirect the marker path to a sandbox-controlled location so the
        # test doesn't need /etc/litclock to exist on the host.
        fake_etc = tmp_path / "etc" / "litclock"
        fake_etc.mkdir(parents=True)
        (fake_etc / ".setup-complete").write_text("ok\n")

        # Run a copy of update.sh whose marker path points at the fake.
        sandbox_script = script_sandbox.root / "scripts" / "update.sh"
        patched = (
            Path(UPDATE_SH)
            .read_text()
            .replace(
                "/etc/litclock/.setup-complete",
                str(fake_etc / ".setup-complete"),
            )
        )
        sandbox_script.write_text(patched)
        sandbox_script.chmod(0o755)

        # Marker present → guard must NOT trigger, even though `systemctl
        # is-active` would still return true post-firstboot. We only assert
        # the guard doesn't fire; later phases may exit non-zero in the
        # sandbox, which is fine.
        result = script_sandbox.run(str(sandbox_script))

        assert "First-boot setup not yet complete" not in (result.stdout + result.stderr), (
            "update.sh exited at the firstboot guard despite "
            "/etc/litclock/.setup-complete being present (#209 regression)"
        )

    def test_aborts_if_not_a_git_repo(self, script_sandbox):
        """Pre-flight: update must abort if INSTALL_DIR has no .git."""
        self._install_stubs(script_sandbox)
        # Do NOT create .git — but we need enough to pass the initial
        # `cd "$INSTALL_DIR"` (directory exists).
        (script_sandbox.root).mkdir(parents=True, exist_ok=True)

        result = script_sandbox.run(UPDATE_SH)

        assert result.returncode == 1
        assert "not a git repository" in result.stdout + result.stderr


# ── #209 structural tests ─────────────────────────────────────────────
# The weekly auto-update cycle adds state (LKG marker, update-failed marker)
# and a new revert path (Phase 4.5 smoke failure). Existing execution-sandbox
# tests can't cover these without a real Python venv + network, so these are
# grep-based invariants against the script source.


class TestAutoUpdateStructure:
    def test_phase1_does_not_clear_lkg_marker(self, update_sh_content):
        """LKG auto-revert (#209 follow-up) INVERTED the old invariant: Phase 1
        must NOT clear lkg-sha. The heartbeat-gated writer only replaces it once
        the NEW code paints, so retaining the old value means lkg-sha always
        points at the last code that actually rendered — a dead-on-arrival
        update can never blank the recovery target. Clearing it here reopened
        the exact DOA gap the auto-revert exists to close."""
        assert 'atomic_remove_file "$LKG_SHA_FILE"' not in update_sh_content, (
            "Phase 1 must NOT clear lkg-sha — that reopens the DOA-update gap "
            "(bootcheck would have no recovery target after a bad OTA)"
        )

    def test_rollback_mode_installs_pinned_lkg_and_pins_revert(self, update_sh_content):
        """Rollback mode: when bootcheck writes rollback-target, update.sh must
        install THAT SHA (not the latest Release) and pin REVERT_SHA to it so a
        pip/smoke failure stays on the LKG instead of falling back to the bad
        code it is fleeing."""
        assert "ROLLBACK_TARGET_FILE" in update_sh_content
        assert 'REVERT_SHA="$_rb"' in update_sh_content
        # Both failure-path reverts must use REVERT_SHA, never a bare OLD_SHA.
        assert update_sh_content.count('git reset --hard "$REVERT_SHA"') >= 2, (
            "both the pip-fail and smoke-fail reverts must use REVERT_SHA"
        )
        assert 'git reset --hard "$OLD_SHA"' not in update_sh_content, (
            "no failure path may revert to the bad OLD_SHA in rollback mode"
        )

    def test_blocked_sha_suppresses_reinstall(self, update_sh_content):
        """A release SHA bootcheck reverted from must be skipped until a newer
        release supersedes it — otherwise the weekly timer re-bricks the device."""
        assert "BLOCKED_SHA_FILE" in update_sh_content
        assert '"$TARGET_SHA" == "$_blocked"' in update_sh_content

    def test_phase7_clears_bootcheck_state(self, update_sh_content):
        """A successful apply clears the failed-boot streak + consumes the
        rollback pin. Normal (non-rollback) updates also clear the recovery
        marker + blocked-sha; rollback mode keeps both."""
        assert 'atomic_remove_file "$BOOT_FAIL_COUNT_FILE"' in update_sh_content
        assert 'atomic_remove_file "$ROLLBACK_TARGET_FILE"' in update_sh_content
        assert 'if [[ "${ROLLBACK_MODE:-0}" -ne 1 ]]; then' in update_sh_content

    def test_complete_summary_rollback_hint_is_mode_aware(self, update_sh_content):
        """The 'Update Complete' summary's manual-rollback hint uses OLD_SHA,
        which in rollback mode is the BAD code just fled — printing
        `git reset --hard $OLD_SHA` there would tell the operator to re-brick
        (QA-caught). The bare hint must be guarded behind non-rollback mode."""
        summary = update_sh_content[update_sh_content.find("Update Complete") :]
        assert 'echo "  Rollback: git reset --hard $OLD_SHA"' in summary
        # ...and it must sit inside an `else` of a ROLLBACK_MODE guard, with a
        # recovery-appropriate line for the rollback branch.
        assert "Recovered to last-known-good" in summary
        hint_idx = summary.find("Rollback: git reset --hard $OLD_SHA")
        guard_idx = summary.find('if [[ "${ROLLBACK_MODE:-0}" -eq 1 ]]; then')
        assert guard_idx != -1 and guard_idx < hint_idx, "rollback hint must be mode-guarded"

    def test_smoke_test_between_phase4_and_phase5(self, update_sh_content):
        """Smoke test MUST run AFTER venv rebuild (Phase 4) and BEFORE
        systemd unit sync (Phase 5). Per plan-eng-review decision A2 — this
        ordering catches new-dependency import failures, not just code
        regressions."""
        phase4_idx = update_sh_content.find("Phase 4: Update Python packages")
        phase4_5_idx = update_sh_content.find("Phase 4.5: Post-rebuild smoke test")
        phase5_idx = update_sh_content.find("Phase 5: Update systemd units")
        assert phase4_idx != -1
        assert phase4_5_idx != -1, "Phase 4.5 header missing — smoke test must be explicit"
        assert phase5_idx != -1
        assert phase4_idx < phase4_5_idx < phase5_idx, (
            "Smoke test must run after Phase 4 (venv rebuild) and before Phase 5 (systemd)"
        )

    def test_smoke_invokes_dry_run_with_timeout(self, update_sh_content):
        """Smoke test MUST use `timeout 60` and `--dry-run` so a hung render
        doesn't wedge the unit. MUST also invoke literary_clock.py
        script-style (matching runtheclock.sh), NOT module-style
        (`python -m src.literary_clock`) — the file uses absolute `from log
        import setup_logging` which only resolves when Python adds src/ to
        sys.path (script-style does this automatically). Module-style
        invocation hits ModuleNotFoundError on every device, reverts every
        update, and pins the fleet (#209 hardware-found regression)."""
        assert 'timeout 60 "$PYTHON" src/literary_clock.py --dry-run' in update_sh_content, (
            "smoke test must invoke literary_clock.py script-style (matching "
            "runtheclock.sh), not via `python -m src.literary_clock`"
        )
        # Defense in depth: the broken module-style invocation must NOT
        # creep back in.
        assert "-m src.literary_clock" not in update_sh_content, (
            "smoke test must NOT use module-style invocation — breaks the "
            "absolute `from log import` resolution. See #209 hardware QA."
        )

    def test_smoke_failure_reverts_head_and_wipes_hash(self, update_sh_content):
        """On smoke-test failure, revert to REVERT_SHA AND delete pip hash file
        so the next run re-rebuilds the venv from scratch. Leaving the hash
        in place would mean the partially-updated venv appears clean to
        Phase 4's hash gate on the next timer fire.

        REVERT_SHA == OLD_SHA in a normal update; in bootcheck rollback mode it
        is the LKG target, so a failure stays on the last-known-good rather than
        the bad code (#209 LKG auto-revert)."""
        # Locate the smoke-fail branch.
        fail_idx = update_sh_content.find("Smoke test failed")
        assert fail_idx != -1, "smoke-fail branch must exist"
        # Within the fail branch, both reset-to-REVERT_SHA and hash-delete
        # must appear before the exit 1.
        exit_idx = update_sh_content.find("exit 1", fail_idx)
        assert exit_idx != -1, "smoke-fail branch must exit non-zero"
        fail_block = update_sh_content[fail_idx:exit_idx]
        assert 'git reset --hard "$REVERT_SHA"' in fail_block, "smoke-fail branch must revert git to REVERT_SHA"
        assert 'rm -f "$HASH_FILE"' in fail_block, "smoke-fail branch must delete the pip hash so next run rebuilds"
        assert "$UPDATE_FAILED_FILE" in fail_block, (
            "smoke-fail branch must write the update-failed marker for the e-ink glyph"
        )

    def test_smoke_pass_clears_update_failed_marker(self, update_sh_content):
        """Every successful smoke test clears /var/lib/litclock/update-failed
        so the corner glyph goes away on the next clock render."""
        pass_idx = update_sh_content.find("Smoke test passed")
        fail_idx = update_sh_content.find("Smoke test failed")
        assert pass_idx != -1 and fail_idx != -1
        pass_block = update_sh_content[pass_idx:fail_idx]
        assert 'atomic_remove_file "$UPDATE_FAILED_FILE"' in pass_block, (
            "smoke-pass branch must clear the update-failed marker"
        )

    def test_pip_install_failure_reverts_and_exits(self, update_sh_content):
        """#324 + codex adversarial review of PR #349: when pip install
        fails, update.sh must revert the git tree, wipe the pip hash, write
        the update-failed marker, and exit non-zero — NEVER fall through to
        Phase 4.5 with a partially-upgraded venv on the new SHA.

        Codex review (HIGH) caught that the original PR's parallel to the
        smoke-failure branch was naive: smoke fires AFTER a successful pip
        install (venv known-good for OLD_SHA after `git reset`), whereas pip
        failure leaves the venv in an indeterminate state (some packages
        upgraded, others not, mid-stream). Reverting git doesn't restore
        the venv — and we cannot cheaply force-reinstall without risking
        another failure. So the terminal status from this branch must be
        `failed_unrecovered` (manual recovery needed), NOT `failed_reverted`
        (which lies about the venv being back to a known-good state).
        """
        fail_idx = update_sh_content.find('log_error "pip install failed')
        assert fail_idx != -1, "pip-install failure branch must exist"
        assert "reverting code to $REVERT_SHA" in update_sh_content[fail_idx : fail_idx + 300], (
            "pip-install failure must log a revert, not just a will-retry message (#324)"
        )

        # The branch must terminate with exit 1 BEFORE Phase 4.5 smoke runs.
        exit_idx = update_sh_content.find("exit 1", fail_idx)
        phase4_5_idx = update_sh_content.find("Phase 4.5: Post-rebuild smoke test")
        assert exit_idx != -1, "pip-install failure branch must exit non-zero"
        assert phase4_5_idx != -1, "Phase 4.5 heading missing"
        assert exit_idx < phase4_5_idx, (
            "pip-install failure must exit BEFORE Phase 4.5 smoke runs (#324) — otherwise the "
            "partial venv proceeds against smoke, which can pass on dep gaps it never imports"
        )

        fail_block = update_sh_content[fail_idx:exit_idx]
        assert 'git reset --hard "$REVERT_SHA"' in fail_block, (
            "pip-install failure branch must revert git to REVERT_SHA "
            "(== OLD_SHA normally; the LKG target in bootcheck rollback mode) (#324)"
        )
        assert 'rm -f "$HASH_FILE"' in fail_block, (
            "pip-install failure branch must delete the pip hash so the next run re-attempts pip install (#324)"
        )
        assert "$UPDATE_FAILED_FILE" in fail_block, (
            "pip-install failure branch must write the update-failed marker for the e-ink glyph (#324)"
        )

        # Codex Finding 1 (HIGH): terminal status MUST be failed_unrecovered,
        # not failed_reverted — venv state is indeterminate after a
        # mid-stream pip failure, and reverting git does not restore packages.
        assert "update_status_failed_unrecovered" in fail_block, (
            "pip-install failure must record terminal status as failed_unrecovered — venv state "
            "is indeterminate after mid-stream pip failure, so failed_reverted would lie about "
            "the clock being back on a known-good state (codex Finding 1 / #324)"
        )
        assert "update_status_failed_reverted" not in fail_block, (
            "pip-install failure must NOT call update_status_failed_reverted — the venv could "
            "be in any state between 'fully upgraded' and 'fully on OLD_SHA pins'; failed_reverted "
            "would mis-paint the PWA as 'clock fine on previous SHA' (codex Finding 1)"
        )

        # Codex Finding 4 (LOW/MED): _LITCLOCK_UPDATE_FINALIZED=1 must be
        # set BEFORE the status write. If we set it after and the status
        # write itself fails (jq missing, disk full, fs read-only), the
        # EXIT trap would not arm and update.status would be stuck at
        # `running`. Setting it first lets the trap's failed_unrecovered
        # fallback still fire on a status-write crash.
        assert "_LITCLOCK_UPDATE_FINALIZED=1" in fail_block, (
            "pip-install failure must set _LITCLOCK_UPDATE_FINALIZED=1 (#324)"
        )
        finalized_idx = fail_block.find("_LITCLOCK_UPDATE_FINALIZED=1")
        status_write_idx = fail_block.find("update_status_failed_unrecovered")
        assert finalized_idx != -1 and status_write_idx != -1
        # The duplicate ordering check below is also covered by the dedicated
        # test_pip_install_failure_finalized_before_status_write test; keep
        # both so a future refactor that moves either line trips two tests.
        assert finalized_idx < status_write_idx, (
            "_LITCLOCK_UPDATE_FINALIZED=1 must be set BEFORE the update_status_failed_unrecovered "
            "call (codex Finding 4) — see test_pip_install_failure_finalized_before_status_write "
            "for the full rationale."
        )

        # Codex Finding 2 (MED): rollback exit codes must be captured.
        # `|| true` swallowed git reset / submodule update failures, so the
        # script would record failed_reverted (now: failed_unrecovered) even
        # when the revert itself failed. Track REVERT_OK to distinguish.
        assert "REVERT_OK" in fail_block, (
            "pip-install failure branch must capture revert exit codes via REVERT_OK so we can "
            "tell 'code reverted, venv uncertain' from 'revert itself failed' (codex Finding 2)"
        )

        # The "|| true" patterns from the original commit must be gone —
        # they swallowed real failure signal. The branch should rely on
        # explicit if-tests + PIPESTATUS instead.
        assert "git reset --hard \"$OLD_SHA\" 2>&1 | sed 's/^/[revert] /' || true" not in fail_block, (
            "rollback `git reset` must NOT use `|| true` — that swallows revert failures and "
            "lets the script lie about state (codex Finding 2)"
        )

    def test_pip_install_failure_finalized_before_status_write(self, update_sh_content):
        """Codex Finding 4 (LOW/MED): _LITCLOCK_UPDATE_FINALIZED=1 must be
        set BEFORE update_status_failed_unrecovered in the pip-install branch.

        If the status write itself throws (jq missing, disk full, fs read-
        only), setting FINALIZED=1 first ensures the EXIT trap still has a
        well-defined fallback path. Setting it AFTER means a failed status
        write would silently disarm the trap and update.status would stay
        stuck at `running` forever.
        """
        fail_idx = update_sh_content.find('log_error "pip install failed')
        exit_idx = update_sh_content.find("exit 1", fail_idx)
        assert fail_idx != -1 and exit_idx != -1
        fail_block = update_sh_content[fail_idx:exit_idx]

        finalized_idx = fail_block.find("_LITCLOCK_UPDATE_FINALIZED=1")
        unrecovered_idx = fail_block.find("update_status_failed_unrecovered")
        assert finalized_idx != -1, "FINALIZED=1 must appear in pip-fail branch"
        assert unrecovered_idx != -1, "failed_unrecovered call must appear in pip-fail branch"
        assert finalized_idx < unrecovered_idx, (
            "_LITCLOCK_UPDATE_FINALIZED=1 must be set BEFORE the status_failed write — "
            "otherwise a status-write crash silently disarms the EXIT trap (codex Finding 4)"
        )

    def test_pip_install_failure_comment_honest_about_hash_delete(self, update_sh_content):
        """Codex Finding 3 (cosmetic): the original PR's comment said the
        hash delete makes the next run 'rebuild the venv from scratch',
        which is wrong — `rm -f $HASH_FILE` only forces the next Phase 4
        to re-attempt pip install against the (now OLD_SHA) requirements;
        it does NOT trigger a full venv rebuild. Fix the comment to match
        actual semantics.
        """
        fail_idx = update_sh_content.find('log_error "pip install failed')
        exit_idx = update_sh_content.find("exit 1", fail_idx)
        assert fail_idx != -1 and exit_idx != -1
        fail_block = update_sh_content[fail_idx:exit_idx]

        # The "from scratch" wording specifically should not survive in
        # the pip-fail branch — it overstates what rm -f HASH_FILE does.
        # The smoke-fail branch still uses similar wording but that one
        # also runs against a known-good post-install venv, so semantics
        # differ; we only assert against the pip-fail block.
        assert "re-rebuilds the venv from scratch" not in fail_block, (
            "pip-install failure branch must not claim hash-delete rebuilds the venv from "
            "scratch — rm -f HASH_FILE only forces the next Phase 4 to re-attempt pip install "
            "against OLD_SHA's requirements (codex Finding 3)"
        )

    def test_resolver_gracefully_offline_skips_git_fetch(self, update_sh_content):
        """When resolver returns empty (offline), update.sh must exit 0
        WITHOUT the subsequent `git fetch origin master` + reset. Otherwise
        a pinned-SHA device that happens to have lost network would still
        silently pull origin/master on every tick."""
        # The offline branch must restart the timer and exit 0.
        import re

        pattern = re.compile(
            r"Could not resolve a blessed Release SHA.*?systemctl start litclock\.timer.*?exit 0",
            re.DOTALL,
        )
        assert pattern.search(update_sh_content), (
            "offline branch must restart litclock.timer and exit 0 without touching git"
        )

    def test_resolver_uses_tag_name_not_target_commitish(self, update_sh_content):
        """Eng-review A1: target_commitish is unreliable (branch name for
        tag-push releases). Resolver must NOT read it functionally — we
        allow mentions in comments (to warn the next maintainer why)."""
        # Strip comment lines; any remaining hit is a functional use.
        non_comment = "\n".join(ln for ln in update_sh_content.splitlines() if not ln.lstrip().startswith("#"))
        assert "target_commitish" not in non_comment, (
            "target_commitish is unreliable; resolver must use tag_name + git rev-list"
        )

    def test_target_sha_is_validated_as_hex40(self, update_sh_content):
        """Injection defense: resolver output must be validated as a 40-char
        hex SHA before it reaches `git reset --hard`."""
        assert "[0-9a-f]{40}" in update_sh_content, "resolver output must be validated as a 40-char hex SHA"

    def test_atomic_write_uses_tmp_rename(self, update_sh_content):
        """State writes to /var/lib/litclock/ must use .tmp + mv pattern so
        an SD power-cycle mid-write can never leave a half-written marker.

        Post-#241 the helper lives in scripts/lib/state.sh — verify the
        definition there and that update.sh sources the lib."""
        import re
        from pathlib import Path

        assert "lib/state.sh" in update_sh_content, "update.sh must source the shared atomic helpers"
        state_lib = Path(__file__).resolve().parent.parent / "scripts" / "lib" / "state.sh"
        assert state_lib.exists(), "scripts/lib/state.sh must exist"
        lib_text = state_lib.read_text()
        fn_body = re.search(
            r"atomic_write_file\(\) \{(.*?)^\}",
            lib_text,
            re.DOTALL | re.MULTILINE,
        )
        assert fn_body is not None, "atomic_write_file must be defined in scripts/lib/state.sh"
        body = fn_body.group(1)
        assert "mktemp" in body or ".tmp." in body, "atomic write must stage to a tmp file first"
        assert "mv " in body, "atomic write must rename tmp → dest (atomic on ext4)"

    def test_update_sh_sources_github_api_lib(self, update_sh_content):
        """The resolver depends on scripts/lib/github_api.sh. Source must
        tolerate a missing lib (fresh-image runs before #209 lands)."""
        assert "lib/github_api.sh" in update_sh_content
        # Guard: the source must be conditional, not unconditional.
        import re

        assert re.search(
            r'\[\[ -f "\$_THIS_SCRIPT_DIR/lib/github_api\.sh" \]\]',
            update_sh_content,
        ), "source must be wrapped in a -f guard so legacy path still works"


class TestPersistentLastUpdateWriter:
    """#334 — update.sh writes a persistent /var/lib/litclock/last-update.json
    after update_status_complete validates. Pins the four invariants the
    eng-review plan locked in:

      1. Single-flight via flock at script entry (Tension 3)
      2. _LITCLOCK_UPDATE_FINALIZED=1 BEFORE the persist (Tension 1 — disarm
         the EXIT trap before persisting so a death between the two writes
         doesn't overwrite update.status with state=failed_unrecovered).
      3. Validate-then-cp: jq-validate state==complete + to_version match +
         finished_at_unix freshness BEFORE staging (Tension 2 — never
         promote a stale or torn file).
      4. No `cp /run/litclock/update.status …` BEFORE the FINALIZED=1 line
         (anti-regression for ordering invariant 2).
    """

    def test_flock_guard_at_script_entry(self, update_sh_content):
        """#334 Tension 3 — flock-based single-flight guard. Two concurrent
        update.sh invocations (user taps Apply + weekly timer fires) would
        race on update.status / last-update.json / lkg-sha-clear. The
        guard wraps the script body under an exclusive flock; the second
        runner exits with a clear "another update is in progress" message.

        Anti-regression: the guard MUST live near the top of the script
        (before any state mutation) and MUST use flock -n so it's
        non-blocking — a blocking flock would queue indefinitely and
        eventually trip systemd's TimeoutStartSec on the second runner."""
        # `flock` invocation lives near the top.
        flock_idx = update_sh_content.find("flock -n")
        assert flock_idx != -1, "update.sh must use `flock -n` for non-blocking single-flight"
        # And it must come before Phase 1 (any state mutation).
        phase1_idx = update_sh_content.find("Phase 1: Stop timer")
        assert phase1_idx != -1
        assert flock_idx < phase1_idx, (
            "flock guard must run BEFORE Phase 1 — otherwise a second runner could mutate "
            "shared state before discovering the first runner already owns the lock"
        )
        # Friendly message on the held-lock branch.
        assert "another update is in progress" in update_sh_content, (
            "flock-held branch must surface a clear user-facing message"
        )
        # The lock file path (overridable via env so tests can isolate).
        assert "LITCLOCK_UPDATE_LOCK_FILE" in update_sh_content, (
            "lock path must be overridable via env for sandboxed tests"
        )

    def test_flock_fallback_emits_log_warn(self, update_sh_content):
        """Review I3 — when the lock file can't be created (parent dir
        missing or unwritable, e.g. fresh-image flow or CI sandbox),
        update.sh degrades to running without serialization. That degraded
        path must emit a `log_warn` so the journal records the loss of the
        concurrency guard — silent fallback hides the regression from ops.

        Anti-regression: any future refactor that drops the log_warn
        falls back to journal-invisible degradation."""
        import re

        # The fallback path lives in the `else` branch of the
        # `[[ -e "$LITCLOCK_UPDATE_LOCK_FILE" ]]` test. Look for a
        # log_warn call mentioning "concurrency guard" in the script —
        # this is the structural signal that the fallback path logs.
        log_warn_pattern = re.compile(r'log_warn\s+"concurrency guard skipped[^"]*"')
        assert log_warn_pattern.search(update_sh_content), (
            "update.sh must log_warn when flock concurrency guard is skipped — "
            "silent fallback hides degraded mode from the journal (review I3)"
        )

    def test_finalized_set_before_persistent_write(self, update_sh_content):
        """#334 Tension 1 — `_LITCLOCK_UPDATE_FINALIZED=1` must be set
        BEFORE the persistent last-update.json write. Otherwise, an OS
        kill between update_status_complete and the persist would let
        the EXIT trap overwrite update.status with state=failed_unrecovered,
        leaving the persistent file pointing at a "successful" update
        the volatile file disagrees with on the next reboot.

        Pins: the FINALIZED=1 line must come BEFORE any reference to
        $LAST_UPDATE_FILE."""
        finalized_idx = update_sh_content.find("_LITCLOCK_UPDATE_FINALIZED=1")
        assert finalized_idx != -1, "missing _LITCLOCK_UPDATE_FINALIZED=1 disarm-trap line"
        last_update_ref_idx = update_sh_content.find("$LAST_UPDATE_FILE")
        # The variable definition itself can come earlier; we care about the
        # USE inside the persist function. Find the first use AFTER the
        # variable definition.
        var_def_idx = update_sh_content.find('LAST_UPDATE_FILE="')
        assert var_def_idx != -1, "LAST_UPDATE_FILE variable must be defined"
        assert last_update_ref_idx != -1, "no $LAST_UPDATE_FILE references in update.sh"
        # First $-expansion (use, not definition) of LAST_UPDATE_FILE must be
        # AFTER the FINALIZED=1 line. We scan from finalized_idx onward.
        use_after_finalized = update_sh_content.find("$LAST_UPDATE_FILE", finalized_idx)
        assert use_after_finalized != -1, (
            "no use of $LAST_UPDATE_FILE after _LITCLOCK_UPDATE_FINALIZED=1 — "
            "persistent write must follow the trap-disarm"
        )

    def test_validate_then_cp_uses_jq_with_to_version_and_freshness(self, update_sh_content):
        """#334 Tension 2 — before staging the persistent file, update.sh
        must read /run/litclock/update.status back through jq -e and
        validate that state == "complete" AND to_version matches the
        just-installed SHA AND finished_at_unix is fresh (within the
        last 60s). Pins the schema agreement with update_status_complete
        without re-implementing it."""
        # The validate-then-cp filter must reference all three fields.
        # We're loose on whitespace — any jq invocation that mentions
        # all three is the right shape.
        finalized_idx = update_sh_content.find("_LITCLOCK_UPDATE_FINALIZED=1")
        assert finalized_idx != -1
        block = update_sh_content[finalized_idx:]
        assert ".state ==" in block, "validate-then-cp must check .state"
        assert ".to_version ==" in block, "validate-then-cp must check .to_version matches new SHA"
        assert ".finished_at_unix" in block, "validate-then-cp must check .finished_at_unix freshness"
        assert "jq -e" in block, "validate-then-cp must use jq -e for the boolean exit code"
        # And it must reference $NEW_SHA — that's the just-installed target.
        assert "$NEW_SHA" in block or "expected" in block, (
            "validate-then-cp must compare to_version against the just-installed SHA ($NEW_SHA)"
        )

    def test_no_cp_of_update_status_before_finalized(self, update_sh_content):
        """Anti-regression for the ordering invariant: NO `cp` of
        /run/litclock/update.status MAY appear BEFORE the FINALIZED=1
        line. The persist is the only legitimate cp of update.status,
        and it must follow the trap-disarm.

        Catches the bug-shape where a future refactor moves the persist
        function above the FINALIZED=1 line and reintroduces the
        Tension 1 race."""
        finalized_idx = update_sh_content.find("_LITCLOCK_UPDATE_FINALIZED=1")
        assert finalized_idx != -1
        # Scan everything BEFORE the FINALIZED line for any cp of update.status
        # or $LITCLOCK_UPDATE_STATUS_FILE or $status_file (the local var inside
        # the persist function). Comments are allowed.
        before = update_sh_content[:finalized_idx]
        for lineno, line in enumerate(before.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            # Look for a cp invocation that targets update.status or its
            # variable. This grep has zero false positives because
            # update.sh has no other reason to cp to/from those paths.
            assert 'cp "$LITCLOCK_UPDATE_STATUS_FILE"' not in line, (
                f"update.sh:{lineno} cp of $LITCLOCK_UPDATE_STATUS_FILE before "
                f"_LITCLOCK_UPDATE_FINALIZED=1 — would trip Tension 1 race"
            )
            assert 'cp "$status_file"' not in line, (
                f"update.sh:{lineno} cp of $status_file (persist local) before "
                f"_LITCLOCK_UPDATE_FINALIZED=1 — would trip Tension 1 race"
            )

    def test_persistent_write_uses_atomic_mv_tmp(self, update_sh_content):
        """The persistent file write must use the same .tmp + mv idiom
        as _write_status_json — ext4 mv is atomic on the same filesystem,
        so an SD power-cycle mid-write can never leave a torn last-update.json."""
        finalized_idx = update_sh_content.find("_LITCLOCK_UPDATE_FINALIZED=1")
        block = update_sh_content[finalized_idx:]
        assert ".tmp." in block, "persistent write must stage to a .tmp file"
        # mv from the staged tmp into LAST_UPDATE_FILE.
        assert 'mv "$tmp" "$LAST_UPDATE_FILE"' in block or "mv $tmp $LAST_UPDATE_FILE" in block, (
            "persistent write must atomic-rename tmp → $LAST_UPDATE_FILE"
        )

    def test_freshness_window_widened_for_late_ntp(self, update_sh_content):
        """#342 I2 — the validate-then-cp gate's freshness floor must be
        widened to 3600s so a Pi Zero 2W cold-boot update fired before
        chrony has synced (timestamp lands pre-1970 relative to real-2026)
        doesn't silently lose the persistent last-update.json write.
        Pre-fix: now-60 floor rejected pre-NTP timestamps → persist
        skipped → next reboot's Status row em-dashed."""
        finalized_idx = update_sh_content.find("_LITCLOCK_UPDATE_FINALIZED=1")
        assert finalized_idx != -1
        block = update_sh_content[finalized_idx:]
        # The floor is computed via `now_unix - <seconds>`. Pin the value.
        assert "now_unix - 3600" in block, (
            "freshness floor must be 3600s (widened from 60s in #342 I2) — narrower "
            "windows lose the persist write on cold-boot updates that race chrony"
        )
        # And the prior tight 60s window must NOT linger.
        assert "now_unix - 60)" not in block, (
            "old 60s freshness floor must be gone — leftover 60s would defeat the I2 widening"
        )

    def test_persist_sweeps_orphan_tmp_files(self, update_sh_content):
        """#342 I4 — orphan sweep before staging the new .tmp.<pid> file.
        Mirrors the manifest-sweep pattern from PR #293: if a prior persist
        died between staging and mv (disk full, OOM, kill -9), the .tmp.<pid>
        sibling lingers. Sweep on the next persist entry keeps
        /var/lib/litclock clean over time."""
        finalized_idx = update_sh_content.find("_LITCLOCK_UPDATE_FINALIZED=1")
        assert finalized_idx != -1
        block = update_sh_content[finalized_idx:]
        # The sweep is a single rm -f glob on the .tmp.* siblings.
        assert 'rm -f "${LAST_UPDATE_FILE}.tmp."*' in block, (
            "persist function must rm -f orphan ${LAST_UPDATE_FILE}.tmp.* siblings before staging"
        )
        # The sweep must run BEFORE the .tmp.$$ stage assignment. Otherwise
        # we'd sweep our own pending stage file out of existence.
        sweep_idx = block.find('rm -f "${LAST_UPDATE_FILE}.tmp."*')
        stage_idx = block.find('tmp="${LAST_UPDATE_FILE}.tmp.$$"')
        assert sweep_idx != -1
        assert stage_idx != -1
        assert sweep_idx < stage_idx, (
            "orphan sweep must run BEFORE staging the current persist's tmp file — "
            "otherwise the sweep would delete the in-flight stage"
        )


class TestPhase3SkipMarker:
    """#274 follow-up #5 — Phase 3 flock-timeout marker.

    When `with_env_lock` returns rc=75 (env.sh sidecar held > 30s),
    update.sh skips the env.sh.sample merge but the skip itself isn't
    visible anywhere persistent — journalctl rotates, and the next
    successful update.sh run leaves no record that the prior run
    deferred Phase 3.

    The marker file at /var/lib/litclock/update-phase3-skipped is
    mtime-only (zero-byte). The PWA Status hero reads it and renders
    a banner when fresh. Cleared on the next clean Phase 3 run, so
    the banner self-clears without a manual ack.

    Two structural invariants pinned:
      1. On rc=75, the marker is written via atomic_write_file.
      2. On any non-rc=75 outcome (Phase 3 ran cleanly OR no sample
         vars to merge), the marker is removed via atomic_remove_file.
    """

    def test_marker_path_defined(self, update_sh_content):
        """The PHASE3_SKIPPED_FILE constant must be defined alongside the
        other STATE_DIR file constants so a future refactor that renames
        STATE_DIR drags it along."""
        assert "PHASE3_SKIPPED_FILE=" in update_sh_content
        # Anchored to STATE_DIR so test-env LITCLOCK_STATE_DIR override
        # cascades to the marker too.
        assert 'PHASE3_SKIPPED_FILE="$STATE_DIR/update-phase3-skipped"' in update_sh_content, (
            'PHASE3_SKIPPED_FILE must be "$STATE_DIR/update-phase3-skipped" — '
            "tests override STATE_DIR via LITCLOCK_STATE_DIR; hardcoding "
            "/var/lib/litclock would break the override"
        )

    def test_marker_written_on_rc75(self, update_sh_content):
        """On Phase 3 flock timeout (rc=75), update.sh must write the
        marker via atomic_write_file. Without this the PWA Status hero
        has no surface to show that env-vars merge was skipped."""
        # Find the rc=75 branch in Phase 3 — the only rc=75 check that
        # falls inside the "if _phase3_rc == 75" branch.
        rc75_idx = update_sh_content.find('if [[ "$_phase3_rc" == "75" ]]')
        assert rc75_idx != -1, "Phase 3 rc=75 branch missing — did update.sh refactor?"
        # Scope to the body of the if/elif/else, ending at the next
        # `unset _phase3_rc` which terminates the Phase 3 block.
        block_end = update_sh_content.find("unset _phase3_rc", rc75_idx)
        assert block_end != -1
        block = update_sh_content[rc75_idx:block_end]
        assert 'atomic_write_file "$PHASE3_SKIPPED_FILE"' in block, (
            "Phase 3 rc=75 branch must write the skip marker via "
            "atomic_write_file so the PWA Status banner can surface "
            "the skipped env-vars merge"
        )

    def test_marker_cleared_outside_inner_rc75_branch(self, update_sh_content):
        """Adversarial-review P1 fix — the atomic_remove_file call must
        sit OUTSIDE the inner rc=75 if/elif so any Phase 3 outcome that
        ISN'T a flock-timeout clears a stale marker from a prior run.

        Original placement (inside the `else` arm of the rc=75 check)
        missed:
          1. The `elif [[ ! -f "$INSTALL_DIR/env.sh" ]]` (first-boot
             copy-from-sample) path — no merge happens but a stale
             prior-run marker should still clear.
          2. The degenerate "neither env.sh nor env.sh.sample" no-op
             path — Phase 3 doesn't run at all but a prior-run marker
             still hangs around for the full 24h freshness window.

        The hoisted clear lives after the outer if/elif/else so every
        path through Phase 3 self-clears unless we just wrote the marker
        on the rc=75 path this run (tracked via the
        `_phase3_marker_just_written` flag).
        """
        # The clear must sit AFTER `unset _phase3_rc` (which terminates
        # the rc=75 conditional) and BEFORE `rm -f "$_PHASE3_ADDED_FILE"`
        # (which terminates the Phase 3 block).
        rc75_idx = update_sh_content.find('if [[ "$_phase3_rc" == "75" ]]')
        assert rc75_idx != -1
        unset_rc_idx = update_sh_content.find("unset _phase3_rc", rc75_idx)
        added_file_cleanup_idx = update_sh_content.find('rm -f "$_PHASE3_ADDED_FILE"', unset_rc_idx)
        assert unset_rc_idx != -1 and added_file_cleanup_idx != -1
        post_block = update_sh_content[unset_rc_idx:added_file_cleanup_idx]
        assert 'atomic_remove_file "$PHASE3_SKIPPED_FILE"' in post_block, (
            "atomic_remove_file for PHASE3_SKIPPED_FILE must be hoisted "
            "outside the inner rc=75 branch so the elif (first-boot copy) "
            "and the no-op path both self-clear a stale prior-run marker"
        )

    def test_marker_clear_guarded_by_just_written_flag(self, update_sh_content):
        """The hoisted clear must NOT fire on the rc=75 path or we'd
        write-then-clear the marker on the same run, racing the PWA's
        15-second poll. Gate the clear on a `_phase3_marker_just_written`
        flag set inside the rc=75 branch, checked outside the outer if.
        """
        assert "_phase3_marker_just_written=0" in update_sh_content, (
            "flag must default to 0 so non-rc=75 paths clear the marker"
        )
        assert "_phase3_marker_just_written=1" in update_sh_content, (
            "flag must be set to 1 inside the rc=75 if-branch so the "
            "outer clear is skipped on the run that wrote the marker"
        )
        rc75_idx = update_sh_content.find('if [[ "$_phase3_rc" == "75" ]]')
        unset_rc_idx = update_sh_content.find("unset _phase3_rc", rc75_idx)
        rc75_branch = update_sh_content[rc75_idx:unset_rc_idx]
        assert "_phase3_marker_just_written=1" in rc75_branch, (
            "set-to-1 must be inside the rc=75 if-branch — outside it would mean every Phase 3 run keeps the marker"
        )
        added_file_cleanup_idx = update_sh_content.find('rm -f "$_PHASE3_ADDED_FILE"', unset_rc_idx)
        post_block = update_sh_content[unset_rc_idx:added_file_cleanup_idx]
        assert '"$_phase3_marker_just_written" == "0"' in post_block, (
            "hoisted clear must gate on the flag — without the gate, "
            "the rc=75 path would write-then-immediately-clear the marker"
        )

    def test_marker_not_written_on_elif_first_boot_path(self, update_sh_content):
        """Structural: the `elif [[ ! -f "$INSTALL_DIR/env.sh" ]]` first-
        boot copy-from-sample path must NOT contain the marker write —
        only the rc=75 inner-branch does. The hoisted clear after the
        outer if handles the stale-marker case for this path.
        """
        elif_idx = update_sh_content.find('elif [[ ! -f "$INSTALL_DIR/env.sh" ]]')
        assert elif_idx != -1, "first-boot elif branch missing from Phase 3"
        next_section = update_sh_content.find("_phase3_marker_just_written", elif_idx)
        assert next_section != -1
        elif_body = update_sh_content[elif_idx:next_section]
        assert 'atomic_write_file "$PHASE3_SKIPPED_FILE"' not in elif_body, (
            "elif branch must not write the skip marker — the marker only writes on the rc=75 flock-timeout path"
        )


def test_migrates_handoff_complete_for_existing_devices():
    """EPIC #383 PR2 (#388) Option-A migration: the upgraded litclock.service
    is gated on .handoff-complete, which pre-PR2 devices never wrote. update.sh
    must touch it (after confirming .setup-complete) so quotes don't stop on
    upgrade. Without this, a Pi glued in its case would go dark."""
    src = UPDATE_SH.read_text()
    assert "/etc/litclock/.handoff-complete" in src
    assert "sudo touch /etc/litclock/.handoff-complete" in src
